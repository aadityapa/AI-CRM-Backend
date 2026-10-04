"""TA's buttons on an Applied Candidates row (28 Sep 2026) —
`services.candidate_profiles.ta_decision`, `send_for_screening`, `budget_fit`,
plus the two gates of the RMG / GM ladder they feed.

Pinned: an upload waits at Sourcing and only "Technical Screening" hands it to
RMG / GM (once — a second send is refused); Hold parks and Release restores;
Reject / Self Withdraw close through the normal transition and read by name;
a batch send notifies ONCE; no L1 is scheduled before the shortlist; an L2 is
requested only after the L1 verdict.

Run:  cd backend && python -m pytest tests/test_ta_decision.py -q
"""
from __future__ import annotations

import importlib

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
]:
    importlib.import_module(f"models.{_m}")

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity, OppType,
    AiInterviewLink, InterviewEvent, PipelineStatus as PS,
)
from services import candidate_status as cs  # noqa: E402
import services.candidate_profiles as cp  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    budget_fit, l1_verdict_recorded, rmg_screening_blocks_l1, send_for_screening, ta_decision,
)

TA = CurrentUser(id=1, username="ta", full_name="Gargee Joshi", roles={"TA"})


@pytest.fixture()
def db(monkeypatch):
    # Notifications are recorded, not sent — this pins who is told, not SMTP.
    sent: list = []
    monkeypatch.setattr(cp, "_notify_stage_owner", lambda *a, **k: None)
    monkeypatch.setattr(cp, "notify_rmg_new_applicant", lambda _db, p, actor=None: sent.append(("one", p.id)))
    monkeypatch.setattr(cp, "_notify_screening_batch",
                        lambda _db, ps, user, actor: sent.append(("batch", [p.id for p in ps])))
    monkeypatch.setattr(cp, "rmg_gate_enabled", lambda: True)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    s.info["sent"] = sent
    try:
        yield s
    finally:
        s.close()


def _profile(db, stage=PS.SOURCING, screening=None):
    cust = Customer(name=f"VISTEON {len(db.execute(select(Customer.id)).all())}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{cust.id}", title="Tester", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name="Ravi", email=f"r{cust.id}@mail.com", phone="9999999999")
    db.add_all([opp, cand]); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         rmg_screening_status=screening, expected_ctc=1_800_000)
    db.add(p); db.flush()
    return p


def _status(db, p):
    return cs.statuses_for(db, [p])[p.id]


def _actions(db, p) -> set[str]:
    return {a for (a,) in db.execute(select(CandidateProfileActivityLog.action_type)
                                     .where(CandidateProfileActivityLog.profile_id == p.id))}


def test_over_budget_needs_both_figures():
    assert budget_fit(1_800_000, 1_600_000)["over_budget"] is True
    assert budget_fit(1_600_000, 1_600_000)["over_budget"] is False
    assert budget_fit(None, 1_600_000)["over_budget"] is False
    assert budget_fit(1_800_000, None)["over_budget"] is False


def test_an_upload_waits_at_sourcing_until_ta_sends_it(db):
    p = _profile(db)
    assert _status(db, p)["label"] == "Sourcing"
    assert rmg_screening_blocks_l1(p)                    # no L1 before screening
    ta_decision(db, p, "screen", None, TA)
    assert p.rmg_screening_status == "Pending"
    assert _status(db, p)["label"] == "Technical Screening"
    assert db.info["sent"] == [("one", p.id)]
    assert cp.SENT_FOR_SCREENING in _actions(db, p)
    # 29 Sep 2026: the hand-over stamps the Hand-offs date (the stage stays Sourcing).
    from datetime import date as _date
    assert p.technical_submission_date == _date.today()
    with pytest.raises(HTTPException) as err:
        ta_decision(db, p, "screen", None, TA)           # already with RMG / GM
    assert err.value.status_code == 409
    p.rmg_screening_status = "Shortlisted"
    assert rmg_screening_blocks_l1(p) is None


def test_a_screening_reject_cannot_be_resent(db):
    p = _profile(db, screening="Rejected")
    with pytest.raises(HTTPException):
        ta_decision(db, p, "screen", None, TA)


