"""Screening Desk (25 Sep 2026) — RMG / GM work every TA-applied candidate in one place.

Before this, screening meant opening each opportunity's requirement, finding
the Applied Candidates tab, opening the resume, pressing ATS, then Shortlist.
The desk is one queue across every live opportunity, grouped by POSITION (the
opportunity's requirement), with the resume, the ATS score and the decision on
the same screen.

Rules that are easy to get wrong:

* **Queue** = profiles still on the TA / RMG side of the pipeline
  (`DESK_STAGES`), not hidden, whose opportunity's LATEST requirement is live
  (`TA_LIVE_STATUSES`). A shortlisted candidate stays listed until they move
  on, so "what did I clear this week" is answerable; once with Sales they
  leave.
* **ATS is automatic.** Uploads already score on arrival; the gaps are
  profile-only applicants (applied from the Candidates page — no resume row)
  and scans that failed. `score_profiles` fills them for the rows on screen.
  It runs the scan ONLY — never `auto_pipeline_after_scan`: opening a page
  must not auto-shortlist anyone or email a candidate a slot invite. The
  decision on this screen is RMG's.
* **Internal candidate** = the person is an ACTIVE Karnex employee: the
  candidate's email is an employee's official or personal address, HR typed
  that employee's Emp ID on the profile, or the employee was created from
  this very profile. Derived, never stored — `profile_type` cannot answer it
  (every employee is Internal, 2 Sep 2026).
* **Fast-track** (`fast_track_internal`) moves an internal candidate from
  `FAST_TRACK_FROM` straight to Sales_Screening — the one jump the transition
  map does not model, so it is its own approval action with a reason, an
  activity-log line and the same arrival side effects as a normal move.
* **The whole ladder lives here (28 Sep 2026).** RMG / GM asked to never
  open Opportunities or Candidate Profiles for their own work, so every row
  now carries the AI L1 outcome (`ai_l1`), the manual L1 / L2 state
  (`rounds`, from the SAME `manual_round_state` the Applied Candidates tab
  reads), a plain-words `next_step` (whose move it is and what it is) and
  `decision` (may Submit to Sales / Reject be pressed, and if not, why —
  the same "record the L1 first" rule the requirement page enforces). A
  fifth tab, **review** (= stage RMG_Review), holds the candidates whose
  rounds are running; "shortlisted" is now the rows BEFORE review (route
  chosen or awaited). Position headers carry `jd_missing` + the JD/skills
  so the ATS gap is fixed from the header, and `meta.approvals` lists the
  positions waiting for RMG approval when the caller may give it. The
  actions themselves stay on the profile endpoints (rounds, feedback,
  status transition, skill evaluation) — the desk renders, never re-implements.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Employee,
    Opportunity, PipelineStatus, Requirement, RequirementStatus, Resume,
)
from services.candidate_status import TA_HOLD
from services.crm_common import log_activity

logger = logging.getLogger("karnex.crm.screening_desk")

PS = PipelineStatus

#: The stages the desk shows: still with TA / RMG, not yet with Sales.
DESK_STAGES: tuple[str, ...] = (
    PS.SOURCING.value, PS.TECHNICAL_SCREENING.value, PS.RMG_REVIEW.value,
)
#: Where an internal candidate may be fast-tracked FROM. Same set on purpose:
#: past RMG Review the candidate is already with Sales.
FAST_TRACK_FROM: tuple[str, ...] = DESK_STAGES
FAST_TRACK_TO = PS.SALES_SCREENING.value

SCREENING_PENDING, SCREENING_SHORTLISTED, SCREENING_REJECTED = "Pending", "Shortlisted", "Rejected"
REVIEW_STAGE = PS.RMG_REVIEW.value
#: ?screening= values → the rmg_screening_status they select ("all" = no filter).
#: "review" is the stage, not a screening status: shortlisted candidates whose
#: L1 / L2 rounds are running and who wait for RMG's decision. "shortlisted"
#: therefore means shortlisted and NOT yet in review — one row, one tab.
SCREENING_FILTERS: dict[str, str | None] = {
    "pending": SCREENING_PENDING,
    "shortlisted": SCREENING_SHORTLISTED,
    "review": SCREENING_SHORTLISTED,
    "rejected": SCREENING_REJECTED,
    "all": None,
}
SCREENING_REVIEW = "review"


def screening_tab_for(screening: str | None, stage: str | None) -> str:
    """Which tab a row belongs to (PURE; the counts and the filter agree)."""
    if screening == SCREENING_PENDING:
        return "pending"
    if screening == SCREENING_REJECTED:
        return "rejected"
    if screening == SCREENING_SHORTLISTED:
        return SCREENING_REVIEW if stage == REVIEW_STAGE else "shortlisted"
    return "all"

#: ATS bands. The same thresholds colour the score on screen (server decides).
ATS_HIGH = 70.0
ATS_MEDIUM = 50.0
ATS_BANDS = ("high", "medium", "low", "unscored")

SORTS = ("newest", "oldest", "ats", "ats_low", "experience")
#: Derived filters (28 Sep 2026 — "search with all required filters"): these
#: read the ladder, not a column, so they are applied after the SQL filters.
NEXT_OWNERS = ("you", "TA", "candidate", "AI", "done")
ROUTES = ("ai", "manual", "none")
EXP_FITS = ("in", "out", "unknown")
#: 29 Sep 2026 filters (user ask: "whatever filter GM & RMG need"). SQL: expected
#: CTC vs the position's budget, requirement priority, waiting at least N days,
#: an experience range. Derived (read per row): notice bucket, AI L1 outcome,
#: manual L1 verdict.
BUDGET_FITS = ("over", "within", "unknown")
PRIORITIES = ("High", "Medium", "Low")
NOTICE_BUCKETS = ("15", "30", "60", "90", "unknown")
AI_RESULTS = ("passed", "failed", "pending", "none")
L1_RESULTS = ("hire", "no_hire", "awaiting", "none")
#: Rows read when a derived filter is on (a to-do list, not a report).
MAX_DERIVED_ROWS = 2000
DEFAULT_LIMIT = 100
MAX_LIMIT = 200
#: Profiles scored per request. Each scan may call the AI review (seconds), so
#: the client walks the unscored rows in small batches rather than one long call.
MAX_SCORE_BATCH = 5
MIN_FAST_TRACK_NOTE = 10


@dataclass(frozen=True)
class DeskFilters:
    screening: str = "pending"
    customer_id: int | None = None
    opportunity_id: int | None = None
    requirement_id: int | None = None
    ta_owner_id: int | None = None
    applied_from: date | None = None
    applied_to: date | None = None
    ats_band: str | None = None
    internal: bool | None = None
    search: str | None = None
    sort: str = "newest"
    #: `?task=` (28 Sep 2026): the rows of one RMG / GM task category
    #: (`services/rmg_tasks.task_profile_ids`) — an empty tuple matches nothing.
    profile_ids: tuple[int, ...] | None = None
    #: Whose move it is (next_step.owner) · the interview route · experience vs
    #: the position's band · the candidate's city · only rows with a result to review.
    next_owner: str | None = None
    route: str | None = None
    exp_fit: str | None = None
    location: str | None = None
    new_results: bool | None = None
    budget: str | None = None
    priority: str | None = None
    waiting_min: int | None = None
    exp_min: float | None = None
    exp_max: float | None = None
    notice: str | None = None
    ai_result: str | None = None
    l1_result: str | None = None

    @property
    def derived(self) -> bool:
        return bool(self.next_owner or self.route or self.new_results or self.notice
                    or self.ai_result or self.l1_result)

    def validate(self) -> "DeskFilters":
        if self.screening not in SCREENING_FILTERS:
            raise HTTPException(status_code=400,
                                detail=f"screening must be one of {', '.join(SCREENING_FILTERS)}")
        if self.ats_band and self.ats_band not in ATS_BANDS:
            raise HTTPException(status_code=400,
                                detail=f"ats_band must be one of {', '.join(ATS_BANDS)}")
        for value, allowed, name in ((self.next_owner, NEXT_OWNERS, "next_owner"), (self.route, ROUTES, "route"),
                                     (self.exp_fit, EXP_FITS, "exp_fit"), (self.budget, BUDGET_FITS, "budget"),
                                     (self.priority, PRIORITIES, "priority"),
                                     (self.notice, NOTICE_BUCKETS, "notice"),
                                     (self.ai_result, AI_RESULTS, "ai_result"),
                                     (self.l1_result, L1_RESULTS, "l1_result")):
            if value and value not in allowed:
                raise HTTPException(status_code=400, detail=f"{name} must be one of {', '.join(allowed)}")
        if self.sort not in SORTS:
            raise HTTPException(status_code=400, detail=f"sort must be one of {', '.join(SORTS)}")
        if self.applied_from and self.applied_to and self.applied_from > self.applied_to:
            raise HTTPException(status_code=400, detail="'Applied from' is after 'Applied to'")
        if self.exp_min is not None and self.exp_max is not None and self.exp_min > self.exp_max:
            raise HTTPException(status_code=400, detail="Minimum experience is above the maximum")
        if self.waiting_min is not None and self.waiting_min < 0:
            raise HTTPException(status_code=400, detail="waiting_min cannot be negative")
        return self


# ------------------------------------------------------------------ pure helpers

def ats_band(score) -> str:
    if score is None:
        return "unscored"
    value = float(score)
    if value >= ATS_HIGH:
        return "high"
    if value >= ATS_MEDIUM:
        return "medium"
    return "low"


def notice_days(text) -> int | None:
    """A free-text notice period in days ("Immediate" → 0, "30 days", "2 months",
    "3 weeks", "45"). None when nothing can be read. PURE."""
    if text is None:
        return None
    t = str(text).strip().lower()
    if not t:
        return None
    if "immediate" in t or t in ("0", "nil", "none", "serving notice"):
        return 0
    import re
    m = re.search(r"(\d+(?:\.\d+)?)", t)
    if not m:
        return None
    n = float(m.group(1))
    if "month" in t:
        n *= 30
    elif "week" in t:
        n *= 7
    return int(round(n))


def notice_bucket(text) -> str:
    """"15" (≤ 15 days / immediate) · "30" · "60" · "90" (more than 60) · "unknown". PURE."""
    days = notice_days(text)
    if days is None:
        return "unknown"
    return "15" if days <= 15 else "30" if days <= 30 else "60" if days <= 60 else "90"


def ai_result_bucket(ai: dict | None) -> str:
    """The AI L1 as a filter value: passed · failed · pending (link, not done) · none. PURE."""
    if not ai:
        return "none"
    eff = (ai.get("ai_effective_result") or "").strip()
    if eff in ("Passed", "Selected"):
        return "passed"
    if eff in ("Failed", "Rejected"):
        return "failed"
    return "pending"


def l1_result_bucket(rounds: dict | None) -> str:
    """The manual Technical L1 as a filter value: hire · no_hire · awaiting · none. PURE."""
    r = rounds or {}
    result = (r.get("l1_manual_result") or "").strip()
    if result in ("Hire", "Strong Hire", "Leaning Hire"):
        return "hire"
    if result in ("No Hire", "Leaning No"):
        return "no_hire"
    if r.get("l1_manual_scheduled") or r.get("l1_manual_requested"):
        return "awaiting"
    return "none"


def _status(value) -> str:
    return getattr(value, "value", value)


def _num(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value is not None else None


def ats_summary(breakdown: dict | None) -> dict | None:
    """The part of the ATS breakdown a screener reads: which skills hit and
    missed, the experience found, and the AI reviewer's one-line view."""
    if not breakdown:
        return None
    details = breakdown.get("score_details") or {}
    ai = breakdown.get("ai_review") or {}
    return {
        "skills_matched": list(breakdown.get("skills_matched") or [])[:20],
        "skills_missing": list(breakdown.get("skills_missing") or [])[:20],
        "jd_keywords_matched": len(breakdown.get("jd_keywords_matched") or []),
        "jd_keywords_total": (len(breakdown.get("jd_keywords_matched") or [])
                              + len(breakdown.get("jd_keywords_missing") or [])),
        "experience_years": details.get("detected_experience_years"),
        "experience_match": breakdown.get("experience_match"),
        "ai_summary": (ai.get("summary") if isinstance(ai, dict) and not ai.get("unavailable") else None),
    }


