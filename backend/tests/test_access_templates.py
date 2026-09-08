"""Access Templates — Phase 2 tests.

White-box: the `effective_access` resolver (template live + per-user override, Admin/CEO full).
Black-box: the CRUD/assign/registry API over HTTP, and `/api/me` returning the modes.

Run:  python -m pytest tests/test_access_templates.py -q
"""
from __future__ import annotations

import json

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
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
           "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
           "ai_links", "scheduling", "user_profiles", "template_requests",
           "access_templates"]:
    importlib.import_module(f"models.{_m}")

from fastapi import FastAPI                                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
from models.access_templates import AccessTemplate                     # noqa: E402
from models.user_profiles import UserProfile                           # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.access_templates as at_router                       # noqa: E402
import routers.crm.me as me_router                                     # noqa: E402
from services.access_templates import effective_access                 # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


# ===================================================== WHITE-BOX: resolver
def test_resolver_admin_is_full(db):
    acc = effective_access(db, 1, {"Admin"})
    assert acc["full"] is True and acc["visible_tabs"] is None
    acc_ceo = effective_access(db, 1, {"CEO"})
    assert acc_ceo["full"] is True


def test_resolver_template_modes(db):
    t = AccessTemplate(name="Sales", is_active=True,
                       tab_access={"customers": "edit", "opportunities": "view"},
                       field_access={"customers": {"name": "edit", "status": "view"}})
    db.add(t); db.flush()
    db.add(UserProfile(user_id=5, access_template_id=t.id)); db.commit()
    acc = effective_access(db, 5, {"Sales"})
    assert acc["full"] is False and acc["source"] == "template"
    assert acc["tabs"] == {"customers": "edit", "opportunities": "view"}
    assert acc["fields"]["customers"]["status"] == "view"
    assert acc["visible_tabs"] == ["customers", "opportunities"]


def test_resolver_override_wins(db):
    t = AccessTemplate(name="RMG", is_active=True, tab_access={"projects": "view"})
    db.add(t); db.flush()
    db.add(UserProfile(user_id=6, access_template_id=t.id,
                       tab_access=json.dumps(["invoices"]),
                       field_access=json.dumps({"invoices": ["grand_total"]})))
    db.commit()
    acc = effective_access(db, 6, {"RMG"})
    assert acc["tabs"]["projects"] == "view"
    # Overrides grant "create": the legacy modal was binary show/hide, and a
    # granted tab carried FULL access under the old single write level.
    assert acc["tabs"]["invoices"] == "create"
    assert acc["fields"]["invoices"]["grand_total"] == "edit"
    assert acc["source"] == "template+override"


def test_resolver_no_template_is_role_default(db):
    db.add(UserProfile(user_id=7)); db.commit()
    acc = effective_access(db, 7, {"HR"})
    assert acc["visible_tabs"] is None and acc["full"] is False   # unrestricted role defaults


def test_resolver_legacy_crm_prefixed_override_normalizes(db):
    """Legacy per-user tab_access stores crm: keys; resolver must map to bare registry keys."""
    db.add(UserProfile(
        user_id=53,
        tab_access=json.dumps(["crm:customers", "crm:opportunities", "crm:dashboard"]),
    ))
    db.commit()
    acc = effective_access(db, 53, {"Sales"})
    assert acc["tabs"]["customers"] == "create"
    assert acc["tabs"]["opportunities"] == "create"
    assert acc["tabs"]["dashboard"] == "create"
    assert "crm:customers" not in acc["tabs"]
    assert acc["visible_tabs"] == ["customers", "dashboard", "opportunities"]


def test_resolver_inactive_template_falls_back_to_role_defaults(db):
    """Deactivating a template must WIDEN its users back to role defaults —
    the old contract (restricted with zero visible tabs) black-screened every
    user still linked to it. Assigning an inactive template is now refused at
    the API instead."""
    t = AccessTemplate(name="Old", is_active=False, tab_access={"customers": "edit"})
    db.add(t); db.flush()
    db.add(UserProfile(user_id=8, access_template_id=t.id)); db.commit()
    acc = effective_access(db, 8, {"Sales"})
    assert acc["tabs"] == {} and acc["visible_tabs"] is None      # unrestricted fallback
    assert acc["source"] == "role_default"


# ===================================================== BLACK-BOX: API
@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(at_router.router)
    app.include_router(me_router.router)

    def _db():
        yield db
    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="admin", roles={"Admin"})
    c = TestClient(app)
    c._db = db
    return c


def test_api_crud_and_assign(client):
    # registry lists grantable tabs + modes
    reg = client.get("/api/access-templates/registry").json()["data"]
    assert reg["modes"] == ["view", "edit", "create"]
    assert any(t["key"] == "customers" for t in reg["tabs"])

    # create a Sales template
    r = client.post("/api/access-templates", json={
        "name": "Sales", "role": "Sales",
        "tab_access": {"customers": "edit", "opportunities": "view"},
        "field_access": {"customers": {"name": "edit"}}})
    assert r.status_code == 200, r.text
    tid = r.json()["data"]["id"]

    # list + get
    assert any(t["name"] == "Sales" for t in client.get("/api/access-templates").json()["data"])
    got = client.get(f"/api/access-templates/{tid}").json()["data"]
    assert got["tab_access"]["customers"] == "edit"

    # update (flip opportunities to edit)
    r = client.put(f"/api/access-templates/{tid}", json={"tab_access": {"customers": "edit", "opportunities": "edit"}})
    assert r.json()["data"]["tab_access"]["opportunities"] == "edit"

    # assign to a user, then that user's effective access reflects the template (live)
    assert client.post("/api/access-templates/assign", json={"user_id": 20, "template_id": tid}).status_code == 200
    acc = effective_access(client._db, 20, {"Sales"})
    assert acc["template_id"] == tid and acc["tabs"]["opportunities"] == "edit"

    # delete unlinks the user
    assert client.delete(f"/api/access-templates/{tid}").status_code == 200
    acc2 = effective_access(client._db, 20, {"Sales"})
    assert acc2["template_id"] is None and acc2["visible_tabs"] is None


def test_api_validation_strips_unknown_tabs_but_rejects_bad_modes(client):
    """Registry-unknown TAB keys are silently dropped (deliberate: templates
    saved before a tab was removed must stay saveable). A bad MODE on a known
    tab is still a hard 400."""
    r = client.post("/api/access-templates", json={"name": "Bad", "tab_access": {"nope": "edit"}})
    assert r.status_code == 200
    assert "nope" not in ((r.json().get("data") or {}).get("tab_access") or {})
    r = client.post("/api/access-templates", json={"name": "Bad2", "tab_access": {"customers": "delete"}})
    assert r.status_code == 400


def test_api_admin_only(client):
    client.app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=2, username="sales", roles={"Sales"})
    assert client.get("/api/access-templates").status_code == 403
    assert client.post("/api/access-templates", json={"name": "X"}).status_code == 403


def test_me_returns_access_modes(client):
    # create + assign a template to the current (admin) user's id, then read /me
    tid = client.post("/api/access-templates", json={
        "name": "TA", "tab_access": {"candidates": "edit"}}).json()["data"]["id"]
    client.post("/api/access-templates/assign", json={"user_id": 1, "template_id": tid})
    me = client.get("/api/me").json()["data"]
    assert "access" in me
    # current user is Admin -> access.full True regardless of the assignment
    assert me["access"]["full"] is True
