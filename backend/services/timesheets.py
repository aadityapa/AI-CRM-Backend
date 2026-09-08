"""Timesheet service helpers: billable computation, serializers, ownership, summary.

Billable rules are resolved branch-wise: the project's opportunity pins a
customer branch, whose own policy fields (when set) override the customer-level
CustomerBillingPolicy, which in turn overrides built-in defaults. The fallback
is field-by-field — a branch with only some fields set inherits the rest.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from models import (
    USERS_TABLE, AttendanceStatus, BillingUnit, Customer, CustomerBillingPolicy,
    CustomerBranch, DayType, Employee, EmployeeLeaveBalance, EntryLocation, Holiday, Invoice,
    LeaveAccrualEvent, LeavePeriod, LeavePolicyType, Opportunity, Project,
    ProjectEmployee, ProjectEmployeeLeaveDetail, Timesheet, TimesheetAttachment,
    TimesheetEntry, TimesheetStatus,
)
from services.employees import (
    ensure_loss_of_pay_type,
    is_comp_off_name,
    is_loss_of_pay_name,
    resolve_leave_policy_type,
)
from services.tax_invoice import line_description

ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")
TWO = Decimal("2")
EIGHT = Decimal("8")
TWO_PLACES = Decimal("0.01")

STATUS_LABELS = {
    TimesheetStatus.DRAFT.value: "Pending for Submission",
    TimesheetStatus.SUBMITTED.value: "Pending for Approval",
    TimesheetStatus.APPROVED.value: "Approved",
    TimesheetStatus.REJECTED.value: "Rejected",
}


@dataclass
class BillingPolicy:
    """Effective billing policy (defaults when the customer has none)."""

    week_off_billable: bool = False
    leave_billable: bool = False
    holidays_billable: bool = False
    comp_off_billable: bool = False
    min_hours_full_day: Decimal = Decimal("8.00")
    min_hours_half_day: Decimal = Decimal("4.00")
    #: Which weekdays are the week-off (0=Mon..6=Sun). Sat+Sun unless a
    #: policy level overrides it (0072) — Gulf customers run Fri+Sat.
    week_off_days: tuple = (5, 6)


def parse_week_off_days(raw) -> tuple | None:
    """CSV "5,6" -> (5, 6). None/blank/invalid -> None (= inherit).

    Silently dropping bad tokens would turn a typo into a 7-day work week;
    instead ANY invalid token invalidates the whole value so the level is
    skipped and the next one in the chain decides.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part.isdigit() or not 0 <= int(part) <= 6:
            return None
        out.append(int(part))
    return tuple(sorted(set(out))) or None


def _project_branch(db: Session, project: Project) -> CustomerBranch | None:
    """Branch the project bills against: project.branch_id, else opportunity.branch_id.

    Customer-guarded: a branch whose customer_id != project.customer_id is ignored
    (returns None) so display/billing never show another customer's branch.
    """
    import logging
    log = logging.getLogger("karnex.timesheets")

    branch_id = getattr(project, "branch_id", None)
    source = "project" if branch_id is not None else None
    if branch_id is None and project.opportunity_id:
        opp = db.get(Opportunity, project.opportunity_id)
        if opp is not None and opp.branch_id is not None:
            branch_id = opp.branch_id
            source = "opportunity"
    if branch_id is None:
        return None
    branch = db.get(CustomerBranch, branch_id)
    # Defensive: ignore a branch that belongs to another customer (stray opp.branch_id).
    if branch is None:
        return None
    if branch.customer_id != project.customer_id:
        log.warning(
            "project_branch_foreign_customer",
            extra={
                "project_id": project.id,
                "project_customer_id": project.customer_id,
                "branch_id": branch.id,
                "branch_customer_id": branch.customer_id,
                "source": source,
            },
        )
        return None
    return branch


def opportunity_branch_foreign_to_project(db: Session, project: Project) -> bool:
    """True when opportunity.branch_id points at a branch of a different customer."""
    if not project.opportunity_id:
        return False
    opp = db.get(Opportunity, project.opportunity_id)
    if opp is None or opp.branch_id is None:
        return False
    branch = db.get(CustomerBranch, opp.branch_id)
    return branch is not None and branch.customer_id != project.customer_id


def resolve_timesheet_display_branch(
    db: Session,
    project: Project | None,
    *,
    pe: ProjectEmployee | None = None,
) -> CustomerBranch | None:
    """Display/header branch — same resolvers as holidays/billing (customer-guarded)."""
    if project is None:
        return None
    if pe is not None:
        from services.project_employees import pe_effective_branch
        return pe_effective_branch(db, pe)
    return _project_branch(db, project)


def _log_project_opportunity_branch_disagree(
    db: Session,
    project: Project,
) -> None:
    """Flag when opportunity.branch_id and project.branch_id point at different rows."""
    import logging

    log = logging.getLogger("karnex.timesheets")
    proj_bid = getattr(project, "branch_id", None)
    if proj_bid is None or not project.opportunity_id:
        return
    opp = db.get(Opportunity, project.opportunity_id)
    if opp is None or opp.branch_id is None or opp.branch_id == proj_bid:
        return
    log.warning(
        "project_opportunity_branch_disagree",
        extra={
            "project_id": project.id,
            "project_branch_id": proj_bid,
            "opportunity_id": project.opportunity_id,
            "opportunity_branch_id": opp.branch_id,
            "project_customer_id": project.customer_id,
            "opportunity_customer_id": getattr(opp, "customer_id", None),
        },
    )


def _same_customer_opportunity_branch(
    db: Session,
    project: Project,
) -> CustomerBranch | None:
    """Opportunity.branch_id only when it belongs to the project's customer."""
    if not project.opportunity_id:
        return None
    opp = db.get(Opportunity, project.opportunity_id)
    if opp is None or opp.branch_id is None:
        return None
    branch = db.get(CustomerBranch, opp.branch_id)
    if branch is None or branch.customer_id != project.customer_id:
        return None
    return branch


def ensure_project_branch_id(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
) -> CustomerBranch | None:
    """Resolve billing/display branch; lazily set project.branch_id when unset.

    MUST NEVER overwrite a non-null ``project.branch_id``. Backfill preference when
    unset: (1) same-customer ``opportunity.branch_id``, (2) PE/display resolver
    (leave-policy hint / unique calendar / unique customer branch). Never backfill
    from a foreign-customer opportunity branch. Callers that commit later persist.
    """
    import logging

    log = logging.getLogger("karnex.timesheets")
    _log_project_opportunity_branch_disagree(db, project)

    existing_id = getattr(project, "branch_id", None)
    if existing_id is not None:
        # Never overwrite — return the linked same-customer row (or None if orphan).
        existing = db.get(CustomerBranch, existing_id)
        if existing is not None and existing.customer_id == project.customer_id:
            return existing
        log.warning(
            "project_branch_id_orphan_or_foreign",
            extra={
                "project_id": project.id,
                "project_branch_id": existing_id,
                "project_customer_id": project.customer_id,
                "branch_customer_id": getattr(existing, "customer_id", None),
            },
        )
        # Fall through to resolve a replacement for callers, but do not write it
        # over the non-null (possibly stale) FK — operator must clear explicitly.
        return resolve_timesheet_display_branch(db, project, pe=pe)

    # Backfill only when unset.
    source = None
    branch = _same_customer_opportunity_branch(db, project)
    if branch is not None:
        source = "opportunity"
    else:
        branch = resolve_timesheet_display_branch(db, project, pe=pe)
        if branch is not None:
            source = "pe_or_fallback"

    if branch is not None:
        project.branch_id = branch.id
        log.info(
            "project_branch_id_backfill",
            extra={
                "project_id": project.id,
                "branch_id": branch.id,
                "branch_name": branch.branch_name,
                "source": source,
            },
        )
    return branch


def effective_billing_policy(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
    branch: CustomerBranch | None = None,
) -> BillingPolicy:
    """Resolve the effective policy: project override → branch → customer → defaults.

    Branch resolution MUST match timesheet header / holidays:
    ``pe_effective_branch`` when PE-mapped, else ``_project_branch`` (customer-guarded).
    Without this, a null ``project.branch_id`` + foreign opp.branch_id yields no branch
    and falls back to customer/default — so Comp Off Billable ON on BMW Pune is ignored.

    Project hour thresholds and billable flags win when set (not None).
    """
    row = db.execute(
        select(CustomerBillingPolicy).where(CustomerBillingPolicy.customer_id == project.customer_id)
    ).scalars().first()
    base = BillingPolicy()
    if row:
        base = BillingPolicy(
            week_off_billable=bool(row.week_off_billable),
            leave_billable=bool(row.leave_billable),
            holidays_billable=bool(row.holidays_billable),
            comp_off_billable=bool(getattr(row, "comp_off_billable", False)),
            min_hours_full_day=Decimal(row.min_hours_full_day),
            min_hours_half_day=Decimal(row.min_hours_half_day),
            week_off_days=parse_week_off_days(getattr(row, "week_off_days", None)) or (5, 6),
        )
    if branch is None:
        branch = resolve_timesheet_display_branch(db, project, pe=pe)
    if branch is not None:
        base = BillingPolicy(
            week_off_billable=base.week_off_billable if branch.weekoff_billable is None
            else bool(branch.weekoff_billable),
            leave_billable=base.leave_billable if branch.leave_billable is None
            else bool(branch.leave_billable),
            holidays_billable=base.holidays_billable if branch.holidays_billable is None
            else bool(branch.holidays_billable),
            comp_off_billable=base.comp_off_billable if branch.comp_off_billable is None
            else bool(branch.comp_off_billable),
            min_hours_full_day=base.min_hours_full_day if branch.hours_required_full_day is None
            else Decimal(branch.hours_required_full_day),
            min_hours_half_day=base.min_hours_half_day if branch.hours_required_half_day is None
            else Decimal(branch.hours_required_half_day),
            week_off_days=parse_week_off_days(getattr(branch, "week_off_days", None))
            or base.week_off_days,
        )
    # Project overrides (spec §6 / resolve_branch_project_policy order)
    return BillingPolicy(
        week_off_billable=base.week_off_billable if project.weekoff_billable is None
        else bool(project.weekoff_billable),
        leave_billable=base.leave_billable if project.leave_billable is None
        else bool(project.leave_billable),
        holidays_billable=base.holidays_billable if project.holidays_billable is None
        else bool(project.holidays_billable),
        comp_off_billable=base.comp_off_billable if project.comp_off_billable is None
        else bool(project.comp_off_billable),
        min_hours_full_day=base.min_hours_full_day if project.hours_required_full_day is None
        else Decimal(project.hours_required_full_day),
        min_hours_half_day=base.min_hours_half_day if project.hours_required_half_day is None
        else Decimal(project.hours_required_half_day),
        week_off_days=parse_week_off_days(getattr(project, "week_off_days", None))
        or base.week_off_days,
    )


DEFAULT_WORKING_HOURS = Decimal("8")
FOUR = Decimal("4")


def default_hours_worked(
    project: Project | None,
    branch: CustomerBranch | None = None,
) -> Decimal:
    """Default Hours Worked for a Present / generated working day.

    Uses the project-resolved max billable hours per day
    (``Project.max_billable_hours_day`` ← branch ``max_billable_hours_per_day``).
    Falls back to ``DEFAULT_WORKING_HOURS`` (8) when unset.
    """
    from services.branch_policy import resolve_branch_project_policy

    resolved = resolve_branch_project_policy(project, branch)
    if resolved.max_billable_hours_per_day is not None:
        return Decimal(str(resolved.max_billable_hours_per_day))
    return DEFAULT_WORKING_HOURS


def attendance_from_hours_worked(
    hours: Decimal,
    *,
    full_day: Decimal | None = None,
    half_day: Decimal | None = None,
) -> AttendanceStatus:
    """Derive working-day attendance from hours worked.

    Thresholds default to 8 (full) / 4 (half) when not supplied; callers should
    pass the project's resolved policy thresholds when available.
    """
    h = Decimal(hours or 0)
    full = Decimal(full_day) if full_day is not None else EIGHT
    half = Decimal(half_day) if half_day is not None else FOUR
    if h >= full:
        return AttendanceStatus.PRESENT
    if h >= half:
        return AttendanceStatus.HALF_DAY
    return AttendanceStatus.ABSENT


def display_billable_day(billable_hours: Decimal) -> Decimal:
    """Report/display billable day = billable hours ÷ 8 (invoice math stays threshold-based)."""
    bh = Decimal(billable_hours or 0)
    if bh <= ZERO:
        return ZERO
    return (bh / EIGHT).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def period_range_label(year: int, month: int) -> str:
    """Human period range, e.g. "01-Jun-2025 to 30-Jun-2025"."""
    start, end = period_bounds(year, month)
    return f"{start.strftime('%d-%b-%Y')} to {end.strftime('%d-%b-%Y')}"


