"""`PATCH /api/requirements/{id}/jd-skills` (15 Sep 2026).

RMG / Sales / Sales Head / Admin can fill a forgotten JD or skill list at any
open status; the full PUT stays creator-only and Draft/Rejected-only. The
handler is called directly with an in-memory SQLite session — the gate itself
is exercised by the access-template suites.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import HTTPException
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
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Customer, OppType, Opportunity, Requirement, RequirementActivityLog, RequirementSkill,
    RequirementStatus, Skill,
)
from crm_deps import CurrentUser  # noqa: E402
from routers.crm.requirements import JdSkillsIn, set_requirement_jd_skills  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _req(db, status=RequirementStatus.IN_PROGRESS):
    cust = Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-1", title="Test Engineer", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id,
                      title="Test Engineer", no_of_positions=2, status=status, created_by=1)
    db.add(req); db.flush()
    for name in ("Python", "CANoe"):
        db.add(Skill(name=name))
    db.flush()
    return req


def _rmg():
    # NOT the creator (created_by=1) — the PUT would 403 this user.
    return CurrentUser(id=2, username="rmg", full_name="RMG User", roles={"RMG"})


def test_rmg_fills_jd_and_skills_on_an_in_progress_requirement(db):
    req = _req(db)
    ids = [s.id for s in db.query(Skill).order_by(Skill.name).all()]
    out = set_requirement_jd_skills(
        req.id,
        JdSkillsIn(rmg_jd_text="  Automotive test engineer JD  ",
                   skills=[{"skill_id": ids[0], "is_mandatory": True, "min_rating": 3},
                           {"skill_id": ids[1]}]),
        db=db, user=_rmg(),
    )
    assert out["success"] is True
    db.refresh(req)
    assert req.rmg_jd_text == "Automotive test engineer JD"
    rows = db.query(RequirementSkill).filter_by(requirement_id=req.id).all()
    assert {(r.skill_id, r.is_mandatory) for r in rows} == {(ids[0], True), (ids[1], False)}
    assert req.status == RequirementStatus.IN_PROGRESS  # status never moves here
    log = db.query(RequirementActivityLog).filter_by(requirement_id=req.id).one()
    assert log.action_type == "JD_SKILLS" and "skills (2)" in log.comment


def test_unset_parts_are_left_alone(db):
    req = _req(db)
    req.rmg_jd_text = "keep me"
    db.flush()
    set_requirement_jd_skills(req.id, JdSkillsIn(description="New description"), db=db, user=_rmg())
    db.refresh(req)
    assert req.rmg_jd_text == "keep me"
    assert req.description == "New description"


def test_empty_payload_is_a_400(db):
    req = _req(db)
    with pytest.raises(HTTPException) as e:
        set_requirement_jd_skills(req.id, JdSkillsIn(), db=db, user=_rmg())
    assert e.value.status_code == 400


def test_unknown_skill_is_a_400(db):
    req = _req(db)
    with pytest.raises(HTTPException) as e:
        set_requirement_jd_skills(req.id, JdSkillsIn(skills=[{"skill_id": 999}]), db=db, user=_rmg())
    assert e.value.status_code == 400


@pytest.mark.parametrize("status", [RequirementStatus.CLOSED, RequirementStatus.CANCELLED,
                                    RequirementStatus.FULFILLED])
def test_terminal_requirements_are_frozen(db, status):
    req = _req(db, status=status)
    with pytest.raises(HTTPException) as e:
        set_requirement_jd_skills(req.id, JdSkillsIn(rmg_jd_text="x"), db=db, user=_rmg())
    assert e.value.status_code == 400


def test_ta_may_write_the_jd_and_the_three_jd_routes_share_one_role_list():
    """30 Sep 2026, user report: a TA opened "Edit JD & skills" but the JD-file
    upload answered 403 (the attachment route said RMG alone). The PATCH, the
    upload and the delete now read ONE tuple, and TA is on it."""
    import inspect

    import routers.crm.requirements as rr

    assert "TA" in rr.JD_EDIT_ROLES and "RMG" in rr.JD_EDIT_ROLES
    for fn in (rr.set_requirement_jd_skills, rr.add_requirement_attachment,
               rr.delete_requirement_attachment):
        assert 'gated_write("requirements", *JD_EDIT_ROLES)' in inspect.getsource(fn), fn.__name__


def test_an_uploaded_jd_file_fills_a_blank_jd_text_and_is_returned_for_review(db, monkeypatch):
    """30 Sep 2026, user ask: "Edit JD & skills" takes the JD as a PDF / Word file.
    The file's text is read out — a blank `rmg_jd_text` is filled from it (the ATS
    and the AI interview read the TEXT), a written one is left alone — and the text
    comes back so the dialog can show it. An unreadable file keeps the attachment."""
    import io

    from fastapi import UploadFile

    import routers.crm.requirements as rr

    req = _req(db)
    monkeypatch.setattr(rr, "save_upload_hashed", lambda f, folder: ("/api/crm-files/requirement_attachments/x.pdf", "sha", 10))
    monkeypatch.setattr("services.resumes.extract_resume_text", lambda url: "  Senior AUTOSAR engineer, 5+ yrs, CAN, Ethernet.  ")

    def upload(name="jd.pdf"):
        return UploadFile(filename=name, file=io.BytesIO(b"%PDF-1.4 fake"))

    out = rr.add_requirement_attachment(req.id, file=upload(), kind="rmg_jd", db=db, user=_rmg())
    assert out["data"]["jd_text_filled"] is True
    assert out["data"]["extracted_text"] == "Senior AUTOSAR engineer, 5+ yrs, CAN, Ethernet."
    assert db.get(Requirement, req.id).rmg_jd_text == "Senior AUTOSAR engineer, 5+ yrs, CAN, Ethernet."
    assert "saved" in out["message"]

    # A second file never overwrites the JD text someone wrote / already has.
    monkeypatch.setattr("services.resumes.extract_resume_text", lambda url: "Another JD")
    out2 = rr.add_requirement_attachment(req.id, file=upload("jd2.docx"), kind="rmg_jd", db=db, user=_rmg())
    assert out2["data"]["jd_text_filled"] is False and out2["data"]["extracted_text"] == "Another JD"
    assert db.get(Requirement, req.id).rmg_jd_text.startswith("Senior AUTOSAR")

    # Unreadable (a scan): the attachment stays, nothing is filled, no error.
    def boom(url):
        raise RuntimeError("no text layer")
    monkeypatch.setattr("services.resumes.extract_resume_text", boom)
    out3 = rr.add_requirement_attachment(req.id, file=upload("scan.pdf"), kind="rmg_jd", db=db, user=_rmg())
    assert out3["data"]["extracted_text"] is None and out3["data"]["id"]
