"""RMG-cleared candidates with no resume reach Applied Candidates (28 Aug 2026).

A candidate added from the Candidates page and applied to an opportunity has a
CandidateProfile but NO Resume row, so the resume-driven Applied Candidates tab
— where every interview is scheduled — could never see them. Once RMG
shortlists them the list appends a profile-only row carrying the ids the
scheduling actions need.

Pinned here: who qualifies, what the row says, and that the search /
applied-by filters still apply to it.

Run:  cd backend && python -m pytest tests/test_applied_profile_only_rows.py -q
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

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
    Candidate, CandidateProfile, Customer, Opportunity, OppType, PipelineStatus,
    Requirement, RequirementStatus, Resume,
)
from routers.crm.resumes import _profile_only_applied_rows  # noqa: E402


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


def _req(db):
    cust = Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-1", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id,
                      title="Manual Integration Test Engineer", no_of_positions=35,
                      status=RequirementStatus.IN_PROGRESS, created_by=1)
    db.add(req); db.flush()
    return req


def _applicant(db, req, name, *, screening, with_resume=False, ta="Mohammed Suhel"):
    cand = Candidate(first_name=name, email=f"{name.lower()}@example.com", phone="9999999999")
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id,
                               pipeline_status=PipelineStatus.SOURCING,
                               rmg_screening_status=screening, ta_owner_name=ta)
    db.add(profile); db.flush()
    if with_resume:
        db.add(Resume(requirement_id=req.id, candidate_id=cand.id, candidate_name=name,
                      email=cand.email, resume_file_url="/x/cv.pdf"))
        db.flush()
    return profile


def _rows(db, req, **kw):
    kw.setdefault("search", None)
    kw.setdefault("applied_by", None)
    return _profile_only_applied_rows(db, req, **kw)


def test_every_applicant_without_a_resume_appears(db):
    """All applied candidates live in Applied Candidates (28 Sep 2026) — the
    TA's separate Applicants tab is gone, so pending / rejected profiles with
    no CV row must show here too. Hidden profiles and CV-backed ones do not."""
    req = _req(db)
    _applicant(db, req, "Vamsi", screening="Shortlisted")
    _applicant(db, req, "Pending", screening="Pending")
    _applicant(db, req, "Rejected", screening="Rejected")
    _applicant(db, req, "HasCv", screening="Shortlisted", with_resume=True)  # listed as a resume
    hidden = _applicant(db, req, "Hidden", screening="Pending")
    hidden.is_hidden = True
    db.flush()

    rows = _rows(db, req)
    assert sorted(r["candidate_name"] for r in rows) == ["Pending", "Rejected", "Vamsi"]
    row = next(r for r in rows if r["candidate_name"] == "Vamsi")
    assert row["is_profile_only"] is True
    assert row["profile_id"] is not None
    assert row["id"] < 0, "profile-only rows use a negative id so they cannot collide"
    assert row["rmg_screening_status"] == "Shortlisted"
    assert row["ats_score"] is None and row["ats_status"] is None
    assert row["over_budget"] is False


def test_ai_l1_state_reaches_the_row(db):
    """The row must show the AI L1 it was given (user report, 28 Aug 2026).

    The interview is linked by PROFILE — there is no resume — so the
    resume-keyed enrichment found nothing and the row read "Not Scheduled"
    forever, which also hid the Schedule L2 button.
    """
    from models import AiInterviewLink
    from routers.crm.resumes import _ai_state_by_profile

    req = _req(db)
    profile = _applicant(db, req, "Pavan", screening="Shortlisted")
    db.add(AiInterviewLink(invite_token="tok-1", candidate_id=profile.candidate_id,
                           opportunity_id=req.opportunity_id, profile_id=profile.id,
                           requirement_id=req.id, level="L1", result="Passed",
                           overall_score_percent=77.7, interview_record_id="rec-1"))
    db.flush()

    state = _ai_state_by_profile(db, [profile.id])[profile.id]
    assert state["ai_interview_status"] == "Passed"
    assert state["ai_overall_score_percent"] == 77.7
    assert state["ai_invite_url"].endswith("?invite=tok-1")
    assert state["l2_scheduled"] is False  # no L2 round yet — button stays offered

    row = _rows(db, req)[0]
    assert row["ai_interview_status"] == "Passed"
    assert row["ai_report_link"] and "rec-1" in row["ai_report_link"]


def test_search_and_applied_by_filters_apply(db):
    req = _req(db)
    _applicant(db, req, "Vamsi", screening="Shortlisted", ta="Mohammed Suhel")
    _applicant(db, req, "Gargeeled", screening="Shortlisted", ta="Gargee Joshi")

    assert [r["candidate_name"] for r in _rows(db, req, search="vamsi")] == ["Vamsi"]
    assert [r["candidate_name"] for r in _rows(db, req, applied_by="Gargee Joshi")] == ["Gargeeled"]
    # Dismissed-only view is about CVs; a profile-only row has none.
    assert _rows(db, req, dismissed=True) == []


def test_merged_page_counts_and_orders_both_kinds(db):
    """The header count and the table have to agree (user report, 1 Sep 2026).

    Profile-only rows used to be appended to page 1 *after* pagination, so the
    meta said "4 resumes" over a table showing 6 — and page 2 dropped them.
    """
    from routers.crm.resumes import _paginate_merged
    from sqlalchemy import select as _select

    req = _req(db)
    for i in range(4):
        _applicant(db, req, f"WithCv{i}", screening="Shortlisted", with_resume=True)
    _applicant(db, req, "NoCv1", screening="Shortlisted")
    _applicant(db, req, "NoCv2", screening="Shortlisted")

    stmt = (_select(Resume).where(Resume.requirement_id == req.id)
            .order_by(Resume.created_at.desc(), Resume.id.desc()))
    extra = _rows(db, req)
    assert len(extra) == 2

    items, extra_page, order, meta = _paginate_merged(db, stmt, extra, 1, 4)
    assert meta["total"] == 6, "every row in the list counts toward the total"
    assert meta["pages"] == 2
    assert len(order) == 4

    items2, extra2, order2, meta2 = _paginate_merged(db, stmt, _rows(db, req), 2, 4)
    assert len(order2) == 2 and meta2["total"] == 6
    # No row appears on both pages.
    assert not (set(order) & set(order2))


def test_the_candidate_just_acted_on_comes_first(db):
    """28 Sep 2026, user ask: once RMG / GM act on a candidate (shortlist, the
    manual L1 route), their row tops Applied Candidates so TA's next move is
    the first thing on the list — whatever the upload order."""
    from datetime import datetime, timedelta, timezone

    from models import CandidateProfileActivityLog
    from routers.crm.resumes import _last_activity_by_candidate, _paginate_merged
    from sqlalchemy import select as _select

    req = _req(db)
    old = _applicant(db, req, "OldWithCv", screening="Shortlisted", with_resume=True)
    for i in range(3):
        _applicant(db, req, f"New{i}", screening="Pending", with_resume=True)
    db.add(CandidateProfileActivityLog(profile_id=old.id, user_id=1, action_type="L1_REQUESTED",
                                       timestamp=datetime.now(timezone.utc) + timedelta(hours=1)))
    db.flush()
    stmt = _select(Resume).where(Resume.requirement_id == req.id)
    items, _extra, order, _meta = _paginate_merged(
        db, stmt, [], 1, 10, _last_activity_by_candidate(db, req.opportunity_id))
    assert items[0].candidate_name == "OldWithCv"


# ------------------------------------------------ ATS for a profile-only row

def test_profile_only_applicant_gets_a_resume_row_from_their_cv(db):
    """ATS had nothing to scan for a candidate applied from the Candidates
    page (user report, 2 Sep 2026): no resume row. Their own CV is it."""
    from fastapi import HTTPException
    from routers.crm.resumes import ensure_resume_for_profile

    req = _req(db)
    p = _applicant(db, req, "Praveen", screening="Shortlisted")
    cand = db.get(Candidate, p.candidate_id)

    # No CV on the record → a plain 422 that says what to do, not a crash.
    with pytest.raises(HTTPException) as err:
        ensure_resume_for_profile(db, p, req)
    assert err.value.status_code == 422
    assert "CV" in err.value.detail

    cand.cv_url = "/api/crm-files/cv/praveen.pdf"
    cand.experience_years = 4
    db.flush()
    resume = ensure_resume_for_profile(db, p, req)
    assert resume.requirement_id == req.id
    assert resume.candidate_id == cand.id
    assert resume.resume_file_url == cand.cv_url
    assert resume.candidate_name == "Praveen"
    assert resume.applicant_experience in ("4", "4.0")  # Numeric(4,1) on SQLite vs PG

    # Idempotent: a second call reuses the row, never doubles the applicant.
    assert ensure_resume_for_profile(db, p, req).id == resume.id
    # And the row is no longer profile-only in the list.
    assert _rows(db, req) == []


def test_stage_pills_split_the_sourcing_stage(db, monkeypatch):
    """"Sourcing" / "Technical Screening" / "Technical Interview" are DERIVED
    stages that all live in the Sourcing pipeline stage (28 Sep 2026) — the
    pill must narrow the resume rows AND the profile-only rows to the right
    phase, and every row carries the budget check."""
    from crm_deps import PageParams
    from fastapi import HTTPException
    import routers.crm.resumes as resumes_router

    monkeypatch.setattr(resumes_router, "enrich_resumes_with_ai",
                        lambda _db, items: [{"id": r.id, "candidate_id": r.candidate_id,
                                             "candidate_name": r.candidate_name}
                                            for r in items])
    req = _req(db)
    req.budget_ctc_max = 1_000_000
    _applicant(db, req, "WithTa", screening=None, with_resume=True)
    rich = _applicant(db, req, "WithRmg", screening="Pending", with_resume=True)
    rich.expected_ctc = 1_500_000
    _applicant(db, req, "ShortCv", screening="Shortlisted", with_resume=True)
    _applicant(db, req, "ShortNoCv", screening="Shortlisted")
    db.commit()

    def rows(phase):
        res = resumes_router.list_resumes(
            req.id, phase=phase, ats_status=None, ai_interview_status=None,
            applied_by=None, applied_from=None, applied_to=None, dismissed=False,
            p=PageParams(page=1, limit=20, search=None, sort_by=None, sort_dir="desc"),
            db=db, user=SimpleNamespace(id=1))
        return res["data"]

    def names(phase):
        return sorted(r["candidate_name"] for r in rows(phase))

    assert names("sourcing") == ["WithTa"]
    assert names("technical_screening") == ["WithRmg"]
    assert names("technical_interview") == ["ShortCv", "ShortNoCv"]
    everyone = rows(None)
    assert len(everyone) == 4
    over = {r["candidate_name"]: r["over_budget"] for r in everyone}
    assert over == {"WithTa": False, "WithRmg": True, "ShortCv": False, "ShortNoCv": False}
    stage = {r["candidate_name"]: r["profile_status"]["stage"]["label"] for r in everyone}
    assert stage["WithRmg"] == "Technical Screening"
    with pytest.raises(HTTPException):
        names("bogus")


def _list(db, req, **kw):
    """Call the handler the way FastAPI does, with every filter defaulted."""
    from crm_deps import PageParams
    import routers.crm.resumes as resumes_router

    kw = {"phase": None, "status_key": None, "bucket": "live", "ats_status": None,
          "ai_interview_status": None, "applied_by": None, "applied_from": None,
          "applied_to": None, "dismissed": False, **kw}
    return resumes_router.list_resumes(
        req.id, p=PageParams(page=1, limit=20, search=None, sort_by=None, sort_dir="desc"),
        db=db, user=SimpleNamespace(id=1), **kw)


def test_rejected_candidates_are_archived_only_by_hand(db, monkeypatch):
    """Archive is MANUAL (30 Sep 2026, user rule): a rejected / withdrawn
    candidate stays on the live list — its row offering RMG / GM an Archive
    button (`archivable`) — until someone archives it; Restore brings it back.
    Since 5 Oct 2026 a live candidacy may be archived too. A legacy resume with no profile
    stays live. The chips count each bucket apart."""
    from fastapi import HTTPException
    import routers.crm.resumes as resumes_router
    from services.candidate_profiles import set_applied_archive

    monkeypatch.setattr(resumes_router, "enrich_resumes_with_ai",
                        lambda _db, items: [{"id": r.id, "candidate_id": r.candidate_id,
                                             "candidate_name": r.candidate_name}
                                            for r in items])
    req = _req(db)
    live_p = _applicant(db, req, "Live", screening="Pending", with_resume=True)
    gone = _applicant(db, req, "Gone", screening="Rejected", with_resume=True)
    gone.pipeline_status = PipelineStatus.RMG_REJECTED
    left = _applicant(db, req, "Left", screening="Shortlisted")
    left.pipeline_status = PipelineStatus.SELF_WITHDRAWN
    db.add(Resume(requirement_id=req.id, candidate_id=None, candidate_name="Legacy",
                  email="legacy@example.com", resume_file_url="/x/l.pdf"))
    db.commit()
    rmg = SimpleNamespace(id=1, full_name="Ravi RMG")

    live = _list(db, req)
    assert sorted(r["candidate_name"] for r in live["data"]) == ["Gone", "Left", "Legacy", "Live"]
    flags = {r["candidate_name"]: r["archivable"] for r in live["data"]}
    # 5 Oct 2026: ANY candidacy may be archived by hand (Sales / RMG / GM).
    assert flags == {"Gone": True, "Legacy": False, "Left": True, "Live": True}
    assert _list(db, req, bucket="archive")["data"] == []

    assert set_applied_archive(db, live_p, True, rmg) is True
    assert set_applied_archive(db, live_p, False, rmg) is True
    db.commit()
    assert set_applied_archive(db, gone, True, rmg) is True
    assert set_applied_archive(db, gone, True, rmg) is False   # idempotent
    db.commit()

    live = _list(db, req)
    assert sorted(r["candidate_name"] for r in live["data"]) == ["Left", "Legacy", "Live"]
    archive = _list(db, req, bucket="archive")
    assert [r["candidate_name"] for r in archive["data"]] == ["Gone"]
    assert archive["data"][0]["archived"] is True and archive["data"][0]["archivable"] is False
    assert archive["data"][0]["archive_reason"] == "manual"
    counts = live["meta"]["status_counts"]
    assert counts["live_total"] == 2 and counts["archive_total"] == 1
    assert counts["archive"] == {"rmg_rejected": 1}
    # The stage chips (1 Oct 2026) read per bucket too: the live list holds one
    # screening candidate and one closed (withdrawn) one; the archive one closed.
    assert counts["phases"]["live"] == {"technical_screening": 1, "closed": 1}
    assert counts["phases"]["archive"] == {"closed": 1}

    assert set_applied_archive(db, gone, False, rmg) is True
    db.commit()
    assert _list(db, req, bucket="archive")["data"] == []
    with pytest.raises(HTTPException):
        _list(db, req, bucket="bin")


def test_a_profile_with_no_archive_row_stays_on_the_live_list(db):
    """6 Oct 2026, production: Candidate Profiles read 0 after the deploy. The live
    list is NOT(archive_clause()), and with no archive row the latest-action
    subquery is NULL — NOT(NULL) is NULL, which dropped every profile. Pinned."""
    from sqlalchemy import not_, select
    from services.candidate_status import archive_clause

    req = _req(db)
    plain = _applicant(db, req, "Plain", screening=None)
    db.commit()
    live = db.execute(select(CandidateProfile.id).where(not_(archive_clause()))).scalars().all()
    archived = db.execute(select(CandidateProfile.id).where(archive_clause())).scalars().all()
    assert live == [plain.id] and archived == []


def test_a_held_deal_parks_its_live_candidates_in_archive(db, monkeypatch):
    """5 Oct 2026: while the opportunity is on Customer / Sales Hold its live
    candidacies sit in Archive (reason "hold") and cannot be restored by hand;
    reactivating the deal brings them back. A Joined candidate stays put."""
    from fastapi import HTTPException
    import routers.crm.resumes as resumes_router
    from models import PipelineStage
    from services.candidate_profiles import set_applied_archive

    monkeypatch.setattr(resumes_router, "enrich_resumes_with_ai",
                        lambda _db, items: [{"id": r.id, "candidate_id": r.candidate_id,
                                             "candidate_name": r.candidate_name}
                                            for r in items])
    req = _req(db)
    live_p = _applicant(db, req, "Live", screening="Pending", with_resume=True)
    joined = _applicant(db, req, "Joined", screening="Shortlisted", with_resume=True)
    joined.pipeline_status = PipelineStatus.JOINED
    opp = db.get(Opportunity, req.opportunity_id)
    opp.pipeline_stage = PipelineStage.ON_HOLD
    db.commit()

    assert sorted(r["candidate_name"] for r in _list(db, req)["data"]) == ["Joined"]
    archive = _list(db, req, bucket="archive")["data"]
    assert [(r["candidate_name"], r["archive_reason"]) for r in archive] == [("Live", "hold")]
    with pytest.raises(HTTPException) as err:
        set_applied_archive(db, live_p, False, SimpleNamespace(id=1, full_name="S"))
    assert err.value.status_code == 409

    opp.pipeline_stage = PipelineStage.ACTIVE
    db.commit()
    assert sorted(r["candidate_name"] for r in _list(db, req)["data"]) == ["Joined", "Live"]


def test_status_chips_narrow_both_kinds_of_row_and_rows_say_how_long_they_wait(db, monkeypatch):
    """`status_key` filters resume rows AND profile-only rows by the DERIVED
    status (the chip strip is status-based now, the Stage column hidden), and
    every row carries `waiting_days` — days since the last thing that
    happened to the candidacy, else since they applied."""
    from datetime import datetime, timedelta
    import routers.crm.resumes as resumes_router
    from models import CandidateProfileActivityLog

    monkeypatch.setattr(resumes_router, "enrich_resumes_with_ai",
                        lambda _db, items: [{"id": r.id, "candidate_id": r.candidate_id,
                                             "candidate_name": r.candidate_name,
                                             "created_at": r.created_at.isoformat()}
                                            for r in items])
    req = _req(db)
    fresh = _applicant(db, req, "Fresh", screening=None, with_resume=True)
    pending = _applicant(db, req, "Pending", screening="Pending")
    db.add(CandidateProfileActivityLog(profile_id=pending.id, user_id=1, action_type="SENT_FOR_SCREENING",
                                       comment="sent", timestamp=datetime.utcnow() - timedelta(days=4)))
    db.commit()

    rows = _list(db, req, status_key="technical_screening")["data"]
    assert [r["candidate_name"] for r in rows] == ["Pending"]
    assert rows[0]["waiting_days"] == 4
    rows = _list(db, req, status_key="sourcing")["data"]
    assert [r["candidate_name"] for r in rows] == ["Fresh"]
    assert rows[0]["waiting_days"] == 0 and rows[0]["waiting_since"]
    assert fresh.id  # the resume-backed row was matched through its profile
