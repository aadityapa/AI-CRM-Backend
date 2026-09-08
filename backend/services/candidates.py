"""Candidate master service helpers: serialization + lookup validation."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateProfile, CandidateSkill,
    Customer, InterviewEvent, Opportunity, Skill,
)


#: Guard against a pathological query from a pasted paragraph.
_MAX_SEARCH_TOKENS = 6


def candidate_full_name_expr():
    """SQL expression for the candidate's whole name.

    `concat_ws` skips NULLs, so a candidate with no middle name yields
    "Anand Kumar" rather than "Anand  Kumar".
    """
    # NOTE: this expression must stay byte-identical to the one indexed in
    # migration 0061 (ix_candidates_fullname_trgm) or Postgres will not use the
    # index and every search falls back to a sequential scan.
    return (
        func.coalesce(Candidate.first_name, "") + " "
        + func.coalesce(Candidate.middle_name, "") + " "
        + func.coalesce(Candidate.last_name, "")
    )


def candidate_search_clause(search: str):
    """Match a search box against a candidate's name, email or phone.

    Matching each column separately (`first_name ILIKE '%Anand Kumar%' OR
    last_name ILIKE '%Anand Kumar%'`) can never match a full name, because no
    single column holds "Anand Kumar" — that was the bug this replaces.

    Instead every whitespace-separated token must match somewhere, so:
      * "anand kumar"  and  "kumar anand"   both find Anand Kumar
      * "anand"        still matches on its own
      * "anand@x.com"  matches on email
      * "98765"        matches on phone
    """
    tokens = (search or "").split()[:_MAX_SEARCH_TOKENS]
    if not tokens:
        return sa.true()
    full_name = candidate_full_name_expr()
    return sa.and_(*[
        sa.or_(
            full_name.ilike(f"%{token}%"),
            Candidate.email.ilike(f"%{token}%"),
            Candidate.phone.ilike(f"%{token}%"),
        )
        for token in tokens
    ])


def _num(value):
    return float(value) if value is not None else None


def _dt(value):
    return value.isoformat() if value is not None else None


def get_candidate_or_404(db: Session, candidate_id: int) -> Candidate:
    candidate = db.get(Candidate, candidate_id)
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def apply_cv_profile_to_candidate(db: Session, candidate: Candidate, profile: dict) -> dict:
    """Fill EMPTY candidate fields and MISSING child records (skills / education /
    experience) from a parsed CV profile. Never overwrites data already present.
    Skill names are mapped to the Skill master (created when new). Best-effort;
    the caller commits. Returns a summary of what was filled."""
    filled = {"fields": [], "skills": 0, "education": 0, "experience": 0}
    if not isinstance(profile, dict):
        return filled

    def _set_if_empty(attr: str, value) -> None:
        if value in (None, ""):
            return
        if getattr(candidate, attr, None) in (None, ""):
            setattr(candidate, attr, value)
            filled["fields"].append(attr)

    dom = (profile.get("technical_domain") or "").strip()
    _set_if_empty("technical_domain", dom[:255] or None)
    lk = (profile.get("linkedin_url") or "").strip()
    _set_if_empty("linkedin_url", lk[:1024] or None)
    if profile.get("experience_years") is not None:
        _set_if_empty("experience_years", profile["experience_years"])
    if profile.get("current_ctc") is not None:
        _set_if_empty("current_ctc", profile["current_ctc"])
    if profile.get("expected_ctc") is not None:
        _set_if_empty("expected_ctc", profile["expected_ctc"])
    # Notice period comes from the apply form (Resume.application_details) and
    # is a free-text string like "30 days" or "Immediate". _set_if_empty means a
    # value a recruiter typed on the candidate record is never overwritten by a
    # later application.
    notice = (str(profile.get("notice_period") or "").strip())[:60]
    if notice:
        _set_if_empty("notice_period", notice)

    # Skills — only when the candidate currently has none.
    has_skills = db.execute(
        select(CandidateSkill.id).where(CandidateSkill.candidate_id == candidate.id).limit(1)
    ).first()
    if not has_skills:
        seen: set[int] = set()
        for raw in (profile.get("skills") or []):
            nm = str(raw).strip()
            if not nm or len(nm) > 80:
                continue
            skill = db.execute(
                select(Skill).where(func.lower(Skill.name) == nm.lower())
            ).scalar_one_or_none()
            if skill is None:
                skill = Skill(name=nm)
                db.add(skill)
                db.flush()
            if skill.id in seen:
                continue
            seen.add(skill.id)
            db.add(CandidateSkill(candidate_id=candidate.id, skill_id=skill.id))
            filled["skills"] += 1

    # Education — only when the candidate has none.
    has_edu = db.execute(
        select(CandidateEducation.id).where(CandidateEducation.candidate_id == candidate.id).limit(1)
    ).first()
    if not has_edu:
        for e in (profile.get("education") or []):
            if not isinstance(e, dict):
                continue
            course = str(e.get("course") or e.get("degree") or "").strip()
            if not course:
                continue
            db.add(CandidateEducation(
                candidate_id=candidate.id,
                course=course[:255],
                institution=(str(e.get("institution") or "").strip()[:255] or None),
            ))
            filled["education"] += 1

    # Experience — only when the candidate has none.
    has_exp = db.execute(
        select(CandidateExperience.id).where(CandidateExperience.candidate_id == candidate.id).limit(1)
    ).first()
    if not has_exp:
        for x in (profile.get("experience") or []):
            if not isinstance(x, dict):
                continue
            company = str(x.get("company") or x.get("company_name") or "").strip()
            if not company:
                continue
            db.add(CandidateExperience(
                candidate_id=candidate.id,
                company_name=company[:255],
                job_title=(str(x.get("title") or x.get("job_title") or "").strip()[:255] or None),
                is_current=bool(x.get("is_current")),
            ))
            filled["experience"] += 1

    return filled


def ensure_skills_exist(db: Session, skill_ids: list[int]) -> list[int]:
    """Validate every skill id exists; 400 with the missing ids otherwise."""
    unique_ids = sorted(set(int(s) for s in skill_ids))
    if not unique_ids:
        return []
    found = set(db.execute(select(Skill.id).where(Skill.id.in_(unique_ids))).scalars().all())
    missing = [s for s in unique_ids if s not in found]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Unknown skill id(s): {', '.join(str(m) for m in missing)}")
    return unique_ids


def candidate_to_dict(candidate: Candidate) -> dict:
    return {
        "id": candidate.id,
        "salutation": getattr(candidate, "salutation", None),
        "first_name": candidate.first_name,
        "middle_name": getattr(candidate, "middle_name", None),
        "last_name": candidate.last_name,
        "full_name": " ".join(
            p for p in (candidate.first_name, getattr(candidate, "middle_name", None), candidate.last_name) if p
        ),
        "email": candidate.email,
        "phone": candidate.phone,
        "date_of_birth": _dt(getattr(candidate, "date_of_birth", None)),
        "gender": getattr(candidate, "gender", None),
        "experience_years": _num(getattr(candidate, "experience_years", None)),
        "notice_period": getattr(candidate, "notice_period", None),
        "current_address": candidate.current_address,
        "permanent_address": candidate.permanent_address,
        "technical_domain": candidate.technical_domain,
        "roles": getattr(candidate, "roles", None),
        "designation_id": candidate.designation_id,
        "cv_url": candidate.cv_url,
        "linkedin_url": candidate.linkedin_url,
        "resignation_status": bool(candidate.resignation_status),
        "last_working_day": _dt(candidate.last_working_day),
        "resignation_certificate_url": getattr(candidate, "resignation_certificate_url", None),
        "current_ctc": _num(getattr(candidate, "current_ctc", None)),
        "expected_ctc": _num(candidate.expected_ctc),
        "preferred_location_id": candidate.preferred_location_id,
        # --- Zoho NEXUS export fields (migration 0057) ----------------------
        "zoho_candidate_id": getattr(candidate, "zoho_candidate_id", None),
        "city": getattr(candidate, "city", None),
        "preferred_locations": getattr(candidate, "preferred_locations", None),
        "recruiter_email": getattr(candidate, "recruiter_email", None),
        "cv_original_filename": getattr(candidate, "cv_original_filename", None),
        "source_created_date": _dt(getattr(candidate, "source_created_date", None)),
        "created_at": _dt(candidate.created_at),
        "updated_at": _dt(candidate.updated_at),
    }


def education_to_dict(edu: CandidateEducation) -> dict:
    return {
        "id": edu.id,
        "candidate_id": edu.candidate_id,
        "course": edu.course,
        "institution": edu.institution,
        "start_date": _dt(edu.start_date),
        "end_date": _dt(edu.end_date),
        "certificate_url": edu.certificate_url,
    }


def experience_to_dict(exp: CandidateExperience) -> dict:
    return {
        "id": exp.id,
        "candidate_id": exp.candidate_id,
        "company_name": exp.company_name,
        "job_title": exp.job_title,
        "start_date": _dt(exp.start_date),
        "end_date": _dt(exp.end_date),
        "certificate_url": exp.certificate_url,
        "is_current": bool(exp.is_current),
    }


def candidate_detail(db: Session, candidate: Candidate) -> dict:
    data = candidate_to_dict(candidate)
    data["education"] = [education_to_dict(e) for e in candidate.education]
    data["experience"] = [experience_to_dict(e) for e in candidate.experience]

    skill_rows = db.execute(
        select(CandidateSkill.skill_id, Skill.name)
        .join(Skill, Skill.id == CandidateSkill.skill_id)
        .where(CandidateSkill.candidate_id == candidate.id)
        .order_by(Skill.name)
    ).all()
    data["skills"] = [{"skill_id": sid, "name": name} for sid, name in skill_rows]

    # Every opportunity this candidate has applied to. LEFT JOIN on Customer so a
    # profile is never hidden just because its opportunity has no customer attached.
    profile_rows = db.execute(
        select(CandidateProfile, Opportunity.opp_id, Opportunity.title, Customer.name)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .where(CandidateProfile.candidate_id == candidate.id)
        .order_by(CandidateProfile.id)
    ).all()
    prof_ids = [p.id for p, *_ in profile_rows]
    rounds: dict[int, int] = {}
    if prof_ids:
        rounds = dict(
            db.execute(
                select(InterviewEvent.profile_id, func.count(InterviewEvent.id))
                .where(InterviewEvent.profile_id.in_(prof_ids))
                .group_by(InterviewEvent.profile_id)
            ).all()
        )
    data["profiles"] = [
        {
            "id": p.id,
            "opportunity_id": p.opportunity_id,
            "opportunity_opp_id": opp_code,
            "opportunity_title": title,
            "customer_name": customer_name,
            "pipeline_status": (p.pipeline_status.value
                                if hasattr(p.pipeline_status, "value") else str(p.pipeline_status)),
            "expected_ctc": _num(p.expected_ctc),
            "applied_on": _dt(getattr(p, "applied_on", None)) or _dt(p.created_at),
            "ta_owner_name": getattr(p, "ta_owner_name", None),
            "interview_rounds": rounds.get(p.id, 0),
        }
        for p, opp_code, title, customer_name in profile_rows
    ]
    return data
