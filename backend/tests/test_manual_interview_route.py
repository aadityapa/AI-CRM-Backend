"""The MANUAL interview route (1 Sep 2026, user flow).

The AI L1 is optional. When RMG decides a candidate is better judged by a
person, the same ladder runs with a human on the call:

    RMG "go manual"  →  manual L1  →  L1 feedback  →  L2  →  submit to Sales
                                                            →  customer L1/L2

What is pinned here:

  * `manual_round_spec` maps "L1"/"L2" onto ONE spec — kind, activity type,
    request type, email template. A round scheduled under one name and logged
    under another is the bug this table exists to prevent, and the default
    stays "L2" so every pre-1-Sep caller is untouched.
  * `manual_round_state` reports both rounds per profile WITHOUT needing an AI
    interview link. Keying it off the link is what left a manually interviewed
    candidate's row with no buttons at all — they never have one.

Run:  cd backend && python -m pytest tests/test_manual_interview_route.py -q
"""
from __future__ import annotations

import importlib

import pytest
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
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, InterviewEvent,
    Opportunity, OppType, PipelineStatus as PS,
)
from routers.crm.candidate_profiles import MANUAL_ROUNDS, manual_round_spec  # noqa: E402
from services.resumes import manual_round_state  # noqa: E402


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


_SEQ = {"n": 0}


def _profile(db, status=PS.RMG_REVIEW):
    _SEQ["n"] += 1
    i = _SEQ["n"]
    cust = Customer(name=f"VISTEON {i}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-M{i}", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Muthu", last_name="Manikandan",
                     email=f"muthu{i}@example.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status)
    db.add(p); db.flush()
    return p


# --------------------------------------------------------------- the spec table

def test_round_spec_defaults_to_l2_so_old_callers_are_untouched():
    assert manual_round_spec(None)["kind"] == "L2_F2F"
    assert manual_round_spec("")["kind"] == "L2_F2F"
    assert manual_round_spec("l2")["kind"] == "L2_F2F"


def test_round_spec_resolves_the_manual_l1():
    spec = manual_round_spec("L1")
    assert spec["kind"] == "L1_Interview"          # services/interview_rounds.ROUNDS
    assert spec["activity"] == "L1_FACE_TO_FACE"
    assert spec["request_activity"] == "L1_REQUESTED"
    assert spec["label"] == "L1"


def test_round_spec_rejects_anything_else():
    from fastapi import HTTPException
    for bad in ("L3", "customer", "L1 ", "1"):
        if bad.strip().upper() in MANUAL_ROUNDS:
            continue
        with pytest.raises(HTTPException) as err:
            manual_round_spec(bad)
        assert err.value.status_code == 400


def test_every_spec_names_a_distinct_kind_and_activity():
    """One round, one kind, one activity type — no accidental sharing."""
    kinds = [s["kind"] for s in MANUAL_ROUNDS.values()]
    acts = [s["activity"] for s in MANUAL_ROUNDS.values()]
    reqs = [s["request_activity"] for s in MANUAL_ROUNDS.values()]
    assert len(set(kinds)) == len(kinds)
    assert len(set(acts)) == len(acts)
    assert len(set(reqs)) == len(reqs)


# ------------------------------------------------------------- the row's state

def test_state_is_empty_for_a_candidate_with_no_rounds(db):
    p = _profile(db)
    st = manual_round_state(db, [p.id])[p.id]
    assert st["l1_manual_requested"] is False
    assert st["l1_manual_scheduled"] is False
    assert st["l2_requested"] is False
    assert st["l1_manual_event_id"] is None


def test_go_manual_shows_as_l1_requested_without_any_ai_link(db):
    """The whole point: this candidate never has an AiInterviewLink."""
    p = _profile(db)
    db.add(CandidateProfileActivityLog(
        profile_id=p.id, user_id=1, action_type="L1_REQUESTED",
        comment="RMG requested a MANUAL L1 round instead of the AI interview"))
    db.flush()

    st = manual_round_state(db, [p.id])[p.id]
    assert st["l1_manual_requested"] is True
    assert st["l1_manual_scheduled"] is False
    # Asking for the L1 must not imply anything about the L2.
    assert st["l2_requested"] is False


def test_scheduled_l1_carries_its_event_id_and_result(db):
    p = _profile(db)
    ev = InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=1,
                        kind="L1_Interview", interview_category="Internal",
                        status="Scheduled")
    db.add(ev); db.flush()

    st = manual_round_state(db, [p.id])[p.id]
    assert st["l1_manual_scheduled"] is True
    assert st["l1_manual_event_id"] == ev.id
    assert st["l1_manual_result"] is None, "no verdict yet — the L2 must stay locked"

    ev.result = "Hire"
    ev.status = "Completed"
    db.flush()
    assert manual_round_state(db, [p.id])[p.id]["l1_manual_result"] == "Hire"


