"""Separation → Inactive (16 Oct 2026). Run: cd backend && python -m pytest tests/test_employee_separation.py -q"""
from __future__ import annotations

import inspect
from datetime import date
from types import SimpleNamespace

from services import employee_separation as es


def _emp(**kw):
    base = dict(is_active=True, is_resigned=False, last_working_day=None, date_of_resignation=None)
    base.update(kw)
    return SimpleNamespace(**base)


TODAY = date(2026, 10, 16)


def test_a_past_last_working_day_makes_the_employee_inactive_now():
    e = _emp(last_working_day=date(2026, 10, 15))
    assert es.apply_separation(e, TODAY) is True
    assert e.is_active is False and e.is_resigned is True


def test_the_last_working_day_itself_is_still_active():
    e = _emp(last_working_day=TODAY)
    assert es.apply_separation(e, TODAY) is False
    assert e.is_active is True and e.is_resigned is True


def test_a_resignation_date_alone_marks_resigned_but_keeps_them_active():
    e = _emp(date_of_resignation=date(2026, 10, 1))
    es.apply_separation(e, TODAY)
    assert e.is_active is True and e.is_resigned is True


def test_nothing_ever_reactivates():
    e = _emp(is_active=False)
    assert es.apply_separation(e, TODAY) is False and e.is_active is False


def test_the_job_is_registered_and_the_update_route_applies_the_rule():
    from services import scheduler
    import routers.crm.employees as r
    assert "employee_separations" in scheduler.JOBS
    assert "apply_separation(emp)" in inspect.getsource(r.update_employee)