def test_hold_parks_and_release_restores(db):
    p = _profile(db)
    ta_decision(db, p, "hold", "Not picking up the phone", TA)
    assert p.budget_status == cs.TA_HOLD
    s = _status(db, p)
    assert s["label"] == "On Hold" and s["stage"]["label"] == "Sourcing"
    ta_decision(db, p, "release", None, TA)
    assert p.budget_status is None and _status(db, p)["label"] == "Sourcing"
    with pytest.raises(HTTPException):
        ta_decision(db, p, "release", None, TA)          # not on hold any more
    ta_decision(db, p, "hold", None, TA)
    ta_decision(db, p, "screen", None, TA)               # sending lifts the hold
    assert p.budget_status is None and p.rmg_screening_status == "Pending"


def test_reject_reads_rejected_by_ta(db):
    p = _profile(db)
    with pytest.raises(HTTPException):
        ta_decision(db, p, "reject", "no", TA)           # a reason is required
    ta_decision(db, p, "reject", "Expected CTC far above budget", TA)
    assert p.pipeline_status == PS.REJECTED
    assert _status(db, p)["label"] == "Rejected by TA"
    assert {"STATUS_CHANGE", "TA_REJECTED"} <= _actions(db, p)


def test_self_withdraw_is_recorded_at_any_live_stage(db):
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    with pytest.raises(HTTPException):
        ta_decision(db, p, "withdraw", "no", TA)
    ta_decision(db, p, "withdraw", "Took another offer", TA)
    assert p.pipeline_status == PS.SELF_WITHDRAWN


def test_a_withdrawn_candidate_can_reapply_and_starts_afresh(db):
    """1 Oct 2026, user ask: after Self Withdrawn, a button to apply to this
    opportunity again. Only a withdrawal reopens; the candidate lands back at
    Sourcing with TA, the screening cleared, an archive flag lifted."""
    from services.candidate_profiles import set_applied_archive
    from services.candidate_status import archived_profile_ids
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    ta_decision(db, p, "withdraw", "Took another offer", TA)
    rmg = CurrentUser(id=1, username="rmg", full_name="Ravi RMG", roles={"RMG"})
    set_applied_archive(db, p, True, rmg)
    db.flush()
    assert p.id in archived_profile_ids(db, [p.id])
    msg = ta_decision(db, p, "reapply", "Candidate is interested again", TA)
    assert "Re-applied" in msg
    assert p.pipeline_status == PS.SOURCING
    assert p.withdrawn_from_status is None and p.rmg_screening_status is None
    assert p.id not in archived_profile_ids(db, [p.id])
    assert _status(db, p)["stage"]["key"] == "sourcing"
    assert "REAPPLIED" in _actions(db, p)
    # A rejection is somebody else's decision — re-applying over it needs a
    # reason (1 Oct 2026, later: "if we want to apply again after a rejection"),
    # and the rejection's own note is what the Rejected filter prints.
    from services.candidate_status import closing_notes
    q = _profile(db)
    ta_decision(db, q, "reject", "Expected CTC far above budget", TA)
    note = closing_notes(db, [q.id])[q.id]
    assert note["status"] == "Rejected" and note["by_id"] == TA.id
    assert note["reason"] == "Rejected by TA: Expected CTC far above budget"
    with pytest.raises(HTTPException) as err:
        ta_decision(db, q, "reapply", "", TA)
    assert err.value.status_code == 400
    assert "Re-applied" in ta_decision(db, q, "reapply", "Budget revised by the customer", TA)
    assert q.pipeline_status == PS.SOURCING
    assert closing_notes(db, [q.id]) == {}   # reopened — no longer closed
    # A live candidacy never reopens.
    with pytest.raises(HTTPException) as err:
        ta_decision(db, q, "reapply", "again", TA)
    assert err.value.status_code == 409


def test_nothing_else_is_decided_after_the_interview_started(db):
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    for decision in ("hold", "screen", "reject"):
        with pytest.raises(HTTPException) as err:
            ta_decision(db, p, decision, "Some good reason", TA)
        assert err.value.status_code == 409


def test_a_batch_send_notifies_once_and_names_the_refusals(db):
    a, b, c = _profile(db), _profile(db), _profile(db, screening="Pending")
    sent, refused = send_for_screening(db, [a, b, c], TA)
    assert [p.id for p in sent] == [a.id, b.id]
    assert [r["profile_id"] for r in refused] == [c.id]
    assert db.info["sent"] == [("batch", [a.id, b.id])]


