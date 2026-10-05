"""Opportunities "Pending Approval" tab + seller GSTIN / PAN shape (5 Oct 2026).

(1) Two positions waiting for RMG approval showed under Active while Pending
Approval was empty — the tab only knew the opportunity's own Sales Head
approval. `awaiting_approval_clause` now includes a live, approved deal whose
requirement awaits the Sales Head / RMG, and its negation keeps it out of
Active, so every deal sits in exactly one tab.
(2) The seller GSTIN and PAN had been saved into each other's boxes; Settings
now refuses a value of the wrong shape.
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine, not_, select
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
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Customer, OppType, Opportunity, OpportunityApprovalStatus, PipelineStage, Requirement,
    RequirementStatus,
)
from services.org_settings import normalize_value, validation_error  # noqa: E402
from services.requirements import awaiting_approval_clause  # noqa: E402


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


def _opp(db, n, approval, stage=PipelineStage.ACTIVE, req_status=None):
    cust = db.query(Customer).first() or Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"C-{n}", title=f"Role {n}", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1,
                      approval_status=approval, pipeline_stage=stage)
    db.add(opp); db.flush()
    if req_status is not None:
        db.add(Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id,
                           title=f"Role {n}", no_of_positions=2, status=req_status, created_by=1))
        db.flush()
    return opp.opp_id


def test_rmg_pending_positions_are_pending_approval_never_active(db):
    A = OpportunityApprovalStatus
    sales_head = _opp(db, 1, A.PENDING_SALES_HEAD_APPROVAL)
    rmg = _opp(db, 2, A.APPROVED, req_status=RequirementStatus.PENDING_ENGINEERING_REVIEW)
    sourcing = _opp(db, 3, A.APPROVED, req_status=RequirementStatus.OPEN_FOR_SOURCING)
    held = _opp(db, 4, A.APPROVED, stage=PipelineStage.ON_HOLD,
                req_status=RequirementStatus.PENDING_ENGINEERING_REVIEW)

    def ids(clause):
        return set(db.execute(select(Opportunity.opp_id).where(clause)).scalars())

    pending = ids(awaiting_approval_clause())
    assert pending == {sales_head, rmg}
    # The Active tab = approved + its stages + NOT pending → the RMG one is gone.
    active = ids(not_(awaiting_approval_clause())
                 & (Opportunity.approval_status == A.APPROVED)
                 & Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE)))
    assert active == {sourcing}
    # A held deal stays in its hold tab even while its position waits for RMG.
    assert held not in pending


def test_gstin_and_pan_must_have_their_own_shape():
    assert normalize_value("invoice.seller_gstin", " 27aahck4749a1zl ") == "27AAHCK4749A1ZL"
    assert validation_error("invoice.seller_gstin", "27AAHCK4749A1ZL") is None
    assert validation_error("invoice.seller_pan", "AAHCK4749A") is None
    # The reported swap is refused in both boxes.
    assert validation_error("invoice.seller_gstin", "AAHCK4749A")
    assert validation_error("invoice.seller_pan", "27AAHCK4749A1ZL")
    assert validation_error("invoice.seller_pan", "") is None  # blank = fall back
    assert validation_error("invoice.seller_name", "anything") is None
