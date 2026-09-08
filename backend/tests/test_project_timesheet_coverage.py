"""Project timesheet tab shows OWED months, not just filed ones.

An onboarded employee with no sheet used to be invisible: the tab listed what
existed, so "January is there" read as "all good" even months later. With
`include_missing=1` every month from onboarding to today appears — real sheets
with their status, absent ones as synthetic `status: "Due"` rows with no id.

Run:  cd backend && python -m pytest tests/test_project_timesheet_coverage.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
import crm_deps  # noqa: E402
import routers.crm.projects as projects_router  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(bind=engine, future=True)
    from models.base import users_table_stub
    db.execute(users_table_stub.insert().values(id=1))
    db.commit()

    app = FastAPI()
    app.include_router(projects_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="hr", roles={"HR"})
    c = TestClient(app)
    c._db = db
    try:
        yield c
    finally:
        db.close()


def _seed(db):
    from models import (
        Customer, Employee, Project, ProjectEmployee, Timesheet, TimesheetStatus,
    )

    cust = Customer(name="Magna")
    db.add(cust)
    db.flush()
    project = Project(name="Magna T&M", customer_id=cust.id)
    emp = Employee(first_name="Apurve", last_name="Sarve", email="apurve@karnex.in")
    db.add_all([project, emp])
    db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id,
                         onboarding_date=date(2026, 1, 1), billing_rate=300000,
                         is_active=True, is_exit=False)
    db.add(pe)
    db.flush()
    # Only January was ever filed.
    db.add(Timesheet(project_id=project.id, employee_id=emp.id,
                     project_employee_id=pe.id, month=1, year=2026,
                     status=TimesheetStatus.DRAFT))
    db.commit()
    return project, emp, pe


FROZEN_TODAY = date(2026, 8, 12)


def test_coverage_lists_every_owed_month(client):
    project, emp, _pe = _seed(client._db)
    with patch("routers.crm.projects._coverage_today", return_value=FROZEN_TODAY):
        r = client.get(f"/api/projects/{project.id}/timesheets"
                       f"?include_missing=1&limit=20")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    # Jan..Aug 2026 = 8 months: 1 real + 7 due.
    assert len(rows) == 8
    assert rows[0]["month"] == 8 and rows[0]["status"] == "Due" and rows[0]["id"] is None
    jan = next(x for x in rows if x["month"] == 1)
    assert jan["status"] == "Draft" and jan["id"] is not None
    assert sum(1 for x in rows if x["status"] == "Due") == 7


def test_due_filter_shows_only_missing_months(client):
    project, _emp, _pe = _seed(client._db)
    with patch("routers.crm.projects._coverage_today", return_value=FROZEN_TODAY):
        r = client.get(f"/api/projects/{project.id}/timesheets?status=Due&limit=20")
    rows = r.json()["data"]
    assert len(rows) == 7
    assert all(x["status"] == "Due" and x["id"] is None for x in rows)
    assert not any(x["month"] == 1 for x in rows), "January exists — never listed as due"


def test_exited_employee_owes_only_up_to_exit_month(client):
    from models import Employee, ProjectEmployee

    project, _emp, _pe = _seed(client._db)
    db = client._db
    emp2 = Employee(first_name="Riya", last_name="K", email="riya@karnex.in")
    db.add(emp2)
    db.flush()
    db.add(ProjectEmployee(project_id=project.id, employee_id=emp2.id,
                           onboarding_date=date(2026, 2, 1), billing_rate=200000,
                           is_active=False, is_exit=True, exit_date=date(2026, 4, 15)))
    db.commit()

    with patch("routers.crm.projects._coverage_today", return_value=FROZEN_TODAY):
        r = client.get(f"/api/projects/{project.id}/timesheets"
                       f"?include_missing=1&employee_id={emp2.id}&limit=20")
    rows = r.json()["data"]
    # Feb, Mar, Apr — nothing owed after the exit month.
    assert [(x["year"], x["month"]) for x in rows] == [(2026, 4), (2026, 3), (2026, 2)]
    assert all(x["status"] == "Due" for x in rows)


def test_without_flag_behaviour_is_unchanged(client):
    project, _emp, _pe = _seed(client._db)
    r = client.get(f"/api/projects/{project.id}/timesheets?limit=20")
    rows = r.json()["data"]
    assert len(rows) == 1 and rows[0]["status"] == "Draft"
