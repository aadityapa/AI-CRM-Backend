"""Project service helpers: lookups, serializers, team/history management."""
from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    BillingUnit, Customer, CustomerBillingPolicy, Designation, Employee,
    EmployeeProjectHistory, Invoice, Opportunity, Project,
    ProjectCommunicationMatrix, ProjectEmployee, TdsRecord, Timesheet,
    TimesheetEntry, TimesheetStatus,
)
from services.finance import active_po_allocation_for_project
from services.timesheets import _project_branch, effective_billing_policy, opportunity_branch_foreign_to_project, period_bounds


def get_project_or_404(db: Session, project_id: int) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def validate_project_refs(db: Session, opportunity_id: int | None = None,
                          customer_id: int | None = None) -> None:
    if opportunity_id is not None and not db.get(Opportunity, opportunity_id):
        raise HTTPException(status_code=400, detail="Opportunity not found")
    if customer_id is not None and not db.get(Customer, customer_id):
        raise HTTPException(status_code=400, detail="Customer not found")


def employee_full_name(emp: Employee | None) -> str | None:
    if not emp:
        return None
    return " ".join(p for p in [emp.first_name, emp.last_name] if p) or None


def _num(v):
    return float(v) if v is not None else None


def project_out(p: Project) -> dict:
    return {
        "id": p.id,
        "opportunity_id": p.opportunity_id,
        "customer_id": p.customer_id,
        "branch_id": getattr(p, "branch_id", None),
        "name": p.name,
        "billing_cycle_start_day": p.billing_cycle_start_day,
        "billing_cycle_end_day": p.billing_cycle_end_day,
        "billing_frequency": getattr(p.billing_frequency, "value", p.billing_frequency),
        "recurring_billing": bool(getattr(p, "recurring_billing", True)),
        "max_billable_hours_day": _num(p.max_billable_hours_day),
        "max_billable_hours_month": _num(p.max_billable_hours_month),
        "max_billable_days_month": p.max_billable_days_month,
        "no_billing_period_days": p.no_billing_period_days,
        "holidays_billable": p.holidays_billable,
        "weekoff_billable": p.weekoff_billable,
        "leave_billable": p.leave_billable,
        "comp_off_billable": p.comp_off_billable,
        "hours_required_half_day": _num(p.hours_required_half_day),
        "hours_required_full_day": _num(p.hours_required_full_day),
        "hours_required_half_day_comp_off": _num(p.hours_required_half_day_comp_off),
        "hours_required_full_day_comp_off": _num(p.hours_required_full_day_comp_off),
        "working_hours_per_day": _num(p.working_hours_per_day),
        "is_max_billable_hours_per_day": p.is_max_billable_hours_per_day,
        "is_max_billable_hours_per_month": p.is_max_billable_hours_per_month,
        "is_max_billable_days_per_month": p.is_max_billable_days_per_month,
        "is_initial_no_billing_period": p.is_initial_no_billing_period,
        # qty = numeric count (UI "Period"); period = unit string (UI "QTY")
        "initial_no_billing_qty": p.initial_no_billing_qty,
        "initial_no_billing_period": p.initial_no_billing_period,
        "status": getattr(p.status, "value", p.status),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def project_leave_policy_out(db: Session, p) -> dict:
    from models import LeavePolicyType
    lt = db.get(LeavePolicyType, p.leave_type_id)
    created = getattr(p, "created_at", None)
    updated = getattr(p, "updated_at", None)
    return {
        "id": p.id,
        "project_id": p.project_id,
        "leave_type_id": p.leave_type_id,
        "leave_name": lt.name if lt else None,
        "name": p.name,
        "leave_credit_type": p.leave_credit_type,
        "leave_credit_timing": getattr(p, "leave_credit_timing", None),
        "leave_credit_balance": _num(p.leave_credit_balance),
        "initial_credit_balance": _num(p.initial_credit_balance),
        "leave_expire": p.leave_expire,
        "leave_expire_timing": getattr(p, "leave_expire_timing", None),
        "is_max_limit": bool(p.is_max_limit),
        "maximum_carry_forward": int(p.maximum_carry_forward or 0),
        "effective_date": p.effective_date.isoformat() if p.effective_date else None,
        "is_active": bool(getattr(p, "is_active", True)),
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


def project_employee_out(pe: ProjectEmployee, emp: Employee | None = None) -> dict:
    emp = emp or pe.employee
    return {
        "id": pe.id,
        "pe_id": pe.id,  # alias for UI drill-down (project-employees/:id)
        "project_id": pe.project_id,
        "employee_id": pe.employee_id,
        "employee_name": employee_full_name(emp),
        "employee_email": emp.email if emp else None,
        "role_title": getattr(pe, "role_title", None) or (getattr(emp, "role_title", None) if emp else None),
        "onboarding_date": pe.onboarding_date.isoformat() if pe.onboarding_date else None,
        "experience_years": _num(pe.experience_years),
        "project_experience_years": _num(getattr(pe, "project_experience_years", None)),
        "work_mode": getattr(pe.work_mode, "value", pe.work_mode) if pe.work_mode else None,
        "billing_rate": _num(pe.billing_rate),
        "billing_unit": getattr(pe.billing_unit, "value", pe.billing_unit),
        "is_active": pe.is_active,
        "is_exit": pe.is_exit,
        "exit_date": pe.exit_date.isoformat() if pe.exit_date else None,
        "billing_date": pe.billing_date.isoformat() if pe.billing_date else None,
        "settlement_pending": bool(getattr(pe, "settlement_pending", False)),
        # Full Commercial Details history, oldest first (relationship order).
        # billing_rate above stays the CURRENT rate; this is the whole story,
        # so the Team tab / PE list / PE detail can show every rate with its
        # derived expiry, not just today's number.
        "rates": [
            {
                "id": r.id,
                "effective_from": r.effective_from.isoformat() if r.effective_from else None,
                "rate": _num(r.rate),
                "billing_unit": getattr(r.billing_unit, "value", r.billing_unit) if r.billing_unit else None,
                "is_current_rate": bool(r.is_current_rate),
            }
            for r in (pe.rates or [])
        ],
    }


def comm_entry_out(entry: ProjectCommunicationMatrix) -> dict:
    return {
        "id": entry.id,
        "project_id": entry.project_id,
        "name": entry.name,
        "role": entry.role,
        "responsible_person": entry.responsible_person,
        "email": entry.email,
        "phone": entry.phone,
        "type": getattr(entry.type, "value", entry.type),
    }


def project_detail_out(db: Session, p: Project) -> dict:
    data = project_out(p)
    customer = db.get(Customer, p.customer_id)
    opportunity = db.get(Opportunity, p.opportunity_id) if p.opportunity_id else None
    branch = _project_branch(db, p)
    foreign = opportunity_branch_foreign_to_project(db, p)
    data["customer_name"] = customer.name if customer else None
    data["opportunity_title"] = opportunity.title if opportunity else None
    data["branch_id"] = branch.id if branch else None
    data["branch_name"] = branch.branch_name if branch else None
    data["branch_unlinked"] = bool(branch is None and foreign)
    data["branch_link_message"] = (
        "Branch not linked to this customer" if data["branch_unlinked"] else None
    )
    team_rows = db.execute(
        select(ProjectEmployee, Employee)
        .join(Employee, Employee.id == ProjectEmployee.employee_id)
        .where(ProjectEmployee.project_id == p.id)
        .order_by(ProjectEmployee.id)
    ).all()
    data["team"] = [project_employee_out(pe, emp) for pe, emp in team_rows]
    comm_rows = db.execute(
        select(ProjectCommunicationMatrix)
        .where(ProjectCommunicationMatrix.project_id == p.id)
        .order_by(ProjectCommunicationMatrix.id)
    ).scalars().all()
    data["communication_matrix"] = [comm_entry_out(e) for e in comm_rows]
    return data


def designation_name_for(db: Session, employee: Employee) -> str | None:
    if not employee.designation_id:
        return None
    designation = db.get(Designation, employee.designation_id)
    return designation.name if designation else None


def open_history_row(db: Session, project_id: int, employee_id: int) -> EmployeeProjectHistory | None:
    return db.execute(
        select(EmployeeProjectHistory)
        .where(
            EmployeeProjectHistory.project_id == project_id,
            EmployeeProjectHistory.employee_id == employee_id,
            EmployeeProjectHistory.end_date.is_(None),
        )
        .order_by(EmployeeProjectHistory.start_date.desc())
    ).scalars().first()


def add_history_entry(db: Session, project_id: int, employee: Employee,
                      start_date: date | None) -> EmployeeProjectHistory:
    row = EmployeeProjectHistory(
        employee_id=employee.id,
        project_id=project_id,
        start_date=start_date or date.today(),
        role=designation_name_for(db, employee),
    )
    db.add(row)
    return row


# ---------------------------------------------------------------------------
# Project 360-degree history (GET /api/projects/{id}/history)
# ---------------------------------------------------------------------------

# monthly_burn_estimate assumptions: a Daily rate is monthly-ized over 22
# working days, an Hourly rate over 8 hours x 22 working days.
_BURN_WORKING_DAYS_PER_MONTH = 22
_BURN_WORKING_HOURS_PER_DAY = 8


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _iso(value):
    return value.isoformat() if value else None


def _short_period(year: int, month: int) -> str:
    """Abbreviated period label, e.g. "Aug 2026"."""
    return f"{calendar.month_abbr[month]} {year}"


def _as_date(value):
    """date | datetime | None -> date | None (for timeline sorting)."""
    if isinstance(value, datetime):
        return value.date()
    return value


def _last_n_months(anchor: date, n: int) -> list[tuple[int, int]]:
    """(year, month) of the n calendar months ending at anchor's, oldest first."""
    y, m = anchor.year, anchor.month
    out: list[tuple[int, int]] = []
    for _ in range(n):
        out.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out.reverse()
    return out


def _monthlyized_rate(pe: ProjectEmployee) -> Decimal:
    """One assignment's contribution to the monthly burn estimate.

    Monthly = rate as-is; Daily = rate * 22 working days;
    Hourly = rate * 8 hours * 22 working days (documented assumption).
    """
    rate = Decimal(pe.billing_rate or 0)
    if pe.billing_unit == BillingUnit.DAILY:
        return rate * _BURN_WORKING_DAYS_PER_MONTH
    if pe.billing_unit == BillingUnit.HOURLY:
        return rate * _BURN_WORKING_HOURS_PER_DAY * _BURN_WORKING_DAYS_PER_MONTH
    return rate  # Monthly


def project_history(db: Session, project: Project) -> dict:
    """Aggregate the Project 360-degree view for one project.

    Read-only; a fixed number of queries regardless of data volume:
      1. customer / opportunity / branch lookups (db.get + the branch select
         inside services.timesheets._project_branch),
      2. effective_billing_policy (customer policy select, reused) + one
         customer-policy select for the comp-off/normal-hours fields the
         BillingPolicy dataclass does not carry,
      3. team assignments (+ employee, single joined select),
      4. active PO allocation (reused finance helper) + its PO,
      5. invoices (+ linked timesheet + its employee, single joined select),
      6. project TDS total (ONE scalar aggregate over TdsRecord),
      7. timesheets (+ employee, single joined select) — status counts,
         coverage and timeline are derived from it in Python,
      8. billables for the 12 most recent timesheets (ONE grouped aggregate
         over TimesheetEntry — no per-timesheet queries).
    """
    today = date.today()

    # -------------------------------------------------------------- project
    customer = db.get(Customer, project.customer_id)
    opp = db.get(Opportunity, project.opportunity_id) if project.opportunity_id else None
    # Same branch resolution effective_billing_policy uses (project ->
    # opportunity -> branch, customer-checked); null-safe.
    branch = _project_branch(db, project)
    created_date = project.created_at.date() if project.created_at else None
    project_block = {
        "id": project.id,
        "title": project.name,
        "status": _ev(project.status),
        "project_type": _ev(opp.opp_type) if opp else None,
        "customer_name": customer.name if customer else None,
        "branch_name": branch.branch_name if branch else None,
        "billing_frequency": _ev(project.billing_frequency),
        "created_at": _iso(project.created_at),
        "duration_days": (today - created_date).days if created_date else None,
    }

    # ------------------------------------------------------- billing policy
    policy = effective_billing_policy(db, project)
    cust_policy = db.execute(
        select(CustomerBillingPolicy)
        .where(CustomerBillingPolicy.customer_id == project.customer_id)
    ).scalars().first()
    # comp_off_billable / normal_hours_per_day are not part of the reused
    # BillingPolicy dataclass — resolve them with the same branch -> customer
    # -> defaults order, field by field.
    if branch is not None and branch.comp_off_billable is not None:
        comp_off_billable = bool(branch.comp_off_billable)
    elif cust_policy is not None:
        comp_off_billable = bool(cust_policy.comp_off_billable)
    else:
        comp_off_billable = False
    if branch is not None and branch.working_hours_per_day is not None:
        normal_hours = branch.working_hours_per_day
    elif cust_policy is not None:
        normal_hours = cust_policy.normal_hours_per_day
    else:
        normal_hours = None
    # source is a coarse indicator (the real resolution is FIELD-BY-FIELD):
    # "branch" when the resolved branch sets at least one policy field,
    # else "customer" when a customer-level policy row exists, else "defaults".
    branch_overrides = branch is not None and any(
        v is not None for v in (
            branch.holidays_billable, branch.weekoff_billable, branch.leave_billable,
            branch.comp_off_billable, branch.hours_required_full_day,
            branch.hours_required_half_day, branch.working_hours_per_day,
        )
    )
    billing_policy = {
        "source": "branch" if branch_overrides else ("customer" if cust_policy else "defaults"),
        "holidays_billable": policy.holidays_billable,
        "weekoff_billable": policy.week_off_billable,
        "leave_billable": policy.leave_billable,
        "comp_off_billable": comp_off_billable,
        "min_hours_full_day": _num(policy.min_hours_full_day),
        "min_hours_half_day": _num(policy.min_hours_half_day),
        "normal_hours_per_day": _num(normal_hours),
        "max_billable_hours_day": _num(project.max_billable_hours_day),
    }

    # ----------------------------------------------------------------- team
    member_rows = db.execute(
        select(ProjectEmployee, Employee)
        .join(Employee, Employee.id == ProjectEmployee.employee_id)
        .where(ProjectEmployee.project_id == project.id)
        .order_by(sa.nullslast(sa.desc(ProjectEmployee.onboarding_date)),
                  ProjectEmployee.id.desc())
    ).all()
    members = []
    active_count = 0
    exited_count = 0
    burn = Decimal("0")
    for pe, emp in member_rows:
        currently_active = bool(pe.is_active) and not bool(pe.is_exit)
        if bool(pe.is_exit):
            exited_count += 1
        elif currently_active:
            active_count += 1
        if currently_active:
            burn += _monthlyized_rate(pe)
        members.append({
            "assignment_id": pe.id,
            "employee_id": pe.employee_id,
            "employee_name": employee_full_name(emp),
            "onboarding_date": _iso(pe.onboarding_date),
            "billing_date": _iso(pe.billing_date),
            "billing_rate": _num(pe.billing_rate),
            "billing_unit": _ev(pe.billing_unit),
            "work_mode": _ev(pe.work_mode) if pe.work_mode else None,
            "experience_years": _num(pe.experience_years),
            "is_active": bool(pe.is_active),
            "is_exit": bool(pe.is_exit),
            "exit_date": _iso(pe.exit_date),
        })
    team = {"active_count": active_count, "exited_count": exited_count, "members": members}

    # -------------------------------------------------------------- finance
    alloc = active_po_allocation_for_project(db, project.id)
    po_block = None
    if alloc is not None:
        po = alloc.po
        allocated = Decimal(alloc.allocated_amount or 0)
        consumed = Decimal(alloc.consumed_amount or 0)
        po_block = {
            "po_id": alloc.po_id,
            "po_number": po.po_number if po else None,
            "allocated": _num(alloc.allocated_amount),
            "consumed": _num(alloc.consumed_amount),
            "balance": float(allocated - consumed),
            "hsn_sac": alloc.hsn_sac,
        }

    invoice_rows = db.execute(
        select(Invoice, Timesheet, Employee)
        .outerjoin(Timesheet, Timesheet.id == Invoice.timesheet_id)
        .outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Invoice.project_id == project.id)
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
    ).all()
    invoices = []
    total_invoiced = Decimal("0")
    total_paid = Decimal("0")
    outstanding = Decimal("0")
    for inv, inv_ts, inv_emp in invoice_rows:
        total_invoiced += Decimal(inv.grand_total or 0)
        total_paid += Decimal(inv.paid_amount or 0)
        outstanding += Decimal(inv.balance_amount or 0)
        invoices.append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "invoice_date": _iso(inv.invoice_date),
            "period_label": _short_period(inv_ts.year, inv_ts.month) if inv_ts else None,
            "employee_name": employee_full_name(inv_emp),
            "grand_total": _num(inv.grand_total),
            "paid_amount": _num(inv.paid_amount),
            "balance_amount": _num(inv.balance_amount),
            "payment_status": _ev(inv.payment_status),
        })
    total_tds = db.execute(
        select(sa.func.coalesce(sa.func.sum(TdsRecord.tds_amount), 0))
        .join(Invoice, Invoice.id == TdsRecord.invoice_id)
        .where(Invoice.project_id == project.id)
    ).scalar()
    finance = {
        "po": po_block,
        "total_invoiced": float(total_invoiced),
        "total_paid": float(total_paid),
        "outstanding": float(outstanding),
        "total_tds": float(total_tds or 0),
        "monthly_burn_estimate": float(burn),
        "invoices": invoices,
    }

    # ----------------------------------------------------------- timesheets
    ts_rows = db.execute(
        select(Timesheet, Employee)
        .outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.project_id == project.id)
        .order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).all()
    status_counts: dict = {}
    period_sa_counts: dict[tuple[int, int], int] = {}
    for ts, _emp in ts_rows:
        status_counts[ts.status] = status_counts.get(ts.status, 0) + 1
        if ts.status in (TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED):
            key = (ts.year, ts.month)
            period_sa_counts[key] = period_sa_counts.get(key, 0) + 1

    # Coverage approximation (documented): an assignment is EXPECTED to file a
    # timesheet for a month when onboarding_date <= month-end AND (not exited,
    # or exit_date >= month-start). Assignments without an onboarding_date are
    # not counted. Timesheets are unique per (project, employee, month, year),
    # so the Submitted/Approved count per period is already distinct.
    coverage = []
    for y, m in _last_n_months(today, 6):
        start, end = period_bounds(y, m)
        expected = 0
        for pe, _emp in member_rows:
            if pe.onboarding_date is None or pe.onboarding_date > end:
                continue
            if pe.is_exit and not (pe.exit_date is not None and pe.exit_date >= start):
                continue
            expected += 1
        coverage.append({
            "month": m,
            "year": y,
            "period_label": _short_period(y, m),
            "expected": expected,
            "submitted_or_approved": period_sa_counts.get((y, m), 0),
        })

    recent_rows = ts_rows[:12]
    recent_ids = [ts.id for ts, _emp in recent_rows]
    entry_totals: dict[int, tuple] = {}
    if recent_ids:
        agg_rows = db.execute(
            select(
                TimesheetEntry.timesheet_id,
                sa.func.coalesce(sa.func.sum(TimesheetEntry.billable_hours), 0),
                sa.func.coalesce(sa.func.sum(TimesheetEntry.billable_days), 0),
            )
            .where(TimesheetEntry.timesheet_id.in_(recent_ids))
            .group_by(TimesheetEntry.timesheet_id)
        ).all()
        entry_totals = {row[0]: (row[1], row[2]) for row in agg_rows}
    recent = []
    for ts, emp in recent_rows:
        b_hours, b_days = entry_totals.get(ts.id, (0, 0))
        recent.append({
            "id": ts.id,
            "employee_name": employee_full_name(emp),
            "period_label": _short_period(ts.year, ts.month),
            "status": _ev(ts.status),
            "billable_hours": _num(b_hours),
            "billable_days": _num(b_days),
        })
    timesheets_block = {
        "total": len(ts_rows),
        "approved": status_counts.get(TimesheetStatus.APPROVED, 0),
        "submitted": status_counts.get(TimesheetStatus.SUBMITTED, 0),
        "draft": status_counts.get(TimesheetStatus.DRAFT, 0),
        "rejected": status_counts.get(TimesheetStatus.REJECTED, 0),
        "coverage": coverage,
        "recent": recent,
    }

    # ------------------------------------------------------------- timeline
    timeline: list[dict] = []

    def add_event(when, event_type: str, label: str) -> None:
        d = _as_date(when)
        if d is None:  # skip null dates
            return
        timeline.append({"date": d.isoformat(), "type": event_type, "label": label,
                         "_sort": d})

    add_event(project.created_at, "PROJECT_CREATED", f"Project {project.name} created")
    for pe, emp in member_rows:
        name = employee_full_name(emp) or f"Employee #{pe.employee_id}"
        add_event(pe.onboarding_date, "MEMBER_ONBOARDED", f"{name} onboarded")
        add_event(pe.billing_date, "BILLING_STARTED", f"Billing started for {name}")
        add_event(pe.exit_date, "MEMBER_EXITED", f"{name} exited")
    for inv, _inv_ts, _inv_emp in invoice_rows:
        add_event(inv.invoice_date, "INVOICE_RAISED", f"Invoice {inv.invoice_number} raised")
    # PO_ALLOCATED is intentionally skipped: POProjectAllocation has no
    # created_at/timestamp column, so there is no allocation date to plot.
    for ts, emp in ts_rows:
        if ts.status == TimesheetStatus.APPROVED and ts.approved_at:
            name = employee_full_name(emp) or f"Employee #{ts.employee_id}"
            add_event(ts.approved_at, "TIMESHEET_APPROVED",
                      f"{_short_period(ts.year, ts.month)} — {name} approved")

    timeline.sort(key=lambda item: item["_sort"])
    for item in timeline:
        del item["_sort"]
    timeline = timeline[:200]

    return {
        "project": project_block,
        "billing_policy": billing_policy,
        "team": team,
        "finance": finance,
        "timesheets": timesheets_block,
        "timeline": timeline,
    }
