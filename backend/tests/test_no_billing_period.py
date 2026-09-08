"""Initial no-billing period enforcement (services/timesheets.py, 26 Aug 2026).

Pins that the "Initial No Billing Period" project setting — captured on the
New Project form since 0008 but never read by any billing engine — now
actually zero-bills the employee's free ramp-up window at invoice time,
anchored on EACH employee's own onboarding date (user decision).

Run:  cd backend && python -m pytest tests/test_no_billing_period.py -q
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from services.timesheets import initial_no_billing_end


class _FakeDb:
    def get(self, *a, **k):
        return None


def _project(**kw):
    base = dict(
        branch_id=None,
        opportunity_id=None,
        customer_id=None,
        is_initial_no_billing_period=True,
        initial_no_billing_qty=2,
        initial_no_billing_period="Week",
        weekoff_billable=None, leave_billable=None, holidays_billable=None,
        comp_off_billable=None, hours_required_full_day=None,
        hours_required_half_day=None, week_off_days=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _pe(onboarding=date(2026, 9, 1)):
    return SimpleNamespace(onboarding_date=onboarding)


def test_two_weeks_from_onboarding():
    end = initial_no_billing_end(_FakeDb(), _project(), _pe(date(2026, 9, 1)))
    assert end == date(2026, 9, 15)  # exclusive: 1st–14th free, 15th bills


def test_units_days_month_year_hours():
    db = _FakeDb()
    assert initial_no_billing_end(
        db, _project(initial_no_billing_qty=10, initial_no_billing_period="Days"),
        _pe(date(2026, 9, 1))) == date(2026, 9, 11)
    assert initial_no_billing_end(
        db, _project(initial_no_billing_qty=1, initial_no_billing_period="Month"),
        _pe(date(2026, 1, 31))) == date(2026, 2, 28)  # clamped to month length
    assert initial_no_billing_end(
        db, _project(initial_no_billing_qty=1, initial_no_billing_period="Year"),
        _pe(date(2026, 9, 1))) == date(2027, 9, 1)
    # Hours round UP to whole free days: 30h -> 2 days.
    assert initial_no_billing_end(
        db, _project(initial_no_billing_qty=30, initial_no_billing_period="Hours"),
        _pe(date(2026, 9, 1))) == date(2026, 9, 3)


def test_disabled_or_incomplete_config_is_none():
    db = _FakeDb()
    assert initial_no_billing_end(db, _project(is_initial_no_billing_period=False), _pe()) is None
    assert initial_no_billing_end(db, _project(initial_no_billing_qty=None), _pe()) is None
    assert initial_no_billing_end(db, _project(initial_no_billing_qty=0), _pe()) is None
    # No onboarding date -> no anchor -> no window (never guess).
    assert initial_no_billing_end(db, _project(), SimpleNamespace(onboarding_date=None)) is None
    assert initial_no_billing_end(db, None, _pe()) is None
    # Unknown unit string degrades to None rather than a wrong window.
    assert initial_no_billing_end(
        db, _project(initial_no_billing_period="Fortnight"), _pe()) is None
