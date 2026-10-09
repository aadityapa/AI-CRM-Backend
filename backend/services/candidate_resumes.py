"""One candidate, several resumes, every open position (9 Oct 2026).

TA asked: "a candidate suitable for many positions — let us keep more than one
resume, see which of our open positions each fits, and apply them in one go."

  * the LIBRARY — every resume version a candidate has (`candidate_resumes`),
    seeded lazily from what is already on file (the record's CV and every
    resume uploaded for a position), so nothing needs re-uploading;
  * MATCHING POSITIONS — each version scored against every position TA can
    apply to today, with the SAME deterministic ATS scorer Applied Candidates
    uses (free, instant). The paid AI review runs only when TA clicks, and is
    kept on the version so a second look costs nothing;
  * MULTI-APPLY — the router (`routers/crm/candidate_resumes.py`) creates the
    candidacies through the ordinary create-profile path, each with the version
    TA picked, and the ATS runs on that version.

`GOOD_FIT_PCT` (60, user decision) is where a match reads "Good fit".
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Candidate,
    CandidateProfile,
    Customer,
    Location,
    Opportunity,
    PipelineStage,
    Requirement,
    RequirementSkill,
    Resume,
    Skill,
)
from models.candidate_resumes import CandidateResume

logger = logging.getLogger("karnex.crm.candidate_resumes")

GOOD_FIT_PCT = 60.0
#: Versions kept per candidate (a library, not an archive).
MAX_VERSIONS = 10
#: Parsed text kept per version — plenty for scoring, bounded for the DB.
TEXT_CAP = 60_000
#: Positions scored per request.
MAX_POSITIONS = 300
#: A position's JD (text + files) is re-read at most this often.
JD_CACHE_TTL_S = 600

_jd_cache: dict[int, tuple[float, str, tuple[str | None, str | None]]] = {}


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

def _name(user) -> str:
    return str(getattr(user, "full_name", "") or getattr(user, "username", "") or "")


def serialize_version(v: CandidateResume) -> dict:
    return {
        "id": v.id,
        "candidate_id": v.candidate_id,
        "file_url": v.file_url,
        "original_filename": v.original_filename,
        "label": v.label or v.original_filename or "Resume",
        "source": v.source,
        "is_primary": bool(v.is_primary),
        "file_size": v.file_size,
        "uploaded_by_name": v.uploaded_by_name,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "has_text": bool(v.extracted_text),
    }


def sync_library(db: Session, cand: Candidate) -> list[CandidateResume]:
    """The candidate's versions, oldest first — adding any file on record that
    the library does not know yet (the record's CV, a resume uploaded for a
    position). Idempotent; keeps exactly one primary, mirrored to `cv_url`."""
    rows = list(db.execute(
        select(CandidateResume).where(CandidateResume.candidate_id == cand.id)
        .order_by(CandidateResume.created_at, CandidateResume.id)
    ).scalars())
    known = {r.file_url for r in rows}
    added = False
    cv = (cand.cv_url or "").strip()
    if cv and cv not in known:
        v = CandidateResume(candidate_id=cand.id, file_url=cv, original_filename=cand.cv_original_filename,
                            label="CV on record", source="cv")
        db.add(v)
        rows.append(v)
        known.add(cv)
        added = True
    uploads = db.execute(
        select(Resume.resume_file_url, Resume.file_sha256, Resume.file_size, Requirement.req_number)
        .outerjoin(Requirement, Requirement.id == Resume.requirement_id)
        .where(Resume.candidate_id == cand.id)
        .order_by(Resume.id.desc())
    ).all()
    for url, sha, size, req_no in uploads:
        url = (url or "").strip()
        if not url or url in known:
            continue
        v = CandidateResume(candidate_id=cand.id, file_url=url, label=f"Uploaded for {req_no or 'a position'}"[:120],
                            source="application", file_sha256=sha, file_size=size)
        db.add(v)
        rows.append(v)
        known.add(url)
        added = True
    if added:
        db.flush()
    primaries = [r for r in rows if r.is_primary]
    if rows and len(primaries) != 1:
        keep = next((r for r in rows if cv and r.file_url == cv), None) or (primaries[-1] if primaries else rows[-1])
        for r in rows:
            r.is_primary = r is keep
        db.flush()
    return rows


def _version_or_404(db: Session, cand: Candidate, resume_id: int) -> CandidateResume:
    v = db.get(CandidateResume, resume_id)
    if v is None or v.candidate_id != cand.id:
        raise HTTPException(status_code=404, detail="Resume not found for this candidate")
    return v


def set_primary(db: Session, cand: Candidate, v: CandidateResume) -> None:
    """The primary version IS the candidate's CV everywhere else in the app."""
    for r in db.execute(select(CandidateResume).where(CandidateResume.candidate_id == cand.id)).scalars():
        r.is_primary = r.id == v.id
    cand.cv_url = v.file_url
    if v.original_filename:
        cand.cv_original_filename = v.original_filename
    db.flush()


