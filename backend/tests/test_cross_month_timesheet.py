"""Timesheet periods that are not the calendar month (19 Oct 2026).

The business bills some customers on a 21st–20th cycle: the March timesheet runs
20 Feb – 20 Mar, April 21 Mar – 20 Apr, and a long bridge sheet (May: 21 Apr –
30 May) takes them back to calendar months. Rules pinned here:

* the period may cross months, must touch its own month, ≤ MAX_PERIOD_DAYS,
  and never shares a day with another sheet of the same employee + project;
* the day grid follows the window across the month boundary;
* a Monthly rate bills ONE month for a window up to one month long, and one
  month + the extra days for a longer one (user decision);
* a new sheet follows on from last month's (starts the day after it ended,
  and keeps the cycle length when last month's was exactly one cycle).

Run:  cd backend && python -m pytest tests/test_cross_month_timesheet.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services.timesheets import (  # noqa: E402
    add_months, billing_cycle_factor, is_cross_month, period_window_error,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------- pure rules

def test_period_rules_are_plain():
    assert period_window_error(2026, 3, date(2026, 2, 20), date(2026, 3, 20)) is None
    assert period_window_error(2026, 5, date(2026, 4, 21), date(2026, 5, 30)) is None
    assert "before the start" in period_window_error(2026, 3, date(2026, 3, 20), date(2026, 3, 1))
    assert "at most" in period_window_error(2026, 3, date(2026, 1, 1), date(2026, 3, 31))
    # A window that never touches March is not March's sheet.
    assert "March 2026" in period_window_error(2026, 3, date(2026, 2, 1), date(2026, 2, 28))
    assert is_cross_month(2026, 3, date(2026, 2, 20), date(2026, 3, 20))
    assert not is_cross_month(2026, 3, date(2026, 3, 5), date(2026, 3, 20))
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)


def test_monthly_cycle_factor_is_one_month_plus_extra_days():
    # Up to one month = one monthly rate.
    assert billing_cycle_factor(date(2026, 2, 20), date(2026, 3, 19)) == Decimal(1)
    assert billing_cycle_factor(date(2026, 3, 21), date(2026, 4, 20)) == Decimal(1)
    # Shorter than a cycle (still crossing a month) = that share of itself = 1.
    assert billing_cycle_factor(date(2026, 2, 25), date(2026, 3, 10)) == Decimal(1)
    # 21 Apr – 30 May: cycle 21 Apr – 20 May is 30 days, 10 extra → 40/30.
    f = billing_cycle_factor(date(2026, 4, 21), date(2026, 5, 30))
    assert f == Decimal(40) / Decimal(30)
    # An initial no-billing window takes its days off the same way.
    assert billing_cycle_factor(date(2026, 3, 21), date(2026, 4, 20),
                                date(2026, 4, 5)) == Decimal(16) / Decimal(31)


# ---------------------------------------------------------------- the API

def _seed(db, *, unit="Monthly", rate=300000):
    from models import (
        BillingUnit, Customer, CustomerBillingPolicy, Employee, Project, ProjectEmployee,
    )
    cust = Customer(name="Uno Minda")
    db.add(cust)
    db.flush()
    db.add(CustomerBillingPolicy(customer_id=cust.id, leave_billable=True,
                                 week_off_billable=False, holidays_billable=False,
                                 min_hours_full_day=8, min_hours_half_day=4))
    project = Project(name="CREAT T&M", customer_id=cust.id)
    emp = Employee(first_name="Aakash", last_name="Malwade", email="aakash@karnex.in")
    db.add_all([project, emp])
    db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id,
                         onboarding_date=date(2025, 1, 1), billing_rate=rate,
                         billing_unit=BillingUnit(unit), is_active=True, is_exit=False)
    db.add(pe)
    db.commit()
    return project, emp, pe


def _client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="hr", roles={"HR"})
    return TestClient(app)


def _create(client, project, emp, month, year=2026):
    r = client.post("/api/timesheets", json={
        "project_id": project.id, "employee_id": emp.id,
        "year": year, "month": month, "generate_days": True})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _dates(db, ts_id):
    from models import TimesheetEntry
    return sorted(e.entry_date for e in db.execute(
        select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts_id)).scalars())


def test_a_march_sheet_can_run_20_feb_to_20_mar_and_never_overlaps_february(db):
    project, emp, _pe = _seed(db)
    client = _client(db)
    feb = _create(client, project, emp, 2)
    mar = _create(client, project, emp, 3)

    # February still covers the whole of February → refused, and the reply says why.
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-20", "end_date": "2026-03-20"})
    assert r.status_code == 409, r.text
    assert "February 2026" in r.json()["detail"]

    # Narrow February to 1–19 Feb first; then March may take 20 Feb – 20 Mar.
    r = client.patch(f"/api/timesheets/{feb['id']}/period",
                     json={"start_date": "2026-02-01", "end_date": "2026-02-19"})
    assert r.status_code == 200, r.text
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-20", "end_date": "2026-03-20"})
    assert r.status_code == 200, r.text
    detail = r.json()["data"]
    assert "20-Feb-2026 to 20-Mar-2026" in detail["timesheet_period"]
    dates = _dates(db, mar["id"])
    assert (dates[0], dates[-1], len(dates)) == (date(2026, 2, 20), date(2026, 3, 20), 29)
    # The grid's rows carry the February days too.
    assert detail["entries"][0]["entry_date"] == "2026-02-20"

    # Hours typed on a February day of the March sheet are accepted.
    r = client.post(f"/api/timesheets/{mar['id']}/entries", json=[
        {"entry_date": "2026-02-23", "hours_worked": 8}])
    assert r.status_code == 200, r.text


def test_monthly_rate_bills_one_month_for_a_cycle_and_more_for_a_long_window(db):
    from models import Timesheet
    from services.timesheets import timesheet_invoice_preview

    project, emp, _pe = _seed(db)
    client = _client(db)
    mar = _create(client, project, emp, 3)
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-20", "end_date": "2026-03-19"})
    assert r.status_code == 200, r.text
    line = timesheet_invoice_preview(db, db.get(Timesheet, mar["id"]))["line_items"][0]
    assert line["total_billed_qty"] == 1.0
    assert line["amount"] == 300000.0

    may = _create(client, project, emp, 5)
    r = client.patch(f"/api/timesheets/{may['id']}/period",
                     json={"start_date": "2026-04-21", "end_date": "2026-05-30"})
    assert r.status_code == 200, r.text
    line = timesheet_invoice_preview(db, db.get(Timesheet, may["id"]))["line_items"][0]
    assert line["total_billed_qty"] == pytest.approx(1.3333, abs=0.0001)
    assert line["amount"] == pytest.approx(400000.0, abs=30)


def test_a_new_sheet_follows_on_from_last_months_cycle(db):
    project, emp, _pe = _seed(db)
    client = _client(db)
    mar = _create(client, project, emp, 3)
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-21", "end_date": "2026-03-20"})
    assert r.status_code == 200, r.text

    apr = _create(client, project, emp, 4)
    # 21 Mar – 20 Apr: the day after March ended, one cycle long.
    assert apr["period_start_date"] == "2026-03-21"
    assert apr["period_end_date"] == "2026-04-20"
    dates = _dates(db, apr["id"])
    assert (dates[0], dates[-1]) == (date(2026, 3, 21), date(2026, 4, 20))

    # A sheet whose previous month was an ordinary calendar month starts on the 1st.
    jan = _create(client, project, emp, 1)
    assert jan["period_start_date"] == "2026-01-01"
