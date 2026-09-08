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


def test_only_rmg_shortlisted_without_a_resume_appear(db):
    req = _req(db)
    _applicant(db, req, "Vamsi", screening="Shortlisted")            # ✅ belongs
    _applicant(db, req, "Pending", screening="Pending")              # still with RMG
    _applicant(db, req, "Rejected", screening="Rejected")            # screened out
    _applicant(db, req, "HasCv", screening="Shortlisted", with_resume=True)  # already listed

    rows = _rows(db, req)
    assert [r["candidate_name"] for r in rows] == ["Vamsi"]
    row = rows[0]
    assert row["is_profile_only"] is True
    assert row["profile_id"] is not None
    assert row["id"] < 0, "profile-only rows use a negative id so they cannot collide"
    assert row["rmg_screening_status"] == "Shortlisted"
    assert row["ats_score"] is None and row["ats_status"] is None


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


def test_stage_pill_filters_profile_only_rows(db):
    """The pills filter resumes server-side; these rows must answer them too,
    or "Sourcing" would show a candidate who is already at RMG Review."""
    req = _req(db)
    a = _applicant(db, req, "AtSourcing", screening="Shortlisted")
    b = _applicant(db, req, "AtRmg", screening="Shortlisted")
    b.pipeline_status = PipelineStatus.RMG_REVIEW
    db.flush()

    assert [r["candidate_name"] for r in
            _rows(db, req, stages=[PipelineStatus.RMG_REVIEW])] == ["AtRmg"]
    assert [r["candidate_name"] for r in
            _rows(db, req, stages=[a.pipeline_status])] == ["AtSourcing"]


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
