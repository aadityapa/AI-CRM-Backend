"""Access templates are AUTHORITATIVE for assigned users (Aug 2026 redesign).

The old model ran role gates first and let templates only narrow them — which
meant "give Sales edit on Projects" in a template did nothing. The new model,
chosen deliberately by the admin team:

  1. Admin/CEO — full, always.
  2. User HAS a template → the template alone decides view/edit/create per
     tab. Roles are not consulted.
  3. User has NO template → the endpoint's role list decides, exactly as
     before templates existed.

Modes are a ladder: view < edit < create — a create grant satisfies both
lower checks.

Run:  cd backend && python -m pytest tests/test_access_template_authority.py -q
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
from models import AccessTemplate, UserProfile  # noqa: E402
import crm_deps  # noqa: E402
from services.access_registry import FIELDS_BY_TAB, TABS, mode_satisfies  # noqa: E402


# ---------------------------------------------------------------- ladder

def test_mode_ladder():
    assert mode_satisfies("create", "view")
    assert mode_satisfies("create", "edit")
    assert mode_satisfies("create", "create")
    assert mode_satisfies("edit", "view")
    assert not mode_satisfies("edit", "create")
    assert not mode_satisfies("view", "edit")
    assert not mode_satisfies(None, "view")


def test_every_tab_has_fields():
    """The registry drives the editor — a tab with no fields cannot be
    field-controlled, so every grantable data tab must list its form fields."""
    # View-only pages have no form, so no field catalogue: dashboard, calendar,
    # finance-reports and payroll (the latter two added 25 Aug 2026).
    NO_FORM = ("dashboard", "calendar", "emails", "activity-log", "finance-reports", "payroll")
    no_fields = [k for k in TABS if k not in NO_FORM and not FIELDS_BY_TAB.get(k)]
    assert no_fields == [], f"tabs missing field catalogues: {no_fields}"


def test_profile_workflow_block_is_a_grantable_field():
    """3 Sep 2026 (user request): Admin/CEO decide per template which logins
    see the profile's Workflow block. The frontend gates the whole block on
    `profiles.workflow`, and update_profile maps every field in it to the same
    key — remove this entry and the block becomes ungovernable again."""
    assert "workflow" in FIELDS_BY_TAB["profiles"]


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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


def _app_with(db, user: crm_deps.CurrentUser) -> TestClient:
    """Tiny app exposing one endpoint per gate flavour on the projects tab."""
    from fastapi import Depends

    app = FastAPI()

    @app.get("/read", dependencies=[Depends(crm_deps.gated_read("projects"))])
    def _read():
        return {"ok": True}

    @app.post("/write", dependencies=[Depends(
        crm_deps.gated_write("projects", "Sales_Head", "Finance"))])
    def _write():
        return {"ok": True}

    @app.post("/create", dependencies=[Depends(
        crm_deps.gated_create("projects", "Sales_Head", "Finance"))])
    def _create():
        return {"ok": True}

    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: user
    return TestClient(app)


def _user(uid: int, *roles: str) -> crm_deps.CurrentUser:
    return crm_deps.CurrentUser(id=uid, username=f"u{uid}", roles=set(roles))


def _assign_template(db, uid: int, tab_access: dict, is_active: bool = True) -> None:
    t = AccessTemplate(name=f"T{uid}", tab_access=tab_access, field_access={},
                       is_active=is_active)
    db.add(t)
    db.flush()
    db.add(UserProfile(user_id=uid, access_template_id=t.id))
    db.commit()


# ------------------------------------------------- untemplated = role defaults

def test_untemplated_user_keeps_role_defaults(db):
    sales = _app_with(db, _user(1, "Sales"))
    assert sales.get("/read").status_code == 200          # reads: any CRM role
    assert sales.post("/write").status_code == 403        # writes: Sales_Head/Finance
    assert sales.post("/create").status_code == 403

    head = _app_with(db, _user(2, "Sales_Head"))
    assert head.post("/write").status_code == 200
    assert head.post("/create").status_code == 200


# ------------------------------------------------- template is the authority

def test_template_grants_beyond_the_role(db):
    """THE fix for the reported bug: Sales + template create on projects
    can now read, write and create — the template decides, not the role."""
    _assign_template(db, 1, {"projects": "create"})
    sales = _app_with(db, _user(1, "Sales"))
    assert sales.get("/read").status_code == 200
    assert sales.post("/write").status_code == 200
    assert sales.post("/create").status_code == 200


def test_template_ladder_view_only(db):
    _assign_template(db, 1, {"projects": "view"})
    sales = _app_with(db, _user(1, "Sales"))
    assert sales.get("/read").status_code == 200
    assert sales.post("/write").status_code == 403
    assert sales.post("/create").status_code == 403


def test_template_edit_but_not_create(db):
    _assign_template(db, 1, {"projects": "edit"})
    sales = _app_with(db, _user(1, "Sales"))
    assert sales.post("/write").status_code == 200
    assert sales.post("/create").status_code == 403


def test_template_restricts_even_privileged_roles(db):
    """The authority cuts both ways: a Sales_Head whose template omits the
    projects tab loses it, role notwithstanding."""
    _assign_template(db, 2, {"customers": "edit"})
    head = _app_with(db, _user(2, "Sales_Head"))
    assert head.get("/read").status_code == 403
    assert head.post("/write").status_code == 403


def test_admin_bypasses_templates_entirely(db):
    _assign_template(db, 3, {"customers": "view"})  # would hide projects
    admin = _app_with(db, _user(3, "Admin"))
    assert admin.get("/read").status_code == 200
    assert admin.post("/create").status_code == 200


def test_no_roles_still_refused(db):
    _assign_template(db, 1, {"projects": "create"})
    nobody = _app_with(db, crm_deps.CurrentUser(id=1, username="u1", roles=set()))
    assert nobody.get("/read").status_code == 403


def test_field_guard_rejects_view_only_fields(db):
    """The API twin of the greyed-out inputs: a field the template locks to
    view-only cannot be saved, even when the tab itself allows edit."""
    from fastapi import HTTPException

    from models import AccessTemplate, UserProfile
    from services.access_templates import reject_view_only_fields

    t = AccessTemplate(name="TA", is_active=True,
                       tab_access={"profiles": "edit"},
                       field_access={"profiles": {"approved_ctc": "view"}})
    db.add(t)
    db.flush()
    db.add(UserProfile(user_id=1, access_template_id=t.id))
    db.commit()

    field_map = {"current_ctc": "current_ctc", "ctc_approval_amount": "approved_ctc",
                 "commercial_approved": "approved_ctc"}

    # Tab-mode fields save fine.
    reject_view_only_fields(db, 1, {"TA"}, "profiles",
                            {"current_ctc": 2200000}, field_map)

    # The locked field is refused, and the error names it.
    with pytest.raises(HTTPException) as exc:
        reject_view_only_fields(db, 1, {"TA"}, "profiles",
                                {"ctc_approval_amount": 2600000}, field_map)
    assert exc.value.status_code == 403
    assert "ctc_approval_amount" in exc.value.detail

    # Unmapped payload keys are governed by the tab mode alone.
    reject_view_only_fields(db, 1, {"TA"}, "profiles",
                            {"offer_letter_reference": "X"}, field_map)


def test_field_guard_ignores_unrestricted_users(db):
    """No template → role defaults already decided upstream; guard is silent."""
    from services.access_templates import reject_view_only_fields

    reject_view_only_fields(db, 2, {"TA"}, "profiles",
                            {"ctc_approval_amount": 1}, {"ctc_approval_amount": "approved_ctc"})


def test_field_guard_field_grant_can_unlock_beyond_view_tab(db):
    """A field granted edit works even when the tab is only view — the field
    grant wins over the tab mode, both to lock AND to unlock."""
    from models import AccessTemplate, UserProfile
    from services.access_templates import reject_view_only_fields

    t = AccessTemplate(name="Viewer+", is_active=True,
                       tab_access={"candidates": "view"},
                       field_access={"candidates": {"phone": "edit"}})
    db.add(t)
    db.flush()
    db.add(UserProfile(user_id=3, access_template_id=t.id))
    db.commit()

    reject_view_only_fields(db, 3, {"TA"}, "candidates",
                            {"phone": "+911234567890"}, {"phone": "phone"})


def test_gated_write_action_honours_template(db):
    """Action-permission gates (timesheet approve etc.) also defer to a
    template grant of edit+ on the tab."""
    from fastapi import Depends

    _assign_template(db, 1, {"timesheets": "edit"})
    app = FastAPI()

    @app.post("/approve", dependencies=[Depends(
        crm_deps.gated_write_action("timesheet.approve", "timesheets", "RMG", "Sales"))])
    def _approve():
        return {"ok": True}

    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: _user(1, "HR")
    client = TestClient(app)
    # HR is not in the action's role list, but the template grants timesheets edit.
    assert client.post("/approve").status_code == 200


def test_saving_a_template_with_removed_tab_keys_succeeds(db):
    """A tab removed from the registry lingers in templates saved earlier. The
    editor round-trips the stored dict, so a verbatim re-save used to 400 —
    the admin couldn't even FIX the template from the UI. Unknown keys are now
    silently dropped on save.

    (This test originally used "requirements" as the removed key; that tab was
    RE-ADDED to the registry on 25 Aug 2026, so a genuinely unknown key stands
    in for the removed-tab scenario now.)"""
    from services.access_templates import create_template, update_template

    created = create_template(db, {
        "name": "Legacy Sales",
        "tab_access": {"customers": "create", "zoho-legacy": "edit",
                       "opportunities": "view"},
        "field_access": {"zoho-legacy": {"jd": "edit"},
                         "opportunities": {"skills": "edit"}},
    })
    assert "zoho-legacy" not in created["tab_access"]
    assert created["tab_access"]["customers"] == "create"
    assert "zoho-legacy" not in created["field_access"]
    assert created["field_access"]["opportunities"] == {"skills": "edit"}

    # Round-trip update with the stale key again (what the editor does).
    updated = update_template(db, created["id"], {
        "tab_access": {"customers": "edit", "zoho-legacy": "view"},
    })
    assert updated["tab_access"] == {"customers": "edit"}

    # A genuinely bad MODE on a KNOWN tab still fails loudly.
    import pytest as _pytest
    from fastapi import HTTPException
    with _pytest.raises(HTTPException):
        update_template(db, created["id"], {"tab_access": {"customers": "banana"}})
