"""Interviews that are over with no verdict (28 Sep 2026) —
`services/interview_followups.py`, the Dashboard panel and the reminder job,
plus the two route gates of the new flow (AI L1 request, L2 after the L1).

Pinned: a round is due only once its time + duration has passed and while it
has no result; cancelled / no-show rounds and closed candidacies are not due;
each owner sees their own rounds (RMG / GM · HR · Sales, TA their candidates);
the job reminds each round once a day, not once a pass.

Run:  cd backend && python -m pytest tests/test_interview_followups.py -q
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from datetime import datetime, timedelta, timezone


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

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity, OppType,
    InterviewEvent, PipelineStatus as PS,
)
import services.interview_followups as fu  # noqa: E402

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
RMG = CurrentUser(id=1, username="rmg", full_name="Rita RMG", roles={"RMG"})
HR = CurrentUser(id=2, username="hr", full_name="Hema HR", roles={"HR"})
SALES = CurrentUser(id=3, username="sales", full_name="Sam Sales", roles={"Sales"})
TA = CurrentUser(id=4, username="ta", full_name="Gargee Joshi", roles={"TA"})
FINANCE = CurrentUser(id=5, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _profile(db, stage=PS.RMG_REVIEW, ta_owner_id=None):
    n = len(db.execute(select(Customer.id)).all())
    cust = Customer(name=f"C{n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{n}", title="Tester", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name=f"Ravi{n}", email=f"r{n}@mail.com", phone="9999999999")
    db.add_all([opp, cand]); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         ta_owner_id=ta_owner_id)
    db.add(p); db.flush()
    return p


def _round(db, p, kind, hours_ago, **kw):
    ev = InterviewEvent(profile_id=p.id, kind=kind, status=kw.pop("status", "Scheduled"),
                        scheduled_at=NOW - timedelta(hours=hours_ago), **kw)
    db.add(ev); db.flush()
    return ev


def _due_ids(db, **kw):
    return [i["event_id"] for i in fu.feedback_due(db, now=NOW, **kw)]


def test_a_round_is_due_once_its_time_is_over_and_until_it_has_a_verdict(db):
    p = _profile(db)
    running = _round(db, p, "L1_Interview", 0.5)                       # still in its hour
    long_call = _round(db, p, "L2_F2F", 1.5, duration_minutes=120)     # still running
    over = _round(db, p, "L1_Interview", 3)
    _round(db, p, "L1_Interview", 3, result="Hire")                    # recorded
    _round(db, p, "L1_Interview", 3, status="Cancelled")               # did not happen
    _round(db, p, "L1_Interview", -2)                                  # future
    _round(db, p, "L1_Interview", 24 * 90)                             # ancient history
    assert _due_ids(db) == [over.id]
    assert running.id and long_call.id


def test_a_closed_candidacy_owes_nobody_feedback(db):
    _round(db, _profile(db, PS.REJECTED), "L1_Interview", 5)
    assert _due_ids(db) == []


def test_each_owner_sees_their_own_rounds(db):
    p = _profile(db, ta_owner_id=TA.id)
    tech = _round(db, p, "L2_F2F", 5)
    hr = _round(db, _profile(db, PS.HR_SCREENING), "HR_Interview", 5)
    cust = _round(db, _profile(db, PS.CUSTOMER_INTERVIEW), "Customer_Interview", 5)

    def ids(user):
        return [i["event_id"] for i in fu.feedback_due_for(db, user, now=NOW)["items"]]

    assert ids(RMG) == [tech.id]
    assert ids(HR) == [hr.id]
    assert ids(SALES) == [cust.id]
    assert ids(TA) == [tech.id]                  # only their own candidates
    assert ids(FINANCE) == []
    panel = fu.feedback_due_for(db, CurrentUser(id=9, username="a", roles={"Admin"}), now=NOW)
    assert panel["total"] == 3 and panel["by_area"] == {"screening": 1, "hr": 1, "sales": 1}


def test_the_job_reminds_each_round_once_a_day(db, monkeypatch):
    import services.candidate_profiles as cp
    import services.notify as notify
    calls: list = []
    monkeypatch.setattr(notify, "notify_roles", lambda _db, roles, *a, **k: calls.append((roles, k)) or 1)
    monkeypatch.setattr(cp, "screening_notify_user_ids", lambda _db: {7, 8})
    _round(db, _profile(db), "L1_Interview", 5)
    _round(db, _profile(db, PS.HR_SCREENING), "HR_Interview", 5)
    assert fu.run_feedback_due_reminders(db, now=NOW)["notified"] == 2
    assert [roles for roles, _ in calls] == [["RMG"], ["HR"]]
    assert calls[0][1]["user_ids"] == {7, 8} and calls[1][1]["user_ids"] is None
    # A later pass the same day says nothing new; the reminder is in the history.
    assert fu.run_feedback_due_reminders(db, now=NOW + timedelta(minutes=15))["notified"] == 0
    assert len(calls) == 2


def test_a_customer_round_reminder_reaches_sales_manager_and_the_tas(db, monkeypatch):
    """29 Sep 2026, user flow: once a Customer L1 / L2 is over, Sales, Sales
    Head, the Sales Manager and the candidate's TAs are asked for the verdict."""
    import services.notify as notify
    calls: list = []
    monkeypatch.setattr(notify, "notify_roles", lambda _db, roles, *a, **k: calls.append((roles, k)) or 1)
    monkeypatch.setattr(fu, "_link", lambda *_a, **_k: "")
    p = _profile(db, PS.CUSTOMER_INTERVIEW, ta_owner_id=TA.id)
    _round(db, p, "Customer_Interview", 5)
    assert fu.run_feedback_due_reminders(db, now=NOW)["notified"] == 1
    roles, kw = calls[0]
    assert roles == ["Sales", "Sales_Head", "Sales Manager"] and kw["user_ids"] == {TA.id}
    manager = CurrentUser(id=11, username="sm", full_name="Sales Manager", roles={"Sales Manager"})
    assert [i["round_kind"] for i in fu.feedback_due_for(db, manager, now=NOW)["items"]] == ["Customer_Interview"]