def add_version(db: Session, cand: Candidate, file: UploadFile, *, label: str | None, user,
                make_primary: bool = False) -> tuple[CandidateResume, bool]:
    """Store a new version. The same bytes uploaded twice are the same version
    (returns it with created=False)."""
    from services.crm_common import save_upload_hashed

    rows = sync_library(db, cand)
    url, sha, size = save_upload_hashed(file, "cv")
    same = next((r for r in rows if r.file_sha256 and r.file_sha256 == sha), None)
    if same is not None:
        if make_primary:
            set_primary(db, cand, same)
        return same, False
    if len(rows) >= MAX_VERSIONS:
        raise HTTPException(status_code=409,
                            detail=f"A candidate can keep up to {MAX_VERSIONS} resumes — remove one first.")
    clean = " ".join(str(label or "").split())[:120] or None
    v = CandidateResume(candidate_id=cand.id, file_url=url, original_filename=(file.filename or "")[:255] or None,
                        label=clean or (file.filename or "Resume")[:120], source="upload",
                        file_sha256=sha, file_size=size,
                        uploaded_by_id=getattr(user, "id", None), uploaded_by_name=_name(user)[:255] or None)
    db.add(v)
    db.flush()
    if make_primary or not any(r.is_primary for r in rows):
        set_primary(db, cand, v)
    return v, True


def rename_version(db: Session, cand: Candidate, v: CandidateResume, label: str) -> None:
    clean = " ".join(str(label or "").split())[:120]
    if not clean:
        raise HTTPException(status_code=400, detail="Give the resume a name.")
    v.label = clean
    db.flush()


def delete_version(db: Session, cand: Candidate, v: CandidateResume) -> None:
    """Remove a version from the library. The primary cannot go (it is the
    candidate's CV); the stored file stays, since applications may use it."""
    if v.is_primary:
        raise HTTPException(status_code=409,
                            detail="This is the main resume — make another one main first.")
    db.delete(v)
    db.flush()


def version_text(db: Session, v: CandidateResume) -> str | None:
    """The parsed text, read once and cached on the row. None when the file
    cannot be read (scan, missing) — the version is then simply not scored."""
    if v.extracted_text:
        return v.extracted_text
    from services.resumes import extract_resume_text
    try:
        text = extract_resume_text(v.file_url)
    except HTTPException:
        return None
    except Exception:
        logger.info("resume text extraction failed for version %s", v.id, exc_info=True)
        return None
    v.extracted_text = text[:TEXT_CAP]
    return v.extracted_text


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def _latest_requirement():
    return (select(sa.func.max(Requirement.id).label("requirement_id"))
            .group_by(Requirement.opportunity_id).subquery())


def live_positions(db: Session) -> list[dict]:
    """Every position TA can apply to today: the latest requirement of each
    New / Active deal, in sourcing (Open · Posted · In progress)."""
    from services.requirements import SOURCING_STATUSES

    lr = _latest_requirement()
    rows = db.execute(
        select(Requirement, Opportunity.opp_id, Opportunity.title, Opportunity.id,
               Customer.name, Location.city)
        .join(lr, lr.c.requirement_id == Requirement.id)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .outerjoin(Location, Location.id == Requirement.location_id)
        .where(Requirement.status.in_(list(SOURCING_STATUSES)),
               Opportunity.pipeline_stage.in_([PipelineStage.NEW, PipelineStage.ACTIVE]))
        .order_by(Requirement.id.desc())
        .limit(MAX_POSITIONS)
    ).all()
    out = []
    for req, opp_code, opp_title, opp_id, customer, city in rows:
        out.append({"req": req, "opportunity_id": opp_id, "opp_id": opp_code, "opportunity_title": opp_title,
                    "customer_name": customer, "city": city})
    return out