# ------------------------------------------------------------------ SQL pieces

def _latest_requirement():
    """opportunity_id → its latest requirement id (the position the desk shows)."""
    return (select(Requirement.opportunity_id.label("opportunity_id"),
                   func.max(Requirement.id).label("requirement_id"))
            .group_by(Requirement.opportunity_id)
            .subquery("desk_req"))


def _latest_resume():
    """(opportunity, candidate) → the latest resume on ANY of the opportunity's
    requirements — the SAME rule as the profiles list's ATS column
    (enrich_profiles_list / _LATEST_ATS_SCORE), so one candidate can never show
    two different scores on two screens."""
    return (select(Requirement.opportunity_id.label("opportunity_id"),
                   Resume.candidate_id.label("candidate_id"),
                   func.max(Resume.id).label("resume_id"))
            .join(Requirement, Requirement.id == Resume.requirement_id)
            .where(Resume.candidate_id.isnot(None))
            .group_by(Requirement.opportunity_id, Resume.candidate_id)
            .subquery("desk_resume"))


def internal_clause():
    """EXISTS an active employee who IS this candidate (see module docstring)."""
    cand_email = func.lower(Candidate.email)
    ref = func.lower(func.trim(func.coalesce(CandidateProfile.employee_ref, "")))
    return (
        select(Employee.id)
        .where(
            Employee.is_active.is_(True),
            or_(
                func.lower(Employee.email) == cand_email,
                func.lower(func.coalesce(Employee.personal_email, "")) == cand_email,
                Employee.candidate_profile_id == CandidateProfile.id,
                sa.and_(ref != "", func.lower(func.coalesce(Employee.employee_code, "")) == ref),
            ),
        )
        .correlate(CandidateProfile, Candidate)
        .exists()
    )


