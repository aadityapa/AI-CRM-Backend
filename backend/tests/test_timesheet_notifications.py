"""Who hears about a submitted / rejected timesheet (25 Sep 2026).

Reported: (1) Sales submitted a timesheet and the GM got no notification —
the GM's approval came from a TEMPLATE's Approvals, and the submit notice was
addressed by role name ("GM") only; (2) when the GM rejected it, the Sales
person who filled it was never told — only the employee was.

Now submit also notifies everyone `action_permissions.user_may` lets approve
(`user_ids_who_may`), and reject tells the submitter who rejected it and why.

Run:  cd backend && python -m pytest tests/test_timesheet_notifications.py -q
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest
from sqlalchemy import select

from tests.test_midmonth_timesheet import _seed_sheet, db  # noqa: F401 - fixture

importlib.import_module("models.custom_roles")
importlib.import_module("models.access_templates")
importlib.import_module("models.user_profiles")

GM, TEMPLATED_APPROVER, SUBMITTER, OTHER_SALES = 2, 3, 4, 5


@pytest.fixture()
def people(db, monkeypatch):  # noqa: F811
    """GM by custom role · an approver by TEMPLATE · the Sales submitter · another Sales."""
    from models import Role, UserProfile, UserRole
    from models.base import users_table_stub
    from models.custom_roles import CustomRole, UserCustomRole
    from models.rbac import RoleName
    from services import access_templates as tpl_svc
    from services import action_permissions as ap

    monkeypatch.setattr(ap, "roles_for_action", lambda action, defaults: list(defaults))
    for uid in (GM, TEMPLATED_APPROVER, SUBMITTER, OTHER_SALES):
        db.execute(users_table_stub.insert().values(id=uid))
    sales = Role(name=RoleName.SALES)
    db.add(sales)
    db.flush()
    for uid in (TEMPLATED_APPROVER, SUBMITTER, OTHER_SALES):
        db.add(UserRole(user_id=uid, role_id=sales.id))
    gm = CustomRole(name="GM", is_active=True, tab_access={"timesheets": "edit"})
    db.add(gm)
    db.flush()
    db.add(UserCustomRole(user_id=GM, custom_role_id=gm.id))
    t = tpl_svc.create_template(db, {"name": "Delivery approver", "role": "Sales",
                                     "tab_access": {"timesheets": "edit"},
                                     "action_access": ["timesheet.approve", "timesheet.reject"]})
    db.add(UserProfile(user_id=TEMPLATED_APPROVER, access_template_id=t["id"]))
    db.commit()


def _client(db, uid: int, roles: set[str]):  # noqa: F811
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.timesheets as ts_router
    app = FastAPI()
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=uid, username=f"u{uid}", full_name=f"User {uid}", roles=roles)
    return TestClient(app)


def _bells(db, starts: str) -> dict[int, str]:  # noqa: F811
    from models import Notification
    return {n.user_id: f"{n.title} | {n.message}" for n in db.execute(
        select(Notification).where(Notification.title.like(f"{starts}%"))).scalars()}


def _draft_sheet(db):  # noqa: F811
    from models import TimesheetStatus
    ts = _seed_sheet(db, onboarding=date(2026, 1, 1))
    ts.status = TimesheetStatus.DRAFT
    db.commit()
    return ts


def test_everyone_who_may_approve_hears_about_a_submitted_sheet(db, people):  # noqa: F811
    from services.action_permissions import user_ids_who_may
    assert user_ids_who_may(db, "timesheet.approve") == {GM, TEMPLATED_APPROVER}

    ts = _draft_sheet(db)
    r = _client(db, SUBMITTER, {"Sales"}).post(f"/api/timesheets/{ts.id}/submit")
    assert r.status_code == 200, r.text
    told = _bells(db, "Timesheet submitted")
    assert set(told) == {GM, TEMPLATED_APPROVER}          # not the submitter, not other Sales
    assert "User 4 submitted" in told[GM]


def test_the_submitter_hears_who_rejected_it_and_why(db, people):  # noqa: F811
    ts = _draft_sheet(db)
    assert _client(db, SUBMITTER, {"Sales"}).post(f"/api/timesheets/{ts.id}/submit").status_code == 200
    r = _client(db, GM, {"GM"}).post(f"/api/timesheets/{ts.id}/reject",
                                    json={"reason": "Leave on 12th is missing its type"})
    assert r.status_code == 200, r.text
    told = _bells(db, "Timesheet rejected")
    assert SUBMITTER in told
    assert "User 2 rejected" in told[SUBMITTER]
    assert "Leave on 12th is missing its type" in told[SUBMITTER]
    assert GM not in told                                  # the rejecter is not told about themselves
