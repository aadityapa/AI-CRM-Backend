"""Project.branch_id linkage — linked_projects + create persistence.

Covers:
  * Branch policy linked_projects via Project.branch_id (even when opp has no/other branch)
  * Legacy fallback: Opportunity.branch_id still lists the project
  * POST /api/projects persists branch_id (explicit or from opportunity)

Run:  python -m pytest tests/test_project_branch_id.py -q
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET


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
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from fastapi import FastAPI                                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
from models.customers import Customer, CustomerBranch                  # noqa: E402
from models.opportunities import Opportunity, OppType                  # noqa: E402
from models.projects import Project                                    # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402
import routers.crm.projects as projects_router                         # noqa: E402

D = Decimal


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(customers_router.router)
    app.include_router(projects_router.router)

    def _override_db():
        yield session

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="qa", roles={"Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user

    c = TestClient(app)
    c._session = session
    try:
        yield c
    finally:
        session.close()


def _seed_customer_branch(session, cust_name="BMW", branch_name="BMW Pune"):
    cust = Customer(name=cust_name)
    session.add(cust)
    session.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name=branch_name)
    session.add(branch)
    session.flush()
    return cust, branch


def test_linked_projects_by_project_branch_id(client):
    """Project.branch_id alone links to branch even when opportunity.branch_id is null."""
    s = client._session
    cust, branch = _seed_customer_branch(s)
    other = CustomerBranch(customer_id=cust.id, branch_name="BMW Mumbai")
    s.add(other)
    s.flush()
    opp = Opportunity(
        opp_id="OPP-BMW-1", title="BMW Opp", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1, branch_id=None,
    )
    s.add(opp)
    s.flush()
    proj = Project(
        opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id,
        name="BMW Project",
    )
    s.add(proj)
    s.commit()

    data = client.get(f"/api/customers/branches/{branch.id}/policy").json()["data"]
    ids = {p["id"] for p in data["linked_projects"]}
    assert proj.id in ids
    assert data["linked_projects"][0]["name"] == "BMW Project"

    # Not listed under the other branch
    other_data = client.get(f"/api/customers/branches/{other.id}/policy").json()["data"]
    assert other_data["linked_projects"] == []


def test_linked_projects_legacy_opportunity_branch(client):
    """Legacy: project with null branch_id still links via opportunity.branch_id."""
    s = client._session
    cust, branch = _seed_customer_branch(s)
    opp = Opportunity(
        opp_id="OPP-LEGACY", title="Legacy Opp", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1, branch_id=branch.id,
    )
    s.add(opp)
    s.flush()
    proj = Project(
        opportunity_id=opp.id, customer_id=cust.id, branch_id=None,
        name="Legacy Project",
    )
    s.add(proj)
    s.commit()

    data = client.get(f"/api/customers/branches/{branch.id}/policy").json()["data"]
    assert any(p["id"] == proj.id for p in data["linked_projects"])


def test_create_project_persists_explicit_branch_id(client):
    """POST /api/projects with branch_id stores it even if opportunity has another/no branch."""
    s = client._session
    cust, branch = _seed_customer_branch(s)
    opp = Opportunity(
        opp_id="OPP-CREATE", title="Create Opp", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1, branch_id=None,
    )
    s.add(opp)
    s.commit()

    r = client.post("/api/projects", json={
        "name": "Wizard Project",
        "opportunity_id": opp.id,
        "customer_id": cust.id,
        "branch_id": branch.id,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["branch_id"] == branch.id

    row = s.get(Project, data["id"])
    assert row is not None and row.branch_id == branch.id


def test_create_project_inherits_branch_from_opportunity(client):
    """When branch_id omitted, create copies opportunity.branch_id onto the project."""
    s = client._session
    cust, branch = _seed_customer_branch(s)
    opp = Opportunity(
        opp_id="OPP-INHERIT", title="Inherit Opp", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1, branch_id=branch.id,
    )
    s.add(opp)
    s.commit()

    r = client.post("/api/projects", json={
        "name": "Inherited Branch Project",
        "opportunity_id": opp.id,
        "customer_id": cust.id,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["branch_id"] == branch.id


def test_project_creates_without_an_opportunity(client):
    """Since 0069 a project needs no sales opportunity behind it.

    The form used to force an arbitrary pick just to pass validation, which
    fabricated sales lineage. Creation, detail and serialization must all be
    null-safe now.
    """
    s = client._session
    cust, branch = _seed_customer_branch(s, cust_name="Internal", branch_name="Internal HQ")
    s.commit()

    r = client.post("/api/projects", json={
        "name": "Internal Tooling",
        "customer_id": cust.id,
        "branch_id": branch.id,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["opportunity_id"] is None
    assert data["name"] == "Internal Tooling"

    detail = client.get(f"/api/projects/{data['id']}").json()["data"]
    assert detail["opportunity_title"] is None
    assert detail["branch_id"] == branch.id