def test_the_l2_waits_for_the_l1_verdict(db):
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    assert l1_verdict_recorded(db, p.id) is False
    ev = InterviewEvent(profile_id=p.id, kind="L1_Interview", status="Scheduled")
    db.add(ev); db.flush()
    assert l1_verdict_recorded(db, p.id) is False
    ev.result = "Hire"; db.flush()
    assert l1_verdict_recorded(db, p.id) is True


def test_a_completed_ai_l1_counts_as_the_l1_verdict(db):
    from datetime import datetime, timezone
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    db.add(AiInterviewLink(profile_id=p.id, candidate_id=p.candidate_id, invite_token="t1", opportunity_id=p.opportunity_id,
                           result="Passed", completed_at=datetime.now(timezone.utc)))
    db.flush()
    assert l1_verdict_recorded(db, p.id) is True


RMG = CurrentUser(id=1, username="rmg", full_name="Rita RMG", roles={"RMG"})


def test_rmg_chooses_the_ai_route_only_after_a_shortlist(db, monkeypatch):
    import routers.crm.candidate_profiles as r
    told: list = []
    monkeypatch.setattr(r, "_notify_ta", lambda _db, p, title, msg, event, user: told.append(event))
    p = _profile(db, screening="Pending")
    with pytest.raises(HTTPException) as err:
        r.request_ai_l1(p.id, r.RequestAiL1In(), db, RMG)
    assert err.value.status_code == 409
    p.rmg_screening_status = "Shortlisted"
    r.request_ai_l1(p.id, r.RequestAiL1In(note="Strong CV"), db, RMG)
    assert told == ["profile.ai_l1_requested"]
    assert _status(db, p)["label"] == "AI L1 – Yet to Schedule"


def test_the_l2_request_is_refused_until_the_l1_verdict(db, monkeypatch):
    import routers.crm.candidate_profiles as r
    monkeypatch.setattr(r, "_notify_ta", lambda *a, **k: None)
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    with pytest.raises(HTTPException) as err:
        r.request_l2_face_to_face(p.id, r.L2RequestIn(round="L2"), db, RMG)
    assert "L1 feedback" in err.value.detail
    db.add(InterviewEvent(profile_id=p.id, kind="L1_Interview", result="Hire")); db.flush()
    r.request_l2_face_to_face(p.id, r.L2RequestIn(round="L2"), db, RMG)
    assert "L2_REQUESTED" in _actions(db, p)


def test_ta_calls_follow_the_derived_stage_not_the_stored_one(db):
    """A legacy candidate still stored at Sourcing but with a manual L1 booked
    reads "Technical Interview" — Technical Screening / Hold / Reject are
    refused, Self Withdraw is still TA's to record."""
    p = _profile(db)
    db.add(InterviewEvent(profile_id=p.id, kind="L1_Interview", status="Scheduled")); db.flush()
    for decision in ("screen", "hold", "reject"):
        with pytest.raises(HTTPException) as err:
            ta_decision(db, p, decision, "Some good reason", TA)
        assert err.value.status_code == 409 and "Technical Interview" in err.value.detail
    ta_decision(db, p, "withdraw", "Not interested any more", TA)
    assert p.pipeline_status == PS.SELF_WITHDRAWN


def test_a_round_time_typed_as_text_is_kept_and_rounds_are_tallied(db):
    from datetime import datetime, timezone
    from services.resumes import manual_round_state
    p = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    db.add_all([
        InterviewEvent(profile_id=p.id, kind="L1_Interview", status="Completed", result="Hire",
                       raw_when="Tomorrow 11 AM"),
        InterviewEvent(profile_id=p.id, kind="L2_F2F", status="Scheduled",
                       scheduled_at=datetime(2026, 9, 30, 5, 30, tzinfo=timezone.utc)),
        InterviewEvent(profile_id=p.id, kind="L3_Interview", status="Cancelled"),
        AiInterviewLink(profile_id=p.id, candidate_id=p.candidate_id, invite_token="t9",
                        opportunity_id=p.opportunity_id, result="Passed",
                        completed_at=datetime.now(timezone.utc)),
    ])
    db.flush()
    state = manual_round_state(db, [p.id])[p.id]
    assert state["l1_manual_when"] == "Tomorrow 11 AM"      # never parsed — shown as typed
    assert state["l2_when"].startswith("2026-09-30")
    assert (state["rounds_booked"], state["rounds_done"]) == (3, 2)   # cancelled L3 not counted


