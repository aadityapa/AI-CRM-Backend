"""Resume services: text extraction + rule-based ATS scoring.

Scoring weights (out of 100):
  - Mandatory skills pool ..... 50 (proportional to matched/total mandatory)
  - Optional skills pool ...... 20 (proportional)
  - Experience match .......... 15 (detected years within [experience_min, experience_max])
  - Location match ............  5 (requirement location city appears in text)
  - Education keywords ........ 10 (any recognised degree keyword)
An empty pool / missing constraint awards its full points (nothing to fail).
"""
from __future__ import annotations

import logging

import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import AtsStatus, Candidate, Location, Requirement, RequirementSkill, Resume, Skill
from models.ai_links import hr_decision_label
from services.crm_common import resolve_crm_file
from services.report_links import ai_report_link

logger = logging.getLogger("karnex.crm.ats")

#: Which OpenAI key pool the ATS resume↔JD review draws on. Its own purpose (not
#: "eval") so scan-all bursts have their own spend line and rate limit, and can
#: never exhaust the quota a live interview is relying on. Resolution order is
#: OPENAI_ATS_API_KEY / OPENAI_API_KEY_ATS -> the eval key -> OPENAI_API_KEY.
ATS_OPENAI_PURPOSE = "ats"


#: How the final ATS score is blended when the AI review is available (6 Oct
#: 2026, user report: a CV the ATS Scoring page rated 75 read 45.84 on Applied
#: Candidates). The keyword score is literal — a JD phrase the CV words
#: differently scores nothing — so the reviewer that reads the CV the way a
#: recruiter does now carries the larger share; the keyword score stays as the
#: evidence-backed floor.
KEYWORD_SHARE = 0.4
AI_SHARE = 0.6

#: Bumped whenever the scoring rules change; a Scored row below it is
#: `ats_outdated` and "Score N" on Applied Candidates re-scores it.
ATS_SCORE_VERSION = 2


def _ai_semantic_review(jd_text: str | None, mandatory: list[str], optional: list[str],
                        resume_text: str, facts: dict | None = None,
                        band: tuple | None = None) -> dict | None:
    """OpenAI semantic assessment of resume↔role fit (returns None when no key /
    on any failure — the deterministic score always stands on its own).

    Unlike keyword matching, this understands synonyms, related tech and context
    (e.g. 'AUTOSAR stack work' implies embedded C), so the final score reflects
    real fit rather than literal word overlap. The facts TA typed for this
    application (experience, locations) are given to it, since a Naukri-style CV
    often states neither in words."""
    try:
        from openai_client import get_openai_client, openai_key_configured
        if not openai_key_configured(ATS_OPENAI_PURPOSE):
            # Surface WHY rather than silently returning the keyword-only score.
            # Without this the blend just vanishes and the number looks wrong with
            # no explanation anywhere in the UI.
            return {"unavailable": "No OpenAI key configured for ATS "
                                   "(set OPENAI_API_KEY_ATS) — score is "
                                   "keyword-only, which under-rates candidates "
                                   "whose wording differs from the JD."}
        import json as _json
        skills_line = ", ".join(mandatory) or "—"
        opt_line = ", ".join(optional) or "—"
        facts = facts or {}
        fact_lines = []
        if band and (band[0] is not None or band[1] is not None):
            fact_lines.append(f"Experience the role asks for: {band[0] if band[0] is not None else '—'}"
                              f"–{band[1] if band[1] is not None else '—'} years")
        if facts.get("experience_years") is not None:
            fact_lines.append(f"Candidate's total experience (confirmed by the recruiter): "
                              f"{facts['experience_years']} years")
        if facts.get("locations"):
            fact_lines.append("Candidate's current / preferred locations: " + ", ".join(facts["locations"]))
        prompt = (
            "You are an experienced technical recruiter screening a CV for a role.\n"
            "Score how well the candidate matches the job, the way a recruiter would.\n"
            f"Required skills: {skills_line}\nNice-to-have skills: {opt_line}\n"
            + ("\n".join(fact_lines) + "\n" if fact_lines else "")
            + (f"Job description:\n{(jd_text or '')[:6000]}\n" if (jd_text or '').strip() else "")
            + f"\nRESUME:\n{resume_text[:9000]}\n\n"
            "Consider synonyms, related technologies, the domain and actual project evidence — "
            "not just literal keyword matches. Do not reward keyword stuffing. Return ONLY JSON: "
            '{"match_percent": 0-100, "summary": "2-3 sentence fit assessment", '
            '"strengths": ["..."], "gaps": ["..."]}'
        )
        # Tracked (29 Sep 2026) so the ATS review shows in AI Costs ▸ Other spend.
        from ai import _db_target
        from prompt_logger import tracked_chat_completion
        res = tracked_chat_completion(
            get_openai_client(ATS_OPENAI_PURPOSE), model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0, response_format={"type": "json_object"},
            call_type="ats_semantic_review", db_target=_db_target(),
        )
        data = _json.loads(res.choices[0].message.content or "{}")
        pct = float(data.get("match_percent"))
        if not (0 <= pct <= 100):
            return {"unavailable": f"AI returned an out-of-range score ({pct})"}
        return {
            "match_percent": round(pct, 1),
            "summary": str(data.get("summary") or "").strip()[:1000],
            "strengths": [str(s).strip() for s in (data.get("strengths") or []) if str(s).strip()][:8],
            "gaps": [str(s).strip() for s in (data.get("gaps") or []) if str(s).strip()][:8],
            "model": "gpt-4o-mini",
        }
    except Exception as exc:  # noqa: BLE001 - never block scoring on the AI call
        logger.warning("ATS semantic review failed: %s", exc)
        return {"unavailable": f"AI review failed: {type(exc).__name__}: {exc}"[:300]}

