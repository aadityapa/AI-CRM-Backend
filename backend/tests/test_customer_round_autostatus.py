"""Customer rounds drive the pipeline (services/candidate_profiles.py, 27 Aug 2026).

Two behaviours pinned here:

  * ONE ROW PER CUSTOMER ROUND — the transition recorder reuses whatever the
    Interviews form already saved (matched by the round's KIND, or a legacy
    stage-tagged row), instead of creating a second card. Duplicate
    "Customer L1 / Customer L2" entries were the symptom.
  * SAVING FEEDBACK IS THE STATUS CHANGE — a Customer L1 verdict moves the
    profile to L1 Feedback, a Customer L2 verdict to L2 Feedback, walking any
    stage in between, and never backwards or out of a terminal state.

Run:  cd backend && python -m pytest tests/test_customer_round_autostatus.py -q
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
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, InterviewEvent, Opportunity, OppType,
    PipelineStatus as PS,
)
from services.candidate_profiles import (  # noqa: E402
    _record_customer_round_from_transition, advance_status_for_customer_round,
    hand_off_to_rmg_review,
)


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


USER = SimpleNamespace(id=1, roles={"Sales"}, is_admin=False, full_name="Sales User",
                       username="sales", email="sales@karnex.in")


_SEQ = {"n": 0}


def _profile(db, status=PS.CUSTOMER_INTERVIEW):
    # Unique names/ids per call — customers.name and opportunities.opp_id are
    # UNIQUE, and one test builds three profiles.
    _SEQ["n"] += 1
    i = _SEQ["n"]
    cust = Customer(name=f"VISTEON {i}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{i}", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Omkar", last_name="Milind", email=f"omkar{i}@example.com")
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                               pipeline_status=status)
    db.add(profile); db.flush()
    return profile


def _rounds(db, profile):
    return db.execute(
        select(InterviewEvent).where(InterviewEvent.profile_id == profile.id)
        .order_by(InterviewEvent.id)
    ).scalars().all()


def test_l1_feedback_moves_the_pipeline(db):
    profile = _profile(db, PS.CUSTOMER_INTERVIEW)
    moved = advance_status_for_customer_round(db, profile, "Customer_Interview", "Cleared L1", USER)
    assert moved == PS.L1_FEEDBACK.value
    assert profile.pipeline_status == PS.L1_FEEDBACK


def test_l2_feedback_walks_through_l1(db):
    # Customer ran both rounds and Sales only recorded the L2 verdict: the
    # profile must still pass THROUGH L1 Feedback, not skip a stage silently.
    profile = _profile(db, PS.CUSTOMER_INTERVIEW)
    moved = advance_status_for_customer_round(db, profile, "Customer_L2", "Final selected", USER)
    assert moved == PS.L2_FEEDBACK.value
    assert profile.pipeline_status == PS.L2_FEEDBACK


def test_never_moves_backwards_or_out_of_terminal(db):
    ahead = _profile(db, PS.SHORTLISTED)
    assert advance_status_for_customer_round(db, ahead, "Customer_Interview", "x", USER) is None
    assert ahead.pipeline_status == PS.SHORTLISTED

    done = _profile(db, PS.JOINED)
    assert advance_status_for_customer_round(db, done, "Customer_L2", "x", USER) is None
    assert done.pipeline_status == PS.JOINED

    # A non-customer round never touches the pipeline.
    at_interview = _profile(db, PS.CUSTOMER_INTERVIEW)
    assert advance_status_for_customer_round(db, at_interview, "L2_F2F", "x", USER) is None


def test_transition_recorder_reuses_the_form_row(db):
    """The duplicate-card bug: form row + transition row for the same round."""
    profile = _profile(db, PS.CUSTOMER_INTERVIEW)
    # Sales saved Customer L2 from the Interviews form (kind, no stage).
    db.add(InterviewEvent(profile_id=profile.id, candidate_id=profile.candidate_id,
                          created_by=1, kind="Customer_L2", interview_category="External",
                          status="Completed", result="Strong Hire", feedback="Final selected"))
    db.flush()
    # Then Sales closes the ladder from L2 Feedback with the customer's verdict.
    _record_customer_round_from_transition(
        db, profile, PS.L2_FEEDBACK.value, PS.SHORTLISTED.value, "Customer confirmed", USER)
    rows = _rounds(db, profile)
    assert len(rows) == 1, "the transition must update the existing round, not add a second"
    assert "Final selected" in (rows[0].feedback or "")
    assert "Customer confirmed" in (rows[0].feedback or "")


def test_moving_on_to_a_customer_round_writes_no_round(db):
    """29 Sep 2026 (user report): the move to "Customer L2 Interview" wrote a
    Completed Customer L2 row with no time and no verdict — it read as
    "Scheduled · Time not set" and hid TA's Schedule button. The move lines
    the round UP; TA books it and the verdict is recorded on the round."""
    from services.candidate_profiles import comment_required_for
    profile = _profile(db, PS.CUSTOMER_INTERVIEW)
    for prev, new in ((PS.CUSTOMER_INTERVIEW.value, PS.L1_FEEDBACK.value),
                      (PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value)):
        _record_customer_round_from_transition(db, profile, prev, new, "move to L2", USER)
        assert comment_required_for(prev, new) is False
    assert _rounds(db, profile) == []
    # Closing the ladder still needs the customer's verdict.
    assert comment_required_for(PS.L2_FEEDBACK.value, PS.SHORTLISTED.value) is True



def test_migration_0113_removes_only_the_placeholders(db):
    """0113 deletes the rows the move used to write (no time, link, panel or
    verdict) and keeps their text on the activity log; real rounds stay."""
    import importlib.util
    import pathlib
    from models import CandidateProfileActivityLog
    path = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions" / \
        "0113_remove_placeholder_customer_rounds.py"
    spec = importlib.util.spec_from_file_location("m0113", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    profile = _profile(db, PS.L2_FEEDBACK)
    common = dict(profile_id=profile.id, candidate_id=profile.candidate_id, created_by=1,
                  interview_category="External", user_role="Customer")
    db.add(InterviewEvent(kind="Customer_L2", status="Completed", feedback="move to L2", **common))
    db.add(InterviewEvent(kind="Customer_Interview", status="Completed", result="Hire", **common))
    db.add(InterviewEvent(kind="Customer_L2", status="Completed", raw_when="tomorrow 3pm", **common))
    db.flush()
    mod.op = SimpleNamespace(get_bind=lambda: db.connection())
    mod.upgrade()
    rows = _rounds(db, profile)
    assert len(rows) == 2 and all(r.result or r.raw_when for r in rows)
    notes = db.execute(select(CandidateProfileActivityLog.comment).where(
        CandidateProfileActivityLog.profile_id == profile.id,
        CandidateProfileActivityLog.action_type == "CUSTOMER_NOTE")).scalars().all()
    assert notes == ["Note on the move to the Customer L2 round: move to L2"]

# --------------------------------------------------------------------------
# AI L1 is OPTIONAL: RMG can take a candidate straight to review (28 Aug 2026)
# --------------------------------------------------------------------------

def test_skip_ai_l1_walks_sourcing_to_rmg_review(db):
    """A candidate applied from the Candidates page sits at SOURCING — the
    stage the old hand-off ignored, stranding them with no L2 button."""
    profile = _profile(db, PS.SOURCING)
    assert hand_off_to_rmg_review(db, profile, USER, "AI L1 skipped by RMG") == PS.RMG_REVIEW.value
    assert profile.pipeline_status == PS.RMG_REVIEW


def test_skip_ai_l1_from_technical_screening(db):
    profile = _profile(db, PS.TECHNICAL_SCREENING)
    assert hand_off_to_rmg_review(db, profile, USER, "skip") == PS.RMG_REVIEW.value
    assert profile.pipeline_status == PS.RMG_REVIEW


def test_skip_ai_l1_is_idempotent_and_refuses_later_stages(db):
    from fastapi import HTTPException

    already = _profile(db, PS.RMG_REVIEW)
    assert hand_off_to_rmg_review(db, already, USER, "skip") is None  # re-click, no-op

    ahead = _profile(db, PS.CUSTOMER_INTERVIEW)
    with pytest.raises(HTTPException) as err:
        hand_off_to_rmg_review(db, ahead, USER, "skip")
    assert err.value.status_code == 400
    assert ahead.pipeline_status == PS.CUSTOMER_INTERVIEW
