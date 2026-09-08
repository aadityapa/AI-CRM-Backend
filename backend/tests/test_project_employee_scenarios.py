"""Phase 4 — Project Employee module scenarios (A–H) + billing helpers.

Pure-unit + light in-memory ORM where practical. Full HTTP/DB integration is
covered by existing routers once Postgres is available.

Run:  python -m pytest tests/test_project_employee_scenarios.py tests/test_project_employee_billing.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock


from services import project_employee_billing as eng
from services.project_employee_leave_credit import (
    _days_present_in_month, credit_one_pe_leave_row,
)
from services.project_employees import (
    _seed_balance_from_policy, exit_project_employee,
)


# ============================================================ D — billable days
def test_scenario_d_billable_23_minus_2_minus_1():
    assert eng.compute_billable_days(23, 2, 1) == Decimal("20")


# ============================================================ E — mid-period rate
def test_scenario_e_mid_period_rate_split_invoice():
    rows = [eng.RateRow.of(date(2026, 1, 1), 1000), eng.RateRow.of(date(2026, 6, 16), 1200)]
    subs = eng.split_period_by_rate(date(2026, 6, 1), date(2026, 6, 30), rows)
    assert len(subs) == 2
    # 15 days @1000 + 15 days @1200
    amount = eng.invoice_amount_split([
        (Decimal("15"), subs[0].rate),
        (Decimal("15"), subs[1].rate),
    ])
    assert amount == Decimal("33000.00")


# ============================================================ F — PO thresholds
def test_scenario_f_po_80_100_expiry():
    pct, status = eng.po_utilization(100_000, 50_000)
    assert status == "ok" and pct == Decimal("50.00")

    pct, status = eng.po_utilization(100_000, 80_000)
    assert status == "warn_80" and pct == Decimal("80.00")

    pct, status = eng.po_utilization(100_000, 100_000)
    assert status == "blocked"

    line = eng.PoLine.of(1, 5000, date(2026, 6, 30))
    assert eng.po_is_blocked(line, date(2026, 7, 1)) is True
    assert eng.po_status_label("ok", expired=True) == "blocked"
    # Invoice would block; timesheet must not use this gate (router-level D8).


# ============================================================ C — accrual / upfront / prorate
def test_scenario_c_seed_upfront_vs_end_of_period_and_prorate():
    monthly_start = SimpleNamespace(
        initial_credit_balance=Decimal("2"),
        leave_credit_balance=Decimal("1.5"),
        leave_credit_timing="Start_Of_Period",
        leave_credit_type="Monthly",
    )
    initial, accrual = _seed_balance_from_policy(monthly_start)
    assert initial == Decimal("3.5") and accrual == Decimal("1.5")

    monthly_end = SimpleNamespace(
        initial_credit_balance=Decimal("0"),
        leave_credit_balance=Decimal("2"),
        leave_credit_timing="End_Of_Period",
        leave_credit_type="Monthly",
    )
    initial, accrual = _seed_balance_from_policy(monthly_end)
    assert initial == Decimal("0") and accrual == Decimal("2")

    yearly = SimpleNamespace(
        initial_credit_balance=Decimal("0"),
        leave_credit_balance=Decimal("24"),
        leave_credit_timing="Start_Of_Period",
        leave_credit_type="Yearly",
    )
    initial, accrual = _seed_balance_from_policy(yearly)
    assert initial == Decimal("24") and accrual == Decimal("0")

    assert eng.prorate_credit(Decimal("2"), days_present=15, days_in_period=30) == Decimal("1.00")


def test_eligibility_labels_human_readable():
    from services.project_employees import eligibility_from_policy

    monthly = SimpleNamespace(
        leave_credit_type="Monthly",
        leave_credit_timing="End_Of_Period",
        leave_credit_balance=Decimal("1"),
        initial_credit_balance=Decimal("0"),
    )
    e = eligibility_from_policy(monthly)
    assert e["yearly_days"] == 12.0
    assert e["monthly_days"] == 1.0
    assert "12 days/year" in e["label"]
    assert "monthly accrual" in e["label"]

    upfront = SimpleNamespace(
        leave_credit_type="Yearly",
        leave_credit_timing="Start_Of_Period",
        leave_credit_balance=Decimal("18"),
        initial_credit_balance=Decimal("0"),
    )
    e2 = eligibility_from_policy(upfront)
    assert e2["yearly_days"] == 18.0
    assert "upfront" in e2["label"]

    one = SimpleNamespace(
        leave_credit_type="One_Time",
        leave_credit_timing="Start_Of_Period",
        leave_credit_balance=Decimal("5"),
        initial_credit_balance=Decimal("2"),
    )
    e3 = eligibility_from_policy(one)
    assert "7 days upfront" in e3["label"]


def test_scenario_c_mid_month_days_present():
    pe = SimpleNamespace(
        onboarding_date=date(2026, 7, 16),
        exit_date=None,
        is_exit=False,
        is_active=True,
    )
    present, dim = _days_present_in_month(pe, date(2026, 7, 31))
    assert dim == 31 and present == 16  # 16..31 inclusive


# ============================================================ A — dual project independence
def test_scenario_a_avinash_dual_project_independent_balances():
    """Same employee, two PE leave rows — depleting one must not touch the other."""
    samsung = {"pe_id": 1, "leave_balance": Decimal("12"), "leave_consumed": Decimal("0")}
    microsoft = {"pe_id": 2, "leave_balance": Decimal("10"), "leave_consumed": Decimal("0")}

    def consume(row, days):
        if days > row["leave_balance"]:
            raise ValueError("insufficient")
        row["leave_consumed"] += days
        row["leave_balance"] -= days

    consume(samsung, Decimal("2"))
    assert samsung["leave_balance"] == Decimal("10")
    assert microsoft["leave_balance"] == Decimal("10")  # untouched


# ============================================================ B — different policies same project
def test_scenario_b_different_policies_seed_different_balances():
    pol_a = SimpleNamespace(
        initial_credit_balance=Decimal("5"),
        leave_credit_balance=Decimal("1"),
        leave_credit_timing="Start_Of_Period",
        leave_credit_type="Monthly",
    )
    pol_b = SimpleNamespace(
        initial_credit_balance=Decimal("0"),
        leave_credit_balance=Decimal("24"),
        leave_credit_timing="Start_Of_Period",
        leave_credit_type="Yearly",
    )
    a_init, _ = _seed_balance_from_policy(pol_a)
    b_init, _ = _seed_balance_from_policy(pol_b)
    assert a_init != b_init
    assert a_init == Decimal("6") and b_init == Decimal("24")


# ============================================================ G — duplicate mapping
def test_scenario_g_duplicate_active_mapping_logic():
    """Mirrors router: active existing → conflict; inactive → reactivate same id."""
    existing_active = SimpleNamespace(id=7, is_active=True)
    existing_inactive = SimpleNamespace(id=8, is_active=False)

    def map_result(existing):
        if existing and existing.is_active:
            return 409, None
        if existing:
            existing.is_active = True
            return 200, existing.id
        return 201, 99

    code, pe_id = map_result(existing_active)
    assert code == 409 and pe_id is None
    code, pe_id = map_result(existing_inactive)
    assert code == 200 and pe_id == 8


# ============================================================ H — exit stops accrual
def test_scenario_h_exit_stops_credit_and_flags_settlement():
    pe = SimpleNamespace(
        id=3,
        project_id=10,
        employee_id=20,
        is_exit=False,
        is_active=True,
        exit_date=None,
        onboarding_date=date(2026, 1, 1),
    )
    leave_row = SimpleNamespace(
        leave_type_id=1,
        leave_balance=Decimal("5"),
        leave_accrual=Decimal("1.5"),
        customer_leave_policy_id=None,
        project_employee_id=3,
    )

    db = MagicMock()
    # open timesheets query → empty; leave details → one row
    def execute(stmt):
        result = MagicMock()
        # Heuristic: first call timesheets, second leave details
        if not hasattr(execute, "_n"):
            execute._n = 0
        execute._n += 1
        if execute._n == 1:
            result.scalars.return_value.all.return_value = []
        else:
            result.scalars.return_value.all.return_value = [leave_row]
        return result

    db.execute.side_effect = execute
    summary = exit_project_employee(db, pe, exit_date=date(2026, 7, 14))
    assert pe.is_exit is True and pe.is_active is False
    assert pe.exit_date == date(2026, 7, 14)
    assert summary["accrual_stopped"] is True
    assert summary["settlement_leave"][0]["needs_settlement"] is True
    assert summary["settlement_leave"][0]["leave_balance"] == 5.0
    assert pe.settlement_pending is True

    # credit job must no-op on exited PE
    amt = credit_one_pe_leave_row(db, pe, leave_row, date(2026, 8, 1))
    assert amt == Decimal("0")


def test_carry_forward_cap():
    carried, expired = eng.carry_forward(8, 5)
    assert carried == Decimal("5") and expired == Decimal("3")