def pe_period_bounds(year: int, month: int, pe) -> tuple[date, date]:
    """The month clamped to the employee's actual time ON the project.

    A mid-month joiner (onboarded the 10th) owes days from the 10th; a
    mid-month leaver owes days up to the exit date. Generating the full
    calendar month for them padded the sheet with days that never happened —
    and a Monthly rate then billed a full month for half a month's presence.

    Onboarding/exit dates on the Project Employee record are the source of
    truth; edit those (HR) and the NEXT generated sheet follows. Sheets keep
    the days they were generated with.
    """
    start, end = period_bounds(year, month)
    onboarding = getattr(pe, "onboarding_date", None) if pe is not None else None
    if onboarding and onboarding > start:
        start = min(onboarding, end)
    exit_date = getattr(pe, "exit_date", None) if pe is not None else None
    if getattr(pe, "is_exit", False) and exit_date and exit_date < end:
        end = max(exit_date, start)
    return start, end


def pe_period_range_label(year: int, month: int, pe) -> str:
    start, end = pe_period_bounds(year, month, pe)
    return f"{start.strftime('%d-%b-%Y')} to {end.strftime('%d-%b-%Y')}"


def sheet_period_bounds(ts, pe) -> tuple[date, date]:
    """THE period resolver for one sheet: explicit override, else PE window.

    ``timesheets.period_start_date/period_end_date`` (0073) win when set — an
    editor adjusted this one sheet deliberately. Otherwise the window derives
    from the PE's onboarding/exit dates as usual. Overrides are stored clamped
    to the sheet's month, so nothing here can leak into a neighbouring month.
    """
    start, end = pe_period_bounds(ts.year, ts.month, pe)
    o_start = getattr(ts, "period_start_date", None)
    o_end = getattr(ts, "period_end_date", None)
    if o_start:
        start = o_start
    if o_end:
        end = o_end
    if end < start:
        end = start
    return start, end


def classify_calendar_day(
    d: date,
    holiday_dates: set[date],
    *,
    default_hours: Decimal | None = None,
    week_off_days: tuple = (5, 6),
) -> tuple[DayType, bool, AttendanceStatus, Decimal]:
    """Auto-classify a calendar day for timesheet generation.

    ``week_off_days`` comes from the resolved billing policy (0072) so a
    Fri+Sat customer's sheets generate with the right rest days.
    """
    hours = default_hours if default_hours is not None else DEFAULT_WORKING_HOURS
    if d in holiday_dates:
        return DayType.HOLIDAY, False, AttendanceStatus.HOLIDAY, ZERO
    if d.weekday() in (week_off_days or (5, 6)):
        return DayType.WEEK_OFF, False, AttendanceStatus.WEEK_OFF, ZERO
    return DayType.WORKING, True, AttendanceStatus.PRESENT, hours


def build_generated_entry(*, timesheet_id: int, d: date, holiday_dates: set[date],
                          project: Project, policy: BillingPolicy,
                          branch: CustomerBranch | None = None) -> TimesheetEntry:
    day_type, is_working, attendance, hours = classify_calendar_day(
        d, holiday_dates, default_hours=default_hours_worked(project, branch),
        week_off_days=policy.week_off_days,
    )
    billable_hours, billable_days = compute_billables(
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        leave_period=None,
        project=project,
        policy=policy,
    )
    return TimesheetEntry(
        timesheet_id=timesheet_id,
        entry_date=d,
        day_of_week=day_name(d),
        day_type=day_type,
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        location=EntryLocation.ONSITE,
        billable_hours=billable_hours,
        billable_days=billable_days,
        view_flag=False,
    )


def resolve_entry_fields(
    *,
    d: date,
    item,
    holiday_dates: set[date],
    policy: BillingPolicy | None = None,
) -> tuple[DayType, bool, Decimal, AttendanceStatus, str | None, LeavePeriod | None]:
    """Normalize client payload into stored day fields (calendar holidays stay locked)."""
    if d in holiday_dates:
        return DayType.HOLIDAY, False, ZERO, AttendanceStatus.HOLIDAY, None, None

    day_type = item.day_type
    attendance = item.attendance_status
    hours = Decimal(item.hours_worked or 0)
    leave_type = item.leave_type
    leave_period = item.leave_period

    if day_type is None:
        if not item.is_working:
            day_type = DayType.WEEK_OFF
        elif attendance == AttendanceStatus.HOLIDAY:
            day_type = DayType.HOLIDAY
        else:
            day_type = DayType.WORKING

    if day_type == DayType.HOLIDAY and d not in holiday_dates:
        raise HTTPException(
            status_code=400,
            detail=f"{d.isoformat()} is not on the client holiday calendar; "
                   "cannot mark as Holiday manually",
        )

    if day_type == DayType.WEEK_OFF:
        is_working = False
        if attendance not in (AttendanceStatus.LEAVE, AttendanceStatus.ABSENT,
                              AttendanceStatus.HALF_DAY):
            attendance = AttendanceStatus.WEEK_OFF
            leave_type = None
            leave_period = None
    elif day_type == DayType.HOLIDAY:
        is_working = False
        attendance = AttendanceStatus.HOLIDAY
        leave_type = None
        leave_period = None
        hours = ZERO
    else:
        is_working = True
        if attendance in (AttendanceStatus.WEEK_OFF, AttendanceStatus.HOLIDAY):
            attendance = AttendanceStatus.PRESENT

    if attendance == AttendanceStatus.LEAVE:
        if not (leave_type or "").strip():
            leave_type = None
    else:
        leave_type = None
        leave_period = None

    if day_type == DayType.WORKING and is_working and attendance != AttendanceStatus.LEAVE:
        pol = policy or BillingPolicy()
        attendance = attendance_from_hours_worked(
            hours,
            full_day=pol.min_hours_full_day,
            half_day=pol.min_hours_half_day,
        )

    if attendance == AttendanceStatus.HOLIDAY and d not in holiday_dates:
        raise HTTPException(
            status_code=400,
            detail=f"{d.isoformat()} is not on the client holiday calendar; "
                   "cannot mark as Holiday manually",
        )

    return day_type, is_working, hours, attendance, leave_type, leave_period


def _capped_hours(hours: Decimal, project: Project) -> Decimal:
    cap = project.max_billable_hours_day
    if cap is None:
        return hours
    return min(hours, Decimal(cap))


def _days_from_hours(hours: Decimal, policy: BillingPolicy) -> Decimal:
    if hours >= policy.min_hours_full_day:
        return ONE
    if hours >= policy.min_hours_half_day:
        return HALF
    return ZERO


def leave_billable_by_type_map(
    db: Session,
    project: Project,
    *,
    pe: ProjectEmployee | None = None,
) -> dict[str, bool]:
    """Project-scoped leave-type → billable. Same map for every employee on the project.

    Derived from CustomerLeavePolicy rows resolved via the project (customer +
    branch), never from employee_id. Primary signal is ``is_billable``; when
    that is unset, a positive ``leave_credit_balance`` counts as billable.
    Types left out of the map fall back to ``policy.leave_billable`` in
    ``compute_billables``.
    """
    # Lazy import: project_employees imports _project_branch from this module.
    from services.project_employees import resolve_customer_leave_policies

    out: dict[str, bool] = {}
    for pol in resolve_customer_leave_policies(db, project, pe=pe):
        name = (pol.leave_type.name if pol.leave_type else None) or ""
        name = name.strip()
        if not name:
            continue
        if pol.is_billable is not None:
            out[name] = bool(pol.is_billable)
        elif pol.leave_credit_balance is not None and Decimal(pol.leave_credit_balance) > 0:
            out[name] = True
        # else leave unset → compute_billables falls back to policy.leave_billable
    return out


def compute_billables(*, is_working: bool, hours_worked: Decimal,
                      attendance_status: AttendanceStatus,
                      leave_period: LeavePeriod | None,
                      project: Project, policy: BillingPolicy,
                      leave_type: str | None = None,
                      leave_billable_by_type: dict[str, bool] | None = None,
                      paid_leave_days: Decimal | None = None,
                      ) -> tuple[Decimal, Decimal]:
    """Return (billable_hours, billable_days) for one timesheet entry.

    When ``paid_leave_days`` is set for a LEAVE row, only that paid portion is
    billable (excess Loss-of-Pay days contribute 0). Explicit Loss of Pay leave
    type is always non-billable.

    Weekend / holiday *worked* hours (hours > 0) precedence (bill XOR credit):
      1. ``week_off_billable`` / ``holidays_billable`` → bill as normal worked time
      2. else ``comp_off_billable`` → bill as comp-off
      3. else → not billed (comp-off leave credited elsewhere)
    Pure holiday-off (0 hours) still uses ``holidays_billable`` only.
    """
    hours = Decimal(hours_worked or 0)

    if attendance_status == AttendanceStatus.HOLIDAY:
        # DECISION (ISSUE-1): holidays_billable > comp_off_billable > credit.
        if hours > ZERO:
            if policy.holidays_billable or policy.comp_off_billable:
                bh = _capped_hours(hours, project)
                return bh, _days_from_hours(hours, policy)
            return ZERO, ZERO
        # Pure holiday-off: bill a full day when Holidays Billable is ON.
        if policy.holidays_billable:
            return Decimal(policy.min_hours_full_day), ONE
        return ZERO, ZERO

    if not is_working:
        # DECISION (ISSUE-1): week_off_billable > comp_off_billable > credit.
        if hours > ZERO and (policy.week_off_billable or policy.comp_off_billable):
            bh = _capped_hours(hours, project)
            return bh, _days_from_hours(hours, policy)
        # Pure week-off (0 hours): Week Off Billable bills the day itself,
        # exactly as Holidays Billable does for an unworked holiday. This is
        # the calendar-month billing model — with both flags on, a 31-day
        # month bills 31 days, not just the worked ones.
        if policy.week_off_billable:
            return Decimal(policy.min_hours_full_day), ONE
        return ZERO, ZERO

    if attendance_status == AttendanceStatus.PRESENT:
        bh = _capped_hours(hours, project)
        return bh, _days_from_hours(hours, policy)

    if attendance_status == AttendanceStatus.HALF_DAY:
        return _capped_hours(hours, project), HALF

    if attendance_status == AttendanceStatus.LEAVE:
        # Explicit Loss of Pay is always unpaid / non-billable.
        if is_loss_of_pay_name(leave_type):
            return ZERO, ZERO
        # Per-leave-type billability (project Leave Billing Policy) wins when set;
        # otherwise fall back to the flat policy.leave_billable flag.
        # Billable leave pays like a working day: full/half hours AND days so
        # hourly invoices include the paid leave (not days-only).
        per_type = (leave_billable_by_type or {}).get((leave_type or "").strip())
        is_leave_billable = per_type if per_type is not None else policy.leave_billable
        if not is_leave_billable:
            return ZERO, ZERO
        # Paid portion only (LOP excess → 0). When unset, use full leave_period.
        if paid_leave_days is not None:
            paid = Decimal(paid_leave_days or 0)
            if paid <= ZERO:
                return ZERO, ZERO
            if paid <= HALF:
                return Decimal(policy.min_hours_half_day), HALF
            return Decimal(policy.min_hours_full_day), ONE
        if leave_period in (LeavePeriod.HALF_AM, LeavePeriod.HALF_PM):
            return Decimal(policy.min_hours_half_day), HALF
        return Decimal(policy.min_hours_full_day), ONE

    # Absent (or anything else): nothing billable.
    return ZERO, ZERO


def recompute_entry_live(
    e: TimesheetEntry,
    *,
    policy: BillingPolicy,
    project: Project | None,
    leave_billable_by_type: dict[str, bool] | None = None,
    paid_leave_days: Decimal | None = None,
) -> tuple[AttendanceStatus, Decimal, Decimal]:
    """Derive (attendance, billable_hours, billable_days) from CURRENT policy.

    Read-path helper: does not mutate ``e``. Mirrors the save-path rules —
    attendance from hours thresholds for working non-leave days, then
    ``compute_billables`` with the project leave-type map. ``paid_leave_days``
    scales leave billables when the sheet splits paid vs Loss of Pay.
    """
    hours = Decimal(e.hours_worked or 0)
    is_working = bool(e.is_working)
    attendance = e.attendance_status
    leave_type = e.leave_type
    leave_period = e.leave_period

    if is_working and attendance not in (
        AttendanceStatus.LEAVE, AttendanceStatus.HOLIDAY, AttendanceStatus.WEEK_OFF,
    ):
        attendance = attendance_from_hours_worked(
            hours,
            full_day=policy.min_hours_full_day,
            half_day=policy.min_hours_half_day,
        )

    proj = project if project is not None else SimpleNamespace(max_billable_hours_day=None)
    bh, bd = compute_billables(
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        leave_period=leave_period,
        project=proj,  # type: ignore[arg-type]
        policy=policy,
        leave_type=leave_type,
        leave_billable_by_type=leave_billable_by_type,
        paid_leave_days=paid_leave_days,
    )
    return attendance, bh, bd


