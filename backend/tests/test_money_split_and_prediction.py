"""6 Nov 2026 — the CEO Finance tab's bank / TDS split, and why "Predicted" read
below "Billed" (April: 61.8 L predicted against 84.6 L billed).

Root causes pinned here:
* `_deployed_rows` filtered `is_active`, and `exit_project_employee` sets it to
  False — so everyone who had left vanished from past months (prediction,
  people cost, gross margin, headcount by month);
* the prediction used today's rate for every month, not the rate history;
* PO cover at the FY zoom averaged whole financial years (67.5 months).
"""
from __future__ import annotations

from datetime import date

import pytest

from services import money_split
from services.revenue_forecast import month_prediction
from services.revenue_report import (
    Month, Period, _deployed_rows, cost_until, current_monthly_burn, monthly_value_in,
    revenue_report, trailing_monthly_billed,
)
from tests.test_revenue_report import TODAY, db, ledger  # noqa: F401 — fixtures

APR = Month(2026, 4)


# --------------------------------------------------------------------------- money split


def _inv(**kw):
    base = {"id": 1, "sub_total": 100000, "grand_total": 118000, "balance": 118000, "status": "Unpaid",
            "invoice_date": date(2026, 4, 30)}
    base.update(kw)
    return base


def test_an_open_invoice_with_no_tds_booked_expects_it_at_the_settings_rate():
    m = money_split.invoice_money(_inv(), cash=0, tds=None, rate=0.10)
    assert m["revenue_ex"] == 100000 and m["revenue_inc"] == 118000
    assert m["receivable_tds"] == 10000 and m["expected_tds"] == 10000
    assert m["receivable_bank"] == 108000
    assert m["due_bank"] == 108000 and m["due_tds"] == 10000 and m["total_due"] == 118000


def test_booked_tds_and_cash_settle_the_bank_side_and_leave_tds_pending():
    m = money_split.invoice_money(_inv(balance=0, status="Paid"), cash=116000,
                                  tds={"amount": 2000, "received": 0}, rate=0.10)
    assert m["receivable_tds"] == 2000 and m["receivable_bank"] == 116000
    assert m["received_bank"] == 116000 and m["due_bank"] == 0
    assert m["due_tds"] == 2000 and m["total_due"] == 2000, "TDS is pending from the client"
    m = money_split.invoice_money(_inv(balance=0, status="Paid"), cash=116000,
                                  tds={"amount": 2000, "received": 2000}, rate=0.10)
    assert m["received_tds"] == 2000 and m["total_due"] == 0


def test_a_fully_paid_invoice_with_no_tds_booked_had_none():
    m = money_split.invoice_money(_inv(balance=0, status="Paid"), cash=118000, tds=None, rate=0.10)
    assert m["receivable_tds"] == 0 and m["receivable_bank"] == 118000 and m["total_due"] == 0


def test_months_add_up_and_on_account_money_is_not_received():
    invs = [_inv(id=1), _inv(id=2, invoice_date=date(2026, 5, 3))]
    pays = [{"invoice_id": 1, "amount": 50000}, {"invoice_id": None, "amount": 9, "on_account": True}]
    out = money_split.money_by_month([APR, Month(2026, 5)], invs, pays, {}, 0.10)
    assert out["2026-04"]["received_bank"] == 50000 and out["2026-04"]["invoices"] == 1
    assert out["2026-04"]["due_bank"] == 58000
    assert out["2026-05"]["received_bank"] == 0
    t = money_split.totals(list(out.values()))
    assert t["revenue_ex"] == 200000 and t["total_due"] == pytest.approx(118000 - 50000 + 118000)


# --------------------------------------------------------------------------- prediction


def test_a_past_month_is_predicted_at_the_rate_in_force_then():
    d = {"rate": 200000, "unit": "Monthly", "start": date(2026, 1, 1), "exit": None,
         "rates": [(date(2026, 1, 1), 100000, None), (date(2026, 4, 16), 200000, None)]}
    # 15 days of April at 1 L, 15 at 2 L.
    assert monthly_value_in(d, d["start"], None, APR) == pytest.approx(150000)
    assert monthly_value_in(d, d["start"], None, Month(2026, 3)) == pytest.approx(100000)
    # No history → today's rate.
    assert monthly_value_in({**d, "rates": []}, d["start"], None, APR) == pytest.approx(200000)


def test_someone_who_left_still_counts_in_the_months_they_worked(ledger):
    from models import ProjectEmployee
    from services.project_employees import exit_project_employee

    pe = ledger.query(ProjectEmployee).first()
    pe.onboarding_date = date(2026, 1, 1)
    exit_project_employee(ledger, pe, exit_date=date(2026, 6, 30))
    ledger.commit()
    rows = _deployed_rows(ledger)
    assert pe.id in {r["pe_id"] for r in rows}, "an exited assignment is no longer dropped"
    assert month_prediction(rows, APR)["amount"] >= 100000
    assert pe.id not in month_prediction(rows, Month(2026, 7))["people"], "but not after the exit"


