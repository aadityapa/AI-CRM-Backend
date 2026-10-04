"""Upload-resume form, 29 Sep 2026: Current location + Note.

Pinned: the current location fills an EMPTY candidate city (never overwrites
one), the note is logged on the profile's Activity Log (nothing when blank),
and the upload route accepts both fields and keeps them on the resume row.

Run:  cd backend && python -m pytest tests/test_upload_form_location_note.py -q
"""
from __future__ import annotations

import importlib
from pathlib import Path

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
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity, OppType,
    PipelineStatus as PS, Resume,
)
from services import slot_booking as sb  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    monkeypatch.setattr(sb, "apply_cv_profile_to_candidate", lambda *a, **k: None)
    try:
        yield s
    finally:
        s.close()


def test_current_location_fills_an_empty_city_only(db):
    fresh = Resume(candidate_name="Asha Rao", email="asha@mail.com",
                   application_details={"current_location": "Pune"})
    cand = sb.find_or_create_candidate_from_resume(db, fresh)
    assert cand.city == "Pune"
    # a known candidate keeps the city already on record
    again = Resume(candidate_name="Asha Rao", email="asha@mail.com",
                   application_details={"current_location": "Mumbai"})
    assert sb.find_or_create_candidate_from_resume(db, again).city == "Pune"


def test_the_note_is_logged_on_the_profile_and_blank_writes_nothing(db):
    cust = Customer(name="C1"); db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-1", title="Embedded", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name="Asha", email="a@mail.com")
    db.add_all([opp, cand]); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=PS.SOURCING)
    db.add(p); db.flush()
    assert sb.record_upload_note(db, p, "  Strong C, can join in 15 days  ", 1) is True
    assert sb.record_upload_note(db, p, "   ", 1) is False
    assert sb.record_upload_note(db, None, "x", 1) is False
    rows = db.execute(select(CandidateProfileActivityLog).where(
        CandidateProfileActivityLog.profile_id == p.id)).scalars().all()
    assert [(r.action_type, r.comment) for r in rows] == [
        (sb.UPLOAD_NOTE_ACTION, "TA note on upload: Strong C, can join in 15 days")]


def test_the_upload_route_takes_both_fields():
    src = (BACKEND / "routers" / "crm" / "resumes.py").read_text(encoding="utf-8")
    body = src.split('@router.post("/api/requirements/{requirement_id}/resumes")', 1)[1].split("@router.", 1)[0]
    assert 'current_location: str = Form("", max_length=120)' in body
    assert 'note: str = Form("", max_length=1000)' in body
    assert '"current_location": (current_location or "").strip() or None' in body
    assert '"note": (note or "").strip() or None' in body
    assert "record_upload_note(db, profile," in body


def test_the_edit_applicant_dialog_can_change_the_current_location():
    """30 Sep 2026: the redesigned Edit applicant dialog edits the same
    `current_location` detail the upload form writes."""
    from routers.crm.resumes import ResumeUpdateIn, _DETAIL_KEYS
    assert "current_location" in _DETAIL_KEYS
    assert ResumeUpdateIn(current_location="Pune").current_location == "Pune"


def test_preferred_location_reaches_the_candidate_record(db):
    """1 Oct 2026: TA typed the Preferred location on the upload form and the
    profile still said "missing" — it stayed on the resume. An upload fills a
    BLANK candidate field; a later CV never overwrites one on record."""
    fresh = Resume(candidate_name="Ravi K", email="ravi@mail.com",
                   application_details={"current_location": "Pune",
                                        "preferred_location": "Chennai, Pune"})
    cand = sb.find_or_create_candidate_from_resume(db, fresh)
    assert (cand.city, cand.preferred_locations) == ("Pune", "Chennai, Pune")
    again = Resume(candidate_name="Ravi K", email="ravi@mail.com",
                   application_details={"preferred_location": "Bangalore"})
    assert sb.find_or_create_candidate_from_resume(db, again).preferred_locations == "Chennai, Pune"
    assert sb.missing_locations(cand) == []


def test_an_edit_overwrites_only_the_locations_ta_changed():
    from types import SimpleNamespace
    cand = SimpleNamespace(city="Pune", preferred_locations="Chennai")
    written = sb.copy_locations_to_candidate(
        cand, {"current_location": "Mumbai", "preferred_location": "Hyderabad"},
        overwrite=True, keys=("preferred_location",))
    assert written == ["preferred_locations"]
    assert (cand.city, cand.preferred_locations) == ("Pune", "Hyderabad")
    assert sb.copy_locations_to_candidate(cand, {"preferred_location": ""}, overwrite=True) == []


def test_the_edit_route_copies_locations_to_the_candidate():
    src = (BACKEND / "routers" / "crm" / "resumes.py").read_text(encoding="utf-8")
    body = src.split('@router.put("/api/resumes/{resume_id}")', 1)[1].split("@router.", 1)[0]
    assert "copy_locations_to_candidate(" in body and "overwrite=True" in body
