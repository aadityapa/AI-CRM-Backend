"""Duplicate candidate detection — a warning before create, not a wall.

Run:  cd backend && python -m pytest tests/test_candidate_duplicates.py -q
"""
from __future__ import annotations

import importlib

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
import routers.crm.candidates as candidates_router  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(bind=engine, future=True)
    from models.base import users_table_stub
    db.execute(users_table_stub.insert().values(id=1))
    db.commit()

    from models import Candidate
    db.add_all([
        Candidate(first_name="Manikantha", last_name="Dasari",
                  email="manikantha.d@gmail.com", phone="+91 81234 56789"),
        Candidate(first_name="Ravi", last_name="Teja",
                  email="ravi.teja.abc123@import.karnex.in", phone=None),
    ])
    db.commit()

    app = FastAPI()
    app.include_router(candidates_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="ta", roles={"TA"})
    return TestClient(app)


def _check(client, **params):
    r = client.get("/api/candidates/check-duplicates", params=params)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def test_phone_matches_across_formatting(client):
    """'+91 81234 56789' stored; a re-typed '8123456789' must still match."""
    out = _check(client, phone="8123456789")
    assert len(out) == 1
    assert out[0]["match_on"] == ["phone"]
    assert "Manikantha" in out[0]["name"]


def test_email_matches_case_insensitively(client):
    out = _check(client, email="MANIKANTHA.D@GMAIL.COM")
    assert len(out) == 1 and "email" in out[0]["match_on"]


def test_placeholder_import_emails_never_match(client):
    """Synthesised @import.karnex.in addresses are unique hashes, not people."""
    out = _check(client, email="ravi.teja.abc123@import.karnex.in")
    assert out == []


def test_name_matches_when_normalised(client):
    out = _check(client, name="manikantha  DASARI")
    assert len(out) == 1 and "name" in out[0]["match_on"]


def test_short_names_do_not_flag(client):
    """<6 letters would flag half the database — name signal stays quiet."""
    assert _check(client, name="Ma Da") == []


def test_multiple_signals_rank_first(client):
    out = _check(client, phone="081234 56789", email="manikantha.d@gmail.com",
                 name="Manikantha Dasari")
    assert len(out) == 1
    assert set(out[0]["match_on"]) == {"phone", "email", "name"}


def test_no_match_returns_empty(client):
    assert _check(client, phone="9999999999", email="new.person@example.com",
                  name="Completely Different") == []
