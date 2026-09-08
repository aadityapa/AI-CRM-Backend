"""No timesheet may exist for a FUTURE month (31 Aug 2026 user bug report:
a December-2025 sheet created with year 2026 sat four months in the future
while the real December 2025 still showed as Due).

Run:  cd backend && python -m pytest tests/test_timesheet_future_period.py -q
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from routers.crm.timesheets import _reject_future_period


def test_current_month_is_allowed():
    t = date.today()
    _reject_future_period(t.month, t.year)  # must not raise


def test_past_months_are_allowed():
    _reject_future_period(12, 2025)
    _reject_future_period(1, 2000)


def test_next_year_same_month_is_rejected_with_year_hint():
    t = date.today()
    with pytest.raises(HTTPException) as e:
        _reject_future_period(t.month, t.year + 1)
    assert e.value.status_code == 400
    assert "future" in e.value.detail
    assert str(t.year) in e.value.detail  # "did you mean <last valid year>?"


def test_future_month_this_year_is_rejected():
    t = date.today()
    if t.month == 12:
        pytest.skip("December — no future month left in this year")
    with pytest.raises(HTTPException) as e:
        _reject_future_period(12, t.year)
    assert e.value.status_code == 400
