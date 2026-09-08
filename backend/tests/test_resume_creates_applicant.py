"""Every resume is an application: ensure_sourcing_profile (services/slot_booking.py).

Pins the 20 Aug 2026 decision that a resume landing on a requirement — TA upload
or public apply link — creates a CandidateProfile at Sourcing, so the person
shows up in the opportunity's Applicants tab immediately instead of only once an
AI interview is scheduled.

Run:  cd backend && python -m pytest tests/test_resume_creates_applicant.py -q
"""
from __future__ import annotations

import importlib

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
    Candidate, CandidateProfile, Customer, Opportunity, OppType, PipelineStatus,
    Requirement, RequirementStatus, Resume,
)
from services.slot_booking import ensure_sourcing_profile  # noqa: E402


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


def _seed(db):
    cust = Customer(name="Aptiv")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-1", title="Embedded HW Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id,
                      title="Embedded HW Engineer", no_of_positions=3,
                      status=RequirementStatus.IN_PROGRESS, created_by=1)
    db.add(req); db.flush()
    cand = Candidate(first_name="Sivakumar", last_name="Sayeeram",
                     email="siva8848@gmail.com")
    db.add(cand); db.flush()
    resume = Resume(requirement_id=req.id, candidate_id=cand.id,
                    candidate_name="Sivakumar Sayeeram", email="siva8848@gmail.com",
                    resume_file_url="/x/cv.pdf")
    db.add(resume); db.flush()
    return opp, req, cand, resume


class _User:
    id = 1
    full_name = "Gargee Joshi"
    username = "gargee"


def _profile_for(db, cand, opp):
    return db.execute(select(CandidateProfile).where(
        CandidateProfile.candidate_id == cand.id,
        CandidateProfile.opportunity_id == opp.id,
    )).scalars().first()


def test_a_resume_creates_a_sourcing_profile(db):
    opp, req, cand, resume = _seed(db)
    p = ensure_sourcing_profile(db, resume, req, ta_user=_User())
    assert p is not None
    assert p.pipeline_status == PipelineStatus.SOURCING
    assert p.opportunity_id == opp.id and p.candidate_id == cand.id


def test_the_uploader_is_stamped_as_ta_owner(db):
    _, req, _, resume = _seed(db)
    p = ensure_sourcing_profile(db, resume, req, ta_user=_User())
    assert p.ta_owner_id == 1
    assert p.ta_owner_name == "Gargee Joshi"
    assert p.applied_on is not None
    assert p.source == "ats"


def test_the_public_apply_link_has_no_ta_owner(db):
    _, req, _, resume = _seed(db)
    p = ensure_sourcing_profile(db, resume, req, source="apply_link")
    assert p.ta_owner_id is None
    assert p.source == "apply_link"


def test_an_existing_profile_is_left_completely_alone(db):
    """A re-uploaded resume must never reset someone already mid-pipeline."""
    opp, req, cand, resume = _seed(db)
    existing = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                                pipeline_status=PipelineStatus.CUSTOMER_SCREENING,
                                ta_owner_id=None)
    db.add(existing); db.flush()
    p = ensure_sourcing_profile(db, resume, req, ta_user=_User())
    assert p.id == existing.id
    assert p.pipeline_status == PipelineStatus.CUSTOMER_SCREENING
    assert p.ta_owner_id is None  # not restamped


def test_idempotent_across_repeat_uploads(db):
    opp, req, cand, resume = _seed(db)
    a = ensure_sourcing_profile(db, resume, req, ta_user=_User())
    b = ensure_sourcing_profile(db, resume, req, ta_user=_User())
    assert a.id == b.id
    rows = db.execute(select(CandidateProfile).where(
        CandidateProfile.candidate_id == cand.id,
        CandidateProfile.opportunity_id == opp.id,
    )).scalars().all()
    assert len(rows) == 1


def test_a_resume_with_no_candidate_is_a_noop_not_a_crash(db):
    """Best-effort contract: profile bootstrap must never sink an upload."""
    _, req, _, resume = _seed(db)
    resume.candidate_id = None
    assert ensure_sourcing_profile(db, resume, req, ta_user=_User()) is None
