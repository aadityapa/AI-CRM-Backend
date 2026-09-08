"""Read-only aggregation queries behind /api/dashboard/* endpoints.

Everything here is SELECT-only (counts / sums / group-bys) — no commits.
Postgres-only features (date_trunc, FILTER aggregates) are OK per project rules.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink,
    AiInterviewStatus,
    AtsStatus,
    Candidate,
    CandidateProfile,
    Customer,
    CustomerStatus,
    Employee,
    Invoice,
    Opportunity,
    PaymentStatus,
    PipelineStage,
    PipelineStatus,
    POStatus,
    Project,
    ProjectStatus,
    PurchaseOrder,
    Requirement,
    RequirementStatus,
    Resume,
    TdsRecord,
    TdsStatus,
)

# Requirements considered "open positions" for sourcing dashboards.
OPEN_SOURCING_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)

# Requirements that can no longer contribute open positions.
TERMINAL_REQ_STATUSES = (
    RequirementStatus.FULFILLED,
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _num(value) -> float:
    """Decimal/None -> float for JSON."""
    return float(value) if value is not None else 0.0


def _usernames(db: Session, user_ids) -> dict[int, str]:
    """id -> username map from the legacy registration_data table (raw SQL —
    the ORM stub for that table only carries the id column)."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        sa.text("SELECT id, username FROM registration_data WHERE id IN :ids")
        .bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    return {row[0]: row[1] for row in rows}


