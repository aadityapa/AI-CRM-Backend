"""5 Nov 2026 — cash received month + "mapping changed after the month closed" (PURE)."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

from services import cash_month


def _m(key, start, end):
    return SimpleNamespace(key=key, start=start, end=end)


SEP = _m("2026-09", date(2026, 9, 1), date(2026, 9, 30))
OCT = _m("2026-10", date(2026, 10, 1), date(2026, 10, 31))


def test_cash_by_month_adds_money_still_on_account_once():
    payments = [{"date": date(2026, 9, 10), "amount": 100.0}, {"date": date(2026, 10, 2), "amount": 50.0}]
    held = [{"date": date(2026, 10, 20), "amount": 30.0}]
    out = cash_month.cash_by_month([SEP, OCT], payments, held)
    assert out["2026-09"] == {"cash_in": 100.0, "cash_mapped": 100.0, "cash_on_account": 0.0,
                              "cash_this_period": 100.0, "cash_earlier": 0.0}
    assert out["2026-10"] == {"cash_in": 80.0, "cash_mapped": 50.0, "cash_on_account": 30.0,
                              "cash_this_period": 50.0, "cash_earlier": 0.0}


def test_mapping_changes_flags_only_payments_recorded_after_the_month_ended():
    payments = [
        # counted in September, recorded in September -> no note
        {"date": date(2026, 9, 1), "amount": 10.0, "mapped_at": datetime(2026, 9, 20, tzinfo=timezone.utc)},
        # counted in September, mapped on 5 Oct -> note
        {"date": date(2026, 9, 1), "amount": 40.0, "mapped_at": datetime(2026, 10, 5, tzinfo=timezone.utc)},
        {"date": date(2026, 9, 1), "amount": 60.0, "mapped_at": datetime(2026, 10, 28, tzinfo=timezone.utc)},
        # no timestamp (old rows) -> ignored
        {"date": date(2026, 10, 1), "amount": 5.0, "mapped_at": None},
    ]
    out = cash_month.mapping_changes([SEP, OCT], payments)
    assert out == {"2026-09": {"mapping_changed_on": "2026-10-28", "mapped_later": 100.0}}


def test_cash_that_pays_earlier_invoices_is_split_out():
    """20 Nov 2026 — Mapped ₹3.80 Cr vs Received ₹2.98 Cr: the cash view also
    holds money that arrived this FY for invoices of an EARLIER work month."""
    payments = [
        {"date": date(2026, 9, 10), "amount": 100.0, "work": date(2026, 8, 1)},   # this FY's invoice
        {"date": date(2026, 9, 12), "amount": 40.0, "work": date(2026, 3, 1)},    # March work, before FY
        {"date": date(2026, 10, 2), "amount": 25.0, "work": None},                # unknown -> this period
    ]
    out = cash_month.cash_by_month([SEP, OCT], payments, [], period_start=date(2026, 4, 1))
    assert out["2026-09"]["cash_mapped"] == 140.0
    assert out["2026-09"]["cash_this_period"] == 100.0
    assert out["2026-09"]["cash_earlier"] == 40.0
    assert out["2026-10"]["cash_earlier"] == 0.0


def test_due_breakdown_nets_money_on_account_and_names_expected_tds():
    money = {"total_due": 309.0, "due_tds": 42.0, "expected_tds": 41.5}
    out = cash_month.due_breakdown(money, {"amount": 121.0})
    assert out == {"due_tds_expected": 41.5, "due_tds_booked": 0.5,
                   "on_account": 121.0, "total_due_net": 188.0}
    # more on account than due -> never negative; out of scope -> nothing netted
    assert cash_month.due_breakdown({"total_due": 10.0}, {"amount": 50.0})["total_due_net"] == 0.0
    assert cash_month.due_breakdown(money, {"amount": 121.0, "out_of_scope": True})["total_due_net"] == 309.0
