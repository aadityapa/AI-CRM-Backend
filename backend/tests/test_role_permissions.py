"""Role permissions — Access Templates merged into Roles (27 Oct 2026).

White-box: the `effective_access` resolver (configured built-in + custom roles
merged, the per-user override on top, Admin/CEO full) and migration 0149's
planner. Black-box: `/api/roles` built-in permissions + the registry at both
addresses, and `/api/me` returning the modes.

Run:  python -m pytest tests/test_role_permissions.py -q
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
           "access_templates", "custom_roles"]:
    importlib.import_module(f"models.{_m}")

from fastapi import FastAPI                                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
from models.user_profiles import UserProfile                           # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.access_templates as at_router                       # noqa: E402
import routers.crm.custom_roles as roles_router                         # noqa: E402
from tests.role_grants import give_role, set_builtin                     # noqa: E402
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
    assert effective_access(db, 1, {"CEO"})["full"] is True


def test_a_configured_built_in_role_restricts_its_holders(db):
    set_builtin(db, 5, "Sales", {"customers": "edit", "opportunities": "view"},
                {"customers": {"name": "edit", "status": "view"}})
    acc = effective_access(db, 5, {"Sales"})
    assert acc["full"] is False and acc["source"] == "role"
    assert acc["tabs"] == {"customers": "edit", "opportunities": "view",
                           "branch-policy": "view"}   # implied: Customers opens the branch page
    assert acc["fields"]["customers"]["status"] == "view"
    assert acc["visible_tabs"] == ["branch-policy", "customers", "opportunities"]


def test_an_unconfigured_built_in_role_adds_nothing_to_a_restricted_login(db):
    set_builtin(db, 6, "RMG", None)                       # follows the role rules
    assert effective_access(db, 6, {"RMG"})["visible_tabs"] is None
    give_role(db, 6, {"projects": "view"})
    acc = effective_access(db, 6, {"RMG"})
    assert acc["tabs"] == {"projects": "view"}


def test_resolver_override_wins(db):
    give_role(db, 6, {"projects": "view"})
    db.add(UserProfile(user_id=6, tab_access=json.dumps(["invoices"]),
                       field_access=json.dumps({"invoices": ["grand_total"]})))
    db.commit()
    acc = effective_access(db, 6, {"RMG"})
    assert acc["tabs"]["projects"] == "view"
    assert acc["tabs"]["invoices"] == "create"
    assert acc["fields"]["invoices"]["grand_total"] == "edit"
    assert acc["source"] == "role+override"


def test_resolver_no_configured_role_is_role_default(db):
    db.add(UserProfile(user_id=7)); db.commit()
    acc = effective_access(db, 7, {"HR"})
    assert acc["visible_tabs"] is None and acc["full"] is False


def test_resolver_legacy_crm_prefixed_override_normalizes(db):
    db.add(UserProfile(user_id=53,
                       tab_access=json.dumps(["crm:customers", "crm:opportunities", "crm:dashboard"])))
    db.commit()
    acc = effective_access(db, 53, {"Sales"})
    assert acc["tabs"]["customers"] == "create"
    assert "crm:customers" not in acc["tabs"]
    assert acc["visible_tabs"] == ["branch-policy", "customers", "dashboard", "opportunities"]


def test_an_inactive_custom_role_restricts_nobody(db):
    give_role(db, 8, {"customers": "edit"}, is_active=False)
    acc = effective_access(db, 8, {"Sales"})
    assert acc["tabs"] == {} and acc["visible_tabs"] is None and acc["source"] == "role_default"


def test_buttons_merge_by_the_rule():
    from services.access_templates import merge_role_grants
    # No role lists its buttons → the action role lists decide.
    assert merge_role_grants([("Sales", False, {"x": "view"}, {}, None)])[2] is None
    # A list somewhere → union; a custom role without one adds its name's defaults.
    _, _, acts = merge_role_grants([("Sales", False, {"a": "view"}, {}, ["po.manage"]),
                                    ("GM", True, {"b": "view"}, {}, None)])
    assert "po.manage" in acts and "timesheet.approve" in acts
    # Sub-tabs one role SHOWS stay shown.
    _, fields, _ = merge_role_grants([("A", True, {"reports": "view"}, {"reports": {"tab:revenue": "view"}}, []),
                                      ("B", True, {"reports": "view"}, {"reports": {"tab:revenue": "hidden"}}, [])])
    assert fields["reports"]["tab:revenue"] == "view"


# ===================================================== BLACK-BOX: API
@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(at_router.router)
    app.include_router(roles_router.router)
    app.include_router(me_router.router)

    def _db():
        yield db
    app.dependency_overrides[crm_deps.get_crm_db] = _db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="admin", roles={"Admin"})
    c = TestClient(app)
    c._db = db
    return c


def test_registry_answers_at_both_addresses(client):
    for url in ("/api/roles/registry", "/api/access-templates/registry"):
        reg = client.get(url).json()["data"]
        assert reg["modes"] == ["view", "edit", "create"]
        assert any(t["key"] == "customers" for t in reg["tabs"])
        assert reg["suggested"]["Sales"]["opportunities"] == "create"


def test_template_crud_is_gone(client):
    assert client.get("/api/access-templates").status_code in (404, 405)
    assert client.post("/api/access-templates/assign", json={"user_id": 2, "template_id": 1}).status_code in (404, 405)


def test_built_in_permissions_over_the_api(client):
    set_builtin(client._db, 20, "TA", None)
    r = client.put("/api/roles/builtin/TA/permissions", json={
        "tab_access": {"candidates": "create", "nope": "edit"}, "action_access": ["candidate.email"]})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["configured"] is True and data["tab_access"] == {"candidates": "create"}
    acc = effective_access(client._db, 20, {"TA"})
    assert acc["tabs"] == {"candidates": "create"} and acc["actions"] == ["candidate.email"]
    listing = {x["name"]: x for x in client.get("/api/roles").json()["data"]["builtin"]}
    assert listing["TA"]["configured"] is True
    assert client.put("/api/roles/builtin/TA/permissions", json={"tab_access": {"customers": "delete"}}).status_code == 400
    assert client.put("/api/roles/builtin/Admin/permissions", json={"tab_access": {"customers": "view"}}).status_code == 400
    assert client.delete("/api/roles/builtin/TA/permissions").status_code == 200
    assert effective_access(client._db, 20, {"TA"})["visible_tabs"] is None


def test_api_admin_only(client):
    client.app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=2, username="sales", roles={"Sales"})
    assert client.get("/api/roles/registry").status_code == 403
    assert client.put("/api/roles/builtin/Sales/permissions", json={"tab_access": {"x": "view"}}).status_code == 403


def test_me_returns_access_modes(client):
    me = client.get("/api/me").json()["data"]
    assert me["access"]["full"] is True


# ===================================================== migration 0149
def _m0149():
    import importlib.util
    from pathlib import Path
    path = next((Path(__file__).resolve().parents[1] / "alembic" / "versions").glob("0149_*.py"))
    spec = importlib.util.spec_from_file_location("m0149", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _t(tid, name, role=None, tabs=None, active=True, actions=None):
    return {"id": tid, "name": name, "role": role, "is_active": active,
            "tab_access": tabs if tabs is not None else {"dashboard": "view"}, "field_access": {},
            "action_access": actions}


def test_role_names_are_valid_and_unique():
    mod = _m0149()
    assert mod.role_name_for("Default — Sales", set()) == "Default - Sales"
    assert mod.role_name_for("Sales", set()) == "Sales access"                  # built-in name reserved
    assert mod.role_name_for("Ops", {"ops"}) == "Ops access"
    assert mod.role_name_for("—", set()).startswith("Access")
    assert len(mod.role_name_for("x" * 80, set())) <= 40


def test_a_default_template_becomes_its_built_in_roles_permissions():
    mod = _m0149()
    p = mod.plan([_t(1, "Default — Sales", "Sales"), _t(2, "Sales lite", "Sales")],
                 {10: 1, 11: 1, 12: 2, 13: 1},
                 {10: {"Sales"}, 11: {"Sales"}, 12: {"Sales"}, 13: {"TA"}}, [])
    assert p["builtin"] == {"Sales": 1}                   # most assignees wins
    created = {c["template_id"]: c for c in p["create"]}
    assert created[1]["members"] == [13]                  # assigned, but not a Sales holder
    assert created[2]["members"] == [12] and created[2]["name"] == "Sales lite"


def test_a_template_tagged_with_a_custom_role_folds_into_it_only_when_exact():
    mod = _m0149()
    roles = [{"id": 7, "name": "Hiring Manager", "is_active": True, "members": {20, 21}}]
    p = mod.plan([_t(3, "HM view", "Hiring Manager")], {20: 3, 21: 3}, {}, roles)
    assert p["overwrite"] == {7: 3} and p["create"] == []
    p = mod.plan([_t(3, "HM view", "Hiring Manager")], {20: 3}, {}, roles)   # 21 holds HM without it
    assert p["overwrite"] == {} and p["create"][0]["members"] == [20]


def test_inactive_unassigned_and_empty_templates_are_not_converted():
    mod = _m0149()
    p = mod.plan([_t(4, "Old", active=False), _t(5, "Spare"), _t(6, "Empty", tabs={})],
                 {30: 4, 31: 6}, {}, [])
    assert p["builtin"] == {} and p["overwrite"] == {} and p["create"] == []
    assert len(p["report"]) == 3


def test_the_0149_upgrade_moves_assignments_onto_roles(db):
    """End to end on SQLite: the SQL the migration runs, not only the planner."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from models import AccessTemplate
    from models.custom_roles import CustomRole, UserCustomRole

    mod = _m0149()
    set_builtin(db, 40, "Sales", None)
    set_builtin(db, 41, "TA", None)
    sales_t = AccessTemplate(name="Default — Sales", role="Sales", is_active=True,
                             tab_access={"opportunities": "create"}, field_access={},
                             action_access=["po.manage"])
    ops_t = AccessTemplate(name="Ops", is_active=True, tab_access={"projects": "view"}, field_access={})
    db.add_all([sales_t, ops_t]); db.flush()
    db.add_all([UserProfile(user_id=40, access_template_id=sales_t.id),
                UserProfile(user_id=41, access_template_id=ops_t.id)])
    db.commit()
    conn = db.connection()
    with Operations.context(MigrationContext.configure(conn)):
        mod.upgrade()
    db.commit()
    db.expire_all()
    acc = effective_access(db, 40, {"Sales"})
    assert acc["tabs"] == {"opportunities": "create"} and acc["actions"] == ["po.manage"]
    role = db.query(CustomRole).filter(CustomRole.name == "Ops").one()
    assert db.query(UserCustomRole).filter_by(user_id=41, custom_role_id=role.id).count() == 1
    assert effective_access(db, 41, {"TA"})["tabs"] == {"projects": "view"}
    assert db.query(UserProfile).filter(UserProfile.access_template_id.isnot(None)).count() == 0
