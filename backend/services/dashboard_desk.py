"""The role-based dashboard ("every role gets its own desk", 14 Sep 2026).

Three SELECT-only aggregates that the Dashboard tab composes per login:

* `today_tiles(db, user)` — the four numbers each role is judged on, as tiles
  with a state (ok / warn / bad) and the page the number came from.
* `upcoming(db, user, days)` — what is coming in the next N days: interview
  rounds, AI L1 slots, joinings, roll-offs, PO expiries, invoice due dates.
* `team_overview(db, user)` — the head's layer (Sales Head, Admin, CEO):
  where the company is stuck, and a per-person view of TA and Sales.

Design rules shared with `dashboards.my_work`: template-aware (a tab the
user cannot open contributes nothing), zero-count tiles still show (a zero
is a good number on a desk), everything links somewhere, and no writes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink,
    AiInterviewStatus,
    CandidateProfile,
    Candidate,
    Customer,
    Employee,
    Invoice,
    LeaveApplication,
    OfferHistory,
    OfferStatus,
    Opportunity,
    OpportunityApprovalStatus,
    PaymentStatus,
    PipelineStage,
    PipelineStatus,
    POStatus,
    Project,
    ProjectEmployee,
    PurchaseOrder,
    Requirement,
    RequirementStatus,
    Resume,
    Timesheet,
    TimesheetStatus,
)
from models.scheduling import InterviewEvent
from services.dashboards import OPEN_SOURCING_STATUSES, _num, _ta_names

# Tiles are capped so a five-role Admin never gets a wall of numbers.
MAX_TILES = 8

#: Sourcing SLA: a requirement open this long without a new profile is red.
SOURCING_SLA_DAYS = 5
#: Engineering review / screening: red after this many hours.
REVIEW_SLA_HOURS = 48
#: A customer that has held a profile this long without moving it is "waiting".
CUSTOMER_WAIT_DAYS = 3
#: An opportunity with no update for this long is stalled.
STALLED_DAYS = 14
#: A requirement open this long with unfilled positions is a company stuck-point.
HIRING_STUCK_DAYS = 30

_CUSTOMER_WAIT_STATUSES = (
    PipelineStatus.CUSTOMER_SCREENING,
    PipelineStatus.CUSTOMER_INTERVIEW,
    PipelineStatus.L1_FEEDBACK,
    PipelineStatus.L2_FEEDBACK,
)
_ROUND_LABELS = {
    "L1_Interview": "Manual L1", "L2_F2F": "Manual L2", "HR_Interview": "HR round",
    "Customer_Interview": "Customer L1", "Customer_L2": "Customer L2",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _count(db: Session, stmt) -> int:
    return int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar() or 0)


def _permits(db: Session, user):
    """`allowed(tab, *roles)` with the same precedence `my_work` uses: Admin/CEO
    see everything, a template decides alone, otherwise roles."""
    from services.access_templates import can_view_tab, effective_access

    roles = set(user.roles or ())
    is_admin = bool(roles & {"Admin", "CEO"})
    acc = effective_access(db, user.id, roles)

    def allowed(tab: str, *needed_roles: str) -> bool:
        if is_admin:
            return True
        if acc.get("visible_tabs") is not None:
            return can_view_tab(acc, tab)
        return bool(roles & set(needed_roles))

    return roles, is_admin, allowed


def _state(n, bad_at=1, warn_at=None) -> str:
    """Tile colour from a count: bad at/above `bad_at`, warn at/above `warn_at`."""
    if bad_at is not None and n >= bad_at:
        return "bad"
    if warn_at is not None and n >= warn_at:
        return "warn"
    return "ok"


def _tile(key, label, value, detail, state, path, fmt="int") -> dict:
    return {"key": key, "label": label, "value": value, "detail": detail,
            "state": state, "path": path, "format": fmt}


def _month_start(today: date) -> date:
    return today.replace(day=1)


def _week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())


def _quarter_start(today: date) -> date:
    return date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)


# ------------------------------------------------------------------ today


def today_tiles(db: Session, user) -> dict:
    roles, is_admin, allowed = _permits(db, user)
    today = date.today()
    now = _now()
    tiles: list[dict] = []

    def add(t: dict) -> None:
        if all(x["key"] != t["key"] for x in tiles):
            tiles.append(t)

    # ---- TA: my sourcing desk ------------------------------------------------
    if "TA" in roles and allowed("requirements", "TA"):
        recent_profile = (
            select(CandidateProfile.id)
            .where(CandidateProfile.opportunity_id == Requirement.opportunity_id,
                   func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at)
                   >= now - timedelta(days=SOURCING_SLA_DAYS))
            .exists()
        )
        breached = _count(db, select(Requirement.id).where(
            Requirement.status.in_(OPEN_SOURCING_STATUSES), ~recent_profile))
        add(_tile("ta_sourcing_sla", "Sourcing SLA breached", breached,
                  f"open requirements with no new profile in {SOURCING_SLA_DAYS} days",
                  _state(breached), "opportunities"))
        pending_l1 = _count(db, select(AiInterviewLink.id).where(
            AiInterviewLink.completed_at.is_(None),
            AiInterviewLink.result == "Pending",
            *( [AiInterviewLink.scheduled_by == user.id] if not is_admin else [] )))
        add(_tile("ta_ai_l1_pending", "AI L1 pending", pending_l1,
                  "invited, not yet taken", _state(pending_l1, bad_at=None, warn_at=5), "calendar"))
        submitted = _count(db, select(CandidateProfile.id).where(
            CandidateProfile.ta_owner_id == user.id,
            func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at)
            >= datetime.combine(_week_start(today), datetime.min.time(), tzinfo=timezone.utc)))
        add(_tile("ta_submitted_week", "Profiles added this week", submitted,
                  "candidates you applied since Monday", "ok", "profiles"))
        joined = _count(db, select(CandidateProfile.id).where(
            CandidateProfile.ta_owner_id == user.id,
            CandidateProfile.pipeline_status == PipelineStatus.JOINED,
            func.coalesce(CandidateProfile.karnex_onboarding_date,
                          sa.cast(CandidateProfile.updated_at, sa.Date)) >= _month_start(today)))
        add(_tile("ta_joined_month", "Joined this month", joined, "your candidates who joined",
                  "ok", "profiles"))

    # ---- RMG: review + screening desk ----------------------------------------
    if "RMG" in roles and allowed("requirements", "RMG"):
        review_q = select(Requirement.id).where(
            Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)
        review_n = _count(db, review_q)
        review_old = _count(db, review_q.where(
            func.coalesce(Requirement.sales_head_approved_at, Requirement.created_at)
            <= now - timedelta(hours=REVIEW_SLA_HOURS)))
        add(_tile("rmg_review_queue", "Engineering review queue", review_n,
                  f"{review_old} older than {REVIEW_SLA_HOURS} h" if review_old else "none overdue",
                  "bad" if review_old else _state(review_n, bad_at=None, warn_at=1), "opportunities"))
        screen_q = select(CandidateProfile.id).where(CandidateProfile.rmg_screening_status == "Pending")
        screen_n = _count(db, screen_q)
        screen_old = _count(db, screen_q.where(
            func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at)
            <= now - timedelta(hours=REVIEW_SLA_HOURS)))
        add(_tile("rmg_screening_queue", "Screening queue", screen_n,
                  f"{screen_old} waiting > {REVIEW_SLA_HOURS} h" if screen_old else "applicants awaiting shortlist",
                  "bad" if screen_old else _state(screen_n, bad_at=None, warn_at=1), "profiles"))
    if roles & {"RMG", "Sales"} and allowed("timesheets", "RMG", "Sales"):
        ts_n = _count(db, select(Timesheet.id).where(Timesheet.status == TimesheetStatus.SUBMITTED))
        add(_tile("timesheets_to_approve", "Timesheets to approve", ts_n,
                  "submitted, waiting for a decision", _state(ts_n, bad_at=None, warn_at=1), "timesheets"))
    if roles & {"RMG", "Sales_Head"} and allowed("project-employees", "RMG", "Sales_Head"):
        from services.dashboards import bench_rolloffs
        bench = bench_rolloffs(db, days=60)
        add(_tile("bench_rolloffs", "Roll-offs · 60 days", len(bench),
                  "project employees whose PO cover ends", _state(len(bench), bad_at=None, warn_at=1),
                  "project-employees"))

    # ---- Sales: my customers desk -------------------------------------------
    if "Sales" in roles and allowed("profiles", "Sales"):
        waiting = _count(db, select(CandidateProfile.id).where(
            CandidateProfile.pipeline_status.in_(_CUSTOMER_WAIT_STATUSES),
            CandidateProfile.updated_at <= now - timedelta(days=CUSTOMER_WAIT_DAYS)))
        add(_tile("sales_awaiting_customer", "Awaiting customer", waiting,
                  f"profiles with the customer > {CUSTOMER_WAIT_DAYS} days",
                  _state(waiting, bad_at=None, warn_at=1), "profiles"))
    if "Sales" in roles and allowed("opportunities", "Sales"):
        mine = [] if is_admin or "Sales_Head" in roles else [Opportunity.created_by == user.id]
        pending = _count(db, select(Opportunity.id).where(
            Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL, *mine))
        add(_tile("sales_pending_approval", "Awaiting Sales Head", pending,
                  "opportunities pending approval", "ok", "opportunities"))
        open_n = _count(db, select(Opportunity.id).where(
            Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE, PipelineStage.ON_HOLD)), *mine))
        add(_tile("sales_open", "Open opportunities", open_n, "New · Active · On hold", "ok", "opportunities"))
        won = _count(db, select(Opportunity.id).where(
            Opportunity.pipeline_stage == PipelineStage.CLOSED_WON,
            sa.cast(Opportunity.updated_at, sa.Date) >= _quarter_start(today), *mine))
        add(_tile("sales_won_quarter", "Closed won · quarter", won, "since the quarter began", "ok",
                  "opportunities"))

    # ---- Sales Head: decisions desk ------------------------------------------
    if "Sales_Head" in roles:
        approvals = 0
        if allowed("opportunities", "Sales_Head"):
            approvals += _count(db, select(Opportunity.id).where(
                Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL))
        if allowed("profiles", "Sales_Head"):
            approvals += _count(db, select(CandidateProfile.id).where(
                CandidateProfile.pipeline_status == PipelineStatus.CUSTOMER_APPROVAL))
        if allowed("invoices", "Sales_Head"):
            from models.finance import InvoiceRevision
            approvals += _count(db, select(InvoiceRevision.id).where(InvoiceRevision.status == "Pending"))
        add(_tile("sh_approvals", "Approvals waiting on me", approvals,
                  "opportunities · offers · invoice changes", _state(approvals), "opportunities"))
        if allowed("opportunities", "Sales_Head"):
            stalled = _count(db, select(Opportunity.id).where(
                Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE)),
                Opportunity.updated_at <= now - timedelta(days=STALLED_DAYS)))
            add(_tile("sh_stalled", f"Stalled > {STALLED_DAYS} days", stalled,
                      "open opportunities with no activity", _state(stalled, bad_at=None, warn_at=1),
                      "opportunities"))
            closed = _count(db, select(Opportunity.id).where(
                Opportunity.pipeline_stage.in_((PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST,
                                                PipelineStage.CLOSED_PARTIAL)),
                sa.cast(Opportunity.updated_at, sa.Date) >= _quarter_start(today)))
            won = _count(db, select(Opportunity.id).where(
                Opportunity.pipeline_stage == PipelineStage.CLOSED_WON,
                sa.cast(Opportunity.updated_at, sa.Date) >= _quarter_start(today)))
            add(_tile("sh_win_rate", "Win rate · quarter", round(100 * won / closed) if closed else 0,
                      f"{won} won / {closed} closed", "ok", "opportunities", fmt="percent"))

    # ---- HR: people desk -----------------------------------------------------
    if "HR" in roles and allowed("leave-applications", "HR"):
        leave_q = select(LeaveApplication.id).where(LeaveApplication.status == "Pending")
        leave_n = _count(db, leave_q)
        oldest = db.execute(select(func.min(LeaveApplication.applied_at))
                            .where(LeaveApplication.status == "Pending")).scalar()
        age = (now - oldest).days if oldest else 0
        add(_tile("hr_leave_pending", "Leave to approve", leave_n,
                  f"oldest {age} days" if leave_n else "nothing waiting",
                  "bad" if age >= 3 else _state(leave_n, bad_at=None, warn_at=1), "leave-applications"))
    if "HR" in roles and allowed("employees", "HR"):
        joining = _count(db, select(OfferHistory.id).join(
            CandidateProfile, CandidateProfile.id == OfferHistory.profile_id).where(
            OfferHistory.status == OfferStatus.ACCEPTED,
            OfferHistory.joining_date.is_not(None),
            OfferHistory.joining_date >= _month_start(today),
            OfferHistory.joining_date < (_month_start(today) + timedelta(days=32)).replace(day=1),
            CandidateProfile.pipeline_status.in_((PipelineStatus.PREBOARDING, PipelineStatus.CUSTOMER_APPROVAL))))
        add(_tile("hr_joining_month", "Joining this month", joining, "accepted offers still in pre-boarding",
                  "ok", "profiles"))
        rounds = _count(db, select(InterviewEvent.id).where(
            InterviewEvent.kind == "HR_Interview",
            InterviewEvent.scheduled_at >= now - timedelta(hours=12),
            InterviewEvent.scheduled_at <= now + timedelta(days=7)))
        add(_tile("hr_rounds_week", "HR rounds · 7 days", rounds, "scheduled on the calendar", "ok", "calendar"))
        active = _count(db, select(Employee.id).where(Employee.is_active.is_(True)))
        add(_tile("hr_headcount", "Active employees", active, "on the roster", "ok", "employees"))

    # ---- Finance: billing desk ----------------------------------------------
    if "Finance" in roles and allowed("invoices", "Finance"):
        invoiced = select(Invoice.id).where(Invoice.timesheet_id == Timesheet.id).exists()
        ready = _count(db, select(Timesheet.id).where(
            Timesheet.status == TimesheetStatus.APPROVED, ~invoiced))
        add(_tile("fin_ready_to_invoice", "Ready to invoice", ready, "approved timesheets without an invoice",
                  _state(ready, bad_at=None, warn_at=1), "timesheets"))
        od_n, od_amt = db.execute(
            select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0)).where(
                Invoice.due_date.is_not(None), Invoice.due_date < today,
                Invoice.payment_status != PaymentStatus.PAID)).one()
        add(_tile("fin_overdue", "Overdue receivables", _num(od_amt),
                  f"{od_n} invoice{'s' if od_n != 1 else ''} past due", _state(od_n), "invoices", fmt="money"))
        out_n, out_amt = db.execute(
            select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0)).where(
                Invoice.payment_status != PaymentStatus.PAID)).one()
        add(_tile("fin_outstanding", "Outstanding", _num(out_amt), f"{out_n} open invoices", "ok",
                  "invoices", fmt="money"))
    if roles & {"Finance", "Sales", "Sales_Head"} and allowed("pos", "Finance", "Sales", "Sales_Head"):
        po_n = _count(db, select(PurchaseOrder.id).where(
            PurchaseOrder.status == POStatus.ACTIVE, PurchaseOrder.end_date.is_not(None),
            PurchaseOrder.end_date <= today + timedelta(days=30)))
        add(_tile("po_expiring", "POs expiring · 30 days", po_n, "still Active, cover ends soon",
                  _state(po_n, bad_at=None, warn_at=1), "pos"))

    # ---- Admin / CEO: company desk -------------------------------------------
    if is_admin:
        open_pos = int(db.execute(select(func.coalesce(func.sum(Requirement.no_of_positions), 0))
                                  .where(Requirement.status.in_(OPEN_SOURCING_STATUSES))).scalar() or 0)
        ageing = _count(db, select(Requirement.id).where(
            Requirement.status.in_(OPEN_SOURCING_STATUSES),
            Requirement.created_at <= now - timedelta(days=HIRING_STUCK_DAYS)))
        add(_tile("co_open_positions", "Open positions", open_pos,
                  f"{ageing} requirement{'s' if ageing != 1 else ''} open > {HIRING_STUCK_DAYS} days",
                  "warn" if ageing else "ok", "opportunities"))
        deployed = _count(db, select(ProjectEmployee.id).where(
            ProjectEmployee.is_active.is_(True), ProjectEmployee.is_exit.is_(False)))
        add(_tile("co_deployed", "Deployed headcount", deployed, "active project employees", "ok",
                  "project-employees"))
        od_n, od_amt = db.execute(
            select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0)).where(
                Invoice.due_date.is_not(None), Invoice.due_date < today,
                Invoice.payment_status != PaymentStatus.PAID)).one()
        add(_tile("co_cash_at_risk", "Overdue receivables", _num(od_amt), f"{od_n} invoices past due",
                  _state(od_n), "invoices", fmt="money"))
        waiting = (
            _count(db, select(Opportunity.id).where(
                Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL))
            + _count(db, select(CandidateProfile.id).where(
                CandidateProfile.pipeline_status == PipelineStatus.CUSTOMER_APPROVAL))
            + _count(db, select(Timesheet.id).where(Timesheet.status == TimesheetStatus.SUBMITTED))
            + _count(db, select(LeaveApplication.id).where(LeaveApplication.status == "Pending"))
            + _count(db, select(Requirement.id).where(
                Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)))
        add(_tile("co_approvals", "Decisions waiting", waiting,
                  "approvals across Sales Head · RMG · HR", _state(waiting, bad_at=None, warn_at=1),
                  "reports"))

    return {"tiles": tiles[:MAX_TILES], "generated_at": now.isoformat()}


# --------------------------------------------------------------- upcoming


def upcoming(db: Session, user, days: int = 7) -> dict:
    """Dated things in the next `days` days the caller can act on or attend."""
    roles, is_admin, allowed = _permits(db, user)
    today = date.today()
    now = _now()
    horizon_dt = now + timedelta(days=days)
    horizon = today + timedelta(days=days)
    items: list[dict] = []

    def add(when, kind, title, subtitle, path, state="info") -> None:
        items.append({"when": when.isoformat() if when else None, "kind": kind, "title": title,
                      "subtitle": subtitle, "path": path, "state": state})

    def cname(c) -> str:
        return (f"{c.first_name} {c.last_name or ''}".strip() if c else "Candidate")

    if allowed("profiles", "TA", "RMG", "Sales", "Sales_Head", "HR"):
        rows = db.execute(
            select(InterviewEvent, Candidate)
            .join(Candidate, Candidate.id == InterviewEvent.candidate_id, isouter=True)
            .where(InterviewEvent.scheduled_at.is_not(None),
                   InterviewEvent.scheduled_at >= now - timedelta(hours=2),
                   InterviewEvent.scheduled_at <= horizon_dt,
                   sa.or_(InterviewEvent.result.is_(None), InterviewEvent.result == ""))
            .order_by(InterviewEvent.scheduled_at).limit(40)).all()
        for ev, cand in rows:
            if "HR" in roles and not (roles - {"HR"}) and ev.kind != "HR_Interview":
                continue  # HR's desk lists HR rounds only
            label = _ROUND_LABELS.get(ev.kind or "", (ev.kind or "Interview").replace("_", " "))
            add(ev.scheduled_at, "interview", f"{label} · {cname(cand)}",
                (ev.interviewer and f"panel: {ev.interviewer}") or (ev.meeting_link and "meeting link ready") or "",
                f"profiles/{ev.profile_id}?tab=interviews")
        ai_rows = db.execute(
            select(Resume.candidate_name, Resume.ai_interview_scheduled_at, Resume.requirement_id,
                   Requirement.title)
            .join(Requirement, Requirement.id == Resume.requirement_id)
            .where(Resume.ai_interview_status == AiInterviewStatus.SCHEDULED,
                   Resume.ai_interview_scheduled_at >= now - timedelta(hours=2),
                   Resume.ai_interview_scheduled_at <= horizon_dt)
            .order_by(Resume.ai_interview_scheduled_at).limit(40)).all()
        for name, when, req_id, title in ai_rows:
            add(when, "ai_l1", f"AI L1 · {name}", title or "", f"requirements/{req_id}?tab=resumes")

    if allowed("profiles", "TA", "Sales", "Sales_Head", "HR"):
        rows = db.execute(
            select(OfferHistory.joining_date, CandidateProfile.id, Candidate, Opportunity.title, Customer.name)
            .join(CandidateProfile, CandidateProfile.id == OfferHistory.profile_id)
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id, isouter=True)
            .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
            .where(OfferHistory.status == OfferStatus.ACCEPTED,
                   OfferHistory.joining_date.is_not(None),
                   OfferHistory.joining_date >= today, OfferHistory.joining_date <= horizon,
                   CandidateProfile.pipeline_status.not_in((PipelineStatus.JOINED,)))
            .order_by(OfferHistory.joining_date).limit(20)).all()
        for jd, pid, cand, title, cust in rows:
            add(jd, "joining", f"Joining · {cname(cand)}", " · ".join(x for x in (cust, title) if x),
                f"profiles/{pid}", "ok")

    if allowed("project-employees", "RMG", "Sales_Head", "HR"):
        rows = db.execute(
            select(ProjectEmployee.id, ProjectEmployee.exit_date, Employee, Project.name, Customer.name)
            .join(Employee, Employee.id == ProjectEmployee.employee_id)
            .join(Project, Project.id == ProjectEmployee.project_id)
            .join(Customer, Customer.id == Project.customer_id, isouter=True)
            .where(ProjectEmployee.exit_date.is_not(None),
                   ProjectEmployee.exit_date >= today, ProjectEmployee.exit_date <= horizon)
            .order_by(ProjectEmployee.exit_date).limit(20)).all()
        for pe_id, xd, emp, proj, cust in rows:
            add(xd, "rolloff", f"Roll-off · {emp.first_name} {emp.last_name or ''}".strip(),
                " · ".join(x for x in (cust, proj) if x), f"project-employees/{pe_id}", "warn")

    if allowed("pos", "Finance", "Sales", "Sales_Head"):
        rows = db.execute(
            select(PurchaseOrder.id, PurchaseOrder.po_number, PurchaseOrder.end_date,
                   PurchaseOrder.balance_value, Customer.name)
            .join(Customer, Customer.id == PurchaseOrder.customer_id, isouter=True)
            .where(PurchaseOrder.status == POStatus.ACTIVE, PurchaseOrder.end_date.is_not(None),
                   PurchaseOrder.end_date <= horizon)
            .order_by(PurchaseOrder.end_date).limit(20)).all()
        for po_id, num, ed, bal, cust in rows:
            add(ed, "po_expiry", f"PO expiry · {num}", f"{cust or ''} · balance ₹{_num(bal):,.0f}".strip(" ·"),
                f"pos/{po_id}", "bad" if ed < today else "warn")

    if allowed("invoices", "Finance", "Sales_Head"):
        rows = db.execute(
            select(Invoice.id, Invoice.invoice_number, Invoice.due_date, Invoice.balance_amount, Customer.name)
            .join(Project, Project.id == Invoice.project_id, isouter=True)
            .join(Customer, Customer.id == Project.customer_id, isouter=True)
            .where(Invoice.payment_status != PaymentStatus.PAID, Invoice.due_date.is_not(None),
                   Invoice.due_date >= today, Invoice.due_date <= horizon)
            .order_by(Invoice.due_date).limit(20)).all()
        for inv_id, num, dd, bal, cust in rows:
            add(dd, "invoice_due", f"Due · {num}", f"{cust or ''} · ₹{_num(bal):,.0f}".strip(" ·"),
                f"invoices/{inv_id}")

    items.sort(key=lambda x: x["when"] or "9999")
    return {"days": days, "items": items[:30]}


# --------------------------------------------------------------- team layer


def team_overview(db: Session, user) -> dict:
    """Sales Head / Admin / CEO: where the company is stuck + per-person rows."""
    roles, is_admin, allowed = _permits(db, user)
    today = date.today()
    now = _now()
    stuck: list[dict] = []

    def add(area, title, detail, count, state, path) -> None:
        if count:
            stuck.append({"area": area, "title": title, "detail": detail, "count": count,
                          "state": state, "path": path})

    # Hiring: open too long with nobody joined.
    ageing = db.execute(
        select(Requirement.id, Requirement.title, Customer.name, Requirement.created_at)
        .join(Customer, Customer.id == Requirement.customer_id, isouter=True)
        .where(Requirement.status.in_(OPEN_SOURCING_STATUSES),
               Requirement.created_at <= now - timedelta(days=HIRING_STUCK_DAYS))
        .order_by(Requirement.created_at).limit(5)).all()
    if ageing:
        eg = ", ".join(f"{(c or '')} {t} ({(now - ca).days} d)".strip() for _, t, c, ca in ageing[:3])
        add("Hiring", f"{len(ageing)} requirement{'s' if len(ageing) != 1 else ''} open > {HIRING_STUCK_DAYS} days",
            eg, len(ageing), "bad", "opportunities")

    # Approvals waiting on the Sales Head.
    oldest = db.execute(select(func.min(Opportunity.created_at)).where(
        Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL)).scalar()
    opp_pending = _count(db, select(Opportunity.id).where(
        Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL))
    signoff = _count(db, select(CandidateProfile.id).where(
        CandidateProfile.pipeline_status == PipelineStatus.CUSTOMER_APPROVAL))
    if opp_pending or signoff:
        age = (now - oldest).days if oldest else 0
        add("Sales", f"Sales Head has {opp_pending + signoff} decision{'s' if opp_pending + signoff != 1 else ''} waiting",
            f"{opp_pending} opportunities · {signoff} offers" + (f" · oldest {age} d" if age else ""),
            opp_pending + signoff, "bad" if age >= 3 else "warn", "opportunities")

    # RMG queues past SLA.
    rev_old = _count(db, select(Requirement.id).where(
        Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW,
        func.coalesce(Requirement.sales_head_approved_at, Requirement.created_at)
        <= now - timedelta(hours=REVIEW_SLA_HOURS)))
    scr_old = _count(db, select(CandidateProfile.id).where(
        CandidateProfile.rmg_screening_status == "Pending",
        func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at)
        <= now - timedelta(hours=REVIEW_SLA_HOURS)))
    add("RMG", f"RMG queue past {REVIEW_SLA_HOURS} h", f"{rev_old} engineering reviews · {scr_old} screenings",
        rev_old + scr_old, "warn", "profiles")

    # Cash.
    od_n, od_amt = db.execute(
        select(func.count(Invoice.id), func.coalesce(func.sum(Invoice.balance_amount), 0)).where(
            Invoice.due_date.is_not(None), Invoice.due_date < today,
            Invoice.payment_status != PaymentStatus.PAID)).one()
    add("Finance", f"₹{_num(od_amt):,.0f} overdue on {od_n} invoice{'s' if od_n != 1 else ''}",
        "no receipt recorded past the due date", int(od_n), "bad", "invoices")
    po_n = _count(db, select(PurchaseOrder.id).where(
        PurchaseOrder.status == POStatus.ACTIVE, PurchaseOrder.end_date.is_not(None),
        PurchaseOrder.end_date <= today + timedelta(days=30)))
    add("Finance", f"{po_n} PO{'s' if po_n != 1 else ''} expire within 30 days", "billing stops when cover ends",
        po_n, "warn", "pos")

    # Timesheets stuck in approval.
    ts_old = _count(db, select(Timesheet.id).where(
        Timesheet.status == TimesheetStatus.SUBMITTED,
        Timesheet.submitted_at <= now - timedelta(days=3)))
    add("Operations", f"{ts_old} timesheet{'s' if ts_old != 1 else ''} waiting > 3 days for approval",
        "invoicing is blocked behind them", ts_old, "warn", "timesheets")

    # Support (admins only — the tickets tab is theirs).
    if is_admin:
        try:
            from models.support import SupportTicket, TicketStatus
            open_old = _count(db, select(SupportTicket.id).where(
                SupportTicket.status.in_((TicketStatus.OPEN.value, TicketStatus.IN_PROGRESS.value)),
                SupportTicket.created_at <= now - timedelta(hours=24)))
            add("Support", f"{open_old} support ticket{'s' if open_old != 1 else ''} open > 24 h",
                "users waiting for an answer", open_old, "warn", "settings?tab=support")
        except Exception:
            pass

    rank = {"bad": 0, "warn": 1, "ok": 2}
    stuck.sort(key=lambda x: (rank.get(x["state"], 9), -x["count"]))

    # Per-person: TA scorecard (existing) + Sales owners.
    from services.dashboards import ta_tracking
    ta_rows = ta_tracking(db)["rows"] if allowed("reports") else []

    sales_rows: list[dict] = []
    if allowed("opportunities", "Sales_Head"):
        rows = db.execute(
            select(
                Opportunity.created_by,
                func.count().filter(Opportunity.pipeline_stage.in_(
                    (PipelineStage.NEW, PipelineStage.ACTIVE, PipelineStage.ON_HOLD))).label("open"),
                func.count().filter(
                    Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE)),
                    Opportunity.updated_at <= now - timedelta(days=STALLED_DAYS)).label("stalled"),
                func.count().filter(
                    Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL).label("pending"),
                func.count().filter(
                    Opportunity.pipeline_stage == PipelineStage.CLOSED_WON,
                    sa.cast(Opportunity.updated_at, sa.Date) >= _quarter_start(today)).label("won"),
            ).group_by(Opportunity.created_by)).all()
        names = _ta_names(db, (r[0] for r in rows))
        for uid, open_n, stalled, pending, won in rows:
            if not (open_n or pending or won):
                continue
            sales_rows.append({"user_id": uid, "name": names.get(uid, f"user:{uid}"), "open": int(open_n),
                               "stalled": int(stalled), "pending_approval": int(pending),
                               "won_quarter": int(won),
                               "state": "warn" if stalled else "ok"})
        sales_rows.sort(key=lambda r: (-r["open"], r["name"].lower()))

    return {"stuck": stuck, "ta": ta_rows, "sales": sales_rows, "generated_at": now.isoformat()}
