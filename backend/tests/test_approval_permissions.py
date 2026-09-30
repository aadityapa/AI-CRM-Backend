"""Approval buttons are their own grant (25 Sep 2026).

Reported with a screenshot: a Sales login (Sanjana) filled and submitted a
timesheet and was then offered Approve / Reject on it. Root cause: the
approve gate accepted ANY templated user whose template granted
Timesheets: Edit — exactly the grant Sales needs to fill the sheet — and the UI
mirrored it. The same hole sat under every approval button behind
`gated_write(tab, Role)`: opportunity / requirement approval, the Sales Head
terms decision, credit notes …

Now an APPROVAL action is decided by `action_permissions.user_may`: Admin/CEO
always; a templated / custom-role user by that template's or role's own
Approvals list (a tab grant never implies it); everyone else by the action's
role list. `/api/me.approvals` is computed by the same function, so the UI and
the server cannot disagree.

Run:  cd backend && python -m pytest tests/test_approval_permissions.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import re
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
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
from crm_deps import CurrentUser  # noqa: E402
from models import AccessTemplate, UserProfile  # noqa: E402
from models.base import Base  # noqa: E402
from services import access_templates as tpl_svc  # noqa: E402
from services import action_permissions as ap  # noqa: E402
from services import custom_roles as role_svc  # noqa: E402
from services.access_templates import effective_access  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (1, 2, 3, 4):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _code_defaults(monkeypatch):
    """`roles_for_action` reads the LIVE Postgres; tests pin the code defaults.
    The ORM's registration_data is a one-column FK stub, so the raw-SQL user
    lookup behind role membership is stubbed too (as in test_custom_roles)."""
    monkeypatch.setattr(ap, "roles_for_action", lambda action, defaults: list(defaults))
    monkeypatch.setattr(role_svc, "_user_rows", lambda db_, ids: {
        uid: {"id": uid, "full_name": f"User {uid}", "email": f"u{uid}@karnex.in",
              "username": f"u{uid}", "is_active": True} for uid in ids})


def _user(uid: int, *roles: str) -> CurrentUser:
    return CurrentUser(id=uid, username=f"u{uid}", full_name=f"User {uid}", roles=set(roles))


def _assign_template(db, uid: int, name: str, role: str | None, tabs: dict, actions=None) -> int:
    payload = {"name": name, "role": role, "tab_access": tabs}
    if actions is not None:
        payload["action_access"] = actions
    t = tpl_svc.create_template(db, payload)
    db.add(UserProfile(user_id=uid, access_template_id=t["id"]))
    db.commit()
    return t["id"]


def _gate_client(db, user: CurrentUser, action: str, tab: str) -> TestClient:
    app = FastAPI()

    @app.post("/act")
    def act(u: CurrentUser = Depends(crm_deps.gated_write_action(action, tab))):
        return {"ok": True}

    app.dependency_overrides[crm_deps.get_current_user] = lambda: user
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    return TestClient(app)


# ------------------------------------------------------------ the report ---


def test_sales_with_timesheets_edit_cannot_approve_the_sheet_they_filled(db):
    """The screenshot: Timesheets: Edit (to fill) must not approve."""
    _assign_template(db, 2, "Default — Sales", "Sales", {"timesheets": "create"})
    sales = _user(2, "Sales")
    for action in ("timesheet.approve", "timesheet.reject", "timesheet.generate_invoice"):
        assert _gate_client(db, sales, action, "timesheets").post("/act").status_code == 403
    assert "timesheet.approve" not in ap.allowed_approvals(db, sales)


def test_the_gm_custom_role_approves_timesheets(db):
    role = role_svc.create_role(db, {"name": "GM", "tab_access": {"timesheets": "edit"}}, actor_id=1)
    role_svc.set_members(db, role["id"], [3])
    db.commit()
    gm = _user(3, "GM")
    assert _gate_client(db, gm, "timesheet.approve", "timesheets").post("/act").status_code == 200
    assert set(ap.allowed_approvals(db, gm)) >= {
        "timesheet.approve", "timesheet.reject", "timesheet.generate_invoice"}


def test_admin_and_ceo_always_approve(db):
    for role in ("Admin", "CEO"):
        assert _gate_client(db, _user(1, role), "timesheet.approve", "timesheets").post("/act").status_code == 200


# -------------------------------------------------- the template decides ---


def test_a_template_can_grant_an_approval_explicitly(db):
    """Admin/CEO may give ANY role an approval — the template is the authority."""
    _assign_template(db, 2, "Sales approver", "Sales", {"timesheets": "edit"},
                     actions=["timesheet.approve"])
    sales = _user(2, "Sales")
    assert _gate_client(db, sales, "timesheet.approve", "timesheets").post("/act").status_code == 200
    # …and only what was ticked.
    assert _gate_client(db, sales, "timesheet.reject", "timesheets").post("/act").status_code == 403


def test_an_approval_still_needs_the_tab(db):
    """Approvals ride ON the tab: without the tab the record cannot be reached."""
    _assign_template(db, 2, "No timesheets", "Sales", {"customers": "view"},
                     actions=["timesheet.approve"])
    assert _gate_client(db, _user(2, "Sales"), "timesheet.approve", "timesheets").post("/act").status_code == 403


def test_view_is_enough_tab_access_for_an_approver(db):
    _assign_template(db, 2, "Viewer approver", None, {"timesheets": "view"},
                     actions=["timesheet.approve"])
    assert _gate_client(db, _user(2, "Sales"), "timesheet.approve", "timesheets").post("/act").status_code == 200


def test_a_template_without_configured_approvals_falls_back_to_role_lists(db):
    """NULL = never configured (an untagged template before 0108): the action's
    role list decides — nobody loses or gains a button by the upgrade alone."""
    tid = _assign_template(db, 2, "Legacy HR", None, {"leave-applications": "edit"})
    db.get(AccessTemplate, tid).action_access = None
    db.commit()
    acc = effective_access(db, 2, {"HR"})
    assert acc["actions"] is None
    assert ap.user_may(db, _user(2, "HR"), "leave.approve", acc)
    assert not ap.user_may(db, _user(2, "Sales"), "leave.approve", effective_access(db, 2, {"Sales"}))


def test_untemplated_users_follow_the_role_list(db):
    assert ap.user_may(db, _user(4, "HR"), "leave.approve")
    assert not ap.user_may(db, _user(4, "Sales"), "timesheet.approve")


def test_manage_actions_keep_the_tab_edit_rule(db):
    """Only APPROVALS changed. Ordinary editing stays template-authoritative."""
    _assign_template(db, 2, "Invoice editor", "Sales", {"invoices": "edit"})
    assert _gate_client(db, _user(2, "Sales"), "invoice.manage", "invoices").post("/act").status_code == 200


# ------------------------------------------------------------- the editor ---


def test_a_new_template_starts_from_its_role_tags_default_approvals(db):
    t = tpl_svc.create_template(db, {"name": "Default — Finance", "role": "Finance",
                                     "tab_access": {"invoices": "create"}})
    assert set(t["action_access"]) == {"invoice.convert_proforma", "credit_note.approve"}
    sales = tpl_svc.create_template(db, {"name": "Default — Sales", "role": "Sales",
                                         "tab_access": {"timesheets": "create"}})
    assert not any(a.startswith("timesheet.") for a in sales["action_access"])


def test_unknown_action_keys_are_dropped_and_the_role_tag_can_be_cleared(db):
    t = tpl_svc.create_template(db, {"name": "T", "role": "GM", "tab_access": {"timesheets": "edit"},
                                     "action_access": ["timesheet.approve", "no.such.action"]})
    assert t["action_access"] == ["timesheet.approve"]
    out = tpl_svc.update_template(db, t["id"], {"role": None})
    assert out["role"] is None


def test_the_registry_lists_every_approval_with_a_group():
    reg = ap.registry()
    assert [r["key"] for r in reg] == list(ap.APPROVAL_ACTIONS)
    assert all(r["group"] and r["label"] for r in reg)
    for key in ("timesheet.approve", "timesheet.reject", "timesheet.generate_invoice"):
        assert ap.ACTIONS[key].roles == ("GM",)


def _snapshot(prefix: str) -> dict:
    path = next((BACKEND / "alembic" / "versions").glob(f"{prefix}_*.py"))
    spec = importlib.util.spec_from_file_location(f"m{prefix}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._APPROVAL_DEFAULTS


def test_the_migration_snapshots_match_the_registry():
    """Migrations backfill from frozen copies; layered in order (0108, then the
    entries 0111 changed or added) they must describe the registry exactly —
    a new approval without a backfill would never reach a saved Approvals list."""
    merged = {**_snapshot("0108"), **_snapshot("0111")}
    assert merged == {k: ap.ACTIONS[k].roles for k in ap.APPROVAL_ACTIONS}


# ---------------------------------------------------------- the call sites ---


APPROVAL_ROUTES = [
    ("routers/crm/timesheets.py", '"/{timesheet_id}/approve"', "timesheet.approve"),
    ("routers/crm/timesheets.py", '"/{timesheet_id}/reject"', "timesheet.reject"),
    ("routers/crm/timesheets.py", '"/{timesheet_id}/recalculate"', "timesheet.approve"),
    ("routers/crm/opportunities.py", '"/{opportunity_id}/approve"', "opportunity.approve"),
    ("routers/crm/opportunities.py", '"/{opportunity_id}/reject"', "opportunity.approve"),
    ("routers/crm/requirements.py", '"/{requirement_id}/sales-head-approve"', "requirement.sales_head_approve"),
    ("routers/crm/requirements.py", '"/{requirement_id}/sales-head-reject"', "requirement.sales_head_approve"),
    ("routers/crm/requirements.py", '"/{requirement_id}/engineering-approve"', "requirement.engineering_approve"),
    ("routers/crm/requirements.py", '"/{requirement_id}/engineering-reject"', "requirement.engineering_approve"),
    ("routers/crm/candidate_profiles.py", '"/{profile_id}/sales-head-decision"', "profile.sales_head_decision"),
    ("routers/crm/candidate_profiles.py", '"/{profile_id}/budget-resolve"', "profile.budget_resolve"),
]


@pytest.mark.parametrize("rel, route, action", APPROVAL_ROUTES)
def test_every_approval_endpoint_is_gated_by_its_approval_action(rel, route, action):
    """A plain `gated_write(tab, Role)` on an approval endpoint is the bug:
    the tab's Edit grant would satisfy it. Pin each one to its action."""
    src = (BACKEND / rel).read_text(encoding="utf-8")
    start = src.index(route)
    body = src[start:start + 900]
    assert f'gated_write_action("{action}"' in body, f"{route} is not gated by {action}"
    assert ap.is_approval(action)


def test_every_gated_action_is_registered():
    """A key missing from ACTIONS would silently be treated as a MANAGE action."""
    keys = set()
    for path in (BACKEND / "routers").rglob("*.py"):
        keys.update(re.findall(r'gated_write_action\(\s*"([^"]+)"', path.read_text(encoding="utf-8")))
    assert keys and keys <= set(ap.ACTIONS), sorted(keys - set(ap.ACTIONS))
