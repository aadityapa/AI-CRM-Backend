"""Monthly PE leave credit / carry-forward / expiry job.

Callable from CLI (`scripts/run_pe_leave_credit.py`) or a scheduler. Credits are
applied to `project_employee_leave_details` only for **active, non-exited**
Project Employees. Policy edits after seed do not rewrite opening balances.

Accrual lower bound: ``accrual_start(policy, pe)`` =
``max(policy.effective_date, pe.onboarding_date)`` over non-null sides. Months
wholly before that date credit 0; mid-period joins prorate when enabled.

Deferred upfront (One_Time / Yearly Start_Of_Period): when accrual_start is
still in the future at seed time, the period grant is stashed on
``leave_accrual`` and granted by this job in the first eligible month.

Idempotency: one `leave_accrual_events` row per PE/leave_type/period via
`source = pe_credit:{pe_id}:{leave_type_id}:{YYYY-MM}`.
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    CustomerLeavePolicy, LeaveAccrualEvent, ProjectEmployee, ProjectEmployeeLeaveDetail,
)
from services.project_employee_billing import carry_forward, prorate_credit

ZERO = Decimal("0")


def _row_policy(db: Session, row: ProjectEmployeeLeaveDetail):
    """Crediting policy for a PE leave row: PROJECT override wins, else the
    customer/branch policy. Both types share the crediting field surface
    (credit type/timing, balances, expire cycle/timing, carry cap); fields a
    ProjectLeavePolicy lacks (prorate_balance_credit, max_limit) are read via
    getattr with safe defaults by the callers."""
    if getattr(row, "project_leave_policy_id", None):
        from models import ProjectLeavePolicy
        pol = db.get(ProjectLeavePolicy, row.project_leave_policy_id)
        if pol is not None:
            return pol
    return (
        db.get(CustomerLeavePolicy, row.customer_leave_policy_id)
        if row.customer_leave_policy_id else None
    )


def accrual_start(
    policy: CustomerLeavePolicy | None,
    pe: ProjectEmployee | None,
) -> date | None:
    """Earliest date this leave type may accrue for the PE.

    ``max(policy.effective_date, pe.onboarding_date)`` over whichever side is
    set. Missing values impose no lower bound on that side. ``None`` means no
    lower bound at all.
    """
    dates: list[date] = []
    if policy is not None and getattr(policy, "effective_date", None) is not None:
        dates.append(policy.effective_date)
    if pe is not None and getattr(pe, "onboarding_date", None) is not None:
        dates.append(pe.onboarding_date)
    return max(dates) if dates else None


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _period_key(as_of: date) -> str:
    return f"{as_of.year:04d}-{as_of.month:02d}"


def _credit_source(pe_id: int, leave_type_id: int, period: str) -> str:
    return f"pe_credit:{pe_id}:{leave_type_id}:{period}"


def _already_credited(db: Session, pe_id: int, leave_type_id: int, period: str) -> bool:
    src = _credit_source(pe_id, leave_type_id, period)
    row = db.execute(
        select(LeaveAccrualEvent.id).where(LeaveAccrualEvent.source == src).limit(1)
    ).first()
    return row is not None


def _policy_period_amount(policy: CustomerLeavePolicy) -> Decimal:
    """The per-cycle accrual amount implied by a customer leave policy (live).

    Monthly    -> leave_credit_balance per month
    Quarterly  -> leave_credit_balance / 4
    Yearly     -> leave_credit_balance / 12 only when credited End_Of_Period
    One_Time   -> 0 (seeded upfront, never accrues per cycle)
    """
    bal = Decimal(policy.leave_credit_balance or 0)
    credit_type = (policy.leave_credit_type or "Monthly")
    timing = (policy.leave_credit_timing or "Start_Of_Period")
    if credit_type == "One_Time":
        return Decimal("0")
    if credit_type == "Yearly":
        return (bal / Decimal(12)).quantize(Decimal("0.01")) if (bal and timing == "End_Of_Period") else Decimal("0")
    if credit_type == "Quarterly":
        return (bal / Decimal(4)).quantize(Decimal("0.01")) if bal else Decimal("0")
    return bal  # Monthly


def _is_upfront_credit(policy: CustomerLeavePolicy | None, credit_type: str) -> bool:
    timing = (policy.leave_credit_timing if policy else "Start_Of_Period") or "Start_Of_Period"
    return credit_type == "One_Time" or (credit_type == "Yearly" and timing != "End_Of_Period")


def _days_present_in_month(
    pe: ProjectEmployee,
    as_of: date,
    *,
    not_before: date | None = None,
) -> tuple[int, int]:
    """Days the PE is present in the month of ``as_of``, clamped by exit and lower bound.

    ``not_before`` is typically ``accrual_start(policy, pe)``. When omitted, falls
    back to ``pe.onboarding_date`` only (legacy behaviour).
    """
    dim = _days_in_month(as_of.year, as_of.month)
    start = date(as_of.year, as_of.month, 1)
    end = date(as_of.year, as_of.month, dim)
    lower = not_before if not_before is not None else pe.onboarding_date
    if lower and lower > end:
        return 0, dim
    if pe.exit_date and pe.exit_date < start:
        return 0, dim
    present_start = max(start, lower or start)
    present_end = min(end, pe.exit_date or end)
    if present_end < present_start:
        return 0, dim
    return (present_end - present_start).days + 1, dim


def _credit_deferred_upfront(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    policy: CustomerLeavePolicy,
    as_of: date,
    period: str,
    start: date | None,
) -> Decimal:
    """Grant One_Time / Yearly-start amount stashed on leave_accrual after accrual_start."""
    pending = Decimal(row.leave_accrual or 0)
    if pending <= 0:
        return ZERO
    dim = _days_in_month(as_of.year, as_of.month)
    month_end = date(as_of.year, as_of.month, dim)
    if start is not None and start > month_end:
        return ZERO

    amount = pending
    # getattr: ProjectLeavePolicy has no prorate/max_limit — defaults apply.
    if getattr(policy, "prorate_balance_credit", False) and start is not None:
        factor = Decimal(13 - start.month) / Decimal(12)
        factor = min(max(factor, Decimal("0")), Decimal("1"))
        amount = (amount * factor).quantize(Decimal("0.01"))

    _max_limit = getattr(policy, "max_limit", None)
    if policy.is_max_limit and _max_limit is not None:
        room = Decimal(_max_limit) - Decimal(row.leave_balance or 0)
        if room <= 0:
            return ZERO
        amount = min(amount, room)

    if amount <= 0:
        row.leave_accrual = ZERO
        return ZERO

    row.leave_balance = Decimal(row.leave_balance or 0) + amount
    row.leave_accrual = ZERO
    db.add(LeaveAccrualEvent(
        employee_id=pe.employee_id,
        leave_type_id=row.leave_type_id,
        event_type="Accrual",
        amount=amount,
        balance_after=row.leave_balance,
        source=_credit_source(pe.id, row.leave_type_id, period),
        note=f"PE deferred upfront credit {period}",
    ))
    return amount


def _apply_cycle_expiry(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    policy: CustomerLeavePolicy,
    as_of: date,
    period: str,
    start: date | None,
) -> Decimal:
    """Cycle expiry for Monthly/Quarterly `leave_expire` policies.

    "Use it in the cycle or lose it": before this period's credit is applied,
    the remainder carried in from PRIOR periods expires to zero (ledgered as a
    negative Adjustment, idempotent via `pe_cycle_expire:{pe}:{type}:{period}`).
    Monthly expires at every month rollover; Quarterly at quarter starts
    (Jan/Apr/Jul/Oct). Yearly keeps the existing Dec-31 `apply_year_end_carry`
    path. The accrual-start period itself never expires (opening/seed balances
    granted that month must survive their own month).
    """
    cycle = str(policy.leave_expire or "").strip().lower()
    if cycle.startswith("month"):
        pass
    elif cycle.startswith("quarter"):
        if as_of.month not in (1, 4, 7, 10):
            return ZERO
    else:
        return ZERO  # Yearly / blank → Dec-31 job handles it
    if start is not None and (start.year, start.month) == (as_of.year, as_of.month):
        return ZERO
    remaining = Decimal(row.leave_balance or 0)
    if remaining <= 0:
        return ZERO
    src = f"pe_cycle_expire:{pe.id}:{row.leave_type_id}:{period}"
    already = db.execute(
        select(LeaveAccrualEvent.id).where(LeaveAccrualEvent.source == src).limit(1)
    ).first()
    if already is not None:
        return ZERO
    row.leave_balance = ZERO
    db.add(LeaveAccrualEvent(
        employee_id=pe.employee_id,
        leave_type_id=row.leave_type_id,
        event_type="Adjustment",
        amount=-remaining,
        balance_after=ZERO,
        source=src,
        note=f"{policy.leave_expire} leave expiry before {period} credit",
    ))
    return remaining


def credit_one_pe_leave_row(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    as_of: date,
    *,
    force: bool = False,
) -> Decimal:
    """Credit one leave-detail row for the month of `as_of`. Return amount credited."""
    if pe.is_exit or not pe.is_active:
        return ZERO
    period = _period_key(as_of)
    if not force and _already_credited(db, pe.id, row.leave_type_id, period):
        return ZERO

    policy = _row_policy(db, row)
    start = accrual_start(policy, pe)
    credit_type = (policy.leave_credit_type if policy else "Monthly") or "Monthly"

    # Monthly/Quarterly expiry cycles: expire the prior remainder BEFORE this
    # period's credit so each cycle starts from only its own grant.
    if policy is not None:
        _apply_cycle_expiry(db, pe, row, policy, as_of, period, start)

    # Deferred One_Time / Yearly-start: leave_accrual holds the pending grant.
    if policy is not None and _is_upfront_credit(policy, credit_type):
        return _credit_deferred_upfront(db, pe, row, policy, as_of, period, start)

    # Seed-by-copy governs the OPENING balance; the recurring per-period accrual
    # rate follows the live customer policy so a mid-engagement rate change takes
    # effect from the next credit cycle (never retroactively rewriting balances).
    accrual = _policy_period_amount(policy) if policy is not None else Decimal(row.leave_accrual or 0)
    if accrual <= 0:
        return ZERO

    if credit_type == "Yearly" and (policy is None or (policy.leave_credit_timing or "") != "End_Of_Period"):
        return ZERO

    days_present, dim = _days_present_in_month(pe, as_of, not_before=start)
    do_prorate = bool(policy and getattr(policy, "prorate_balance_credit", False))
    amount = prorate_credit(accrual, days_present, dim) if do_prorate else accrual
    if days_present <= 0:
        amount = ZERO
    if amount <= 0:
        return ZERO

    _max_limit = getattr(policy, "max_limit", None) if policy else None
    if policy and policy.is_max_limit and _max_limit is not None:
        room = Decimal(_max_limit) - Decimal(row.leave_balance or 0)
        if room <= 0:
            return ZERO
        amount = min(amount, room)

    row.leave_balance = Decimal(row.leave_balance or 0) + amount
    db.add(LeaveAccrualEvent(
        employee_id=pe.employee_id,
        leave_type_id=row.leave_type_id,
        event_type="Accrual",
        amount=amount,
        balance_after=row.leave_balance,
        source=_credit_source(pe.id, row.leave_type_id, period),
        note=f"PE monthly credit {period}",
    ))
    return amount


def apply_year_end_carry(
    db: Session,
    pe: ProjectEmployee,
    row: ProjectEmployeeLeaveDetail,
    as_of: date,
) -> tuple[Decimal, Decimal]:
    """On Dec 31 apply carry-forward cap and expire remainder when policy says so."""
    if pe.is_exit or not pe.is_active:
        return ZERO, ZERO
    if as_of.month != 12 or as_of.day != 31:
        return ZERO, ZERO
    policy = _row_policy(db, row)
    remaining = Decimal(row.leave_balance or 0)
    if remaining <= 0 or policy is None:
        return ZERO, ZERO
    expire_enabled = bool(policy.leave_expire)
    has_cap = policy.maximum_carry_forward is not None
    if not expire_enabled and not has_cap:
        return ZERO, ZERO
    max_cf = Decimal(policy.maximum_carry_forward) if has_cap else remaining
    carried, expired = carry_forward(remaining, max_cf)
    if not expire_enabled:
        expired = ZERO
        carried = remaining if remaining <= max_cf else max_cf
        expired = remaining - carried
    row.leave_balance = carried
    period = str(as_of.year)
    if expired > 0:
        db.add(LeaveAccrualEvent(
            employee_id=pe.employee_id,
            leave_type_id=row.leave_type_id,
            event_type="Adjustment",
            amount=-expired,
            balance_after=row.leave_balance,
            source=f"pe_expire:{pe.id}:{row.leave_type_id}:{period}",
            note=f"PE leave expiry {period}",
        ))
    if carried > 0 and (expire_enabled or has_cap):
        db.add(LeaveAccrualEvent(
            employee_id=pe.employee_id,
            leave_type_id=row.leave_type_id,
            event_type="Carry_Forward",
            amount=carried,
            balance_after=row.leave_balance,
            source=f"pe_carry:{pe.id}:{row.leave_type_id}:{period}",
            note=f"PE carry forward into {as_of.year + 1}",
        ))
    return carried, expired


def run_pe_leave_credit(db: Session, as_of: date | None = None, *, pe_id: int | None = None) -> dict:
    """Run period credit (+ Dec 31 carry) for all or one PE. Returns summary counts."""
    as_of = as_of or date.today()
    stmt = select(ProjectEmployee).where(
        ProjectEmployee.is_active.is_(True),
        ProjectEmployee.is_exit.is_(False),
    )
    if pe_id is not None:
        stmt = stmt.where(ProjectEmployee.id == pe_id)
    pes = db.execute(stmt).scalars().all()
    credited = Decimal("0")
    rows_touched = 0
    for pe in pes:
        details = db.execute(
            select(ProjectEmployeeLeaveDetail).where(
                ProjectEmployeeLeaveDetail.project_employee_id == pe.id
            )
        ).scalars().all()
        for row in details:
            amt = credit_one_pe_leave_row(db, pe, row, as_of)
            if amt > 0:
                credited += amt
                rows_touched += 1
            apply_year_end_carry(db, pe, row, as_of)
    db.commit()
    return {
        "as_of": as_of.isoformat(),
        "project_employees": len(pes),
        "rows_credited": rows_touched,
        "total_credited": float(credited),
    }


# --------------------------------------------------------------- gap repair
#
# This job used to be CLI-only, which made a missed month invisible: nobody
# notices an under-credited balance until someone disputes it, and by then the
# month it belonged to is closed. The helpers below let the scheduler answer
# "which months never ran?" and replay them. Replay is safe because every
# credit is keyed `pe_credit:{pe}:{type}:{YYYY-MM}` — a month that DID run
# credits nothing the second time.


def month_end(year: int, month: int) -> date:
    """Last calendar day of the month — the as_of a replayed month must use.

    Month end (not the 1st) matters twice: `_days_present_in_month` prorates
    against it, and 31 December is the only date `apply_year_end_carry` acts
    on, so replaying December this way also replays a missed carry-forward.
    """
    return date(year, month, _days_in_month(year, month))


def _period_of(source: str | None) -> str | None:
    """`pe_credit:12:3:2026-07` -> `2026-07`. Anything else -> None."""
    if not source or not source.startswith("pe_credit:"):
        return None
    tail = source.rsplit(":", 1)[-1]
    return tail if len(tail) == 7 and tail[4] == "-" else None


def _iter_periods(start: date, end: date):
    """Yield (year, month) from start's month through end's month inclusive."""
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def earliest_accrual_start(db: Session) -> date | None:
    """Earliest date any active PE could have started accruing. None = nothing to do."""
    pes = db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.is_active.is_(True),
            ProjectEmployee.is_exit.is_(False),
        )
    ).scalars().all()
    starts: list[date] = []
    for pe in pes:
        rows = db.execute(
            select(ProjectEmployeeLeaveDetail).where(
                ProjectEmployeeLeaveDetail.project_employee_id == pe.id
            )
        ).scalars().all()
        for row in rows:
            start = accrual_start(_row_policy(db, row), pe)
            if start is not None:
                starts.append(start)
    return min(starts) if starts else None


def missing_credit_periods(
    db: Session,
    *,
    as_of: date | None = None,
    lookback_months: int = 12,
) -> list[str]:
    """Closed months (never the current one) with no PE credit ledger row at all.

    Deliberately coarse: one `pe_credit` row anywhere in a month is taken as
    proof the job ran. A finer per-PE check would flag every employee who
    simply had nothing to accrue that month.
    """
    as_of = as_of or date.today()
    start = earliest_accrual_start(db)
    if start is None:
        return []

    # Never look further back than the window — a system that has been off for
    # a year should be repaired deliberately, not by a background job.
    floor_y, floor_m = as_of.year, as_of.month
    for _ in range(max(0, lookback_months)):
        floor_y, floor_m = (floor_y - 1, 12) if floor_m == 1 else (floor_y, floor_m - 1)
    if (start.year, start.month) < (floor_y, floor_m):
        start = date(floor_y, floor_m, 1)

    seen: set[str] = set()
    for (source,) in db.execute(
        select(LeaveAccrualEvent.source).where(LeaveAccrualEvent.source.like("pe_credit:%"))
    ).all():
        period = _period_of(source)
        if period:
            seen.add(period)

    missing: list[str] = []
    for year, month in _iter_periods(start, as_of):
        if (year, month) == (as_of.year, as_of.month):
            continue  # the current month is this run's job, not a gap
        period = f"{year:04d}-{month:02d}"
        if period not in seen:
            missing.append(period)
    return missing


def run_pe_leave_credit_backfill(
    db: Session,
    *,
    periods: list[str],
    pe_id: int | None = None,
) -> dict:
    """Replay `run_pe_leave_credit` at the month end of each `YYYY-MM` given."""
    runs: list[dict] = []
    for period in periods:
        try:
            year, month = int(period[:4]), int(period[5:7])
        except (ValueError, IndexError):
            continue
        runs.append(run_pe_leave_credit(db, as_of=month_end(year, month), pe_id=pe_id))
    return {
        "periods": [r["as_of"][:7] for r in runs],
        "rows_credited": sum(r["rows_credited"] for r in runs),
        "total_credited": sum(r["total_credited"] for r in runs),
        "runs": runs,
    }