def _pe_for_timesheet(db: Session, ts: Timesheet) -> ProjectEmployee | None:
    if not ts.project_employee_id:
        return None
    return db.get(ProjectEmployee, ts.project_employee_id)


def live_entries_from_policy(
    db: Session,
    ts: Timesheet,
    entries: list[TimesheetEntry],
) -> tuple[Project | None, BillingPolicy, dict[str, bool], list]:
    """Resolve current project policy and return ephemeral entry views with live billables.

    Used by GET detail/summary/invoice-preview so policy changes reflect without re-save.
    Does not write to the DB. Leave rows use paid-vs-LOP classification so excess
    days are non-billable.
    """
    project = db.get(Project, ts.project_id)
    pe = _pe_for_timesheet(db, ts)
    if project is not None:
        # Align project.branch_id with PE/display resolver when legacy null.
        ensure_project_branch_id(db, project, pe=pe)
    policy = effective_billing_policy(db, project, pe=pe) if project else BillingPolicy()
    leave_map = leave_billable_by_type_map(db, project, pe=pe) if project else {}
    classification = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    # CURRENT holiday calendar for this period — so holiday additions/removals
    # reflect on read without re-saving the sheet (matches how holiday_dates is
    # served to the UI). A day whose holiday status changed is recomputed here
    # so both display day_type AND billables stay correct.
    current_holidays = (
        set(holidays_for_project_period(db, project, ts.year, ts.month)) if project else set()
    )
    live: list = []
    for e in entries:
        split = classification.splits_by_date.get(e.entry_date)
        paid = split.paid_days if split is not None else None

        # Reconcile the stored calendar nature with the CURRENT holiday set,
        # but never touch employee-applied Leave or Week-Off rows.
        eff = e
        eff_day_type = e.day_type
        eff_is_working = bool(e.is_working)
        if e.leave_type is None and e.day_type != DayType.WEEK_OFF:
            now_holiday = e.entry_date in current_holidays
            was_holiday = (
                e.day_type == DayType.HOLIDAY or e.attendance_status == AttendanceStatus.HOLIDAY
            )
            if now_holiday and not was_holiday:
                eff_day_type, eff_is_working = DayType.HOLIDAY, False
                eff = SimpleNamespace(
                    hours_worked=e.hours_worked, is_working=False,
                    attendance_status=AttendanceStatus.HOLIDAY,
                    leave_type=None, leave_period=None, day_type=DayType.HOLIDAY,
                    entry_date=e.entry_date, location=e.location,
                )
            elif was_holiday and not now_holiday:
                # No longer a holiday → treat as a normal working day; attendance
                # is re-derived from hours by recompute_entry_live below.
                eff_day_type, eff_is_working = DayType.WORKING, True
                eff = SimpleNamespace(
                    hours_worked=e.hours_worked, is_working=True,
                    attendance_status=AttendanceStatus.PRESENT,
                    leave_type=None, leave_period=None, day_type=DayType.WORKING,
                    entry_date=e.entry_date, location=e.location,
                )

        att, bh, bd = recompute_entry_live(
            eff, policy=policy, project=project, leave_billable_by_type=leave_map,
            paid_leave_days=paid,
        )
        live.append(SimpleNamespace(
            id=e.id,
            timesheet_id=e.timesheet_id,
            entry_date=e.entry_date,
            day_of_week=e.day_of_week,
            day_type=eff_day_type,
            is_working=eff_is_working,
            hours_worked=e.hours_worked,
            attendance_status=att,
            leave_type=e.leave_type,
            leave_period=e.leave_period,
            billable_hours=bh,
            billable_days=bd,
            location=e.location,
            view_flag=e.view_flag,
            entry_project_id=e.entry_project_id,
            paid_leave_days=float(split.paid_days) if split else None,
            lop_leave_days=float(split.lop_days) if split else None,
        ))
    return project, policy, leave_map, live


def persist_recomputed_entries(
    db: Session,
    ts: Timesheet,
    entries: list[TimesheetEntry],
) -> None:
    """Recompute and write attendance/billables from the CURRENT project policy.

    Used on submit (and may be reused by save) so finalized sheets match policy.
    Applies paid-vs-LOP classification so excess leave days store as non-billable.
    """
    project = db.get(Project, ts.project_id)
    pe = _pe_for_timesheet(db, ts)
    if project is not None:
        ensure_project_branch_id(db, project, pe=pe)
    policy = effective_billing_policy(db, project, pe=pe) if project else BillingPolicy()
    leave_map = leave_billable_by_type_map(db, project, pe=pe) if project else {}
    classification = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    for e in entries:
        split = classification.splits_by_date.get(e.entry_date)
        paid = split.paid_days if split is not None else None
        att, bh, bd = recompute_entry_live(
            e, policy=policy, project=project, leave_billable_by_type=leave_map,
            paid_leave_days=paid,
        )
        e.attendance_status = att
        e.billable_hours = bh
        e.billable_days = bd


def timesheet_leave_balances_map(
    db: Session, ts: Timesheet,
) -> dict[str, float]:
    """Leave-type name → remaining balance for the Leave Balance column.

    PE-mapped timesheets use ``project_employee_leave_details`` (same pool
    ``consume_timesheet_leaves`` debits on submit/approve); otherwise employee
    yearly balances. All balances (incl. Comp-Off) are clamped at >= 0 for display.
    """
    out: dict[str, float] = {}
    if ts.project_employee_id is not None:
        rows = db.execute(
            select(ProjectEmployeeLeaveDetail, LeavePolicyType.name)
            .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id)
            .where(ProjectEmployeeLeaveDetail.project_employee_id == ts.project_employee_id)
        ).all()
        for row, name in rows:
            if name:
                out[str(name)] = max(0.0, float(row.leave_balance or 0))
        return out
    rows = db.execute(
        select(EmployeeLeaveBalance, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == EmployeeLeaveBalance.leave_type_id)
        .where(
            EmployeeLeaveBalance.employee_id == ts.employee_id,
            EmployeeLeaveBalance.year == ts.year,
        )
    ).all()
    for row, name in rows:
        if name:
            out[str(name)] = max(0.0, float(row.balance or 0))
    return out


@dataclass
class LeaveDaySplit:
    """Paid vs Loss-of-Pay split for one LEAVE entry."""

    entry_date: date
    leave_type_id: int
    leave_type_name: str
    req_days: Decimal
    paid_days: Decimal
    lop_days: Decimal
    is_comp_off: bool
    is_explicit_lop: bool


@dataclass
class TimesheetLeaveClassification:
    """Sheet-level paid vs LOP classification (date order, per leave type)."""

    splits_by_date: dict[date, LeaveDaySplit] = field(default_factory=dict)
    paid_required_by_type_id: dict[int, Decimal] = field(default_factory=dict)
    lop_days_total: Decimal = ZERO
    lop_type_id: int | None = None


def _entry_leave_days(e) -> Decimal:
    period = getattr(e, "leave_period", None)
    period_val = getattr(period, "value", period)
    if period_val in (LeavePeriod.HALF_AM, LeavePeriod.HALF_PM, "Half_AM", "Half_PM"):
        return HALF
    return ONE


def _is_leave_attendance(status) -> bool:
    val = getattr(status, "value", status)
    return val == AttendanceStatus.LEAVE or val == "Leave"


def _is_working_day_type(day_type) -> bool:
    val = getattr(day_type, "value", day_type)
    return val == DayType.WORKING or val == "Working"


def _attendance_reporting_lop(entries) -> tuple[Decimal, Decimal]:
    """Reporting-only LOP from Absent (1.0) and Half_Day (0.5) on Working days.

    Does not affect compute_billables / billed amounts — visibility only.
    """
    lop_absent = ZERO
    lop_half = ZERO
    for e in entries:
        if not _is_working_day_type(getattr(e, "day_type", None)):
            continue
        status = getattr(e, "attendance_status", None)
        status_val = getattr(status, "value", status)
        if status_val in (AttendanceStatus.ABSENT, "Absent"):
            lop_absent += ONE
        elif status_val in (AttendanceStatus.HALF_DAY, "Half_Day"):
            lop_half += HALF
    return lop_absent, lop_half


def _timesheet_prior_consumption(db: Session, ts: Timesheet) -> dict[int, Decimal]:
    """leave_type_id → days already consumed by ledger source timesheet:{id}."""
    prior: dict[int, Decimal] = {}
    for ev in db.execute(
        select(LeaveAccrualEvent).where(
            LeaveAccrualEvent.source == f"timesheet:{ts.id}",
            LeaveAccrualEvent.event_type == "Consumption",
        )
    ).scalars().all():
        prior[ev.leave_type_id] = prior.get(ev.leave_type_id, ZERO) - Decimal(ev.amount or 0)
    return prior


def _supports_row_lock(db: Session) -> bool:
    """Postgres supports SELECT … FOR UPDATE; SQLite tests skip the lock."""
    try:
        name = db.get_bind().dialect.name
        return isinstance(name, str) and name != "sqlite"
    except Exception:
        return False


def _available_balances_before_timesheet(
    db: Session, ts: Timesheet, *, for_update: bool = False,
) -> dict[int, Decimal]:
    """Balance by leave_type_id BEFORE this timesheet's own ledger consumption.

    DECISION (ISSUE-5): when ``for_update`` (commit path), lock PE leave detail /
    EmployeeLeaveBalance rows with ``FOR UPDATE`` on Postgres so concurrent
    submit/approve of two sheets cannot over-consume the same balance.
    """
    prior = _timesheet_prior_consumption(db, ts)
    balances: dict[int, Decimal] = {}
    lock = for_update and _supports_row_lock(db)
    if ts.project_employee_id is not None:
        stmt = select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == ts.project_employee_id
        )
        if lock:
            stmt = stmt.with_for_update()
        rows = db.execute(stmt).scalars().all()
        for row in rows:
            balances[row.leave_type_id] = (
                Decimal(row.leave_balance or 0) + prior.get(row.leave_type_id, ZERO)
            )
    else:
        stmt = select(EmployeeLeaveBalance).where(
            EmployeeLeaveBalance.employee_id == ts.employee_id,
            EmployeeLeaveBalance.year == ts.year,
        )
        if lock:
            stmt = stmt.with_for_update()
        rows = db.execute(stmt).scalars().all()
        for row in rows:
            balances[row.leave_type_id] = (
                Decimal(row.balance or 0) + prior.get(row.leave_type_id, ZERO)
            )
    for tid, consumed in prior.items():
        if tid not in balances:
            balances[tid] = consumed
    return balances


