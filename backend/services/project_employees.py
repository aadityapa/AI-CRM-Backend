"""Project Employee ownership helpers: leave seeding, rate history, detail rollups.

Policy precedence (Section 5): Customer/branch leave policy governs while deployed
on a client project. Karnex/bench balances are not used for PE-scoped leave apps.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, or_, and_, func
from sqlalchemy.orm import Session

from models import (
    AttendanceStatus, BranchHolidayYear, Customer, CustomerBranch, CustomerLeavePolicy,
    Employee, Holiday, LeaveAccrualEvent, LeaveApplication, LeavePolicyType, POProjectAllocation,
    POStatus, Project, ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectEmployeeRate,
    PurchaseOrder, Timesheet, TimesheetEntry, TimesheetStatus,
)
from services.project_employee_billing import (
    RateRow, compute_billable_days, monthly_accrual, po_status_label, po_utilization,
)
from services.project_employee_leave_credit import accrual_start
from services.projects import project_employee_out
from services.timesheets import _project_branch


def _weekdays_in_month(year: int, month: int) -> int:
    """Number of Mon–Fri days in a month (the T&M working-day base)."""
    return sum(
        1 for day in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, day).weekday() < 5
    )


def effective_customer_branch_for_project(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
    year: int | None = None,
) -> CustomerBranch | None:
    """Resolve the customer branch for holidays / leave policies for a project (and optional PE).

    Precedence:
      1. project.branch_id, else opportunity.branch_id (same-customer) via ``_project_branch``
      2. PE leave-detail → CustomerLeavePolicy.branch_id (majority vote among non-null)
      3. Unique BranchHolidayYear for this customer + year
      4. Unique active branch-scoped CustomerLeavePolicy for this customer
      5. Unique CustomerBranch for this customer
    """
    branch = _project_branch(db, project)
    if branch is not None:
        return branch

    # (2) PE-seeded policy branch hints
    if pe is not None:
        policy_ids = [
            r.customer_leave_policy_id
            for r in db.execute(
                select(ProjectEmployeeLeaveDetail).where(
                    ProjectEmployeeLeaveDetail.project_employee_id == pe.id,
                    ProjectEmployeeLeaveDetail.customer_leave_policy_id.is_not(None),
                )
            ).scalars().all()
            if r.customer_leave_policy_id
        ]
        if policy_ids:
            counts: dict[int, int] = {}
            for pol in db.execute(
                select(CustomerLeavePolicy).where(CustomerLeavePolicy.id.in_(policy_ids))
            ).scalars().all():
                if pol.branch_id is not None:
                    counts[pol.branch_id] = counts.get(pol.branch_id, 0) + 1
            if counts:
                best_id = max(counts, key=counts.get)
                b = db.get(CustomerBranch, best_id)
                if b is not None and b.customer_id == project.customer_id:
                    return b

    year = year or date.today().year

    # (3) Unique holiday calendar for this customer/year
    cal_branches = db.execute(
        select(BranchHolidayYear.branch_id)
        .join(CustomerBranch, CustomerBranch.id == BranchHolidayYear.branch_id)
        .where(
            CustomerBranch.customer_id == project.customer_id,
            BranchHolidayYear.calendar_year == year,
        )
        .distinct()
    ).scalars().all()
    if len(cal_branches) == 1:
        b = db.get(CustomerBranch, cal_branches[0])
        if b is not None:
            return b

    # (4) Unique branch that has active leave policies for this customer
    pol_branches = db.execute(
        select(CustomerLeavePolicy.branch_id).where(
            CustomerLeavePolicy.customer_id == project.customer_id,
            CustomerLeavePolicy.is_active.is_(True),
            CustomerLeavePolicy.branch_id.is_not(None),
        ).distinct()
    ).scalars().all()
    if len(pol_branches) == 1:
        b = db.get(CustomerBranch, pol_branches[0])
        if b is not None:
            return b

    # (5) Unique customer branch
    cust_branches = db.execute(
        select(CustomerBranch).where(CustomerBranch.customer_id == project.customer_id)
    ).scalars().all()
    if len(cust_branches) == 1:
        return cust_branches[0]
    return None


def pe_effective_branch(
    db: Session,
    pe: ProjectEmployee,
    *,
    year: int | None = None,
) -> CustomerBranch | None:
    """Branch this PE belongs to for holiday calendars and leave-policy resolution."""
    project = db.get(Project, pe.project_id)
    if project is None:
        return None
    return effective_customer_branch_for_project(db, project, pe=pe, year=year)


def get_pe_or_404(db: Session, pe_id: int) -> ProjectEmployee:
    pe = db.get(ProjectEmployee, pe_id)
    if not pe:
        raise HTTPException(status_code=404, detail="Project employee not found")
    return pe


def _num(v):
    return float(v) if v is not None else None


def _fmt_days(v: float | Decimal | int | None) -> str:
    if v is None:
        return "0"
    f = float(v)
    return f"{f:g}"


def eligibility_from_policy(policy: CustomerLeavePolicy | None) -> dict:
    """Human-readable + numeric entitlement from a CustomerLeavePolicy copy/link."""
    if policy is None:
        return {
            "yearly_days": None,
            "monthly_days": None,
            "credit_type": None,
            "credit_timing": None,
            "initial_credit": None,
            "period_credit": None,
            "label": "No linked customer leave policy",
        }
    credit_type = (policy.leave_credit_type or "Monthly")
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    period = Decimal(policy.leave_credit_balance or 0)
    initial = Decimal(policy.initial_credit_balance or 0)
    accrual_timing = timing == "End_Of_Period"

    if credit_type == "One_Time":
        total = initial + period
        return {
            "yearly_days": _num(total),
            "monthly_days": None,
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": f"{_fmt_days(total)} days upfront (one-time)",
        }
    if credit_type == "Yearly":
        yearly = period
        monthly = monthly_accrual(period, 12) if period else Decimal("0")
        if accrual_timing:
            label = f"{_fmt_days(yearly)} days/year (monthly accrual)"
        else:
            label = f"{_fmt_days(yearly)} days/year (credited upfront)"
        if initial:
            label = f"{label}; +{_fmt_days(initial)} initial"
        return {
            "yearly_days": _num(yearly),
            "monthly_days": _num(monthly),
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": label,
        }
    if credit_type == "Quarterly":
        yearly = period
        per_q = (period / Decimal(4)).quantize(Decimal("0.01")) if period else Decimal("0")
        monthly = monthly_accrual(period, 12) if period else Decimal("0")
        mode = "accrual" if accrual_timing else "upfront each quarter"
        label = f"{_fmt_days(yearly)} days/year (quarterly · {_fmt_days(per_q)}/quarter · {mode})"
        if initial:
            label = f"{label}; +{_fmt_days(initial)} initial"
        return {
            "yearly_days": _num(yearly),
            "monthly_days": _num(monthly),
            "credit_type": credit_type,
            "credit_timing": timing,
            "initial_credit": _num(initial),
            "period_credit": _num(period),
            "label": label,
        }
    # Monthly (default) — leave_credit_balance is the per-month amount
    monthly = period
    yearly = period * Decimal(12)
    if accrual_timing:
        label = f"{_fmt_days(yearly)} days/year (monthly accrual · {_fmt_days(monthly)}/month)"
    else:
        label = f"{_fmt_days(yearly)} days/year (monthly credit · {_fmt_days(monthly)}/month upfront)"
    if initial:
        label = f"{label}; +{_fmt_days(initial)} initial"
    return {
        "yearly_days": _num(yearly),
        "monthly_days": _num(monthly),
        "credit_type": credit_type,
        "credit_timing": timing,
        "initial_credit": _num(initial),
        "period_credit": _num(period),
        "label": label,
    }


def leave_detail_out(row: ProjectEmployeeLeaveDetail, leave_type: LeavePolicyType | None = None,
                     *, settlement: bool = False,
                     policy: CustomerLeavePolicy | None = None) -> dict:
    from services.employees import is_comp_off_name, is_loss_of_pay_name

    leave_type = leave_type or row.leave_type
    eligibility = eligibility_from_policy(policy)
    name = leave_type.name if leave_type else None
    raw_balance = Decimal(row.leave_balance or 0)
    # Display hygiene: clamp paid (and LOP) balances at >= 0; Comp-Off may stay negative.
    if name and is_comp_off_name(name):
        display_balance = raw_balance
    else:
        display_balance = max(raw_balance, Decimal("0"))
    data = {
        "id": row.id,
        "project_employee_id": row.project_employee_id,
        "leave_type_id": row.leave_type_id,
        "leave_type_name": name,
        "customer_leave_policy_id": row.customer_leave_policy_id,
        "initial_balance": _num(row.initial_balance),
        "opening_balance": _num(row.opening_balance),
        "leave_accrual": _num(row.leave_accrual),
        "leave_consumed": _num(row.leave_consumed),
        "leave_balance": float(display_balance),
        "eligibility": eligibility,
        "eligibility_label": eligibility.get("label"),
        "yearly_entitlement": eligibility.get("yearly_days"),
        "monthly_entitlement": eligibility.get("monthly_days"),
        "is_loss_of_pay": bool(name and is_loss_of_pay_name(name)),
        # Policy cycle metadata so the employee's leave section can show WHEN
        # credits are granted and WHEN unused balance lapses (project override
        # → branch → customer inheritance).
        "leave_credit_type": policy.leave_credit_type if policy else None,
        "leave_credit_timing": getattr(policy, "leave_credit_timing", None) if policy else None,
        "leave_expire": policy.leave_expire if policy else None,
        "leave_expire_timing": getattr(policy, "leave_expire_timing", None) if policy else None,
        # Provenance: which layer this row's crediting rules came from.
        "policy_source": (
            "project" if getattr(row, "project_leave_policy_id", None)
            else ("branch" if (policy is not None and getattr(policy, "branch_id", None) is not None)
                  else ("customer" if policy is not None else None))
        ),
        "project_leave_policy_id": getattr(row, "project_leave_policy_id", None),
    }
    if settlement:
        data["settlement_leave_balance"] = float(display_balance)
        data["needs_settlement"] = True
    return data


def rate_out(row: ProjectEmployeeRate) -> dict:
    return {
        "id": row.id,
        "project_employee_id": row.project_employee_id,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "rate": _num(row.rate),
        "billing_unit": getattr(row.billing_unit, "value", row.billing_unit) if row.billing_unit else None,
        "is_current_rate": bool(row.is_current_rate),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def load_rate_rows(db: Session, pe_id: int) -> list[RateRow]:
    rows = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe_id)
        .order_by(ProjectEmployeeRate.effective_from)
    ).scalars().all()
    return [RateRow.of(r.effective_from, r.rate) for r in rows]


def sync_pe_billing_from_current_rate(pe: ProjectEmployee, rate: ProjectEmployeeRate) -> None:
    """Keep denormalized pe.billing_rate / billing_unit aligned with current commercial rate."""
    pe.billing_rate = rate.rate
    if rate.billing_unit is not None:
        pe.billing_unit = rate.billing_unit


def clear_other_current_rates(db: Session, pe_id: int, keep_id: int | None = None) -> None:
    rows = db.execute(
        select(ProjectEmployeeRate).where(
            ProjectEmployeeRate.project_employee_id == pe_id,
            ProjectEmployeeRate.is_current_rate.is_(True),
        )
    ).scalars().all()
    for r in rows:
        if keep_id is not None and r.id == keep_id:
            continue
        r.is_current_rate = False


def ensure_initial_rate(db: Session, pe: ProjectEmployee, *, flush: bool = True) -> ProjectEmployeeRate:
    """Ensure the PE has at least one current rate row (from billing_rate)."""
    existing = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe.id)
        .order_by(ProjectEmployeeRate.is_current_rate.desc(), ProjectEmployeeRate.effective_from.desc())
    ).scalars().first()
    if existing:
        return existing
    effective = pe.billing_date or pe.onboarding_date or date.today()
    row = ProjectEmployeeRate(
        project_employee_id=pe.id,
        effective_from=effective,
        rate=pe.billing_rate,
        billing_unit=pe.billing_unit,
        is_current_rate=True,
    )
    db.add(row)
    if flush:
        db.flush()
    return row


def apply_rate_rows(db: Session, pe: ProjectEmployee, rows) -> None:
    """Install the Map Employee wizard's Commercial Details as rate history.

    Each incoming row is (effective_from, rate). Expiry is never stored — a rate
    simply ends the day before the next one starts, which is how the invoice
    engine (split_period_by_rate) already reads the table. So "auto-detecting
    the expiry" costs nothing here: it falls out of the ordering.

    Upserts by effective_from, so remapping an exited employee refines the
    history rather than duplicating dates. The current flag then lands on the
    row in force TODAY — the latest effective_from <= today — or the earliest
    row when every date is still in the future. pe.billing_rate is synced from
    that row so list pages and PO checks keep reading one denormalized number.
    """
    if not rows:
        return
    existing = {
        r.effective_from: r
        for r in db.execute(
            select(ProjectEmployeeRate)
            .where(ProjectEmployeeRate.project_employee_id == pe.id)
        ).scalars()
    }
    for entry in sorted(rows, key=lambda r: r.effective_from):
        row = existing.get(entry.effective_from)
        if row is not None:
            row.rate = entry.rate
            row.billing_unit = pe.billing_unit
        else:
            row = ProjectEmployeeRate(
                project_employee_id=pe.id,
                effective_from=entry.effective_from,
                rate=entry.rate,
                billing_unit=pe.billing_unit,
                is_current_rate=False,
            )
            db.add(row)
            existing[entry.effective_from] = row
    db.flush()

    all_rows = sorted(existing.values(), key=lambda r: r.effective_from)
    today = date.today()
    in_force = [r for r in all_rows if r.effective_from <= today]
    current = in_force[-1] if in_force else all_rows[0]
    for r in all_rows:
        r.is_current_rate = r is current
    sync_pe_billing_from_current_rate(pe, current)


def _seed_balance_from_policy(policy: CustomerLeavePolicy) -> tuple[Decimal, Decimal]:
    """Return (initial_balance, leave_accrual) respecting leave_credit_timing.

    Start_Of_Period / One_Time / Yearly: credit initial + period balance upfront.
    End_Of_Period (monthly/quarterly): open with initial only; accrual tracks the
    per-period amount that will credit later.
    """
    initial = Decimal(policy.initial_credit_balance or 0)
    period = Decimal(policy.leave_credit_balance or 0)
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    credit_type = (policy.leave_credit_type or "Monthly")

    if credit_type == "One_Time":
        return initial + period, Decimal("0")
    if credit_type == "Yearly":
        if timing == "End_Of_Period":
            return initial, monthly_accrual(period, 12) if period else Decimal("0")
        return initial + period, Decimal("0")
    if credit_type == "Quarterly":
        per = (period / Decimal(4)).quantize(Decimal("0.01")) if period else Decimal("0")
        if timing == "End_Of_Period":
            return initial, per
        return initial + per, per
    # Monthly (default)
    if timing == "End_Of_Period":
        return initial, period
    return initial + period, period


def resolve_customer_leave_policies(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
    branch: CustomerBranch | None = None,
) -> list[CustomerLeavePolicy]:
    """Active customer leave policies for the project (branch-specific preferred, else customer-wide)."""
    if branch is None:
        branch = (
            pe_effective_branch(db, pe) if pe is not None
            else effective_customer_branch_for_project(db, project)
        )
    all_pols = db.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.customer_id == project.customer_id,
            CustomerLeavePolicy.is_active.is_(True),
        )
    ).scalars().all()
    if not all_pols:
        return []
    if branch is not None:
        branch_pols = [p for p in all_pols if p.branch_id == branch.id]
        if branch_pols:
            return branch_pols
    return [p for p in all_pols if p.branch_id is None] or all_pols


def resolve_effective_leave_policies(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
    branch: CustomerBranch | None = None,
) -> list[tuple[object, str]]:
    """CREDITING policy chain: Project override → Branch → Customer.

    Returns ``[(policy, source)]`` where ``policy`` is a ProjectLeavePolicy
    ("project") or CustomerLeavePolicy ("branch"/"customer"). A project row for
    a leave type REPLACES the branch/customer row for that type; other types
    keep inheriting. Billability (`is_billable`) intentionally stays on the
    branch/customer chain (`resolve_customer_leave_policies`) — project rows do
    not carry it.
    """
    from models import ProjectLeavePolicy

    merged: dict[int, tuple[object, str]] = {}
    for pol in resolve_customer_leave_policies(db, project, pe=pe, branch=branch):
        merged[pol.leave_type_id] = (
            pol, "branch" if pol.branch_id is not None else "customer",
        )
    overrides = db.execute(
        select(ProjectLeavePolicy).where(
            ProjectLeavePolicy.project_id == project.id,
            ProjectLeavePolicy.is_active.is_(True),
        )
    ).scalars().all()
    for pol in overrides:
        merged[pol.leave_type_id] = (pol, "project")
    return list(merged.values())


def seed_leave_details_from_customer_policy(
    db: Session,
    pe: ProjectEmployee,
    project: Project | None = None,
    *,
    seed_as_of: date | None = None,
) -> list[ProjectEmployeeLeaveDetail]:
    """Seed PE leave rows from Customer Leave Policy. Skip types that already exist.

    ``policy.effective_date`` (via ``accrual_start``) only pushes the accrual start
    later — never earlier than onboarding. When accrual_start is still in the future
    relative to ``seed_as_of`` (default today):

    * One_Time / Yearly Start_Of_Period — open at ``initial_credit_balance`` only;
      stash ``leave_credit_balance`` on ``leave_accrual`` for the monthly credit job.
    * Monthly / Quarterly Start_Of_Period — open like End_Of_Period (initial only;
      recurring accrual on ``leave_accrual``).

    Editing ``effective_date`` later never rewrites already-seeded rows; only new
    seeds and future credit-job runs observe the new date.
    """
    project = project or db.get(Project, pe.project_id)
    if project is None:
        return []
    seed_as_of = seed_as_of or date.today()
    existing_types = {
        r.leave_type_id
        for r in db.execute(
            select(ProjectEmployeeLeaveDetail)
            .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
        ).scalars().all()
    }
    created: list[ProjectEmployeeLeaveDetail] = []
    for policy, policy_source in resolve_effective_leave_policies(db, project, pe=pe):
        if policy.leave_type_id in existing_types:
            continue
        initial, accrual = _seed_balance_from_policy(policy)
        # Upfront (One_Time / Yearly-start) policies with proration enabled credit a
        # fraction of the annual grant based on the join month: remaining months / 12
        # (e.g. onboard in July -> 6/12 of 18 = 9). Accrual policies open at 0 and are
        # prorated per-day by the monthly credit job instead.
        _ct = (policy.leave_credit_type or "Monthly")
        _timing = (policy.leave_credit_timing or "Start_Of_Period")
        _is_upfront = _ct == "One_Time" or (_ct == "Yearly" and _timing != "End_Of_Period")
        start = accrual_start(policy, pe)
        deferred = start is not None and start > seed_as_of
        if deferred:
            base_initial = Decimal(policy.initial_credit_balance or 0)
            period_amt = Decimal(policy.leave_credit_balance or 0)
            if _is_upfront:
                # Defer the period grant; credit job releases it on/after accrual_start.
                initial = base_initial
                accrual = period_amt
            elif _timing != "End_Of_Period":
                # Monthly/Quarterly Start → behave like End_Of_Period until start.
                initial = base_initial
                if _ct == "Quarterly":
                    accrual = (
                        (period_amt / Decimal(4)).quantize(Decimal("0.01"))
                        if period_amt else Decimal("0")
                    )
                else:
                    accrual = period_amt
        elif _is_upfront and getattr(policy, "prorate_balance_credit", False) and initial > 0:
            # Prorate by accrual_start month (onboarding, or later effective_date).
            # ProjectLeavePolicy has no prorate flag → treated as False.
            proration_date = start or pe.onboarding_date
            if proration_date is not None:
                factor = Decimal(13 - proration_date.month) / Decimal(12)
                factor = min(max(factor, Decimal("0")), Decimal("1"))
                initial = (initial * factor).quantize(Decimal("0.01"))
        bal = initial
        _max_limit = getattr(policy, "max_limit", None)  # not on ProjectLeavePolicy
        if policy.is_max_limit and _max_limit is not None:
            bal = min(bal, Decimal(_max_limit))
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe.id,
            leave_type_id=policy.leave_type_id,
            # Exactly one FK per row: project override OR customer/branch policy.
            customer_leave_policy_id=policy.id if policy_source != "project" else None,
            project_leave_policy_id=policy.id if policy_source == "project" else None,
            initial_balance=initial,
            opening_balance=initial,
            leave_accrual=accrual,
            leave_consumed=Decimal("0"),
            leave_balance=bal,
        )
        db.add(row)
        created.append(row)
        existing_types.add(policy.leave_type_id)
        if initial > 0:
            db.add(LeaveAccrualEvent(
                employee_id=pe.employee_id,
                leave_type_id=policy.leave_type_id,
                event_type="Accrual",
                amount=initial,
                balance_after=bal,
                source=f"pe_seed:{pe.id}:{policy.leave_type_id}",
                note=f"PE#{pe.id} leave seeded from {policy_source} policy #{policy.id}",
            ))
    if created:
        db.flush()
    return created


def sync_pe_leave_from_customer_policy(
    db: Session, pe: ProjectEmployee,
) -> dict:
    """Idempotent back-fill: add missing leave types from customer/branch policy.

    Existing PE leave rows (balances / consumed) are never modified.
    """
    project = db.get(Project, pe.project_id)
    created = seed_leave_details_from_customer_policy(db, pe, project)
    type_names: dict[int, str] = {}
    if created:
        ids = [r.leave_type_id for r in created]
        for lt in db.execute(
            select(LeavePolicyType).where(LeavePolicyType.id.in_(ids))
        ).scalars().all():
            type_names[lt.id] = lt.name
    return {
        "added_count": len(created),
        "added": [
            {
                "leave_type_id": r.leave_type_id,
                "leave_type_name": type_names.get(r.leave_type_id),
                "leave_balance": float(r.leave_balance or 0),
                "customer_leave_policy_id": r.customer_leave_policy_id,
            }
            for r in created
        ],
    }


def pe_leave_detail_for(db: Session, pe_id: int,
                        leave_type_id: int) -> ProjectEmployeeLeaveDetail | None:
    return db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe_id,
            ProjectEmployeeLeaveDetail.leave_type_id == leave_type_id,
        )
    ).scalars().first()


def pe_leave_balance_total(db: Session, pe_id: int) -> float:
    """Net leave balance from per-type balances clamped at >= 0 (excl. LOP type).

    Comp-Off negatives are also floored for the net total so one overdrawn type
    cannot understate the remaining paid pool. Loss of Pay is excluded (shown
    separately via consumed).
    """
    from services.employees import is_loss_of_pay_name

    rows = db.execute(
        select(ProjectEmployeeLeaveDetail, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id,
              isouter=True)
        .where(ProjectEmployeeLeaveDetail.project_employee_id == pe_id)
    ).all()
    total = Decimal("0")
    for row, name in rows:
        if name and is_loss_of_pay_name(name):
            continue
        total += max(Decimal(row.leave_balance or 0), Decimal("0"))
    return float(total)


def pe_leave_balance_totals(db: Session, pe_ids) -> dict[int, float]:
    """Batched pe_leave_balance_total: ONE query for a whole list page instead
    of one per row. Same clamping semantics as the single-row version."""
    from services.employees import is_loss_of_pay_name

    ids = [int(i) for i in pe_ids]
    if not ids:
        return {}
    rows = db.execute(
        select(ProjectEmployeeLeaveDetail, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id,
              isouter=True)
        .where(ProjectEmployeeLeaveDetail.project_employee_id.in_(ids))
    ).all()
    totals: dict[int, Decimal] = {i: Decimal("0") for i in ids}
    for row, name in rows:
        if name and is_loss_of_pay_name(name):
            continue
        totals[row.project_employee_id] += max(Decimal(row.leave_balance or 0), Decimal("0"))
    return {i: float(v) for i, v in totals.items()}


def consume_pe_leave(db: Session, pe_id: int, leave_type_id: int, days: Decimal,
                     *, allow_negative: bool = False) -> ProjectEmployeeLeaveDetail:
    """Debit PE leave balance on leave approval. Creates a zero row if missing."""
    row = pe_leave_detail_for(db, pe_id, leave_type_id)
    if row is None:
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe_id,
            leave_type_id=leave_type_id,
            initial_balance=0,
            opening_balance=0,
            leave_accrual=0,
            leave_consumed=0,
            leave_balance=0,
        )
        db.add(row)
        db.flush()
    available = Decimal(row.leave_balance or 0)
    if days > available and not allow_negative:
        leave_type = db.get(LeavePolicyType, leave_type_id)
        name = leave_type.name if leave_type else "Leave"
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient PE {name} balance: requested {float(days):g}, "
                   f"available {float(available):g}",
        )
    row.leave_consumed = Decimal(row.leave_consumed or 0) + days
    row.leave_balance = available - days
    return row


def credit_pe_leave(db: Session, pe_id: int, leave_type_id: int, days: Decimal) -> ProjectEmployeeLeaveDetail:
    """Credit PE leave balance (comp-off earned on PE-scoped apps)."""
    row = pe_leave_detail_for(db, pe_id, leave_type_id)
    if row is None:
        row = ProjectEmployeeLeaveDetail(
            project_employee_id=pe_id,
            leave_type_id=leave_type_id,
            initial_balance=0,
            opening_balance=0,
            leave_accrual=0,
            leave_consumed=0,
            leave_balance=0,
        )
        db.add(row)
        db.flush()
    row.leave_accrual = Decimal(row.leave_accrual or 0) + days
    row.leave_balance = Decimal(row.leave_balance or 0) + days
    return row


def project_po_summary(db: Session, project_id: int, *, on_date: date | None = None) -> dict:
    """PO drawdown summary for a project (PE commercial tab + list chip)."""
    on_date = on_date or date.today()
    alloc = db.execute(
        select(POProjectAllocation, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == POProjectAllocation.po_id)
        .where(POProjectAllocation.project_id == project_id)
        .order_by(PurchaseOrder.status.asc(), POProjectAllocation.id)
    ).first()
    if not alloc:
        return {
            "po_id": None,
            "po_number": None,
            "allocated_amount": None,
            "consumed_amount": None,
            "utilization_pct": None,
            "po_status": "ok",
            "expired": False,
            "end_date": None,
        }
    row, po = alloc
    allocated = Decimal(row.allocated_amount or 0)
    consumed = Decimal(row.consumed_amount or 0)
    pct, status = po_utilization(allocated, consumed)
    expired = bool(po.end_date and on_date > po.end_date)
    if getattr(po.status, "value", po.status) == POStatus.EXHAUSTED.value:
        status = "blocked"
    status = po_status_label(status, expired=expired)
    return {
        "po_id": po.id,
        "po_number": po.po_number,
        "allocated_amount": float(allocated),
        "consumed_amount": float(consumed),
        "balance_value": _num(po.balance_value),
        "utilization_pct": float(pct),
        "po_status": status,
        "expired": expired,
        "end_date": po.end_date.isoformat() if po.end_date else None,
    }


def holidays_for_pe(db: Session, pe: ProjectEmployee, *, year: int | None = None) -> list[dict]:
    """Read-only National + customer-wide + PE-branch holiday calendar for this PE."""
    project = db.get(Project, pe.project_id)
    if project is None:
        return []
    year = year or date.today().year
    branch = pe_effective_branch(db, pe, year=year)
    return _holidays_for_customer_branch(db, project.customer_id, branch, year=year)


def _holidays_for_customer_branch(
    db: Session,
    customer_id: int,
    branch: CustomerBranch | None,
    *,
    year: int,
) -> list[dict]:
    """National + customer-wide + branch-scoped (and calendar-attached) holidays."""
    calendar_ids: list[int] = []
    if branch is not None:
        calendar_ids = list(db.execute(
            select(BranchHolidayYear.id).where(
                BranchHolidayYear.branch_id == branch.id,
                BranchHolidayYear.calendar_year == year,
            )
        ).scalars().all())

    branch_parts = []
    if branch is not None:
        branch_parts.append(Holiday.branch_id == branch.id)
    if calendar_ids:
        branch_parts.append(Holiday.holiday_calendar_id.in_(calendar_ids))

    if branch_parts:
        customer_clause = and_(
            Holiday.customer_id == customer_id,
            or_(Holiday.branch_id.is_(None), *branch_parts),
        )
    else:
        customer_clause = and_(
            Holiday.customer_id == customer_id,
            Holiday.branch_id.is_(None),
        )

    rows = db.execute(
        select(Holiday).where(
            Holiday.is_active.is_(True),
            Holiday.year == year,
            or_(Holiday.customer_id.is_(None), customer_clause),
        )
    ).scalars().all()

    # Prefer branch/customer rows over National when the same date+name appears twice.
    def _rank(h: Holiday) -> int:
        if h.customer_id is not None and (h.branch_id is not None or h.holiday_calendar_id):
            return 0
        if h.customer_id is not None:
            return 1
        return 2

    rows_sorted = sorted(
        rows,
        key=lambda h: (h.holiday_date or date.min, _rank(h), h.id or 0),
    )
    seen: set[tuple] = set()
    out: list[dict] = []
    for h in rows_sorted:
        key = (h.holiday_date, (h.name or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": h.id,
            "name": h.name,
            "holiday_date": h.holiday_date.isoformat() if h.holiday_date else None,
            "holiday_type": h.holiday_type,
            "customer_id": h.customer_id,
            "branch_id": h.branch_id,
            "holiday_calendar_id": h.holiday_calendar_id,
            "year": h.year,
            "scope": ("National" if h.customer_id is None
                      else ("Branch" if h.branch_id is not None or h.holiday_calendar_id
                            else "Customer")),
        })
    return out


def pe_credit_history(db: Session, pe: ProjectEmployee, *, limit: int = 50) -> list[dict]:
    """LeaveAccrualEvent rows scoped to this PE (seed / credit job / PE leave apps)."""
    pe_note = f"%PE#{pe.id}%"
    rows = db.execute(
        select(LeaveAccrualEvent, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveAccrualEvent.leave_type_id, isouter=True)
        .where(
            LeaveAccrualEvent.employee_id == pe.employee_id,
            or_(
                LeaveAccrualEvent.source.like(f"pe_credit:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_seed:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_expire:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_carry:{pe.id}:%"),
                LeaveAccrualEvent.note.like(pe_note),
            ),
        )
        .order_by(LeaveAccrualEvent.created_at.desc(), LeaveAccrualEvent.id.desc())
        .limit(limit)
    ).all()
    return [{
        "id": ev.id,
        "event_type": ev.event_type,
        "leave_type_id": ev.leave_type_id,
        "leave_type_name": type_name,
        "amount": _num(ev.amount),
        "balance_after": _num(ev.balance_after),
        "source": ev.source,
        "note": ev.note,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    } for ev, type_name in rows]


def pe_leave_applications(db: Session, pe: ProjectEmployee, *, limit: int = 50) -> list[dict]:
    rows = db.execute(
        select(LeaveApplication, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveApplication.leave_type_id, isouter=True)
        .where(LeaveApplication.project_employee_id == pe.id)
        .order_by(LeaveApplication.id.desc())
        .limit(limit)
    ).all()
    return [{
        "id": app.id,
        "leave_type_id": app.leave_type_id,
        "leave_type_name": type_name,
        "leave_period_type": app.leave_period_type,
        "from_date": app.from_date.isoformat() if app.from_date else None,
        "to_date": app.to_date.isoformat() if app.to_date else None,
        "days": _num(app.days),
        "status": app.status,
        "reason": app.reason,
        "comp_off_type": app.comp_off_type,
        "applied_at": app.applied_at.isoformat() if app.applied_at else None,
        "decided_at": app.decided_at.isoformat() if app.decided_at else None,
    } for app, type_name in rows]


def pe_accrued_this_year(db: Session, pe: ProjectEmployee, *, year: int | None = None) -> float:
    """Sum of positive PE-scoped credit events in the calendar year."""
    year = year or date.today().year
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    pe_note = f"%PE#{pe.id}%"
    total = db.execute(
        select(func.coalesce(func.sum(LeaveAccrualEvent.amount), 0)).where(
            LeaveAccrualEvent.employee_id == pe.employee_id,
            LeaveAccrualEvent.amount > 0,
            LeaveAccrualEvent.event_type.in_(["Accrual", "Comp_Off_Credit", "Carry_Forward", "Adjustment"]),
            func.date(LeaveAccrualEvent.created_at) >= start,
            func.date(LeaveAccrualEvent.created_at) <= end,
            or_(
                LeaveAccrualEvent.source.like(f"pe_credit:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_seed:{pe.id}:%"),
                LeaveAccrualEvent.source.like(f"pe_carry:{pe.id}:%"),
                LeaveAccrualEvent.note.like(pe_note),
            ),
        )
    ).scalar()
    return float(total or 0)


def timesheet_rollups_for_pe(db: Session, pe: ProjectEmployee) -> list[dict]:
    """Per-period rollup of timesheets belonging to this PE mapping."""
    stmt = select(Timesheet).where(
        Timesheet.project_id == pe.project_id,
        Timesheet.employee_id == pe.employee_id,
    ).order_by(Timesheet.year.desc(), Timesheet.month.desc())
    sheets = db.execute(stmt).scalars().all()
    # Batch-load ALL entries for these timesheets in ONE query (was N+1: a SELECT
    # per timesheet), grouped by timesheet_id — identical data, far fewer queries.
    entries_by_ts: dict[int, list[TimesheetEntry]] = {}
    _sheet_ids = [ts.id for ts in sheets]
    if _sheet_ids:
        for _e in db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_(_sheet_ids))
        ).scalars().all():
            entries_by_ts.setdefault(_e.timesheet_id, []).append(_e)
    out: list[dict] = []
    # Client holiday calendar for this mapping, counted per period (weekend holidays
    # still count — a client holiday on a Saturday is a non-billable day under T&M).
    holiday_dates_by_month: dict[int, int] = {}
    for h in holidays_for_pe(db, pe):
        hd = h.get("holiday_date")
        if hd:
            m = int(hd[5:7])
            holiday_dates_by_month[m] = holiday_dates_by_month.get(m, 0) + 1
    for ts in sheets:
        entries = entries_by_ts.get(ts.id, [])
        billable_hours = Decimal("0")
        billable_days_stored = Decimal("0")
        leave = 0
        for e in entries:
            if e.attendance_status == AttendanceStatus.LEAVE:
                leave += 1
            billable_days_stored += Decimal(e.billable_days or 0)
            billable_hours += Decimal(e.billable_hours or 0)
        # Working base = weekdays in the period; billable = base − leave − holidays.
        # This is the invoice basis (day-count), independent of hours logged.
        working = _weekdays_in_month(ts.year, ts.month)
        holidays = holiday_dates_by_month.get(ts.month, 0)
        formula_billable = compute_billable_days(working, leave, holidays)
        out.append({
            "timesheet_id": ts.id,
            "month": ts.month,
            "year": ts.year,
            "status": getattr(ts.status, "value", ts.status),
            "working_days": working,
            "leave_days": leave,
            "holiday_days": holidays,
            "billable_days": float(formula_billable),
            "billable_days_formula": float(formula_billable),
            "billable_days_worked": float(billable_days_stored),
            "billable_formula": "working (weekdays) − leave − holidays (client calendar)",
            "billable_hours": float(billable_hours),
        })
    return out


def exit_project_employee(db: Session, pe: ProjectEmployee, *, exit_date: date | None = None) -> dict:
    """Mark PE exited: stop accrual (flag), deactivate, flag open Draft/Submitted sheets."""
    exit_date = exit_date or date.today()
    pe.is_exit = True
    pe.exit_date = exit_date
    pe.is_active = False
    open_sheets = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == pe.project_id,
            Timesheet.employee_id == pe.employee_id,
            Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.SUBMITTED,
                                  TimesheetStatus.REJECTED]),
        )
    ).scalars().all()
    closed = []
    for ts in open_sheets:
        note = f"PE exited {exit_date.isoformat()}; period flagged for settlement"
        if not ts.rejection_reason:
            ts.rejection_reason = note
        closed.append({"timesheet_id": ts.id, "month": ts.month, "year": ts.year,
                       "status": getattr(ts.status, "value", ts.status)})
    leave_rows = db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all()
    settlement = [{
        "leave_type_id": r.leave_type_id,
        "leave_balance": _num(r.leave_balance),
        "needs_settlement": True,
    } for r in leave_rows]
    pe.settlement_pending = bool(settlement)
    return {
        "exit_date": exit_date.isoformat(),
        "closed_periods": closed,
        "settlement_leave": settlement,
        "accrual_stopped": True,
        "settlement_pending": pe.settlement_pending,
    }


def project_employee_detail_out(db: Session, pe: ProjectEmployee) -> dict:
    project = db.get(Project, pe.project_id)
    emp = db.get(Employee, pe.employee_id) or pe.employee
    customer = db.get(Customer, project.customer_id) if project else None
    data = project_employee_out(pe, emp)
    data["project_name"] = project.name if project else None
    data["customer_id"] = project.customer_id if project else None
    data["customer_name"] = customer.name if customer else None
    holiday_year = date.today().year
    branch = pe_effective_branch(db, pe, year=holiday_year) if project else None
    opp_branch = _project_branch(db, project) if project else None
    from services.timesheets import opportunity_branch_foreign_to_project
    foreign = opportunity_branch_foreign_to_project(db, project) if project else False
    data["branch_id"] = branch.id if branch else None
    data["branch_name"] = branch.branch_name if branch else None
    data["branch_unlinked"] = bool(branch is None and foreign)
    data["branch_link_message"] = (
        "Branch not linked to this customer" if data["branch_unlinked"] else None
    )
    data["branch_resolved_via"] = (
        "opportunity" if (branch is not None and opp_branch is not None and branch.id == opp_branch.id)
        else ("fallback" if branch is not None else None)
    )
    data["leave_balance_total"] = pe_leave_balance_total(db, pe.id)

    leave_rows = db.execute(
        select(ProjectEmployeeLeaveDetail, LeavePolicyType)
        .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id, isouter=True)
        .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
        .order_by(ProjectEmployeeLeaveDetail.id)
    ).all()
    settlement = bool(pe.is_exit or getattr(pe, "settlement_pending", False))
    policy_ids = {r.customer_leave_policy_id for r, _ in leave_rows if r.customer_leave_policy_id}
    policies = {
        p.id: p
        for p in db.execute(
            select(CustomerLeavePolicy).where(CustomerLeavePolicy.id.in_(policy_ids or [-1]))
        ).scalars().all()
    } if policy_ids else {}
    # Project-level overrides: rows seeded from a ProjectLeavePolicy carry that
    # FK instead — load them in one query so cycle metadata + provenance render.
    from models import ProjectLeavePolicy
    proj_policy_ids = {
        r.project_leave_policy_id
        for r, _ in leave_rows
        if getattr(r, "project_leave_policy_id", None)
    }
    proj_policies = {
        p.id: p
        for p in db.execute(
            select(ProjectLeavePolicy).where(ProjectLeavePolicy.id.in_(proj_policy_ids or [-1]))
        ).scalars().all()
    } if proj_policy_ids else {}
    details_out = []
    consumed_total = Decimal("0")
    lop_consumed = Decimal("0")
    from services.employees import is_loss_of_pay_name
    for r, lt in leave_rows:
        if getattr(r, "project_leave_policy_id", None):
            policy = proj_policies.get(r.project_leave_policy_id)
        else:
            policy = policies.get(r.customer_leave_policy_id) if r.customer_leave_policy_id else None
        details_out.append(leave_detail_out(r, lt, settlement=settlement, policy=policy))
        if lt is not None and is_loss_of_pay_name(lt.name):
            lop_consumed += Decimal(r.leave_consumed or 0)
        else:
            consumed_total += Decimal(r.leave_consumed or 0)
    data["leave_details"] = details_out
    data["leave_eligibility"] = [
        {
            "leave_type_id": d["leave_type_id"],
            "leave_type_name": d["leave_type_name"],
            "label": d.get("eligibility_label"),
            "yearly_days": d.get("yearly_entitlement"),
            "monthly_days": d.get("monthly_entitlement"),
            "credit_type": (d.get("eligibility") or {}).get("credit_type"),
            "credit_timing": (d.get("eligibility") or {}).get("credit_timing"),
        }
        for d in details_out
    ]
    credit_history = pe_credit_history(db, pe)
    accrued_ytd = pe_accrued_this_year(db, pe)
    # Fallback when ledger is empty: opening/initial balances seeded without events
    if accrued_ytd <= 0 and details_out and not credit_history:
        accrued_ytd = float(sum(Decimal(d.get("initial_balance") or 0) for d in details_out))
    data["leave_summary"] = {
        "balance": data["leave_balance_total"],
        "consumed": float(consumed_total),
        "accrued_this_year": accrued_ytd,
        "year": date.today().year,
        "loss_of_pay_consumed": float(lop_consumed),
    }
    data["credit_history"] = credit_history
    data["leave_applications"] = pe_leave_applications(db, pe)

    rates = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe.id)
        .order_by(ProjectEmployeeRate.effective_from.desc(), ProjectEmployeeRate.id.desc())
    ).scalars().all()
    data["rates"] = [rate_out(r) for r in rates]
    data["timesheet_rollups"] = timesheet_rollups_for_pe(db, pe)
    data["holidays"] = holidays_for_pe(db, pe, year=holiday_year)
    branch_linked = branch is not None
    data["holiday_calendar"] = {
        "year": holiday_year,
        "customer_id": project.customer_id if project else None,
        "customer_name": customer.name if customer else None,
        "branch_id": branch.id if branch else None,
        "branch_name": branch.branch_name if branch else None,
        "branch_linked": branch_linked,
        "label": (
            f"{customer.name} / {branch.branch_name} / {holiday_year}"
            if customer and branch
            else (f"{customer.name} / {holiday_year}" if customer
                  else f"Client calendar {holiday_year}")
        ),
        "note": (
            None if branch_linked
            else "Branch not linked — showing National and customer-wide holidays only."
        ),
        "count": len(data["holidays"]),
        "read_only": True,
    }
    data["po"] = project_po_summary(db, pe.project_id)
    data["po_status"] = data["po"].get("po_status")
    return data


def enrich_pe_list_row(db: Session, pe: ProjectEmployee, base: dict) -> dict:
    base["leave_balance_total"] = pe_leave_balance_total(db, pe.id)
    po = project_po_summary(db, pe.project_id)
    base["po_status"] = po.get("po_status")
    base["po_utilization_pct"] = po.get("utilization_pct")
    base["po_number"] = po.get("po_number")
    return base


def group_pe_rows_by_employee(rows: list[dict]) -> list[dict]:
    """Collapse flat PE list rows into one group per employee (UC-12).

    Preserves first-seen employee order; each group carries mappings[] with
    per-mapping rate / leave / PO chips unchanged.
    """
    groups: dict[int, dict] = {}
    order: list[int] = []
    for row in rows:
        eid = int(row["employee_id"])
        if eid not in groups:
            groups[eid] = {
                "employee_id": eid,
                "employee_name": row.get("employee_name"),
                "employee_email": row.get("employee_email"),
                "mapping_count": 0,
                "mappings": [],
            }
            order.append(eid)
        groups[eid]["mappings"].append(row)
        groups[eid]["mapping_count"] = len(groups[eid]["mappings"])
    return [groups[eid] for eid in order]


# --------------------------------------------------------------- invoice rollup
def invoice_rollups_for_pe(db: Session, pe: ProjectEmployee) -> dict:
    """Read-only per-period invoice preview for the Project Employee Invoice sub-tab.

    One row per timesheet period, reusing the same computed preview the Finance
    module uses (effective-dated / split rates, Monthly-vs-Daily-vs-Hourly basis,
    linked invoice + can_generate). Billable days come from the standardised
    day-count roll-up so the Timesheet and Invoice tabs always agree.
    """
    from services.timesheets import timesheet_invoice_preview

    rollup_by_period = {
        (r["year"], r["month"]): r for r in timesheet_rollups_for_pe(db, pe)
    }
    sheets = db.execute(
        select(Timesheet).where(
            Timesheet.project_id == pe.project_id,
            Timesheet.employee_id == pe.employee_id,
        ).order_by(Timesheet.year.desc(), Timesheet.month.desc())
    ).scalars().all()
    periods: list[dict] = []
    for ts in sheets:
        try:
            prev = timesheet_invoice_preview(db, ts)
        except Exception:
            continue
        line = prev["line_items"][0] if prev.get("line_items") else {}
        roll = rollup_by_period.get((ts.year, ts.month), {})
        periods.append({
            "timesheet_id": ts.id,
            "month": ts.month,
            "year": ts.year,
            "status": getattr(ts.status, "value", ts.status),
            "billable_days": roll.get("billable_days"),
            "rate_per_unit": line.get("rate_per_unit"),
            "monthly_cost": line.get("monthly_cost"),
            "rate_split": line.get("rate_split", False),
            "amount": prev.get("totals", {}).get("sub_total"),
            "description": line.get("description"),
            "linked_invoice": prev.get("linked_invoice"),
            "can_generate": prev.get("can_generate", False),
        })
    return {
        "project_employee_id": pe.id,
        "billing_unit": getattr(pe.billing_unit, "value", pe.billing_unit),
        "current_rate": _num(pe.billing_rate),
        "po": project_po_summary(db, pe.project_id),
        "periods": periods,
        "read_only": True,
    }


# --------------------------------------------------- employee cross-project leave
def employee_project_leave(db: Session, employee_id: int) -> dict:
    """Consolidated 'My Leave' rollup: every project mapping's leave balance for one
    employee, grouped by project, with a grand total. This is the employee-facing
    view that makes multi-project leave legible without breaking per-client accounting.
    """
    pes = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.employee_id == employee_id)
        .order_by(ProjectEmployee.is_active.desc(), ProjectEmployee.id)
    ).scalars().all()
    projects_out: list[dict] = []
    grand_total = Decimal("0")
    for pe in pes:
        project = db.get(Project, pe.project_id)
        customer = db.get(Customer, project.customer_id) if project else None
        rows = db.execute(
            select(ProjectEmployeeLeaveDetail, LeavePolicyType)
            .join(LeavePolicyType,
                  LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id, isouter=True)
            .where(ProjectEmployeeLeaveDetail.project_employee_id == pe.id)
            .order_by(ProjectEmployeeLeaveDetail.id)
        ).all()
        # Bulk-load linked policies (customer/branch + project override) so
        # each row serializes with cycle metadata and provenance.
        from models import ProjectLeavePolicy
        cust_ids = {r.customer_leave_policy_id for r, _ in rows if r.customer_leave_policy_id}
        proj_ids = {
            r.project_leave_policy_id for r, _ in rows
            if getattr(r, "project_leave_policy_id", None)
        }
        cust_pols = {
            p.id: p for p in db.execute(
                select(CustomerLeavePolicy).where(CustomerLeavePolicy.id.in_(cust_ids or [-1]))
            ).scalars().all()
        } if cust_ids else {}
        proj_pols = {
            p.id: p for p in db.execute(
                select(ProjectLeavePolicy).where(ProjectLeavePolicy.id.in_(proj_ids or [-1]))
            ).scalars().all()
        } if proj_ids else {}

        def _pol(r):
            if getattr(r, "project_leave_policy_id", None):
                return proj_pols.get(r.project_leave_policy_id)
            return cust_pols.get(r.customer_leave_policy_id) if r.customer_leave_policy_id else None

        subtotal = sum((Decimal(r.leave_balance or 0) for r, _ in rows), Decimal("0"))
        grand_total += subtotal
        projects_out.append({
            "project_employee_id": pe.id,
            "project_id": pe.project_id,
            "project_name": project.name if project else None,
            "customer_name": customer.name if customer else None,
            "is_active": bool(pe.is_active),
            "is_exit": bool(pe.is_exit),
            "leave_balance_total": float(subtotal),
            "leave_details": [leave_detail_out(r, lt, policy=_pol(r)) for r, lt in rows],
        })
    return {
        "employee_id": employee_id,
        "total_leave_balance": float(grand_total),
        "projects": projects_out,
    }