CRM_FILES_PREFIX = "/api/crm-files/"

EDUCATION_KEYWORDS = [
    "B.E", "B.Tech", "M.Tech", "BE", "BTech", "MTech",
    "BSc", "MSc", "MCA", "Bachelor", "Master", "Diploma", "PhD",
]

EXPERIENCE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years|yrs)", re.IGNORECASE)

MANDATORY_POOL = 50.0
OPTIONAL_POOL = 20.0
EXPERIENCE_POINTS = 15.0
LOCATION_POINTS = 5.0
EDUCATION_POINTS = 10.0


def _num(v):
    return float(v) if v is not None else None


def _val(v):
    return v.value if hasattr(v, "value") else v


def ats_outdated(r: Resume) -> bool:
    """A Scored row produced by older scoring rules (`ATS_SCORE_VERSION`)."""
    if _val(r.ats_status) != AtsStatus.SCORED.value:
        return False
    version = (r.ats_score_breakdown or {}).get("score_version") if isinstance(r.ats_score_breakdown, dict) else None
    return (version or 1) < ATS_SCORE_VERSION


def serialize_resume(r: Resume) -> dict:
    return {
        "id": r.id,
        "requirement_id": r.requirement_id,
        "candidate_id": r.candidate_id,
        "candidate_name": r.candidate_name,
        "email": r.email,
        "phone": r.phone,
        "source_portal": r.source_portal,
        "applicant_experience": r.applicant_experience,
        "application_details": r.application_details,
        "resume_file_url": r.resume_file_url,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "ats_score": _num(r.ats_score),
        "ats_score_breakdown": r.ats_score_breakdown,
        "ats_status": _val(r.ats_status),
        "ats_outdated": ats_outdated(r),
        "screened_by": r.screened_by,
        "ai_interview_status": _val(r.ai_interview_status),
        "ai_interview_scheduled_at": r.ai_interview_scheduled_at.isoformat() if r.ai_interview_scheduled_at else None,
        "possible_duplicate_of": r.possible_duplicate_of,
        "duplicate_dismissed": bool(getattr(r, "duplicate_dismissed", None)),
        "created_at": r.created_at.isoformat() if r.created_at else None,
        # Filled by enrich_resumes_with_ai when listing / detail-enriching
        "ai_overall_score_percent": None,
        "ai_interview_result": None,
        "ai_report_link": None,
        "ai_interview_record_id": None,
        "profile_id": None,
        "ai_invite_token": None,
        "ai_invite_url": None,
        "ai_access_key": None,
    }


