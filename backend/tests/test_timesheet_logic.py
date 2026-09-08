"""Timesheet logic coverage for ISSUES 1–5 + scenarios 1–16 (unit-level).

Run:  python -m pytest tests/test_timesheet_logic.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.timesheets import AttendanceStatus, DayType, LeavePeriod
from services.timesheets import (
    BillingPolicy,
    LeaveDaySplit,
    TimesheetLeaveClassification,
    _supports_row_lock,
    classify_timesheet_leave_paid_vs_lop,
    comp_off_billed,
    comp_off_earned,
    compute_billables,
    reverse_timesheet_ledger_effects,
    timesheet_summary,
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


def _ts(*, pe_id=10, employee_id=7, year=2026, ts_id=42):
    return SimpleNamespace(
        id=ts_id,
        employee_id=employee_id,
        year=year,
        month=6,
        project_employee_id=pe_id,
        project_id=1,
        comp_off_accrued=D("0"),
    )


def _leave_entry(name="Earned Leave", period=LeavePeriod.FULL, day=1):
    return SimpleNamespace(
        attendance_status=AttendanceStatus.LEAVE,
        leave_type=name,
        leave_period=period,
        entry_date=date(2026, 6, day),
    )


# ---------------------------------------------------------------- ISSUE-1


def test_issue1_week_off_billable_bills_no_credit():
    """week_off_billable > comp_off_billable > credit."""
    saturday = date(2026, 6, 6)
    proj = _project()
    policy = _policy(week_off_billable=True, comp_off_billable=False)
    bh, bd = compute_billables(
        is_working=False, hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF, leave_period=None,
        project=proj, policy=policy,
    )
    assert bh == D("8") and bd == ONE
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_issue1_comp_off_billable_when_week_off_off():
    saturday = date(2026, 6, 6)
    proj = _project()
    policy = _policy(week_off_billable=False, comp_off_billable=True)
    bh, bd = compute_billables(
        is_working=False, hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF, leave_period=None,
        project=proj, policy=policy,
    )
    assert bh == D("8") and bd == ONE
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_issue1_both_flags_off_credits_not_bills():
    saturday = date(2026, 6, 6)
    policy = _policy(week_off_billable=False, comp_off_billable=False)
    bh, bd = compute_billables(
        is_working=False, hours_worked=D("8"),
        attendance_status=AttendanceStatus.WEEK_OFF, leave_period=None,
        project=_project(), policy=policy,
    )
    assert (bh, bd) == (ZERO, ZERO)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ONE
    assert comp_off_billed(entries, policy) == ZERO


def test_issue1_holidays_billable_worked_holiday_no_credit():
    policy = _policy(holidays_billable=True, comp_off_billable=False)
    proj = _project()
    bh, bd = compute_billables(
        is_working=False, hours_worked=D("8"),
        attendance_status=AttendanceStatus.HOLIDAY, leave_period=None,
        project=proj, policy=policy,
    )
    assert bh == D("8") and bd == ONE
    entries = [_entry(
        entry_date=date(2026, 6, 15), hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY, day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_issue1_bill_xor_credit_never_both():
    saturday = date(2026, 6, 6)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    for kw in (
        {"week_off_billable": True},
        {"comp_off_billable": True},
        {"week_off_billable": True, "comp_off_billable": True},
        {},
    ):
        policy = _policy(**kw)
        earned = comp_off_earned(entries, policy)
        billed = comp_off_billed(entries, policy)
        assert not (earned > ZERO and billed > ZERO)


# ---------------------------------------------------------------- ISSUE-2


def test_issue2_reverse_ledger_calls_leave_and_comp_off_release():
    ts = _ts()
    db = MagicMock()
    with patch("services.timesheets.release_timesheet_leaves") as rel_leave, \
         patch("services.timesheets.release_timesheet_comp_off") as rel_co:
        reverse_timesheet_ledger_effects(db, ts)
    rel_leave.assert_called_once_with(db, ts)
    rel_co.assert_called_once_with(db, ts)


# ---------------------------------------------------------------- ISSUE-3


def test_issue3_comp_off_capped_excess_lop():
    comp = SimpleNamespace(id=5, name="Comp-Off")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=5, leave_balance=D("1"))
    ts = _ts()
    entries = [_leave_entry("Comp-Off", day=1), _leave_entry("Comp-Off", day=2)]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = comp
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id.get(5) == D("1")
    assert clf.lop_days_total == D("1")
    assert clf.splits_by_date[date(2026, 6, 1)].paid_days == D("1")
    assert clf.splits_by_date[date(2026, 6, 2)].lop_days == D("1")


# ---------------------------------------------------------------- ISSUE-4


def test_issue4_seed_leave_skips_existing_types():
    """seed_leave_details_from_customer_policy adds missing types only."""
    from services.project_employees import seed_leave_details_from_customer_policy

    pe = SimpleNamespace(id=1, project_id=9, employee_id=7, onboarding_date=date(2026, 1, 1))
    project = SimpleNamespace(id=9, customer_id=3, branch_id=None)
    existing = SimpleNamespace(leave_type_id=11)
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = [existing]
        else:
            result.scalars.return_value.all.return_value = []
        return result

    db.execute.side_effect = execute
    db.get = MagicMock(return_value=project)
    with patch(
        "services.project_employees.resolve_customer_leave_policies",
        # branch_id is required since the branch-policy work: the resolver tags
        # each row "branch" or "customer" by reading it (project_employees.py:479).
        # A stub without it raised AttributeError instead of exercising the skip.
        return_value=[SimpleNamespace(leave_type_id=11, id=1, branch_id=None)],
    ):
        created = seed_leave_details_from_customer_policy(db, pe, project)
    assert created == []


# ---------------------------------------------------------------- ISSUE-5


def test_issue5_sqlite_skips_row_lock():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    assert _supports_row_lock(db) is False


def test_issue5_postgres_enables_row_lock():
    db = MagicMock()
    db.get_bind.return_value = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    assert _supports_row_lock(db) is True


def test_issue5_mock_db_without_dialect_skips_lock():
    db = MagicMock()
    # MagicMock dialect.name is not a str → skip lock (safe for unit tests).
    assert _supports_row_lock(db) is False


# ---------------------------------------------------------------- Scenarios 1–16 (core pure helpers)


def test_scenario1_leave_with_balance_paid():
    earned = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=3, leave_balance=D("5"))
    ts = _ts()
    entries = [_leave_entry(day=1)]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = earned
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id.get(3) == ONE
    assert clf.lop_days_total == ZERO
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE, leave_period=LeavePeriod.FULL,
        project=_project(), policy=_policy(leave_billable=True),
        leave_type="Earned Leave", paid_leave_days=ONE,
    )
    assert bh == D("8") and bd == ONE


def test_scenario2_no_balance_all_lop():
    sick = SimpleNamespace(id=4, name="Sick Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    ts = _ts()
    entries = [_leave_entry("Sick Leave")]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = []
        elif n == 2:
            result.scalars.return_value.first.return_value = sick
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id == {}
    assert clf.lop_days_total == ONE
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE, leave_period=LeavePeriod.FULL,
        project=_project(), policy=_policy(leave_billable=True),
        leave_type="Sick Leave", paid_leave_days=ZERO,
    )
    assert (bh, bd) == (ZERO, ZERO)


def test_scenario3_partial_balance():
    earned = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=3, leave_balance=D("1"))
    ts = _ts()
    entries = [_leave_entry(day=i + 1) for i in range(3)]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = earned
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id.get(3) == ONE
    assert clf.lop_days_total == D("2")


def test_scenario4_half_day_leave():
    earned = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=3, leave_balance=D("5"))
    ts = _ts()
    entries = [_leave_entry(period=LeavePeriod.HALF_AM)]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = earned
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id.get(3) == HALF


def test_scenario5_explicit_lop_non_billable():
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE, leave_period=LeavePeriod.FULL,
        project=_project(), policy=_policy(leave_billable=True),
        leave_type="Loss of Pay", leave_billable_by_type={"Loss of Pay": True},
    )
    assert (bh, bd) == (ZERO, ZERO)


def test_scenario6_per_type_leave_billable():
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE, leave_period=LeavePeriod.FULL,
        project=_project(), policy=_policy(leave_billable=False),
        leave_type="Casual", leave_billable_by_type={"Casual": True},
    )
    assert bh == D("8") and bd == ONE


def test_scenario7_8_weekend_comp_off_billable_on():
    saturday = date(2026, 6, 6)
    policy = _policy(comp_off_billable=True)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_scenario8_weekend_comp_off_billable_off():
    saturday = date(2026, 6, 6)
    policy = _policy(comp_off_billable=False)
    entries = [_entry(entry_date=saturday, hours=D("8"))]
    assert comp_off_earned(entries, policy) == ONE
    assert comp_off_billed(entries, policy) == ZERO


def test_scenario9_holiday_work_same_gate():
    policy = _policy(comp_off_billable=True, holidays_billable=False)
    entries = [_entry(
        entry_date=date(2026, 6, 15), hours=D("8"),
        attendance=AttendanceStatus.HOLIDAY, day_type=DayType.HOLIDAY,
    )]
    assert comp_off_earned(entries, policy) == ZERO
    assert comp_off_billed(entries, policy) == ONE


def test_scenario10_pure_holiday_off():
    bh, bd = compute_billables(
        is_working=False, hours_worked=ZERO,
        attendance_status=AttendanceStatus.HOLIDAY, leave_period=None,
        project=_project(), policy=_policy(holidays_billable=True),
    )
    assert bh == D("8") and bd == ONE


def test_scenario11_comp_off_leave_capped():
    """Apply Comp-Off with balance → paid portion only (ISSUE-3 caps overdraft)."""
    comp = SimpleNamespace(id=5, name="Comp-Off")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=5, leave_balance=D("0.5"))
    ts = _ts()
    entries = [_leave_entry("Comp-Off")]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = comp
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id.get(5) == HALF
    assert clf.lop_days_total == HALF


def test_scenario12_present_8h():
    bh, bd = compute_billables(
        is_working=True, hours_worked=D("8"),
        attendance_status=AttendanceStatus.PRESENT, leave_period=None,
        project=_project(), policy=_policy(),
    )
    assert bh == D("8") and bd == ONE


def test_scenario13_hours_zero_working_day_absent_billables():
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.ABSENT, leave_period=None,
        project=_project(), policy=_policy(),
    )
    assert (bh, bd) == (ZERO, ZERO)


def test_scenario15_billable_leave_full_hours_and_days():
    bh, bd = compute_billables(
        is_working=True, hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE, leave_period=LeavePeriod.FULL,
        project=_project(), policy=_policy(leave_billable=True),
        leave_type="Casual",
    )
    assert bh == D("8") and bd == ONE


# ---------------------------------------------------------------- Summary leave / LOP (read path)


def _live_leave_ns(*, day: int, paid: Decimal, lop: Decimal, billable_days: Decimal):
    """Ephemeral live entry view (as produced by live_entries_from_policy)."""
    return SimpleNamespace(
        id=day,
        timesheet_id=42,
        entry_date=date(2026, 6, day),
        day_of_week="Mon",
        day_type=DayType.WORKING,
        is_working=True,
        hours_worked=ZERO,
        attendance_status=AttendanceStatus.LEAVE,
        leave_type="Earned Leave",
        leave_period=LeavePeriod.FULL,
        billable_hours=D("8") if billable_days > ZERO else ZERO,
        billable_days=billable_days,
        location=None,
        view_flag=None,
        entry_project_id=None,
        paid_leave_days=float(paid),
        lop_leave_days=float(lop),
    )


def test_timesheet_summary_leave_includes_lop_on_draft_read():
    """Unapproved/draft read: 3 paid + 1 LOP → Leave=4, Leave Billable=3, leave-LOP=1.

    Invariant: total_leave_days == total_leave_billable_days + loss_of_pay_from_leave
    when leave types are billable (invoice already excludes LOP hours).
    Absent/Half_Day LOP is separate and does not enter this leave invariant.
    """
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    ts.status = "Draft"
    entries = [_leave_entry(day=i + 1) for i in range(4)]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    live = [
        _live_leave_ns(day=1, paid=ONE, lop=ZERO, billable_days=ONE),
        _live_leave_ns(day=2, paid=ONE, lop=ZERO, billable_days=ONE),
        _live_leave_ns(day=3, paid=ONE, lop=ZERO, billable_days=ONE),
        _live_leave_ns(day=4, paid=ZERO, lop=ONE, billable_days=ZERO),
    ]
    clf = TimesheetLeaveClassification(
        splits_by_date={
            date(2026, 6, 1): LeaveDaySplit(
                entry_date=date(2026, 6, 1), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ONE,
                lop_days=ZERO, is_comp_off=False, is_explicit_lop=False,
            ),
            date(2026, 6, 2): LeaveDaySplit(
                entry_date=date(2026, 6, 2), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ONE,
                lop_days=ZERO, is_comp_off=False, is_explicit_lop=False,
            ),
            date(2026, 6, 3): LeaveDaySplit(
                entry_date=date(2026, 6, 3), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ONE,
                lop_days=ZERO, is_comp_off=False, is_explicit_lop=False,
            ),
            date(2026, 6, 4): LeaveDaySplit(
                entry_date=date(2026, 6, 4), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ZERO,
                lop_days=ONE, is_comp_off=False, is_explicit_lop=False,
            ),
        },
        paid_required_by_type_id={3: D("3")},
        lop_days_total=ONE,
        lop_type_id=99,
    )
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {"Earned Leave": True}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=clf,
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 4.0
    assert summary["total_leave_billable_days"] == 3.0
    assert summary["loss_of_pay_from_leave"] == 1.0
    assert summary["loss_of_pay_from_absent"] == 0.0
    assert summary["loss_of_pay_from_half_day"] == 0.0
    assert summary["total_loss_of_pay_days"] == 1.0
    assert summary["total_leave_paid_days"] == 3.0
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )


def test_timesheet_summary_half_day_lop_fraction():
    """Half-day over-balance leave → 0.5 in both total_leave_days and leave-LOP."""
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    entries = [_leave_entry(period=LeavePeriod.HALF_AM, day=1)]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    live = [SimpleNamespace(
        id=1, timesheet_id=42, entry_date=date(2026, 6, 1),
        day_of_week="Mon", day_type=DayType.WORKING, is_working=True,
        hours_worked=ZERO, attendance_status=AttendanceStatus.LEAVE,
        leave_type="Sick Leave", leave_period=LeavePeriod.HALF_AM,
        billable_hours=ZERO, billable_days=ZERO, location=None,
        view_flag=None, entry_project_id=None,
        paid_leave_days=0.0, lop_leave_days=0.5,
    )]
    clf = TimesheetLeaveClassification(
        splits_by_date={
            date(2026, 6, 1): LeaveDaySplit(
                entry_date=date(2026, 6, 1), leave_type_id=4,
                leave_type_name="Sick Leave", req_days=HALF, paid_days=ZERO,
                lop_days=HALF, is_comp_off=False, is_explicit_lop=False,
            ),
        },
        paid_required_by_type_id={},
        lop_days_total=HALF,
        lop_type_id=99,
    )
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {"Sick Leave": True}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=clf,
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 0.5
    assert summary["loss_of_pay_from_leave"] == 0.5
    assert summary["total_loss_of_pay_days"] == 0.5
    assert summary["total_leave_billable_days"] == 0.0
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )


def _live_ns(
    *,
    day: int,
    attendance,
    hours=ZERO,
    billable_hours=ZERO,
    billable_days=ZERO,
    day_type=DayType.WORKING,
    is_working=True,
    leave_type=None,
    leave_period=None,
):
    return SimpleNamespace(
        id=day,
        timesheet_id=42,
        entry_date=date(2026, 6, day),
        day_of_week="Mon",
        day_type=day_type,
        is_working=is_working,
        hours_worked=hours,
        attendance_status=attendance,
        leave_type=leave_type,
        leave_period=leave_period,
        billable_hours=billable_hours,
        billable_days=billable_days,
        location=None,
        view_flag=None,
        entry_project_id=None,
        paid_leave_days=None,
        lop_leave_days=None,
    )


def _empty_clf():
    return TimesheetLeaveClassification(
        splits_by_date={},
        paid_required_by_type_id={},
        lop_days_total=ZERO,
        lop_type_id=None,
    )


def test_timesheet_summary_absent_counts_as_lop():
    """Working + Absent → 1.0 LOP; not counted as leave."""
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    entries = [
        SimpleNamespace(
            attendance_status=AttendanceStatus.ABSENT,
            leave_type=None,
            leave_period=None,
            entry_date=date(2026, 6, 18),
            day_type=DayType.WORKING,
        ),
    ]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    live = [
        _live_ns(day=18, attendance=AttendanceStatus.ABSENT, hours=ZERO),
    ]
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=_empty_clf(),
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 0.0
    assert summary["loss_of_pay_from_leave"] == 0.0
    assert summary["loss_of_pay_from_absent"] == 1.0
    assert summary["loss_of_pay_from_half_day"] == 0.0
    assert summary["total_loss_of_pay_days"] == 1.0
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )


def test_timesheet_summary_half_day_attendance_lop():
    """Working + Half_Day → 0.5 LOP; billable half day unchanged in rollup."""
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    entries = [
        SimpleNamespace(
            attendance_status=AttendanceStatus.HALF_DAY,
            leave_type=None,
            leave_period=None,
            entry_date=date(2026, 6, 10),
            day_type=DayType.WORKING,
        ),
    ]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    # 5h worked → half-day billable (0.5 day) — billing math untouched.
    live = [
        _live_ns(
            day=10,
            attendance=AttendanceStatus.HALF_DAY,
            hours=D("5"),
            billable_hours=D("4"),
            billable_days=HALF,
        ),
    ]
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=_empty_clf(),
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 0.0
    assert summary["loss_of_pay_from_half_day"] == 0.5
    assert summary["loss_of_pay_from_absent"] == 0.0
    assert summary["total_loss_of_pay_days"] == 0.5
    assert summary["total_billable_days"] == 0.5
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )


def test_timesheet_summary_combined_lop_sources():
    """Leave LOP + Absent + Half_Day sum; leave invariant uses leave-LOP only."""
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    entries = [
        _leave_entry(day=1),  # full leave → 1.0 LOP (over balance)
        _leave_entry(period=LeavePeriod.HALF_PM, day=2),  # half leave → 0.5 LOP
        SimpleNamespace(
            attendance_status=AttendanceStatus.ABSENT,
            leave_type=None, leave_period=None,
            entry_date=date(2026, 6, 3), day_type=DayType.WORKING,
        ),
        SimpleNamespace(
            attendance_status=AttendanceStatus.HALF_DAY,
            leave_type=None, leave_period=None,
            entry_date=date(2026, 6, 4), day_type=DayType.WORKING,
        ),
        # Week_Off Absent must NOT count
        SimpleNamespace(
            attendance_status=AttendanceStatus.ABSENT,
            leave_type=None, leave_period=None,
            entry_date=date(2026, 6, 5), day_type=DayType.WEEK_OFF,
        ),
    ]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    live = [
        _live_leave_ns(day=1, paid=ZERO, lop=ONE, billable_days=ZERO),
        SimpleNamespace(
            id=2, timesheet_id=42, entry_date=date(2026, 6, 2),
            day_of_week="Tue", day_type=DayType.WORKING, is_working=True,
            hours_worked=ZERO, attendance_status=AttendanceStatus.LEAVE,
            leave_type="Earned Leave", leave_period=LeavePeriod.HALF_PM,
            billable_hours=ZERO, billable_days=ZERO, location=None,
            view_flag=None, entry_project_id=None,
            paid_leave_days=0.0, lop_leave_days=0.5,
        ),
        _live_ns(day=3, attendance=AttendanceStatus.ABSENT),
        _live_ns(
            day=4, attendance=AttendanceStatus.HALF_DAY,
            hours=D("5"), billable_hours=D("4"), billable_days=HALF,
        ),
        _live_ns(
            day=5, attendance=AttendanceStatus.ABSENT,
            day_type=DayType.WEEK_OFF, is_working=False,
        ),
    ]
    clf = TimesheetLeaveClassification(
        splits_by_date={
            date(2026, 6, 1): LeaveDaySplit(
                entry_date=date(2026, 6, 1), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ZERO,
                lop_days=ONE, is_comp_off=False, is_explicit_lop=False,
            ),
            date(2026, 6, 2): LeaveDaySplit(
                entry_date=date(2026, 6, 2), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=HALF, paid_days=ZERO,
                lop_days=HALF, is_comp_off=False, is_explicit_lop=False,
            ),
        },
        paid_required_by_type_id={},
        lop_days_total=D("1.5"),
        lop_type_id=99,
    )
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {"Earned Leave": True}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=clf,
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 1.5  # leave rows only
    assert summary["loss_of_pay_from_leave"] == 1.5
    assert summary["loss_of_pay_from_absent"] == 1.0
    assert summary["loss_of_pay_from_half_day"] == 0.5
    assert summary["total_loss_of_pay_days"] == 3.0  # 1.5 + 1 + 0.5
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )
    # Total LOP is NOT part of the leave invariant
    assert summary["total_leave_days"] != summary["total_loss_of_pay_days"]


def test_timesheet_summary_paid_leave_zero_lop():
    """Paid leave with balance → 0 leave-LOP; still no attendance LOP."""
    ts = _ts()
    ts.approved_at = None
    ts.approved_by = None
    ts.rejection_reason = None
    ts.file_attachment_url = None
    entries = [_leave_entry(day=1)]
    proj = SimpleNamespace(
        max_billable_hours_day=D("8"),
        max_billable_hours_month=None,
        max_billable_days_month=None,
    )
    policy = _policy(leave_billable=True)
    live = [_live_leave_ns(day=1, paid=ONE, lop=ZERO, billable_days=ONE)]
    clf = TimesheetLeaveClassification(
        splits_by_date={
            date(2026, 6, 1): LeaveDaySplit(
                entry_date=date(2026, 6, 1), leave_type_id=3,
                leave_type_name="Earned Leave", req_days=ONE, paid_days=ONE,
                lop_days=ZERO, is_comp_off=False, is_explicit_lop=False,
            ),
        },
        paid_required_by_type_id={3: ONE},
        lop_days_total=ZERO,
        lop_type_id=None,
    )
    db = MagicMock()
    with patch(
        "services.timesheets.live_entries_from_policy",
        return_value=(proj, policy, {"Earned Leave": True}, live),
    ), patch(
        "services.timesheets.classify_timesheet_leave_paid_vs_lop",
        return_value=clf,
    ), patch(
        "services.timesheets._user_display_name",
        return_value=None,
    ):
        summary = timesheet_summary(db, ts, entries)

    assert summary["total_leave_days"] == 1.0
    assert summary["total_leave_billable_days"] == 1.0
    assert summary["loss_of_pay_from_leave"] == 0.0
    assert summary["total_loss_of_pay_days"] == 0.0
    assert (
        summary["total_leave_days"]
        == summary["total_leave_billable_days"] + summary["loss_of_pay_from_leave"]
    )
