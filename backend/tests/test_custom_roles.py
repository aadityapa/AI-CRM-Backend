"""Custom roles — Admin/CEO-defined roles with their own tab grants (23 Sep 2026).

The ask: "create a new role GM from Access Control and give it invoice access".
A custom role is data (name + the Access-Template grant map). It takes effect
through the two places every CRM request already passes: `get_current_user`
(the name joins `roles`, so "GM" is not "no CRM role") and `effective_access`
(the grants act like an authoritative template). Nothing gained a new gate.

Run:  cd backend && python -m pytest tests/test_custom_roles.py -q
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import Depends, FastAPI, HTTPException
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
    "custom_roles",
]:
    importlib.import_module(f"models.{_m}")

import crm_deps  # noqa: E402
from models.base import Base  # noqa: E402
from models import AccessTemplate, UserProfile  # noqa: E402
from models.custom_roles import CustomRole, UserCustomRole  # noqa: E402
from services import custom_roles as svc  # noqa: E402
from services.access_templates import effective_access  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (1, 2, 3):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _stub_user_rows(monkeypatch):
    """The ORM's registration_data is a one-column FK stub; stand in for the
    raw-SQL user lookup with the three seeded ids."""
    def fake(db_, user_ids):
        return {uid: {"id": uid, "full_name": f"User {uid}", "email": f"u{uid}@karnex.in",
                      "username": f"u{uid}", "is_active": True}
                for uid in user_ids if uid in (1, 2, 3)}
    monkeypatch.setattr(svc, "_user_rows", fake)


def _gm(db, uid=2, tabs=None, name="GM"):
    role = svc.create_role(db, {"name": name, "description": "General Manager",
                                "tab_access": tabs or {"invoices": "edit", "pos": "view"}}, actor_id=1)
    svc.set_members(db, role["id"], [uid])
    return role


# --------------------------------------------------------------- naming

def test_builtin_names_are_reserved(db):
    for taken in ("Finance", "finance", "Admin", "CEO", "Sales_Head"):
        with pytest.raises(HTTPException) as exc:
            svc.create_role(db, {"name": taken, "tab_access": {"invoices": "view"}}, actor_id=1)
        assert exc.value.status_code == 400


def test_duplicate_names_are_refused_case_insensitively(db):
    _gm(db)
    with pytest.raises(HTTPException) as exc:
        svc.create_role(db, {"name": "gm", "tab_access": {"invoices": "view"}}, actor_id=1)
    assert "already exists" in exc.value.detail


def test_a_role_must_grant_something(db):
    with pytest.raises(HTTPException) as exc:
        svc.create_role(db, {"name": "Empty", "tab_access": {}}, actor_id=1)
    assert exc.value.status_code == 400


def test_unknown_tab_keys_are_dropped_and_bad_modes_refused(db):
    role = svc.create_role(db, {"name": "GM", "tab_access": {"invoices": "edit", "no-such-tab": "edit"}}, actor_id=1)
    assert role["tab_access"] == {"invoices": "edit"}        # unknown keys are dead weight — dropped, as Access Templates do
    with pytest.raises(HTTPException):
        svc.create_role(db, {"name": "GM2", "tab_access": {"invoices": "admin"}}, actor_id=1)


# --------------------------------------------------------------- membership

def test_members_replace_and_unknown_users_are_refused(db):
    role = _gm(db, uid=2)
    assert [m["id"] for m in svc.list_members(db, role["id"])] == [2]
    svc.set_members(db, role["id"], [2, 3])
    assert [m["id"] for m in svc.list_members(db, role["id"])] == [2, 3] or \
           sorted(m["id"] for m in svc.list_members(db, role["id"])) == [2, 3]
    with pytest.raises(HTTPException) as exc:
        svc.set_members(db, role["id"], [999])
    assert exc.value.status_code == 400
    svc.set_members(db, role["id"], [])
    assert svc.list_members(db, role["id"]) == []


def test_delete_refuses_while_members_remain(db):
    role = _gm(db, uid=2)
    with pytest.raises(HTTPException) as exc:
        svc.delete_role(db, role["id"])
    assert exc.value.status_code == 409
    svc.set_members(db, role["id"], [])
    assert svc.delete_role(db, role["id"])["deleted"] is True


def test_listing_shows_builtin_and_custom_side_by_side(db):
    _gm(db, uid=2)
    out = svc.list_roles(db)
    assert {r["name"] for r in out["builtin"]} >= {"Admin", "CEO", "Finance", "Sales"}
    assert all(r["builtin"] for r in out["builtin"])
    assert [(r["name"], r["members_count"]) for r in out["custom"]] == [("GM", 1)]


# --------------------------------------------------------------- effect on access

def test_the_name_joins_the_users_role_set(db):
    _gm(db, uid=2)
    assert crm_deps.custom_role_names(db, 2) == {"GM"}
    assert crm_deps.custom_role_names(db, 3) == set()


def test_an_inactive_role_grants_nothing(db):
    role = _gm(db, uid=2)
    svc.update_role(db, role["id"], {"is_active": False})
    assert crm_deps.custom_role_names(db, 2) == set()
    assert effective_access(db, 2, {"GM"})["visible_tabs"] is None


def test_grants_resolve_like_an_authoritative_template(db):
    _gm(db, uid=2)
    acc = effective_access(db, 2, {"GM"})
    assert acc["source"] == "custom_role"
    assert acc["tabs"] == {"invoices": "edit", "pos": "view"}
    assert acc["visible_tabs"] == ["invoices", "pos"]


def test_two_roles_merge_with_the_higher_mode_winning(db):
    _gm(db, uid=2, tabs={"invoices": "view"}, name="GM")
    _gm(db, uid=2, tabs={"invoices": "create", "tds": "view"}, name="Auditor")
    acc = effective_access(db, 2, {"GM", "Auditor"})
    assert acc["tabs"] == {"invoices": "create", "tds": "view"}


def test_an_explicit_template_outranks_the_role(db):
    _gm(db, uid=2)
    t = AccessTemplate(name="Narrow", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.flush()
    db.add(UserProfile(user_id=2, access_template_id=t.id)); db.commit()
    acc = effective_access(db, 2, {"GM"})
    assert acc["source"] == "template"
    assert acc["tabs"] == {"dashboard": "view"}


def _client(db, user):
    app = FastAPI()

    @app.get("/inv", dependencies=[Depends(crm_deps.gated_read("invoices", "Finance", "Sales"))])
    def _r():
        return {"ok": True}

    @app.post("/inv", dependencies=[Depends(crm_deps.gated_write("invoices", "Finance"))])
    def _w():
        return {"ok": True}

    @app.post("/inv/create", dependencies=[Depends(crm_deps.gated_create("invoices", "Finance"))])
    def _c():
        return {"ok": True}

    @app.get("/hard", dependencies=[Depends(crm_deps.role_required("Finance"))])
    def _h():
        return {"ok": True}

    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: user
    return TestClient(app)


def test_gm_reaches_invoices_through_the_ordinary_gates(db):
    """The whole ask, end to end: a GM with invoices=edit reads and edits
    invoices, cannot create them (edit < create), and a hard `role_required`
    endpoint stays closed — exactly as an Access Template behaves."""
    _gm(db, uid=2)
    gm = _client(db, crm_deps.CurrentUser(id=2, username="gm", roles={"GM"}))
    assert gm.get("/inv").status_code == 200
    assert gm.post("/inv").status_code == 200
    assert gm.post("/inv/create").status_code == 403
    assert gm.get("/hard").status_code == 403


def test_a_user_with_only_a_custom_role_is_not_no_role(db):
    """`_gate` used to 403 "No CRM role assigned" on an empty role set; the
    custom role's name is what keeps that door open."""
    _gm(db, uid=2)
    roles = crm_deps.custom_role_names(db, 2)
    assert roles == {"GM"}
    gm = _client(db, crm_deps.CurrentUser(id=2, username="gm", roles=roles))
    assert gm.get("/inv").status_code == 200