def enrich_resumes_with_ai(db: Session, rows: list[Resume]) -> list[dict]:
    """Attach latest AI L1 score, report link, and invite share details from ai_interview_links."""
    from auth_db import get_schedule_by_token
    from models import AiInterviewLink
    from services.ai_interview_bridge import _legacy_db_target

    data = [serialize_resume(r) for r in rows]
    ids = [r.id for r in rows]
    if not ids:
        return data

    links = db.execute(
        select(AiInterviewLink)
        .where(AiInterviewLink.resume_id.in_(ids))
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
    ).scalars().all()
    latest: dict[int, AiInterviewLink] = {}
    for link in links:
        if link.resume_id is not None and link.resume_id not in latest:
            latest[link.resume_id] = link

    cand_ids = {link.candidate_id for link in latest.values() if link.candidate_id}
    emails_by_cand: dict[int, str] = {}
    if cand_ids:
        for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all():
            if c.email:
                emails_by_cand[c.id] = c.email.strip().lower()

    from services.invite_links import resolve_invite_base
    base = resolve_invite_base()  # settings/env, else the last request's origin (15 Sep 2026)
    access_by_token: dict[str, str] = {}
    # The SCHEDULED time lives on the legacy interview_schedule row, not on the
    # link (whose created_at is the moment the TA clicked — that is what the
    # invite panel used to print, e.g. "12:52 PM" for a 4 PM slot; 15 Sep 2026).
    scheduled_by_token: dict[str, str | None] = {}
    from services.ist import local_stamp_to_iso
    for link in latest.values():
        token = (link.invite_token or "").strip()
        if not token or token in access_by_token:
            continue
        try:
            sched = get_schedule_by_token(_legacy_db_target(), token) or {}
            access_by_token[token] = str(sched.get("access_key") or "")
            scheduled_by_token[token] = local_stamp_to_iso(sched.get("scheduled_at_local"))
        except Exception:
            access_by_token[token] = ""
            scheduled_by_token[token] = None

    # Pipeline status of each linked profile — lets the requirement's Resumes tab
    # surface "RMG review needed" (and the RMG decision actions) per row.
    from models import CandidateProfile, CandidateProfileActivityLog, PipelineStatus
    from services.crm_common import log_activity as _log_activity
    prof_ids = {link.profile_id for link in latest.values() if link.profile_id}
    passed_profiles = {link.profile_id for link in latest.values()
                       if link.profile_id and link.result == "Passed"}
    status_by_profile: dict[int, str] = {}
    if prof_ids:
        healed = False
        for p in db.execute(
            select(CandidateProfile).where(CandidateProfile.id.in_(prof_ids))
        ).scalars().all():
            status = getattr(p.pipeline_status, "value", str(p.pipeline_status))
            # Self-heal profiles whose L1 passed before the RMG hand-off existed.
            if status in (PipelineStatus.SOURCING.value,
                          PipelineStatus.TECHNICAL_SCREENING.value) and p.id in passed_profiles:
                p.pipeline_status = PipelineStatus.RMG_REVIEW
                status = PipelineStatus.RMG_REVIEW.value
                # Automatic self-heal: no acting user — log_activity falls back
                # to the system user (never a candidate id, which is a
                # different table and corrupts the audit trail).
                _log_activity(db, CandidateProfileActivityLog, "profile_id", p.id, None,
                              "STATUS_CHANGE",
                              f"{status} -> RMG_Review: AI L1 already passed — "
                              "auto-forwarded for RMG review")
                healed = True
            status_by_profile[p.id] = status
        if healed:
            db.commit()

    # L2 face-to-face state (28 Aug 2026): drives one-shot button disabling —
    # RMG's "L2 — Face-to-face" locks once requested, TA's "Schedule L2" locks
    # once a round exists. Batched: two IN-queries for the whole page.
    from models import InterviewEvent
    l2_requested_ids: set[int] = set()
    l2_scheduled_ids: set[int] = set()
    l2_meta: dict[int, dict] = {}
    if prof_ids:
        l2_requested_ids = {row[0] for row in db.execute(
            select(CandidateProfileActivityLog.profile_id)
            .where(CandidateProfileActivityLog.profile_id.in_(prof_ids),
                   CandidateProfileActivityLog.action_type == "L2_REQUESTED")
        ).all()}
        # Latest L2 round per profile (highest id wins): its id lets the RMG
        # record feedback straight from the row, its result drives the chip.
        for pid, eid, res in db.execute(
            select(InterviewEvent.profile_id, InterviewEvent.id, InterviewEvent.result)
            .where(InterviewEvent.profile_id.in_(prof_ids),
                   InterviewEvent.kind == "L2_F2F")
            .order_by(InterviewEvent.id)
        ).all():
            l2_scheduled_ids.add(pid)
            l2_meta[pid] = {"event_id": eid, "result": res or None}

    for d, r in zip(data, rows):
        link = latest.get(r.id)
        if link is None:
            continue
        d["l2_requested"] = link.profile_id in l2_requested_ids
        d["l2_scheduled"] = link.profile_id in l2_scheduled_ids
        d["l2_event_id"] = (l2_meta.get(link.profile_id) or {}).get("event_id")
        d["l2_result"] = (l2_meta.get(link.profile_id) or {}).get("result")
        d["ai_overall_score_percent"] = (
            float(link.overall_score_percent) if link.overall_score_percent is not None else None
        )
        d["ai_interview_result"] = link.result
        # The recruiter's override, when they disagreed with the AI verdict.
        # Without these the Resumes tab kept showing the raw score-threshold
        # result, so a candidate already marked Selected still read "Failed
        # 57.2%" here — the same mismatch that was fixed on the profile page.
        d["ai_hr_decision"] = link.hr_decision
        d["ai_hr_decision_label"] = hr_decision_label(link.hr_decision)
        d["ai_effective_result"] = link.effective_result
        d["ai_is_overridden"] = bool(link.hr_decision) and link.effective_result != link.result
        d["ai_interview_record_id"] = link.interview_record_id
        d["profile_id"] = link.profile_id
        d["profile_pipeline_status"] = status_by_profile.get(link.profile_id)
        # The CANDIDATE's email first (27 Aug 2026): the interview bridge
        # schedules — and the legacy interview_records row is keyed — by
        # candidate.email, not resume.email. When the two differ (shared or
        # corrected addresses) a resume-email link 404'd "Candidate not found"
        # on a report that existed.
        email = emails_by_cand.get(link.candidate_id, "") or (r.email or "").strip().lower()
        d["ai_report_link"] = ai_report_link(email, link.interview_record_id)
        token = (link.invite_token or "").strip()
        if token:
            d["ai_invite_token"] = token
            d["ai_invite_url"] = f"{base}/?invite={token}" if base else f"/?invite={token}"
            d["ai_access_key"] = access_by_token.get(token) or None
        if token and scheduled_by_token.get(token):
            d["ai_interview_scheduled_at"] = scheduled_by_token[token]
        elif link.created_at and not d.get("ai_interview_scheduled_at"):
            d["ai_interview_scheduled_at"] = link.created_at.isoformat()
    return data