def test_the_two_rounds_never_bleed_into_each_other(db):
    p = _profile(db)
    db.add(InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=1,
                          kind="L1_Interview", result="Hire", status="Completed"))
    db.add(InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=1,
                          kind="L2_F2F", status="Scheduled"))
    db.flush()

    st = manual_round_state(db, [p.id])[p.id]
    assert st["l1_manual_result"] == "Hire"
    assert st["l2_scheduled"] is True
    assert st["l2_result"] is None


def test_interviewer_names_is_reachable_and_ta_readable():
    """TA books the manual L1 but has no Employees access, so the picker needs
    its own narrow route — and a literal one, which must be declared BEFORE
    GET /{employee_id} or FastAPI coerces "interviewer-names" to an int."""
    from routers.crm import employees as emp

    paths = [r.path for r in emp.router.routes]
    assert "/api/employees/interviewer-names" in paths
    assert (paths.index("/api/employees/interviewer-names")
            < paths.index("/api/employees/{employee_id}")), \
        "literal route must precede the parametric sibling"

    # The gate is a role check, not the Employees tab gate — TA has no
    # Employees access and the full list 403s for them.
    dep = next(d for d in emp.interviewer_names.__defaults__ if hasattr(d, "dependency"))
    assert dep is not None


def test_state_is_batched_across_profiles_and_ignores_strangers(db):
    a, b, c = _profile(db), _profile(db), _profile(db)
    db.add(InterviewEvent(profile_id=b.id, candidate_id=b.candidate_id, created_by=1,
                          kind="L1_Interview", status="Scheduled"))
    db.flush()

    st = manual_round_state(db, [a.id, b.id])
    assert set(st) == {a.id, b.id}, "only the profiles asked for come back"
    assert st[b.id]["l1_manual_scheduled"] is True
    assert st[a.id]["l1_manual_scheduled"] is False
    assert c.id not in st
    assert manual_round_state(db, []) == {}


# ------------------------------------------------ no AI slot link on the manual route

def test_slot_invite_is_blocked_once_rmg_goes_manual(db):
    """3 Sep 2026 (user report): Dummy RAO went manual and still got "pick
    your interview slot" with the AI booking link. The invite fires from the
    resume side; this is the profile-side truth it has to consult."""
    from services.candidate_profiles import manual_route_blocks_slot_invite
    p = _profile(db, status=PS.SOURCING)
    assert manual_route_blocks_slot_invite(db, p) is None          # AI route: invite allowed

    db.add(CandidateProfileActivityLog(profile_id=p.id, user_id=1,
                                       action_type="L1_REQUESTED", comment="go manual"))
    db.flush()
    why = manual_route_blocks_slot_invite(db, p)
    assert why and "MANUAL" in why


def test_slot_invite_is_blocked_by_a_scheduled_manual_l1_or_a_later_stage(db):
    from services.candidate_profiles import manual_route_blocks_slot_invite
    p = _profile(db, status=PS.SOURCING)
    db.add(InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=1,
                          kind="L1_Interview", status="Scheduled"))
    db.flush()
    assert manual_route_blocks_slot_invite(db, p)

    q = _profile(db, status=PS.SALES_SCREENING)
    assert "past the AI L1" in (manual_route_blocks_slot_invite(db, q) or "")
    assert manual_route_blocks_slot_invite(db, None) is None
