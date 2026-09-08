"""Tests for the Project-Employee billing & leave engine.

Run:  python -m pytest tests/test_project_employee_billing.py -q
"""
from datetime import date
from decimal import Decimal

import pytest

from services import project_employee_billing as eng


# ------------------------------------------------------------- billable days
def test_billable_days_basic():
    # 22 working - 2 leave - 1 holiday, none billable
    assert eng.compute_billable_days(22, 2, 1) == Decimal("19")
    assert eng.compute_billable_days(23, 2, 1) == Decimal("20")


def test_po_utilization_thresholds():
    assert eng.po_utilization(1000, 0)[1] == "ok"
    assert eng.po_utilization(1000, 800)[1] == "warn_80"
    assert eng.po_utilization(1000, 1000)[1] == "blocked"


def test_billable_days_honours_billable_flags():
    assert eng.compute_billable_days(22, 2, 1, leave_billable=True) == Decimal("21")
    assert eng.compute_billable_days(22, 2, 1, holidays_billable=True) == Decimal("20")
    assert eng.compute_billable_days(22, 2, 1, leave_billable=True, holidays_billable=True) == Decimal("22")


def test_billable_days_never_negative():
    assert eng.compute_billable_days(5, 10, 3) == Decimal("0")


# ----------------------------------------------------------- effective rates
def test_rate_for_date_picks_latest_effective():
    rows = [eng.RateRow.of(date(2026, 1, 1), 1000), eng.RateRow.of(date(2026, 6, 1), 1200)]
    assert eng.rate_for_date(rows, date(2026, 5, 31)) == Decimal("1000.00")
    assert eng.rate_for_date(rows, date(2026, 6, 1)) == Decimal("1200.00")
    assert eng.rate_for_date(rows, date(2025, 12, 31)) is None  # before any rate


def test_split_period_by_rate_across_change():
    rows = [eng.RateRow.of(date(2026, 1, 1), 1000), eng.RateRow.of(date(2026, 6, 15), 1200)]
    subs = eng.split_period_by_rate(date(2026, 6, 1), date(2026, 6, 30), rows)
    assert len(subs) == 2
    assert subs[0].start == date(2026, 6, 1) and subs[0].end == date(2026, 6, 14) and subs[0].rate == Decimal("1000.00")
    assert subs[1].start == date(2026, 6, 15) and subs[1].end == date(2026, 6, 30) and subs[1].rate == Decimal("1200.00")


def test_invoice_amount_uniform_and_split():
    assert eng.invoice_amount_uniform(19, 1000) == Decimal("19000.00")
    # 9 days @1000 + 10 days @1200
    assert eng.invoice_amount_split([(Decimal("9"), Decimal("1000")), (Decimal("10"), Decimal("1200"))]) == Decimal("21000.00")


# --------------------------------------------------------------- leave ledger
def test_leave_balance_from_ledger():
    entries = [
        eng.LeaveLedgerEntry.of("credit", 12, date(2026, 1, 1)),
        eng.LeaveLedgerEntry.of("consume", 2, date(2026, 3, 10)),
        eng.LeaveLedgerEntry.of("credit", 1, date(2026, 4, 1)),
        eng.LeaveLedgerEntry.of("expire", 1, date(2026, 12, 31)),
    ]
    assert eng.leave_balance(entries) == Decimal("10")
    # as-of before the expiry
    assert eng.leave_balance(entries, as_of=date(2026, 6, 1)) == Decimal("11")


def test_leave_ledger_rejects_bad_type():
    with pytest.raises(ValueError):
        eng.LeaveLedgerEntry.of("bogus", 1, date(2026, 1, 1))


# ---------------------------------------------------------- accrual & rollover
def test_monthly_accrual():
    assert eng.monthly_accrual(24) == Decimal("2.00")
    assert eng.monthly_accrual(30) == Decimal("2.50")


def test_prorate_credit_mid_period():
    # joined on day 16 of a 30-day month -> 15 days present
    assert eng.prorate_credit(Decimal("2"), days_present=15, days_in_period=30) == Decimal("1.00")
    assert eng.prorate_credit(2, 0, 30) == Decimal("0")


def test_carry_forward_cap_and_expiry():
    carried, expired = eng.carry_forward(remaining=8, max_carry_forward=5)
    assert carried == Decimal("5") and expired == Decimal("3")
    carried, expired = eng.carry_forward(3, 5)
    assert carried == Decimal("3") and expired == Decimal("0")


# ------------------------------------------------------------------ PO drawdown
def test_po_blocked_on_expiry_and_exhaustion():
    p_exp = eng.PoLine.of(1, 1000, date(2026, 6, 30))
    assert eng.po_is_blocked(p_exp, date(2026, 7, 1)) is True     # expired
    assert eng.po_is_blocked(p_exp, date(2026, 6, 30)) is False
    assert eng.po_is_blocked(eng.PoLine.of(2, 0, None), date(2026, 6, 1)) is True  # exhausted


def test_allocate_drawdown_fifo_by_expiry():
    pos = [
        eng.PoLine.of(1, 5000, date(2026, 12, 31)),
        eng.PoLine.of(2, 5000, date(2026, 6, 30)),   # earlier expiry -> drawn first
    ]
    res = eng.allocate_drawdown(7000, pos, on_date=date(2026, 6, 1))
    assert res.shortfall == Decimal("0")
    assert res.allocations[0].po_id == 2 and res.allocations[0].amount == Decimal("5000.00")
    assert res.allocations[1].po_id == 1 and res.allocations[1].amount == Decimal("2000.00")


def test_allocate_drawdown_skips_blocked_and_reports_shortfall():
    pos = [
        eng.PoLine.of(1, 3000, date(2026, 5, 1)),    # expired on the draw date -> skipped
        eng.PoLine.of(2, 2000, date(2026, 12, 31)),
    ]
    res = eng.allocate_drawdown(5000, pos, on_date=date(2026, 6, 1))
    assert [a.po_id for a in res.allocations] == [2]
    assert res.allocations[0].amount == Decimal("2000.00")
    assert res.shortfall == Decimal("3000.00")  # 5000 needed, only 2000 fundable