def _skills_by_requirement(db: Session, req_ids: list[int]) -> dict[int, tuple[list[str], list[str]]]:
    out: dict[int, tuple[list[str], list[str]]] = {i: ([], []) for i in req_ids}
    if not req_ids:
        return out
    for rid, mandatory, name in db.execute(
        select(RequirementSkill.requirement_id, RequirementSkill.is_mandatory, Skill.name)
        .join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id.in_(req_ids))
    ).all():
        (out[rid][0] if mandatory else out[rid][1]).append(name)
    return out


def _jd_for(db: Session, req: Requirement) -> tuple[str | None, str | None]:
    from services.resumes import ats_jd_text
    stamp = f"{getattr(req, 'updated_at', '')}|{len(req.rmg_jd_text or '')}"
    hit = _jd_cache.get(req.id)
    now = time.monotonic()
    if hit and hit[0] > now and hit[1] == stamp:
        return hit[2]
    try:
        val = ats_jd_text(db, req)
    except Exception:
        val = (None, None)
    _jd_cache[req.id] = (now + JD_CACHE_TTL_S, stamp, val)
    if len(_jd_cache) > 2000:
        _jd_cache.clear()
    return val


def _f(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _facts(cand: Candidate) -> dict:
    import re
    locs = []
    for v in (cand.city, cand.preferred_locations):
        for part in re.split(r"[,;/|]+", str(v or "")):
            part = part.strip()
            if part and part.lower() not in {x.lower() for x in locs}:
                locs.append(part)
    return {"experience_years": _f(cand.experience_years), "locations": locs[:8]}


def score_version(text: str, *, mandatory: list[str], optional: list[str], exp_min, exp_max, city,
                  jd_text: str | None, weights, facts: dict) -> dict | None:
    """The deterministic ATS score of one version against one position, or
    None when the position has nothing to score against. PURE."""
    from services.ats_scoring import AtsConfigError, score_resume_against_requirement
    try:
        res = score_resume_against_requirement(text, mandatory, optional, _f(exp_min), _f(exp_max), city,
                                               jd_text=jd_text, weights=weights, candidate_facts=facts)
    except AtsConfigError:
        return None
    b = res.get("breakdown") or {}
    return {
        "score": float(res.get("ats_score") or 0.0),
        "skills_matched": list(b.get("skills_matched") or [])[:12],
        "skills_missing": list(b.get("skills_missing") or [])[:12],
        "jd_matched": list(b.get("jd_keywords_matched") or [])[:12],
        "experience_match": b.get("experience_match"),
    }


def _applications(db: Session, cand_id: int, opp_ids: list[int]) -> dict[int, dict]:
    if not opp_ids:
        return {}
    profiles = list(db.execute(
        select(CandidateProfile).where(CandidateProfile.candidate_id == cand_id,
                                       CandidateProfile.opportunity_id.in_(opp_ids))
    ).scalars())
    if not profiles:
        return {}
    from services.candidate_status import statuses_for
    statuses = statuses_for(db, profiles)
    out = {}
    for p in profiles:
        st = statuses.get(int(p.id)) or {}
        out[int(p.opportunity_id)] = {
            "profile_id": p.id,
            "status": st.get("label"),
            "tone": st.get("tone"),
            "applied_by": p.ta_owner_name,
            "applied_on": p.applied_on.isoformat() if p.applied_on else None,
        }
    return out


def matching_positions(db: Session, cand: Candidate) -> dict:
    """Every live position scored against every resume version of the candidate.
    Rows are ordered good fits first (not yet applied), then by score."""
    versions = sync_library(db, cand)
    texts = {v.id: version_text(db, v) for v in versions}
    scorable = [v for v in versions if texts.get(v.id)]
    positions = live_positions(db)
    req_ids = [p["req"].id for p in positions]
    skills = _skills_by_requirement(db, req_ids)
    applied = _applications(db, cand.id, [p["opportunity_id"] for p in positions])
    facts = _facts(cand)
    expected = _f(cand.expected_ctc)
    rows = []
    for p in positions:
        req: Requirement = p["req"]
        mandatory, optional = skills.get(req.id, ([], []))
        jd_text, jd_source = _jd_for(db, req)
        per = []
        best = None
        for v in scorable:
            s = score_version(texts[v.id], mandatory=mandatory, optional=optional,
                              exp_min=req.experience_min, exp_max=req.experience_max, city=p["city"],
                              jd_text=jd_text, weights=getattr(req, "ats_weights", None), facts=facts)
            if s is None:
                continue
            per.append({"resume_id": v.id, "score": s["score"]})
            if best is None or s["score"] > best["score"]:
                best = {**s, "resume_id": v.id, "label": v.label or v.original_filename or "Resume"}
        reviews = {}
        for v in versions:
            r = (v.ai_reviews or {}).get(str(req.id)) if isinstance(v.ai_reviews, dict) else None
            if r:
                reviews[str(v.id)] = r
        budget_max = _f(req.budget_ctc_max)
        exp_min, exp_max = _f(req.experience_min), _f(req.experience_max)
        yrs = facts["experience_years"]
        exp_fit = None
        if yrs is not None and (exp_min is not None or exp_max is not None):
            exp_fit = (exp_min is None or yrs >= exp_min) and (exp_max is None or yrs <= exp_max)
        rows.append({
            "requirement_id": req.id,
            "req_number": req.req_number,
            "title": req.title,
            "opportunity_id": p["opportunity_id"],
            "opp_id": p["opp_id"],
            "opportunity_title": p["opportunity_title"],
            "customer_name": p["customer_name"],
            "location": p["city"],
            "experience_min": exp_min,
            "experience_max": exp_max,
            "budget_ctc_min": _f(req.budget_ctc_min),
            "budget_ctc_max": budget_max,
            "priority": getattr(req.priority, "value", req.priority),
            "positions": int(req.no_of_positions or 1),
            "mandatory_skills": mandatory,
            "jd_source": jd_source,
            "scorable": best is not None,
            "best": best,
            "scores": per,
            "good_fit": bool(best and best["score"] >= GOOD_FIT_PCT),
            "experience_fit": exp_fit,
            "over_budget": expected is not None and budget_max is not None and expected > budget_max,
            "applied": applied.get(p["opportunity_id"]),
            "ai_reviews": reviews,
        })
    rows.sort(key=lambda r: (r["applied"] is not None, -(r["best"]["score"] if r["best"] else -1)))
    return {
        "versions": [serialize_version(v) for v in versions],
        "unreadable": [v.id for v in versions if not texts.get(v.id)],
        "positions": rows,
        "good_fit_pct": GOOD_FIT_PCT,
        "summary": {
            "positions": len(rows),
            "good_fit": sum(1 for r in rows if r["good_fit"] and not r["applied"]),
            "applied": sum(1 for r in rows if r["applied"]),
        },
    }


def ai_review(db: Session, cand: Candidate, v: CandidateResume, req: Requirement, user) -> dict:
    """The AI fit review TA asked for (paid, so only on a click), blended with
    the keyword score the way Applied Candidates blends it, and kept on the
    version under the position's id."""
    from services.resumes import AI_SHARE, KEYWORD_SHARE, _ai_semantic_review

    text = version_text(db, v)
    if not text:
        raise HTTPException(status_code=422, detail="This resume cannot be read (a scan or a missing file).")
    mandatory, optional = _skills_by_requirement(db, [req.id]).get(req.id, ([], []))
    jd_text, _ = _jd_for(db, req)
    city = None
    if req.location_id:
        loc = db.get(Location, req.location_id)
        city = loc.city if loc else None
    facts = _facts(cand)
    det = score_version(text, mandatory=mandatory, optional=optional, exp_min=req.experience_min,
                        exp_max=req.experience_max, city=city, jd_text=jd_text,
                        weights=getattr(req, "ats_weights", None), facts=facts)
    if det is None:
        raise HTTPException(status_code=400, detail="This position has no skills or JD to compare against yet.")
    ai = _ai_semantic_review(jd_text, mandatory, optional, text, facts, (_f(req.experience_min), _f(req.experience_max)))
    if not ai or ai.get("unavailable"):
        raise HTTPException(status_code=503, detail=(ai or {}).get("unavailable") or "AI review is unavailable.")
    blended = round(KEYWORD_SHARE * det["score"] + AI_SHARE * float(ai["match_percent"]), 1)
    review = {
        "score": blended,
        "keyword_score": det["score"],
        "ai_score": ai["match_percent"],
        "summary": ai.get("summary"),
        "strengths": ai.get("strengths") or [],
        "gaps": ai.get("gaps") or [],
        "at": datetime.now(timezone.utc).isoformat(),
        "by": _name(user),
    }
    stored = dict(v.ai_reviews or {}) if isinstance(v.ai_reviews, dict) else {}
    stored[str(req.id)] = review
    v.ai_reviews = stored  # reassign: JSON columns do not track in-place edits
    db.flush()
    return review


def other_open_fits(db: Session, candidate_ids: list[int]) -> dict[int, int]:
    """{candidate_id: candidacies they already have on OTHER live deals} — the
    Applied Candidates "also in N other positions" hint. One query."""
    ids = [int(i) for i in set(candidate_ids or []) if i]
    if not ids:
        return {}
    rows = db.execute(
        select(CandidateProfile.candidate_id, sa.func.count())
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.candidate_id.in_(ids),
               Opportunity.pipeline_stage.in_([PipelineStage.NEW, PipelineStage.ACTIVE]))
        .group_by(CandidateProfile.candidate_id)
    ).all()
    return {int(c): int(n) for c, n in rows}


