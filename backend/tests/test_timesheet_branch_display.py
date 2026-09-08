"""Timesheet header branch must use customer-guarded project/PE branch, never raw opp.branch_id.

Run:  python -m pytest tests/test_timesheet_branch_display.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET

for _T in (JSONB,):
    @compiles(_T, "sqlite")
    def _json(el, comp, **kw):  # noqa: ANN001
        return "JSON"


@compiles(ARRAY, "sqlite")
def _arr(el, comp, **kw):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _uuid(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _inet(el, comp, **kw):  # noqa: ANN001
    return "VARCHAR(64)"


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer, CustomerBranch  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import Project, ProjectEmployee  # noqa: E402
from models.timesheets import Timesheet, TimesheetStatus  # noqa: E402
from services.timesheets import (  # noqa: E402
    _project_branch,
    opportunity_branch_foreign_to_project,
    timesheet_detail_out,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db: Session, *, set_project_branch: bool, with_pe: bool = True, extra_bmw_branch: bool = False):
    bmw = Customer(name="BMW")
    harman = Customer(name="HARMAN India")
    db.add_all([bmw, harman])
    db.flush()

    pune = CustomerBranch(customer_id=bmw.id, branch_name="BMW Pune")
    bangalore = CustomerBranch(customer_id=harman.id, branch_name="Harman - Bangalore")
    db.add_all([pune, bangalore])
    db.flush()
    if extra_bmw_branch:
        db.add(CustomerBranch(customer_id=bmw.id, branch_name="BMW Mumbai"))
        db.flush()

    opp = Opportunity(
        opp_id="OPP-BMW-BRANCH",
        title="BMW Opp",
        customer_id=bmw.id,
        branch_id=bangalore.id,  # stray foreign branch
        opp_type=OppType.T_AND_M,
        created_by=1,
    )
    db.add(opp)
    db.flush()

    proj = Project(
        name="BMW Project",
        customer_id=bmw.id,
        opportunity_id=opp.id,
        branch_id=pune.id if set_project_branch else None,
    )
    db.add(proj)
    db.flush()

    emp = Employee(first_name="Pawan", last_name="Sanap", email="pawan.branch@test.in")
    db.add(emp)
    db.flush()

    pe = None
    if with_pe:
        pe = ProjectEmployee(
            project_id=proj.id, employee_id=emp.id,
            onboarding_date=date(2026, 1, 1), is_active=True,
            billing_rate=Decimal("1000"),
        )
        db.add(pe)
        db.flush()

    ts = Timesheet(
        project_id=proj.id,
        employee_id=emp.id,
        project_employee_id=pe.id if pe else None,
        year=2026,
        month=6,
        status=TimesheetStatus.DRAFT,
    )
    db.add(ts)
    db.flush()
    return ts, proj, pe, pune, bangalore


def test_timesheet_detail_uses_project_branch_not_foreign_opp(db):
    ts, proj, pe, pune, bangalore = _seed(db, set_project_branch=True)
    assert proj.branch_id == pune.id
    assert opportunity_branch_foreign_to_project(db, proj) is True
    assert _project_branch(db, proj).id == pune.id

    data = timesheet_detail_out(db, ts, [])
    assert data["branch_id"] == pune.id
    assert data["branch_name"] == "BMW Pune"
    assert data["branch_name"] != "Harman - Bangalore"
    assert data.get("branch_unlinked") is False


def test_timesheet_detail_unlinked_when_only_foreign_opp_branch(db):
    # No project.branch_id, no PE fallbacks (extra BMW branch blocks unique fallback).
    ts, proj, pe, pune, bangalore = _seed(
        db, set_project_branch=False, with_pe=False, extra_bmw_branch=True,
    )
    assert proj.branch_id is None
    assert _project_branch(db, proj) is None
    assert opportunity_branch_foreign_to_project(db, proj) is True

    data = timesheet_detail_out(db, ts, [])
    assert data["branch_id"] is None
    assert data["branch_name"] is None
    assert data["branch_unlinked"] is True
    assert "not linked" in (data.get("branch_link_message") or "").lower()
