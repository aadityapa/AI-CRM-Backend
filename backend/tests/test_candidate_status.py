"""The ONE candidate status (25 Sep 2026) — `services.candidate_status`.

Pinned: the business sheet's flow (Manual L1/L2 · customer L1/L2 · Candidate
Selected · HR Discussion · HR Round · Pre-Onboarding · Joined), the naming
convention, "Sourcing → Technical Screening → Technical Interview" at the front
of the flow (28 Sep 2026), that every derived
status declares the stage it came from (the list filter pre-narrows on it),
the batched loader, and the `status_key` list filter.
In-memory SQLite with the usual Postgres-type shims.
"""
from __future__ import annotations

import importlib
import itertools
from datetime import datetime, timezone

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
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer,
    InterviewEvent, Opportunity, OppType, PipelineStatus as PS,
)
from services import candidate_status as cs  # noqa: E402
from services.candidate_status import StatusFacts, derive_status  # noqa: E402


def _key(stage, **kw) -> str:
    return derive_status(StatusFacts(pipeline_status=stage.value, **kw)).key


def _label(stage, **kw) -> str:
    return derive_status(StatusFacts(pipeline_status=stage.value, **kw)).label


# ------------------------------------------------------------------ the sheet

def test_sourcing_then_technical_screening_then_technical_interview():
    """User rule, 28 Sep 2026: with TA → with RMG / GM → shortlisted."""
    assert _label(PS.SOURCING) == "Sourcing"                      # not sent to RMG yet
    for stage in (PS.SOURCING, PS.TECHNICAL_SCREENING):
        assert _label(stage, rmg_screening="Pending") == "Technical Screening"
        assert _key(stage, rmg_screening="Pending") == "technical_screening"
        assert _label(stage, rmg_screening="Shortlisted") == "Technical Interview"
        assert _label(stage, rmg_screening="Rejected") == "RMG Rejected"
    # …then the round takes over.
    assert _label(PS.SOURCING, rmg_screening="Shortlisted",
                  requested=frozenset({"manual_l1"})) == "Manual L1 – Yet to Schedule"
    assert _label(PS.SOURCING, rmg_screening="Shortlisted", ai_l1=cs.SCHEDULED) == "AI L1 – Scheduled"
    # RMG Review keeps its own reading: the L1 is what is wanted there.
    assert _label(PS.RMG_REVIEW, rmg_screening="Shortlisted") == "Manual L1 – Yet to Schedule"


def test_every_candidate_has_a_stage_and_a_round():
    """Stage = the phase, round = what is happening in it (28 Sep 2026)."""
    def st(stage, **kw):
        d = derive_status(StatusFacts(pipeline_status=stage.value, **kw)).as_dict()
        return d["stage"]["label"], d["round"]["label"], d["round"]["state"]

    assert st(PS.SOURCING) == ("Sourcing", "New Applicant", None)
    assert st(PS.SOURCING, rmg_screening="Pending") == ("Technical Screening", "CV Screening", "With RMG / GM")
    assert st(PS.SOURCING, rmg_screening="Shortlisted") == \
        ("Technical Interview", "Technical L1 Interview", "Yet to Schedule")
    assert st(PS.RMG_REVIEW, rounds={"manual_l1": cs.SCHEDULED}) == \
        ("Technical Interview", "Technical L1 Interview", "Scheduled")
    assert st(PS.SOURCING, rmg_screening="Shortlisted", ai_l1=cs.PASSED) == \
        ("Technical Interview", "Technical L1 Interview (AI)", "Passed")
    assert st(PS.RMG_REVIEW, rounds={"manual_l2": cs.FAILED}) == \
        ("Technical Interview", "Technical L2 Interview", "Failed")
    assert st(PS.SALES_SCREENING) == ("Sales Screening", "With Sales – Ready to Submit", None)
    assert st(PS.L1_FEEDBACK, rounds={"customer_l1": cs.SCHEDULED}) == \
        ("Customer Interviewing", "Customer L1 Interview", "Scheduled")
    assert st(PS.HR_INTERVIEWING) == ("HR Screening", "HR Round", "Scheduled")
    assert st(PS.PREBOARDING) == ("Onboarding", "Preboarding", None)
    assert st(PS.JOINED) == ("Onboarding", "Joined", None)
    assert st(PS.SELF_WITHDRAWN, withdrawn_from=PS.SALES_SCREENING.value)[0] == "Sales Screening"
    assert st(PS.REJECTED)[0] == "Closed"
    # The round key tells the client which row field carries the date.
    assert derive_status(StatusFacts(PS.HR_INTERVIEWING.value)).round_key == "hr"
    assert derive_status(StatusFacts(PS.RMG_REVIEW.value, rounds={"manual_l2": cs.SCHEDULED})).round_key \
        == "manual_l2"


