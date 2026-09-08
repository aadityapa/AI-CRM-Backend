"""Candidate-profile list enrichment (candidate + opportunity summary fields).

Asserts Sales and Sales_Head can read the enriched list payload.
"""
from __future__ import annotations

from decimal import Decimal

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


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.candidates import Candidate  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.opportunities import Opportunity, OppType, PipelineStage  # noqa: E402
from models.profiles import CandidateProfile, PipelineStatus  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.candidate_profiles as profiles_router  # noqa: E402


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    session.execute(users_table_stub.insert().values(id=1))
    cust = Customer(name="Acme Corp")
    session.add(cust)
    session.flush()
    opp = Opportunity(
        opp_id="OPP-2026-APP",
        title="Backend Engineer",
        customer_id=cust.id,
        opp_type=OppType.T_AND_M,
        pipeline_stage=PipelineStage.ACTIVE,
        created_by=1,
    )
    session.add(opp)
    session.flush()
    cand = Candidate(
        first_name="Priya",
        last_name="Sharma",
        email="priya@example.com",
        phone="+919876543210",
        experience_years=Decimal("5.5"),
        notice_period="30 days",
        technical_domain="Backend",
        cv_url="/files/priya-cv.pdf",
        current_ctc=Decimal("1200000"),
        expected_ctc=Decimal("1500000"),
    )
    session.add(cand)
    session.flush()
    profile = CandidateProfile(
        candidate_id=cand.id,
        opportunity_id=opp.id,
        pipeline_status=PipelineStatus.SOURCING,
        current_ctc=Decimal("1200000"),
        expected_ctc=Decimal("1500000"),
        hike_percent=Decimal("25.00"),
    )
    session.add(profile)
    session.commit()

    app = FastAPI()
    app.include_router(profiles_router.router)

    def _db():
        yield session

    current = {"roles": {"Sales"}}

    def _user():
        return crm_deps.CurrentUser(id=1, username="tester", roles=current["roles"])

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = _user

    client = TestClient(app)
    client._session = session
    client._roles = current
    client._opp_id = opp.id
    client._profile_id = profile.id
    try:
        yield client
    finally:
        session.close()


@pytest.mark.parametrize("role", ["Sales", "Sales_Head"])
def test_list_profiles_enriched_for_sales_roles(env, role):
    env._roles["roles"] = {role}
    r = env.get(f"/api/candidate-profiles?opportunity_id={env._opp_id}")
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_name"] == "Priya Sharma"
    assert row["email"] == "priya@example.com"
    assert row["phone"] == "+919876543210"
    assert row["experience_years"] == 5.5
    assert row["notice_period"] == "30 days"
    assert row["technical_domain"] == "Backend"
    assert row["cv_url"] == "/files/priya-cv.pdf"
    assert row["opportunity_title"] == "Backend Engineer"
    assert row["opportunity_opp_id"] == "OPP-2026-APP"
    assert row["pipeline_status"] == "Sourcing"


def test_list_profiles_search_by_email(env):
    env._roles["roles"] = {"Sales"}
    r = env.get("/api/candidate-profiles?search=priya@example.com")
    assert r.status_code == 200
    assert len(r.json()["data"]) == 1
    r2 = env.get("/api/candidate-profiles?search=nobody-here")
    assert r2.status_code == 200
    assert r2.json()["data"] == []
