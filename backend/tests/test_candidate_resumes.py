"""Resume library, matching positions and multi-apply (9 Oct 2026).

Pinned: the library seeds itself from what is on file (the record's CV is the
main version, every uploaded resume joins it); the main version IS `cv_url`;
the main version cannot be removed; every live position is scored against
every version with the deterministic ATS and >= 60 reads "Good fit"; positions
the candidate is already in are flagged and sorted last; multi-apply applies
each position through the Apply button's own path with the version TA chose,
and one refusal never blocks the others; only TA may multi-apply.

Run:  cd backend && python -m pytest tests/test_candidate_resumes.py -q
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
    "candidate_resumes",
]:
    importlib.import_module(f"models.{_m}")

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, Opportunity, OppType, PipelineStage, Requirement,
    RequirementSkill, RequirementStatus, Resume, Skill,
)
from models.candidate_resumes import CandidateResume  # noqa: E402
from services import candidate_resumes as lib  # noqa: E402

TA = CurrentUser(id=1, username="ta", full_name="Gargee Joshi", roles={"TA"})
SALES = CurrentUser(id=2, username="s", full_name="Sanjana", roles={"Sales"})

BLE_CV = ("Varshini M — Bluetooth Developer. Email varshini@mail.com Phone 9999999999. "
          "Experience: 5 years building BLE stacks, GATT servers, Zephyr RTOS, embedded C. "
          "Education: B.E Electronics. Projects: BLE audio, nRF52, C programming.")
ADAS_CV = ("Varshini M — Embedded Engineer. Email varshini@mail.com Phone 9999999999. "
           "Experience: 5 years on ADAS camera pipelines, AUTOSAR, C++. Education: B.E.")


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    texts = {"/api/crm-files/cv/ble.pdf": BLE_CV, "/api/crm-files/resumes/adas.pdf": ADAS_CV}
    monkeypatch.setattr(lib, "version_text", lambda _db, v: texts.get(v.file_url))
    monkeypatch.setattr(lib, "_jd_for", lambda _db, req: (None, None))
    try:
        yield s
    finally:
        s.close()


def _skill(db, name):
    sk = db.execute(select(Skill).where(Skill.name == name)).scalars().first()
    if sk is None:
        sk = Skill(name=name)
        db.add(sk)
        db.flush()
    return sk


def _position(db, title, skills, *, status=RequirementStatus.OPEN_FOR_SOURCING, stage=PipelineStage.NEW):
    n = len(db.execute(select(Customer.id)).all()) + 1
    cust = Customer(name=f"Customer {n}")
    db.add(cust)
    db.flush()
    opp = Opportunity(opp_id=f"C-2026-{n:05d}", title=title, customer_id=cust.id, opp_type=OppType.T_AND_M,
                      created_by=1, pipeline_stage=stage)
    db.add(opp)
    db.flush()
    req = Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id, title=title,
                      status=status, created_by=1, experience_min=3, experience_max=8)
    db.add(req)
    db.flush()
    for name in skills:
        db.add(RequirementSkill(requirement_id=req.id, skill_id=_skill(db, name).id, is_mandatory=True))
    db.flush()
    return req


def _candidate(db):
    cand = Candidate(first_name="Varshini", last_name="M", email="varshini@mail.com", phone="9999999999",
                     experience_years=5, cv_url="/api/crm-files/cv/ble.pdf")
    db.add(cand)
    db.flush()
    return cand


def test_the_library_seeds_itself_and_the_main_version_is_the_cv(db):
    cand = _candidate(db)
    req = _position(db, "AGM ADAS", ["ADAS"])
    db.add(Resume(requirement_id=req.id, candidate_id=cand.id, candidate_name="Varshini",
                  resume_file_url="/api/crm-files/resumes/adas.pdf"))
    db.flush()
    rows = lib.sync_library(db, cand)
    assert [r.file_url for r in rows] == ["/api/crm-files/cv/ble.pdf", "/api/crm-files/resumes/adas.pdf"]
    assert [r.is_primary for r in rows] == [True, False]
    assert rows[1].label == f"Uploaded for {req.req_number}"
    assert len(lib.sync_library(db, cand)) == 2, "idempotent"
    lib.set_primary(db, cand, rows[1])
    assert cand.cv_url == "/api/crm-files/resumes/adas.pdf"
    with pytest.raises(HTTPException) as e:
        lib.delete_version(db, cand, rows[1])
    assert e.value.status_code == 409
    lib.delete_version(db, cand, rows[0])
    assert db.execute(select(CandidateResume).where(CandidateResume.candidate_id == cand.id)).scalars().all() == [rows[1]]


def test_every_live_position_is_scored_and_the_best_version_named(db):
    cand = _candidate(db)
    ble = _position(db, "Bluetooth Developer", ["BLE", "GATT", "Zephyr"])
    adas = _position(db, "AGM ADAS", ["ADAS", "AUTOSAR"])
    _position(db, "Closed one", ["BLE"], status=RequirementStatus.CLOSED)
    _position(db, "Held deal", ["BLE"], stage=PipelineStage.ON_HOLD)
    db.add(Resume(requirement_id=adas.id, candidate_id=cand.id, candidate_name="Varshini",
                  resume_file_url="/api/crm-files/resumes/adas.pdf"))
    db.flush()
    out = lib.matching_positions(db, cand)
    by_req = {r["requirement_id"]: r for r in out["positions"]}
    assert set(by_req) == {ble.id, adas.id}, "only positions TA can apply to today"
    assert by_req[ble.id]["good_fit"] and by_req[ble.id]["best"]["label"] == "CV on record"
    assert by_req[adas.id]["best"]["label"].startswith("Uploaded for")
    assert len(by_req[ble.id]["scores"]) == 2
    assert out["good_fit_pct"] == lib.GOOD_FIT_PCT


def test_positions_already_applied_are_flagged_and_sorted_last(db):
    cand = _candidate(db)
    ble = _position(db, "Bluetooth Developer", ["BLE", "GATT", "Zephyr"])
    other = _position(db, "BLE Lead", ["BLE", "GATT"])
    db.add(CandidateProfile(candidate_id=cand.id, opportunity_id=ble.opportunity_id, ta_owner_name="Gargee"))
    db.flush()
    rows = lib.matching_positions(db, cand)["positions"]
    assert rows[-1]["requirement_id"] == ble.id and rows[-1]["applied"]["applied_by"] == "Gargee"
    assert rows[0]["requirement_id"] == other.id and rows[0]["applied"] is None


def test_multi_apply_uses_the_chosen_version_and_never_blocks_on_one_refusal(db, monkeypatch):
    import routers.crm.candidate_profiles as cpr
    import services.resumes as sr
    import services.slot_booking as sb
    from routers.crm import candidate_resumes as router
    monkeypatch.setattr(sr, "auto_score_profile", lambda *a, **k: False)
    monkeypatch.setattr(sb, "remind_missing_location", lambda *a, **k: [])
    monkeypatch.setattr(cpr, "rmg_gate_enabled", lambda: False, raising=False)
    cand = _candidate(db)
    ble = _position(db, "Bluetooth Developer", ["BLE"])
    lead = _position(db, "BLE Lead", ["BLE"])
    db.add(CandidateProfile(candidate_id=cand.id, opportunity_id=lead.opportunity_id, ta_owner_name="Mohammed"))
    db.flush()
    versions = lib.sync_library(db, cand)
    body = router.MultiApplyIn(items=[
        router.ApplyItem(requirement_id=ble.id, resume_id=versions[0].id),
        router.ApplyItem(requirement_id=lead.id),
        router.ApplyItem(requirement_id=99999),
    ])
    out = router.multi_apply(cand.id, body, db=db, user=TA)
    res = {r["requirement_id"]: r for r in out["data"]}
    assert res[ble.id]["ok"] and res[ble.id]["resume"] == "CV on record"
    assert not res[lead.id]["ok"] and "already applied" in res[lead.id]["message"]
    assert not res[99999]["ok"]
    row = db.execute(select(Resume).where(Resume.requirement_id == ble.id)).scalars().one()
    assert row.resume_file_url == versions[0].file_url
    assert row.application_details["resume_version"] == "CV on record"
    assert out["message"] == "Applied to 1 of 3 positions"


def test_only_ta_may_multi_apply_or_ask_the_ai():
    from routers.crm import candidate_resumes as router
    assert router.ta_gate(user=TA) is TA
    with pytest.raises(HTTPException) as e:
        router.ta_gate(user=SALES)
    assert e.value.status_code == 403


def test_the_applied_candidates_hint_counts_other_live_positions(db):
    cand = _candidate(db)
    a = _position(db, "A", ["BLE"])
    b = _position(db, "B", ["BLE"])
    c = _position(db, "C", ["BLE"], stage=PipelineStage.CLOSED_LOST)
    for req in (a, b, c):
        db.add(CandidateProfile(candidate_id=cand.id, opportunity_id=req.opportunity_id))
    db.flush()
    assert lib.other_open_fits(db, [cand.id]) == {cand.id: 2}
    lib.sync_library(db, cand)
    assert lib.library_counts(db, [cand.id]) == {cand.id: 1}
