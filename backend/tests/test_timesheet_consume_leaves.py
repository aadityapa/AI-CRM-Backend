"""Unit tests: consume_timesheet_leaves paid/LOP split + idempotent deduction.

Run:  python -m pytest tests/test_timesheet_consume_leaves.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from models.timesheets import AttendanceStatus, LeavePeriod
from services.timesheets import consume_timesheet_leaves

D = Decimal
ONE = D("1")
TWO = D("2")
FIVE = D("5")


def _leave_entry(*, leave_type="Casual Leave", period=LeavePeriod.FULL, day=1):
    return SimpleNamespace(
        attendance_status=AttendanceStatus.LEAVE,
        leave_type=leave_type,
        leave_period=period,
        entry_date=date(2026, 6, day),
    )


def _ts(*, ts_id=42, employee_id=7, year=2026, pe_id=None):
    return SimpleNamespace(
        id=ts_id,
        employee_id=employee_id,
        year=year,
        month=6,
        project_employee_id=pe_id,
    )


def test_consume_timesheet_leaves_debits_employee_balance_once():
    """First approval consumes 1 day; re-run with same entries applies delta 0."""
    lt = SimpleNamespace(id=3, name="Casual Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    balance = SimpleNamespace(
        employee_id=7, leave_type_id=3, year=2026,
        accrued=D("10"), consumed=D("0"), balance=D("10"), carry_forward=D("0"),
    )
    ts = _ts()
    entries = [_leave_entry()]
    prior_events: list = []

    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        # classify: prior, employee balances, type lookup, lop lookup
        # consume: prior again, then balance row
        if n in (0, 4):
            result.scalars.return_value.all.return_value = list(prior_events)
        elif n == 1:
            result.scalars.return_value.all.return_value = [balance]
        elif n == 2:
            result.scalars.return_value.first.return_value = lt
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        elif n == 5:
            result.scalars.return_value.first.return_value = balance
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute
    db.add = MagicMock()
    db.flush = MagicMock()
    applied = consume_timesheet_leaves(db, ts, entries)
    assert applied.get(3) == ONE
    assert Decimal(balance.consumed) == ONE
    assert Decimal(balance.balance) == D("9")
    assert db.add.called
    event = db.add.call_args[0][0]
    assert event.event_type == "Consumption"
    assert Decimal(event.amount) == D("-1")
    prior_events.append(event)

    # Call 2: same entries → delta 0
    db2 = MagicMock()
    calls2 = {"n": 0}

    def execute2(_stmt):
        result = MagicMock()
        n = calls2["n"]
        calls2["n"] += 1
        if n in (0, 4):
            result.scalars.return_value.all.return_value = list(prior_events)
        elif n == 1:
            result.scalars.return_value.all.return_value = [balance]
        elif n == 2:
            result.scalars.return_value.first.return_value = lt
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db2.execute.side_effect = execute2
    db2.add = MagicMock()
    applied2 = consume_timesheet_leaves(db2, ts, entries)
    assert applied2 == {}
    assert Decimal(balance.consumed) == ONE
    assert not db2.add.called


def test_consume_timesheet_leaves_skips_non_leave_entries():
    ts = _ts()
    entries = [SimpleNamespace(
        attendance_status=AttendanceStatus.PRESENT,
        leave_type="",
        leave_period=None,
        entry_date=date(2026, 6, 1),
    )]
    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        # prior + balances + lop lookup + prior again
        result.scalars.return_value.all.return_value = []
        result.scalars.return_value.first.return_value = SimpleNamespace(
            id=99, name="Loss of Pay",
        )
        return result

    db.execute.side_effect = execute
    db.add = MagicMock()
    assert consume_timesheet_leaves(db, ts, entries) == {}


def test_consume_earned_5_take_7_splits_to_lop():
    """Earned 5, take 7 → consume 5 earned + 2 LOP; floor earned at 0."""
    earned = SimpleNamespace(id=3, name="Earned Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_earned = SimpleNamespace(
        leave_type_id=3, leave_balance=FIVE, leave_consumed=D("0"),
    )
    pe_earned_after = SimpleNamespace(leave_balance=D("0"), leave_consumed=FIVE)
    pe_lop_after = SimpleNamespace(leave_balance=D("-2"), leave_consumed=TWO)
    ts = _ts(pe_id=10)
    entries = [_leave_entry(leave_type="Earned Leave", day=i + 1) for i in range(7)]

    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n in (0, 4):
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = [pe_earned]
        elif n == 2:
            result.scalars.return_value.first.return_value = earned
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute
    db.add = MagicMock()
    db.flush = MagicMock()
    db.get = MagicMock(side_effect=lambda _m, i: earned if i == 3 else lop)

    with patch("services.timesheets.ensure_loss_of_pay_type", return_value=lop):
        with patch(
            "services.project_employees.consume_pe_leave",
            side_effect=[pe_earned_after, pe_lop_after],
        ) as mock_consume:
            applied = consume_timesheet_leaves(db, ts, entries)

    assert applied.get(3) == FIVE
    assert applied.get(99) == TWO
    assert mock_consume.call_count == 2
    by_type = {c.args[2]: c for c in mock_consume.call_args_list}
    assert by_type[3].args[3] == FIVE
    assert by_type[3].kwargs.get("allow_negative") is False
    assert by_type[99].args[3] == TWO
    assert by_type[99].kwargs.get("allow_negative") is True


def test_consume_sick_0_take_1_all_lop():
    sick = SimpleNamespace(id=4, name="Sick Leave")
    lop = SimpleNamespace(id=99, name="Loss of Pay")
    pe_lop_after = SimpleNamespace(leave_balance=D("-1"), leave_consumed=ONE)
    ts = _ts(pe_id=10)
    entries = [_leave_entry(leave_type="Sick Leave", day=1)]

    db = MagicMock()
    calls = {"n": 0}

    def execute(_stmt):
        result = MagicMock()
        n = calls["n"]
        calls["n"] += 1
        if n in (0, 4):
            result.scalars.return_value.all.return_value = []
        elif n == 1:
            result.scalars.return_value.all.return_value = []  # no sick entitlement
        elif n == 2:
            result.scalars.return_value.first.return_value = sick
        elif n == 3:
            result.scalars.return_value.first.return_value = lop
        else:
            result.scalars.return_value.all.return_value = []
            result.scalars.return_value.first.return_value = None
        return result

    db.execute.side_effect = execute
    db.add = MagicMock()
    db.flush = MagicMock()
    db.get = MagicMock(return_value=lop)

    with patch("services.timesheets.ensure_loss_of_pay_type", return_value=lop):
        with patch(
            "services.project_employees.consume_pe_leave",
            return_value=pe_lop_after,
        ) as mock_consume:
            applied = consume_timesheet_leaves(db, ts, entries)

    assert 4 not in applied  # no sick debit
    assert applied.get(99) == ONE
    assert mock_consume.call_args.kwargs.get("allow_negative") is True