def test_the_stage_chip_counts_match_the_stage_column(db):
    from models import CandidateProfile
    from services.candidate_status import phase_counts
    _profile(db)                                   # Sourcing
    _profile(db, screening="Pending")              # Technical Screening
    _profile(db, screening="Shortlisted")          # Technical Interview
    _profile(db, PS.REJECTED)                      # Closed
    counts = phase_counts(db, select(CandidateProfile))
    assert counts["all"] == 4
    assert (counts["sourcing"], counts["technical_screening"], counts["technical_interview"],
            counts["closed"]) == (1, 1, 1, 1)


def test_the_selection_stage_reads_customer_shortlisted():
    assert cs.STAGE_LABEL["selection"] == "Customer Shortlisted"


def test_rmg_decisions_reach_the_ta_who_sent_the_candidate_too(db, monkeypatch):
    """28 Sep 2026 report: Gargee sent Mohammed's candidate for screening, RMG
    chose the manual L1, and only Mohammed heard. Both TAs are told now — the
    owner first, the sender once, never the actor."""
    from routers.crm import candidate_profiles as router
    told: list[int] = []
    monkeypatch.setattr("services.notify.notify_user", lambda _db, uid, *a, **k: told.append(uid))
    monkeypatch.setattr(router, "applied_candidates_link", lambda *_a, **_k: "")
    p = _profile(db)
    p.ta_owner_id = 7                                     # Mohammed applied the candidate
    ta_decision(db, p, "screen", None, TA)                # Gargee (id 1) sent it for screening
    assert router._ta_recipients(db, p) == [7, 1]
    rmg = CurrentUser(id=9, username="rmg", full_name="RMG", roles={"RMG"})
    router._notify_ta(db, p, "Schedule manual L1", "…", "candidate.l1_requested", rmg)
    assert told == [7, 1]
    told.clear()
    router._notify_ta(db, p, "Hi", "…", "candidate.l1_requested", TA)   # the sender acting
    assert told == [7]


def test_customer_slots_go_to_ta_and_book_nothing(db, monkeypatch):
    """29 Sep 2026, user flow: Sales moves to Customer Interviewing with the
    customer's slots; TA is told and schedules — no round is booked and the
    candidate is not mailed until TA does."""
    from routers.crm import candidate_profiles as router
    from schemas.candidate_profiles import CustomerRoundScheduleIn, CustomerSlotIn
    told: list[tuple[int, str, str]] = []
    monkeypatch.setattr("services.notify.notify_user",
                        lambda _db, uid, title, msg, *a, **k: told.append((uid, title, msg)))
    monkeypatch.setattr(router, "applied_candidates_link", lambda *_a, **_k: "")
    p = _profile(db, PS.CUSTOMER_INTERVIEW)
    p.ta_owner_id = 7
    sales = CurrentUser(id=5, username="s", full_name="Sanjana", roles={"Sales"})
    sched = CustomerRoundScheduleIn(slots=[CustomerSlotIn(scheduled_at="2026-09-30T10:00", meeting_link="https://teams/x"),
                                           CustomerSlotIn(scheduled_at="2026-09-30T15:30")], interviewer="Anup")
    kind = router._propose_customer_slots(db, p, PS.CUSTOMER_INTERVIEW.value, sched, "", sales)
    assert kind == "Customer_Interview"
    assert db.execute(select(InterviewEvent).where(InterviewEvent.profile_id == p.id)).first() is None
    assert cp.CUSTOMER_SLOTS_PROPOSED in _actions(db, p)
    assert told and told[0][0] == 7 and "Customer" in told[0][1]
    text = router.customer_slots_text(sched)
    assert text.splitlines()[0] == "1. 30 Sep 2026, 10:00 AM IST — https://teams/x"
    assert "panel Anup" in text
    # No slot typed (29 Sep 2026) → nothing logged, but TA is still told the
    # round is theirs to book ...
    before = len(told)
    q = _profile(db, PS.L1_FEEDBACK)
    q.ta_owner_id = 7
    assert router._propose_customer_slots(db, q, PS.L2_FEEDBACK.value, CustomerRoundScheduleIn(), "", sales) \
        == "Customer_L2"
    assert cp.CUSTOMER_SLOTS_PROPOSED not in _actions(db, q)
    assert len(told) == before + 1 and "No customer slots" in told[-1][2]
    # ... unless that round is already booked — then there is nothing to do.
    db.add(InterviewEvent(profile_id=q.id, candidate_id=q.candidate_id, created_by=1, kind="Customer_L2",
                          status="Scheduled", interview_category="External"))
    db.flush()
    assert router._propose_customer_slots(db, q, PS.L2_FEEDBACK.value, CustomerRoundScheduleIn(), "", sales) is None
    assert len(told) == before + 1


