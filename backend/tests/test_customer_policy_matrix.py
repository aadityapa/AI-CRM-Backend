"""Settings → Customer Policies matrix (/api/customers/policy-matrix).

A window onto the SAME tables the Opportunity form inherits from — never a
second copy. Also pins the billable_leaves_per_year round-trip through the
billing-policy PUT (it was silently dropped before this tab existed).

Run:  cd backend && python -m pytest tests/test_customer_policy_matrix.py -q
"""
from __future__ import annotations

import importlib
from decimal import Decimal as D

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
import routers.crm.customers as customers_router  # noqa: E402


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
    app.include_router(customers_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="sales", roles={"Sales_Head"})
    c = TestClient(app)
    c._db = db
    try:
        yield c
    finally:
        db.close()


def _seed(db):
    from models import (
        Customer, CustomerBillingPolicy, CustomerBranch, CustomerLeavePolicy,
        LeavePolicyType,
    )
    harman = Customer(name="Harman-Valueleaf")
    minda = Customer(name="Creat UNO Minda")
    db.add_all([harman, minda])
    db.flush()
    db.add(CustomerBillingPolicy(
        customer_id=harman.id, holidays_billable=False, week_off_billable=False,
        leave_billable=False, comp_off_billable=True, billing_type="Per_Hour",
        normal_hours_per_day=D("8"), min_hours_full_day=D("8"), min_hours_half_day=D("4")))
    db.add(CustomerBranch(customer_id=harman.id, branch_name="Bangalore",
                          is_max_billable_hours_per_month=True,
                          max_billable_hours_per_month=D("176")))
    el = LeavePolicyType(name="EL")
    db.add(el)
    db.flush()
    db.add(CustomerLeavePolicy(customer_id=minda.id, leave_type_id=el.id,
                               leave_credit_balance=D("1.75"),
                               maximum_carry_forward=D("30")))
    db.commit()
    return harman, minda


def test_matrix_reads_existing_policy_tables(client):
    harman, minda = _seed(client._db)
    r = client.get("/api/customers/policy-matrix")
    assert r.status_code == 200, r.text
    rows = {x["customer_name"]: x for x in r.json()["data"]}

    h = rows["Harman-Valueleaf"]
    assert h["holidays_billable"] is False and h["week_off_billable"] is False
    assert h["comp_off_billable"] is True
    assert h["billing_type"] == "Per_Hour"
    assert h["hours_per_day"] == 8.0
    assert h["max_billable_hours_per_month"] == 176.0  # branch cap surfaces

    m = rows["Creat UNO Minda"]
    assert m["has_policy"] is False               # no billing policy yet
    assert m["leaves"] == [{"type": "EL", "monthly_credit": 1.75,
                            "carry_forward": True, "branch_id": None}]


def test_billable_leaves_round_trips_through_policy_put(client):
    harman, _ = _seed(client._db)
    r = client.put(f"/api/customers/{harman.id}/billing-policy", json={
        "min_hours_full_day": 8, "min_hours_half_day": 4,
        "billable_leaves_per_year": 18,
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["billable_leaves_per_year"] == 18.0
    row = next(x for x in client.get("/api/customers/policy-matrix").json()["data"]
               if x["customer_id"] == harman.id)
    assert row["billable_leaves_per_year"] == 18.0
    # Omitting the field on a later PUT must NOT wipe it.
    r = client.put(f"/api/customers/{harman.id}/billing-policy", json={
        "min_hours_full_day": 9, "min_hours_half_day": 4})
    assert r.json()["data"]["billable_leaves_per_year"] == 18.0