def _base(filters: DeskFilters, *, with_screening: bool = True):
    """(select-from with every join, [where clauses]) for the filtered queue."""
    from services.requirements import TA_LIVE_STATUSES

    lr = _latest_requirement()
    lres = _latest_resume()
    joined = (
        select(CandidateProfile.id)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .join(lr, lr.c.opportunity_id == CandidateProfile.opportunity_id)
        .join(Requirement, Requirement.id == lr.c.requirement_id)
        .outerjoin(lres, (lres.c.opportunity_id == CandidateProfile.opportunity_id)
                   & (lres.c.candidate_id == CandidateProfile.candidate_id))
        .outerjoin(Resume, Resume.id == lres.c.resume_id)
    )
    where = [
        CandidateProfile.pipeline_status.in_([PS(s) for s in DESK_STAGES]),
        CandidateProfile.is_hidden.is_(False),
        Requirement.status.in_(list(TA_LIVE_STATUSES)),
        # TA parked it (over budget, 28 Sep 2026) — not RMG's to screen yet.
        or_(CandidateProfile.budget_status.is_(None), CandidateProfile.budget_status != TA_HOLD),
        # Not sent by TA yet (28 Sep 2026): an upload waits at Sourcing until TA
        # presses "Technical Screening". RMG Review rows always show.
        or_(CandidateProfile.rmg_screening_status.isnot(None),
            CandidateProfile.pipeline_status == PS(REVIEW_STAGE)),
    ]
    if with_screening:
        wanted = SCREENING_FILTERS[filters.screening]
        if wanted is not None:
            where.append(CandidateProfile.rmg_screening_status == wanted)
        if filters.screening == SCREENING_REVIEW:
            where.append(CandidateProfile.pipeline_status == PS(REVIEW_STAGE))
        elif filters.screening == "shortlisted":
            where.append(CandidateProfile.pipeline_status != PS(REVIEW_STAGE))
    if filters.location:
        where.append(Candidate.city.ilike(f"%{filters.location.strip()}%"))
    exp = Candidate.experience_years
    in_band = sa.and_(exp.isnot(None),
                      or_(Requirement.experience_min.is_(None), exp >= Requirement.experience_min),
                      or_(Requirement.experience_max.is_(None), exp <= Requirement.experience_max))
    if filters.exp_fit == "in":
        where.append(in_band)
    elif filters.exp_fit == "out":
        where.append(sa.and_(exp.isnot(None), ~in_band))
    elif filters.exp_fit == "unknown":
        where.append(exp.is_(None))
    if filters.exp_min is not None:
        where.append(exp >= filters.exp_min)
    if filters.exp_max is not None:
        where.append(exp <= filters.exp_max)
    expected = func.coalesce(CandidateProfile.expected_ctc, Candidate.expected_ctc)
    cap = Requirement.budget_ctc_max
    if filters.budget == "over":
        where.append(sa.and_(expected.isnot(None), cap.isnot(None), cap > 0, expected > cap))
    elif filters.budget == "within":
        where.append(sa.and_(expected.isnot(None), cap.isnot(None), cap > 0, expected <= cap))
    elif filters.budget == "unknown":
        where.append(or_(expected.is_(None), cap.is_(None), cap == 0))
    if filters.priority:
        from models.requirements import Priority
        where.append(Requirement.priority == Priority(filters.priority))
    if filters.profile_ids is not None:
        where.append(CandidateProfile.id.in_(list(filters.profile_ids) or [-1]))
    if filters.customer_id is not None:
        where.append(Opportunity.customer_id == filters.customer_id)
    if filters.opportunity_id is not None:
        where.append(CandidateProfile.opportunity_id == filters.opportunity_id)
    if filters.requirement_id is not None:
        where.append(Requirement.id == filters.requirement_id)
    if filters.ta_owner_id is not None:
        where.append(CandidateProfile.ta_owner_id == filters.ta_owner_id)
    applied = func.date(func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at))
    if filters.applied_from is not None:
        where.append(applied >= filters.applied_from)
    if filters.applied_to is not None:
        where.append(applied <= filters.applied_to)
    if filters.waiting_min:
        from datetime import timedelta
        where.append(applied <= date.today() - timedelta(days=int(filters.waiting_min)))
    if filters.ats_band == "unscored":
        where.append(Resume.ats_score.is_(None))
    elif filters.ats_band == "high":
        where.append(Resume.ats_score >= ATS_HIGH)
    elif filters.ats_band == "medium":
        where.append(sa.and_(Resume.ats_score >= ATS_MEDIUM, Resume.ats_score < ATS_HIGH))
    elif filters.ats_band == "low":
        where.append(Resume.ats_score < ATS_MEDIUM)
    if filters.internal is True:
        where.append(internal_clause())
    elif filters.internal is False:
        where.append(~internal_clause())
    if filters.search:
        like = f"%{filters.search.strip()}%"
        full = Candidate.first_name + " " + func.coalesce(Candidate.last_name, "")
        customer_names = select(Customer.id).where(Customer.name.ilike(like))
        where.append(or_(full.ilike(like), Candidate.email.ilike(like), Candidate.phone.ilike(like),
                         Opportunity.title.ilike(like), Opportunity.opp_id.ilike(like),
                         Requirement.title.ilike(like), Requirement.req_number.ilike(like),
                         Candidate.city.ilike(like), Candidate.technical_domain.ilike(like),
                         Opportunity.customer_id.in_(customer_names)))
    return joined, where


# ------------------------------------------------------------------ internal lookup

