"""Unit tests: live recompute from current project policy (no re-save needed).

Run:  python -m pytest tests/test_timesheet_recompute_live.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from models.timesheets import AttendanceStatus, DayType, LeavePeriod
from services.timesheets import (
    BillingPolicy, compute_billables, recompute_entry_live,
)

D = Decimal
ZERO = D("0")
ONE = D("1")


def _policy(**kw) -> BillingPolicy:
    return BillingPolicy(
        week_off_billable=kw.get("week_off_billable", False),
        leave_billable=kw.get("leave_billable", False),
        holidays_billable=kw.get("holidays_billable", False),
        comp_off_billable=kw.get("comp_off_billable", False),
        min_hours_full_day=D(str(kw.get("min_hours_full_day", "8"))),
        min_hours_half_day=D(str(kw.get("min_hours_half_day", "4"))),
    )


def _entry(**kw):
    return SimpleNamespace(
        hours_worked=kw.get("hours_worked", ZERO),
        is_working=kw.get("is_working", True),
        attendance_status=kw.get("attendance_status", AttendanceStatus.PRESENT),
        leave_type=kw.get("leave_type"),
        leave_period=kw.get("leave_period"),
        day_type=kw.get("day_type", DayType.WORKING),
        entry_date=kw.get("entry_date", date(2026, 7, 4)),
    )


def _project():
    return SimpleNamespace(max_billable_hours_day=D("8"))


def test_holiday_billable_returns_full_day_hours():
    policy = _policy(holidays_billable=True)
    # Real holiday rows are is_working=False — must still bill when flag is ON.
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=_project(),
        policy=policy,
    )
    assert bh == D("8")
    assert bd == ONE


def test_holiday_not_billable_returns_zero():
    policy = _policy(holidays_billable=False)
    bh, bd = compute_billables(
        is_working=False,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=_project(),
        policy=policy,
    )
    assert (bh, bd) == (ZERO, ZERO)


def test_recompute_entry_live_reflects_policy_flip_without_mutating_stored():
    """Stored billable_hours=0; after Holidays Billable ON, live returns 8h / 1d."""
    e = _entry(
        is_working=False,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.HOLIDAY,
        day_type=DayType.HOLIDAY,
    )
    e.billable_hours = ZERO
    e.billable_days = ZERO
    policy_off = _policy(holidays_billable=False)
    _att0, bh0, bd0 = recompute_entry_live(
        e, policy=policy_off, project=_project(), leave_billable_by_type={},
    )
    assert bh0 == ZERO and bd0 == ZERO

    policy_on = _policy(holidays_billable=True)
    _att1, bh1, bd1 = recompute_entry_live(
        e, policy=policy_on, project=_project(), leave_billable_by_type={},
    )
    assert bh1 == D("8") and bd1 == ONE
    assert Decimal(e.billable_hours) == ZERO


def test_recompute_leave_billable_hours_from_map():
    e = _entry(
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_type="Casual Leave",
        leave_period=LeavePeriod.FULL,
    )
    e.billable_hours = ZERO
    policy = _policy(leave_billable=False)
    _att, bh, bd = recompute_entry_live(
        e, policy=policy, project=_project(),
        leave_billable_by_type={"Casual Leave": True},
    )
    assert bh == D("8") and bd == ONE


def test_recompute_threshold_reclassifies_present_vs_half():
    e = _entry(
        is_working=True,
        hours_worked=D("5"),
        attendance_status=AttendanceStatus.PRESENT,
    )
    policy_strict = _policy(min_hours_full_day=D("8"), min_hours_half_day=D("4"))
    att, _bh, bd = recompute_entry_live(
        e, policy=policy_strict, project=_project(), leave_billable_by_type={},
    )
    assert att == AttendanceStatus.HALF_DAY
    assert bd == D("0.5")