# --------------------------------------------------------------- password reset

def test_reset_password_generates_a_policy_passing_temporary_password(db, monkeypatch):
    from services import users_admin
    import password_hashing as pwh
    stored = {}
    monkeypatch.setattr(users_admin, "_require_user_row",
                        lambda db_, uid: {"id": uid, "username": "gargee", "email": "g@karnex.in"})
    monkeypatch.setattr(users_admin, "_legacy_db_target", lambda: "sqlite:///ignored")
    monkeypatch.setattr("auth_db.update_user_password", lambda target, uid, h: stored.update({uid: h}))
    out = users_admin.reset_password(db, 7, None, actor_id=1)
    assert out["generated"] is True and out["temporary_password"]
    pwh.validate_password(out["temporary_password"])          # policy holds
    assert pwh.verify_password(out["temporary_password"], stored[7])[0] is True


def test_reset_password_honours_an_explicit_value_and_the_policy(db, monkeypatch):
    from services import users_admin
    stored = {}
    monkeypatch.setattr(users_admin, "_require_user_row",
                        lambda db_, uid: {"id": uid, "username": "gargee", "email": "g@karnex.in"})
    monkeypatch.setattr(users_admin, "_legacy_db_target", lambda: "sqlite:///ignored")
    monkeypatch.setattr("auth_db.update_user_password", lambda target, uid, h: stored.update({uid: h}))
    out = users_admin.reset_password(db, 7, "Str0ng!Passw0rd", actor_id=1)
    assert out["generated"] is False and out["temporary_password"] is None
    with pytest.raises(HTTPException) as exc:
        users_admin.reset_password(db, 7, "short", actor_id=1)
    assert exc.value.status_code == 400