def classify_timesheet_leave_paid_vs_lop(
    db: Session,
    ts: Timesheet,
    entries: list[TimesheetEntry] | list,
    *,
    for_update: bool = False,
) -> TimesheetLeaveClassification:
    """Split each leave day into paid vs Loss-of-Pay (date order, per type).

    AVAIL = max(paid_balance, 0) before this sheet's consumption (prior
    timesheet:{id} ledger rows are added back). PAID = min(REQ, AVAIL);
    EXCESS → Loss of Pay. Comp-Off uses the same cap (no overdraft); floor
    running balance at 0. Explicit Loss of Pay entries are all LOP.

    Pass ``for_update=True`` on commit paths (consume) to row-lock balances.
    """
    running = _available_balances_before_timesheet(db, ts, for_update=for_update)
    type_cache: dict[str, LeavePolicyType] = {}
    leave_rows: list[tuple] = []
    for e in entries:
        if not _is_leave_attendance(getattr(e, "attendance_status", None)):
            continue
        name = (getattr(e, "leave_type", None) or "").strip()
        if not name:
            continue
        key = name.lower()
        lt = type_cache.get(key)
        if lt is None:
            lt = resolve_leave_policy_type(db, name)
            if lt is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown leave type '{name}' — cannot classify leave",
                )
            type_cache[key] = lt
        leave_rows.append((e, lt))

    leave_rows.sort(key=lambda pair: getattr(pair[0], "entry_date", date.min) or date.min)

    # Lookup only (no create) on read paths; consume ensures the type exists.
    lop_type = db.execute(
        select(LeavePolicyType).where(
            func.lower(LeavePolicyType.name).in_(("loss of pay", "loss off pay", "lop"))
        )
    ).scalars().first()
    result = TimesheetLeaveClassification(
        lop_type_id=lop_type.id if lop_type else None,
    )
    paid_by_type: dict[int, Decimal] = {}
    lop_total = ZERO

    for e, lt in leave_rows:
        req = _entry_leave_days(e)
        name = lt.name or ""
        is_comp = is_comp_off_name(name)
        is_lop = is_loss_of_pay_name(name)

        # DECISION (ISSUE-3): Comp-Off is capped like other paid types —
        # PAID = min(REQ, max(AVAIL, 0)); excess → LOP; never allow negative
        # leave-application overdraft. Accrue reversal still uses allow_negative.
        if is_lop:
            paid, lop = ZERO, req
            lop_total += req
        else:
            avail = max(running.get(lt.id, ZERO), ZERO)
            paid = min(req, avail)
            lop = req - paid
            running[lt.id] = avail - paid
            if paid > ZERO:
                paid_by_type[lt.id] = paid_by_type.get(lt.id, ZERO) + paid
            lop_total += lop

        entry_date = getattr(e, "entry_date", None)
        if entry_date is not None:
            result.splits_by_date[entry_date] = LeaveDaySplit(
                entry_date=entry_date,
                leave_type_id=lt.id,
                leave_type_name=name,
                req_days=req,
                paid_days=paid,
                lop_days=lop,
                is_comp_off=is_comp,
                is_explicit_lop=is_lop,
            )

    result.paid_required_by_type_id = paid_by_type
    result.lop_days_total = lop_total
    return result


def day_name(d: date) -> str:
    return d.strftime("%A")


def month_days(year: int, month: int) -> list[date]:
    _, last = calendar.monthrange(year, month)
    return [date(year, month, day) for day in range(1, last + 1)]


def holidays_for_project_period(db: Session, project: Project,
                                year: int, month: int) -> set[date]:
    """Active holiday dates applying to this project during one month.

    A holiday row matches when its customer is NULL (global) or equals the
    project's customer, AND its branch is NULL (customer-wide) or equals the
    branch resolved for the project (opportunity → branch, with customer-branch
    fallbacks aligned with PE holiday calendars).
    """
    from services.project_employees import effective_customer_branch_for_project

    start, end = period_bounds(year, month)
    branch = effective_customer_branch_for_project(db, project, year=year)
    rows = db.execute(
        select(Holiday).where(
            Holiday.is_active.is_(True),
            Holiday.observance == "Mandatory",
            Holiday.holiday_date >= start,
            Holiday.holiday_date <= end,
            or_(Holiday.customer_id.is_(None), Holiday.customer_id == project.customer_id),
        )
    ).scalars().all()

    calendar_ids: set[int] = set()
    if branch is not None:
        from models import BranchHolidayYear
        calendar_ids = set(db.execute(
            select(BranchHolidayYear.id).where(
                BranchHolidayYear.branch_id == branch.id,
                BranchHolidayYear.calendar_year == year,
            )
        ).scalars().all())

    dates: set[date] = set()
    for h in rows:
        if h.customer_id is None:
            dates.add(h.holiday_date)
            continue
        if h.branch_id is None and not h.holiday_calendar_id:
            dates.add(h.holiday_date)
            continue
        if branch is None:
            continue
        if h.branch_id == branch.id or (h.holiday_calendar_id and h.holiday_calendar_id in calendar_ids):
            dates.add(h.holiday_date)
    return dates


def get_timesheet_or_404(db: Session, timesheet_id: int) -> Timesheet:
    ts = db.get(Timesheet, timesheet_id)
    if not ts:
        raise HTTPException(status_code=404, detail="Timesheet not found")
    return ts


def employee_for_user(db: Session, user_id: int) -> Employee | None:
    return db.execute(select(Employee).where(Employee.user_id == user_id)).scalars().first()


def _num(v):
    return float(v) if v is not None else None


def entry_out(e: TimesheetEntry) -> dict:
    bh = Decimal(e.billable_hours or 0)
    return {
        "id": e.id,
        "timesheet_id": e.timesheet_id,
        "entry_date": e.entry_date.isoformat() if e.entry_date else None,
        "day_of_week": e.day_of_week,
        "day_type": getattr(e.day_type, "value", e.day_type),
        "is_working": e.is_working,
        "hours_worked": _num(e.hours_worked),
        "attendance_status": getattr(e.attendance_status, "value", e.attendance_status),
        "leave_type": e.leave_type,
        "leave_period": getattr(e.leave_period, "value", e.leave_period) if e.leave_period else None,
        "leave_reason": getattr(e, "leave_reason", None) or None,
        "billable_hours": _num(e.billable_hours),
        "billable_days": _num(e.billable_days),
        "billable_day": float(display_billable_day(bh)),
        "location": getattr(e.location, "value", e.location) if e.location else None,
        "view_flag": bool(e.view_flag),
        "entry_project_id": e.entry_project_id,
    }


def timesheet_status_label(status: TimesheetStatus | str, *, due_missing: bool = False) -> str:
    if due_missing:
        return "Due"
    val = getattr(status, "value", status)
    return STATUS_LABELS.get(val, str(val))


def customer_short_name(customer: Customer | None) -> str:
    if customer is None or not (customer.name or "").strip():
        return "Unknown"
    return customer.name.strip().split()[0]


def assignment_role_title(pe: ProjectEmployee | None, project: Project | None) -> str:
    if pe is not None and (pe.role_title or "").strip():
        return pe.role_title.strip()
    return project.name if project and project.name else "Project"


def project_title_label(*, customer: Customer | None, employee: Employee | None,
                        pe: ProjectEmployee | None, project: Project | None) -> str:
    role = assignment_role_title(pe, project)
    first = (employee.first_name or "").strip() if employee else "Employee"
    return f"{customer_short_name(customer)}_{first}__{role}"


def _report_date(ts: Timesheet) -> str | None:
    dt = ts.created_at or ts.submitted_at
    return dt.isoformat() if dt else None


def attachment_out(a: TimesheetAttachment) -> dict:
    return {
        "id": a.id,
        "timesheet_id": a.timesheet_id,
        "file_url": a.file_url,
        "file_name": a.file_name,
        "file_sha256": a.file_sha256,
        "file_size": a.file_size,
        "kind": a.kind,
        "uploaded_by": a.uploaded_by,
        "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
    }


def list_attachments(db: Session, ts: Timesheet) -> list[dict]:
    rows = db.execute(
        select(TimesheetAttachment).where(TimesheetAttachment.timesheet_id == ts.id)
        .order_by(TimesheetAttachment.uploaded_at, TimesheetAttachment.id)
    ).scalars().all()
    if rows:
        return [attachment_out(a) for a in rows]
    if ts.file_attachment_url:
        return [{
            "id": None,
            "timesheet_id": ts.id,
            "file_url": ts.file_attachment_url,
            "file_name": "legacy-attachment",
            "file_sha256": None,
            "file_size": None,
            "kind": "legacy",
            "uploaded_by": None,
            "uploaded_at": ts.submitted_at.isoformat() if ts.submitted_at else None,
        }]
    return []


def timesheet_out(ts: Timesheet) -> dict:
    return {
        "id": ts.id,
        "project_id": ts.project_id,
        "employee_id": ts.employee_id,
        "project_employee_id": ts.project_employee_id,
        "month": ts.month,
        "year": ts.year,
        "status": getattr(ts.status, "value", ts.status),
        "status_label": timesheet_status_label(ts.status),
        "rejection_reason": ts.rejection_reason,
        "reason_for_rejection": ts.rejection_reason,
        "submitted_at": ts.submitted_at.isoformat() if ts.submitted_at else None,
        "approved_by": ts.approved_by,
        "approved_at": ts.approved_at.isoformat() if ts.approved_at else None,
        "file_attachment_url": ts.file_attachment_url,
        "created_at": ts.created_at.isoformat() if getattr(ts, "created_at", None) else None,
        "date": _report_date(ts),
    }


def period_label(year: int, month: int) -> str:
    """Human period label for a timesheet, e.g. "July 2026"."""
    return f"{calendar.month_name[month]} {year}"


def period_bounds(year: int, month: int) -> tuple[date, date]:
    """(first day, last day) of the timesheet month."""
    _, last = calendar.monthrange(year, month)
    return date(year, month, 1), date(year, month, last)


def employee_display_name(emp: Employee | None) -> str | None:
    if emp is None:
        return None
    return " ".join(p for p in (emp.first_name, emp.last_name) if p) or None


def _user_display_name(db: Session, user_id: int | None) -> str | None:
    """Display name of an auth user (registration_data): full_name, else username.

    Never raises: this decorates summaries that sit on the BILLING path (the
    approval freeze snapshots the summary), and a missing legacy users table
    or column must cost a name, not an invoice figure.
    """
    if not user_id:
        return None
    try:
        row = db.execute(
            text(f"SELECT full_name, username FROM {USERS_TABLE} WHERE id = :i"),
            {"i": user_id},
        ).first()
    except Exception:
        return None
    if not row:
        return None
    return row[0] or row[1]


