"""Week Off Billable bills the unworked weekend day itself (ISSUE-1 close-out).

Holidays Billable has always billed an unworked holiday as a full day; Week Off
Billable only billed weekend *worked* hours, so the checkbox looked broken and
a calendar-month customer's sheet showed 22 billable days instead of 31. These
tests pin the now-symmetric rule and the resulting month totals.

Run:  cd backend && python -m pytest tests/test_weekoff_billable_day.py -q
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from models.timesheets import AttendanceStatus
from services.timesheets import BillingPolicy, compute_billables

_PROJECT = SimpleNamespace(max_billable_hours_day=None)


def _policy(**kw) -> BillingPolicy:
    base = dict(week_off_billable=False, leave_billable=False, holidays_billable=False,
                comp_off_billable=False, min_hours_full_day=8, min_hours_half_day=4)
    base.update(kw)
    return BillingPolicy(**base)


def _weekoff(policy: BillingPolicy, hours: float = 0):
    return compute_billables(
        is_working=False,
        hours_worked=Decimal(str(hours)),
        attendance_status=AttendanceStatus.WEEK_OFF,
        leave_period=None,
        project=_PROJECT,  # type: ignore[arg-type]
        policy=policy,
    )


def test_unworked_weekoff_bills_a_full_day_when_flag_on():
    bh, bd = _weekoff(_policy(week_off_billable=True))
    assert (float(bh), float(bd)) == (8.0, 1.0)


def test_unworked_weekoff_bills_nothing_when_flag_off():
    # comp_off_billable alone must NOT bill an unworked weekend — comp-off is
    # about compensating worked weekend hours, not paying for rest days.
    assert _weekoff(_policy()) == (Decimal("0"), Decimal("0"))
    assert _weekoff(_policy(comp_off_billable=True)) == (Decimal("0"), Decimal("0"))


def test_worked_weekoff_still_bills_actual_hours():
    bh, bd = _weekoff(_policy(week_off_billable=True), hours=6)
    assert float(bh) == 6.0
    assert float(bd) == 0.5  # 6h is above the half-day, below the full-day threshold


def test_symmetry_with_unworked_holiday():
    """The rule the fix restores: both flags treat an unworked day identically."""
    hol_bh, hol_bd = compute_billables(
        is_working=False,
        hours_worked=Decimal("0"),
        attendance_status=AttendanceStatus.HOLIDAY,
        leave_period=None,
        project=_PROJECT,  # type: ignore[arg-type]
        policy=_policy(holidays_billable=True),
    )
    wo_bh, wo_bd = _weekoff(_policy(week_off_billable=True))
    assert (hol_bh, hol_bd) == (wo_bh, wo_bd)


def test_calendar_month_bills_every_day_with_both_flags():
    """20 working + 9 week-off + 2 holidays = 31 billable days, 248 hours."""
    policy = _policy(week_off_billable=True, holidays_billable=True)
    total_days = Decimal("0")
    total_hours = Decimal("0")

    def add(status, *, is_working, hours):
        nonlocal total_days, total_hours
        bh, bd = compute_billables(
            is_working=is_working, hours_worked=Decimal(str(hours)),
            attendance_status=status, leave_period=None,
            project=_PROJECT, policy=policy,  # type: ignore[arg-type]
        )
        total_days += bd
        total_hours += bh

    for _ in range(20):
        add(AttendanceStatus.PRESENT, is_working=True, hours=8)
    for _ in range(9):
        add(AttendanceStatus.WEEK_OFF, is_working=False, hours=0)
    for _ in range(2):
        add(AttendanceStatus.HOLIDAY, is_working=False, hours=0)

    assert float(total_days) == 31.0
    assert float(total_hours) == 248.0