#: Human interview rounds, keyed by the prefix the row fields use. The manual
#: L1 (1 Sep 2026) is the round RMG runs INSTEAD of the AI screen, so the
#: Applied Candidates row has to be able to show it exactly like the L2 —
#: requested / scheduled / result — for a candidate who has no AI link at all.
#: The activity row RMG / GM leave when they choose the AI L1 route and ask TA
#: to schedule it (the AI twin of L1_REQUESTED).
AI_L1_REQUESTED = "AI_L1_REQUESTED"

_MANUAL_ROUND_SOURCES = {
    "l1_manual": ("L1_Interview", "L1_REQUESTED"),
    "l2": ("L2_F2F", "L2_REQUESTED"),
    # Every round TA schedules (2 Sep 2026): the HR round after Sales Head's
    # approval, and the customer's own rounds. The customer rounds have no
    # "request" step — the pipeline stage is the request.
    "hr": ("HR_Interview", "HR_REQUESTED"),
    "cust_l1": ("Customer_Interview", None),
    "cust_l2": ("Customer_L2", None),
}


def manual_round_state(db: Session, profile_ids) -> dict[int, dict]:
    """Per-profile state of the internal L1/L2 rounds, batched.

    A handful of batched queries, whatever the page size. Keyed by profile because that is
    what both list paths have — the resume rows resolve theirs through the
    candidate, the profile-only rows already are one. An AI link is NOT
    required: a manually interviewed candidate never has one, and keying this
    off the link is what left those rows with no buttons at all.
    """
    ids = [int(p) for p in set(profile_ids or []) if p]
    if not ids:
        return {}
    from models import CandidateProfileActivityLog, InterviewEvent

    out: dict[int, dict] = {pid: {} for pid in ids}
    # The latest offer's terms (2 Sep 2026): the Applied Candidates tab shows
    # Sales Head what they are approving without a trip to the profile page,
    # and prefills Sales' resubmission after a send-back.
    from models import OfferHistory
    seen_offer: set[int] = set()
    for o in db.execute(
        select(OfferHistory).where(OfferHistory.profile_id.in_(ids))
        .order_by(OfferHistory.id.desc())
    ).scalars().all():
        if o.profile_id in seen_offer:
            continue
        seen_offer.add(o.profile_id)
        out[o.profile_id]["latest_offer"] = {
            "ctc": float(o.ctc) if o.ctc is not None else None,
            "joining_date": o.joining_date.isoformat() if o.joining_date else None,
            "offer_date": o.offer_date.isoformat() if o.offer_date else None,
            "status": getattr(o.status, "value", o.status),
            "rate_unit": getattr(o, "rate_unit", None),
            "rate_value": (float(o.rate_value) if getattr(o, "rate_value", None) is not None else None),
        }
    for pid in ids:
        out[pid].setdefault("latest_offer", None)
    # The rounds tally (28 Sep 2026): every interview booked for this
    # candidacy — manual, customer, HR, L3/L4 and the AI L1 — and how many
    # carry a verdict. Rounds that did not happen are not counted.
    from sqlalchemy import case, func

    from models import AiInterviewLink
    from services.interview_rounds import NOT_HELD_STATUSES
    for pid in ids:
        out[pid].update({"rounds_booked": 0, "rounds_done": 0})
    for pid, booked, done in db.execute(
        select(InterviewEvent.profile_id, func.count(InterviewEvent.id),
               func.sum(case((func.coalesce(InterviewEvent.result, "") != "", 1), else_=0)))
        .where(InterviewEvent.profile_id.in_(ids),
               func.coalesce(InterviewEvent.status, "").notin_(NOT_HELD_STATUSES))
        .group_by(InterviewEvent.profile_id)
    ).all():
        out[pid].update({"rounds_booked": int(booked or 0), "rounds_done": int(done or 0)})
    for pid, booked, done in db.execute(
        select(AiInterviewLink.profile_id, func.count(AiInterviewLink.id),
               func.sum(case((AiInterviewLink.completed_at.isnot(None), 1), else_=0)))
        .where(AiInterviewLink.profile_id.in_(ids))
        .group_by(AiInterviewLink.profile_id)
    ).all():
        # One AI L1 counts once however many links a reschedule minted.
        out[pid]["rounds_booked"] += 1 if booked else 0
        out[pid]["rounds_done"] += 1 if done else 0
    # RMG / GM chose the AI route and asked TA to schedule it (28 Sep 2026).
    ai_requested = {row[0] for row in db.execute(
        select(CandidateProfileActivityLog.profile_id)
        .where(CandidateProfileActivityLog.profile_id.in_(ids),
               CandidateProfileActivityLog.action_type == AI_L1_REQUESTED)
    ).all()}
    for pid in ids:
        out[pid]["ai_l1_requested"] = pid in ai_requested
    for prefix, (kind, request_action) in _MANUAL_ROUND_SOURCES.items():
        requested: set[int] = set()
        if request_action:
            requested = {row[0] for row in db.execute(
                select(CandidateProfileActivityLog.profile_id)
                .where(CandidateProfileActivityLog.profile_id.in_(ids),
                       CandidateProfileActivityLog.action_type == request_action)
            ).all()}
        # Latest round per profile (highest id wins): its id lets feedback be
        # recorded straight from the row, its result drives the chip.
        # A round that did not happen (cancelled, no-show, rescheduled) is not
        # "scheduled" (28 Sep 2026): the row offered no way to rebook it, and
        # the tally below already leaves it out — the two now agree.
        meta: dict[int, dict] = {}
        for pid, eid, res, when, raw_when, link, who in db.execute(
            select(InterviewEvent.profile_id, InterviewEvent.id, InterviewEvent.result,
                   InterviewEvent.scheduled_at, InterviewEvent.raw_when, InterviewEvent.meeting_link,
                   InterviewEvent.interviewer)
            .where(InterviewEvent.profile_id.in_(ids), InterviewEvent.kind == kind,
                   func.coalesce(InterviewEvent.status, "").notin_(NOT_HELD_STATUSES))
            .order_by(InterviewEvent.id)
        ).all():
            # The typed text stands in when it never parsed into a timestamp
            # (28 Sep 2026: "Technical L1 – Scheduled" showed no time at all).
            meta[pid] = {"event_id": eid, "result": res or None,
                         "when": when.isoformat() if when else ((raw_when or "").strip() or None),
                         "link": bool((link or "").strip()),
                         # Who took it (30 Sep 2026): the Status cell names the panel.
                         "interviewer": (who or "").strip() or None}
        for pid in ids:
            row = meta.get(pid) or {}
            out[pid].update({
                f"{prefix}_requested": pid in requested,
                f"{prefix}_scheduled": pid in meta,
                f"{prefix}_event_id": row.get("event_id"),
                f"{prefix}_result": row.get("result"),
                # (7 Sep 2026) so the row can offer "Add meeting link" when
                # Sales booked the slot without the customer's link.
                f"{prefix}_when": row.get("when"),
                f"{prefix}_link": bool(row.get("link")),
                f"{prefix}_interviewer": row.get("interviewer"),
            })
    # The customer's slots Sales passed on (29 Sep 2026) — TA sees them on the
    # row beside "Schedule Customer L1 / L2" and picks one in the form.
    from services.candidate_profiles import latest_customer_slots
    offers = latest_customer_slots(db, ids)
    for pid in ids:
        out[pid]["customer_slots"] = offers.get(pid)
    return out


