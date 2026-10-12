"""Customer pickers list the Customers tab's Active customers (12 Oct 2026).

Reported with a screenshot: New Project's Customer dropdown listed every customer
ever imported while the Customers tab (default Active) shows a handful.
``GET /api/customers/names?status=Active`` is what the pickers that START a record
ask for; the bare call (list filters, reports) still returns everyone.

Run:  cd backend && python -m pytest tests/test_customer_names_status.py -q
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
from fastapi import HTTPException  # noqa: E402

from models import Customer  # noqa: E402
from routers.crm.customers import list_customer_names  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    s.add_all([
        Customer(name="Harman", status="Active", customer_type="NN"),
        Customer(name="Ford", status="Inactive", customer_type="NN"),
        Customer(name="Aptiv", status="Active", customer_type="NN"),
    ])
    s.commit()
    yield s
    s.close()


def _names(resp):
    return [r["name"] for r in resp["data"]]


def test_active_only_matches_the_customers_tab(db):
    resp = list_customer_names(status="Active", db=db, user=None)
    assert _names(resp) == ["Aptiv", "Harman"]
    assert {r["status"] for r in resp["data"]} == {"Active"}


def test_bare_call_still_lists_everyone_for_filters(db):
    resp = list_customer_names(status=None, db=db, user=None)
    assert _names(resp) == ["Aptiv", "Ford", "Harman"]


def test_unknown_status_is_refused(db):
    with pytest.raises(HTTPException) as e:
        list_customer_names(status="Archived", db=db, user=None)
    assert e.value.status_code == 400