def internal_matches(db: Session, pairs: list[tuple[CandidateProfile, Candidate]]) -> dict[int, dict]:
    """profile_id → the employee this candidate IS, with today's deployment.

    One employee query for the whole page, then matched in Python by the same
    three keys `internal_clause` uses, in precedence order (created from this
    profile › Emp ID › email)."""
    if not pairs:
        return {}
    emails = {(c.email or "").strip().lower() for _, c in pairs if c is not None and c.email}
    refs = {(p.employee_ref or "").strip().lower() for p, _ in pairs if (p.employee_ref or "").strip()}
    pids = {p.id for p, _ in pairs}
    conds = [Employee.candidate_profile_id.in_(pids)]
    if emails:
        conds += [func.lower(Employee.email).in_(emails),
                  func.lower(Employee.personal_email).in_(emails)]
    if refs:
        conds.append(func.lower(Employee.employee_code).in_(refs))
    employees = db.execute(
        select(Employee).where(Employee.is_active.is_(True), or_(*conds))
    ).scalars().all()
    if not employees:
        return {}
    by_profile = {e.candidate_profile_id: e for e in employees if e.candidate_profile_id}
    by_code = {(e.employee_code or "").strip().lower(): e for e in employees if e.employee_code}
    by_email: dict[str, Employee] = {}
    for e in employees:
        for addr in (e.email, e.personal_email):
            if addr:
                by_email.setdefault(addr.strip().lower(), e)

    from services.project_closure import deployment_by_employee
    deployment = deployment_by_employee(db, {e.id for e in employees})
    out: dict[int, dict] = {}
    for profile, cand in pairs:
        emp = (by_profile.get(profile.id)
               or by_code.get((profile.employee_ref or "").strip().lower())
               or by_email.get(((cand.email if cand else "") or "").strip().lower()))
        if emp is None:
            continue
        dep = deployment.get(emp.id, {})
        out[profile.id] = {
            "employee_id": emp.id,
            "employee_code": emp.employee_code,
            "name": " ".join(p for p in (emp.first_name, emp.last_name) if p),
            "deployment": dep.get("status"),
            "projects": dep.get("projects") or [],
            "is_resigned": bool(emp.is_resigned),
        }
    return out


def internal_employee_for(db: Session, profile: CandidateProfile) -> dict | None:
    cand = db.get(Candidate, profile.candidate_id)
    return internal_matches(db, [(profile, cand)]).get(profile.id)


def fast_track_block(profile: CandidateProfile, internal: dict | None) -> str | None:
    """Why this profile cannot be fast-tracked, or None when it can."""
    if internal is None:
        return ("Only an internal candidate — an existing Karnex employee — can skip the "
                "L1 and L2 rounds.")
    if internal.get("is_resigned"):
        return "This employee has resigned — send them through the normal rounds."
    status = _status(profile.pipeline_status)
    if status not in FAST_TRACK_FROM:
        return (f"The candidate is already at {status.replace('_', ' ')} — fast-track only "
                "applies while they are with TA or RMG.")
    return None


# ------------------------------------------------------------------ the queue