def extract_resume_text(resume_file_url: str) -> str:
    """Locate the stored file behind /api/crm-files/... and extract plain text.

    Supports .pdf (pypdf), .docx (python-docx, imported lazily) and .txt.
    Raises HTTPException 422 on missing file, unsupported type or empty/failed
    extraction (e.g. scanned image PDFs with no text layer).
    """
    rel = resume_file_url or ""
    if rel.startswith(CRM_FILES_PREFIX):
        rel = rel[len(CRM_FILES_PREFIX):]
    path = resolve_crm_file(rel)
    if path is None:
        raise HTTPException(status_code=422, detail="Resume file not found on server; cannot scan")

    ext = path.suffix.lower()
    text = ""
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif ext == ".docx":
            import docx  # lazy: python-docx may not be installed at module-import time
            document = docx.Document(str(path))
            parts = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    parts.extend(cell.text for cell in row.cells)
            text = "\n".join(parts)
        elif ext == ".txt":
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported resume file type '{ext}'. Supported: .pdf, .docx, .txt",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Failed to extract text from resume: {exc}")

    text = (text or "").strip()
    if not text:
        raise HTTPException(
            status_code=422,
            detail="No text could be extracted from the resume (empty or image-only file)",
        )
    return text


def _word_match(term: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE) is not None


def _detect_experience_years(text: str) -> float | None:
    found = [float(m.group(1)) for m in EXPERIENCE_RE.finditer(text)]
    return max(found) if found else None


#: Above this word-overlap the "resume" is really the JD (or a copy of it).
_JD_SELF_MATCH_THRESHOLD = 0.85