def test_ta_hold_and_reject_read_by_name():
    hold = derive_status(StatusFacts(PS.SOURCING.value, budget_status=cs.TA_HOLD))
    assert hold.label == "On Hold" and hold.stage_label == "Sourcing"
    assert _label(PS.REJECTED, ta_closed="ta") == "Rejected by TA"
    assert _label(PS.REJECTED) == "Rejected"
    assert derive_status(StatusFacts(PS.REJECTED.value, ta_closed="ta")).stage_label == "Sourcing"
    # A hold means nothing once RMG Review has started.
    assert _label(PS.RMG_REVIEW, budget_status=cs.TA_HOLD) == "Manual L1 – Yet to Schedule"


def test_the_ai_route_reads_yet_to_schedule_until_the_link_exists():
    facts = dict(rmg_screening="Shortlisted", requested=frozenset({"ai_l1"}))
    assert _label(PS.SOURCING, **facts) == "AI L1 – Yet to Schedule"
    assert _label(PS.SOURCING, rmg_screening="Shortlisted") == "Technical Interview"
    assert _label(PS.SOURCING, ai_l1=cs.SCHEDULED, **facts) == "AI L1 – Scheduled"


def test_phase_parsing():
    with pytest.raises(ValueError):
        cs.parse_phases("nope")
    assert cs.parse_phases("sourcing, closed") == ["sourcing", "closed"]

def test_manual_l1_then_l2_ladder():
    assert _label(PS.RMG_REVIEW) == "Manual L1 – Yet to Schedule"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l1": cs.SCHEDULED}) == "Manual L1 – Scheduled"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l1": cs.PASSED}) == "Manual L1 – Passed"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l1": cs.FAILED}) == "Manual L1 – Failed"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l1": cs.PASSED},
                  requested=frozenset({"manual_l2"})) == "Manual L2 – Yet to Schedule"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l1": cs.PASSED, "manual_l2": cs.SCHEDULED}) \
        == "Manual L2 – Scheduled"
    assert _label(PS.RMG_REVIEW, rounds={"manual_l2": cs.PASSED}) == "Manual L2 – Passed"
    held = derive_status(StatusFacts(PS.RMG_REVIEW.value, rounds={"manual_l1": cs.HELD}))
    assert held.label == "Manual L1 – Scheduled" and "verdict" in held.hint


def test_rmg_rejection_names_the_round_that_failed():
    assert _label(PS.RMG_REJECTED, rounds={"manual_l2": cs.FAILED}) == "Manual L2 – Failed"
    assert _label(PS.RMG_REJECTED, rounds={"manual_l1": cs.FAILED}) == "Manual L1 – Failed"
    assert _label(PS.RMG_REJECTED, ai_l1=cs.FAILED) == "AI L1 – Failed"
    assert _label(PS.RMG_REJECTED) == "RMG Rejected"


def test_an_ai_interview_is_never_read_as_manual_l1_yet_to_schedule():
    assert _label(PS.TECHNICAL_SCREENING, ai_l1=cs.SCHEDULED) == "AI L1 – Scheduled"
    assert _label(PS.RMG_REVIEW, ai_l1=cs.PASSED) == "AI L1 – Passed"
    assert _label(PS.RMG_REVIEW, ai_l1=cs.REVIEW) == "AI L1 – Under Review"
    # A manual round outranks the AI one (RMG chose the manual route after).
    assert _label(PS.RMG_REVIEW, ai_l1=cs.FAILED, rounds={"manual_l1": cs.SCHEDULED}) \
        == "Manual L1 – Scheduled"


