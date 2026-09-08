"""Unit tests: per-leave-type billability + Comp Off billable vs credit gating.

Run:  python -m pytest tests/test_timesheet_leave_billable_comp_off.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from models.timesheets import AttendanceStatus, DayType, LeavePeriod
from services.timesheets import (
    BillingPolicy, comp_off_billed, comp_off_billed_hours, comp_off_earned,
    compute_billables,
)

D = Decimal
ZERO = D("0")
HALF = D("0.5")
ONE = D("1")


def _project(**kw):
    return SimpleNamespace(max_billable_hours_day=kw.get("max_billable_hours_day"))


def _policy(**kw) -> BillingPolicy:
    return BillingPolicy(
        week_off_billable=kw.get("week_off_billable", False),
        leave_billable=kw.get("leave_billable", False),
        holidays_billable=kw.get("holidays_billable", False),
        comp_off_billable=kw.get("comp_off_billable", False),
        min_hours_full_day=D(str(kw.get("min_hours_full_day", "8"))),
        min_hours_half_day=D(str(kw.get("min_hours_half_day", "4"))),
    )


def _entry(*, entry_date, hours, attendance=AttendanceStatus.WEEK_OFF,
           day_type=DayType.WEEK_OFF):
    return SimpleNamespace(
        entry_date=entry_date,
        hours_worked=hours,
        attendance_status=attendance,
        day_type=day_type,
    )


# ---------------------------------------------------------------- leave billability


def test_leave_billable_via_per_type_map_when_flat_flag_off():
    """Leave type marked billable in project Leave Billing Policy → full-day hours + 1 day."""
    proj = _project()
    policy = _policy(leave_billable=False)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Casual",
        leave_billable_by_type={"Casual": True},
    )
    assert bh == D("8")
    assert bd == ONE


def test_leave_not_billable_when_per_type_false_even_if_flat_on():
    proj = _project()
    policy = _policy(leave_billable=True)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Unpaid",
        leave_billable_by_type={"Unpaid": False},
    )
    assert bh == ZERO
    assert bd == ZERO


def test_leave_falls_back_to_flat_policy_when_type_unset():
    proj = _project()
    policy = _policy(leave_billable=True)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.HALF_AM,
        project=proj,
        policy=policy,
        leave_type="Sick",
        leave_billable_by_type={},  # type not in map
    )
    assert bh == D("4")
    assert bd == HALF


def test_leave_falls_back_to_flat_false_when_map_empty():
    proj = _project()
    policy = _policy(leave_billable=False)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=None,
        project=proj,
        policy=policy,
        leave_type="Casual",
        leave_billable_by_type=None,
    )
    assert (bh, bd) == (ZERO, ZERO)


def test_billable_leave_uses_policy_hour_thresholds():
    """Paid leave hours come from min_hours_full/half_day, not hardcoded 8/4."""
    proj = _project()
    policy = _policy(leave_billable=True, min_hours_full_day=D("7.5"), min_hours_half_day=D("3.5"))
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Casual",
        leave_billable_by_type={"Casual": True},
    )
    assert bh == D("7.5")
    assert bd == ONE
    bh2, bd2 = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.HALF_PM,
        project=proj,
        policy=policy,
        leave_type="Casual",
        leave_billable_by_type={"Casual": True},
    )
    assert bh2 == D("3.5")
    assert bd2 == HALF


def test_same_map_yields_identical_billability_for_two_employees():
    """Policy is project-scoped: same day pattern → same billable_days regardless of employee."""
    proj = _project()
    policy = _policy(leave_billable=False)
    leave_map = {"Casual": True, "Sick": True}
    kwargs = dict(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Casual",
        leave_billable_by_type=leave_map,
    )
    assert compute_billables(**kwargs) == compute_billables(**kwargs)


def test_explicit_loss_of_pay_always_non_billable():
    proj = _project()
    policy = _policy(leave_billable=True)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Loss of Pay",
        leave_billable_by_type={"Loss of Pay": True},
    )
    assert bh == ZERO and bd == ZERO


def test_paid_leave_days_scales_billables():
    """Full-day leave with only 0.5 paid → half-day billable; 0 paid → zero."""
    proj = _project()
    policy = _policy(leave_billable=True)
    bh, bd = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Earned Leave",
        paid_leave_days=HALF,
    )
    assert bh == D("4")
    assert bd == HALF
    bh0, bd0 = compute_billables(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_period=LeavePeriod.FULL,
        project=proj,
        policy=policy,
        leave_type="Earned Leave",
        paid_leave_days=ZERO,
    )
    assert bh0 == ZERO and bd0 == ZERO


# ---------------------------------------------------------------- comp-off billing vs credit


def test_weekend_work_bills_when_comp_off_billable():
    """Comp Off Billable ON → Saturday 8h is billable; no leave credit."""
    saturday = date(2026, 6, 6)  # Saturday
    proj = _project(max_billable_hours_day=D("8"))
    policy = _policy(comp_off_billable=True)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert bh == D("8")
    assert bd == ONE
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE
    assert comp_off_billed_hours(entries, policy, proj) == D("8")


def test_weekend_work_credits_when_comp_off_not_billable():
    """Comp Off Billable OFF → Saturday 8h not billed; leave credit 1.0."""
    saturday = date(2026, 6, 6)
    proj = _project()
    policy = _policy(comp_off_billable=False)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert (bh, bd) == (ZERO, ZERO)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ONE
    assert comp_off_billed(entries, policy) == ZERO


def test_weekend_half_day_comp_off_when_not_billable():
    sunday = date(2026, 6, 7)
    policy = _policy(comp_off_billable=False, min_hours_half_day=D("4"))
    entries = [_entry(entry_date=sunday, hours=D("4"))]
    assert comp_off_earned(entries, policy) == HALF
    assert comp_off_billed(entries, policy) == ZERO


def test_week_off_billable_flag_bills_worked_weekend():
    """ISSUE-1: week_off_billable ON bills weekend work even when comp_off_billable OFF."""
    proj = _project()
    policy = _policy(week_off_billable=True, comp_off_billable=False)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert bh == D("8") and bd == ONE
    saturday = date(2026, 6, 6)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_holiday_work_bills_when_comp_off_billable():
    """Holiday with hours > 0 + Comp Off Billable ON → bill worked hours, no credit."""
    proj = _project(max_billable_hours_day=D("8"))
    policy = _policy(comp_off_billable=True, holidays_billable=False)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert bh == D("8") and bd == ONE
    entries = [_entry(
        entry_date=date(2026, 6, 15),
        hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_holiday_work_credits_when_comp_off_not_billable():
    policy = _policy(comp_off_billable=False, holidays_billable=False)
    proj = _project()
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert (bh, bd) == (ZERO, ZERO)
    entries = [_entry(
        entry_date=date(2026, 6, 15),
        hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ONE


def test_pure_holiday_off_still_uses_holidays_billable():
    """0-hour holiday (not worked) still bills via holidays_billable."""
    proj = _project()
    policy = _policy(holidays_billable=True, comp_off_billable=False)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert bh == D("8") and bd == ONE
    entries = [_entry(
        entry_date=date(2026, 6, 15),
        hours=ZERO,
        attendance=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ZERO


def test_never_both_bill_and_credit():
    """comp_off_earned and comp_off_billed are mutually exclusive for the same sheet."""
    saturday = date(2026, 6, 6)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    on = _policy(comp_off_billable=True)
    off = _policy(comp_off_billable=False)
    assert comp_off_earned(entries, on) == ZERO and comp_off_billed(entries, on) == ONE
    assert comp_off_earned(entries, off) == ONE and comp_off_billed(entries, off) == ZERO


def test_two_employees_identical_comp_off_treatment():
    """Same project policy → identical billable / earned for identical weekend rows."""
    saturday = date(2026, 6, 6)
    proj = _project(max_billable_hours_day=D("8"))
    policy = _policy(comp_off_billable=True)
    kwargs = dict(
        is_working=False,
        hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF,
        leave_period=None,
        project=proj,
        policy=policy,
    )
    assert compute_billables(**kwargs) == compute_billables(**kwargs)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_billed(entries, policy) == comp_off_billed(entries, policy)


def test_weekday_present_never_earns_or_bills_comp_off():
    policy = _policy(comp_off_billable=False)
    entries = [_entry(
        entry_date=date(2026, 6, 15),  # Monday
        hours=D("8"),
        attendance=AttendanceStatus.PRESENT,
        day_type=DayType.WORKING,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, _policy(comp_off_billable=True)) == ZERO


# Legacy names kept as aliases of the new behaviour (comp_off_billable gating).


def test_weekend_work_no_comp_off_when_week_off_billable():
    """week_off_billable ON → billed; earned = 0 (bill XOR credit)."""
    saturday = date(2026, 6, 6)
    policy = _policy(week_off_billable=True, comp_off_billable=False)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_weekend_work_earns_comp_off_when_week_off_not_billable():
    saturday = date(2026, 6, 6)
    policy = _policy(week_off_billable=False, comp_off_billable=False)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ONE


def test_holiday_work_no_comp_off_when_holidays_billable():
    """Holiday hours > 0 + holidays_billable → billed; no credit (even if comp_off off)."""
    policy = _policy(holidays_billable=True, comp_off_billable=False)
    entries = [_entry(
        entry_date=date(2026, 6, 15),
        hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_holiday_work_earns_comp_off_when_holidays_not_billable():
    policy = _policy(holidays_billable=False, comp_off_billable=False)
    entries = [_entry(
        entry_date=date(2026, 6, 15),
        hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ONE