def test_ta_gets_the_customer_slots_back_as_picks(db, monkeypatch):
    """29 Sep 2026: TA's Schedule form lists the slots Sales passed on — the
    text the activity row carries reads back into the exact datetime-local
    value, link, panel and length (round-trip pinned so the two cannot drift),
    and the Applied Candidates rows + the form's options carry the offer."""
    from routers.crm import candidate_profiles as router
    from schemas.candidate_profiles import CustomerRoundScheduleIn, CustomerSlotIn
    from services.resumes import manual_round_state
    monkeypatch.setattr("services.notify.notify_user", lambda *a, **k: None)
    monkeypatch.setattr(router, "applied_candidates_link", lambda *_a, **_k: "")
    sched = CustomerRoundScheduleIn(slots=[CustomerSlotIn(scheduled_at="2026-09-30T10:00", meeting_link="https://teams/x"),
                                           CustomerSlotIn(scheduled_at="2026-10-01T15:30"),
                                           CustomerSlotIn(scheduled_at="next Monday")],
                                    interviewer="Anup, Priya", duration_minutes=45)
    parsed = cp.parse_customer_slots(cp.customer_slots_comment("Customer_L2", sched, "prefers mornings"))
    assert parsed["kind"] == "Customer_L2"
    assert parsed["slots"][0] == {"scheduled_at": "2026-09-30T10:00", "label": "30 Sep 2026, 10:00 AM IST",
                                  "meeting_link": "https://teams/x"}
    assert parsed["slots"][1]["scheduled_at"] == "2026-10-01T15:30" and parsed["slots"][1]["meeting_link"] is None
    assert parsed["slots"][2] == {"scheduled_at": None, "label": "next Monday", "meeting_link": None}
    assert parsed["duration_minutes"] == 45 and parsed["note"] == "prefers mornings"
    assert parsed["interviewer"] == "Anup, Priya"
    assert cp.parse_customer_slots("") is None and cp.parse_customer_slots("random note") is None

    p = _profile(db, PS.CUSTOMER_INTERVIEW)
    sales = CurrentUser(id=5, username="s", full_name="Sanjana", roles={"Sales"})
    router._propose_customer_slots(db, p, PS.CUSTOMER_INTERVIEW.value, sched, "", sales)
    db.flush()
    offer = cp.latest_customer_slots(db, [p.id])[p.id]
    assert offer["kind"] == "Customer_Interview" and len(offer["slots"]) == 3 and offer["proposed_by_id"] == 5
    assert manual_round_state(db, [p.id])[p.id]["customer_slots"]["slots"][0]["scheduled_at"] == "2026-09-30T10:00"
    ta = CurrentUser(id=1, username="ta", full_name="TA", roles={"TA"})
    opts = router.interview_round_options(p.id, db=db, user=ta)["data"]
    assert opts["customer_slots"]["slots"][1]["label"] == "01 Oct 2026, 03:30 PM IST"


def test_a_ta_reject_or_withdrawal_tells_everyone_who_screens(db, monkeypatch):
    """29 Sep 2026 (user report): TA rejected a candidate and RMG / GM got no email.
    Reject and Self Withdraw now notify the screeners (the same people who may
    Shortlist), never the TA who acted."""
    import services.notify as nt
    told = []
    monkeypatch.setattr(cp, "screening_notify_user_ids", lambda _db: {1, 7, 9})
    monkeypatch.setattr(nt, "notify_roles",
                        lambda _db, roles, title, *a, **k: told.append((tuple(roles), title, k["user_ids"],
                                                                        k["event"])))
    p = _profile(db)
    ta_decision(db, p, "reject", "Expected CTC far above budget", TA)
    assert told[-1][0] == ("RMG", "GM") and told[-1][1] == "Rejected by TA: Ravi"
    assert sorted(told[-1][2]) == [7, 9] and told[-1][3] == cp.TA_CLOSED_EVENT
    q = _profile(db, PS.RMG_REVIEW, "Shortlisted")
    ta_decision(db, q, "withdraw", "Took another offer", TA)
    assert told[-1][1] == "Candidate withdrew: Ravi"


