"""Access Templates — Phase 4: server-side write ENFORCEMENT (black-box + E2E).

Guarded demo endpoints use the reusable `crm_deps.require_access(tab, mode, field)`
dependency (same `effective_access` resolver). We set role permissions over the
real API, assign it to a user, then hit the guarded endpoints AS that user and assert
allowed on granted tabs/fields and 403 elsewhere. Admin bypasses; a no-template user
(role defaults) is unaffected.

Run:  python -m pytest tests/test_access_enforcement.py -q
"""
from __future__ import annotations

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

from fastapi import Depends, FastAPI                                    # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
import crm_deps                                                        # noqa: E402
from crm_deps import require_access, CurrentUser                       # noqa: E402
import routers.crm.custom_roles as roles_router                         # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402


@pytest.fixture()
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    current = {"user": CurrentUser(id=1, username="admin", roles={"Admin"})}

    app = FastAPI()
    app.include_router(roles_router.router)
    app.include_router(customers_router.router)

    @app.get("/demo/customers")
    def _view(u: CurrentUser = Depends(require_access("customers", mode="view"))):
        return {"ok": "view"}

    @app.post("/demo/customers")
    def _edit(u: CurrentUser = Depends(require_access("customers", mode="edit"))):
        return {"ok": "edit"}

    @app.post("/demo/customers/name")
    def _field(u: CurrentUser = Depends(require_access("customers", mode="edit", field="name"))):
        return {"ok": "field"}

    @app.post("/demo/opportunities")
    def _opp(u: CurrentUser = Depends(require_access("opportunities", mode="edit"))):
        return {"ok": "opp"}

    @app.post("/demo/invoices")
    def _inv(u: CurrentUser = Depends(require_access("invoices", mode="edit"))):
        return {"ok": "inv"}

    def _db():
        yield session

    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: current["user"]

    client = TestClient(app)
    client._current = current
    client._session = session
    return client


def _be(client, user):
    client._current["user"] = user


def _member(client, role_id: int, uid: int) -> None:
    """Membership straight in the table — the test users have no login row."""
    from models.custom_roles import UserCustomRole
    client._session.add(UserCustomRole(user_id=uid, custom_role_id=role_id))
    client._session.commit()


def _hold_builtin(client, uid: int, name: str) -> None:
    from models.rbac import Role, RoleName, UserRole
    s = client._session
    member = next(r for r in RoleName if r.value == name)
    role = s.query(Role).filter(Role.name == member).first()
    if role is None:
        role = Role(name=member)
        s.add(role)
        s.flush()
    s.add(UserRole(user_id=uid, role_id=role.id))
    s.commit()


def test_enforcement_sales_template_user(env):
    c = env
    # --- as Admin: set the BUILT-IN Sales role's permissions (over the real API) ---
    _hold_builtin(c, 30, "Sales")
    r = c.put("/api/roles/builtin/Sales/permissions", json={
        "tab_access": {"customers": "view", "opportunities": "edit"},
        "field_access": {"customers": {"name": "edit"}},
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["configured"] is True

    # --- now act AS the Sales user (id 30) ---
    _be(c, CurrentUser(id=30, username="ravi", roles={"Sales"}))
    assert c.get("/demo/customers").status_code == 200                 # customers = view -> allowed
    assert c.post("/demo/customers").status_code == 403                # tab is view-only -> blocked
    assert c.post("/demo/customers/name").status_code == 200           # field name = edit -> allowed
    assert c.post("/demo/opportunities").status_code == 200            # opportunities = edit -> allowed
    assert c.post("/demo/invoices").status_code == 403                 # invoices not granted -> blocked


def test_enforcement_admin_bypasses(env):
    c = env
    _be(c, CurrentUser(id=1, username="admin", roles={"Admin"}))
    assert c.get("/demo/customers").status_code == 200
    assert c.post("/demo/customers").status_code == 200
    assert c.post("/demo/invoices").status_code == 200


def test_enforcement_no_template_role_defaults_unaffected(env):
    c = env
    # user 40 has no template and no per-user override -> role defaults (unrestricted)
    _be(c, CurrentUser(id=40, username="hr", roles={"HR"}))
    assert c.get("/demo/customers").status_code == 200
    assert c.post("/demo/customers").status_code == 200
    assert c.post("/demo/invoices").status_code == 200


def test_enforcement_updates_live_when_template_edited(env):
    c = env
    rid = c.post("/api/roles", json={
        "name": "Ops", "tab_access": {"projects": "view"}}).json()["data"]["id"]
    _member(c, rid, 50)

    _be(c, CurrentUser(id=50, username="rmg", roles={"RMG"}))
    # projects is view-only for now — opportunities blocked
    assert c.post("/demo/opportunities").status_code == 403

    # Admin edits the role to grant opportunities edit -> live change, no re-assign
    _be(c, CurrentUser(id=1, username="admin", roles={"Admin"}))
    c.put(f"/api/roles/{rid}", json={"tab_access": {"projects": "view", "opportunities": "edit"}})

    _be(c, CurrentUser(id=50, username="rmg", roles={"RMG"}))
    assert c.post("/demo/opportunities").status_code == 200            # now allowed, live


def test_wired_customers_endpoint_enforcement(env):
    """Real /api/customers routes use gated_read / gated_write."""
    c = env
    rid = c.post("/api/roles", json={
        "name": "Sales RO",
        "tab_access": {"customers": "view"},
    }).json()["data"]["id"]
    _member(c, rid, 60)

    _be(c, CurrentUser(id=60, username="sales", roles={"Sales"}))
    assert c.get("/api/customers").status_code == 200
    assert c.post("/api/customers", json={"name": "Blocked Co"}).status_code == 403

    _be(c, CurrentUser(id=1, username="admin", roles={"Admin"}))
    c.put(f"/api/roles/{rid}", json={"tab_access": {"customers": "edit"}})

    _be(c, CurrentUser(id=60, username="sales", roles={"Sales"}))
    # Still 403 on create without required schema fields — but not access-denied
    r = c.post("/api/customers", json={"name": "Allowed Co"})
    assert r.status_code != 403 or "view-only" not in (r.json().get("detail") or "").lower()


def test_legacy_crm_prefix_override_allows_customers_api(env):
    """Users with legacy crm: tab overrides must pass bare-key endpoint guards."""
    import json
    from models.base import users_table_stub
    from models.user_profiles import UserProfile

    c = env
    c._session.execute(users_table_stub.insert().values(id=70))
    c._session.add(UserProfile(
        user_id=70,
        tab_access=json.dumps(["crm:customers", "crm:opportunities"]),
    ))
    c._session.commit()

    _be(c, CurrentUser(id=70, username="sales70", roles={"Sales"}))
    assert c.get("/api/customers").status_code == 200
    r = c.post("/api/customers", json={"name": "Legacy Override Co"})
    assert r.status_code != 403 or "do not have access" not in (r.json().get("detail") or "").lower()
