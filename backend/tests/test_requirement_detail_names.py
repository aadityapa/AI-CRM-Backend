"""Single-requirement responses carry the customer / location NAME (28 Sep 2026).

Reported with a screenshot: a TA opened a position and the header read
"Customer #60". The detail page looked the name up through /api/customers/{id},
which is gated to the Customers tab. The payload is now self-sufficient.

Run:  cd backend && python -m pytest tests/test_requirement_detail_names.py -q
"""
from __future__ import annotations

import importlib

import pytest
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
from models import (  # noqa: E402
    Customer, Location, Opportunity, OppType, Requirement, RequirementStatus,
)
from routers.crm.requirements import _attach_names, _one  # noqa: E402


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


def _req(db, *, with_location=True):
    cust = Customer(name="HARMAN")
    db.add(cust)
    db.flush()
    loc = None
    if with_location:
        loc = Location(city="Bengaluru", state="Karnataka")
        db.add(loc)
        db.flush()
    opp = Opportunity(opp_id="C-2026-00085", title="Manual Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    req = Requirement(req_number="REQ-1", title=opp.title, opportunity_id=opp.id,
                      customer_id=cust.id, location_id=loc.id if loc else None,
                      status=RequirementStatus.OPEN_FOR_SOURCING, created_by=1,
                      no_of_positions=2)
    db.add(req)
    db.commit()
    return req


def test_detail_payload_names_the_customer_and_location(db):
    req = _req(db)
    data = _one(db, req)
    assert data["customer_id"] == req.customer_id
    assert data["customer_name"] == "HARMAN"
    assert data["location_name"] == "Bengaluru, Karnataka"


def test_missing_links_give_none_not_an_error(db):
    req = _req(db, with_location=False)
    from types import SimpleNamespace
    orphan = SimpleNamespace(customer_id=None, location_id=req.location_id)
    data: dict = {}
    _attach_names(db, data, orphan)
    assert data == {"customer_name": None, "location_name": None}