def _texts_are_near_identical(a: str, b: str) -> bool:
    """Jaccard overlap on the distinct words of two documents.

    Used to catch the case where the JD itself was uploaded as the resume. A
    genuine CV shares maybe 10-25% of its vocabulary with the JD; the JD shares
    ~100% with itself, and a lightly-edited copy still shares most of it.
    """
    def bag(t: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", (t or "").lower()) if len(w) > 2}

    wa, wb = bag(a), bag(b)
    if len(wa) < 20 or len(wb) < 20:
        return False
    return len(wa & wb) / len(wa | wb) >= _JD_SELF_MATCH_THRESHOLD


def _jd_files(db: Session, requirement: Requirement) -> tuple[list[str], list[str]]:
    """(RMG JD file urls, the customer's JD file urls on the opportunity)."""
    from models import OpportunityAttachment, RequirementAttachment

    rmg = db.execute(select(RequirementAttachment.file_url).where(
        RequirementAttachment.requirement_id == requirement.id,
        RequirementAttachment.kind == "rmg_jd")).scalars().all()
    customer = db.execute(select(OpportunityAttachment.file_url).where(
        OpportunityAttachment.opportunity_id == requirement.opportunity_id,
        OpportunityAttachment.kind == "customer_jd")).scalars().all() \
        if requirement.opportunity_id else []
    return list(rmg), list(customer)


def ats_jd_text(db: Session, requirement: Requirement) -> tuple[str | None, str | None]:
    """The JD a resume is scored against, and where it came from.

    The RMG JD (text + Word / PDF files) when there is one; otherwise the
    CUSTOMER's JD on the opportunity (6 Oct 2026, user report: the ATS Scoring
    page scored a CV against the customer JD at 75 while Applied Candidates,
    which only ever read the RMG JD, had nothing but the skill list to go on).
    Returns (text, "rmg" | "customer") or (None, None)."""
    rmg_files, customer_files = _jd_files(db, requirement)

    def read(urls):
        parts = []
        for url in urls:
            try:
                parts.append(extract_resume_text(url))
            except HTTPException:
                continue
        return parts

    rmg = ([requirement.rmg_jd_text.strip()] if (requirement.rmg_jd_text or "").strip() else []) + read(rmg_files)
    if "\n\n".join(rmg).strip():
        return "\n\n".join(rmg).strip(), "rmg"
    customer = "\n\n".join(read(customer_files)).strip()
    return (customer, "customer") if customer else (None, None)


def _years(value) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", str(value or ""))
    try:
        years = float(m.group(1)) if m else None
    except ValueError:
        return None
    return years if years is not None and 0 <= years <= 60 else None


def candidate_facts(db: Session, resume: Resume) -> dict:
    """What the recruiter recorded for this applicant (6 Oct 2026): the
    experience typed on the application (else the candidate record's) and every
    location they gave — current and preferred. Fed to the scorer and to the AI
    reviewer, because a CV often states neither in words."""
    details = resume.application_details or {}
    cand = db.get(Candidate, resume.candidate_id) if resume.candidate_id else None
    years = _years(resume.applicant_experience)
    if years is None and cand is not None and cand.experience_years is not None:
        years = float(cand.experience_years)
    locations = []
    for v in (details.get("current_location"), details.get("preferred_location"),
              getattr(cand, "city", None), getattr(cand, "preferred_locations", None)):
        for part in re.split(r"[,;/|]+", str(v or "")):
            part = part.strip()
            if part and part.lower() not in {x.lower() for x in locations}:
                locations.append(part)
    return {"experience_years": years, "locations": locations[:8]}


def run_ats_scan(db: Session, resume: Resume, requirement: Requirement, user_id: int) -> dict:
    """Score a resume against its requirement; mutates the resume row (caller commits).

    Honest scoring (see services/ats_scoring.py):
      1. Gate — the file must parse to text (422) AND look like a resume (422).
      2. Skills are OPTIONAL (Aug 2026): a requirement with no skills scores on
         the RMG JD overlap alone. Only a requirement with NEITHER skills NOR a
         JD is a 400 config error (nothing to score against).
      3. The score is renormalised over configured criteria only, so an empty
         criterion never awards free points, and 100 needs every required skill
         (no required skills ⇒ capped at 99).
      4. When an RMG JD is present (text and/or rmg_jd attachment), JD keyword
         overlap is included in the score and breakdown.
    """
    from services.ats_scoring import AtsConfigError, looks_like_resume, score_resume_against_requirement

    text = extract_resume_text(resume.resume_file_url)  # raises 422 on empty / image-only
    is_resume, signals = looks_like_resume(text)
    if not is_resume:
        if signals.get("looks_like_job_description"):
            raise HTTPException(
                status_code=422,
                detail=(
                    "This file looks like a job description, not a resume "
                    f"(found: {', '.join(signals.get('jd_markers') or [])}). "
                    "Scoring a JD against its own requirement returns a near-perfect "
                    "score that means nothing. Upload the candidate's CV instead."
                ),
            )
        raise HTTPException(
            status_code=422,
            detail="Document does not appear to be a resume (no contact details or résumé sections found).",
        )

    skill_rows = db.execute(
        select(RequirementSkill, Skill.name)
        .join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id == requirement.id)
    ).all()
    mandatory = [name for rs, name in skill_rows if rs.is_mandatory]
    optional = [name for rs, name in skill_rows if not rs.is_mandatory]

    city = None
    if requirement.location_id:
        loc = db.get(Location, requirement.location_id)
        city = loc.city if loc else None

    jd_text, jd_source = ats_jd_text(db, requirement)
    facts = candidate_facts(db, resume)

    # Belt and braces: even if the JD sneaks past looks_like_resume, refuse to
    # score a document that IS the job description. Without this the candidate
    # scores ~100 for having uploaded the wrong file.
    if jd_text and _texts_are_near_identical(text, jd_text):
        raise HTTPException(
            status_code=422,
            detail=(
                "The uploaded file is (almost) the same document as this "
                "requirement's job description, so it cannot be scored against "
                "it — the result would be a meaningless near-100. Please upload "
                "the candidate's CV."
            ),
        )

    try:
        result = score_resume_against_requirement(
            text, mandatory, optional,
            _num(requirement.experience_min), _num(requirement.experience_max), city,
            jd_text=jd_text,
            weights=getattr(requirement, "ats_weights", None),
            candidate_facts=facts,
        )
    except AtsConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = result["ats_score"]
    breakdown = result["breakdown"]
    breakdown["parse_confidence"] = "high" if signals["word_count"] >= 120 else "low"
    breakdown["score_version"] = ATS_SCORE_VERSION
    if jd_text:
        breakdown["jd_text_preview"] = jd_text[:500]
        breakdown["jd_source"] = jd_source

    # OpenAI semantic review — blends real understanding (synonyms, related tech,
    # project evidence) with the deterministic keyword score. Deterministic-only
    # when no key is configured or the call fails.
    ai = _ai_semantic_review(jd_text, mandatory, optional, text, facts,
                             (_num(requirement.experience_min), _num(requirement.experience_max)))
    if ai is not None and ai.get("unavailable"):
        # Record the reason so the breakdown can say "keyword-only, because ..."
        breakdown["ai_review"] = ai
        details = breakdown.get("score_details") or {}
        details["blend"] = "keyword/criteria only — AI review unavailable"
        details["ai_unavailable_reason"] = ai["unavailable"]
        breakdown["score_details"] = details
    elif ai is not None:
        deterministic = float(total)
        blended = round(KEYWORD_SHARE * deterministic + AI_SHARE * ai["match_percent"], 2)
        # Honest cap preserved: only a full required-skill match may reach 100.
        details = breakdown.get("score_details") or {}
        if not details.get("all_required_matched") and blended >= 100.0:
            blended = 99.0
        breakdown["ai_review"] = ai
        details["deterministic_score"] = deterministic
        details["ai_semantic_score"] = ai["match_percent"]
        details["blend"] = (f"{round(KEYWORD_SHARE * 100)}% keyword/criteria + "
                            f"{round(AI_SHARE * 100)}% AI semantic")
        breakdown["score_details"] = details
        total = blended

    resume.ats_score = total
    resume.ats_score_breakdown = breakdown
    resume.ats_status = AtsStatus.SCORED
    resume.screened_by = user_id
    return {"ats_score": total, "breakdown": breakdown}


