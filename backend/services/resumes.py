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

from models import AtsStatus, Location, Requirement, RequirementSkill, Resume, Skill
from models.ai_links import hr_decision_label
from services.crm_common import resolve_crm_file

logger = logging.getLogger("karnex.crm.ats")

#: Which OpenAI key pool the ATS resume↔JD review draws on. Its own purpose (not
#: "eval") so scan-all bursts have their own spend line and rate limit, and can
#: never exhaust the quota a live interview is relying on. Resolution order is
#: OPENAI_ATS_API_KEY / OPENAI_API_KEY_ATS -> the eval key -> OPENAI_API_KEY.
ATS_OPENAI_PURPOSE = "ats"


def _ai_semantic_review(jd_text: str | None, mandatory: list[str], optional: list[str],
                        resume_text: str) -> dict | None:
    """OpenAI semantic assessment of resume↔role fit (returns None when no key /
    on any failure — the deterministic score always stands on its own).

    Unlike keyword matching, this understands synonyms, related tech and context
    (e.g. 'AUTOSAR stack work' implies embedded C), so the final score reflects
    real fit rather than literal word overlap."""
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
        prompt = (
            "You are a strict technical recruiter. Assess how well the RESUME fits the ROLE.\n"
            f"Required skills: {skills_line}\nNice-to-have skills: {opt_line}\n"
            + (f"Job description:\n{(jd_text or '')[:4000]}\n" if (jd_text or '').strip() else "")
            + f"\nRESUME:\n{resume_text[:9000]}\n\n"
            "Consider synonyms, related technologies and actual project evidence — not just "
            "literal keyword matches. Do not reward keyword stuffing. Return ONLY JSON: "
            '{"match_percent": 0-100, "summary": "2-3 sentence fit assessment", '
            '"strengths": ["..."], "gaps": ["..."]}'
        )
        res = get_openai_client(ATS_OPENAI_PURPOSE).chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
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
    import os

    from auth_db import get_schedule_by_token
    from models import AiInterviewLink, Candidate
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

    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    access_by_token: dict[str, str] = {}
    for link in latest.values():
        token = (link.invite_token or "").strip()
        if not token or token in access_by_token:
            continue
        try:
            sched = get_schedule_by_token(_legacy_db_target(), token) or {}
            access_by_token[token] = str(sched.get("access_key") or "")
        except Exception:
            access_by_token[token] = ""

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
        d["ai_report_link"] = (
            f"/admin?view=candidateReport&cid={email}&iid={link.interview_record_id}"
            if link.interview_record_id and email
            else None
        )
        token = (link.invite_token or "").strip()
        if token:
            d["ai_invite_token"] = token
            d["ai_invite_url"] = f"{base}/?invite={token}" if base else f"/?invite={token}"
            d["ai_access_key"] = access_by_token.get(token) or None
        if link.created_at and not d.get("ai_interview_scheduled_at"):
            d["ai_interview_scheduled_at"] = link.created_at.isoformat()
    return data


#: Human interview rounds, keyed by the prefix the row fields use. The manual
#: L1 (1 Sep 2026) is the round RMG runs INSTEAD of the AI screen, so the
#: Applied Candidates row has to be able to show it exactly like the L2 —
#: requested / scheduled / result — for a candidate who has no AI link at all.
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

    Two queries total, whatever the page size. Keyed by profile because that is
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
        meta: dict[int, dict] = {}
        for pid, eid, res, when, link in db.execute(
            select(InterviewEvent.profile_id, InterviewEvent.id, InterviewEvent.result,
                   InterviewEvent.scheduled_at, InterviewEvent.meeting_link)
            .where(InterviewEvent.profile_id.in_(ids), InterviewEvent.kind == kind)
            .order_by(InterviewEvent.id)
        ).all():
            meta[pid] = {"event_id": eid, "result": res or None,
                         "when": when.isoformat() if when else None,
                         "link": bool((link or "").strip())}
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
            })
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
    from models import RequirementAttachment
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

    jd_parts: list[str] = []
    if (requirement.rmg_jd_text or "").strip():
        jd_parts.append(requirement.rmg_jd_text.strip())
    jd_atts = db.execute(
        select(RequirementAttachment).where(
            RequirementAttachment.requirement_id == requirement.id,
            RequirementAttachment.kind == "rmg_jd",
        )
    ).scalars().all()
    for att in jd_atts:
        try:
            jd_parts.append(extract_resume_text(att.file_url))
        except HTTPException:
            continue
    jd_text = "\n\n".join(jd_parts).strip() or None

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
        )
    except AtsConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    total = result["ats_score"]
    breakdown = result["breakdown"]
    breakdown["parse_confidence"] = "high" if signals["word_count"] >= 120 else "low"
    if jd_text:
        breakdown["jd_text_preview"] = jd_text[:500]

    # OpenAI semantic review — blends real understanding (synonyms, related tech,
    # project evidence) with the deterministic keyword score. Deterministic-only
    # when no key is configured or the call fails.
    ai = _ai_semantic_review(jd_text, mandatory, optional, text)
    if ai is not None and ai.get("unavailable"):
        # Record the reason so the breakdown can say "keyword-only, because ..."
        breakdown["ai_review"] = ai
        details = breakdown.get("score_details") or {}
        details["blend"] = "keyword/criteria only — AI review unavailable"
        details["ai_unavailable_reason"] = ai["unavailable"]
        breakdown["score_details"] = details
    elif ai is not None:
        deterministic = float(total)
        blended = round(0.6 * deterministic + 0.4 * ai["match_percent"], 2)
        # Honest cap preserved: only a full required-skill match may reach 100.
        details = breakdown.get("score_details") or {}
        if not details.get("all_required_matched") and blended >= 100.0:
            blended = 99.0
        breakdown["ai_review"] = ai
        details["deterministic_score"] = deterministic
        details["ai_semantic_score"] = ai["match_percent"]
        details["blend"] = "60% keyword/criteria + 40% AI semantic"
        breakdown["score_details"] = details
        total = blended

    resume.ats_score = total
    resume.ats_score_breakdown = breakdown
    resume.ats_status = AtsStatus.SCORED
    resume.screened_by = user_id
    return {"ats_score": total, "breakdown": breakdown}