def timesheet_detail_out(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> dict:
    data = timesheet_out(ts)
    project = db.get(Project, ts.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    pe = (
        db.get(ProjectEmployee, ts.project_employee_id)
        if ts.project_employee_id
        else None
    )
    # Header BRANCH must match billing/holidays (customer-guarded). Never use raw
    # opportunity.branch_id — it can point at another customer's branch.
    if project is not None:
        branch = ensure_project_branch_id(db, project, pe=pe)
    else:
        branch = None
    foreign_opp_branch = (
        opportunity_branch_foreign_to_project(db, project) if project else False
    )
    emp = db.get(Employee, ts.employee_id)
    # Period shown = editable override when set, else the employee's actual
    # window on the project (mid-month join/exit aware).
    start, end = sheet_period_bounds(ts, pe)
    # Header/display enrichment (all null-safe). Note: employees has no
    # employee_code column, so the employee's primary key doubles as the code.
    data["project_title"] = project_title_label(customer=customer, employee=emp,
                                                pe=pe, project=project)
    data["customer_id"] = customer.id if customer else None
    data["customer_name"] = customer.name if customer else None
    data["branch_id"] = branch.id if branch else None
    data["branch_name"] = branch.branch_name if branch else None
    # Only flag when opp points at a foreign-customer branch and we refuse to show it.
    data["branch_unlinked"] = bool(branch is None and foreign_opp_branch)
    data["branch_link_message"] = (
        "Branch not linked to this customer" if data["branch_unlinked"] else None
    )
    data["project_type"] = getattr(opp.opp_type, "value", opp.opp_type) if opp else None
    data["timesheet_period"] = f"{start.strftime('%d-%b-%Y')} to {end.strftime('%d-%b-%Y')}"
    data["period_start_date"] = start.isoformat()
    data["period_end_date"] = end.isoformat()
    data["employee_name"] = employee_display_name(emp)
    data["employee_code"] = emp.id if emp else None
    # Live recompute from CURRENT project policy (no DB write).
    _proj, policy, leave_map, live = live_entries_from_policy(db, ts, entries)
    data["billing_policy"] = {
        "week_off_billable": policy.week_off_billable,
        "leave_billable": policy.leave_billable,
        "holidays_billable": policy.holidays_billable,
        "comp_off_billable": policy.comp_off_billable,
        "min_hours_full_day": float(policy.min_hours_full_day),
        "min_hours_half_day": float(policy.min_hours_half_day),
    }
    data["leave_billable_by_type"] = leave_map
    data["leave_balances_by_type"] = timesheet_leave_balances_map(db, ts)
    # Project → branch resolved max billable hrs/day (same source as Hours Worked default).
    if project:
        from services.branch_policy import resolve_branch_project_policy
        resolved_caps = resolve_branch_project_policy(project, branch)
        data["max_billable_hours_day"] = (
            float(resolved_caps.max_billable_hours_per_day)
            if resolved_caps.max_billable_hours_per_day is not None
            else None
        )
    else:
        data["max_billable_hours_day"] = None
    data["holiday_dates"] = sorted(
        d.isoformat() for d in holidays_for_project_period(db, project, ts.year, ts.month)
    ) if project else []
    data["entries"] = [entry_out(e) for e in live]  # type: ignore[arg-type]
    data["summary"] = timesheet_summary(db, ts, entries)
    data["attachments"] = list_attachments(db, ts)
    # The linked invoice, so the detail page can say WHY an approved sheet
    # can no longer be rejected (3 Sep 2026: the button was offered, the
    # server 409'd, and the message never reached the screen).
    invoice = linked_invoice_for(db, ts)
    data["invoice"] = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "payment_status": getattr(invoice.payment_status, "value", invoice.payment_status),
    } if invoice else None
    data["can_generate_invoice"] = ts.status == TimesheetStatus.APPROVED and invoice is None
    data["can_reject"] = (
        ts.status == TimesheetStatus.SUBMITTED
        or (ts.status == TimesheetStatus.APPROVED and invoice is None)
    )
    return data


def _billable_rollup(project: Project | None,
                     entries: list[TimesheetEntry]) -> dict[str, Decimal | int]:
    """Decimal rollups shared by the summary and the invoice preview.

    total_* are the raw sums of the stored per-entry billables; actual_* are
    the same sums capped by the project's monthly maximums
    (max_billable_hours_month / max_billable_days_month) when those are set.
    """
    billable_hours = ZERO
    billable_days = ZERO
    leave_billable_days = ZERO
    working_days = 0
    for e in entries:
        billable_hours += Decimal(e.billable_hours or 0)
        billable_days += Decimal(e.billable_days or 0)
        if _is_leave_attendance(e.attendance_status):
            leave_billable_days += Decimal(e.billable_days or 0)
        if e.is_working:
            working_days += 1
    actual_billable_hours = billable_hours
    actual_billable_days = billable_days
    if project is not None and project.max_billable_hours_month is not None:
        actual_billable_hours = min(billable_hours, Decimal(project.max_billable_hours_month))
    if project is not None and project.max_billable_days_month is not None:
        actual_billable_days = min(billable_days, Decimal(project.max_billable_days_month))
    return {
        "billable_hours": billable_hours,
        "billable_days": billable_days,
        "actual_billable_hours": actual_billable_hours,
        "actual_billable_days": actual_billable_days,
        "leave_billable_days": leave_billable_days,
        "working_days": working_days,
    }


def _cap_view(rollup: dict, policy: "BillingPolicy") -> dict[str, Decimal]:
    """The customer-cap view of a month's billables (3 Sep 2026).

    ``shown_hours`` / ``shown_days`` are what the customer is billed — the
    capped figures, with the un-capped unit scaled to agree (176 of 184 hours
    is 22 of 23 days; a days cap scales hours the same way). ``excess_*`` is
    the work above the cap, and ``comp_off_from_cap`` converts it to leave
    days at the policy's full-day hours, in half-day steps — the same
    granularity weekend comp-off uses. Nothing here touches the invoice
    quantities (``actual_billable_*``), which keep their exact cap.
    """
    worked_h = Decimal(rollup["billable_hours"])
    worked_d = Decimal(rollup["billable_days"])
    shown_h = Decimal(rollup["actual_billable_hours"])
    shown_d = Decimal(rollup["actual_billable_days"])
    hours_capped = shown_h < worked_h
    days_capped = shown_d < worked_d
    if hours_capped and not days_capped and worked_h > ZERO:
        shown_d = min(shown_d, (worked_d * shown_h / worked_h).quantize(Decimal("0.01")))
    elif days_capped and not hours_capped and worked_d > ZERO:
        shown_h = min(shown_h, (worked_h * shown_d / worked_d).quantize(Decimal("0.01")))
    excess_h = max(worked_h - shown_h, ZERO)
    excess_d = max(worked_d - shown_d, ZERO)
    full_day = Decimal(policy.min_hours_full_day or 0) or Decimal("8")
    from_hours = excess_h / full_day
    comp = max(excess_d, from_hours)
    # Half-day steps, rounded down: 8 h → 1.0, 4 h → 0.5, 3 h → 0.
    comp = (comp * 2).to_integral_value(rounding=ROUND_FLOOR) / 2
    return {
        "shown_hours": shown_h, "shown_days": shown_d,
        "excess_hours": excess_h, "excess_days": excess_d,
        "comp_off_from_cap": comp,
    }


def _is_comp_off_work_day(e, policy: "BillingPolicy | None" = None) -> bool:
    """True when the entry is a week-off or holiday day that can earn/bill comp-off.

    day_type is AUTHORITATIVE when present (fix, Aug 2026): a Working day that
    merely falls on a Saturday — an ad-hoc working weekend, or a customer whose
    week-off pattern isn't Sat/Sun — is normal billed time. The old weekday>=5
    fallback fired for those rows too, so the same day was billed as worked
    time AND credited comp-off, breaking the bill-XOR-credit rule. The weekday
    fallback now (a) runs only when day_type is absent (legacy rows) and
    (b) uses the policy's week_off_days pattern, not hardcoded Sat/Sun.
    """
    att = e.attendance_status
    att_val = getattr(att, "value", att)
    day = getattr(e, "day_type", None)
    day_val = getattr(day, "value", day)
    if att_val in (AttendanceStatus.HOLIDAY, "Holiday") or day_val in (DayType.HOLIDAY, "Holiday"):
        return True
    if att_val in (AttendanceStatus.WEEK_OFF, "Week_Off") or day_val in (DayType.WEEK_OFF, "Week_Off"):
        return True
    if day_val not in (None, ""):
        return False  # an explicit Working/other day_type is never comp-off
    week_off = tuple(policy.week_off_days) if policy is not None and policy.week_off_days else (5, 6)
    return e.entry_date is not None and e.entry_date.weekday() in week_off


def _is_holiday_work_day(e) -> bool:
    """True when the entry is a holiday day (calendar / attendance / day_type)."""
    att = e.attendance_status
    att_val = getattr(att, "value", att)
    day = getattr(e, "day_type", None)
    day_val = getattr(day, "value", day)
    return (
        att_val in (AttendanceStatus.HOLIDAY, "Holiday")
        or day_val in (DayType.HOLIDAY, "Holiday")
    )


def _work_day_is_billed(e, policy: BillingPolicy) -> bool:
    """Weekend/holiday worked hours billed by direct flag OR comp_off_billable.

    Precedence (ISSUE-1): week_off_billable / holidays_billable > comp_off_billable.
    Either path bills → no leave credit (bill XOR credit).
    """
    hours = Decimal(e.hours_worked or 0)
    if hours <= ZERO or not _is_comp_off_work_day(e, policy):
        return False
    if _is_holiday_work_day(e):
        return bool(policy.holidays_billable or policy.comp_off_billable)
    return bool(policy.week_off_billable or policy.comp_off_billable)


def _comp_off_day_fraction(hours: Decimal, policy: BillingPolicy) -> Decimal:
    if hours >= policy.min_hours_full_day:
        return ONE
    if hours >= policy.min_hours_half_day:
        return HALF
    return ZERO


def comp_off_earned(entries: list[TimesheetEntry], policy: BillingPolicy) -> Decimal:
    """Comp-off leave credit earned by working on week-offs or holidays.

    Mutually exclusive with billing (project policy, same for every employee):
    - Billed by ``week_off_billable`` / ``holidays_billable`` OR ``comp_off_billable``
      → no leave credit.
    - Otherwise → credit leave (full/half by hour thresholds); do not bill.

    Full credit (1.0) when hours >= the policy's full-day threshold, half (0.5)
    when >= the half-day threshold, else none.
    """
    earned = ZERO
    for e in entries:
        hours = Decimal(e.hours_worked or 0)
        if hours <= ZERO or not _is_comp_off_work_day(e, policy):
            continue
        # DECISION (ISSUE-1): any billing path → earned = 0 for that day.
        if _work_day_is_billed(e, policy):
            continue
        earned += _comp_off_day_fraction(hours, policy)
    return earned


def comp_off_billed(entries: list[TimesheetEntry], policy: BillingPolicy) -> Decimal:
    """Day-fractions of weekend/holiday work billed (direct flag OR Comp Off Billable).

    Mutually exclusive with ``comp_off_earned`` — never both bill and credit.
    """
    billed = ZERO
    for e in entries:
        hours = Decimal(e.hours_worked or 0)
        if hours <= ZERO or not _work_day_is_billed(e, policy):
            continue
        billed += _comp_off_day_fraction(hours, policy)
    return billed


def comp_off_billed_hours(entries: list[TimesheetEntry], policy: BillingPolicy,
                          project: Project | None = None) -> Decimal:
    """Capped hours of weekend/holiday work included in billables when billed."""
    total = ZERO
    proj = project if project is not None else SimpleNamespace(max_billable_hours_day=None)
    for e in entries:
        hours = Decimal(e.hours_worked or 0)
        if hours <= ZERO or not _work_day_is_billed(e, policy):
            continue
        total += _capped_hours(hours, proj)  # type: ignore[arg-type]
    return total


def accrue_comp_off(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> Decimal:
    """Idempotently credit earned comp-off into PE or employee leave balance.

    Called on timesheet submit AND approve (DECISION ISSUE-2). Skips credit when
    weekend/holiday work is billed (direct billable flag or Comp Off Billable).
    Applies only the DELTA versus what this timesheet already granted
    (ts.comp_off_accrued), so submit→approve never double-credits. PE-mapped
    sheets credit ``project_employee_leave_details``; otherwise employee yearly
    balances. Caller commits.
    """
    project = db.get(Project, ts.project_id)
    pe = _pe_for_timesheet(db, ts)
    if project is not None:
        ensure_project_branch_id(db, project, pe=pe)
    policy = effective_billing_policy(db, project, pe=pe) if project else BillingPolicy()
    # NET of LOP cover (the Harman rule): a weekend day that made up a
    # Loss-of-Pay day is spent — it must not ALSO credit comp-off leave.
    # timesheet_summary is the single source of that netting.
    earned = Decimal(str(timesheet_summary(db, ts, entries)["comp_off_earned"]))        .quantize(Decimal("0.01"))
    prior = Decimal(ts.comp_off_accrued or 0)
    delta = earned - prior
    if delta == ZERO:
        return earned

    leave_type = db.execute(
        select(LeavePolicyType).where(func.lower(LeavePolicyType.name).like("comp%off%"))
    ).scalars().first()
    if leave_type is None:
        leave_type = LeavePolicyType(
            name="Comp-Off",
            accrual_rule="Earned on approved weekend/holiday work",
            carry_forward_rule="Expires in 90 days",
        )
        db.add(leave_type)
        db.flush()

    if ts.project_employee_id is not None:
        from services.project_employees import credit_pe_leave
        if delta > ZERO:
            pe_row = credit_pe_leave(db, ts.project_employee_id, leave_type.id, delta)
        else:
            # DECISION (ISSUE-3): reverse prior credit may go negative temporarily.
            from services.project_employees import consume_pe_leave
            pe_row = consume_pe_leave(
                db, ts.project_employee_id, leave_type.id, -delta,
                allow_negative=True,
            )
        balance_after = pe_row.leave_balance
    else:
        balance = db.execute(
            select(EmployeeLeaveBalance).where(
                EmployeeLeaveBalance.employee_id == ts.employee_id,
                EmployeeLeaveBalance.leave_type_id == leave_type.id,
                EmployeeLeaveBalance.year == ts.year,
            )
        ).scalars().first()
        if balance is None:
            balance = EmployeeLeaveBalance(
                employee_id=ts.employee_id, leave_type_id=leave_type.id, year=ts.year,
                accrued=0, consumed=0, balance=0, carry_forward=0,
            )
            db.add(balance)
            db.flush()

        balance.accrued = Decimal(balance.accrued or 0) + delta
        balance.balance = (Decimal(balance.accrued or 0) + Decimal(balance.carry_forward or 0)
                           - Decimal(balance.consumed or 0))
        balance_after = balance.balance

    ts.comp_off_accrued = earned
    # Ledger entry: every comp-off movement is auditable (migration 0021).
    db.add(LeaveAccrualEvent(
        employee_id=ts.employee_id,
        leave_type_id=leave_type.id,
        event_type="Comp_Off_Credit" if delta > ZERO else "Adjustment",
        amount=delta,
        balance_after=balance_after,
        source=f"timesheet:{ts.id}",
        note=f"Comp-off from timesheet {period_label(ts.year, ts.month)} "
             f"(earned {float(earned):g}, prior {float(prior):g})",
    ))
    return earned


def consume_timesheet_leaves(
    db: Session, ts: Timesheet, entries: list[TimesheetEntry] | list,
) -> dict[int, Decimal]:
    """Idempotently consume leave for LEAVE days (submit + approve).

    Paid portion of each leave type (incl. Comp-Off) is consumed from PE/employee
    balances (``allow_negative=False``, floor 0). Excess days plus explicit Loss
    of Pay entries debit the seeded Loss of Pay type.

    Applies only the DELTA vs existing ``LeaveAccrualEvent`` Consumption rows
    with ``source == timesheet:{id}`` so reject → resubmit → re-approve never
    double-deducts. Pass an empty ``entries`` list to reverse all prior
    consumption (used on reject). Caller commits.
    """
    from services.project_employees import consume_pe_leave

    # DECISION (ISSUE-5): lock balance rows at commit (Postgres FOR UPDATE).
    classification = classify_timesheet_leave_paid_vs_lop(
        db, ts, entries, for_update=True,
    )
    required: dict[int, Decimal] = dict(classification.paid_required_by_type_id)
    type_names: dict[int, str] = {}
    for split in classification.splits_by_date.values():
        if split.paid_days > ZERO and not split.is_explicit_lop:
            type_names[split.leave_type_id] = split.leave_type_name
    if classification.lop_days_total > ZERO:
        lop_type = ensure_loss_of_pay_type(db)
        required[lop_type.id] = (
            required.get(lop_type.id, ZERO) + classification.lop_days_total
        )
        type_names[lop_type.id] = lop_type.name

    prior = _timesheet_prior_consumption(db, ts)

    applied: dict[int, Decimal] = {}
    for leave_type_id in set(required) | set(prior):
        need = required.get(leave_type_id, ZERO)
        already = prior.get(leave_type_id, ZERO)
        delta = need - already
        if delta == ZERO:
            continue

        leave_name = type_names.get(leave_type_id)
        if not leave_name:
            lt_row = db.get(LeavePolicyType, leave_type_id)
            leave_name = lt_row.name if lt_row else f"type#{leave_type_id}"
        # LOP tracking may go above available; Comp-Off / paid types floor at 0.
        exempt = is_loss_of_pay_name(leave_name)

        if ts.project_employee_id is not None:
            if delta > ZERO:
                pe_row = consume_pe_leave(
                    db, ts.project_employee_id, leave_type_id, delta,
                    allow_negative=exempt,
                )
            else:
                # Reverse prior consumption (do not credit via accrual).
                from services.project_employees import pe_leave_detail_for
                pe_row = pe_leave_detail_for(db, ts.project_employee_id, leave_type_id)
                if pe_row is None:
                    pe_row = consume_pe_leave(
                        db, ts.project_employee_id, leave_type_id, ZERO,
                        allow_negative=True,
                    )
                pe_row.leave_consumed = Decimal(pe_row.leave_consumed or 0) + delta
                if Decimal(pe_row.leave_consumed or 0) < ZERO:
                    pe_row.leave_consumed = ZERO
                pe_row.leave_balance = Decimal(pe_row.leave_balance or 0) - delta
            balance_after = Decimal(pe_row.leave_balance or 0)
            # Floor paid balances at 0 if a prior negative somehow remains.
            if not exempt and balance_after < ZERO:
                pe_row.leave_balance = ZERO
                balance_after = ZERO
        else:
            balance = db.execute(
                select(EmployeeLeaveBalance).where(
                    EmployeeLeaveBalance.employee_id == ts.employee_id,
                    EmployeeLeaveBalance.leave_type_id == leave_type_id,
                    EmployeeLeaveBalance.year == ts.year,
                )
            ).scalars().first()
            if balance is None:
                balance = EmployeeLeaveBalance(
                    employee_id=ts.employee_id,
                    leave_type_id=leave_type_id,
                    year=ts.year,
                    accrued=0, consumed=0, balance=0, carry_forward=0,
                )
                db.add(balance)
                db.flush()
            available = Decimal(balance.balance or 0)
            if delta > ZERO and not exempt:
                # Floor: never consume more than available (paid types).
                if available < ZERO:
                    available = ZERO
                if delta > available:
                    delta = available
                if delta == ZERO:
                    continue
            balance.consumed = Decimal(balance.consumed or 0) + delta
            if Decimal(balance.consumed or 0) < ZERO:
                balance.consumed = ZERO
            balance.balance = (
                Decimal(balance.accrued or 0)
                + Decimal(balance.carry_forward or 0)
                - Decimal(balance.consumed or 0)
            )
            if not exempt and Decimal(balance.balance or 0) < ZERO:
                balance.balance = ZERO
            balance_after = Decimal(balance.balance or 0)

        db.add(LeaveAccrualEvent(
            employee_id=ts.employee_id,
            leave_type_id=leave_type_id,
            event_type="Consumption",
            amount=-delta,
            balance_after=balance_after,
            source=f"timesheet:{ts.id}",
            note=(
                f"{leave_name} from timesheet {period_label(ts.year, ts.month)} "
                f"(need {float(need):g}, prior {float(already):g})"
            ),
        ))
        applied[leave_type_id] = delta
    return applied


def release_timesheet_leaves(db: Session, ts: Timesheet) -> dict[int, Decimal]:
    """Reverse all leave consumption for this timesheet (reject path)."""
    return consume_timesheet_leaves(db, ts, [])


def release_timesheet_comp_off(db: Session, ts: Timesheet) -> Decimal:
    """Reverse comp-off accrual for this timesheet (reject / revert-to-draft).

    Forces earned target to 0 via ``accrue_comp_off`` delta vs ``ts.comp_off_accrued``.
    """
    return accrue_comp_off(db, ts, [])


def reverse_timesheet_ledger_effects(db: Session, ts: Timesheet) -> None:
    """DECISION (ISSUE-2): on REJECT / DELETE / revert to draft, reverse ledger keyed on
    ``source=timesheet:{id}`` — re-credit paid leave, remove LOP consumption, and
    reverse any submit-time comp-off credit.
    """
    release_timesheet_leaves(db, ts)
    release_timesheet_comp_off(db, ts)


def validate_timesheet_leave_balances(
    db: Session,
    ts: Timesheet,
    entries: list[TimesheetEntry] | list,
) -> None:
    """Soft check: unknown leave types only.

    Over-balance leave is allowed and converted to Loss of Pay on
    classify/consume — this no longer raises Insufficient balance.
    """
    type_cache: dict[str, LeavePolicyType] = {}
    for e in entries:
        if not _is_leave_attendance(getattr(e, "attendance_status", None)):
            continue
        name = (getattr(e, "leave_type", None) or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in type_cache:
            continue
        lt = resolve_leave_policy_type(db, name)
        if lt is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown leave type '{name}'",
            )
        type_cache[key] = lt


def timesheet_summary(db: Session, ts: Timesheet, entries: list[TimesheetEntry]) -> dict:
    project, policy, _leave_map, live = live_entries_from_policy(db, ts, entries)
    # Same classification the billable recompute uses (paid vs LOP per leave day).
    classification = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    rollup = _billable_rollup(project, live)  # type: ignore[arg-type]
    total_days = len(live)
    working_days = 0
    comp_off_days = 0
    hours_worked = ZERO
    holidays = 0
    present_days = 0
    absent_days = 0
    half_days = 0
    total_week_off = 0
    total_no_of_days_worked = 0
    for e in live:
        status = e.attendance_status
        if e.is_working:
            working_days += 1
        elif not _is_leave_attendance(status) and status not in (
            AttendanceStatus.HOLIDAY, "Holiday",
        ):
            # Non-working row that isn't a leave/holiday = weekend / week-off.
            total_week_off += 1
        if (e.leave_type or "").strip().lower() == "comp-off":
            comp_off_days += 1
        hours = Decimal(e.hours_worked or 0)
        hours_worked += hours
        if hours > ZERO:
            total_no_of_days_worked += 1
        if status in (AttendanceStatus.HOLIDAY, "Holiday"):
            holidays += 1
        elif status in (AttendanceStatus.PRESENT, "Present"):
            present_days += 1
        elif status in (AttendanceStatus.ABSENT, "Absent"):
            absent_days += 1
        elif status in (AttendanceStatus.HALF_DAY, "Half_Day"):
            half_days += 1

    # Leave / LOP MUST match classify_timesheet_leave_paid_vs_lop (same as billing
    # recompute + grid). Count every Leave attendance (paid + LOP), not only the
    # billable/paid subset. Prefer classification req_days when splits exist so
    # half-days match the paid/LOP split; otherwise sum entry leave fractions.
    leave_days = sum(
        (_entry_leave_days(e) for e in entries if _is_leave_attendance(e.attendance_status)),
        ZERO,
    )
    leave_req_from_clf = sum(
        (s.req_days for s in classification.splits_by_date.values()),
        ZERO,
    )
    if leave_req_from_clf > ZERO:
        leave_days = leave_req_from_clf
    # LOP reporting = leave excess + Absent working days + Half_Day unworked half.
    lop_from_leave = classification.lop_days_total
    lop_from_absent, lop_from_half = _attendance_reporting_lop(live)
    total_lop_days = lop_from_leave + lop_from_absent + lop_from_half

    # WEEKEND WORK COVERS LOP (14 Aug 2026, the "Harman rule"): in comp-off
    # CREDIT mode, a worked week-off/holiday day first MAKES UP a Loss-of-Pay
    # day — the employee delivered the month's required time, so the invoice
    # bills the full month and the LOP disappears. The covering fraction earns
    # NO comp-off credit (one day of work buys one benefit, never two); only
    # the remainder credits. Billed modes are untouched: comp_off_earned is
    # already zero there, so cover is zero too.
    raw_comp_off_earned = comp_off_earned(live, policy)  # type: ignore[arg-type]
    lop_cover = min(total_lop_days, raw_comp_off_earned)
    total_lop_days = total_lop_days - lop_cover
    net_comp_off_earned = raw_comp_off_earned - lop_cover
    paid_leave_days = sum(
        (s.paid_days for s in classification.splits_by_date.values()),
        ZERO,
    )
    # Paid leave that actually bills (LOP excess already zeroed in live recompute).
    leave_billable_days = Decimal(str(rollup["leave_billable_days"]))

    # THE CUSTOMER'S MONTHLY CAP (3 Sep 2026, user decision). When the
    # project caps billable hours/days per month (max_billable_hours_month /
    # max_billable_days_month), the invoice bills the CAPPED figure — and the
    # Summary must say the same number, not the raw sum ("176 on the invoice,
    # 184 in the summary" was the report). Time worked ABOVE the cap is real
    # work the customer will not pay for, so it is credited to the employee
    # as comp-off (kept in their balance, usable this month or later) instead
    # of vanishing. Only one of the two caps is usually set, so the other
    # unit is scaled to match — 176 of 184 hours is 22 of 23 days.
    cap = _cap_view(rollup, policy)
    net_comp_off_earned += cap["comp_off_from_cap"]

    return {
        "total_days": total_days,
        "working_days": working_days,
        "comp_off_days": comp_off_days,
        "hours_worked": float(hours_worked),
        "total_hours_worked": float(hours_worked),
        "billable_hours": float(rollup["billable_hours"]),
        "billable_days": float(rollup["billable_days"]),
        "leave_days": float(leave_days),
        "holidays": holidays,
        "present_days": present_days,
        "absent_days": absent_days,
        "half_days": half_days,
        # --- Tab 9 additions (existing keys above are kept untouched) ---
        "total_week_off": total_week_off,
        "total_no_of_days_worked": total_no_of_days_worked,
        # What the CUSTOMER is billed — the same figure the invoice shows.
        "total_billable_hours": float(cap["shown_hours"]),
        "total_billable_days": float(cap["shown_days"]),
        # What was WORKED and billable before the customer's monthly cap.
        "worked_billable_hours": float(rollup["billable_hours"]),
        "worked_billable_days": float(rollup["billable_days"]),
        "cap_excess_hours": float(cap["excess_hours"]),
        "cap_excess_days": float(cap["excess_days"]),
        "comp_off_from_cap": float(cap["comp_off_from_cap"]),
        "billable_cap_hours_month": (float(project.max_billable_hours_month)
                                     if project is not None and project.max_billable_hours_month is not None else None),
        "billable_cap_days_month": (float(project.max_billable_days_month)
                                    if project is not None and project.max_billable_days_month is not None else None),
        "actual_billable_hours": float(rollup["actual_billable_hours"]),
        "actual_billable_days": float(rollup["actual_billable_days"]),
        "actual_billable_day": float(display_billable_day(rollup["actual_billable_hours"])),
        # Leave attendance only (paid + leave-LOP). Absent/Half_Day are NOT leave.
        # Invariant when leave types are billable:
        # total_leave_days == total_leave_billable_days + loss_of_pay_from_leave
        "total_leave_days": float(leave_days),
        "total_leave_billable_days": float(leave_billable_days),
        # Raw components — their sum can EXCEED total_loss_of_pay_days when
        # weekend work covered part of it (lop_covered_days below).
        "loss_of_pay_from_leave": float(lop_from_leave),
        "loss_of_pay_from_absent": float(lop_from_absent),
        "loss_of_pay_from_half_day": float(lop_from_half),
        "total_loss_of_pay_days": float(total_lop_days),
        "lop_covered_days": float(lop_cover),
        "total_leave_paid_days": float(paid_leave_days),
        # Comp-off EARNED by weekend/holiday work (leave credit) when NOT
        # billable — NET of any fraction spent covering LOP days above.
        # Comp-off BILLED when Comp Off Billable ON (invoiced instead of credit).
        "comp_off_earned": float(net_comp_off_earned),
        "comp_off_billed": float(comp_off_billed(live, policy)),  # type: ignore[arg-type]
        "comp_off_billed_hours": float(comp_off_billed_hours(live, policy, project)),  # type: ignore[arg-type]
        "comp_off_credited": float(ts.comp_off_accrued or 0),
        "approved_time": {
            "approved_at": ts.approved_at.isoformat() if ts.approved_at else None,
            "approver_name": _user_display_name(db, ts.approved_by),
        },
        "reason_for_rejection": ts.rejection_reason,
        "attachment_url": ts.file_attachment_url,
    }


def _entries_for_timesheets(db: Session, ids: list[int]) -> dict[int, list[TimesheetEntry]]:
    if not ids:
        return {}
    rows = db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id.in_(ids))
        .order_by(TimesheetEntry.timesheet_id, TimesheetEntry.entry_date)
    ).scalars().all()
    out: dict[int, list[TimesheetEntry]] = {}
    for e in rows:
        out.setdefault(e.timesheet_id, []).append(e)
    return out


