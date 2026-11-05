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
    assert out["2026-09"] == {"cash_in": 100.0, "cash_mapped": 100.0, "cash_on_account": 0.0}
    assert out["2026-10"] == {"cash_in": 80.0, "cash_mapped": 50.0, "cash_on_account": 30.0}


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
