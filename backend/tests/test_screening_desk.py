"""Screening Desk (25 Sep 2026) — `services.screening_desk` + the profile fast-track.

Pinned: what the queue holds and how it filters / counts / groups; who counts
as an INTERNAL candidate; auto-ATS scoring (scan only, per-profile savepoints,
batch cap); the internal fast-track to Sales and every way it refuses; the
ATS column announcement in table preferences; and the 0111 backfill.
In-memory SQLite with the usual Postgres-type shims.
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
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

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    AtsStatus, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Employee,
    Opportunity, OppType, PipelineStatus, Requirement, RequirementStatus, Resume,
)
from models.ai_links import AiInterviewLink  # noqa: E402
from services import screening_desk as desk  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
TODAY = date(2026, 9, 25)
USER = SimpleNamespace(id=1, username="rmg", full_name="Ravi RMG", roles={"RMG"})
REASON = "Existing employee on bench, already vetted"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    told: list[tuple] = []
    import services.candidate_profiles as cp
    monkeypatch.setattr(cp, "_notify_stage_owner",
                        lambda db, profile, prev, new, comment, user: told.append(("stage", new)))
    monkeypatch.setattr(desk, "_tell_ta", lambda db, profile, reason, user, **kw: told.append(("ta", profile.id)))
    s.info["told"] = told
    try:
        yield s
    finally:
        s.close()


_seq = iter(range(1, 10_000))


def _position(db, customer="HARMAN", title="Embedded C Developer", status=RequirementStatus.IN_PROGRESS):
    n = next(_seq)
    cust = db.execute(select(Customer).where(Customer.name == customer)).scalars().first()
    if cust is None:
        cust = Customer(name=customer)
        db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{n}", title=title, customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id,
                      title=title, no_of_positions=3, status=status, created_by=1)
    db.add(req); db.flush()
    return req


def _applicant(db, req, name, *, screening="Pending", stage=PipelineStatus.SOURCING,
               score=None, cv="/x/cv.pdf", resume=True, ta=2, applied=None, email=None, **extra):
    cand = Candidate(first_name=name, email=email or f"{name.lower()}@mail.com",
                     phone="9999999999", cv_url=cv)
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id,
                               pipeline_status=stage, rmg_screening_status=screening,
                               ta_owner_id=ta, ta_owner_name="Tara TA",
                               applied_on=applied or datetime(2026, 9, 20, tzinfo=timezone.utc),
                               **extra)
    db.add(profile); db.flush()
    if resume:
        db.add(Resume(requirement_id=req.id, candidate_id=cand.id, candidate_name=name,
                      email=cand.email, resume_file_url=cv or "/x/cv.pdf", ats_score=score,
                      ats_status=AtsStatus.SCORED if score is not None else AtsStatus.PENDING_SCAN))
        db.flush()
    return profile


def _employee(db, email, *, code="KX-001", active=True, personal=None, resigned=False):
    emp = Employee(first_name="Emp", last_name=code, email=email, employee_code=code,
                   personal_email=personal, is_active=active, is_resigned=resigned)
    db.add(emp); db.flush()
    return emp


def _queue(db, **kw):
    rows, meta = desk.desk_queue(db, desk.DeskFilters(**kw), today=TODAY)
    return rows, meta


# ------------------------------------------------------------------ the queue

def test_the_queue_is_pending_desk_stage_rows_on_live_positions(db):
    req = _position(db)
    _applicant(db, req, "Asha", score=82)
    _applicant(db, req, "Bala", screening="Shortlisted")
    _applicant(db, req, "Chitra", stage=PipelineStatus.SALES_SCREENING)          # with Sales now
    _applicant(db, req, "Deepa", is_hidden=True)                                 # hidden
    _applicant(db, req, "Farah", budget_status="TA_Hold")                        # TA parked it
    _applicant(db, req, "Gita", screening=None)                                  # TA has not sent it
    closed = _position(db, title="Closed role", status=RequirementStatus.CLOSED)
    _applicant(db, closed, "Esha")                                               # closed position

    rows, meta = _queue(db)
    assert [r["candidate_name"] for r in rows] == ["Asha"]
    row = rows[0]
    assert row["position_title"] == "Embedded C Developer"
    assert row["customer_name"] == "HARMAN"
    assert row["ats_score"] == 82.0 and row["ats_band"] == "high"
    assert row["waiting_days"] == 5
    assert row["internal"] is None and row["fast_track_block"]
    assert meta["counts"] == {"pending": 1, "shortlisted": 1, "review": 0, "rejected": 0, "all": 2}


def test_counts_ignore_the_screening_tab_but_honour_the_other_filters(db):
    harman, bosch = _position(db), _position(db, customer="BOSCH", title="Tester")
    _applicant(db, harman, "A"); _applicant(db, harman, "B", screening="Rejected")
    _applicant(db, bosch, "C")
    _, meta = _queue(db, screening="rejected", customer_id=harman.customer_id)
    assert meta["counts"] == {"pending": 1, "shortlisted": 0, "review": 0, "rejected": 1, "all": 2}


def test_positions_group_the_whole_result_with_pending_first(db):
    quiet, busy = _position(db, title="Quiet"), _position(db, title="Busy")
    _applicant(db, quiet, "Q1", screening="Shortlisted")
    for n in ("B1", "B2"):
        _applicant(db, busy, n)
    _, meta = _queue(db, screening="all")
    assert [(p["position_title"], p["count"], p["pending"]) for p in meta["positions"]] == [
        ("Busy", 2, 2), ("Quiet", 1, 0)]


def test_ats_bands_and_the_score_sort(db):
    req = _position(db)
    for name, score in (("Low", 30), ("Mid", 60), ("High", 90), ("None", None)):
        _applicant(db, req, name, score=score)
    assert [r["candidate_name"] for r in _queue(db, ats_band="medium")[0]] == ["Mid"]
    assert [r["candidate_name"] for r in _queue(db, ats_band="unscored")[0]] == ["None"]
    assert [r["candidate_name"] for r in _queue(db, sort="ats")[0]] == ["High", "Mid", "Low", "None"]


def test_the_latest_requirement_is_the_position_and_old_resumes_still_count(db):
    old = _position(db, title="Old JD")
    profile = _applicant(db, old, "Farah", score=55)
    newer = Requirement(req_number="REQ-NEW", opportunity_id=old.opportunity_id,
                        customer_id=old.customer_id, title="Revised JD", no_of_positions=2,
                        status=RequirementStatus.IN_PROGRESS, created_by=1)
    db.add(newer); db.flush()
    row = _queue(db)[0][0]
    assert row["profile_id"] == profile.id
    assert row["position_title"] == "Revised JD"
    assert row["ats_score"] == 55.0, "a resume on an earlier requirement of the deal still scores"


def test_search_and_bad_filters(db):
    req = _position(db)
    _applicant(db, req, "Gita"); _applicant(db, req, "Hari")
    assert [r["candidate_name"] for r in _queue(db, search="git")[0]] == ["Gita"]
    for bad in ({"screening": "maybe"}, {"ats_band": "great"}, {"sort": "random"},
                {"applied_from": date(2026, 9, 30), "applied_to": date(2026, 9, 1)}):
        with pytest.raises(HTTPException) as err:
            _queue(db, **bad)
        assert err.value.status_code == 400


# ------------------------------------------------------------------ internal

def test_internal_is_an_active_employee_by_email_personal_email_or_emp_id(db):
    req = _position(db)
    by_mail = _applicant(db, req, "Ira", email="ira@karnex.in")
    by_personal = _applicant(db, req, "Jay", email="jay@gmail.com")
    by_ref = _applicant(db, req, "Kavi", employee_ref="kx-077")
    gone = _applicant(db, req, "Leo", email="leo@karnex.in")
    outsider = _applicant(db, req, "Mona")
    _employee(db, "ira@karnex.in", code="KX-010")
    _employee(db, "jay@karnex.in", code="KX-011", personal="JAY@gmail.com")
    _employee(db, "kavi@karnex.in", code="KX-077")
    _employee(db, "leo@karnex.in", code="KX-099", active=False)

    rows = {r["profile_id"]: r for r in _queue(db)[0]}
    assert rows[by_mail.id]["internal"]["employee_code"] == "KX-010"
    assert rows[by_personal.id]["internal"]["employee_code"] == "KX-011"
    assert rows[by_ref.id]["internal"]["employee_code"] == "KX-077"
    assert rows[by_mail.id]["internal"]["deployment"] == "Bench"
    assert rows[by_mail.id]["fast_track_block"] is None
    assert rows[gone.id]["internal"] is None, "an inactive employee is not internal"
    assert rows[outsider.id]["internal"] is None

    internal_only = {r["candidate_name"] for r in _queue(db, internal=True)[0]}
    external_only = {r["candidate_name"] for r in _queue(db, internal=False)[0]}
    assert internal_only == {"Ira", "Jay", "Kavi"}
    assert external_only == {"Leo", "Mona"}


# ------------------------------------------------------------------ auto ATS

def test_scoring_fills_gaps_only_and_never_runs_the_auto_pipeline(db, monkeypatch):
    import services.resumes as res
    import services.slot_booking as sb

    def fake_scan(db, resume, req, user_id):
        if "broken" in resume.resume_file_url:
            raise HTTPException(status_code=422, detail="Document does not appear to be a resume")
        resume.ats_score = 64
        resume.ats_status = AtsStatus.SCORED
        return {"ats_score": 64, "breakdown": {}}

    monkeypatch.setattr(res, "run_ats_scan", fake_scan)
    # 29 Sep 2026: the ATS auto-shortlist + slot-invite pipeline is GONE — no scan
    # anywhere mails the candidate; candidate mail is TA's call.
    assert not hasattr(sb, "auto_pipeline_after_scan")
    monkeypatch.setattr(sb, "send_slot_invite",
                        lambda *a, **k: pytest.fail("scoring must never invite the candidate"))
    req = _position(db)
    profile_only = _applicant(db, req, "Nila", resume=False)
    unscored = _applicant(db, req, "Om")
    done = _applicant(db, req, "Pia", score=71)
    no_cv = _applicant(db, req, "Quin", resume=False, cv=None)
    broken = _applicant(db, req, "Ravi", cv="/x/broken.pdf")

    out = desk.score_profiles(db, [profile_only.id, unscored.id, done.id, no_cv.id, broken.id], USER)
    assert {s["profile_id"] for s in out["scored"]} == {profile_only.id, unscored.id}
    assert [s["profile_id"] for s in out["skipped"]] == [done.id]
    failed = {f["profile_id"]: f["reason"] for f in out["failed"]}
    assert set(failed) == {no_cv.id, broken.id}
    assert "No CV on file" in failed[no_cv.id]
    # The profile-only applicant now has a resume row carrying the score.
    made = db.execute(select(Resume).where(Resume.candidate_id == profile_only.candidate_id)).scalars().one()
    assert float(made.ats_score) == 64
    # A failed scan leaves nothing half-written behind.
    assert db.execute(select(Resume).where(Resume.candidate_id == no_cv.candidate_id)).first() is None


def test_scoring_is_capped_per_request(db, monkeypatch):
    import services.resumes as res
    seen: list[int] = []
    monkeypatch.setattr(res, "run_ats_scan",
                        lambda db, resume, req, uid: seen.append(resume.id) or {"ats_score": 50})
    req = _position(db)
    ids = [_applicant(db, req, f"P{i}").id for i in range(desk.MAX_SCORE_BATCH + 3)]
    desk.score_profiles(db, ids, USER)
    assert len(seen) == desk.MAX_SCORE_BATCH


# ------------------------------------------------------------------ fast-track

def test_fast_track_sends_an_internal_candidate_straight_to_sales(db):
    req = _position(db)
    profile = _applicant(db, req, "Sita", email="sita@karnex.in", stage=PipelineStatus.TECHNICAL_SCREENING)
    _employee(db, "sita@karnex.in", code="KX-321")

    emp = desk.fast_track_internal(db, profile, REASON, USER)
    assert emp["employee_code"] == "KX-321"
    assert profile.pipeline_status == PipelineStatus.SALES_SCREENING
    assert profile.rmg_screening_status == "Shortlisted"
    assert profile.rmg_screening_by == USER.id
    assert profile.employee_ref == "KX-321", "the Emp ID travels with the profile for the join"
    assert profile.sales_submission_date == date.today()
    logs = [a for (a,) in db.execute(select(CandidateProfileActivityLog.action_type)
                                     .where(CandidateProfileActivityLog.profile_id == profile.id)).all()]
    assert logs == ["FAST_TRACKED", "STATUS_CHANGE"]
    assert db.info["told"] == [("stage", "Sales_Screening"), ("ta", profile.id)]
    # The desk no longer lists them — they are Sales's now.
    assert profile.id not in {r["profile_id"] for r in _queue(db, screening="all")[0]}


def test_direct_to_sales_skips_the_ladder_for_any_strong_match(db):
    """30 Sep 2026 (user ask): RMG / GM can send a close match straight to Sales
    for the customer round — external candidates too — with a mandatory reason."""
    req = _position(db)
    profile = _applicant(db, req, "Ravi", email="ravi@gmail.com", stage=PipelineStatus.SOURCING)
    with pytest.raises(HTTPException) as err:
        desk.direct_to_sales(db, profile, "short", USER)
    assert err.value.status_code == 400 and profile.pipeline_status == PipelineStatus.SOURCING

    desk.direct_to_sales(db, profile, REASON, USER)
    assert profile.pipeline_status == PipelineStatus.SALES_SCREENING
    assert profile.rmg_screening_status == "Shortlisted" and profile.rmg_screening_by == USER.id
    assert profile.sales_submission_date == date.today()
    logs = db.execute(select(CandidateProfileActivityLog.action_type, CandidateProfileActivityLog.comment)
                      .where(CandidateProfileActivityLog.profile_id == profile.id)).all()
    assert [a for a, _ in logs] == ["FAST_TRACKED", "STATUS_CHANGE"]
    assert "[direct to Sales]" in logs[1][1] and REASON in logs[1][1]
    assert db.info["told"] == [("stage", "Sales_Screening"), ("ta", profile.id)]
    # Once with Sales the button is gone (409), and the desk row said so beforehand.
    assert desk.direct_to_sales_block(profile)
    with pytest.raises(HTTPException) as err:
        desk.direct_to_sales(db, profile, REASON, USER)
    assert err.value.status_code == 409


@pytest.mark.parametrize("case, status", [
    ("external", 400), ("short_note", 400), ("with_sales", 409), ("resigned", 400),
])
def test_fast_track_refuses(db, case, status):
    req = _position(db)
    stage = PipelineStatus.SALES_SCREENING if case == "with_sales" else PipelineStatus.SOURCING
    profile = _applicant(db, req, "Uma", email="uma@karnex.in", stage=stage)
    if case != "external":
        _employee(db, "uma@karnex.in", resigned=(case == "resigned"))
    note = "too short" if case == "short_note" else REASON
    with pytest.raises(HTTPException) as err:
        desk.fast_track_internal(db, profile, note, USER)
    assert err.value.status_code == status
    assert profile.pipeline_status == stage, "nothing is written when the move is refused"
    assert db.info["told"] == []


# ------------------------------------------------------------------ interview route

@pytest.mark.parametrize("screening, stage, ai, rounds, chosen, open_", [
    # Pending screening: nothing to choose yet.
    ("Pending", "Sourcing", None, None, None, False),
    # Shortlisted, nothing taken: RMG / GM must pick AI L1 or manual.
    ("Shortlisted", "Sourcing", None, None, None, True),
    ("Shortlisted", "Technical_Screening", None, {}, None, True),
    # An AI link — even a Pending one — is the AI route, chosen.
    ("Shortlisted", "Sourcing", {"ai_interview_status": "Pending"}, None, "ai", False),
    # A manual L1 asked for or booked is the manual route.
    ("Shortlisted", "Sourcing", None, {"l1_manual_requested": True}, "manual", False),
    ("Shortlisted", "Sourcing", None, {"l1_manual_scheduled": True}, "manual", False),
    # Past the desk stages the choice belongs to the round owners, not here.
    ("Shortlisted", "RMG_Review", None, None, None, False),
    ("Rejected", "Sourcing", None, None, None, False),
])
def test_interview_route_is_open_only_for_a_shortlisted_undecided_candidate(
        screening, stage, ai, rounds, chosen, open_):
    r = desk.interview_route(screening=screening, stage=stage, ai=ai, rounds=rounds)
    assert (r["chosen"], r["open"]) == (chosen, open_)


def test_desk_rows_carry_the_interview_route_from_the_shared_helpers(db):
    req = _position(db)
    fresh = _applicant(db, req, "Fara", screening="Shortlisted")
    ai = _applicant(db, req, "Gita", screening="Shortlisted")
    db.add(AiInterviewLink(invite_token="tok-gita", candidate_id=ai.candidate_id,
                           opportunity_id=req.opportunity_id, profile_id=ai.id,
                           requirement_id=req.id, result="Pending"))
    manual = _applicant(db, req, "Hema", screening="Shortlisted")
    db.add(CandidateProfileActivityLog(profile_id=manual.id, user_id=1,
                                       action_type="L1_REQUESTED", comment="go manual"))
    db.flush()

    rows, _ = _queue(db, screening="shortlisted")
    routes = {r["candidate_name"]: r["interview_route"] for r in rows}
    assert routes["Fara"]["open"] is True and routes["Fara"]["chosen"] is None
    assert routes["Gita"] == {**routes["Gita"], "chosen": "ai", "open": False,
                              "ai_interview_status": "Pending"}
    assert routes["Hema"] == {**routes["Hema"], "chosen": "manual", "open": False,
                              "manual_l1_requested": True, "manual_l1_scheduled": False}


# ------------------------------------------------------------------ wiring

def test_the_endpoints_are_gated_by_their_approval_actions():
    from services import action_permissions as ap

    desk_src = (BACKEND / "routers/crm/screening_desk.py").read_text(encoding="utf-8")
    prof_src = (BACKEND / "routers/crm/candidate_profiles.py").read_text(encoding="utf-8")
    assert 'desk_gate = gated_write_action("profile.rmg_screening", "profiles")' in desk_src
    assert desk_src.count("Depends(desk_gate)") == 5   # queue · score · tasks · results-reviewed · handed-over
    assert 'fast_track_gate = gated_write_action("profile.fast_track_internal", "profiles")' in prof_src
    route = prof_src[prof_src.index('"/{profile_id}/fast-track-to-sales"'):][:400]
    assert "Depends(fast_track_gate)" in route
    for action in ("profile.rmg_screening", "profile.fast_track_internal"):
        assert ap.is_approval(action)
        assert ap.ACTIONS[action].roles == ("RMG", "GM")
    from routers.crm import _MODULES
    assert "screening_desk" in _MODULES
    from routers.crm.email_flows import EVENTS
    assert "profile.fast_tracked" in {e["event"] for e in EVENTS}


def test_the_ats_column_is_announced_to_saved_layouts():
    from routers.crm.table_preferences import TABLE_REGISTRY, _clean

    reg = TABLE_REGISTRY["candidate_profiles"]
    saved = {"columns": [{"key": "candidate_name"}, {"key": "ai_interview"},
                         {"key": "pipeline_status"}], "sort": []}
    cols = _clean(saved, reg)["columns"]
    keys = [c["key"] for c in cols]
    assert keys[:3] == ["candidate_name", "ai_interview", "ats_score"]
    assert cols[2]["visible"] is True
    # Only ANNOUNCED columns arrive visible (29 Sep 2026: the round ladder, the
    # stage and the next interview joined ats_score); the rest stay hidden.
    announced = set(reg["announce"])
    assert all(c["visible"] == (c["key"] in announced or c["key"] in {"candidate_name", "ai_interview", "pipeline_status"})
               for c in cols), "other new columns still arrive hidden"
    # Once the user has it in their layout, their choice stands.
    hidden = {"columns": saved["columns"] + [{"key": "ats_score", "visible": False}], "sort": []}
    assert next(c for c in _clean(hidden, reg)["columns"] if c["key"] == "ats_score")["visible"] is False
    assert "ats_score" in reg["sortable"]


def test_0111_backfills_saved_approval_lists_for_rmg_and_gm_only():
    path = next((BACKEND / "alembic" / "versions").glob("0111_*.py"))
    spec = importlib.util.spec_from_file_location("m0111", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE access_templates (id INTEGER PRIMARY KEY, role TEXT, action_access JSON)"))
        conn.execute(sa.text("CREATE TABLE custom_roles (id INTEGER PRIMARY KEY, name TEXT, action_access JSON)"))
        conn.execute(sa.text("""INSERT INTO access_templates VALUES
            (1, 'RMG', '["requirement.engineering_approve", "profile.rmg_screening"]'),
            (2, 'Sales', '["invoice.revision.approve"]'),
            (3, 'RMG', NULL)"""))
        conn.execute(sa.text("""INSERT INTO custom_roles VALUES (1, 'GM', '["timesheet.approve"]')"""))
        mod._backfill(conn, "access_templates", "role")
        mod._backfill(conn, "custom_roles", "name")
        rows = dict(conn.execute(sa.text("SELECT id, action_access FROM access_templates")).all())
        gm = conn.execute(sa.text("SELECT action_access FROM custom_roles")).scalar_one()
    import json
    assert json.loads(rows[1]) == ["requirement.engineering_approve", "profile.rmg_screening",
                                   "profile.fast_track_internal"]
    assert json.loads(rows[2]) == ["invoice.revision.approve"]
    assert rows[3] is None, "an unconfigured list keeps following the code defaults"
    assert json.loads(gm) == ["timesheet.approve", "profile.rmg_screening", "profile.fast_track_internal"]


# ------------------------------------------------------------------ 28 Sep 2026: the whole ladder on the desk

def _rounds(**kw):
    base = {f"{p}_{k}": None for p in ("l1_manual", "l2") for k in ("requested", "scheduled", "event_id", "result", "when", "link")}
    base.update({k: False for k in base if k.endswith(("_requested", "_scheduled", "_link"))})
    base.update(kw)
    return base


def test_next_step_reads_like_a_to_do_list():
    ns = lambda **kw: desk.next_step(**{"screening": "Shortlisted", "stage": "Sourcing", "ai": None, "rounds": None, **kw})  # noqa: E731
    assert ns(screening="Pending")["key"] == "screen"
    assert ns(screening="Rejected")["key"] == "rejected"
    assert ns()["key"] == "route"                                           # shortlisted, nothing chosen
    assert ns(ai={"ai_effective_result": "Pending"})["key"] == "ai_wait"
    assert ns(ai={"ai_effective_result": "Failed"})["key"] == "ai_failed"
    assert ns(ai={"ai_effective_result": "Selected"})["key"] == "ai_done"
    assert ns(rounds=_rounds(l1_manual_requested=True))["key"] == "l1_book"
    assert ns(rounds=_rounds(l1_manual_requested=True, l1_manual_scheduled=True))["key"] == "l1_feedback"
    assert ns(stage="RMG_Review", rounds=_rounds(l1_manual_scheduled=True, l1_manual_result="Hire"))["key"] == "decide"
    assert ns(stage="RMG_Review", rounds=_rounds(l2_requested=True))["key"] == "l2_book"
    assert ns(stage="RMG_Review", rounds=_rounds(l2_scheduled=True))["key"] == "l2_feedback"
    assert ns(stage="RMG_Review", rounds=_rounds(l2_scheduled=True, l2_result="Hire"))["key"] == "decide"
    # every step names an owner and a tone the UI can key off
    for step in (ns(), ns(screening="Pending"), ns(stage="RMG_Review")):
        assert step["owner"] in ("you", "TA", "candidate", "AI", "done") and step["tone"] in ("ok", "warn", "bad", "none")


def test_decision_is_only_at_review_and_only_once_the_ladder_is_judged():
    assert desk.decision_state(stage="Sourcing", rounds=None) == {"can_decide": False, "blocked": None}
    assert desk.decision_state(stage="RMG_Review", rounds=None) == {"can_decide": True, "blocked": None}
    assert desk.decision_state(stage="RMG_Review", rounds=_rounds(l1_manual_requested=True))["blocked"] == "Record the manual L1 outcome first"
    assert desk.decision_state(stage="RMG_Review", rounds=_rounds(l1_manual_scheduled=True, l1_manual_result="Hire",
                                                                    l2_scheduled=True))["blocked"] == "Record the L2 outcome first"
    assert desk.decision_state(stage="RMG_Review", rounds=_rounds(l2_scheduled=True, l2_result="No Hire"))["blocked"] is None


def test_the_review_tab_holds_rmg_review_rows_and_shortlisted_holds_the_rest(db):
    req = _position(db)
    _applicant(db, req, "Pend")
    _applicant(db, req, "Short", screening="Shortlisted")
    _applicant(db, req, "Rev", screening="Shortlisted", stage=PipelineStatus.RMG_REVIEW)
    rows, meta = _queue(db, screening="review")
    assert [r["candidate_name"] for r in rows] == ["Rev"]
    assert rows[0]["tab"] == "review" and rows[0]["decision"]["can_decide"] is True
    assert rows[0]["next_step"]["key"] == "decide"
    rows, _ = _queue(db, screening="shortlisted")
    assert [r["candidate_name"] for r in rows] == ["Short"]
    assert rows[0]["next_step"]["key"] == "route" and rows[0]["decision"]["can_decide"] is False
    assert meta["counts"] == {"pending": 1, "shortlisted": 1, "review": 1, "rejected": 0, "all": 3}
    pos = meta["positions"][0]
    assert pos["review"] == 1 and pos["jd_missing"] is True and pos["skills"] == []
    assert "rounds" in rows[0] and "ai_l1" in rows[0]
    assert meta["round_results"][-1] == "Strong Hire"


def test_position_headers_say_when_the_jd_is_in_place(db):
    from models import RequirementSkill, Skill
    req = _position(db)
    skill = Skill(name="C++")
    db.add(skill); db.flush()
    db.add(RequirementSkill(requirement_id=req.id, skill_id=skill.id, is_mandatory=True, min_rating=4)); db.flush()
    _applicant(db, req, "A")
    _, meta = _queue(db)
    pos = meta["positions"][0]
    assert pos["jd_missing"] is False
    assert pos["skills"] == [{"skill_id": skill.id, "skill_name": "C++", "is_mandatory": True, "min_rating": 4}]


def test_approvals_queue_lists_pending_rmg_reviews_only_for_someone_who_may_approve(db, monkeypatch):
    import services.action_permissions as ap
    waiting = _position(db, title="Waiting", status=RequirementStatus.PENDING_ENGINEERING_REVIEW)
    _position(db, title="Live")
    monkeypatch.setattr(ap, "user_may", lambda db, user, action, access=None: False)
    assert desk.approvals_queue(db, USER) == {"can_approve": False, "items": []}
    monkeypatch.setattr(ap, "user_may", lambda db, user, action, access=None: action == desk.APPROVE_ACTION)
    out = desk.approvals_queue(db, USER)
    assert out["can_approve"] is True
    assert [i["requirement_id"] for i in out["items"]] == [waiting.id]
    assert out["items"][0]["has_jd_file"] is False and out["items"][0]["skills"] == []


def test_a_gm_with_the_screening_approval_acts_as_rmg_on_the_technical_ladder(monkeypatch):
    """GM is a custom role: roles == {"GM"}. Holding `profile.rmg_screening`
    makes them RMG for the RMG_Review stage and the L1–L4 rounds — never for
    Sales' customer rounds or HR's round."""
    import services.action_permissions as ap
    from services import interview_rounds as ir
    from services.candidate_profiles import user_may_transition_from
    gm = SimpleNamespace(id=9, username="gm", full_name="Gita GM", roles={"GM"}, is_admin=False)
    monkeypatch.setattr(ap, "user_may", lambda db, user, action, access=None: action == ap.SCREENING_ACTION)
    db = object()
    assert ap.screens_as_rmg(db, gm) is True
    assert ap.screens_as_rmg(None, gm) is False                       # no DB → no lookup, no widening
    assert ap.screens_as_rmg(None, SimpleNamespace(roles={"RMG"}, is_admin=False)) is True
    assert user_may_transition_from("RMG_Review", gm, db) is True
    assert user_may_transition_from("RMG_Review", gm) is False        # callers without a session keep the pure rule
    assert user_may_transition_from("Sales_Screening", gm, db) is False
    assert set(ir.rounds_writable_by(gm, db)) == {"L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview"}
    assert ir.rounds_writable_by(gm) == []
    ir.ensure_may_write_round(gm, "L2_F2F", db)
    with pytest.raises(HTTPException) as exc:
        ir.ensure_may_write_round(gm, "Customer_Interview", db)
    assert exc.value.status_code == 403
    # a template that grants a different approval is not RMG
    monkeypatch.setattr(ap, "user_may", lambda db, user, action, access=None: action == "timesheet.approve")
    assert ap.screens_as_rmg(db, gm) is False


# ------------------------------------------------------------------ search + filters (28 Sep 2026)

def test_search_matches_customer_position_number_and_city(db):
    harman = _position(db, customer="HARMAN")
    visteon = _position(db, customer="VISTEON", title="Tester")
    _applicant(db, harman, "Asha")
    b = _applicant(db, visteon, "Bala")
    db.get(Candidate, b.candidate_id).city = "Pune"
    db.flush()
    assert [r["candidate_name"] for r in _queue(db, search="visteon")[0]] == ["Bala"]
    assert [r["candidate_name"] for r in _queue(db, search=visteon.req_number)[0]] == ["Bala"]
    assert [r["candidate_name"] for r in _queue(db, search="pune")[0]] == ["Bala"]
    assert [r["candidate_name"] for r in _queue(db, location="pun")[0]] == ["Bala"]


def test_experience_fit_reads_the_positions_band(db):
    req = _position(db)
    req.experience_min, req.experience_max = 4, 7
    fits = _applicant(db, req, "Fits")
    junior = _applicant(db, req, "Junior")
    _applicant(db, req, "Blank")
    db.get(Candidate, fits.candidate_id).experience_years = 5
    db.get(Candidate, junior.candidate_id).experience_years = 2
    db.flush()
    names = lambda **kw: sorted(r["candidate_name"] for r in _queue(db, **kw)[0])  # noqa: E731
    assert names(exp_fit="in") == ["Fits"]
    assert names(exp_fit="out") == ["Junior"]
    assert names(exp_fit="unknown") == ["Blank"]


def test_whose_move_and_route_filter_through_the_ladder(db):
    req = _position(db)
    _applicant(db, req, "Pending")                                             # your move: screen
    manual = _applicant(db, req, "Manual", screening="Shortlisted")
    db.add(CandidateProfileActivityLog(profile_id=manual.id, user_id=1, action_type="L1_REQUESTED"))
    _applicant(db, req, "Open", screening="Shortlisted")                       # route still open
    db.flush()
    names = lambda **kw: sorted(r["candidate_name"] for r in _queue(db, screening="all", **kw)[0])  # noqa: E731
    assert names(next_owner="TA") == ["Manual"]                                # TA books the L1
    assert names(next_owner="you") == ["Open", "Pending"]
    assert names(route="manual") == ["Manual"]
    assert names(route="none") == ["Open", "Pending"]
    rows, meta = desk.desk_queue(db, desk.DeskFilters(screening="all", next_owner="you"), limit=1, today=TODAY)
    assert len(rows) == 1 and meta["total"] == 2                               # paged after the filter


def test_bad_derived_filter_values_are_refused(db):
    for kw in ({"next_owner": "boss"}, {"route": "phone"}, {"exp_fit": "maybe"}):
        with pytest.raises(HTTPException):
            _queue(db, **kw)


def test_options_list_the_positions_for_the_position_filter(db):
    req = _position(db, title="Embedded C Developer")
    _applicant(db, req, "Asha")
    _, meta = _queue(db)
    assert [(p["id"], p["label"]) for p in meta["options"]["positions"]] == [
        (req.id, f"Embedded C Developer · {req.req_number}")]



def test_no_scan_route_mails_the_candidate():
    """29 Sep 2026 (user report): applying a candidate and pressing "Score N
    pending" mailed "pick your interview slot" — the ATS auto-threshold hook.
    Candidate mail now only ever follows a TA action (schedule / send invite)."""
    import inspect
    import routers.crm.resumes as r
    for fn in (r.ats_scan, r.ats_scan_profile, r.scan_all_resumes):
        src = inspect.getsource(fn)
        assert "send_slot_invite" not in src and "notify_candidate" not in src and "auto_pipeline" not in src


def test_notice_ai_and_l1_buckets_read_what_is_recorded():
    """29 Sep 2026 filters: the notice text, the AI L1 and the manual L1 as buckets (PURE)."""
    assert [desk.notice_bucket(t) for t in ("Immediate", "15 days", "30", "2 months", "90 days", None, "tbd")] \
        == ["15", "15", "30", "60", "90", "unknown", "unknown"]
    assert desk.notice_days("3 weeks") == 21
    assert desk.ai_result_bucket(None) == "none"
    assert desk.ai_result_bucket({"ai_effective_result": "Passed"}) == "passed"
    assert desk.ai_result_bucket({"ai_effective_result": "Failed"}) == "failed"
    assert desk.ai_result_bucket({"ai_effective_result": None}) == "pending"
    assert desk.l1_result_bucket({"l1_manual_result": "Hire"}) == "hire"
    assert desk.l1_result_bucket({"l1_manual_result": "No Hire"}) == "no_hire"
    assert desk.l1_result_bucket({"l1_manual_requested": True}) == "awaiting"
    assert desk.l1_result_bucket({}) == "none"


def test_budget_priority_waiting_experience_and_notice_filters(db):
    """29 Sep 2026 (user ask: "whatever filter GM & RMG need"): expected CTC vs the
    position's budget, requirement priority, waiting at least N days, an
    experience range and the notice bucket all narrow the queue."""
    from datetime import date as _date, timedelta as _td
    from models.requirements import Priority

    req = _position(db)
    req.budget_ctc_max = 1_000_000
    req.priority = Priority.HIGH
    over = _applicant(db, req, "Over", applied=datetime.now(timezone.utc) - _td(days=10))
    within = _applicant(db, req, "Within", applied=datetime.now(timezone.utc) - _td(days=1))
    _applicant(db, req, "Blank", applied=datetime.now(timezone.utc))
    over.expected_ctc, within.expected_ctc = 1_500_000, 800_000
    c_over, c_within = db.get(Candidate, over.candidate_id), db.get(Candidate, within.candidate_id)
    c_over.experience_years, c_within.experience_years = 8, 3
    c_over.notice_period, c_within.notice_period = "60 days", "Immediate"
    other = _position(db, customer="VISTEON")
    other.priority = Priority.LOW
    _applicant(db, other, "LowPri")
    db.flush()
    names = lambda **kw: sorted(r["candidate_name"] for r in _queue(db, **kw)[0])  # noqa: E731
    assert names(budget="over") == ["Over"]
    assert names(budget="within") == ["Within"]
    assert "Blank" in names(budget="unknown")
    assert names(priority="High") == ["Blank", "Over", "Within"]
    assert names(waiting_min=7) == ["LowPri", "Over"]                  # LowPri applied 20 Sep
    assert names(exp_min=5) == ["Over"]
    assert names(exp_max=5) == ["Within"]
    assert names(notice="15") == ["Within"]
    assert names(notice="60") == ["Over"]
    assert _date.today()                                                # (today drives waiting_min)
    for kw in ({"budget": "cheap"}, {"priority": "Urgent"}, {"notice": "soon"}, {"ai_result": "maybe"},
               {"exp_min": 9, "exp_max": 2}):
        with pytest.raises(HTTPException):
            _queue(db, **kw)
