"""Unit tests: timesheet leave over-balance converts to Loss of Pay (no hard reject).

Run:  python -m pytest tests/test_timesheet_leave_balance_guard.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from models.timesheets import AttendanceStatus, LeavePeriod
from services.timesheets import (
    classify_timesheet_leave_paid_vs_lop,
    consume_timesheet_leaves,
    validate_timesheet_leave_balances,
)

D = Decimal


def _ts(*, pe_id=10, employee_id=7, year=2026, ts_id=42):
    return SimpleNamespace(
        id=ts_id,
        employee_id=employee_id,
        year=year,
        month=6,
        project_employee_id=pe_id,
    )


def _leave_entry(name="Earned Leave", period=LeavePeriod.FULL, day=1):
    return SimpleNamespace(
        attendance_status=AttendanceStatus.LEAVE,
        leave_type=name,
        leave_period=period,
        entry_date=date(2026, 6, day),
    )


def _sequenced_db(steps: list):
    """Each step is ('first', val) or ('all', list)."""
    db = MagicMock()
    state = {"i": 0}

    def execute(_stmt):
        result = MagicMock()
        kind, value = steps[state["i"]]
        state["i"] += 1
        if kind == "first":
            result.scalars.return_value.first.return_value = value
            result.scalars.return_value.all.return_value = [value] if value is not None else []
        else:
            result.scalars.return_value.all.return_value = value
            result.scalars.return_value.first.return_value = value[0] if value else None
        return result

    db.execute.side_effect = execute
    db.add = MagicMock()
    db.flush = MagicMock()
    return db


def test_validate_allows_over_balance_no_reject():
    """Over-balance leave is allowed (converts to LOP on consume) — no 400."""
    lt = SimpleNamespace(id=3, name="Earned Leave")
    ts = _ts()
    entries = [_leave_entry(day=i + 1) for i in range(6)]
    db = _sequenced_db([("first", lt)])
    validate_timesheet_leave_balances(db, ts, entries)


def test_validate_allows_exact_balance():
    lt = SimpleNamespace(id=3, name="Earned Leave")
    ts = _ts()
    entries = [_leave_entry(day=i + 1) for i in range(5)]
    db = _sequenced_db([("first", lt)])
    validate_timesheet_leave_balances(db, ts, entries)


def test_validate_allows_zero_entitlement_as_lop():
    """Sick 0 take 1 — soft validate passes; classify marks as LOP."""
    lt = SimpleNamespace(id=4, name="Sick Leave")
    ts = _ts()
    entries = [_leave_entry("Sick Leave")]
    db = _sequenced_db([("first", lt)])
    validate_timesheet_leave_balances(db, ts, entries)


def test_validate_unknown_leave_type_still_raises():
    ts = _ts()
    entries = [_leave_entry("NoSuchType")]
    # resolve: candidate SQL miss, then full-table stem scan miss
    db = _sequenced_db([("first", None), ("all", [])])
    with pytest.raises(HTTPException) as ei:
        validate_timesheet_leave_balances(db, ts, entries)
    assert ei.value.status_code == 400
    assert "Unknown leave type" in str(ei.value.detail)


def test_validate_resolves_casual_stem_to_casual_leave():
    """Entries may store stem 'casual' while policy types are 'Casual Leave'."""
    lt = SimpleNamespace(id=1, name="Casual Leave")
    ts = _ts()
    entries = [_leave_entry("casual")]
    db = _sequenced_db([("first", lt)])
    validate_timesheet_leave_balances(db, ts, entries)


def test_classify_earned_5_take_7_splits_paid_and_lop():
    earned = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(leave_type_id=3, leave_balance=D("5"))
    ts = _ts()
    entries = [_leave_entry(day=i + 1) for i in range(7)]
    db = _sequenced_db([
        ("all", []),          # prior consumption
        ("all", [pe_row]),    # PE leave details
        ("first", earned),    # type lookup (cached after first)
        ("first", lop),       # LOP lookup in classify
    ])
    # First entry looks up Earned; subsequent use cache — but our mock advances
    # per execute. Re-build with enough earned lookups + one LOP.
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        # 0: prior events, 1: PE balances, 2+: leave type lookups (earned then lop)
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
    assert clf.paid_required_by_type_id.get(3) == D("5")
    assert clf.lop_days_total == D("2")


def test_classify_sick_0_take_1_all_lop():
    sick = SimpleNamespace(id=4, name="Sick Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    ts = _ts()
    entries = [_leave_entry("Sick Leave", day=1)]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n == 0:
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = []  # no PE rows → avail 0
        elif n == 2:
            result.scalars.return_value.first.return_value = sick
        else:
            result.scalars.return_value.first.return_value = lop
        return result

    db.execute.side_effect = execute
    clf = classify_timesheet_leave_paid_vs_lop(db, ts, entries)
    assert clf.paid_required_by_type_id == {}
    assert clf.lop_days_total == D("1")
    split = clf.splits_by_date[date(2026, 6, 1)]
    assert split.paid_days == D("0")
    assert split.lop_days == D("1")


def test_consume_allow_negative_false_for_earned_paid_portion():
    lt = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(
        leave_type_id=3, leave_balance=D("5"), leave_consumed=D("0"),
    )
    pe_after = SimpleNamespace(leave_balance=D("4"), leave_consumed=D("1"))
    ts = _ts()
    entries = [_leave_entry()]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n in (0,):
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_row]
        elif n == 2:
            result.scalars.return_value.first.return_value = lt
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        else:
            # prior consumption inside consume (classify already ran)
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute
    db.get = MagicMock(return_value=lt)
    db.add = MagicMock()
    db.flush = MagicMock()
    with patch("services.project_employees.consume_pe_leave", return_value=pe_after) as mock_consume:
        consume_timesheet_leaves(db, ts, entries)
    assert mock_consume.called
    assert mock_consume.call_args.kwargs.get("allow_negative") is False


def test_consume_allow_negative_false_when_comp_off_has_balance():
    """ISSUE-3: Comp-Off paid portion consumes with allow_negative=False."""
    lt = SimpleNamespace(id=5, name="Comp-Off")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_row = SimpleNamespace(
        leave_type_id=5, leave_balance=D("2"), leave_consumed=D("0"),
    )
    pe_after = SimpleNamespace(leave_balance=D("1"), leave_consumed=D("1"))
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
            result.scalars.return_value.first.return_value = lt
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute
    db.get = MagicMock(return_value=lt)
    db.add = MagicMock()
    db.flush = MagicMock()
    with patch("services.project_employees.consume_pe_leave", return_value=pe_after) as mock_consume:
        consume_timesheet_leaves(db, ts, entries)
    assert mock_consume.called
    assert mock_consume.call_args.kwargs.get("allow_negative") is False
    assert mock_consume.call_args.args[2] == 5  # Comp-Off type id


def test_classify_comp_off_excess_becomes_lop():
    """ISSUE-3: Comp-Off with balance 1, apply 2 → paid 1 + LOP 1."""
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
