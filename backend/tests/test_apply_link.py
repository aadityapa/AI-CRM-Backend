"""Public application link (routers/crm/apply.py) — 19 Oct 2026 user ask.

Once RMG approves, TA copies a link from the opportunity's Details tab, posts it
on Naukri / LinkedIn, and every application lands in THAT position at Sourcing
with the details the candidate typed; the position's TAs are told.

Run:  cd backend && python -m pytest tests/test_apply_link.py -q
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
    CandidateProfile, Customer, Location, Opportunity, OppType, PipelineStatus,
    Requirement, RequirementStatus, RequirementTaAssignment, Resume,
)
from routers.crm import apply as ap  # noqa: E402


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


class _Req:
    """Enough of a starlette Request for the base-URL resolver."""
    headers: dict = {}
    base_url = "http://orbit.example.com/"


def _seed(db, status=RequirementStatus.OPEN_FOR_SOURCING):
    cust = Customer(name="Aptiv")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-7", title="Embedded HW Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    loc = Location(city="Pune", state="Maharashtra")
    db.add(loc); db.flush()
    req = Requirement(req_number="REQ-7", opportunity_id=opp.id, customer_id=cust.id,
                      title="Embedded HW Engineer", no_of_positions=2, status=status,
                      description="sales notes", rmg_jd_text="Design embedded boards.",
                      experience_min=3, experience_max=6, location_id=loc.id, created_by=1)
    db.add(req); db.flush()
    db.commit()
    return opp, req


@pytest.fixture(autouse=True)
def _no_config(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://careers.karnex.in")
    monkeypatch.setenv("AUTH_SECRET", "t" * 48)
    import services.invite_links as il
    monkeypatch.setattr(il, "configured_base", lambda: "https://careers.karnex.in")


def test_link_carries_post_text_with_rmg_jd_and_facts(db):
    _, req = _seed(db)
    data = ap.get_apply_link(req.id, _Req(), db=db, user=None)["data"]
    assert data["apply_url"].startswith("https://careers.karnex.in/apply/")
    assert ap.parse_apply_token(data["token"]) == req.id
    assert data["is_open"] is True and data["applications"] == 0
    post = data["post_text"]
    assert "Embedded HW Engineer" in post and "3–6 yrs" in post and "Pune" in post
    assert "Design embedded boards." in post and "sales notes" not in post
    assert data["apply_url"] in post


def test_form_shows_rmg_jd_and_asks_current_location(db):
    _, req = _seed(db)
    html = ap.apply_form(ap.make_apply_token(req.id), db=db).body.decode()
    assert "Design embedded boards." in html
    assert 'name="current_location"' in html
    assert "3–6 yrs" in html


def test_link_is_closed_before_rmg_approval(db):
    _, req = _seed(db, status=RequirementStatus.PENDING_ENGINEERING_REVIEW)
    data = ap.get_apply_link(req.id, _Req(), db=db, user=None)["data"]
    assert data["is_open"] is False
    html = ap.apply_form(ap.make_apply_token(req.id), db=db).body.decode()
    assert "currently closed" in html


def _submit(db, monkeypatch, req, sent):
    import io
    from fastapi import UploadFile
    monkeypatch.setattr(ap, "save_upload_hashed", lambda f, d: ("/u/cv.pdf", "abc123", 10))
    import services.notify as n
    monkeypatch.setattr(n, "notify_user", lambda _db, uid, *a, **k: sent.append(("user", uid)))
    monkeypatch.setattr(n, "notify_role", lambda _db, role, *a, **k: sent.append(("role", role)) or 1)
    return ap.submit_application(
        _Req(), ap.make_apply_token(req.id), candidate_name="Asha Rao", email="asha@example.com",
        phone="9876543210", experience="4", education="", technical_domain="", skills="C, RTOS",
        notice_period="30 days", current_ctc="10 LPA", expected_ctc="14 LPA",
        preferred_location="Pune", current_location="Nagpur",
        file=UploadFile(file=io.BytesIO(b"%PDF-1"), filename="cv.pdf"), db=db)


def test_application_lands_at_sourcing_and_assigned_ta_is_told(db, monkeypatch):
    opp, req = _seed(db)
    db.add(RequirementTaAssignment(requirement_id=req.id, user_id=1, user_name="Tara")); db.commit()
    sent: list = []
    _submit(db, monkeypatch, req, sent)
    resume = db.execute(select(Resume).where(Resume.requirement_id == req.id)).scalars().one()
    assert resume.source_portal == "Apply Link"
    assert resume.application_details["current_location"] == "Nagpur"
    prof = db.execute(select(CandidateProfile).where(
        CandidateProfile.opportunity_id == opp.id)).scalars().one()
    assert prof.pipeline_status == PipelineStatus.SOURCING and prof.source == "apply_link"
    assert sent == [("user", 1)]


def test_no_assigned_ta_tells_every_ta(db, monkeypatch):
    _, req = _seed(db)
    sent: list = []
    _submit(db, monkeypatch, req, sent)
    assert sent == [("role", "TA")]


def test_base_reach_flags_an_office_network_address():
    from services.invite_links import base_reach
    assert base_reach("https://192.168.1.87:2020") == "private"
    assert base_reach("http://localhost:2020") == "private"
    assert base_reach("http://10.0.0.5") == "private"
    assert base_reach("https://karnexgroup.com") == "public"
    assert base_reach("") == "unknown"