def desk_queue(db: Session, filters: DeskFilters, *, page: int = 1,
               limit: int = DEFAULT_LIMIT, today: date | None = None) -> tuple[list[dict], dict]:
    filters.validate()
    today = today or date.today()
    page = max(1, page)
    limit = min(max(1, limit), MAX_LIMIT)
    joined, where = _base(filters)

    order = {
        "newest": [func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at).desc(),
                   CandidateProfile.id.desc()],
        "oldest": [func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at).asc(),
                   CandidateProfile.id.asc()],
        "ats": [Resume.ats_score.desc().nullslast(), CandidateProfile.id.desc()],
        "ats_low": [Resume.ats_score.asc().nullslast(), CandidateProfile.id.desc()],
        "experience": [Candidate.experience_years.desc().nullslast(), CandidateProfile.id.desc()],
    }[filters.sort]
    if filters.derived:
        # Whose move / route / new result read the ladder: narrow the SQL result
        # with the SAME helpers the rows print, then page in Python.
        all_ids = db.execute(joined.where(*where).order_by(*order).limit(MAX_DERIVED_ROWS)).scalars().all()
        kept = _derived_keep(db, filters, all_ids)
        total = len(kept)
        ids = kept[(page - 1) * limit: page * limit]
    else:
        total = db.execute(
            select(func.count()).select_from(joined.where(*where).subquery())
        ).scalar_one()
        ids = db.execute(
            joined.where(*where).order_by(*order).offset((page - 1) * limit).limit(limit)
        ).scalars().all()

    rows = _serialize(db, ids, today)
    meta = {
        "page": page, "limit": limit, "total": total,
        "pages": max(1, -(-total // limit)),
        "counts": _screening_counts(db, filters),
        "positions": _positions(db, filters),
        "options": _options(db),
        "ats_thresholds": {"high": ATS_HIGH, "medium": ATS_MEDIUM},
        "max_score_batch": MAX_SCORE_BATCH,
        "round_results": list(ROUND_RESULTS),
    }
    return rows, meta


def _derived_keep(db: Session, filters: DeskFilters, ids: list[int]) -> list[int]:
    """The ids (in order) that pass the derived filters — `next_step.owner`,
    the interview route, and "has a result nobody reviewed"."""
    if not ids:
        return []
    from services.candidate_profiles import latest_ai_interviews
    from services.resumes import manual_round_state
    from services.rmg_tasks import unreviewed_results

    profiles = db.execute(select(CandidateProfile).where(CandidateProfile.id.in_(ids))).scalars().all()
    by_id = {p.id: p for p in profiles}
    ai = latest_ai_interviews(db, profiles)
    rounds = manual_round_state(db, ids)
    fresh = unreviewed_results(db, ids) if filters.new_results else {}
    notices = ({c.id: c.notice_period for c in db.execute(
        select(Candidate).where(Candidate.id.in_({p.candidate_id for p in profiles}))).scalars()}
        if filters.notice else {})
    kept = []
    for pid in ids:
        p = by_id.get(pid)
        if p is None:
            continue
        stage = _status(p.pipeline_status)
        if filters.next_owner:
            step = next_step(screening=p.rmg_screening_status, stage=stage, ai=ai.get(pid), rounds=rounds.get(pid))
            if step["owner"] != filters.next_owner:
                continue
        if filters.route:
            chosen = interview_route(screening=p.rmg_screening_status, stage=stage, ai=ai.get(pid),
                                     rounds=rounds.get(pid))["chosen"]
            if (chosen or "none") != filters.route:
                continue
        if filters.new_results and not fresh.get(pid):
            continue
        if filters.notice and notice_bucket(notices.get(p.candidate_id)) != filters.notice:
            continue
        if filters.ai_result and ai_result_bucket(ai.get(pid)) != filters.ai_result:
            continue
        if filters.l1_result and l1_result_bucket(rounds.get(pid)) != filters.l1_result:
            continue
        kept.append(pid)
    return kept


def _serialize(db: Session, ids: list[int], today: date) -> list[dict]:
    if not ids:
        return []
    lr = _latest_requirement()
    lres = _latest_resume()
    result = db.execute(
        select(CandidateProfile, Candidate, Opportunity, Customer, Requirement, Resume)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .join(lr, lr.c.opportunity_id == CandidateProfile.opportunity_id)
        .join(Requirement, Requirement.id == lr.c.requirement_id)
        .outerjoin(lres, (lres.c.opportunity_id == CandidateProfile.opportunity_id)
                   & (lres.c.candidate_id == CandidateProfile.candidate_id))
        .outerjoin(Resume, Resume.id == lres.c.resume_id)
        .where(CandidateProfile.id.in_(ids))
    ).all()
    by_id = {r[0].id: r for r in result}
    internal = internal_matches(db, [(r[0], r[1]) for r in result])

    out = []
    for pid in ids:   # keep the query's order
        row = by_id.get(pid)
        if row is None:
            continue
        profile, cand, opp, cust, req, resume = row
        applied = profile.applied_on or profile.created_at
        applied_day = applied.date() if isinstance(applied, datetime) else applied
        emp = internal.get(profile.id)
        score = _num(resume.ats_score) if resume is not None else None
        out.append({
            "profile_id": profile.id,
            "candidate_id": cand.id,
            "candidate_name": " ".join(p for p in (cand.first_name, cand.last_name) if p),
            "email": cand.email,
            "phone": cand.phone,
            "experience_years": _num(cand.experience_years),
            "notice_period": cand.notice_period,
            "current_ctc": _num(profile.current_ctc if profile.current_ctc is not None else cand.current_ctc),
            "expected_ctc": _num(profile.expected_ctc if profile.expected_ctc is not None else cand.expected_ctc),
            "location": cand.city,
            "technical_domain": cand.technical_domain,
            "opportunity_id": opp.id,
            "opp_id": opp.opp_id,
            "opportunity_title": opp.title,
            "customer_id": opp.customer_id,
            "customer_name": cust.name if cust is not None else None,
            "requirement_id": req.id,
            "req_number": req.req_number,
            "position_title": req.title,
            "positions": req.no_of_positions,
            "exp_min": _num(req.experience_min),
            "exp_max": _num(req.experience_max),
            "pipeline_status": _status(profile.pipeline_status),
            "rmg_screening_status": profile.rmg_screening_status,
            "rmg_screening_note": profile.rmg_screening_note,
            "rmg_screening_at": _iso(profile.rmg_screening_at),
            "ta_owner_id": profile.ta_owner_id,
            "ta_owner_name": profile.ta_owner_name,
            "applied_on": _iso(applied),
            "waiting_days": (today - applied_day).days if applied_day else None,
            "resume_id": resume.id if resume is not None else None,
            "resume_url": (resume.resume_file_url if resume is not None else None) or cand.cv_url,
            "has_cv": bool((resume is not None and resume.resume_file_url) or cand.cv_url),
            "ats_score": score,
            "ats_band": ats_band(score),
            "ats_status": _status(resume.ats_status) if resume is not None else None,
            "ats": ats_summary(resume.ats_score_breakdown) if resume is not None else None,
            "internal": emp,
            "fast_track_block": fast_track_block(profile, emp),
            "direct_to_sales_block": direct_to_sales_block(profile),
        })
    # The candidate status every screen shows (services/candidate_status.py).
    from services.candidate_status import attach_to_rows
    attach_to_rows(db, out)
    _attach_interview_route(db, out, [r[0] for r in result])
    # Finished interviews nobody has marked reviewed — the row's "New result"
    # highlight and the detail pane's review banner (services/rmg_tasks.py).
    from services.rmg_tasks import unreviewed_results
    fresh = unreviewed_results(db, [r["profile_id"] for r in out])
    for row in out:
        row["new_results"] = fresh.get(row["profile_id"], [])
    return out


#: Stages at which the interview ROUTE (AI L1 vs manual L1) is still RMG's to
#: pick — the same window the Applied Candidates row and the profile page use.
ROUTE_STAGES = (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value)


def interview_route(*, screening: str | None, stage: str | None, ai: dict | None,
                    rounds: dict | None) -> dict:
    """What has been decided about HOW this candidate is interviewed.

    `open` is True while RMG / GM still has to choose: screening Shortlisted,
    stage in ROUTE_STAGES, no AI L1 link yet and no manual L1 asked for or
    booked. PURE — the Screening Desk renders exactly this.
    """
    ai = ai or {}
    rounds = rounds or {}
    ai_status = ai.get("ai_interview_status")
    # Any AI link at all (Pending included) means the AI route was taken —
    # `latest_ai_interviews` only yields a row for profiles that have one.
    ai_taken = bool(ai) or bool(rounds.get("ai_l1_requested"))
    manual_requested = bool(rounds.get("l1_manual_requested"))
    manual_scheduled = bool(rounds.get("l1_manual_scheduled"))
    chosen = "ai" if ai_taken else "manual" if (manual_requested or manual_scheduled) else None
    return {
        "chosen": chosen,
        "open": (screening == "Shortlisted" and stage in ROUTE_STAGES and chosen is None),
        "ai_interview_status": ai_status,
        "ai_l1_requested": bool(rounds.get("ai_l1_requested")),
        "ai_effective_result": ai.get("ai_effective_result"),
        "ai_overall_score_percent": ai.get("ai_overall_score_percent"),
        "manual_l1_requested": manual_requested,
        "manual_l1_scheduled": manual_scheduled,
        "manual_l1_result": rounds.get("l1_manual_result"),
    }


#: The verdict scale of a manual round (mirror of interview_rounds.RESULTS).
ROUND_RESULTS = ("No Hire", "Leaning No", "Leaning Hire", "Hire", "Strong Hire")
_ROUND_KEYS = ("requested", "scheduled", "event_id", "result", "when", "link")


def ladder_state(rounds: dict | None) -> dict:
    """The manual L1 / L2 facts a desk row prints and acts on (PURE)."""
    rounds = rounds or {}
    out = {}
    for prefix, name in (("l1_manual", "l1"), ("l2", "l2")):
        for key in _ROUND_KEYS:
            out[f"{name}_{key}"] = rounds.get(f"{prefix}_{key}")
        out[f"{name}_requested"] = bool(out[f"{name}_requested"])
        out[f"{name}_scheduled"] = bool(out[f"{name}_scheduled"])
        out[f"{name}_link"] = bool(out[f"{name}_link"])
    # RMG / GM chose the AI route; TA is scheduling it (28 Sep 2026).
    out["ai_requested"] = bool(rounds.get("ai_l1_requested"))
    return out


def decision_state(*, stage: str | None, rounds: dict | None) -> dict:
    """May Submit to Sales / Reject be pressed on this row, and if not, why.

    The SAME rule the requirement page enforces (1 Sep + 8 Sep 2026): the
    ladder has to be JUDGED before Sales sees anyone — a manual L1 that was
    asked for or booked must carry a result, and so must an L2 that was asked
    for or booked. The L2 itself is optional. PURE.
    """
    l = ladder_state(rounds)
    if stage != REVIEW_STAGE:
        return {"can_decide": False, "blocked": None}
    if (l["l1_requested"] or l["l1_scheduled"]) and not l["l1_result"]:
        return {"can_decide": True, "blocked": "Record the manual L1 outcome first"}
    if (l["l2_requested"] or l["l2_scheduled"]) and not l["l2_result"]:
        return {"can_decide": True, "blocked": "Record the L2 outcome first"}
    return {"can_decide": True, "blocked": None}


def next_step(*, screening: str | None, stage: str | None, ai: dict | None,
              rounds: dict | None) -> dict:
    """Whose move it is on this candidate, in plain words (PURE).

    `{key, label, owner ("you" | "TA" | "candidate" | "AI" | "done"), tone}` —
    the row's one-line hint and the detail pane's headline. Keys are stable
    so the UI can key icons off them; labels are what a screener reads.
    """
    ai = ai or {}
    l = ladder_state(rounds)
    route = interview_route(screening=screening, stage=stage, ai=ai, rounds=rounds)

    def step(key, label, owner="you", tone="warn"):
        return {"key": key, "label": label, "owner": owner, "tone": tone}

    if screening == SCREENING_REJECTED:
        return step("rejected", "Rejected at screening", "done", "bad")
    if screening != SCREENING_SHORTLISTED:
        return step("screen", "Read the resume, then shortlist or reject")
    # manual ladder, whichever stage it runs in
    if l["l1_scheduled"] and not l["l1_result"]:
        return step("l1_feedback", "L1 booked — record the outcome after the call")
    if l["l1_requested"] and not l["l1_scheduled"]:
        return step("l1_book", "Manual L1 requested — TA is booking it (or book it yourself)", "TA", "none")
    if l["l2_scheduled"] and not l["l2_result"]:
        return step("l2_feedback", "L2 booked — record the outcome after the call")
    if l["l2_requested"] and not l["l2_scheduled"]:
        return step("l2_book", "L2 requested — TA is booking it (or book it yourself)", "TA", "none")
    if stage == REVIEW_STAGE:
        return step("decide", "Rounds done — submit to Sales or reject")
    if route["open"]:
        return step("route", "Choose the interview route — AI L1 or manual L1")
    if route["chosen"] == "ai":
        if not ai:
            return step("ai_book", "AI L1 chosen — TA is scheduling it", "TA", "none")
        result = ai.get("ai_effective_result")
        if result in (None, "Pending"):
            return step("ai_wait", "AI L1 scheduled — waiting for the candidate to take it", "candidate", "none")
        if result in ("Failed", "Rejected"):
            return step("ai_failed", "AI L1 not cleared — reject, or override the verdict on the profile", "you", "bad")
        return step("ai_done", "AI L1 cleared — moving to review", "AI", "ok")
    return step("wait", "Waiting", "done", "none")


def _attach_interview_route(db: Session, rows: list[dict], profiles: list) -> None:
    """Add `interview_route`, `ai_l1`, `rounds`, `next_step` and `decision` to
    every desk row — two batched queries, the SAME helpers the Applied
    Candidates tab reads, so the two never disagree."""
    from services.candidate_profiles import latest_ai_interviews
    from services.resumes import manual_round_state

    ai = latest_ai_interviews(db, profiles)
    rounds = manual_round_state(db, [p.id for p in profiles])
    for row in rows:
        pid = row["profile_id"]
        screening, stage = row.get("rmg_screening_status"), row.get("pipeline_status")
        row["interview_route"] = interview_route(screening=screening, stage=stage,
                                                 ai=ai.get(pid), rounds=rounds.get(pid))
        row["ai_l1"] = ai.get(pid)
        row["rounds"] = ladder_state(rounds.get(pid))
        row["next_step"] = next_step(screening=screening, stage=stage, ai=ai.get(pid),
                                     rounds=rounds.get(pid))
        row["decision"] = decision_state(stage=stage, rounds=rounds.get(pid))
        row["tab"] = screening_tab_for(screening, stage)


def _screening_counts(db: Session, filters: DeskFilters) -> dict[str, int]:
    """How many rows each screening tab would show under the other filters."""
    joined, where = _base(filters, with_screening=False)
    sub = (joined.add_columns(CandidateProfile.rmg_screening_status.label("s"),
                              CandidateProfile.pipeline_status.label("st"))
           .where(*where).subquery())
    counts = {k: 0 for k in SCREENING_FILTERS}
    for status, stage, n in db.execute(select(sub.c.s, sub.c.st, func.count())
                                       .group_by(sub.c.s, sub.c.st)).all():
        counts["all"] += n
        tab = screening_tab_for(status, _status(stage))
        if tab in counts and tab != "all":
            counts[tab] += n
    return counts


def _positions(db: Session, filters: DeskFilters) -> list[dict]:
    """Every position with at least one row under the current filters — the
    group headers, counted over the WHOLE result, not just this page."""
    joined, where = _base(filters)
    sub = (joined.add_columns(Requirement.id.label("rid"),
                              CandidateProfile.rmg_screening_status.label("s"),
                              CandidateProfile.pipeline_status.label("st"))
           .where(*where).subquery())
    grouped = db.execute(
        select(sub.c.rid, func.count(),
               func.sum(sa.case((sub.c.s == SCREENING_PENDING, 1), else_=0)),
               func.sum(sa.case((sub.c.st == PS(REVIEW_STAGE), 1), else_=0)))
        .group_by(sub.c.rid)
    ).all()
    if not grouped:
        return []
    reqs = {
        r.id: (r, o, c) for r, o, c in db.execute(
            select(Requirement, Opportunity, Customer)
            .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
            .outerjoin(Customer, Customer.id == Opportunity.customer_id)
            .where(Requirement.id.in_([g[0] for g in grouped]))
        ).all()
    }
    skills = _skills_by_requirement(db, list(reqs))
    out = []
    for rid, total, pending, review in grouped:
        if rid not in reqs:
            continue
        req, opp, cust = reqs[rid]
        out.append({
            "review": int(review or 0),
            "jd_missing": not (req.rmg_jd_text or "").strip() and not skills.get(rid),
            "description": req.description,
            "rmg_jd_text": req.rmg_jd_text,
            "skills": skills.get(rid, []),
            "requirement_id": rid,
            "req_number": req.req_number,
            "position_title": req.title,
            "positions": req.no_of_positions,
            "opportunity_id": opp.id,
            "opp_id": opp.opp_id,
            "opportunity_title": opp.title,
            "customer_id": opp.customer_id,
            "customer_name": cust.name if cust is not None else None,
            "requirement_status": _status(req.status),
            "count": int(total or 0),
            "pending": int(pending or 0),
        })
    out.sort(key=lambda p: (-p["pending"], -p["count"], p["customer_name"] or "", p["position_title"] or ""))
    return out


def _skills_by_requirement(db: Session, requirement_ids: list[int]) -> dict[int, list[dict]]:
    """requirement_id → its Skill Evaluation Details, ONE query for the page."""
    if not requirement_ids:
        return {}
    from models import RequirementSkill, Skill
    out: dict[int, list[dict]] = {}
    for rs, name in db.execute(
        select(RequirementSkill, Skill.name)
        .outerjoin(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id.in_(requirement_ids))
        .order_by(RequirementSkill.requirement_id, RequirementSkill.id)
    ).all():
        out.setdefault(rs.requirement_id, []).append({
            "skill_id": rs.skill_id, "skill_name": name,
            "is_mandatory": bool(rs.is_mandatory), "min_rating": rs.min_rating,
        })
    return out


#: The requirement approval RMG gives before TA may source.
APPROVE_ACTION = "requirement.engineering_approve"


def approvals_queue(db: Session, user: CurrentUser) -> dict:
    """Positions waiting for RMG approval, when THIS user may give it.

    `{"can_approve": bool, "items": [...]}` — items only when allowed, so the
    desk shows the strip to exactly the people whose click would succeed
    (`user_may`, the same decision the approve endpoint's gate makes). One
    query plus the skills batch; ordered oldest first (the longest wait on top).
    """
    from services.action_permissions import user_may
    try:
        allowed = user_may(db, user, APPROVE_ACTION)
    except Exception:
        allowed = False
    if not allowed:
        return {"can_approve": False, "items": []}
    from models import RequirementAttachment
    rows = db.execute(
        select(Requirement, Opportunity, Customer)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .where(Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)
        .order_by(Requirement.created_at.asc(), Requirement.id.asc())
    ).all()
    ids = [r.id for r, _, _ in rows]
    skills = _skills_by_requirement(db, ids)
    with_file = {row[0] for row in db.execute(
        select(RequirementAttachment.requirement_id)
        .where(RequirementAttachment.requirement_id.in_(ids), RequirementAttachment.kind == "rmg_jd")
    ).all()} if ids else set()
    items = []
    for req, opp, cust in rows:
        items.append({
            "requirement_id": req.id,
            "req_number": req.req_number,
            "title": req.title,
            "positions": req.no_of_positions,
            "opportunity_id": opp.id,
            "opp_id": opp.opp_id,
            "opportunity_title": opp.title,
            "customer_id": opp.customer_id,
            "customer_name": cust.name if cust is not None else None,
            "created_at": _iso(req.created_at),
            "description": req.description,
            "rmg_jd_text": req.rmg_jd_text,
            "has_jd_file": req.id in with_file,
            "skills": skills.get(req.id, []),
            "exp_min": _num(req.experience_min),
            "exp_max": _num(req.experience_max),
        })
    return {"can_approve": True, "items": items}


def _options(db: Session) -> dict:
    """Filter dropdowns: everything that appears anywhere in the unfiltered desk."""
    joined, where = _base(DeskFilters(screening="all"), with_screening=False)
    sub = (joined.add_columns(Opportunity.customer_id.label("cid"),
                              CandidateProfile.ta_owner_id.label("ta"),
                              CandidateProfile.ta_owner_name.label("ta_name"),
                              Opportunity.id.label("oid"),
                              Requirement.id.label("rid"))
           .where(*where).subquery())
    cust_ids = {r[0] for r in db.execute(select(sub.c.cid).distinct()).all() if r[0]}
    customers = [
        {"id": cid, "name": name}
        for cid, name in db.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids)).order_by(Customer.name)
        ).all()
    ] if cust_ids else []
    tas = sorted(
        ({"id": ta, "name": name or f"User #{ta}"}
         for ta, name in db.execute(select(sub.c.ta, func.max(sub.c.ta_name))
                                    .where(sub.c.ta.isnot(None)).group_by(sub.c.ta)).all()),
        key=lambda t: t["name"].lower(),
    )
    opp_ids = {r[0] for r in db.execute(select(sub.c.oid).distinct()).all()}
    opportunities = [
        {"id": oid, "label": f"{code} — {title}", "customer_id": cid}
        for oid, code, title, cid in db.execute(
            select(Opportunity.id, Opportunity.opp_id, Opportunity.title, Opportunity.customer_id)
            .where(Opportunity.id.in_(opp_ids)).order_by(Opportunity.opp_id)
        ).all()
    ] if opp_ids else []
    req_ids = {r[0] for r in db.execute(select(sub.c.rid).distinct()).all() if r[0]}
    positions = [
        {"id": rid, "label": f"{title} · {num}", "customer_id": cid, "opportunity_id": oid}
        for rid, num, title, cid, oid in db.execute(
            select(Requirement.id, Requirement.req_number, Requirement.title, Requirement.customer_id,
                   Requirement.opportunity_id).where(Requirement.id.in_(req_ids)).order_by(Requirement.title)
        ).all()
    ] if req_ids else []
    return {"customers": customers, "ta_owners": tas, "opportunities": opportunities, "positions": positions}


