"""L2 face-to-face flow, end to end at the function level (28 Aug 2026).

Pins the user-reported chain: TA clicks "Schedule & notify" →
  1. an InterviewEvent(kind=L2_F2F) exists with a real timestamp,
  2. the requesting RMG gets a BELL notification row,
  3. the round appears in the Interview Calendar's manual-round source.

Run:  cd backend && python -m pytest tests/test_l2_flow.py -q
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
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
    "notifications",
]:
    try:
        importlib.import_module(f"models.{_m}")
    except ModuleNotFoundError:
        pass

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer,
    InterviewEvent, Notification, Opportunity, OppType, PipelineStatus,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))  # the TA
    s.execute(users_table_stub.insert().values(id=2))  # the RMG
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    cust = Customer(name="Visteon")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00067", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Omkar", last_name="Kamathe", email="omkar@karnex.in")
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                               pipeline_status=PipelineStatus.RMG_REVIEW,
                               ta_owner_id=1)
    db.add(profile); db.flush()
    # The RMG (user 2) asked for the L2 — as the l2-request endpoint records it.
    db.add(CandidateProfileActivityLog(profile_id=profile.id, user_id=2,
                                       action_type="L2_REQUESTED",
                                       comment="RMG asked TA to arrange an L2"))
    db.commit()
    return profile


def _ta():
    return SimpleNamespace(id=1, roles={"TA"}, is_admin=False,
                           full_name="Gargee Joshi", username="gargee")


def test_schedule_and_notify_creates_event_bell_and_calendar(db, monkeypatch):
    from routers.crm import candidate_profiles as cp

    # Email side needs a live registration_data + SMTP — out of scope here;
    # the BELL row is what the user reported missing, and that is in-DB.
    monkeypatch.setattr("services.notify.paused_user_ids", lambda _db: set())
    monkeypatch.setattr("services.notify.user_recipient", lambda _db, _uid: None)

    profile = _seed(db)
    payload = cp.L2FaceToFaceIn(scheduled_at="2026-09-02T15:30",
                                meeting_link="https://teams.microsoft.com/l/x",
                                note="Panel: RMG core team")
    res = cp.schedule_l2_face_to_face(profile.id, payload, db=db, user=_ta())
    assert res["success"] is True

    # 1) the round exists, with a REAL timestamp (not just raw_when)
    ev = db.execute(select(InterviewEvent).where(
        InterviewEvent.profile_id == profile.id,
        InterviewEvent.kind == "L2_F2F")).scalars().one()
    assert ev.scheduled_at is not None
    assert ev.meeting_link and "teams.microsoft.com" in ev.meeting_link

    # 2) the requesting RMG (user 2) got a bell notification
    bells = db.execute(select(Notification).where(Notification.user_id == 2)).scalars().all()
    assert any("L2" in (b.title or "") for b in bells), \
        f"expected an L2 bell for the RMG, got: {[b.title for b in bells]}"

    # 3) the Interview Calendar's manual source shows it in that week
    from services.interview_calendar import calendar_events, parse_range
    window = parse_range("2026-08-31", "2026-09-07")
    events = calendar_events(db, window, sources={"manual_round"})
    assert any(e.get("profile_id") == profile.id for e in events), \
        f"L2 round missing from calendar events: {events}"
