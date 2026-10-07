"""AI L1 template status: ready / requested / missing, and the refusal every route shares (6 Oct 2026)."""
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
from models.base import Base, users_table_stub  # noqa: E402
from models.customers import Customer  # noqa: E402
from models.opportunities import Opportunity, OppType, PipelineStage  # noqa: E402
from models.requirements import Requirement, RequirementStatus, Priority  # noqa: E402
from models.template_requests import TemplateRequest, TemplateRequestStatus  # noqa: E402
from services import ai_interview_bridge as bridge  # noqa: E402


def _book():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(bind=engine, future=True)
    db.execute(users_table_stub.insert().values(id=1))
    cust = Customer(name="Acme")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00085", title="Manual Test Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, pipeline_stage=PipelineStage.ACTIVE, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-1", title="Manual Test Engineer", opportunity_id=opp.id,
                      customer_id=cust.id, status=RequirementStatus.OPEN_FOR_SOURCING,
                      priority=Priority.MEDIUM, no_of_positions=2, created_by=1)
    db.add(req); db.commit()
    return db, opp, req


def _tr(db, opp, req, status, job=None, n="TR-2026-001"):
    tr = TemplateRequest(tr_number=n, requirement_id=req.id, opportunity_id=opp.id, role_title="x",
                         status=status, requested_by=1, template_job_id=job)
    db.add(tr); db.commit()
    return tr


@pytest.fixture(autouse=True)
def _no_name_match(monkeypatch):
    monkeypatch.setattr(bridge, "_matching_job_template_id", lambda opp: "")


def test_missing_when_nothing_was_requested():
    db, opp, req = _book()
    st = bridge.l1_template_status(db, opp, req)
    assert st["ready"] is False and st["state"] == "missing" and st["request"] is None
    assert st["code"] == bridge.TEMPLATE_NOT_READY_CODE
    assert "not ready" in st["reason"] and "Template Request" in st["reason"]


def test_requested_names_the_waiting_request_and_ignores_a_cancelled_one():
    db, opp, req = _book()
    _tr(db, opp, req, TemplateRequestStatus.CANCELLED, n="TR-2026-001")
    assert bridge.l1_template_status(db, opp, req)["state"] == "missing"
    _tr(db, opp, req, TemplateRequestStatus.PENDING_RMG, n="TR-2026-002")
    st = bridge.l1_template_status(db, opp, req)
    assert st["state"] == "requested" and st["request"]["tr_number"] == "TR-2026-002"
    assert "TR-2026-002" in st["reason"] and "RMG" in st["reason"]


def test_ready_once_rmg_links_a_template_and_the_guard_passes():
    db, opp, req = _book()
    _tr(db, opp, req, TemplateRequestStatus.TEMPLATE_READY, job="job-42")
    st = bridge.l1_template_status(db, opp, req)
    assert st["ready"] is True and st["state"] == "ready" and st["template_job_id"] == "job-42"
    bridge.ensure_l1_template_ready(db, opp, req)  # no raise


def test_the_guard_refuses_with_the_same_reason():
    from fastapi import HTTPException
    db, opp, req = _book()
    with pytest.raises(HTTPException) as e:
        bridge.ensure_l1_template_ready(db, opp, req)
    assert e.value.status_code == 400
    assert e.value.detail == bridge.l1_template_status(db, opp, req)["reason"]


def test_every_ai_l1_route_checks_the_template():
    """Profile schedule, Applied Candidates schedule, slot-invite preview and send."""
    import inspect
    import routers.crm.ai_interviews as ai
    import routers.crm.resumes as rs
    import routers.crm.slots as sl
    assert "ensure_l1_template_ready" in inspect.getsource(ai.trigger_ai_interview)
    assert "ensure_l1_template_ready" in inspect.getsource(rs.schedule_ai_interview)
    assert "_ensure_ai_template" in inspect.getsource(sl.slot_invite_preview)
    assert "_ensure_ai_template" in inspect.getsource(sl.send_slot_invite_manual)