def test_po_cover_at_the_fy_zoom_uses_the_last_three_months(ledger):
    fy = revenue_report(ledger, "2026-09", today=TODAY, period_raw="fy")["pipeline"]
    month = revenue_report(ledger, "2026-09", today=TODAY)["pipeline"]
    assert fy["avg_monthly_billed"] == month["avg_monthly_billed"] == 433333.33
    # 8 Nov 2026: cover divides by what the team deployed TODAY bills a month,
    # the same at every zoom; the 3-month invoice average is only the fallback.
    assert fy["cover_basis"] == month["cover_basis"] == "team"
    assert fy["monthly_burn"] == month["monthly_burn"] > 0
    assert fy["po_cover_months"] == month["po_cover_months"] == round(
        fy["active_po_balance"] / fy["monthly_burn"], 1)


def test_po_cover_ignores_quiet_invoice_months_when_a_team_is_deployed():
    """The reported 2,265-month cover: ₹12.69 Cr of PO balance over three months of
    late / catch-up invoicing. The team's own monthly billing is the denominator."""
    team = [{"start": date(2026, 4, 1), "exit": None, "release": None, "rate": 100000,
             "unit": "Monthly", "rates": []},
            {"start": date(2026, 4, 1), "exit": date(2026, 8, 31), "release": None,
             "rate": 90000, "unit": "Monthly", "rates": []},           # left — not in the burn
            {"start": date(2026, 12, 1), "exit": None, "release": None,
             "rate": 80000, "unit": "Monthly", "rates": []}]           # not started yet
    assert current_monthly_burn(team, date(2026, 11, 5)) == pytest.approx(100000)
    raised = [{**team[0], "rates": [(date(2026, 4, 1), 100000, "Monthly"),
                                    (date(2026, 10, 1), 120000, "Monthly")]}]
    assert current_monthly_burn(raised, date(2026, 11, 5)) == pytest.approx(120000)


def test_people_cost_never_counts_the_days_still_to_come():
    """FY 2026-27 used to be charged 12 months of salary against 7 months of
    billing (Gross margin −95 %)."""
    assert cost_until(None, date(2026, 11, 5)) == date(2026, 11, 5)
    assert cost_until(date(2026, 8, 31), date(2026, 11, 5)) == date(2026, 8, 31)
    assert cost_until(date(2027, 1, 31), date(2026, 11, 5)) == date(2026, 11, 5)
    assert cost_until(date(2027, 1, 31), None) == date(2027, 1, 31)
    fy = Period("fy", Month(2026, 9))
    from services.revenue_report import _margin
    person = [{"start": date(2026, 4, 1), "exit": None, "ctc": 1200000, "customer_id": 1,
               "customer": "Acme"}]
    whole = _margin(fy, [], person)["cost"]
    to_today = _margin(fy, [], person, date(2026, 9, 30))["cost"]
    assert whole == pytest.approx(1200000)
    assert to_today == pytest.approx(1200000 * 183 / 365, rel=1e-3)   # 1 Apr – 30 Sep


def test_trailing_average_never_reaches_past_today():
    invs = [{"invoice_date": date(2026, 9, 1), "sub_total": 300000}]
    p = Period("fy", Month(2026, 9))
    assert trailing_monthly_billed(invs, p, date(2026, 9, 18)) == pytest.approx(100000)


def test_headline_flags_a_comparison_that_is_not_like_for_like(ledger, monkeypatch):
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2026-03")
    from services import org_settings
    org_settings._cache = {}  # noqa: SLF001
    try:
        h = revenue_report(ledger, "2026-09", today=TODAY, period_raw="fy")["headline"]
    finally:
        org_settings._cache = {}  # noqa: SLF001 — never leak 2026-03 into later tests
    assert h["comparison_partial"] is True and h["yoy_same_as_previous"] is True


# --------------------------------------------------------------------------- people cost switch


def test_people_cost_switch_hides_every_cost_alert_and_is_in_the_payload(ledger, monkeypatch):
    """8 Nov 2026 (user: "we do not track expense"): with `finance.track_people_cost`
    off the report says so, and no alert compares billing with people cost."""
    from services.revenue_report import _alerts
    monkeypatch.setenv("FINANCE_TRACK_PEOPLE_COST", "false")
    import services.org_settings as org
    monkeypatch.setattr(org, "_cache", {}, raising=False)
    rep = revenue_report(ledger, "2026-09", today=date(2026, 9, 18))
    assert rep["track_people_cost"] is False
    margin = {"loss_making": [{"customer": "HARMAN", "billed": 1, "margin": -5}]}
    by_project = {"loss_making": [{"project": "P1", "billed": 1, "margin": -5}]}
    cash = dict(rep["cashflow"], covers_cost=False)
    args = (Period.parse("month", "2026-09", date(2026, 9, 18)), rep["headline"], rep["targets"],
            rep["by_customer"], rep["ageing"], rep["pipeline"], margin, [], by_project, rep["by_employee"], cash)
    on = {a["key"] for a in _alerts(*args, track_cost=True)}
    off = {a["key"] for a in _alerts(*args, track_cost=False)}
    assert {"loss_making", "loss_making_projects", "cash_shortfall"} <= on
    assert not off & {"loss_making", "loss_making_projects", "cash_shortfall"}


def test_people_cost_is_hidden_by_default():
    from services.org_settings import KEYS
    assert KEYS["finance.track_people_cost"][1] == "false"
