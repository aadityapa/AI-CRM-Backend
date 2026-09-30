"""Suggested candidates for an opportunity — deterministic match scoring.

Answers "who in OUR database already fits this position?" the moment an
opportunity is created, from data the CRM already holds, and — since
28 Sep 2026 — against what the position ACTUALLY asks for: the RMG JD.

THE BASIS (what we score against), in order of preference:
  * the opportunity's latest REQUIREMENT — its Skill Evaluation Details
    (mandatory / optional, min rating) and its RMG JD text (`rmg_jd_text`,
    else the requirement description). This is the position as RMG defined it,
    the same JD the ATS scans resumes against;
  * the opportunity's own skill list, for a deal that has no requirement yet
    (or whose requirement carries no skills);
  * the CTC-slab experience band — the commercial band the customer pays for.
`suggestion_basis()` returns this so the page can SAY what it scored against.

THE CANDIDATE (what we score with) — every fact already on file, no file is
opened and no model is called (the tab must be instant and identical for
every user): recorded skills, technical domain, roles, job titles and
employers, education, and the skills/JD terms the ATS already found in the
candidate's previous resumes (`resumes.ats_score_breakdown`), so a person whose
skills were never typed in still matches on what their CV proved.

THE SCORE — points are earned per component and renormalised over the
components that are CONFIGURED (a position with no JD text is not scored on
JD overlap; one with no optional skills has no optional pool), exactly like
the ATS does, so the % always means "of what we could check":
  * MANDATORY skills 30 · OPTIONAL skills 15 — recorded skill OR seen in the
    corpus (alias-aware: React ≈ ReactJS, C++ safe). A candidate with NO
    recorded skills and nothing learned from resumes falls back to their latest
    resume's ATS score for these pools (flagged in the gaps).
  * JD FIT 15 — share of the RMG JD's technical terms found in the corpus.
  * EXPERIENCE 20 — inside the band = full; within a year of it = half.
  * HISTORY 15 — reached a late stage on any opportunity (+6, proven
    performer), previously applied to THIS customer (+6, knows their process),
    any prior application at all (+3, a known reachable person).
  * CONTACTABLE 5 — CV on file (+3), phone (+2).
  * PENALTY — rejected by THIS customer within `RECENT_REJECTION_DAYS`:
    −15 after normalisation. The customer already said no; surfacing the same
    person at 90 % is how a recruiter loses credibility. Older rejections and
    rejections elsewhere are shown in the history, never penalised.

Every candidate carries `strengths` / `gaps` (area + detail) — "which area is
good & which lacks" — and `history`: EVERY previous application (opportunity,
customer, stage, outcome, AI L1 result, RMG screening), not just the last.
Candidates already applied to THIS opportunity are excluded; candidates
currently Joined/Preboarding elsewhere are flagged `engaged` rather than
hidden — poaching decisions belong to people.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateProfile,
    CandidateSkill, Customer, Opportunity, OpportunityCtcSlab, OpportunitySkill,
    PipelineStatus, Requirement, RequirementSkill, Resume, Skill,
)
from services.ats_scoring import _skill_present, _word_match, extract_jd_keywords

# Stages that prove the candidate performs in real pipelines.
_LATE_STAGES = {
    PipelineStatus.L1_FEEDBACK, PipelineStatus.L2_FEEDBACK,
    PipelineStatus.SHORTLISTED, PipelineStatus.CUSTOMER_APPROVAL,
    PipelineStatus.PREBOARDING, PipelineStatus.JOINED,
}
_ENGAGED_STAGES = {PipelineStatus.PREBOARDING, PipelineStatus.JOINED}

# Point pools — see the module docstring for what each one means.
W_MANDATORY = 30.0
W_OPTIONAL = 15.0
W_JD = 15.0
W_EXPERIENCE = 20.0
W_HISTORY = 15.0          # 6 late stage + 6 same customer + 3 any history
W_CONTACT = 5.0
RECENT_REJECTION_PENALTY = 15.0
RECENT_REJECTION_DAYS = 365

ATS_POOL_MIN = 50.0       # a resume the ATS rated at least this well puts its owner in the pool
_POOL_CAP = 800     # candidates considered
_RESULT_CAP = 50    # suggestions returned
_JD_TERMS_SHOWN = 6


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


def _latest_requirement(db: Session, opp: Opportunity) -> Requirement | None:
    return db.execute(
        select(Requirement).where(Requirement.opportunity_id == opp.id)
        .order_by(Requirement.id.desc()).limit(1)
    ).scalars().first()


def suggestion_basis(db: Session, opp: Opportunity) -> dict:
    """What the scan scores against, spelled out for the page header.

    Requirement skills win over opportunity skills for the same skill id (RMG's
    Skill Evaluation Details are the reviewed list); the opportunity list fills
    in anything the requirement does not name.
    """
    req = _latest_requirement(db, opp)
    skills: dict[int, dict] = {}
    for s in db.execute(
        select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)
    ).scalars().all():
        skills[s.skill_id] = {"mandatory": bool(s.is_mandatory),
                              "level": s.required_level, "from": "opportunity"}
    if req is not None:
        for s in db.execute(
            select(RequirementSkill).where(RequirementSkill.requirement_id == req.id)
        ).scalars().all():
            skills[s.skill_id] = {"mandatory": bool(s.is_mandatory),
                                  "level": s.min_rating, "from": "requirement"}
    names = {
        s.id: s.name for s in db.execute(
            select(Skill).where(Skill.id.in_(list(skills)))
        ).scalars().all()
    } if skills else {}
    for sid, meta in skills.items():
        meta["name"] = names.get(sid, f"Skill #{sid}")

    jd_text = ""
    jd_source = None
    if req is not None:
        if (req.rmg_jd_text or "").strip():
            jd_text, jd_source = req.rmg_jd_text.strip(), "rmg_jd"
        elif (req.description or "").strip():
            jd_text, jd_source = req.description.strip(), "requirement_description"
    keywords = extract_jd_keywords(jd_text) if jd_text else []
    band = _opp_experience_band(db, opp)
    return {
        "requirement_id": req.id if req else None,
        "req_number": req.req_number if req else None,
        "requirement_title": req.title if req else None,
        "requirement_status": getattr(req.status, "value", req.status) if req else None,
        "jd_source": jd_source,          # rmg_jd | requirement_description | None
        "jd_keywords": keywords,
        "mandatory_skills": sorted(m["name"] for m in skills.values() if m["mandatory"]),
        "optional_skills": sorted(m["name"] for m in skills.values() if not m["mandatory"]),
        "experience_band": {"min": band[0], "max": band[1]} if band else None,
        "weights": {
            "mandatory": W_MANDATORY, "optional": W_OPTIONAL, "jd": W_JD,
            "experience": W_EXPERIENCE, "history": W_HISTORY, "contact": W_CONTACT,
            "recent_rejection_penalty": RECENT_REJECTION_PENALTY,
        },
        "_skills": skills,               # internal — stripped by the router
    }


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _outcome(status: PipelineStatus | str) -> str:
    from services.candidate_profiles import REJECTED_BUCKET
    v = getattr(status, "value", status)
    if v == PipelineStatus.JOINED.value:
        return "joined"
    if v == PipelineStatus.SELF_WITHDRAWN.value:
        return "withdrawn"
    if v in REJECTED_BUCKET:
        return "rejected"
    if v == PipelineStatus.PREBOARDING.value:
        return "engaged"
    return "in_progress"


def suggest_candidates(db: Session, opp: Opportunity, basis: dict | None = None) -> list[dict]:
    from services.candidate_profiles import latest_ai_interviews
    from services.candidate_status import stage_label

    basis = basis or suggestion_basis(db, opp)
    skills: dict[int, dict] = basis["_skills"]
    mandatory_ids = {sid for sid, m in skills.items() if m["mandatory"]}
    optional_ids = {sid for sid, m in skills.items() if not m["mandatory"]}
    all_skill_ids = set(skills)
    skill_names = {sid: m["name"] for sid, m in skills.items()}
    jd_keywords: list[str] = basis["jd_keywords"]
    band_meta = basis["experience_band"]
    band = (band_meta["min"], band_meta["max"]) if band_meta else None

    # ---- candidate pool: anyone with a matching skill, same-customer history,
    # or a late-stage record. Falls back to experience band when the
    # position carries no skills at all.
    # The pool is built in TIERS and truncated in tier order, so when the cap
    # bites it is the weakest tier (band-only) that is dropped, never a skill
    # match.
    already_applied: set[int] = {
        cid for (cid,) in db.execute(
            select(CandidateProfile.candidate_id)
            .where(CandidateProfile.opportunity_id == opp.id)
        )
    }
    ordered: list[int] = []
    seen: set[int] = set(already_applied)

    def take(rows) -> None:
        for (cid,) in rows:
            if cid is not None and cid not in seen:
                seen.add(cid)
                ordered.append(cid)

    if all_skill_ids:
        take(db.execute(
            select(CandidateSkill.candidate_id).distinct()
            .where(CandidateSkill.skill_id.in_(all_skill_ids))
        ))
    take(db.execute(
        select(CandidateProfile.candidate_id).distinct()
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(Opportunity.customer_id == opp.customer_id)
    ))
    take(db.execute(
        select(CandidateProfile.candidate_id).distinct()
        .where(CandidateProfile.pipeline_status.in_(_LATE_STAGES))
    ))
    # A CV the ATS already rated well against SOME position is evidence too —
    # it is how a candidate with no typed-in skills can still be found.
    take(db.execute(
        select(Resume.candidate_id).distinct()
        .where(Resume.candidate_id.is_not(None), Resume.ats_score >= ATS_POOL_MIN)
        .order_by(Resume.candidate_id)
        .limit(_POOL_CAP)
    ))
    if not all_skill_ids and band is not None:
        lo, hi = band
        take(db.execute(
            select(Candidate.id).where(
                Candidate.experience_years >= Decimal(str(max(0.0, lo - 1))),
                Candidate.experience_years <= Decimal(str(hi + 1)),
            ).limit(_POOL_CAP)
        ))

    if not ordered:
        return []
    pool_ids = set(ordered[:_POOL_CAP])

    candidates = db.execute(
        select(Candidate).where(Candidate.id.in_(pool_ids))
    ).scalars().all()

    # ---- bulk-load everything the corpus and the history need (no per-row query)
    cand_skills: dict[int, set[int]] = {}
    cand_skill_names: dict[int, list[str]] = {}
    for cid, sid, name in db.execute(
        select(CandidateSkill.candidate_id, CandidateSkill.skill_id, Skill.name)
        .join(Skill, Skill.id == CandidateSkill.skill_id)
        .where(CandidateSkill.candidate_id.in_(pool_ids))
    ):
        cand_skills.setdefault(cid, set()).add(sid)
        cand_skill_names.setdefault(cid, []).append(name)

    corpus_bits: dict[int, list[str]] = {}
    for cid, company, title in db.execute(
        select(CandidateExperience.candidate_id, CandidateExperience.company_name,
               CandidateExperience.job_title)
        .where(CandidateExperience.candidate_id.in_(pool_ids))
    ):
        corpus_bits.setdefault(cid, []).extend(b for b in (company, title) if b)
    for cid, course in db.execute(
        select(CandidateEducation.candidate_id, CandidateEducation.course)
        .where(CandidateEducation.candidate_id.in_(pool_ids))
    ):
        if course:
            corpus_bits.setdefault(cid, []).append(course)

    # What the ATS already found in the candidate's resumes, plus their latest
    # ATS score (the fallback for an untyped skill list).
    ats_terms: dict[int, set[str]] = {}
    latest_ats: dict[int, tuple[float, int]] = {}      # cid -> (score, resume id)
    ats_by_requirement: dict[tuple[int, int], float] = {}
    for rid, cid, req_id, score, breakdown in db.execute(
        select(Resume.id, Resume.candidate_id, Resume.requirement_id, Resume.ats_score,
               Resume.ats_score_breakdown)
        .where(Resume.candidate_id.in_(pool_ids))
        .order_by(Resume.id.desc())
    ):
        if cid is None:
            continue
        if isinstance(breakdown, dict):
            for key in ("matched", "jd_keywords_matched"):
                vals = breakdown.get(key)
                if isinstance(vals, list):
                    ats_terms.setdefault(cid, set()).update(str(v) for v in vals if v)
        if score is not None:
            if cid not in latest_ats:
                latest_ats[cid] = (float(score), rid)
            key = (cid, req_id)
            ats_by_requirement[key] = max(ats_by_requirement.get(key, 0.0), float(score))

    history: dict[int, list] = {}
    profiles: list[CandidateProfile] = []
    for prof, opp_row, customer_name in db.execute(
        select(CandidateProfile, Opportunity, Customer.name)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        .where(CandidateProfile.candidate_id.in_(pool_ids))
        .order_by(CandidateProfile.id.desc())
    ):
        history.setdefault(prof.candidate_id, []).append((prof, opp_row, customer_name))
        profiles.append(prof)
    ai_by_profile = latest_ai_interviews(db, profiles) if profiles else {}
    req_opp: dict[int, int] = {}
    if ats_by_requirement:
        for rid, oid in db.execute(
            select(Requirement.id, Requirement.opportunity_id)
            .where(Requirement.id.in_({k[1] for k in ats_by_requirement}))
        ):
            req_opp[rid] = oid
    ats_by_opportunity: dict[tuple[int, int], float] = {}
    for (cid, rid), score in ats_by_requirement.items():
        oid = req_opp.get(rid)
        if oid is not None:
            key = (cid, oid)
            ats_by_opportunity[key] = max(ats_by_opportunity.get(key, 0.0), score)

    today = datetime.now(timezone.utc).date()
    results: list[dict] = []
    for c in candidates:
        have = cand_skills.get(c.id, set())
        corpus = " ; ".join(
            [*cand_skill_names.get(c.id, []), c.technical_domain or "", c.roles or "",
             *corpus_bits.get(c.id, []), *sorted(ats_terms.get(c.id, ()))]
        )
        strengths: list[dict] = []
        gaps: list[dict] = []
        earned = 0.0
        possible = 0.0

        def has_skill(sid: int) -> bool:
            return sid in have or _skill_present(skill_names[sid], corpus)[0]

        # ---- skills -----------------------------------------------------
        matched = sorted(skill_names[s] for s in all_skill_ids if has_skill(s))
        missing_mand = sorted(skill_names[s] for s in mandatory_ids if not has_skill(s))
        missing_opt = sorted(skill_names[s] for s in optional_ids if not has_skill(s))
        skills_from_ats = False
        if all_skill_ids:
            no_evidence = not have and not ats_terms.get(c.id)
            if no_evidence and c.id in latest_ats:
                # Nothing typed in and nothing learned from a scanned CV — but the
                # ATS DID score their latest resume. Use that rather than 0.
                skills_from_ats = True
                ats_score, _rid = latest_ats[c.id]
                pool = (W_MANDATORY if mandatory_ids else 0.0) + (W_OPTIONAL if optional_ids else 0.0)
                earned += pool * ats_score / 100.0
                possible += pool
                gaps.append({"area": "Skills",
                             "detail": f"No skills recorded — using the latest resume's ATS score ({ats_score:g}%) instead"})
            else:
                if mandatory_ids:
                    hit = len(mandatory_ids) - len(missing_mand)
                    earned += W_MANDATORY * hit / len(mandatory_ids)
                    possible += W_MANDATORY
                if optional_ids:
                    hit = len(optional_ids) - len(missing_opt)
                    earned += W_OPTIONAL * hit / len(optional_ids)
                    possible += W_OPTIONAL
                if matched:
                    strengths.append({"area": "Skills",
                                      "detail": "Has " + ", ".join(matched[:6])
                                      + (f" +{len(matched) - 6} more" if len(matched) > 6 else "")})
                if missing_mand:
                    gaps.append({"area": "Skills",
                                 "detail": "Missing mandatory: " + ", ".join(missing_mand)})
                if missing_opt:
                    gaps.append({"area": "Skills",
                                 "detail": "Missing optional: " + ", ".join(missing_opt)})

        # ---- JD fit -------------------------------------------------------
        jd_hit: list[str] = []
        jd_miss: list[str] = []
        if jd_keywords:
            for kw in jd_keywords:
                (jd_hit if _word_match(kw, corpus) else jd_miss).append(kw)
            earned += W_JD * len(jd_hit) / len(jd_keywords)
            possible += W_JD
            if jd_hit:
                strengths.append({"area": "JD fit",
                                  "detail": f"Matches {len(jd_hit)}/{len(jd_keywords)} JD terms: "
                                  + ", ".join(jd_hit[:_JD_TERMS_SHOWN])
                                  + (f" +{len(jd_hit) - _JD_TERMS_SHOWN} more" if len(jd_hit) > _JD_TERMS_SHOWN else "")})
            if jd_miss:
                gaps.append({"area": "JD fit",
                             "detail": "Not seen on file: " + ", ".join(jd_miss[:_JD_TERMS_SHOWN])
                             + (f" +{len(jd_miss) - _JD_TERMS_SHOWN} more" if len(jd_miss) > _JD_TERMS_SHOWN else "")})

        # ---- experience -------------------------------------------------
        exp = float(c.experience_years) if c.experience_years is not None else None
        if band is not None:
            lo, hi = band
            possible += W_EXPERIENCE
            if exp is None:
                gaps.append({"area": "Experience", "detail": "Experience not recorded"})
            elif lo <= exp <= hi:
                earned += W_EXPERIENCE
                strengths.append({"area": "Experience",
                                  "detail": f"{exp:g} yrs fits the {lo:g}–{hi:g} band"})
            elif (lo - 1) <= exp <= (hi + 1):
                earned += W_EXPERIENCE / 2
                gaps.append({"area": "Experience",
                             "detail": f"{exp:g} yrs is just outside the {lo:g}–{hi:g} band"})
            else:
                gaps.append({"area": "Experience",
                             "detail": f"{exp:g} yrs is outside the {lo:g}–{hi:g} band"})

        # ---- history ----------------------------------------------------
        rows = history.get(c.id, [])
        engaged = False
        same_customer = False
        late_stage = False
        recent_rejection: tuple[str, int] | None = None
        hist_out: list[dict] = []
        for prof, opp_row, customer_name in rows:
            status = prof.pipeline_status
            this_customer = opp_row.customer_id == opp.customer_id
            if status in _ENGAGED_STAGES:
                engaged = True
            if status in _LATE_STAGES:
                late_stage = True
            if this_customer:
                same_customer = True
            outcome = _outcome(status)
            when = _as_date(prof.updated_at) or _as_date(prof.applied_on)
            if this_customer and outcome == "rejected" and when is not None:
                age = (today - when).days
                if 0 <= age <= RECENT_REJECTION_DAYS:
                    if recent_rejection is None or age < recent_rejection[1]:
                        recent_rejection = (opp_row.title, age)
            ai = ai_by_profile.get(prof.id) or {}
            status_value = getattr(status, "value", status)
            hist_out.append({
                "profile_id": prof.id,
                "opportunity_id": opp_row.id,
                "opp_id": opp_row.opp_id,
                "opportunity_title": opp_row.title,
                "customer_id": opp_row.customer_id,
                "customer_name": customer_name,
                "this_customer": this_customer,
                "pipeline_status": status_value,
                "stage_label": stage_label(status_value),
                "outcome": outcome,
                "withdrawn_from": prof.withdrawn_from_status,
                "rmg_screening_status": prof.rmg_screening_status,
                "applied_on": _iso(prof.applied_on),
                "updated_at": _iso(prof.updated_at),
                "ai_result": ai.get("ai_effective_result"),
                "ai_score": ai.get("ai_overall_score_percent"),
                "ats_score": ats_by_opportunity.get((c.id, opp_row.id)),
            })
        possible += W_HISTORY
        if late_stage:
            earned += 6
            strengths.append({"area": "History",
                              "detail": "Reached a late pipeline stage before (proven performer)"})
        if same_customer:
            earned += 6
            strengths.append({"area": "History", "detail": "Previously applied to this customer"})
        if rows:
            earned += 3
            if not late_stage and not same_customer:
                strengths.append({"area": "History",
                                  "detail": f"{len(rows)} previous application(s) on record"})
        else:
            gaps.append({"area": "History", "detail": "No previous application with us"})

        # ---- completeness ----------------------------------------------
        possible += W_CONTACT
        if c.cv_url:
            earned += 3
        else:
            gaps.append({"area": "Contact", "detail": "No CV on file"})
        if c.phone:
            earned += 2
        else:
            gaps.append({"area": "Contact", "detail": "No phone number"})

        if possible <= 0 or earned <= 0:
            continue
        score = 100.0 * earned / possible
        penalty = 0.0
        if recent_rejection is not None:
            title, age = recent_rejection
            penalty = RECENT_REJECTION_PENALTY
            months = max(1, round(age / 30))
            gaps.insert(0, {"area": "History",
                            "detail": f"Rejected at this customer {months} month(s) ago ({title}) — −{penalty:g} pts"})
        score = max(0.0, score - penalty)
        if score <= 0:
            continue

        name = " ".join(p for p in [c.first_name, c.last_name] if p)
        last = hist_out[0] if hist_out else None
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
            "missing_optional_skills": missing_opt,
            "jd_terms_matched": jd_hit,
            "jd_terms_missing": jd_miss,
            "skills_from_ats": skills_from_ats,
            "strengths": strengths,
            "gaps": gaps,
            "penalty": penalty,
            # Kept for older callers: the strengths as plain lines.
            "reasons": [s["detail"] for s in strengths],
            "engaged": engaged,
            "applications_count": len(rows),
            "history": hist_out,
            "last_application": {
                "opportunity_title": last["opportunity_title"],
                "pipeline_status": last["pipeline_status"],
            } if last else None,
        })

    results.sort(key=lambda r: (-r["score"], r["candidate_id"]))
    return results[:_RESULT_CAP]
