"""Template-request opportunity link + fulfill stamps job template opportunityId."""
from __future__ import annotations

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


import importlib

for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave",
    "timesheets", "finance", "hr", "candidates", "masters", "requirements",
    "profiles", "resumes", "ai_links", "scheduling",
    "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.opportunities import Opportunity, OppType, PipelineStage  # noqa: E402
from models.requirements import Requirement, RequirementStatus, Priority  # noqa: E402
from models.template_requests import TemplateRequest, TemplateRequestStatus  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.template_requests as tr_router  # noqa: E402
from services.ai_interview_bridge import (  # noqa: E402
    _template_job_id_from_request,
    ai_interview_autosend_enabled,
)


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
    session.execute(users_table_stub.insert().values(id=2))
    cust = Customer(name="Acme")
    session.add(cust)
    session.flush()
    opp = Opportunity(
        opp_id="OPP-2026-L1",
        title="L1 Role",
        customer_id=cust.id,
        opp_type=OppType.T_AND_M,
        pipeline_stage=PipelineStage.ACTIVE,
        created_by=1,
    )
    session.add(opp)
    session.flush()
    req = Requirement(
        req_number="REQ-2026-L1",
        title="L1 Requirement",
        opportunity_id=opp.id,
        customer_id=cust.id,
        status=RequirementStatus.OPEN_FOR_SOURCING,
        priority=Priority.MEDIUM,
        no_of_positions=1,
        created_by=1,
    )
    session.add(req)
    session.flush()
    tr = TemplateRequest(
        tr_number="TR-2026-001",
        requirement_id=req.id,
        opportunity_id=opp.id,
        role_title="L1 Role",
        status=TemplateRequestStatus.PENDING_RMG,
        requested_by=1,
    )
    session.add(tr)
    session.commit()

    app = FastAPI()
    app.include_router(tr_router.router)
    current = {"roles": {"RMG"}}

    def _db():
        yield session

    def _user():
        return crm_deps.CurrentUser(id=2, username="rmg", roles=current["roles"])

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = _user

    client = TestClient(app)
    client._session = session
    client._roles = current
    client._tr_id = tr.id
    client._opp = opp
    client._req = req
    try:
        yield client
    finally:
        session.close()


def test_create_stores_opportunity_id(env):
    env._roles["roles"] = {"TA"}
    # recreate as TA create
    r = env.post("/api/template-requests", json={"requirement_id": env._req.id})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["opportunity_id"] == env._opp.id
    assert data["opportunity_opp_id"] == "OPP-2026-L1"


def test_fulfill_requires_real_job_and_stamps_opportunity(env):
    fake_tpl = {
        "jobId": "tpl-abc",
        "jobTitle": "Backend L1 Screen",
        "opportunityId": "",
        "requiredSkills": [],
        "optionalSkills": [],
        "expMin": 0,
        "expMax": 5,
        "difficulty": "medium",
        "numQ": 5,
        "weights": {},
    }
    stamped = {**fake_tpl, "opportunityId": "OPP-2026-L1"}

    with patch("auth_db.get_job_template", return_value=dict(fake_tpl)) as get_mock, \
         patch("auth_db.upsert_job_template", return_value=stamped) as upsert_mock, \
         patch("routers.crm.template_requests._legacy_db_target", return_value="sqlite://"):
        r = env.post(
            f"/api/template-requests/{env._tr_id}/fulfill",
            json={"template_job_id": "tpl-abc"},
        )
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["status"] == "Template_Ready"
    assert body["template_job_id"] == "tpl-abc"
    assert body["template_name"] == "Backend L1 Screen"
    assert body["opportunity_id"] == env._opp.id
    get_mock.assert_called()
    upsert_mock.assert_called()
    args = upsert_mock.call_args[0][1]
    assert args["opportunityId"] == "OPP-2026-L1"


def test_fulfill_unknown_job_rejected(env):
    with patch("auth_db.get_job_template", return_value=None), \
         patch("routers.crm.template_requests._legacy_db_target", return_value="sqlite://"):
        r = env.post(
            f"/api/template-requests/{env._tr_id}/fulfill",
            json={"template_job_id": "missing"},
        )
    assert r.status_code == 400


def test_template_job_id_from_request_prefers_ready(env):
    session = env._session
    tr = session.get(TemplateRequest, env._tr_id)
    tr.status = TemplateRequestStatus.TEMPLATE_READY
    tr.template_job_id = "linked-tpl"
    session.commit()
    job = _template_job_id_from_request(session, env._opp, env._req)
    assert job == "linked-tpl"


def test_autosend_defaults_off(monkeypatch):
    monkeypatch.delenv("AI_INTERVIEW_AUTOSEND", raising=False)
    assert ai_interview_autosend_enabled() is False
    monkeypatch.setenv("AI_INTERVIEW_AUTOSEND", "true")
    assert ai_interview_autosend_enabled() is True