def library_counts(db: Session, candidate_ids: list[int]) -> dict[int, int]:
    """{candidate_id: resume versions in the library}. One query; a candidate
    whose library was never opened counts 0 (it is seeded on first open)."""
    ids = [int(i) for i in set(candidate_ids or []) if i]
    if not ids:
        return {}
    rows = db.execute(
        select(CandidateResume.candidate_id, sa.func.count())
        .where(CandidateResume.candidate_id.in_(ids))
        .group_by(CandidateResume.candidate_id)
    ).all()
    return {int(c): int(n) for c, n in rows}


def attach_version_to_profile(db: Session, profile, version: CandidateResume, user) -> Resume | None:
    """The application's resume row, built from the version TA picked, on the
    opportunity's latest requirement — so Applied Candidates shows that file
    and `auto_score_profile` scores it. An existing row for the candidate on
    that requirement is left as it is (it already holds their resume)."""
    req = db.execute(
        select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
        .order_by(Requirement.id.desc()).limit(1)
    ).scalars().first()
    if req is None:
        return None
    existing = db.execute(
        select(Resume).where(Resume.requirement_id == req.id, Resume.candidate_id == profile.candidate_id)
        .order_by(Resume.id.desc())
    ).scalars().first()
    if existing is not None:
        return existing
    cand = db.get(Candidate, profile.candidate_id)
    if cand is None:
        return None
    row = Resume(
        requirement_id=req.id,
        candidate_id=cand.id,
        candidate_name=(" ".join(p for p in (cand.first_name, cand.last_name) if p) or f"Candidate #{cand.id}")[:255],
        email=cand.email,
        phone=cand.phone,
        source_portal="resume library",
        applicant_experience=str(cand.experience_years) if cand.experience_years is not None else None,
        application_details={"resume_version": version.label or version.original_filename or "Resume",
                             "resume_version_id": version.id},
        resume_file_url=version.file_url,
        file_sha256=version.file_sha256,
        file_size=version.file_size,
    )
    db.add(row)
    db.flush()
    return row


def version_for_application(db: Session, cand: Candidate, resume_id: int | None) -> CandidateResume | None:
    if resume_id is None:
        return None
    return _version_or_404(db, cand, int(resume_id))