def test_customer_direct_path_and_l1_l2_path():
    assert _label(PS.SALES_SCREENING) == "With Sales – Ready to Submit"
    assert _label(PS.CUSTOMER_SCREENING) == "Submitted to Customer"
    assert _label(PS.CUSTOMER_INTERVIEW) == "Customer Interviewing"
    assert _label(PS.CUSTOMER_INTERVIEW, rounds={"customer_l1": cs.PENDING}) \
        == "Customer L1 – Profile Shortlisted"
    assert _label(PS.CUSTOMER_INTERVIEW, rounds={"customer_l1": cs.SCHEDULED}) \
        == "Customer L1 – Scheduled"
    assert _label(PS.L1_FEEDBACK, rounds={"customer_l1": cs.PASSED}) == "Customer L1 – Passed"
    assert _label(PS.L1_FEEDBACK, rounds={"customer_l1": cs.PASSED, "customer_l2": cs.SCHEDULED}) \
        == "Customer L2 – Scheduled"
    assert _label(PS.L2_FEEDBACK, rounds={"customer_l2": cs.PASSED}) == "Customer L2 – Passed"
    assert _label(PS.CUSTOMER_L1_REJECTED) == "Customer L1 – Failed"
    assert _label(PS.CUSTOMER_L2_REJECTED) == "Customer L2 – Failed"
    assert _label(PS.CUSTOMER_REJECTED, rounds={"customer_l2": cs.FAILED}) == "Customer L2 – Failed"
    assert _label(PS.CUSTOMER_REJECTED) == "Customer Rejected"
    assert _label(PS.CUSTOMER_SCREEN_REJECTED) == "Customer Rejected"


def test_selection_and_joining_use_the_new_words():
    assert _label(PS.SHORTLISTED) == "Customer Shortlisted"
    assert _label(PS.CUSTOMER_APPROVAL) == "Pending Sales Head Approval"
    assert _label(PS.HR_SCREENING) == "HR Discussion"
    assert _label(PS.HR_INTERVIEWING) == "HR Round"
    assert _label(PS.PREBOARDING) == "Pre-Onboarding"
    assert _label(PS.JOINED) == "Joined"


def test_self_withdrawn_names_where_they_left():
    assert _label(PS.SELF_WITHDRAWN, withdrawn_from=PS.HR_SCREENING.value) \
        == "Self Withdrawn (HR Discussion)"
    assert _label(PS.SELF_WITHDRAWN) == "Self Withdrawn"


def test_tones_follow_the_result():
    assert derive_status(StatusFacts(PS.RMG_REVIEW.value, rounds={"manual_l1": cs.PASSED})).tone == cs.OK
    assert derive_status(StatusFacts(PS.RMG_REVIEW.value, rounds={"manual_l1": cs.FAILED})).tone == cs.BAD
    assert derive_status(StatusFacts(PS.RMG_REVIEW.value, rounds={"manual_l1": cs.SCHEDULED})).tone == cs.WARN
    assert derive_status(StatusFacts(PS.RMG_REVIEW.value)).tone == cs.NEUTRAL


# ------------------------------------------------------------ the catalogue

def test_the_naming_convention_holds():
    labels = [d.label for d in cs.STATUS_DEFS]
    assert len(set(d.key for d in cs.STATUS_DEFS)) == len(cs.STATUS_DEFS)
    assert len(set(labels)) == len(labels)
    for prefix in ("Manual L1", "Manual L2", "Customer L1", "Customer L2", "AI L1"):
        assert any(label.startswith(f"{prefix} – ") for label in labels)
    assert not any("HR Screening" in label for label in labels)
    groups = {k for k, _ in cs.GROUPS}
    assert all(d.group in groups for d in cs.STATUS_DEFS)


_STATES = [None, cs.PENDING, cs.SCHEDULED, cs.HELD, cs.PASSED, cs.FAILED]


