"""Who hears when a TA applies a candidate (26 Sep 2026).

Reported: a TA applied a candidate to an opportunity and neither RMG nor the
GM received the bell or the email. The notice was addressed to the role NAME
"RMG" alone — the GM is a CUSTOM role, and an RMG whose access comes from a
template's Approvals is not necessarily in a role called RMG either.

Now every "applicants are waiting for screening" notice (single apply, bulk
ZIP summary, SLA reminder) also reaches everyone `action_permissions.user_may`
lets Shortlist (`screening_notify_user_ids`), and both events are listed in
Email Flows so Admin can see and re-route them.

Run:  cd backend && python -m pytest tests/test_screening_notifications.py -q
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

# The SQLite shims for Postgres-only column types live with the desk tests.
from tests.test_screening_desk import _applicant, _position  # noqa: F401

importlib.import_module("models.custom_roles")
importlib.import_module("models.access_templates")
importlib.import_module("models.user_profiles")

from models.base import Base  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
TA, RMG, GM, TEMPLATED, SALES = 1, 2, 3, 4, 5


@pytest.fixture()
def db(monkeypatch):
    """A TA · an RMG by built-in role · a GM by CUSTOM role · a screener by
    TEMPLATE Approvals · a Sales person who must NOT hear."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models import Role, UserProfile, UserRole
    from models.base import users_table_stub
    from models.custom_roles import CustomRole, UserCustomRole
    from models.rbac import RoleName
    from services import access_templates as tpl_svc
    from services import action_permissions as ap

    monkeypatch.setattr(ap, "roles_for_action", lambda action, defaults: list(defaults))
    for uid in (TA, RMG, GM, TEMPLATED, SALES):
        s.execute(users_table_stub.insert().values(id=uid))
    roles = {n: Role(name=n) for n in (RoleName.TA, RoleName.RMG, RoleName.SALES)}
    s.add_all(roles.values())
    s.flush()
    s.add(UserRole(user_id=TA, role_id=roles[RoleName.TA].id))
    s.add(UserRole(user_id=RMG, role_id=roles[RoleName.RMG].id))
    s.add(UserRole(user_id=SALES, role_id=roles[RoleName.SALES].id))
    s.add(UserRole(user_id=TEMPLATED, role_id=roles[RoleName.SALES].id))
    gm = CustomRole(name="GM", is_active=True, tab_access={"profiles": "edit"})
    s.add(gm)
    s.flush()
    s.add(UserCustomRole(user_id=GM, custom_role_id=gm.id))
    t = tpl_svc.create_template(s, {"name": "Screener", "role": "Sales",
                                    "tab_access": {"profiles": "edit"},
                                    "action_access": ["profile.rmg_screening"]})
    s.add(UserProfile(user_id=TEMPLATED, access_template_id=t["id"]))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _bells(db, starts: str) -> dict[int, str]:
    from models import Notification
    return {n.user_id: n.title for n in db.execute(
        select(Notification).where(Notification.title.like(f"{starts}%"))).scalars()}


def test_everyone_who_may_screen_is_told_about_a_new_applicant(db):
    from services.candidate_profiles import notify_rmg_new_applicant, screening_notify_user_ids

    assert screening_notify_user_ids(db) == {RMG, GM, TEMPLATED}, \
        "the RMG role, the GM custom role and a template's Approvals — the same rule as the button"

    req = _position(db)
    profile = _applicant(db, req, "Asha", ta=TA)
    notify_rmg_new_applicant(db, profile, actor=SimpleNamespace(id=TA, full_name="Tara TA"))
    db.flush()

    bells = _bells(db, "RMG screening needed")
    assert set(bells) == {RMG, GM, TEMPLATED}, bells
    assert TA not in bells and SALES not in bells


def test_the_screening_events_are_routable_in_email_flows():
    from routers.crm.email_flows import EVENTS
    from services.candidate_profiles import RMG_SCREENING_REQUESTED_EVENT, RMG_SCREENING_SLA_EVENT

    by_event = {e["event"]: e for e in EVENTS}
    for ev in (RMG_SCREENING_REQUESTED_EVENT, RMG_SCREENING_SLA_EVENT):
        assert ev in by_event, f"{ev} must be listed so Admin can see and re-route it"
        assert {"RMG", "GM"} <= set(by_event[ev]["default_roles"])


def test_every_screening_notice_adds_the_people_who_may_screen():
    """Source pin: the single send, the batch send, the SLA reminder, the AI
    L1 outcome and the feedback-due reminder all reach `screening_notify_user_ids`
    — a new notice that addresses the role name alone would recreate the report."""
    sources = {
        "services/candidate_profiles.py": "RMG screening needed",
        "services/candidate_profiles.py ": "candidates await Technical Screening",
        "services/scheduler.py": "waiting on RMG screening",
    }
    for rel, marker in sources.items():
        text = (BACKEND / rel.strip()).read_text(encoding="utf-8")
        start = text.index(marker)
        call = text[start:start + 900]
        assert re.search(r"user_ids=screening_notify_user_ids\(db\)", call), rel


def test_ai_outcome_and_feedback_notices_reach_every_screener():
    bridge = (BACKEND / "services/ai_interview_bridge.py").read_text(encoding="utf-8")
    for event in ('"ai_interview.passed_review"', '"ai_interview.failed_review"'):
        at = bridge.index(event)
        # `screeners` = `_screeners(db)`, read once per sync (29 Sep 2026).
        assert "user_ids=screeners" in bridge[at:at + 200], event
    assert "screeners = _screeners(db)" in bridge
    followups = (BACKEND / "services/interview_followups.py").read_text(encoding="utf-8")
    assert "extra = screeners if it[\"area\"] == SCREENING else None" in followups
    assert "user_ids=extra or None" in followups
