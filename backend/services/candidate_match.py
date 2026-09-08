"""Suggested candidates for an opportunity — deterministic match scoring.

Answers "who in OUR database already fits this position?" the moment an
opportunity is created, from data the CRM already holds:

  * SKILLS (up to 50) — overlap with the opportunity's skill list; mandatory
    skills carry 35 of the 50, optional the remaining 15. Missing mandatory
    skills are reported by name — a 90% match missing one hard requirement is
    a different conversation from a 70% match missing none.
  * EXPERIENCE (up to 20) — inside the opportunity's CTC-slab band = 20;
    within a year of it = 10. The slab IS the commercial band the customer
    will pay for, so it is the fit that matters.
  * HISTORY (up to 25) — +10 for having reached a late pipeline stage on ANY
    opportunity (proven interview performer), +10 for a previous application
    to THIS customer (knows their process), +5 when any prior application
    exists at all (a known, reachable person).
  * PROFILE COMPLETENESS (up to 5) — has a CV on file (+3), phone (+2):
    proxies for "can be contacted and submitted today".

Deliberately NOT a model call: scoring runs on every open of the tab, must be
instant, explainable line-by-line ("matched Java, Selenium; missing AUTOSAR"),
and identical for every user. Candidates already applied to THIS opportunity
are excluded; candidates currently Joined/Preboarding elsewhere are flagged
`engaged` rather than hidden — poaching decisions belong to people.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateProfile, CandidateSkill, Opportunity, OpportunityCtcSlab,
    OpportunitySkill, PipelineStatus, Skill,
)

# Stages that prove the candidate performs in real pipelines.
_LATE_STAGES = {
    PipelineStatus.L1_FEEDBACK, PipelineStatus.L2_FEEDBACK,
    PipelineStatus.SHORTLISTED, PipelineStatus.CUSTOMER_APPROVAL,
    PipelineStatus.PREBOARDING, PipelineStatus.JOINED,
}
_ENGAGED_STAGES = {PipelineStatus.PREBOARDING, PipelineStatus.JOINED}

_POOL_CAP = 800     # candidates considered
_RESULT_CAP = 50    # suggestions returned


def _opp_experience_band(db: Session, opp: Opportunity) -> tuple[float, float] | None:
    """The commercial experience band = min exp_min .. max(exp_max, target)."""
    rows = db.execute(
        select(OpportunityCtcSlab).where(OpportunityCtcSlab.opportunity_id == opp.id)
    ).scalars().all()
    lo: float | None = None
    hi: float | None = None
    for r in rows:
        for v in (r.exp_min,):
            if v is not None:
                lo = float(v) if lo is None else min(lo, float(v))
        for v in (r.exp_max, r.target_exp):
            if v is not None:
                hi = float(v) if hi is None else max(hi, float(v))
    if lo is None or hi is None or hi < lo:
        return None
    return lo, hi


def suggest_candidates(db: Session, opp: Opportunity) -> list[dict]:
    opp_skills = db.execute(
        select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)
    ).scalars().all()
    mandatory_ids = {s.skill_id for s in opp_skills if s.is_mandatory}
    optional_ids = {s.skill_id for s in opp_skills if not s.is_mandatory}
    all_skill_ids = mandatory_ids | optional_ids
    skill_names: dict[int, str] = {
        s.id: s.name for s in db.execute(
            select(Skill).where(Skill.id.in_(all_skill_ids))
        ).scalars().all()
    } if all_skill_ids else {}

    band = _opp_experience_band(db, opp)

    # ---- candidate pool: anyone with a matching skill, same-customer history,
    # or a late-stage record. Falls back to experience band when the
    # opportunity carries no skills at all.
    pool_ids: set[int] = set()
    already_applied: set[int] = {
        cid for (cid,) in db.execute(
            select(CandidateProfile.candidate_id)
            .where(CandidateProfile.opportunity_id == opp.id)
        )
    }
    if all_skill_ids:
        for (cid,) in db.execute(
            select(CandidateSkill.candidate_id).distinct()
            .where(CandidateSkill.skill_id.in_(all_skill_ids))
        ):
            pool_ids.add(cid)
    for (cid,) in db.execute(
        select(CandidateProfile.candidate_id).distinct()
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(Opportunity.customer_id == opp.customer_id)
    ):
        pool_ids.add(cid)
    for (cid,) in db.execute(
        select(CandidateProfile.candidate_id).distinct()
        .where(CandidateProfile.pipeline_status.in_(_LATE_STAGES))
    ):
        pool_ids.add(cid)
    if not all_skill_ids and band is not None:
        lo, hi = band
        for (cid,) in db.execute(
            select(Candidate.id).where(
                Candidate.experience_years >= Decimal(str(max(0.0, lo - 1))),
                Candidate.experience_years <= Decimal(str(hi + 1)),
            ).limit(_POOL_CAP)
        ):
            pool_ids.add(cid)

    pool_ids -= already_applied
    if not pool_ids:
        return []
    pool_ids = set(list(pool_ids)[:_POOL_CAP])

    candidates = db.execute(
        select(Candidate).where(Candidate.id.in_(pool_ids))
    ).scalars().all()

    # Bulk-load skills + history for the pool.
    cand_skills: dict[int, set[int]] = {}
    for cid, sid in db.execute(
        select(CandidateSkill.candidate_id, CandidateSkill.skill_id)
        .where(CandidateSkill.candidate_id.in_(pool_ids))
    ):
        cand_skills.setdefault(cid, set()).add(sid)

    history: dict[int, list] = {}
    for prof, opp_title, opp_customer in db.execute(
        select(CandidateProfile, Opportunity.title, Opportunity.customer_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.candidate_id.in_(pool_ids))
        .order_by(CandidateProfile.id.desc())
    ):
        history.setdefault(prof.candidate_id, []).append((prof, opp_title, opp_customer))

    results: list[dict] = []
    for c in candidates:
        have = cand_skills.get(c.id, set())
        reasons: list[str] = []
        score = 0.0

        # ---- skills -----------------------------------------------------
        matched = sorted(skill_names[s] for s in (have & all_skill_ids))
        missing_mand = sorted(skill_names[s] for s in (mandatory_ids - have))
        if mandatory_ids:
            mand_ratio = len(mandatory_ids & have) / len(mandatory_ids)
            score += 35 * mand_ratio
        elif all_skill_ids:
            score += 35 * (1 if (have & all_skill_ids) else 0)
        if optional_ids:
            score += 15 * (len(optional_ids & have) / len(optional_ids))
        if matched:
            reasons.append(f"Skills: {', '.join(matched[:6])}"
                           + (f" +{len(matched) - 6} more" if len(matched) > 6 else ""))

        # ---- experience -------------------------------------------------
        exp = float(c.experience_years) if c.experience_years is not None else None
        if band is not None and exp is not None:
            lo, hi = band
            if lo <= exp <= hi:
                score += 20
                reasons.append(f"Experience {exp:g} yrs fits the {lo:g}–{hi:g} band")
            elif (lo - 1) <= exp <= (hi + 1):
                score += 10
                reasons.append(f"Experience {exp:g} yrs is near the {lo:g}–{hi:g} band")

        # ---- history ----------------------------------------------------
        rows = history.get(c.id, [])
        engaged = False
        same_customer = False
        late_stage = False
        last = rows[0] if rows else None
        for prof, _title, customer_id in rows:
            if prof.pipeline_status in _ENGAGED_STAGES:
                engaged = True
            if prof.pipeline_status in _LATE_STAGES:
                late_stage = True
            if customer_id == opp.customer_id:
                same_customer = True
        if late_stage:
            score += 10
            reasons.append("Reached a late pipeline stage before (proven performer)")
        if same_customer:
            score += 10
            reasons.append("Previously applied to this customer")
        if rows and not late_stage and not same_customer:
            score += 5
            reasons.append(f"{len(rows)} previous application(s) on record")

        # ---- completeness ----------------------------------------------
        if c.cv_url:
            score += 3
        if c.phone:
            score += 2

        if score <= 0:
            continue

        name = " ".join(p for p in [c.first_name, c.last_name] if p)
        results.append({
            "candidate_id": c.id,
            "name": name or f"Candidate #{c.id}",
            "email": c.email,
            "phone": c.phone,
            "experience_years": exp,
            "notice_period": c.notice_period,
            "technical_domain": c.technical_domain,
            "current_ctc": float(c.current_ctc) if c.current_ctc is not None else None,
            "expected_ctc": float(c.expected_ctc) if c.expected_ctc is not None else None,
            "city": c.city,
            "cv_url": c.cv_url,
            "linkedin_url": c.linkedin_url,
            "score": round(min(score, 100.0), 1),
            "matched_skills": matched,
            "missing_mandatory_skills": missing_mand,
            "reasons": reasons,
            "engaged": engaged,
            "applications_count": len(rows),
            "last_application": {
                "opportunity_title": last[1],
                "pipeline_status": getattr(last[0].pipeline_status, "value",
                                           last[0].pipeline_status),
            } if last else None,
        })

    results.sort(key=lambda r: (-r["score"], r["candidate_id"]))
    return results[:_RESULT_CAP]