def _ta_names(db: Session, user_ids) -> dict[int, str]:
    """id -> display name (full_name, else username) from registration_data."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        sa.text("SELECT id, full_name, username FROM registration_data WHERE id IN :ids")
        .bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    return {r[0]: (r[1] or r[2] or f"user:{r[0]}") for r in rows}


#: Profile statuses that count as a recruiter "win in progress" or "closed".
_TA_SHORTLIST_STATUSES = (
    PipelineStatus.SHORTLISTED,
    PipelineStatus.CUSTOMER_APPROVAL,
    PipelineStatus.PREBOARDING,
)
_TA_REJECT_STATUSES = (
    PipelineStatus.SALES_REJECTED,
    PipelineStatus.RMG_REJECTED,
    PipelineStatus.CUSTOMER_REJECTED,
    PipelineStatus.CUSTOMER_SCREEN_REJECTED,
    PipelineStatus.CUSTOMER_L1_REJECTED,
    PipelineStatus.CUSTOMER_L2_REJECTED,
    PipelineStatus.SELF_WITHDRAWN,
    PipelineStatus.REJECTED,
)


def ta_tracking(db: Session, only_user_id: int | None = None) -> dict:
    """Per-TA scorecard: applications, interviews scheduled, shortlisted, joined.

    Attribution is CandidateProfile.ta_owner_id (stamped when a candidate is
    applied to an opportunity). When only_user_id is set (a non-admin viewer)
    the result is scoped to that user's own record.
    """
    prof_q = (
        select(
            CandidateProfile.ta_owner_id.label("uid"),
            func.count().label("applied"),
            func.count().filter(CandidateProfile.pipeline_status == PipelineStatus.JOINED).label("joined"),
            func.count().filter(CandidateProfile.pipeline_status.in_(_TA_SHORTLIST_STATUSES)).label("shortlisted"),
            func.count().filter(CandidateProfile.pipeline_status.in_(_TA_REJECT_STATUSES)).label("rejected"),
        )
        .where(CandidateProfile.ta_owner_id.isnot(None))
    )
    iv_q = select(AiInterviewLink.scheduled_by, func.count()).where(AiInterviewLink.scheduled_by.isnot(None))
    if only_user_id is not None:
        prof_q = prof_q.where(CandidateProfile.ta_owner_id == only_user_id)
        iv_q = iv_q.where(AiInterviewLink.scheduled_by == only_user_id)
    prof_rows = db.execute(prof_q.group_by(CandidateProfile.ta_owner_id)).all()
    interviews = {r[0]: int(r[1]) for r in db.execute(iv_q.group_by(AiInterviewLink.scheduled_by)).all()}

    ids = {r.uid for r in prof_rows} | set(interviews)
    names = _ta_names(db, ids)

    out: list[dict] = []
    seen: set[int] = set()
    for r in prof_rows:
        seen.add(r.uid)
        applied, joined, rejected = int(r.applied or 0), int(r.joined or 0), int(r.rejected or 0)
        out.append({
            "user_id": r.uid,
            "name": names.get(r.uid, f"user:{r.uid}"),
            "applied": applied,
            "active": max(0, applied - joined - rejected),
            "shortlisted": int(r.shortlisted or 0),
            "joined": joined,
            "rejected": rejected,
            "interviews_scheduled": interviews.get(r.uid, 0),
        })
    # TAs who scheduled interviews but own no profiles yet.
    for uid, n in interviews.items():
        if uid not in seen:
            out.append({
                "user_id": uid, "name": names.get(uid, f"user:{uid}"),
                "applied": 0, "active": 0, "shortlisted": 0, "joined": 0,
                "rejected": 0, "interviews_scheduled": n,
            })
    out.sort(key=lambda x: (-x["applied"], -x["joined"], x["name"].lower()))
    totals = {
        "tas": len(out),
        "applied": sum(x["applied"] for x in out),
        "interviews_scheduled": sum(x["interviews_scheduled"] for x in out),
        "shortlisted": sum(x["shortlisted"] for x in out),
        "joined": sum(x["joined"] for x in out),
    }
    return {"rows": out, "totals": totals}


def _quarter_label(dt) -> str:
    return f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"


def _last_quarter_labels(n: int = 4) -> list[str]:
    """Labels for the last n quarters (oldest first), including the current one."""
    today = date.today()
    year, quarter = today.year, (today.month - 1) // 3 + 1
    labels: list[str] = []
    for _ in range(n):
        labels.append(f"{year}-Q{quarter}")
        quarter -= 1
        if quarter == 0:
            year, quarter = year - 1, 4
    return list(reversed(labels))


# ---------------------------------------------------------------------------
# Executive (Sales_Head)
# ---------------------------------------------------------------------------

def executive_dashboard(db: Session) -> dict:
    headcount = db.execute(
        select(func.count(Employee.id)).where(Employee.is_active.is_(True))
    ).scalar() or 0
    customer_count = db.execute(
        select(func.count(Customer.id)).where(Customer.status == CustomerStatus.ACTIVE)
    ).scalar() or 0
    project_count = db.execute(
        select(func.count(Project.id)).where(Project.status == ProjectStatus.ACTIVE)
    ).scalar() or 0

    opp_counts = {
        _ev(stage): count
        for stage, count in db.execute(
            select(Opportunity.pipeline_stage, func.count(Opportunity.id))
            .group_by(Opportunity.pipeline_stage)
        ).all()
    }
    opportunity_funnel = [
        {"stage": stage.value, "count": opp_counts.get(stage.value, 0)}
        for stage in PipelineStage
    ]

    req_counts = {
        _ev(status): count
        for status, count in db.execute(
            select(Requirement.status, func.count(Requirement.id)).group_by(Requirement.status)
        ).all()
    }
    requirement_funnel = [
        {"status": status.value, "count": req_counts.get(status.value, 0)}
        for status in RequirementStatus
    ]

    # Quarterly matrix: profiles that reached Joined (bucketed by when they were
    # last updated, i.e. moved to Joined) x invoiced revenue by invoice date.
    joined_q = func.date_trunc("quarter", CandidateProfile.updated_at).label("q")
    joined_map = {
        _quarter_label(bucket): count
        for bucket, count in db.execute(
            select(joined_q, func.count(CandidateProfile.id))
            .where(CandidateProfile.pipeline_status == PipelineStatus.JOINED)
            .group_by(joined_q)
        ).all()
    }
    revenue_q = func.date_trunc("quarter", Invoice.invoice_date).label("q")
    revenue_map = {
        _quarter_label(bucket): _num(total)
        for bucket, total in db.execute(
            select(revenue_q, func.sum(Invoice.grand_total)).group_by(revenue_q)
        ).all()
    }
    quarterly_matrix = [
        {
            "quarter": label,
            "joined_count": joined_map.get(label, 0),
            "revenue": revenue_map.get(label, 0.0),
        }
        for label in _last_quarter_labels(4)
    ]

    return {
        "headcount": headcount,
        "customer_count": customer_count,
        "project_count": project_count,
        "opportunity_funnel": opportunity_funnel,
        "quarterly_matrix": quarterly_matrix,
        "requirement_funnel": requirement_funnel,
    }


# ---------------------------------------------------------------------------
# RMG
# ---------------------------------------------------------------------------

def rmg_dashboard(db: Session) -> dict:
    req_rows = db.execute(
        select(
            Requirement.id,
            Requirement.req_number,
            Requirement.title,
            Requirement.no_of_positions,
            Requirement.opportunity_id,
            Customer.name,
        )
        .join(Customer, Customer.id == Requirement.customer_id)
        .where(Requirement.status.in_(OPEN_SOURCING_STATUSES))
        .order_by(Requirement.req_number)
    ).all()

    req_ids = [row[0] for row in req_rows]
    opp_ids = {row[4] for row in req_rows}

    resume_counts: dict[int, int] = {}
    if req_ids:
        resume_counts = {
            req_id: count
            for req_id, count in db.execute(
                select(Resume.requirement_id, func.count(Resume.id))
                .where(Resume.requirement_id.in_(req_ids))
                .group_by(Resume.requirement_id)
            ).all()
        }

    profiles_by_opp: dict[int, dict[str, int]] = {}
    if opp_ids:
        for opp_id, status, count in db.execute(
            select(
                CandidateProfile.opportunity_id,
                CandidateProfile.pipeline_status,
                func.count(CandidateProfile.id),
            )
            .where(CandidateProfile.opportunity_id.in_(opp_ids))
            .group_by(CandidateProfile.opportunity_id, CandidateProfile.pipeline_status)
        ).all():
            profiles_by_opp.setdefault(opp_id, {})[_ev(status)] = count

    opp_ids_by_req: dict[int, str] = {}
    if opp_ids:
        for rid, oid in db.execute(
            select(Requirement.id, Opportunity.opp_id)
            .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
            .where(Requirement.id.in_(req_ids))
        ).all():
            opp_ids_by_req[rid] = oid

    pipeline = [
        {
            "req_number": req_number,
            # ONE id rule (18 Aug 2026): the opp_id is what humans see.
            "opp_id": opp_ids_by_req.get(req_id),
            "title": title,
            "customer": customer_name,
            "no_of_positions": no_of_positions,
            "resumes_count": resume_counts.get(req_id, 0),
            "profiles_by_status": profiles_by_opp.get(opportunity_id, {}),
        }
        for req_id, req_number, title, no_of_positions, opportunity_id, customer_name in req_rows
    ]

    pending_engineering_reviews = db.execute(
        select(func.count(Requirement.id))
        .where(Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)
    ).scalar() or 0

    # RMG screening queue (25 Aug 2026): TA-applied candidates waiting for the
    # Shortlist/Reject decision that unlocks their AI L1. Oldest first — the
    # SLA clock runs on these.
    screening_rows = db.execute(
        select(CandidateProfile, Candidate, Opportunity)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.rmg_screening_status == "Pending")
        .order_by(func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at))
        .limit(10)
    ).all()
    pending_screening_count = db.execute(
        select(func.count(CandidateProfile.id))
        .where(CandidateProfile.rmg_screening_status == "Pending")
    ).scalar() or 0
    pending_screening = [
        {
            "profile_id": p.id,
            "candidate_name": " ".join(x for x in (c.first_name, c.last_name) if x) or f"#{c.id}",
            "opportunity_title": f"{o.opp_id} — {o.title}",
            "applied_on": (p.applied_on or p.created_at).isoformat() if (p.applied_on or p.created_at) else None,
            "ta_owner_name": p.ta_owner_name,
        }
        for p, c, o in screening_rows
    ]

    return {
        "pipeline": pipeline,
        "pending_screening": pending_screening,
        "totals": {
            "pending_engineering_reviews": pending_engineering_reviews,
            "pending_screening": pending_screening_count,
        },
    }


# ---------------------------------------------------------------------------
# TA
# ---------------------------------------------------------------------------

def ta_dashboard(db: Session) -> dict:
    open_requirements_count = db.execute(
        select(func.count(Requirement.id)).where(Requirement.status.in_(OPEN_SOURCING_STATUSES))
    ).scalar() or 0
    resumes_pending_scan = db.execute(
        select(func.count(Resume.id)).where(Resume.ats_status == AtsStatus.PENDING_SCAN)
    ).scalar() or 0

    queue_rows = db.execute(
        select(
            Resume.id,
            Resume.candidate_name,
            Requirement.id,
            Requirement.req_number,
            Opportunity.opp_id,
            Resume.ai_interview_scheduled_at,
        )
        .join(Requirement, Requirement.id == Resume.requirement_id)
        .outerjoin(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .where(Resume.ai_interview_status == AiInterviewStatus.SCHEDULED)
        .order_by(Resume.ai_interview_scheduled_at)
    ).all()
    interview_pending_queue = [
        {
            "id": resume_id,
            "candidate_name": candidate_name,
            "requirement_id": requirement_id,
            # ONE id rule (18 Aug 2026): show the opp_id, keep req_number as fallback.
            "requirement": opp_id or req_number,
            "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
        }
        for resume_id, candidate_name, requirement_id, req_number, opp_id, scheduled_at in queue_rows
    ]

    # Recruiter productivity, keyed by resumes.screened_by (Postgres FILTER aggregates).
    prod_rows = db.execute(
        select(
            Resume.screened_by,
            func.count(Resume.id).filter(
                sa.cast(Resume.created_at, sa.Date) == func.current_date()
            ),
            func.count(Resume.id).filter(
                Resume.created_at >= func.date_trunc("week", func.now())
            ),
            func.count(Resume.id),
            func.count(Resume.id).filter(Resume.ats_status == AtsStatus.SHORTLISTED),
        )
        .where(Resume.screened_by.is_not(None))
        .group_by(Resume.screened_by)
    ).all()
    usernames = _usernames(db, (row[0] for row in prod_rows))
    recruiter_productivity = [
        {
            "user": usernames.get(user_id, f"user:{user_id}"),
            "resumes_screened_today": today_count,
            "this_week": week_count,
            "total": total_count,
            "shortlisted_total": shortlisted_count,
        }
        for user_id, today_count, week_count, total_count, shortlisted_count in prod_rows
    ]
    recruiter_productivity.sort(key=lambda r: r["total"], reverse=True)

    return {
        "open_requirements_count": open_requirements_count,
        "resumes_pending_scan": resumes_pending_scan,
        "interview_pending_queue": interview_pending_queue,
        "recruiter_productivity": recruiter_productivity,
    }


# ---------------------------------------------------------------------------
# Finance
# ---------------------------------------------------------------------------

def finance_dashboard(db: Session) -> dict:
    out_count, out_amount = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0))
        .where(Invoice.payment_status != PaymentStatus.PAID)
    ).one()
    tds_count, tds_amount = db.execute(
        select(func.count(TdsRecord.id), func.coalesce(func.sum(TdsRecord.tds_balance), 0))
        .where(TdsRecord.tds_status != TdsStatus.PAID)
    ).one()

    po_rows = db.execute(
        select(
            PurchaseOrder.po_number,
            PurchaseOrder.total_value,
            PurchaseOrder.consumed_value,
            PurchaseOrder.balance_value,
        )
        .where(PurchaseOrder.status == POStatus.ACTIVE)
        .order_by(PurchaseOrder.total_value.desc())
        .limit(20)
    ).all()
    po_consumption = []
    for po_number, total_value, consumed_value, balance_value in po_rows:
        total = _num(total_value)
        consumed = _num(consumed_value)
        po_consumption.append({
            "po_number": po_number,
            "total_value": total,
            "consumed_value": consumed,
            "balance_value": _num(balance_value),
            "pct_consumed": round(consumed / total * 100, 1) if total else 0.0,
        })

    return {
        "outstanding_invoices": {"count": out_count, "amount": _num(out_amount)},
        "tds_pending": {"count": tds_count, "amount": _num(tds_amount)},
        "po_consumption": po_consumption,
    }


# ---------------------------------------------------------------------------
# Requirements funnel / stage timing
# ---------------------------------------------------------------------------

def requirements_dashboard(db: Session) -> dict:
    counts = {
        _ev(status): count
        for status, count in db.execute(
            select(Requirement.status, func.count(Requirement.id)).group_by(Requirement.status)
        ).all()
    }
    funnel = [
        {"status": status.value, "count": counts.get(status.value, 0)}
        for status in RequirementStatus
    ]

    def _avg_days(delta_expr, *conditions) -> float | None:
        value = db.execute(
            select(func.avg(sa.extract("epoch", delta_expr) / 86400.0)).where(*conditions)
        ).scalar()
        return round(float(value), 1) if value is not None else None

    avg_days_in_stage = {
        "submission_to_sales_head_approval": _avg_days(
            Requirement.sales_head_approved_at - Requirement.created_at,
            Requirement.sales_head_approved_at.is_not(None),
        ),
        "sales_head_to_engineering": _avg_days(
            Requirement.engineering_reviewed_at - Requirement.sales_head_approved_at,
            Requirement.sales_head_approved_at.is_not(None),
            Requirement.engineering_reviewed_at.is_not(None),
        ),
    }

    open_positions = db.execute(
        select(func.coalesce(func.sum(Requirement.no_of_positions), 0))
        .where(Requirement.status.not_in(TERMINAL_REQ_STATUSES))
    ).scalar() or 0
    filled_positions = db.execute(
        select(func.coalesce(func.sum(Requirement.no_of_positions), 0))
        .where(Requirement.status == RequirementStatus.FULFILLED)
    ).scalar() or 0

    return {
        "funnel": funnel,
        "avg_days_in_stage": avg_days_in_stage,
        "positions": {"open": int(open_positions), "filled": int(filled_positions)},
    }


# ------------------------------------------------------- interview calendar

def upcoming_interview_events(db: Session, days: int = 30) -> list[dict]:
    """Human interview rounds (L2 F2F / customer) from today forward, soonest
    first. Undated events (free-form 'when') are included at the end."""
    from datetime import datetime, timedelta, timezone

    from models import Candidate as Cand, InterviewEvent

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    rows = db.execute(
        select(InterviewEvent, Cand)
        .join(Cand, Cand.id == InterviewEvent.candidate_id, isouter=True)
        .where(
            sa.or_(
                sa.and_(InterviewEvent.scheduled_at.isnot(None),
                        InterviewEvent.scheduled_at >= now - timedelta(hours=12),
                        InterviewEvent.scheduled_at <= horizon),
                InterviewEvent.scheduled_at.is_(None),
            )
        )
        .order_by(InterviewEvent.scheduled_at.asc().nullslast(), InterviewEvent.id.desc())
        .limit(100)
    ).all()
    out: list[dict] = []
    for ev, cand in rows:
        name = (f"{cand.first_name} {cand.last_name or ''}".strip()
                if cand else f"Candidate #{ev.candidate_id or '?'}")
        out.append({
            "id": ev.id,
            "profile_id": ev.profile_id,
            "candidate_name": name,
            "kind": ev.kind,
            "scheduled_at": ev.scheduled_at.isoformat() if ev.scheduled_at else None,
            "raw_when": ev.raw_when,
            "meeting_link": ev.meeting_link,
            "note": ev.note,
        })
    return out


# --------------------------------------------------------- bench / roll-offs

def bench_rolloffs(db: Session, days: int = 60) -> list[dict]:
    """Active project employees whose project's PO coverage ends within `days`
    (or already ended) — the redeployment radar. Coverage = the LATEST end_date
    across the project's POs, so renewed projects don't false-alarm."""
    from datetime import timedelta

    from models import POProjectAllocation, ProjectEmployee

    today = date.today()
    horizon = today + timedelta(days=days)
    # Latest PO end per project (NULL end dates = open-ended → excluded).
    po_end = (
        select(
            POProjectAllocation.project_id.label("project_id"),
            func.max(PurchaseOrder.end_date).label("last_end"),
        )
        .join(PurchaseOrder, PurchaseOrder.id == POProjectAllocation.po_id)
        .where(PurchaseOrder.end_date.isnot(None))
        .group_by(POProjectAllocation.project_id)
        .subquery()
    )
    rows = db.execute(
        select(ProjectEmployee, Employee, Project, Customer.name, po_end.c.last_end)
        .join(Employee, Employee.id == ProjectEmployee.employee_id)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .join(Customer, Customer.id == Project.customer_id, isouter=True)
        .join(po_end, po_end.c.project_id == ProjectEmployee.project_id)
        .where(
            ProjectEmployee.is_active.is_(True),
            ProjectEmployee.is_exit.is_(False),
            po_end.c.last_end <= horizon,
        )
        .order_by(po_end.c.last_end.asc())
        .limit(100)
    ).all()
    out: list[dict] = []
    for pe, emp, project, customer_name, last_end in rows:
        days_left = (last_end - today).days if last_end else None
        out.append({
            "project_employee_id": pe.id,
            "employee_id": emp.id,
            "employee_name": f"{emp.first_name} {emp.last_name or ''}".strip(),
            "role_title": pe.role_title,
            "project_id": project.id,
            "project_name": project.name,
            "customer_name": customer_name,
            "po_end_date": last_end.isoformat() if last_end else None,
            "days_left": days_left,
            "candidate_profile_id": emp.candidate_profile_id,
        })
    return out


def bench_requirement_matches(db: Session, employee_id: int) -> list[dict]:
    """Rank open requirements for a rolling-off employee using the real ATS
    scorer over their CV (falls back to skill-name overlap without a CV)."""
    from models import CandidateProfile as CP, CandidateSkill, RequirementSkill, Skill

    emp = db.get(Employee, employee_id)
    if emp is None:
        return []
    # Resolve the employee back to a candidate (CV + skills live there).
    candidate = None
    if emp.candidate_profile_id:
        prof = db.get(CP, emp.candidate_profile_id)
        if prof is not None:
            from models import Candidate as Cand
            candidate = db.get(Cand, prof.candidate_id)
    cv_text = ""
    if candidate is not None and candidate.cv_url:
        try:
            from services.resumes import extract_resume_text
            cv_text = extract_resume_text(candidate.cv_url)
        except Exception:
            cv_text = ""
    emp_skills: set[str] = set()
    if candidate is not None:
        emp_skills = {
            n.lower() for n in db.execute(
                select(Skill.name).join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
                .where(CandidateSkill.candidate_id == candidate.id)
            ).scalars().all()
        }

    open_statuses = (
        RequirementStatus.OPEN_FOR_SOURCING, RequirementStatus.POSTED_ON_PORTALS,
        RequirementStatus.IN_PROGRESS,
    )
    reqs = db.execute(
        select(Requirement).where(Requirement.status.in_(open_statuses))
        .order_by(Requirement.id.desc()).limit(25)
    ).scalars().all()
    out: list[dict] = []
    for req in reqs:
        skill_rows = db.execute(
            select(RequirementSkill, Skill.name)
            .join(Skill, Skill.id == RequirementSkill.skill_id)
            .where(RequirementSkill.requirement_id == req.id)
        ).all()
        mandatory = [name for rs, name in skill_rows if rs.is_mandatory]
        optional = [name for rs, name in skill_rows if not rs.is_mandatory]
        score = None
        matched: list[str] = []
        if cv_text and mandatory:
            try:
                from services.ats_scoring import score_resume_against_requirement
                res = score_resume_against_requirement(
                    cv_text, mandatory, optional,
                    float(req.experience_min) if req.experience_min is not None else None,
                    float(req.experience_max) if req.experience_max is not None else None,
                    None, jd_text=req.rmg_jd_text,
                )
                score = res["ats_score"]
                matched = res["breakdown"].get("skills_matched", [])
            except Exception:
                score = None
        if score is None:
            # Skill-name overlap fallback (no CV / unscorable requirement).
            all_names = [n for _, n in skill_rows]
            hits = [n for n in all_names if n.lower() in emp_skills]
            score = round(100.0 * len(hits) / len(all_names), 1) if all_names else 0.0
            matched = hits
        out.append({
            "requirement_id": req.id,
            "req_number": req.req_number,
            "opp_id": getattr(req.opportunity, "opp_id", None),
            "title": req.title,
            "status": getattr(req.status, "value", str(req.status)),
            "match_score": score,
            "skills_matched": matched[:10],
            "mandatory_total": len(mandatory),
        })
    out.sort(key=lambda x: x["match_score"] or 0, reverse=True)
    return out[:10]


# --------------------------------------------------------------------- my work

def my_work(db: Session, user) -> dict:
    """Action items for THIS user — the to-do list that teaches each role its job.

    Design rules, learned from every dashboard that came before it:

    * **Only actionable items.** Every entry is something the caller's role
      decides or unblocks, with the page it happens on. Informational counts
      belong on the role dashboards, not here.
    * **Template-aware.** A user whose access template hides a tab never sees
      its items — a to-do you cannot open is an irritation, not a task.
    * **Zero-count items are dropped** server-side. An empty list means
      "all clear", and the UI says exactly that.
    """
    from datetime import timedelta

    from models import LeaveApplication, Timesheet, TimesheetStatus
    from services.access_templates import can_view_tab, effective_access

    roles = set(user.roles)
    is_admin = bool(roles & {"Admin", "CEO"})
    acc = effective_access(db, user.id, roles)

    def allowed(tab: str, *needed_roles: str) -> bool:
        if is_admin:
            return True
        if acc.get("visible_tabs") is not None:
            return can_view_tab(acc, tab)
        return bool(roles & set(needed_roles))

    def count(stmt) -> int:
        return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)

    today = date.today()
    items: list[dict] = []

    def add(key: str, n: int, label: str, path: str, urgency: str = "normal") -> None:
        if n > 0:
            items.append({"key": key, "count": n, "label": label, "path": path,
                          "urgency": urgency})

    # Timesheets waiting for a decision (RMG/Sales approve; admin sees all).
    if allowed("timesheets", "RMG", "Sales"):
        add("timesheets_to_approve",
            count(select(Timesheet.id).where(Timesheet.status == TimesheetStatus.SUBMITTED)),
            "timesheets waiting for your approval", "timesheets", "warning")

    # Own unsubmitted sheets — anyone linked to an employee record.
    emp_id = db.execute(
        sa.text("SELECT id FROM employees WHERE user_id = :uid LIMIT 1"), {"uid": user.id}
    ).scalar()
    if emp_id:
        add("own_draft_timesheets",
            count(select(Timesheet.id).where(Timesheet.employee_id == emp_id,
                                             Timesheet.status == TimesheetStatus.DRAFT)),
            "of your timesheets are not submitted yet", "timesheets", "warning")

    # Opportunities awaiting Sales Head approval.
    if allowed("opportunities", "Sales_Head"):
        from models import OpportunityApprovalStatus

        add("opportunities_to_approve",
            count(select(Opportunity.id).where(
                Opportunity.approval_status
                == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL)),
            "opportunities awaiting Sales Head approval", "opportunities", "warning")

    # Requirements stuck in engineering review (RMG must attach a JD + approve).
    if allowed("requirements", "RMG"):
        add("requirements_engineering_review",
            count(select(Requirement.id).where(
                Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)),
            "requirements waiting for RMG review (JD + approve)", "requirements", "warning")

    # Requirements open for sourcing (TA's queue).
    if allowed("requirements", "TA"):
        add("requirements_to_source",
            count(select(Requirement.id).where(
                Requirement.status.in_(OPEN_SOURCING_STATUSES))),
            "requirements open for sourcing", "requirements")

    # Leave applications waiting on HR.
    if allowed("leave-applications", "HR"):
        add("leave_to_decide",
            count(select(LeaveApplication.id).where(LeaveApplication.status == "Pending")),
            "leave applications waiting for HR", "leave-applications", "warning")

    # POs expiring within 30 days (or already past) but still Active.
    if allowed("pos", "Finance", "Sales_Head"):
        add("pos_expiring",
            count(select(PurchaseOrder.id).where(
                PurchaseOrder.status == POStatus.ACTIVE,
                PurchaseOrder.end_date.is_not(None),
                PurchaseOrder.end_date <= today + timedelta(days=30))),
            "purchase orders expire within 30 days", "pos", "danger")

    # Overdue receivables.
    if allowed("invoices", "Finance", "Sales_Head"):
        add("invoices_overdue",
            count(select(Invoice.id).where(
                Invoice.due_date.is_not(None),
                Invoice.due_date < today,
                Invoice.payment_status != PaymentStatus.PAID)),
            "invoices are overdue for payment", "invoices", "danger")

    # Profiles at Customer_Approval — only the Sales Head can move them.
    if allowed("profiles", "Sales_Head"):
        add("profiles_awaiting_signoff",
            count(select(CandidateProfile.id).where(
                CandidateProfile.pipeline_status == PipelineStatus.CUSTOMER_APPROVAL)),
            "candidates await your final sign-off", "profiles", "warning")

    # Danger first, then warning, then the rest — the eye reads top-down.
    rank = {"danger": 0, "warning": 1, "normal": 2}
    items.sort(key=lambda x: (rank.get(x["urgency"], 9), -x["count"]))
    return {"items": items, "all_clear": not items}