@pytest.mark.parametrize("stage", list(PS))
def test_every_derived_status_declares_its_stage(stage):
    """The list filter pre-narrows on StatusDef.stages — a status derived from a
    stage it does not declare would silently vanish from the filter."""
    for m1, m2, c1, c2, ai, req, screen, (budget, closed) in itertools.product(
        _STATES, _STATES[:3], _STATES, _STATES[:3],
        [None, cs.SCHEDULED, cs.FAILED, cs.REVIEW], [frozenset(), frozenset({"manual_l1"}), frozenset({"ai_l1"})],
        [None, "Pending", "Shortlisted", "Rejected"],
        [(None, None), (cs.TA_HOLD, None), (None, "ta")],
    ):
        rounds = {k: v for k, v in {"manual_l1": m1, "manual_l2": m2,
                                    "customer_l1": c1, "customer_l2": c2}.items() if v}
        status = derive_status(StatusFacts(stage.value, rmg_screening=screen, rounds=rounds,
                                           requested=req, ai_l1=ai,
                                           withdrawn_from=PS.RMG_REVIEW.value,
                                           budget_status=budget, ta_closed=closed))
        assert stage.value in cs.STATUS_BY_KEY[status.key].stages, (stage, status.key)


def test_catalogue_splits_statuses_by_bucket():
    cat = cs.catalogue()
    by_key = {s["key"]: s for s in cat["statuses"]}
    assert by_key["joined"]["active"] and not by_key["joined"]["closed"]
    assert by_key["self_withdrawn"]["closed"] and not by_key["self_withdrawn"]["active"]
    # Failed can be open (a "Leaning No" awaiting a call) or closed (No Hire).
    assert by_key["manual_l1_failed"]["active"] and by_key["manual_l1_failed"]["closed"]
    assert [g["key"] for g in cat["groups"]] == [k for k, _ in cs.GROUPS]


@pytest.mark.parametrize("status,result,timed,expected", [
    ("Scheduled", None, True, cs.SCHEDULED),
    ("Pending", None, False, cs.PENDING),
    (None, None, True, cs.SCHEDULED),
    ("Completed", None, True, cs.HELD),
    ("Completed", "Hire", True, cs.PASSED),
    ("Completed", "Leaning Hire", True, cs.PASSED),
    ("Completed", "No Hire", True, cs.FAILED),
    ("Completed", "Leaning No", True, cs.FAILED),
    ("Cancelled", None, True, None),
    ("No Show", None, True, None),
])
def test_round_state(status, result, timed, expected):
    assert cs.round_state(status, result, timed) == expected


# ---------------------------------------------------------------- loader

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


_seq = itertools.count(1)


def _profile(db, stage=PS.SOURCING, screening=None):
    n = next(_seq)
    cust = Customer(name=f"C{n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{n}", title="Role", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name=f"N{n}", email=f"n{n}@mail.com", phone="9999999999")
    db.add_all([opp, cand]); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         rmg_screening_status=screening)
    db.add(p); db.flush()
    return p


def _round(db, p, kind, *, status="Scheduled", result=None, stage=None):
    db.add(InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, kind=kind,
                          status=status, result=result, stage=stage,
                          scheduled_at=datetime(2026, 9, 26, 10, tzinfo=timezone.utc)))
    db.flush()


def test_loader_reads_rounds_requests_and_ai_in_batch(db):
    sourcing = _profile(db, screening="Shortlisted")
    l1_booked = _profile(db, PS.RMG_REVIEW)
    _round(db, l1_booked, "L1_Interview")
    l2_asked = _profile(db, PS.RMG_REVIEW)
    _round(db, l2_asked, "L1_Interview", status="Completed", result="Hire")
    db.add(CandidateProfileActivityLog(profile_id=l2_asked.id, user_id=1, action_type="L2_REQUESTED"))
    cancelled = _profile(db, PS.RMG_REVIEW)
    _round(db, cancelled, "L1_Interview", status="Cancelled")
    legacy_l2 = _profile(db, PS.L2_FEEDBACK)
    _round(db, legacy_l2, "Customer_Interview", status="Completed", result="Hire", stage="L2")
    ai = _profile(db, PS.TECHNICAL_SCREENING)
    db.add(AiInterviewLink(invite_token=f"tok-{ai.id}", candidate_id=ai.candidate_id,
                           opportunity_id=ai.opportunity_id, profile_id=ai.id, result="Passed"))
    db.flush()

    got = {pid: s["label"] for pid, s in cs.statuses_for(
        db, [sourcing, l1_booked, l2_asked, cancelled, legacy_l2, ai]).items()}
    assert got == {
        sourcing.id: "Technical Interview",
        l1_booked.id: "Manual L1 – Scheduled",
        l2_asked.id: "Manual L2 – Yet to Schedule",
        cancelled.id: "Manual L1 – Yet to Schedule",
        legacy_l2.id: "Customer L2 – Passed",
        ai.id: "AI L1 – Passed",
    }