# ------------------------------------------------------------------ auto ATS

def score_profiles(db: Session, profile_ids: list[int], user: CurrentUser) -> dict:
    """Score the unscored desk rows named (at most MAX_SCORE_BATCH).

    Each profile runs in its own savepoint so one unreadable CV cannot roll back
    the others. Scan only — see the module docstring for why the auto-threshold
    pipeline is NOT run from here. The caller commits.
    """
    from services.resumes import ensure_resume_for_profile, run_ats_scan

    ids = list(dict.fromkeys(int(i) for i in profile_ids))[:MAX_SCORE_BATCH]
    scored: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    for pid in ids:
        profile = db.get(CandidateProfile, pid)
        if profile is None or _status(profile.pipeline_status) not in DESK_STAGES:
            skipped.append({"profile_id": pid, "reason": "Not on the screening desk"})
            continue
        resume = db.execute(
            select(Resume)
            .join(Requirement, Requirement.id == Resume.requirement_id)
            .where(Requirement.opportunity_id == profile.opportunity_id,
                   Resume.candidate_id == profile.candidate_id)
            .order_by(Resume.id.desc())
        ).scalars().first()
        if resume is not None and resume.ats_score is not None:
            skipped.append({"profile_id": pid, "reason": "Already scored",
                            "ats_score": _num(resume.ats_score)})
            continue
        try:
            with db.begin_nested():
                if resume is None:
                    req = db.execute(
                        select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
                        .order_by(Requirement.id.desc())
                    ).scalars().first()
                    if req is None:
                        raise HTTPException(status_code=400, detail="This opportunity has no requirement yet")
                    resume = ensure_resume_for_profile(db, profile, req)
                else:
                    req = db.get(Requirement, resume.requirement_id)
                result = run_ats_scan(db, resume, req, user.id)
            scored.append({"profile_id": pid, "resume_id": resume.id,
                           "ats_score": _num(result["ats_score"]),
                           "ats_band": ats_band(result["ats_score"])})
        except HTTPException as exc:
            failed.append({"profile_id": pid, "reason": str(exc.detail)})
        except Exception:   # one bad file must not stop the batch
            logger.warning("screening desk: ATS scan failed for profile %s", pid, exc_info=True)
            failed.append({"profile_id": pid, "reason": "The scan failed unexpectedly — try the ATS "
                                                        "button on the requirement page"})
    return {"scored": scored, "failed": failed, "skipped": skipped}