def _invoice_map(db: Session, ids: list[int]) -> dict[int, Invoice]:
    if not ids:
        return {}
    rows = db.execute(select(Invoice).where(Invoice.timesheet_id.in_(ids))).scalars().all()
    return {inv.timesheet_id: inv for inv in rows if inv.timesheet_id is not None}


def timesheet_report_out(db: Session, ts: Timesheet,
                         entries: list[TimesheetEntry] | None = None,
                         invoice: Invoice | None = None) -> dict:
    """Enriched row for list/report endpoints (display fields + report-only math)."""
    data = timesheet_out(ts)
    project = db.get(Project, ts.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    emp = db.get(Employee, ts.employee_id)
    pe = db.get(ProjectEmployee, ts.project_employee_id) if ts.project_employee_id else None
    if pe is None and project and emp:
        pe = db.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == ts.project_id,
                ProjectEmployee.employee_id == ts.employee_id,
            )
        ).scalars().first()
    start, end = sheet_period_bounds(ts, pe)
    if entries is None:
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
    project, _policy, _leave_map, live = live_entries_from_policy(db, ts, entries)
    rollup = _billable_rollup(project, live)  # type: ignore[arg-type]
    hours_worked = sum((Decimal(e.hours_worked or 0) for e in live), ZERO)
    actual_billable_hours = rollup["actual_billable_hours"]
    actual_billable_day = display_billable_day(actual_billable_hours)
    summary = timesheet_summary(db, ts, entries)

    data.update({
        "project_title": project_title_label(customer=customer, employee=emp, pe=pe, project=project),
        "customer_name": customer.name if customer else None,
        "project_type": getattr(opp.opp_type, "value", opp.opp_type) if opp else None,
        "project_employee_name": employee_display_name(emp),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_start_date": start.isoformat(),
        "period_end_date": end.isoformat(),
        "timesheet_period": f"{start.strftime('%d-%b-%Y')} to {end.strftime('%d-%b-%Y')}",
        "actual_billable_hours": float(actual_billable_hours),
        "total_hours_worked": float(hours_worked),
        "total_leave_billable_days": float(rollup["leave_billable_days"]),
        "actual_billable_day": float(actual_billable_day),
        "total_leave_days": summary["total_leave_days"],
        "loss_of_pay_from_leave": summary["loss_of_pay_from_leave"],
        "loss_of_pay_from_absent": summary["loss_of_pay_from_absent"],
        "loss_of_pay_from_half_day": summary["loss_of_pay_from_half_day"],
        "total_loss_of_pay_days": summary["total_loss_of_pay_days"],
        "attachments": list_attachments(db, ts),
    })
    if invoice is None:
        invoice = linked_invoice_for(db, ts)
    data["invoice"] = {
        "id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "payment_status": getattr(invoice.payment_status, "value", invoice.payment_status),
    } if invoice else None
    data["can_generate_invoice"] = (
        ts.status == TimesheetStatus.APPROVED and invoice is None
    )
    return data