def ensure_resume_for_profile(db: Session, profile, req) -> Resume:
    """The Resume row a profile-only applicant needs before anything CV-shaped
    can happen to them — built from the CV on the candidate's own record.

    A candidate applied from the Candidates page (or "Apply to Opportunity")
    has a profile but no `resumes` row, so ATS, slot invites and the AI L1
    all had nothing to work on (user report, 2 Sep 2026). Once this row
    exists the applicant is an ordinary resume row everywhere. Idempotent:
    an existing (requirement, candidate) row is reused, never duplicated.
    """
    existing = db.execute(
        select(Resume).where(Resume.requirement_id == req.id,
                             Resume.candidate_id == profile.candidate_id)
        .order_by(Resume.id.desc())
    ).scalars().first()
    if existing is not None:
        return existing
    cand = db.get(Candidate, profile.candidate_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not (cand.cv_url or "").strip():
        raise HTTPException(
            status_code=422,
            detail="No CV on file for this candidate — upload one on their record "
                   "(Candidates → their profile → CV), then run the ATS scan.")
    resume = Resume(
        requirement_id=req.id,
        candidate_id=cand.id,
        candidate_name=(" ".join(p for p in (cand.first_name, cand.last_name) if p)
                        or f"Candidate #{cand.id}")[:255],
        email=cand.email,
        phone=cand.phone,
        source_portal=(profile.source or "app")[:64],
        applicant_experience=(str(cand.experience_years)
                              if cand.experience_years is not None else None),
        resume_file_url=cand.cv_url,
    )
    # Stamp the application date the profile carries, not today — the row is
    # catching up with an application that already happened. Left unset (so
    # the column's server default applies) when the profile has no date.
    applied = profile.applied_on or profile.created_at
    if applied is not None:
        resume.received_date = applied.date()
    db.add(resume)
    db.flush()
    return resume


def auto_score_profile(db: Session, profile, user_id: int | None) -> bool:
    """ATS for a candidate applied WITHOUT an upload (Candidates page, Apply to
    Opportunity) — the same automatic scan the upload and the bulk ZIP already
    run (28 Sep 2026, user rule: "when TA adds a candidate the ATS is done
    automatically", so the row carries no Run ATS button).

    Materialises the resume row from the candidate's CV and scores it against
    the opportunity's LATEST requirement. Best-effort in a savepoint: no
    requirement, no CV or a parser failure leaves the row unscored and never
    fails the apply. The auto-threshold pipeline is NOT run — screening is RMG
    / GM's call. Returns True when a score was written.
    """
    try:
        with db.begin_nested():
            req = db.execute(
                select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
                .order_by(Requirement.id.desc()).limit(1)
            ).scalars().first()
            if req is None:
                return False
            # An UPLOADED resume on this requirement is scored as it is — the
            # candidate record may carry no CV of its own (30 Sep 2026: an
            # upload that could not be scored when it arrived stayed "Not
            # scored" for ever).
            resume = db.execute(
                select(Resume).where(Resume.requirement_id == req.id,
                                     Resume.candidate_id == profile.candidate_id)
                .order_by(Resume.id.desc())
            ).scalars().first()
            if resume is None:
                cand = db.get(Candidate, profile.candidate_id)
                if cand is None or not (cand.cv_url or "").strip():
                    return False
                resume = ensure_resume_for_profile(db, profile, req)
            if resume.ats_status != AtsStatus.PENDING_SCAN or not (resume.resume_file_url or "").strip():
                return False
            run_ats_scan(db, resume, req, user_id)
            return True
    except Exception:
        logger.info("auto ATS skipped for profile %s", getattr(profile, "id", "?"), exc_info=True)
        return False


#: Candidates re-scored by one JD / skills change. More than this is a job for
#: "Score N pending" on the list — one save must not run for minutes.
RESCORE_MAX = 200

#: ATS statuses a JD / skills change may re-score. A Shortlisted / Rejected
#: resume is a DECISION someone took — `run_ats_scan` would overwrite it.
RESCORABLE_STATUSES = (AtsStatus.PENDING_SCAN, AtsStatus.SCORED)


def rescore_requirement(db: Session, req: Requirement, user_id: int | None,
                        limit: int = RESCORE_MAX) -> dict:
    """Score every live applicant of `req` again — after RMG / GM add or change
    the JD (text or Word / PDF) or the skills (30 Sep 2026, user report: the
    Screening Desk read "Could not score" because the position had nothing to
    score against, and adding the JD later changed nothing).

    Covers resume rows still Pending_Scan or Scored (never a Shortlisted /
    Rejected one), skipping closed candidacies, plus live profile-only
    applicants with a CV on file. Each scan in its own savepoint; the caller
    commits. Returns {scored, failed, skipped}.
    """
    from models import CandidateProfile
    from services.candidate_profiles import REJECTED_BUCKET

    closed = set(db.execute(
        select(CandidateProfile.candidate_id).where(
            CandidateProfile.opportunity_id == req.opportunity_id,
            CandidateProfile.pipeline_status.in_(REJECTED_BUCKET),
        )
    ).scalars().all())
    resumes = db.execute(
        select(Resume).where(Resume.requirement_id == req.id,
                             Resume.ats_status.in_(RESCORABLE_STATUSES))
        .order_by(Resume.id.desc()).limit(limit)
    ).scalars().all()
    seen = {r.candidate_id for r in resumes if r.candidate_id}
    work = [r for r in resumes if r.candidate_id not in closed or r.candidate_id is None]
    room = max(0, limit - len(work))
    if room and req.opportunity_id:
        live = db.execute(
            select(CandidateProfile).where(
                CandidateProfile.opportunity_id == req.opportunity_id,
                CandidateProfile.pipeline_status.not_in(REJECTED_BUCKET),
            ).limit(room * 2)
        ).scalars().all()
        for prof in live:
            if len(work) >= limit:
                break
            if prof.candidate_id in seen:
                continue
            seen.add(prof.candidate_id)
            cand = db.get(Candidate, prof.candidate_id)
            if cand is None or not (cand.cv_url or "").strip():
                continue
            try:
                with db.begin_nested():
                    work.append(ensure_resume_for_profile(db, prof, req))
            except Exception:
                continue
    scored = failed = skipped = 0
    for resume in work:
        if resume.ats_status not in RESCORABLE_STATUSES or not (resume.resume_file_url or "").strip():
            skipped += 1
            continue
        try:
            with db.begin_nested():
                run_ats_scan(db, resume, req, user_id)
            scored += 1
        except Exception:
            failed += 1
    return {"scored": scored, "failed": failed, "skipped": skipped}


def has_ats_criteria(db: Session, req: Requirement) -> bool:
    """Is there anything to score against — a skill, the RMG JD (text or file),
    or the customer's JD on the opportunity?"""
    if (req.rmg_jd_text or "").strip():
        return True
    if db.execute(select(RequirementSkill.id)
                  .where(RequirementSkill.requirement_id == req.id).limit(1)).first():
        return True
    rmg_files, customer_files = _jd_files(db, req)
    return bool(rmg_files or customer_files)


def rescore_requirement_in_background(requirement_id: int, user_id: int | None) -> None:
    """`rescore_requirement` on its own session in a daemon thread — the JD
    save answers at once; the scores land a few seconds later."""
    import threading

    def _run() -> None:
        from crm_db import get_session_factory

        try:
            db = get_session_factory()()
        except Exception:
            logger.info("ATS background scoring skipped: CRM database not available")
            return
        try:
            req = db.get(Requirement, requirement_id)
            if req is not None:
                res = rescore_requirement(db, req, user_id)
                db.commit()
                logger.info("ATS re-score of requirement %s: %s", requirement_id, res)
        except Exception:
            db.rollback()
            logger.warning("ATS re-score of requirement %s failed", requirement_id, exc_info=True)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name=f"ats-rescore-{requirement_id}").start()


def score_profiles_in_background(profile_ids: list[int], user_id: int | None) -> None:
    """`auto_score_profile` for a batch sent for Technical Screening, off the request."""
    import threading

    def _run() -> None:
        from crm_db import get_session_factory
        from models import CandidateProfile

        try:
            db = get_session_factory()()
        except Exception:
            logger.info("ATS background scoring skipped: CRM database not available")
            return
        try:
            for pid in profile_ids:
                prof = db.get(CandidateProfile, pid)
                if prof is not None and auto_score_profile(db, prof, user_id):
                    db.commit()
        except Exception:
            db.rollback()
            logger.warning("screening ATS batch failed", exc_info=True)
        finally:
            db.close()

    threading.Thread(target=_run, daemon=True, name="ats-screening-batch").start()