# ------------------------------------------------------------------ fast-track

def fast_track_internal(db: Session, profile: CandidateProfile, note: str,
                        user: CurrentUser) -> dict:
    """Internal candidate → Sales_Screening, skipping L1 / L2. Caller commits.

    Returns the matched employee. Raises 400 (not internal / short note) or 409
    (wrong stage) — the checks run before anything is written.
    """
    reason = (note or "").strip()
    if len(reason) < MIN_FAST_TRACK_NOTE:
        raise HTTPException(status_code=400,
                            detail=f"Give a reason of at least {MIN_FAST_TRACK_NOTE} characters — it is "
                                   "the only record of why the L1 and L2 rounds were skipped.")
    internal = internal_employee_for(db, profile)
    block = fast_track_block(profile, internal)
    if block:
        status_code = 409 if internal is not None and not internal.get("is_resigned") else 400
        raise HTTPException(status_code=status_code, detail=block)

    code = internal.get("employee_code") or f"employee #{internal['employee_id']}"
    if not (profile.employee_ref or "").strip() and internal.get("employee_code"):
        profile.employee_ref = internal["employee_code"]
    _send_straight_to_sales(db, profile, reason, user,
                            who=f"Internal candidate ({code})", tag="fast-track, internal",
                            why="is an internal candidate")
    return internal