def test_sending_for_screening_scores_the_ats_at_once(db, monkeypatch):
    """30 Sep 2026: a candidate reaches the Screening Desk with its ATS score —
    a small send is scored inside the request."""
    import services.resumes as rs

    scored: list = []
    monkeypatch.setattr(rs, "auto_score_profile", lambda _db, prof, uid: scored.append(prof.id) or True)
    a, b = _profile(db), _profile(db)
    send_for_screening(db, [a, b], TA)
    assert scored == [a.id, b.id]


def _with_requirement(db, p, *, jd=None):
    from models import Requirement, RequirementStatus, Resume, AtsStatus

    from models import Opportunity as _Opp
    req = Requirement(req_number=f"REQ-{p.id}", opportunity_id=p.opportunity_id, title="Tester",
                      customer_id=db.get(_Opp, p.opportunity_id).customer_id,
                      status=RequirementStatus.OPEN_FOR_SOURCING, created_by=1, rmg_jd_text=jd)
    db.add(req); db.flush()
    r = Resume(requirement_id=req.id, candidate_id=p.candidate_id, candidate_name="Ravi",
               email="r@mail.com", resume_file_url="/x/cv.pdf", ats_status=AtsStatus.PENDING_SCAN)
    db.add(r); db.flush()
    return req, r


def test_an_uploaded_resume_is_scored_even_without_a_cv_on_the_candidate(db, monkeypatch):
    import services.resumes as rs
    from models import AtsStatus

    p = _profile(db)
    req, r = _with_requirement(db, p, jd="Python testing")
    calls: list = []

    def fake_scan(_db, resume, requirement, uid):
        calls.append(resume.id)
        resume.ats_status = AtsStatus.SCORED
        resume.ats_score = 72
    monkeypatch.setattr(rs, "run_ats_scan", fake_scan)
    assert rs.auto_score_profile(db, p, 1) is True
    assert calls == [r.id] and r.ats_score == 72
    assert rs.auto_score_profile(db, p, 1) is False     # scored once, not again


def test_a_jd_change_rescores_live_applicants_but_never_a_decision(db, monkeypatch):
    """Adding the JD / skills later re-scores Pending / Scored rows; a Shortlisted
    or Rejected resume is a decision and a closed candidacy is left alone."""
    import services.resumes as rs
    from models import AtsStatus

    live = _profile(db)
    req, r_live = _with_requirement(db, live)
    assert rs.has_ats_criteria(db, req) is False
    req.rmg_jd_text = "Python, pytest, CAN"
    assert rs.has_ats_criteria(db, req) is True
    from models import Candidate, CandidateProfile, Resume
    def add(name, status, stage=PS.SOURCING):
        c = Candidate(first_name=name, email=f"{name}@m.com", phone="9")
        db.add(c); db.flush()
        db.add(CandidateProfile(candidate_id=c.id, opportunity_id=live.opportunity_id, pipeline_status=stage))
        r = Resume(requirement_id=req.id, candidate_id=c.id, candidate_name=name, email=c.email,
                   resume_file_url="/x/a.pdf", ats_status=status)
        db.add(r); db.flush()
        return r
    done = add("Done", AtsStatus.SHORTLISTED)
    closed = add("Closed", AtsStatus.SCORED, PS.RMG_REJECTED)
    again = add("Again", AtsStatus.SCORED)
    seen: list = []
    monkeypatch.setattr(rs, "run_ats_scan", lambda _db, resume, requirement, uid: seen.append(resume.id))
    res = rs.rescore_requirement(db, req, 1)
    assert sorted(seen) == sorted([r_live.id, again.id])
    assert done.id not in seen and closed.id not in seen
    assert res["scored"] == 2
