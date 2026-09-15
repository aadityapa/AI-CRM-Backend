"""11 Sep 2026 fixes: a half-day leave on a worked day keeps the worked half
billable, and the Monthly denominator counts billable week-offs/holidays."""
from decimal import Decimal
from types import SimpleNamespace

from models import AttendanceStatus, LeavePeriod
from services.timesheets import BillingPolicy, _billable_rollup, compute_billables

PROJECT = SimpleNamespace(max_billable_hours_day=None, max_billable_hours_month=None,
                          max_billable_days_month=None)


def test_half_day_leave_on_a_worked_day_bills_worked_half_plus_paid_leave_half():
    policy = BillingPolicy(leave_billable=True, min_hours_full_day=Decimal("9.5"),
                           min_hours_half_day=Decimal("4.5"))
    bh, bd = compute_billables(is_working=True, hours_worked=Decimal("4.5"),
                               attendance_status=AttendanceStatus.LEAVE,
                               leave_period=LeavePeriod.HALF_PM, project=PROJECT,
                               policy=policy, leave_type="Sick Leave")
    assert bd == Decimal("1")
    assert bh == Decimal("9.0")


def test_half_day_leave_when_leave_is_not_billable_still_bills_the_worked_half():
    policy = BillingPolicy(leave_billable=False, min_hours_full_day=Decimal("9.5"),
                           min_hours_half_day=Decimal("4.5"))
    bh, bd = compute_billables(is_working=True, hours_worked=Decimal("4.5"),
                               attendance_status=AttendanceStatus.LEAVE,
                               leave_period=LeavePeriod.HALF_AM, project=PROJECT,
                               policy=policy, leave_type="Comp-Off")
    assert bd == Decimal("0.5")
    assert bh == Decimal("4.5")


def _row(att, working, day_type="Working"):
    return SimpleNamespace(billable_hours=0, billable_days=0, attendance_status=att,
                           is_working=working, day_type=day_type)


def test_billed_days_counts_week_offs_and_holidays_only_when_billable():
    rows = [_row(AttendanceStatus.PRESENT, True)] * 21 \
        + [_row(AttendanceStatus.WEEK_OFF, False, "Week_Off")] * 9 \
        + [_row(AttendanceStatus.HOLIDAY, False, "Holiday")]
    all_on = BillingPolicy(week_off_billable=True, holidays_billable=True)
    assert _billable_rollup(None, rows, all_on)["billed_days"] == 31
    wk_only = BillingPolicy(week_off_billable=True, holidays_billable=False)
    assert _billable_rollup(None, rows, wk_only)["billed_days"] == 30
    none_on = BillingPolicy()
    assert _billable_rollup(None, rows, none_on)["billed_days"] == 21
    assert _billable_rollup(None, rows)["billed_days"] == 21   # no policy → working days