#: Direct-to-Sales is allowed from the same stages as the internal fast-track.
DIRECT_TO_SALES_FROM = FAST_TRACK_FROM


def direct_to_sales_block(profile: CandidateProfile) -> str | None:
    """Why RMG / GM cannot send this profile straight to Sales, or None."""
    status = _status(profile.pipeline_status)
    if status not in DIRECT_TO_SALES_FROM:
        return (f"The candidate is already at {status.replace('_', ' ')} — a direct submission "
                "only applies while they are with TA or RMG.")
    return None


def direct_to_sales(db: Session, profile: CandidateProfile, note: str, user: CurrentUser) -> None:
    """RMG / GM's call: a strong match for the position goes straight to Sales for the
    customer round, skipping the technical L1 / L2 (30 Sep 2026, user ask). The
    reason is mandatory — it is the only record of why the rounds were skipped.
    Caller commits. Raises 400 (short note) or 409 (wrong stage)."""
    reason = (note or "").strip()
    if len(reason) < MIN_FAST_TRACK_NOTE:
        raise HTTPException(status_code=400,
                            detail=f"Give a reason of at least {MIN_FAST_TRACK_NOTE} characters — it is "
                                   "the only record of why the L1 and L2 rounds were skipped.")
    block = direct_to_sales_block(profile)
    if block:
        raise HTTPException(status_code=409, detail=block)
    _send_straight_to_sales(db, profile, reason, user,
                            who="Candidate", tag="direct to Sales",
                            why="was judged a strong match for the position")


def _send_straight_to_sales(db: Session, profile: CandidateProfile, reason: str, user: CurrentUser,
                            *, who: str, tag: str, why: str) -> None:
    """The ONE move to Sales_Screening that skips the technical ladder — shared by
    the internal fast-track and RMG / GM's direct submission. Checks run before."""
    from services.candidate_profiles import record_stage_arrival, stamp_technical_submission

    previous = _status(profile.pipeline_status)
    actor = user.full_name or user.username
    now = datetime.now(timezone.utc)

    # The screening gate is part of what is being skipped: record it as cleared
    # by the same person, so the AI-L1 lock and the desk both read "done".
    if profile.rmg_screening_status != SCREENING_SHORTLISTED:
        profile.rmg_screening_status = SCREENING_SHORTLISTED
        profile.rmg_screening_by = user.id
        profile.rmg_screening_at = now
        profile.rmg_screening_note = f"Sent straight to Sales: {reason}"[:1000]
    stamp_technical_submission(profile)

    profile.pipeline_status = PS(FAST_TRACK_TO)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "FAST_TRACKED",
                 f"{who} sent straight to Sales for customer screening by {actor} — "
                 f"L1 and L2 rounds skipped: {reason}")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "STATUS_CHANGE", f"{previous} -> {FAST_TRACK_TO}: [{tag}] {reason}")
    record_stage_arrival(db, profile, previous, FAST_TRACK_TO,
                         f"{who} — L1/L2 skipped: {reason}", user)
    _tell_ta(db, profile, reason, user, why=why)


def _tell_ta(db: Session, profile: CandidateProfile, reason: str, user: CurrentUser,
             *, why: str = "is an internal candidate") -> None:
    """The TA who applied them may be about to book an L1 — tell them not to."""
    if not profile.ta_owner_id or profile.ta_owner_id == user.id:
        return
    try:
        from services.notify import notify_user

        cand = db.get(Candidate, profile.candidate_id)
        name = " ".join(p for p in (getattr(cand, "first_name", None), getattr(cand, "last_name", None)) if p)
        opp = db.get(Opportunity, profile.opportunity_id)
        notify_user(
            db, profile.ta_owner_id,
            f"Sent to Sales directly: {name or 'candidate'}",
            f"{name} on {getattr(opp, 'opp_id', '')} — {getattr(opp, 'title', '')} {why} "
            f"and went straight to Sales for customer screening (L1 and L2 skipped): "
            f"{reason}. No interview needs booking.",
            f"/admin/?view=crm&p=profiles/{profile.id}",
            event="profile.fast_tracked", actor=user,
            related_type="candidate", related_id=profile.candidate_id,
        )
    except Exception:  # pragma: no cover — a notification never undoes the move
        logger.warning("fast-track: could not notify TA for profile %s", profile.id, exc_info=True)
