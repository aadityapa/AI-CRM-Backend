"""Change period next to another sheet + Fill from file on a cross-month sheet (20 Oct 2026).

Reported after the deploy: moving March to 21 Feb – 20 Mar was refused because
February still covered 1–28 Feb; some sheets that did move showed a wrong
amount; and "Fill from file" did nothing useful on a 21st–20th sheet. Pinned:

* an OPEN February (Draft) gives its 21–28 Feb days to March when the editor
  confirms — the days MOVE with their hours, nothing is re-typed;
* a LOCKED February (submitted / approved) refuses with the date to use;
* a period may not start before the employee joined the project;
* period-options tells the dialog all of this up front;
* bare day numbers "21 … 28 1 … 20" read onto 21 Feb – 20 Mar, and Fill from
  file puts every day of that period on THIS sheet.

Run:  cd backend && python -m pytest tests/test_timesheet_period_neighbours.py -q
"""
from __future__ import annotations

import importlib
import io
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
from services import timesheet_import as tsi  # noqa: E402


# ------------------------------------------------------------------ pure rules

def test_bare_day_numbers_walk_the_period_across_the_month_end():
    row = ["Day"] + list(range(21, 29)) + list(range(1, 21))
    cols = tsi._date_columns(row, 2026, 3, (date(2026, 2, 21), date(2026, 3, 20)))
    assert (cols[0][1], cols[-1][1], len(cols)) == (date(2026, 2, 21), date(2026, 3, 20), 28)
    # Without the window the old reader keeps only the longest straight run (1–20).
    old = tsi._date_columns(row, 2026, 3)
    assert (old[0][1], len(old)) == (date(2026, 3, 1), 20)


def test_read_day_marks_uses_the_window():
    grid = [["Date"] + list(range(21, 29)) + list(range(1, 21)),
            ["Status"] + ["P"] * 28]
    marks = tsi.read_day_marks([grid], 2026, 3, window=(date(2026, 2, 21), date(2026, 3, 20)))
    assert len(marks) == 28 and marks[0].day == date(2026, 2, 21) and marks[0].code == "P"


# ------------------------------------------------------------------ the API

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


def _seed(db, onboarding=date(2025, 1, 1)):
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
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id, onboarding_date=onboarding,
                         billing_rate=300000, billing_unit=BillingUnit("Monthly"),
                         is_active=True, is_exit=False)
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


def _entry(db, ts_id, d):
    from models import TimesheetEntry
    return db.execute(select(TimesheetEntry).where(
        TimesheetEntry.timesheet_id == ts_id, TimesheetEntry.entry_date == d)).scalars().first()


def test_an_open_february_gives_its_days_to_march_with_their_hours(db):
    project, emp, _pe = _seed(db)
    client = _client(db)
    feb = _create(client, project, emp, 2)
    mar = _create(client, project, emp, 3)
    r = client.post(f"/api/timesheets/{feb['id']}/entries",
                    json=[{"entry_date": "2026-02-23", "hours_worked": 6}])
    assert r.status_code == 200, r.text

    # The dialog knows February is open before anything is sent.
    opts = client.get(f"/api/timesheets/{mar['id']}/period-options").json()["data"]
    assert opts["previous"]["label"] == "February 2026" and opts["previous"]["open"] is True

    body = {"start_date": "2026-02-21", "end_date": "2026-03-20"}
    r = client.patch(f"/api/timesheets/{mar['id']}/period", json=body)
    assert r.status_code == 409 and "confirm moving" in r.json()["detail"]

    r = client.patch(f"/api/timesheets/{mar['id']}/period", json={**body, "adjust_neighbours": True})
    assert r.status_code == 200, r.text
    assert "moved over from February 2026" in r.json()["message"]
    # February now ends on 20 Feb; the 23 Feb day — with its 6 hours — is March's.
    feb_detail = client.get(f"/api/timesheets/{feb['id']}").json()["data"]
    assert feb_detail["period_end_date"] == "2026-02-20"
    moved = _entry(db, mar["id"], date(2026, 2, 23))
    assert moved is not None and Decimal(moved.hours_worked) == Decimal(6)
    assert _entry(db, feb["id"], date(2026, 2, 23)) is None