def _assignment_start(pe: ProjectEmployee) -> date:
    return pe.billing_date or pe.onboarding_date or date.today()


def _month_range(start: date, end: date) -> list[tuple[int, int]]:
    """Inclusive (year, month) pairs from start through end."""
    y, m = start.year, start.month
    ey, em = end.year, end.month
    out: list[tuple[int, int]] = []
    while (y, m) <= (ey, em):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _due_context(db: Session, pe: ProjectEmployee, year: int, month: int) -> dict:
    project = db.get(Project, pe.project_id)
    customer = db.get(Customer, project.customer_id) if project else None
    opp = db.get(Opportunity, project.opportunity_id) if project and project.opportunity_id else None
    emp = db.get(Employee, pe.employee_id)
    start, end = period_bounds(year, month)
    return {
        "project_employee_id": pe.id,
        "project_employee_name": employee_display_name(emp),
        "employee_id": pe.employee_id,
        "project_id": pe.project_id,
        "project_type": getattr(opp.opp_type, "value", opp.opp_type) if opp else None,
        "customer_name": customer.name if customer else None,
        "project_title": project_title_label(customer=customer, employee=emp, pe=pe, project=project),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "month": month,
        "year": year,
    }


def due_report_rows(db: Session) -> list[dict]:
    """One row per active assignment/month from start through current month.

    Includes missing timesheets (Due) and Submitted (not yet Approved).
    """
    today = date.today()
    assignments = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.is_active.is_(True))
        .order_by(ProjectEmployee.project_id, ProjectEmployee.employee_id)
    ).scalars().all()
    rows: list[dict] = []
    for pe in assignments:
        start = _assignment_start(pe)
        for year, month in _month_range(start, today):
            ts = db.execute(
                select(Timesheet).where(
                    Timesheet.project_id == pe.project_id,
                    Timesheet.employee_id == pe.employee_id,
                    Timesheet.month == month,
                    Timesheet.year == year,
                )
            ).scalars().first()
            if ts is not None and ts.status == TimesheetStatus.APPROVED:
                continue
            if ts is not None and ts.status not in (TimesheetStatus.SUBMITTED,):
                continue
            ctx = _due_context(db, pe, year, month)
            if ts is None:
                ctx.update({
                    "timesheet_id": None,
                    "status": "Due",
                    "status_label": "Due",
                })
            else:
                ctx.update({
                    "timesheet_id": ts.id,
                    "status": getattr(ts.status, "value", ts.status),
                    "status_label": timesheet_status_label(ts.status),
                })
            rows.append(ctx)
    rows.sort(key=lambda r: (r.get("project_title") or "", r.get("year", 0), r.get("month", 0)))
    return rows


def _due_rows(db: Session, month: int, year: int) -> list[dict]:
    """Legacy month-scoped due list (HR reminders)."""
    assignments = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.is_active.is_(True))
        .order_by(ProjectEmployee.project_id, ProjectEmployee.employee_id)
    ).scalars().all()
    rows: list[dict] = []
    for pe in assignments:
        start = _assignment_start(pe)
        period_start, _ = period_bounds(year, month)
        if period_start < date(start.year, start.month, 1):
            continue
        ts = db.execute(
            select(Timesheet).where(
                Timesheet.project_id == pe.project_id,
                Timesheet.employee_id == pe.employee_id,
                Timesheet.month == month,
                Timesheet.year == year,
            )
        ).scalars().first()
        if ts is not None and ts.status in (TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED):
            continue
        ctx = _due_context(db, pe, year, month)
        if ts is None:
            due_status = "Due"
        elif ts.status == TimesheetStatus.DRAFT:
            due_status = "Draft"
        else:
            due_status = "Rejected"
        ctx.update({
            "employee_name": ctx["project_employee_name"],
            "status": due_status,
            "status_label": timesheet_status_label(ts.status, due_missing=ts is None)
            if ts else "Due",
            "timesheet_id": ts.id if ts else None,
        })
        rows.append(ctx)
    return rows


def for_submission_report_rows(db: Session) -> list[dict]:
    sheets = db.execute(
        select(Timesheet).where(Timesheet.status.in_([
            TimesheetStatus.DRAFT, TimesheetStatus.REJECTED,
        ])).order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).scalars().all()
    entries_map = _entries_for_timesheets(db, [t.id for t in sheets])
    return [
        timesheet_report_out(db, ts, entries=entries_map.get(ts.id, []))
        for ts in sheets
    ]


def approvals_report_rows(db: Session) -> list[dict]:
    sheets = db.execute(
        select(Timesheet).where(Timesheet.status.in_([
            TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED,
        ])).order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).scalars().all()
    ids = [t.id for t in sheets]
    entries_map = _entries_for_timesheets(db, ids)
    inv_map = _invoice_map(db, ids)
    return [
        timesheet_report_out(db, ts, entries=entries_map.get(ts.id, []),
                             invoice=inv_map.get(ts.id))
        for ts in sheets
    ]


# ---------------------------------------------------------------- invoice preview

def get_assignment_or_400(db: Session, ts: Timesheet) -> ProjectEmployee:
    """The employee's ProjectEmployee row on this timesheet's project, or 400."""
    assignment = db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == ts.project_id,
            ProjectEmployee.employee_id == ts.employee_id,
        )
    ).scalars().first()
    if assignment is None:
        raise HTTPException(
            status_code=400,
            detail="Employee has no project assignment (ProjectEmployee) on this project, "
                   "so billing rate/unit cannot be resolved",
        )
    return assignment


def linked_invoice_for(db: Session, ts: Timesheet) -> Invoice | None:
    return db.execute(
        select(Invoice).where(Invoice.timesheet_id == ts.id)
    ).scalars().first()


