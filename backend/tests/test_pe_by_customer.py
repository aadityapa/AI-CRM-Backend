"""Project employees list filtered by customer_id (Replacement Engineer source).

Run:  python -m pytest tests/test_pe_by_customer.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET
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


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.hr import Employee  # noqa: E402
from models.masters import Designation  # noqa: E402
from models.opportunities import Opportunity, OppType  # noqa: E402
from models.projects import BillingUnit, Project, ProjectEmployee  # noqa: E402
import crm_deps  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
import routers.crm.projects as projects_router  # noqa: E402

D = Decimal


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub

    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(projects_router.router)

    def _db():
        yield session

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: CurrentUser(
        id=1, username="admin", roles={"Admin"},
    )
    test_client = TestClient(app)
    test_client._session = session
    return test_client


def _project(db: Session, customer: Customer, name: str) -> Project:
    opp = Opportunity(
        opp_id=f"OPP-{name}", title=name, customer_id=customer.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=customer.id, name=name)
    db.add(proj)
    db.flush()
    return proj


def test_all_employees_filter_by_customer_id_returns_role_title(client):
    db = client._session
    harman = Customer(name="HARMAN India")
    other = Customer(name="Other Corp")
    db.add_all([harman, other])
    db.flush()

    desig = Designation(name="Engineer-I")
    db.add(desig)
    db.flush()

    akshay = Employee(
        first_name="Akshay", last_name="Gurjar", email="akshay@example.com",
        designation_id=desig.id, role_title=None,
    )
    outsider = Employee(
        first_name="Other", last_name="Eng", email="other@example.com",
        role_title="Senior Engineer",
    )
    db.add_all([akshay, outsider])
    db.flush()

    harman_proj = _project(db, harman, "Harman Delivery")
    other_proj = _project(db, other, "Other Delivery")

    # PE has explicit role_title — prefer that over designation.
    pe_role = ProjectEmployee(
        project_id=harman_proj.id, employee_id=akshay.id,
        onboarding_date=date(2026, 1, 1), billing_rate=D("8000"),
        billing_unit=BillingUnit.MONTHLY, is_active=True, is_exit=False,
        role_title="Engineer-I",
    )
    pe_other = ProjectEmployee(
        project_id=other_proj.id, employee_id=outsider.id,
        onboarding_date=date(2026, 1, 1), billing_rate=D("9000"),
        billing_unit=BillingUnit.MONTHLY, is_active=True, is_exit=False,
        role_title="Senior Engineer",
    )
    # Exited assignment on HARMAN must be excluded when status=active.
    pe_exited = ProjectEmployee(
        project_id=harman_proj.id, employee_id=outsider.id,
        onboarding_date=date(2025, 1, 1), billing_rate=D("7000"),
        billing_unit=BillingUnit.MONTHLY, is_active=False, is_exit=True,
        role_title="Should-Not-Appear",
    )
    db.add_all([pe_role, pe_other, pe_exited])
    db.commit()

    res = client.get(
        f"/api/projects/all-employees?customer_id={harman.id}&status=active&limit=100",
    )
    assert res.status_code == 200, res.text
    body = res.json()
    rows = body["data"] if isinstance(body, dict) else body
    assert len(rows) == 1
    row = rows[0]
    assert row["employee_id"] == akshay.id
    assert row["customer_id"] == harman.id
    assert "Akshay" in (row.get("employee_name") or "")
    assert row.get("role_title") == "Engineer-I"


def test_all_employees_role_title_falls_back_to_designation(client):
    db = client._session
    cust = Customer(name="Designation Corp")
    db.add(cust)
    db.flush()
    desig = Designation(name="Engineer-II")
    db.add(desig)
    db.flush()
    emp = Employee(
        first_name="Riya", last_name="Shah", email="riya@example.com",
        designation_id=desig.id, role_title=None,
    )
    db.add(emp)
    db.flush()
    proj = _project(db, cust, "Desig Proj")
    db.add(ProjectEmployee(
        project_id=proj.id, employee_id=emp.id,
        onboarding_date=date(2026, 2, 1), billing_rate=D("5000"),
        billing_unit=BillingUnit.MONTHLY, is_active=True, is_exit=False,
        role_title=None,
    ))
    db.commit()

    res = client.get(
        f"/api/projects/all-employees?customer_id={cust.id}&status=active&limit=50",
    )
    assert res.status_code == 200, res.text
    rows = res.json()["data"]
    assert len(rows) == 1
    assert rows[0]["role_title"] == "Engineer-II"