def test_a_locked_february_says_which_day_march_can_start(db):
    from models import Timesheet, TimesheetStatus
    project, emp, _pe = _seed(db)
    client = _client(db)
    feb = _create(client, project, emp, 2)
    mar = _create(client, project, emp, 3)
    db.get(Timesheet, feb["id"]).status = TimesheetStatus.APPROVED
    db.commit()

    opts = client.get(f"/api/timesheets/{mar['id']}/period-options").json()["data"]
    assert opts["previous"]["open"] is False and opts["free_start"] == "2026-03-01"

    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-21", "end_date": "2026-03-20", "adjust_neighbours": True})
    assert r.status_code == 409
    assert "start this sheet on 01-Mar-2026" in r.json()["detail"]


def test_a_period_cannot_start_before_the_employee_joined(db):
    project, emp, _pe = _seed(db, onboarding=date(2026, 3, 5))
    client = _client(db)
    mar = _create(client, project, emp, 3)
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-21", "end_date": "2026-03-20"})
    assert r.status_code == 400 and "joined this project on 05-Mar-2026" in r.json()["detail"]


def test_fill_from_file_puts_the_whole_cross_month_period_on_this_sheet(db):
    from openpyxl import Workbook
    project, emp, _pe = _seed(db)
    client = _client(db)
    mar = _create(client, project, emp, 3)
    r = client.patch(f"/api/timesheets/{mar['id']}/period",
                     json={"start_date": "2026-02-21", "end_date": "2026-03-20"})
    assert r.status_code == 200, r.text

    wb = Workbook()
    ws = wb.active
    ws.append(["Day"] + list(range(21, 29)) + list(range(1, 21)))
    ws.append(["Status"] + ["P"] * 28)
    buf = io.BytesIO()
    wb.save(buf)
    r = client.post(
        f"/api/timesheets/bulk-import?project_id={project.id}&employee_id={emp.id}"
        f"&year=2026&month=3&timesheet_id={mar['id']}",
        files={"file": ("customer.xlsx", buf.getvalue(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200, r.text
    months = r.json()["data"]["months"]
    assert len(months) == 1 and months[0]["timesheet_id"] == mar["id"] and months[0]["applied"] == 28
    # No February sheet was created to swallow the 21–28 Feb days.
    from models import Timesheet
    assert db.execute(select(Timesheet).where(Timesheet.month == 2)).scalars().first() is None
    assert _entry(db, mar["id"], date(2026, 2, 23)).attendance_status.value == "Present"


def test_removed_days_go_to_the_open_next_sheet_with_their_hours(db):
    project, emp, _pe = _seed(db)
    client = _client(db)
    mar = _create(client, project, emp, 3)
    apr = _create(client, project, emp, 4)
    r = client.post(f"/api/timesheets/{mar['id']}/entries",
                    json=[{"entry_date": "2026-03-25", "hours_worked": 7}])
    assert r.status_code == 200, r.text

    opts = client.get(f"/api/timesheets/{mar['id']}/period-options").json()["data"]
    assert opts["next"]["label"] == "April 2026" and opts["next"]["open"] is True

    # March shrinks to 1–20 Mar; April (open, starts 1 Apr) takes 21–31 Mar.
    r = client.patch(f"/api/timesheets/{mar['id']}/period", json={
        "start_date": "2026-03-01", "end_date": "2026-03-20", "hand_over_removed": True})
    assert r.status_code == 200, r.text
    assert "handed to April 2026" in r.json()["message"]
    apr_detail = client.get(f"/api/timesheets/{apr['id']}").json()["data"]
    assert apr_detail["period_start_date"] == "2026-03-21"
    moved = _entry(db, apr["id"], date(2026, 3, 25))
    assert moved is not None and Decimal(moved.hours_worked) == Decimal(7)
    assert _entry(db, mar["id"], date(2026, 3, 25)) is None