def test_status_key_filter_keeps_pagination_exact(db):
    from routers.crm.candidate_profiles import _narrow_by_status

    booked = [_profile(db, PS.RMG_REVIEW) for _ in range(3)]
    for p in booked:
        _round(db, p, "L1_Interview")
    _profile(db, PS.RMG_REVIEW)                 # yet to schedule
    joined = _profile(db, PS.JOINED)
    stmt = _narrow_by_status(db, select(CandidateProfile), "manual_l1_scheduled")
    assert {p.id for p in db.execute(stmt).scalars()} == {p.id for p in booked}
    stmt = _narrow_by_status(db, select(CandidateProfile), "joined,manual_l1_pending")
    assert len(db.execute(stmt).scalars().all()) == 2 and joined.id in {
        p.id for p in db.execute(stmt).scalars()}
    assert db.execute(_narrow_by_status(db, select(CandidateProfile), "hr_round")).first() is None
    # A statement already carrying a join (search / sort) keeps it.
    joined = (select(CandidateProfile)
              .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
              .where(Candidate.first_name == Candidate.first_name))
    assert {p.id for p in db.execute(_narrow_by_status(db, joined, "manual_l1_scheduled")).scalars()} \
        == {p.id for p in booked}
    with pytest.raises(Exception) as err:
        _narrow_by_status(db, select(CandidateProfile), "nope")
    assert getattr(err.value, "status_code", None) == 400


def test_attach_to_rows_labels_rows_keyed_by_profile(db):
    p = _profile(db, PS.HR_SCREENING)
    rows = [{"profile_id": p.id}, {"profile_id": None}]
    cs.attach_to_rows(db, rows)
    assert rows[0]["profile_status"]["label"] == "HR Discussion"
    assert rows[1]["profile_status"] is None


def test_stage_column_is_gone_from_the_profiles_table():
    from routers.crm.table_preferences import TABLE_REGISTRY
    assert "stage" not in TABLE_REGISTRY["candidate_profiles"]["columns"]
    assert "pipeline_status" in TABLE_REGISTRY["candidate_profiles"]["columns"]


def test_status_options_route_is_declared_before_the_profile_route():
    from routers.crm.candidate_profiles import router
    paths = [r.path for r in router.routes if "GET" in getattr(r, "methods", set())]
    assert paths.index("/api/candidate-profiles/status-options") \
        < paths.index("/api/candidate-profiles/{profile_id}")


def test_phase_filter_narrows_the_list(db):
    from routers.crm.candidate_profiles import _narrow_by_status

    with_ta = _profile(db)
    with_rmg = _profile(db, screening="Pending")
    held = _profile(db, screening="Pending")
    held.budget_status = cs.TA_HOLD
    shortlisted = _profile(db, screening="Shortlisted")
    booked = _profile(db, PS.RMG_REVIEW)
    _round(db, booked, "L1_Interview")
    closed = _profile(db, PS.REJECTED)
    db.flush()

    def ids(**kw):
        return {p.id for p in db.execute(
            _narrow_by_status(db, select(CandidateProfile), kw.get("key"), kw.get("phase"))).scalars()}

    assert ids(phase="sourcing") == {with_ta.id, held.id}
    assert ids(phase="technical_screening") == {with_rmg.id}
    assert ids(phase="technical_interview") == {shortlisted.id, booked.id}
    assert ids(phase="closed") == {closed.id}
    assert ids(key="technical_interview", phase="technical_interview") == {shortlisted.id}


def test_moved_on_to_customer_l2_reads_yet_to_schedule():
    """29 Sep 2026 (user report): Sales moved the candidate to "Customer L2
    Interview" and it read "Customer L2 – Scheduled · Time not set" — TA had
    nothing to press. With no L2 booked it is TA's move."""
    assert _label(PS.L2_FEEDBACK, rounds={"customer_l1": cs.PASSED}) == "Customer L2 – Yet to Schedule"
    assert _label(PS.L2_FEEDBACK, rounds={"customer_l2": cs.SCHEDULED}) == "Customer L2 – Scheduled"
