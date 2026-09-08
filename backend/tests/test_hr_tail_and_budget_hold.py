"""The HR tail of the pipeline (3 Sep 2026, user flow) + the budget hold.

    Sales Head approves → HR Screening
      → HR requests the HR round (TA is told)
      → TA books it → HR INTERVIEWING (auto)
      → HR records Hire / Not Recommend → PREBOARDING (auto, either way)
      → in budget: HR completes onboarding → Joined (no approval)
      → not in budget: HR flags OUT OF BUDGET (Sales Head + submitting Sales
        person told) → Sales replies (HR told) → HR decides.

Called through the endpoint bodies directly (no HTTP), same as the approval-
gate tests.

Run:  cd backend && python -m pytest tests/test_hr_tail_and_budget_hold.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, InterviewEvent,
    OfferHistory, OfferStatus, Opportunity, OppType, PipelineStatus as PS,
)
from routers.crm.candidate_profiles import (  # noqa: E402
    BudgetFlagIn, BudgetResolveIn, InterviewRoundIn, InterviewRoundUpdate, L2FaceToFaceIn,
    L2RequestIn, create_interview_round, flag_out_of_budget, request_l2_face_to_face,
    resolve_budget, schedule_l2_face_to_face, update_interview_round,
)
from services.candidate_profiles import (  # noqa: E402
    BUDGET_CONCERN, BUDGET_OUT, BUDGET_RESOLVED, allowed_next_statuses,
)
from services.interview_rounds import HR_RESULTS, results_for  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
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


HR = SimpleNamespace(id=1, roles={"HR"}, is_admin=False, full_name="Vishal HR", username="vishal")
TA = SimpleNamespace(id=2, roles={"TA"}, is_admin=False, full_name="Gargee TA", username="gargee")
SALES = SimpleNamespace(id=3, roles={"Sales"}, is_admin=False, full_name="Bala Sales", username="bala")
HEAD = SimpleNamespace(id=4, roles={"Sales_Head"}, is_admin=False, full_name="Head", username="head")

_SEQ = {"n": 0}


def _profile(db, status=PS.HR_SCREENING, *, with_offer=True):
    _SEQ["n"] += 1
    i = _SEQ["n"]
    cust = Customer(name=f"VISTEON {i}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-H{i}", title="Embedded Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=3)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Praveen", last_name="C", email=f"praveen-h{i}@example.com")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status,
                         ta_owner_id=TA.id, current_ctc=900_000, expected_ctc=1_200_000)
    db.add(p); db.flush()
    if with_offer:
        db.add(OfferHistory(profile_id=p.id, status=OfferStatus.PENDING, offer_date=date(2026, 9, 1),
                            ctc=1_200_000, rate_unit="Yearly", rate_value=1_200_000,
                            joining_date=date(2026, 10, 1)))
        # The submission row is how the code finds "the Sales person".
        db.add(CandidateProfileActivityLog(profile_id=p.id, user_id=SALES.id,
                                           action_type="SUBMITTED_FOR_APPROVAL",
                                           comment="Rate 12 L yearly, onboarding 1 Oct"))
        db.flush()
    return p


def _acts(db, p, action):
    return db.execute(select(CandidateProfileActivityLog).where(
        CandidateProfileActivityLog.profile_id == p.id,
        CandidateProfileActivityLog.action_type == action)).scalars().all()


# --------------------------------------------------------------- the map

def test_hr_interviewing_sits_between_screening_and_preboarding():
    assert allowed_next_statuses(PS.HR_SCREENING.value)[:2] == [PS.HR_INTERVIEWING.value, PS.PREBOARDING.value]
    assert allowed_next_statuses(PS.HR_INTERVIEWING.value)[0] == PS.PREBOARDING.value
    assert PS.HR_SCREENING.value in allowed_next_statuses(PS.HR_INTERVIEWING.value)   # re-book
    assert results_for("HR_Interview") == HR_RESULTS == ["Hire", "Not Recommend", "Drop"]
    assert results_for("L2_F2F")[-1] == "Strong Hire"


# ------------------------------------------------------- request → schedule

def test_hr_requests_the_round_and_ta_booking_moves_the_stage(db):
    p = _profile(db)
    # TA cannot ask for HR's round; HR can.
    with pytest.raises(HTTPException) as err:
        request_l2_face_to_face(p.id, L2RequestIn(round="HR", note="please book"), db=db, user=TA)
    assert err.value.status_code == 403
    request_l2_face_to_face(p.id, L2RequestIn(round="HR", note="please book"), db=db, user=HR)
    assert len(_acts(db, p, "HR_REQUESTED")) == 1

    res = schedule_l2_face_to_face(
        p.id, L2FaceToFaceIn(round="HR", scheduled_at="2026-09-08T10:30", meeting_link="https://t.me/x",
                             interviewer="Vishal Harjani"),
        db=db, user=TA)
    db.refresh(p)
    assert p.pipeline_status == PS.HR_INTERVIEWING
    assert res["data"]["pipeline_status"] == PS.HR_INTERVIEWING.value
    assert "HR Interviewing" in res["message"]

    # Re-booking the same round while already at HR Interviewing is fine.
    schedule_l2_face_to_face(p.id, L2FaceToFaceIn(round="HR", scheduled_at="2026-09-09T10:30"), db=db, user=TA)
    db.refresh(p)
    assert p.pipeline_status == PS.HR_INTERVIEWING


def test_ta_booking_via_the_interviews_tab_also_moves_the_stage(db):
    p = _profile(db)
    create_interview_round(
        p.id, InterviewRoundIn(kind="HR_Interview", status="Scheduled",
                               scheduled_at=datetime(2026, 9, 8, 10, 30), meeting_link="https://t.me/x"),
        db=db, user=TA)
    db.refresh(p)
    assert p.pipeline_status == PS.HR_INTERVIEWING


# ------------------------------------------------------------ the verdict

@pytest.mark.parametrize("verdict", ["Hire", "Not Recommend"])
def test_either_verdict_moves_to_preboarding(db, verdict):
    p = _profile(db, PS.HR_INTERVIEWING)
    ev = InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=TA.id,
                        kind="HR_Interview", status="Scheduled", interview_category="Internal")
    db.add(ev); db.flush()

    # TA may not record HR's verdict.
    with pytest.raises(HTTPException) as err:
        update_interview_round(p.id, ev.id, InterviewRoundUpdate(result=verdict), db=db, user=TA)
    assert err.value.status_code == 403

    res = update_interview_round(p.id, ev.id, InterviewRoundUpdate(result=verdict, feedback="CTC is high"),
                                 db=db, user=HR)
    db.refresh(p)
    assert p.pipeline_status == PS.PREBOARDING
    assert "Preboarding" in res["message"]
    # Not Recommend raises the budget CONCERN so Pre-Onboarding asks HR to flag it.
    assert (p.budget_status == BUDGET_CONCERN) == (verdict == "Not Recommend")


def test_drop_verdict_closes_the_profile_as_self_withdrawn(db):
    """HR "Drop" (4 Sep 2026): the candidate took a better offer elsewhere."""
    p = _profile(db, PS.HR_INTERVIEWING)
    ev = InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, created_by=TA.id,
                        kind="HR_Interview", status="Scheduled", interview_category="Internal")
    db.add(ev); db.flush()
    update_interview_round(p.id, ev.id, InterviewRoundUpdate(result="Drop", feedback="Joined elsewhere"),
                           db=db, user=HR)
    db.refresh(p)
    assert p.pipeline_status == PS.SELF_WITHDRAWN
    assert p.budget_status is None


def test_hr_round_rejects_the_five_step_scale_labels_it_never_used(db):
    p = _profile(db, PS.HR_INTERVIEWING)
    with pytest.raises(HTTPException) as err:
        create_interview_round(p.id, InterviewRoundIn(kind="HR_Interview", result="Maybe"), db=db, user=HR)
    assert err.value.status_code == 400 and "Hire, Not Recommend" in err.value.detail


def test_verdict_recorded_straight_from_hr_screening_walks_both_stages(db):
    p = _profile(db, PS.HR_SCREENING)
    create_interview_round(p.id, InterviewRoundIn(kind="HR_Interview", result="Hire"), db=db, user=HR)
    db.refresh(p)
    assert p.pipeline_status == PS.PREBOARDING
    steps = [a.comment.split(":")[0] for a in _acts(db, p, "STATUS_CHANGE")]
    assert steps == ["HR_Screening -> HR_Interviewing", "HR_Interviewing -> Preboarding"]


# --------------------------------------------------------- the budget hold

def test_budget_flag_and_reply_round_trip(db):
    from models import Notification

    p = _profile(db, PS.PREBOARDING)
    # Only HR flags; only at Pre-Onboarding (or HR Interviewing).
    with pytest.raises(HTTPException) as err:
        flag_out_of_budget(p.id, BudgetFlagIn(note="too expensive for the slot"), db=db, user=SALES)
    assert err.value.status_code == 403
    early = _profile(db, PS.SHORTLISTED)
    with pytest.raises(HTTPException) as err:
        flag_out_of_budget(early.id, BudgetFlagIn(note="too expensive for the slot"), db=db, user=HR)
    assert err.value.status_code == 400

    # Nobody may reply before HR has flagged anything.
    with pytest.raises(HTTPException) as err:
        resolve_budget(p.id, BudgetResolveIn(note="customer agreed"), db=db, user=HEAD)
    assert err.value.status_code == 400

    res = flag_out_of_budget(
        p.id, BudgetFlagIn(note="Candidate now expects 14 L; approved rate is 12 L",
                           expected_ctc=1_400_000, customer_onboarding_date=date(2026, 10, 15)),
        db=db, user=HR)
    db.refresh(p)
    assert p.budget_status == BUDGET_OUT and p.budget_flagged_by == HR.id
    assert float(p.expected_ctc) == 1_400_000 and p.customer_onboarding_date == date(2026, 10, 15)
    assert p.pipeline_status == PS.PREBOARDING, "a flag, not a stage move"
    assert "Sales Head" in res["message"]
    flagged = _acts(db, p, "BUDGET_FLAGGED")
    assert len(flagged) == 1 and "expected ctc ₹1,200,000 → ₹1,400,000" in flagged[0].comment
    # The submitting Sales person (offer.created_by) is told, not only the role.
    bells = db.execute(select(Notification).where(Notification.user_id == SALES.id)).scalars().all()
    assert any("Out of budget" in b.title for b in bells)

    # TA cannot reply; Sales Head can, with revised terms — HR is told.
    with pytest.raises(HTTPException) as err:
        resolve_budget(p.id, BudgetResolveIn(note="customer agreed"), db=db, user=TA)
    assert err.value.status_code == 403
    resolve_budget(p.id, BudgetResolveIn(note="Customer agreed to 1,300 per hour",
                                         rate_value=1300, rate_unit="Hourly"), db=db, user=HEAD)
    db.refresh(p)
    assert p.budget_status == BUDGET_RESOLVED and p.budget_resolved_by == HEAD.id
    offer = db.execute(select(OfferHistory).where(OfferHistory.profile_id == p.id)).scalars().one()
    assert offer.rate_unit == "Hourly" and float(offer.rate_value) == 1300
    assert float(offer.ctc) == 1300 * 8 * 22 * 12
    hr_bells = db.execute(select(Notification).where(Notification.user_id == HR.id)).scalars().all()
    assert any("Budget reply" in b.title for b in hr_bells)
    # HR now decides — Joined stays reachable, nothing else moved.
    assert PS.JOINED.value in allowed_next_statuses(p.pipeline_status.value)


# ------------------------------------------------------------ No Hire closes the round (7 Sep 2026)

@pytest.mark.parametrize("kind,start,expect", [
    ("Customer_Interview", PS.L1_FEEDBACK, PS.CUSTOMER_L1_REJECTED),
    ("Customer_Interview", PS.CUSTOMER_INTERVIEW, PS.CUSTOMER_L1_REJECTED),
    ("Customer_L2", PS.L2_FEEDBACK, PS.CUSTOMER_L2_REJECTED),
    ("L1_Interview", PS.RMG_REVIEW, PS.RMG_REJECTED),
])
def test_no_hire_on_a_round_closes_the_profile(db, kind, start, expect):
    """User report 7 Sep 2026: a customer L1 'No Hire' left the profile at
    L1 Feedback and TA was still offered 'Schedule Customer L2'."""
    from services.candidate_profiles import reject_on_round_verdict
    p = _profile(db, start)
    moved = reject_on_round_verdict(db, p, kind, "No Hire", "Not good", HR)
    db.refresh(p)
    assert moved == expect.value and p.pipeline_status == expect


def test_leaning_no_does_not_close_the_profile(db):
    from services.candidate_profiles import reject_on_round_verdict
    p = _profile(db, PS.L1_FEEDBACK)
    assert reject_on_round_verdict(db, p, "Customer_Interview", "Leaning No", "", HR) is None
    db.refresh(p)
    assert p.pipeline_status == PS.L1_FEEDBACK


def test_no_hire_never_reopens_a_terminal_profile(db):
    from services.candidate_profiles import reject_on_round_verdict
    p = _profile(db, PS.JOINED)
    assert reject_on_round_verdict(db, p, "Customer_L2", "No Hire", "", HR) is None
    db.refresh(p)
    assert p.pipeline_status == PS.JOINED