def test_admins_cannot_reset_their_own_password_here(db):
    from services import users_admin
    with pytest.raises(HTTPException) as exc:
        users_admin.reset_password(db, 1, "Whatever1!x", actor_id=1)
    assert exc.value.status_code == 400


# --------------------------------------------------------------- registry

def test_router_is_registered_and_backed_up():
    from routers.crm import _MODULES
    from services.data_backup import DATASETS
    assert "custom_roles" in _MODULES
    tables = {t for d in DATASETS for t in d.tables}
    assert {"custom_roles", "user_custom_roles"} <= tables


# ------------------------------------------------ one source of access per user
# Reported 23 Sep 2026: a user in "Sales Manager" still showed "Default — Sales"
# in the Access Template dropdown, and that template silently outranked the
# role. A template and a custom role are now EXCLUSIVE.

def test_joining_a_role_drops_an_explicit_template(db):
    t = AccessTemplate(name="Default — Sales", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.flush()
    db.add(UserProfile(user_id=2, access_template_id=t.id)); db.commit()
    _gm(db, uid=2)
    profile = db.execute(__import__("sqlalchemy").select(UserProfile).where(UserProfile.user_id == 2)).scalars().first()
    assert profile.access_template_id is None
    assert effective_access(db, 2, {"GM"})["source"] == "custom_role"


def _give_builtin(db, uid: int, name: str = "Sales") -> None:
    from models.rbac import Role, RoleName, UserRole
    role = Role(name=RoleName(name))
    db.add(role); db.flush()
    db.add(UserRole(user_id=uid, role_id=role.id)); db.commit()


def test_assigning_a_template_drops_custom_roles(db):
    from services.access_templates import assign_template
    _give_builtin(db, 2)                       # keeps a role → can still enter the CRM
    _gm(db, uid=2)
    t = AccessTemplate(name="Narrow", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.commit()
    assign_template(db, 2, t.id)
    assert crm_deps.custom_role_names(db, 2) == set()
    assert svc.list_members(db, 1) == [] or all(m["id"] != 2 for m in svc.list_members(db, 1))


# ------------------------------------ a template never locks a user out (25 Sep)
# Reported with a screenshot: "No CRM role is assigned" right after a template
# was assigned. A template decides the TABS; a ROLE is what lets someone into
# the CRM, and one-source-of-access removed the user's only (custom) role.

def test_a_template_never_removes_a_users_last_role(db, monkeypatch):
    from services.access_templates import assign_template
    _users_admin(monkeypatch)
    _gm(db, uid=2)                             # GM is their ONLY role
    t = AccessTemplate(name="Narrow", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.commit()
    with pytest.raises(HTTPException) as exc:
        assign_template(db, 2, t.id)
    assert exc.value.status_code == 400 and "no built-in role" in exc.value.detail
    assert crm_deps.custom_role_names(db, 2) == {"GM"}        # nothing was taken away


def test_a_tagged_template_brings_its_built_in_role(db, monkeypatch):
    """The "Default — Sales" pattern: assigning it to a role-less user makes them Sales."""
    from services.access_templates import assign_template
    from services.users_admin import _roles_map
    _users_admin(monkeypatch)
    _gm(db, uid=2)
    t = AccessTemplate(name="Default — Sales", role="Sales", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.commit()
    out = assign_template(db, 2, t.id)
    assert out["role_added"] == "Sales"
    assert _roles_map(db, [2])[2] == ["Sales"]


def test_edit_roles_refuses_to_clear_every_role(db, monkeypatch):
    users_admin = _users_admin(monkeypatch)
    _give_builtin(db, 3)
    with pytest.raises(HTTPException) as exc:
        users_admin.replace_roles(db, 3, [], custom_role_ids=[])
    assert "at least one role" in exc.value.detail


def test_access_source_dropdown_is_exclusive(db, monkeypatch):
    from services import users_admin
    monkeypatch.setattr(users_admin, "_require_user_row",
                        lambda db_, uid: {"id": uid, "username": f"u{uid}", "email": "", "full_name": "", "role": "hr", "is_active": True})
    _give_builtin(db, 2)                       # a built-in role survives every switch below
    gm = svc.create_role(db, {"name": "GM", "tab_access": {"invoices": "edit"}}, actor_id=1)
    sm = svc.create_role(db, {"name": "Sales Manager", "tab_access": {"opportunities": "edit"}}, actor_id=1)
    t = AccessTemplate(name="Narrow", tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.commit()

    out = users_admin.set_access_source(db, 2, "role", gm["id"])
    assert out["custom_roles"] == ["GM"] and out["access_template_id"] is None
    out = users_admin.set_access_source(db, 2, "role", sm["id"])
    assert out["custom_roles"] == ["Sales Manager"]            # one role from this control
    out = users_admin.set_access_source(db, 2, "template", t.id)
    assert out["custom_roles"] == [] and out["access_template_id"] == t.id
    out = users_admin.set_access_source(db, 2, "default", None)
    assert out["custom_roles"] == [] and out["access_template_id"] is None
    with pytest.raises(HTTPException):
        users_admin.set_access_source(db, 2, "bogus", None)


# ------------------------------------------ Edit Roles dialog lists custom roles
# Reported 23 Sep 2026 (screenshot): the Users-tab "Edit Roles" dialog showed
# only the eight built-in roles — Sales Manager and GM were nowhere to pick.
# `POST /users/{id}/roles` now takes `custom_roles: [ids]` beside `roles`.

def _users_admin(monkeypatch):
    from services import users_admin
    monkeypatch.setattr(users_admin, "_require_user_row",
                        lambda db_, uid: {"id": uid, "username": f"u{uid}", "email": "", "full_name": "", "role": "hr", "is_active": True})
    return users_admin


def test_edit_roles_saves_builtin_and_custom_in_one_call(db, monkeypatch):
    users_admin = _users_admin(monkeypatch)
    gm = svc.create_role(db, {"name": "GM", "tab_access": {"invoices": "edit"}}, actor_id=1)
    sm = svc.create_role(db, {"name": "Sales Manager", "tab_access": {"opportunities": "edit"}}, actor_id=1)

    out = users_admin.replace_roles(db, 2, ["Sales"], custom_role_ids=[gm["id"], sm["id"]])
    assert out["roles"] == ["Sales"]
    assert out["custom_roles"] == ["GM", "Sales Manager"]
    assert crm_deps.custom_role_names(db, 2) == {"GM", "Sales Manager"}

    out = users_admin.replace_roles(db, 2, ["Sales"], custom_role_ids=[])     # a list REPLACES
    assert out["custom_roles"] == []
    users_admin.replace_roles(db, 2, [], custom_role_ids=[gm["id"]])
    out = users_admin.replace_roles(db, 2, ["TA"])                            # None leaves it alone
    assert out["custom_roles"] == ["GM"]


def test_edit_roles_refuses_unknown_and_inactive_custom_roles(db, monkeypatch):
    users_admin = _users_admin(monkeypatch)
    gm = svc.create_role(db, {"name": "GM", "tab_access": {"invoices": "edit"}, "is_active": False}, actor_id=1)
    with pytest.raises(HTTPException) as exc:
        users_admin.replace_roles(db, 2, [], custom_role_ids=[999])
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        users_admin.replace_roles(db, 2, [], custom_role_ids=[gm["id"]])
    assert "Inactive" in exc.value.detail


def test_role_defaults_never_reattach_a_template_over_a_custom_role(db, monkeypatch):
    """`_auto_assign_role_template` runs on EVERY Edit Roles save and used to
    hand a Sales Manager the template tagged 'Sales' — which then outranked
    the custom role. That is exactly how Balasaheb got 'Default — Sales'."""
    users_admin = _users_admin(monkeypatch)
    t = AccessTemplate(name="Default — Sales", role="Sales", is_active=True,
                       tab_access={"dashboard": "view"}, field_access={})
    db.add(t); db.commit()
    sm = svc.create_role(db, {"name": "Sales Manager", "tab_access": {"opportunities": "edit"}}, actor_id=1)

    out = users_admin.replace_roles(db, 2, ["Sales"], custom_role_ids=[sm["id"]])
    assert out["access_template_id"] is None
    assert effective_access(db, 2, {"Sales", "Sales Manager"})["source"] == "custom_role"

    out = users_admin.replace_roles(db, 2, ["Sales"])                        # a later plain save, too
    assert out["access_template_id"] is None

    out = users_admin.replace_roles(db, 3, ["Sales"])                        # no custom role → default still applies
    assert out["access_template_id"] == t.id


# --------------------------------------------------------------- Sales Manager = the whole of Sales

def test_the_sales_manager_is_above_sales_and_below_sales_head(db):
    """29 Sep 2026 (later), user rule — a ladder: "Sales has limited access, the
    Sales Manager more than Sales, the Sales Head all of Sales". The custom role
    passes every SALES check (never a Sales Head gate), sees the whole team,
    approves what Sales + Sales Head approve by default, and hears Sales Head
    notices."""
    from services.role_implications import implied_roles, sees_team, with_implied
    assert implied_roles(["Sales Manager"]) == {"Sales"}
    assert implied_roles(["sales manager "]) == {"Sales"}                 # names are case-insensitive
    assert implied_roles(["GM", "TA"]) == set()
    assert with_implied({"Sales Manager"}) == {"Sales Manager", "Sales"}
    assert sees_team({"Sales Manager", "Sales"}) and not sees_team({"Sales"})

    _gm(db, uid=2, name="Sales Manager", tabs={"opportunities": "edit"})
    from services.access_templates import _custom_role_grants
    _tabs, _fields, actions = _custom_role_grants(db, 2)
    # A new Sales Manager role starts with the approvals Sales + Sales Head have.
    assert {"opportunity.approve", "profile.sales_head_decision", "profile.budget_resolve"} <= set(actions)
    from services.notify import _user_ids_in_role
    assert 2 in _user_ids_in_role(db, "Sales_Head")                     # a Sales Head notice reaches them


def test_the_0112_merge_raises_grants_and_never_lowers_them():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0112_sales_manager_full_sales.py"
    spec = importlib.util.spec_from_file_location("m0112", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._merge_tabs({"invoices": "create", "pos": "view"}, {"invoices": "view", "pos": "edit", "profiles": "edit"}) \
        == {"invoices": "create", "pos": "edit", "profiles": "edit"}
    assert mod._merge_fields({"customers": {"gst": "edit"}}, {"customers": {"gst": "view", "pan": "view"}}) \
        == {"customers": {"gst": "edit", "pan": "view"}}
    # The snapshot names exactly the approvals the registry gives Sales / Sales Head.
    from services.action_permissions import ACTIONS, APPROVAL_ACTIONS
    assert set(mod._SALES_APPROVALS) == {k for k in APPROVAL_ACTIONS
                                         if {"Sales", "Sales_Head"} & set(ACTIONS[k].roles)}
