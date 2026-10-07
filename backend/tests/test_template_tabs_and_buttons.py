"""One template decides every tab and every button (7 Oct 2026).

* Interview Platform tabs are grantable in an Access Template; a template that
  names any of them is AUTHORITATIVE for the legacy Interview Platform routes
  (`crm_deps.enforce_roles(tab=)`), and a template that names none keeps the
  role rule (nothing changes for templates saved before).
* Manage buttons join the Approvals list: a configured list decides every
  button; a never-configured list keeps "tab Edit is enough".
* Migration 0124's backfill logic.

Run:  cd backend && python -m pytest tests/test_template_tabs_and_buttons.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
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


for _m in ["base", "rbac", "access_templates", "user_profiles", "custom_roles"]:
    importlib.import_module(f"models.{_m}")

import crm_deps  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from models import UserProfile  # noqa: E402
from models.base import Base  # noqa: E402
from services import access_registry as reg  # noqa: E402
from services import access_templates as tpl_svc  # noqa: E402
from services import action_permissions as ap  # noqa: E402
from services import custom_roles as role_svc  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


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
def _code_defaults(monkeypatch):
    monkeypatch.setattr(ap, "roles_for_action", lambda action, defaults: list(defaults))
    monkeypatch.setattr(role_svc, "_user_rows", lambda db_, ids: {
        uid: {"id": uid, "full_name": f"User {uid}", "email": f"u{uid}@karnex.in",
              "username": f"u{uid}", "is_active": True} for uid in ids})


def _user(uid: int, *roles: str) -> CurrentUser:
    return CurrentUser(id=uid, username=f"u{uid}", full_name=f"User {uid}", roles=set(roles))


def _template(db, uid, tabs, actions=None, role=None):
    payload = {"name": f"T{uid}", "role": role, "tab_access": tabs}
    if actions is not None:
        payload["action_access"] = actions
    t = tpl_svc.create_template(db, payload)
    db.add(UserProfile(user_id=uid, access_template_id=t["id"]))
    db.commit()
    return t


# ------------------------------------------------------- Interview Platform tabs


def test_interview_platform_tabs_are_in_the_registry_and_survive_a_save(db):
    assert set(reg.IV_TABS) <= set(reg.TABS)
    assert "iv:promptLogs" not in reg.TABS               # Admin-only on the server, never grantable
    groups = {t["key"]: t["group"] for t in reg.registry()["tabs"]}
    assert groups["iv:integrityLogs"] == "Interview Platform" and groups["customers"] == "CRM"
    t = tpl_svc.create_template(db, {"name": "RMG+", "role": "RMG",
                                      "tab_access": {"profiles": "edit", "iv:integrityLogs": "view"}})
    assert t["tab_access"]["iv:integrityLogs"] == "view"   # not stripped as unknown
    t = tpl_svc.create_template(db, {"name": "bad", "tab_access": {"iv:questionBank": "view", "iv:ats": "view"}})
    assert t["tab_access"] == {"iv:ats": "view"}           # an ungrantable key is dropped, never saved


def _platform_app(db, user: CurrentUser, roles_db: dict[str, set[str]]):
    """A legacy-style route guarded by enforce_roles(tab=), with the user lookup stubbed."""
    app = FastAPI()

    def fake_lookup(username, tab=None):
        roles = roles_db[username]
        uid = int(username[1:])
        template_mode = None
        if tab and not roles & {"Admin", "CEO"}:
            acc = tpl_svc.effective_access(db, uid, roles)
            tabs = acc.get("tabs", {}) if acc.get("visible_tabs") is not None else {}
            if not acc.get("full") and any(k.startswith("iv:") for k in tabs):
                template_mode = tabs.get(tab) or ""
        return uid, roles, template_mode

    @app.get("/integrity")
    def integrity(request: Request):
        crm_deps.enforce_roles(request, "TA", "HR", tab="iv:integrityLogs")
        return {"ok": True}

    @app.get("/templates-edit")
    def templates(request: Request):
        crm_deps.enforce_roles(request, "RMG", tab="iv:templates", mode="edit")
        return {"ok": True}

    return app, fake_lookup


def test_a_template_that_names_a_platform_tab_decides_it_whatever_the_role(db, monkeypatch):
    # The report: Integrity ticked for an RMG, the server still said 403.
    _template(db, 2, {"profiles": "edit", "iv:integrityLogs": "view", "iv:templates": "view"})
    app, lookup = _platform_app(db, _user(2, "RMG"), {"u2": {"RMG"}})
    monkeypatch.setattr(crm_deps, "_decode_bearer", lambda request: {"sub": "u2"})
    monkeypatch.setattr(crm_deps, "_user_access_for_username", lookup)
    c = TestClient(app)
    assert c.get("/integrity").status_code == 200           # granted → in, although RMG
    r = c.get("/templates-edit")                            # view only → edit refused, although RMG
    assert r.status_code == 403 and "edit level" in r.json()["detail"]


def test_a_template_naming_no_platform_tab_keeps_the_role_rule(db, monkeypatch):
    _template(db, 2, {"profiles": "edit"})                  # saved before the tabs were grantable
    app, lookup = _platform_app(db, _user(2, "RMG"), {"u2": {"RMG"}})
    monkeypatch.setattr(crm_deps, "_decode_bearer", lambda request: {"sub": "u2"})
    monkeypatch.setattr(crm_deps, "_user_access_for_username", lookup)
    c = TestClient(app)
    assert c.get("/integrity").status_code == 403           # RMG is excluded by role, as before
    assert c.get("/templates-edit").status_code == 200      # RMG authors templates, as before


def test_a_template_naming_a_platform_tab_hides_the_others(db, monkeypatch):
    _template(db, 2, {"iv:ats": "view"})
    app, lookup = _platform_app(db, _user(2, "TA"), {"u2": {"TA"}})
    monkeypatch.setattr(crm_deps, "_decode_bearer", lambda request: {"sub": "u2"})
    monkeypatch.setattr(crm_deps, "_user_access_for_username", lookup)
    assert TestClient(app).get("/integrity").status_code == 403   # TA by role, but the template rules


# ------------------------------------------------------------------- buttons


def _gate_client(db, user, action, tab):
    app = FastAPI()

    @app.post("/act")
    def act(u: CurrentUser = Depends(crm_deps.gated_write_action(action, tab))):
        return {"ok": True}

    app.dependency_overrides[crm_deps.get_current_user] = lambda: user
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    return TestClient(app)


def test_the_registry_lists_every_button_with_its_kind_and_tab():
    keys = [r["key"] for r in ap.registry()]
    assert "po.manage" in keys and "timesheet.approve" in keys
    assert ap.clean_action_list(["po.manage", "nope", "timesheet.approve"]) == ["timesheet.approve", "po.manage"]
    assert "po.manage" in ap.default_actions_for_role("Finance")
    assert "po.manage" not in ap.default_actions_for_role("TA")


def test_a_configured_list_decides_the_manage_buttons(db):
    # Invoices at Edit, but the list grants only the change request — the
    # create / edit button is off.
    _template(db, 2, {"invoices": "edit"}, actions=["invoice.revision.request"])
    fin = _user(2, "Finance")
    assert _gate_client(db, fin, "invoice.revision.request", "invoices").post("/act").status_code == 200
    r = _gate_client(db, fin, "invoice.manage", "invoices").post("/act")
    assert r.status_code == 403 and "create / edit invoices" in r.json()["detail"]
    # …and the list can GRANT a button the role never had, once the tab is visible.
    _template(db, 3, {"pos": "view"}, actions=["po.manage"])
    assert _gate_client(db, _user(3, "TA"), "po.manage", "pos").post("/act").status_code == 200


def test_a_never_configured_list_keeps_tab_edit_as_the_rule(db):
    t = _template(db, 2, {"invoices": "edit"}, role=None)
    # Untagged: no role defaults, but the buttons the tab grants imply.
    assert t["action_access"] == ["invoice.manage", "invoice.revision.request"]
    from models import AccessTemplate
    row = db.get(AccessTemplate, t["id"])
    row.action_access = None                                # a pre-0108 template
    db.commit()
    assert _gate_client(db, _user(2, "TA"), "invoice.manage", "invoices").post("/act").status_code == 200


def test_a_new_tagged_template_starts_with_the_roles_buttons(db):
    t = tpl_svc.create_template(db, {"name": "Fin", "role": "Finance", "tab_access": {"invoices": "edit"}})
    assert {"invoice.manage", "po.manage", "invoice.convert_proforma"} <= set(t["action_access"])


# ----------------------------------------------------------------- migration


def _migration():
    path = next((BACKEND / "alembic" / "versions").glob("0124_*.py"))
    spec = importlib.util.spec_from_file_location("m0124", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_0124_snapshots_match_the_registry():
    mod = _migration()
    assert mod._IV_TAB_ROLES == reg.IV_TAB_DEFAULT_ROLES
    assert mod._IV_ROLE_ALIASES == reg.IV_ROLE_ALIASES
    assert mod._BUTTON_TABS == {k: ap.ACTIONS[k].tab for k in ap.MANAGE_ACTIONS}


def test_rmg_and_the_gm_open_reports_ats_and_integrity_by_default():
    """User decision 7 Oct 2026: RMG / GM screen the AI L1 outcome, so they
    read the report, the ATS page and the integrity log without a hand-made grant."""
    wanted = {"iv:candidates", "iv:ats", "iv:integrityLogs"}
    assert wanted <= set(reg.iv_tabs_for_roles(["RMG"]))
    assert reg.iv_tabs_for_roles(["GM"]) == reg.iv_tabs_for_roles(["RMG"])   # the alias
    assert reg.iv_tabs_for_roles(["Finance"]) == {}


def test_the_0124_backfill_adds_only_what_the_old_rules_already_gave(db):
    mod = _migration()
    from sqlalchemy import text
    from models import Role, RoleName, UserRole
    rmg = Role(name=RoleName.RMG); db.add(rmg); db.flush()
    db.add(UserRole(user_id=2, role_id=rmg.id))
    # Template A: Finance-tagged, invoices at edit, pos at view, list configured.
    a = _template(db, 1, {"invoices": "edit", "pos": "view", "projects": "create"},
                  actions=["invoice.convert_proforma"], role="Finance")
    # Template B: untagged, used by an RMG (user 2), no platform tabs → gets RMG's platform tabs.
    b = _template(db, 2, {"profiles": "edit"}, actions=[])
    # Template C: already names a platform tab → untouched.
    c = _template(db, 3, {"iv:ats": "view"}, actions=[], role="TA")
    db.commit()
    mod._backfill_buttons(db.connection(), "access_templates")
    mod._backfill_iv_tabs(db.connection())
    db.commit()
    rows = {r[0]: (r[1], r[2]) for r in db.execute(text(
        "SELECT id, tab_access, action_access FROM access_templates")).all()}
    import json
    ta, aa = (json.loads(x) if isinstance(x, str) else x for x in rows[a["id"]])
    assert set(aa) == {"invoice.convert_proforma", "invoice.manage", "invoice.revision.request", "project.close"}
    assert not any(k.startswith("iv:") for k in ta)   # Finance tag → no platform tabs; no members with roles
    tb, _ = (json.loads(x) if isinstance(x, str) else x for x in rows[b["id"]])
    assert {k for k in tb if k.startswith("iv:")} == {"iv:dashboard", "iv:templates", "iv:candidates", "iv:ats", "iv:integrityLogs"}
    tc, _ = (json.loads(x) if isinstance(x, str) else x for x in rows[c["id"]])
    assert {k for k in tc if k.startswith("iv:")} == {"iv:ats"}


def test_the_0124_backfill_gives_the_gm_role_rmgs_platform_tabs(db):
    mod = _migration()
    import json
    from sqlalchemy import text
    from models.custom_roles import CustomRole
    gm = CustomRole(name="GM", tab_access={"profiles": "edit"}, field_access={}, is_active=True)
    sm = CustomRole(name="Sales Manager", tab_access={"opportunities": "edit"}, field_access={}, is_active=True)
    db.add_all([gm, sm]); db.commit()
    mod._backfill_custom_role_iv_tabs(db.connection())
    db.commit()
    rows = {r[0]: (json.loads(r[1]) if isinstance(r[1], str) else r[1])
            for r in db.execute(text("SELECT name, tab_access FROM custom_roles")).all()}
    assert {k for k in rows["GM"] if k.startswith("iv:")} == set(reg.iv_tabs_for_roles(["RMG"]))
    assert not any(k.startswith("iv:") for k in rows["Sales Manager"])