def initial_no_billing_end(db: Session, project: Project | None, pe) -> date | None:
    """EXCLUSIVE end date of the employee's initial no-billing window, or None.

    ENFORCEMENT of the "Initial No Billing Period" setting (26 Aug 2026) —
    captured on the New Project form since 0008 but never read by any billing
    engine, so a "2 weeks free" commercial term still billed from day one.

    Config resolves project override → branch → customer default (the same
    chain as every other billing property). The window anchors on EACH
    employee's own onboarding date (user decision): a person mapped three
    months into the project still gets their own free ramp-up window.
    """
    if project is None or pe is None:
        return None
    from services.branch_policy import resolve_branch_project_policy
    resolved = resolve_branch_project_policy(project, _project_branch(db, project))
    if not getattr(resolved, "is_initial_no_billing_period", False):
        return None
    qty = getattr(resolved, "initial_no_billing_qty", None)
    unit = (getattr(resolved, "initial_no_billing_period", None) or "").strip().lower()
    anchor = getattr(pe, "onboarding_date", None)
    if not qty or qty <= 0 or anchor is None:
        return None
    from datetime import timedelta
    if unit.startswith("hour"):
        days = -(-int(qty) // 24)  # ceil: any started day is a free day
        return anchor + timedelta(days=days)
    if unit.startswith("day"):
        return anchor + timedelta(days=int(qty))
    if unit.startswith("week"):
        return anchor + timedelta(weeks=int(qty))
    if unit.startswith("month"):
        m = anchor.month - 1 + int(qty)
        y, m = anchor.year + m // 12, m % 12 + 1
        return date(y, m, min(anchor.day, calendar.monthrange(y, m)[1]))
    if unit.startswith("year"):
        try:
            return anchor.replace(year=anchor.year + int(qty))
        except ValueError:  # Feb 29
            return anchor.replace(year=anchor.year + int(qty), day=28)
    return None


def timesheet_invoice_preview(db: Session, ts: Timesheet,
                              entries: list[TimesheetEntry] | None = None) -> dict:
    """Computed invoice subform for a timesheet (nothing is persisted).

    One line item per timesheet (one employee, one project, one month).

    Rates come from project_employee_rates (effective-dated). Mid-period rate
    changes split billable days/hours via project_employee_billing.split_period_by_rate.

    Amount formula by the assignment's billing_unit:
      * Hourly : sum over rate sub-periods of (hours_in_sub * rate)
      * Daily  : sum over rate sub-periods of (days_in_sub * rate)
      * Monthly: when leave is BILLABLE, charge each sub-period's monthly rate
        pro-rated by calendar days in the sub-period; when leave is NOT billable,
        use billable_days / working_days against the (split) monthly rates.
    """
    from services.project_employee_billing import (
        RateRow, invoice_amount_split, invoice_amount_uniform, rate_for_date, split_period_by_rate,
    )
    from services.project_employees import load_rate_rows

    project = db.get(Project, ts.project_id)
    emp = db.get(Employee, ts.employee_id)
    assignment = get_assignment_or_400(db, ts)
    if entries is None:
        entries = db.execute(
            select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
            .order_by(TimesheetEntry.entry_date)
        ).scalars().all()
    # Live recompute so invoice preview matches CURRENT Leave/Holiday Billing Policy.
    raw_entries = entries
    project, policy, _leave_map, live = live_entries_from_policy(db, ts, entries)
    # LOP reporting (leave excess + Absent + Half_Day) — display only; billing unchanged.
    lop_summary = timesheet_summary(db, ts, entries)
    entries = live  # type: ignore[assignment]

    # INITIAL NO-BILLING PERIOD (26 Aug 2026): days inside the employee's free
    # ramp-up window bill NOTHING. The money path works off `entries` filtered
    # to on-or-after the window's end; the sheet itself (hours, attendance,
    # leave) is untouched — the time happened, it just isn't invoiced.
    nb_end = initial_no_billing_end(db, project, assignment)
    nb_days_excluded = 0
    lop_billing = lop_summary
    if nb_end is not None:
        before = len(entries)
        entries = [e for e in entries if e.entry_date >= nb_end]
        nb_days_excluded = before - len(entries)
        if nb_days_excluded:
            # LOP inside the free window must not ALSO deduct from a Monthly
            # amount that already excludes those days — recompute for billing.
            lop_billing = timesheet_summary(
                db, ts, [e for e in raw_entries if e.entry_date >= nb_end])
    rollup = _billable_rollup(project, entries)

    rate_rows = load_rate_rows(db, assignment.id)
    unit = assignment.billing_unit
    period_month = f"{calendar.month_abbr[ts.month]} {ts.year}"
    employee_name = employee_display_name(emp) or f"Employee #{ts.employee_id}"
    period_start, period_end = period_bounds(ts.year, ts.month)

    fallback_rate = Decimal(assignment.billing_rate or 0)
    if not rate_rows:
        rate_rows = [RateRow.of(period_start, fallback_rate)]

    if unit == BillingUnit.YEARLY:
        # A Yearly assignment stores the ANNUAL price; a monthly invoice bills
        # its monthly equivalent. Converting the rate rows up front (rather
        # than special-casing every formula below) means Yearly then flows
        # through the Monthly branch untouched — including mid-period rate
        # splits, which keep their effective dates and just carry /12 values.
        fallback_rate = fallback_rate / Decimal(12)
        rate_rows = [RateRow.of(r.effective_from, r.rate / Decimal(12)) for r in rate_rows]

    current_rate = rate_for_date(rate_rows, period_end) or fallback_rate
    subs = split_period_by_rate(period_start, period_end, rate_rows)
    rate_changed = len({s.rate for s in subs}) > 1

    # Mid-month join/exit (PE onboarding/exit dates): a Monthly rate bills the
    # PRESENT fraction of the month, not the whole month. Hourly/Daily need no
    # ratio — their quantities come from the (already clamped) entries.
    window_start, window_end = sheet_period_bounds(ts, assignment)
    # The no-billing window shortens the BILLED window exactly like a
    # mid-month join: a Monthly rate bills only the fraction after it ends.
    if nb_end is not None and nb_end > window_start:
        window_start = nb_end
    month_len = Decimal((period_end - period_start).days + 1)
    window_len = (Decimal((window_end - window_start).days + 1)
                  if window_end >= window_start else ZERO)
    presence_ratio = (window_len / month_len) if month_len > ZERO else ONE
    if presence_ratio > ONE:
        presence_ratio = ONE

    def _entry_in_range(e: TimesheetEntry, start: date, end: date) -> bool:
        return start <= e.entry_date <= end

    if unit == BillingUnit.HOURLY:
        if rate_changed:
            parts: list[tuple[Decimal, Decimal]] = []
            for sub in subs:
                hrs = sum(
                    (Decimal(e.billable_hours or 0) for e in entries
                     if _entry_in_range(e, sub.start, sub.end)),
                    ZERO,
                )
                parts.append((hrs, sub.rate))
            amount = invoice_amount_split(parts)
            qty = rollup["actual_billable_hours"]
            rate = current_rate
        else:
            qty = rollup["actual_billable_hours"]
            rate = current_rate
            amount = invoice_amount_uniform(qty, rate)
        monthly_cost = None
    elif unit == BillingUnit.DAILY:
        if rate_changed:
            parts = []
            for sub in subs:
                days_ = sum(
                    (Decimal(e.billable_days or 0) for e in entries
                     if _entry_in_range(e, sub.start, sub.end)),
                    ZERO,
                )
                parts.append((days_, sub.rate))
            amount = invoice_amount_split(parts)
            qty = rollup["actual_billable_days"]
            rate = current_rate
        else:
            qty = rollup["actual_billable_days"]
            rate = current_rate
            amount = invoice_amount_uniform(qty, rate)
        monthly_cost = None
    else:  # Monthly
        qty = ONE
        monthly_cost = current_rate
        working_days = Decimal(rollup["working_days"])
        # Week-off/holiday days the employee WORKED and the policy bills.
        # These are EXTRA effort above the standard working month the flat
        # rate covers — valued per working day and added on top (below).
        co_billed_days = comp_off_billed(entries, policy)  # type: ignore[arg-type]
        if rate_changed:
            parts = []
            for sub in subs:
                if policy.leave_billable:
                    cal_days = Decimal((sub.end - sub.start).days + 1)
                    month_days_n = Decimal((period_end - period_start).days + 1)
                    ratio = (cal_days / month_days_n) if month_days_n > ZERO else ZERO
                    parts.append((ratio, sub.rate))
                else:
                    if working_days > ZERO:
                        sub_entries = [e for e in entries
                                       if _entry_in_range(e, sub.start, sub.end)]
                        sub_billable = sum(
                            (Decimal(e.billable_days or 0) for e in sub_entries), ZERO)
                        # Same rule as the uniform path: billed weekend work is
                        # priced as the explicit extra below, not in the ratio.
                        sub_co = comp_off_billed(sub_entries, policy)  # type: ignore[arg-type]
                        base_days = max(sub_billable - sub_co, ZERO)
                        parts.append((min(base_days / working_days, ONE), sub.rate))
                    else:
                        parts.append((ZERO, sub.rate))
            amount = invoice_amount_split(parts)
            rate = current_rate
        else:
            rate = current_rate
            if policy.leave_billable:
                amount = rate.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
            else:
                if working_days > ZERO:
                    # Billed weekend/holiday work is excluded here and priced
                    # as the explicit extra below — its billable_days used to
                    # inflate this numerator (while the denominator counts
                    # only working days), double-paying it on partial months
                    # and silently discarding it on full ones (the min cap).
                    base_days = max(rollup["actual_billable_days"] - co_billed_days, ZERO)
                    ratio = min(base_days / working_days, ONE)
                else:
                    ratio = ZERO
                amount = (rate * ratio).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        if presence_ratio < ONE:
            # Present a fraction of the month -> bill that fraction.
            amount = (amount * presence_ratio).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        # LOSS OF PAY reduces a Monthly bill (fix, Aug 2026). The leave-
        # billable path used to charge the full month regardless — an employee
        # with an unpaid day still invoiced 100%, so LOP was a red number that
        # cost nobody anything. Each LOP day (over-balance leave, explicit
        # Loss of Pay, Absent, unworked half-days) now deducts one working
        # day's value: rate × lop / working-days-in-window.
        # The non-leave-billable path needs no deduction here — its ratio is
        # built from billable days, where LOP days already contribute zero.
        lop_days = Decimal(str(lop_billing["total_loss_of_pay_days"]))
        if policy.leave_billable and lop_days > ZERO and working_days > ZERO:
            lop_ratio = min(lop_days / working_days, ONE)
            amount = (amount * (ONE - lop_ratio)).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

        # COMP-OFF BILLED work ADDS to a Monthly bill (fix, 13 Aug 2026).
        # Hourly/Daily units already carry worked week-off/holiday time inside
        # their quantities; the Monthly flat rate swallowed it — the popup
        # said "Comp-Off Billed (qty) 1 · ₹86.96" while the sub-total stayed
        # at the bare month. Each billed day-fraction is worth one working
        # day of this window: (rate × presence) / working_days — the same
        # per-day figure the popup prints, so its maths tie out exactly.
        if co_billed_days > ZERO and working_days > ZERO:
            per_day_val = (rate * presence_ratio) / working_days
            amount = (amount + co_billed_days * per_day_val).quantize(
                TWO_PLACES, rounding=ROUND_HALF_UP)

        # QTY is the fraction of the monthly rate actually billed, at 4dp —
        # and the amount is then REDEFINED as qty x rate, so stored line,
        # printed PDF and GST base all reconcile exactly by construction
        # (max rounding cost: rate x 0.00005). 2dp qty was the old precision;
        # 0.96 x 3,00,000 = 2,88,000 vs an amount of 2,86,957 put a phantom
        # thousand rupees between the popup and the invoice.
        if rate > ZERO:
            qty = (amount / rate).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            amount = (qty * rate).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    # Rate basis for the breakdown popup: WHICH unit the assignment bills in
    # (straight from the Project Employee record) and the derived per-day /
    # per-hour charge for this sheet's window. Monthly/Yearly derive per-day
    # from the window's working days — the same denominator the LOP deduction
    # uses, so "1 LOP day costs the per-day charge" reads true in the popup.
    wd_count = Decimal(rollup["working_days"])
    hours_per_day = policy.min_hours_full_day or Decimal("8")
    if unit == BillingUnit.HOURLY:
        per_hour: Decimal | None = current_rate
        per_day: Decimal | None = current_rate * hours_per_day
    elif unit == BillingUnit.DAILY:
        per_day = current_rate
        per_hour = (current_rate / hours_per_day) if hours_per_day > ZERO else None
    else:  # Monthly — and Yearly, whose rate rows were already divided by 12
        # presence_ratio matters: a mid-month joiner's window bills HALF the
        # monthly rate over the window's working days, so one day of that
        # window is worth (rate x presence) / wd — this keeps the popup's
        # per-day figure equal to what one LOP day actually deducts.
        per_day = ((current_rate * presence_ratio) / wd_count) if wd_count > ZERO else None
        per_hour = (per_day / hours_per_day) if per_day is not None and hours_per_day > ZERO else None

    def _money(v: Decimal | None) -> float | None:
        return float(v.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)) if v is not None else None

    line = {
        "s_no": 1,
        "description": line_description(employee_name, period_month),
        "monthly_cost": float(monthly_cost) if monthly_cost is not None else None,
        "total_billed_qty": float(qty),
        "rate_per_unit": float(rate),
        "billing_unit": getattr(unit, "value", str(unit)),
        "working_days_in_period": float(wd_count),
        "hours_per_full_day": float(hours_per_day),
        "per_day_charge": _money(per_day),
        "per_hour_charge": _money(per_hour),
        "leave_billable_days": float(rollup["leave_billable_days"]),
        # Comp-off billed qty: hours for Hourly unit, day-fractions for Daily/Monthly.
        "comp_off_billable_qty": float(
            comp_off_billed_hours(entries, policy, project)  # type: ignore[arg-type]
            if unit == BillingUnit.HOURLY
            else comp_off_billed(entries, policy)  # type: ignore[arg-type]
        ),
        # Since Aug 2026 this also REDUCES a leave-billable Monthly amount.
        # NET of lop_covered_days (weekend work that made up the time).
        "loss_of_pay_days": float(lop_summary["total_loss_of_pay_days"]),
        "lop_covered_days": float(lop_summary.get("lop_covered_days", 0)),
        # Initial no-billing period (26 Aug 2026): how many of this sheet's
        # days fell inside the employee's free ramp-up window and billed zero.
        "no_billing_days_excluded": nb_days_excluded,
        "no_billing_until": nb_end.isoformat() if nb_end is not None and nb_days_excluded else None,
        "amount": float(amount),
        "rate_split": rate_changed,
    }
    linked = linked_invoice_for(db, ts)
    return {
        "line_items": [line],
        "totals": {"sub_total": float(amount)},
        "linked_invoice": {
            "id": linked.id,
            "invoice_number": linked.invoice_number,
            "payment_status": getattr(linked.payment_status, "value", linked.payment_status),
        } if linked else None,
        "can_generate": ts.status == TimesheetStatus.APPROVED and linked is None,
    }
