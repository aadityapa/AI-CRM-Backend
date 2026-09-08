"""Suggested candidates for an opportunity (services/candidate_match.py).

Run:  cd backend && python -m pytest tests/test_candidate_match.py -q
"""
from __future__ import annotations

import importlib
from decimal import Decimal as D

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
    Candidate, CandidateProfile, CandidateSkill, Customer, Opportunity,
    OpportunityCtcSlab, OpportunitySkill, OppType, PipelineStatus, Skill,
)
from services.candidate_match import suggest_candidates  # noqa: E402


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
    cust = Customer(name="Magna")
    other_cust = Customer(name="Bosch")
    db.add_all([cust, other_cust])
    db.flush()
    java = Skill(name="Java")
    sel = Skill(name="Selenium")
    autosar = Skill(name="AUTOSAR")
    db.add_all([java, sel, autosar])
    db.flush()

    opp = Opportunity(opp_id="OPP-1", title="Automation Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    old_opp = Opportunity(opp_id="OPP-0", title="Old Position",
                          customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    other_opp = Opportunity(opp_id="OPP-X", title="Elsewhere",
                            customer_id=other_cust.id, opp_type=OppType.T_AND_M,
                            created_by=1)
    db.add_all([opp, old_opp, other_opp])
    db.flush()
    db.add_all([
        OpportunitySkill(opportunity_id=opp.id, skill_id=java.id, is_mandatory=True),
        OpportunitySkill(opportunity_id=opp.id, skill_id=sel.id, is_mandatory=True),
        OpportunitySkill(opportunity_id=opp.id, skill_id=autosar.id, is_mandatory=False),
        OpportunityCtcSlab(opportunity_id=opp.id, exp_min=D("3"), exp_max=D("3.5"),
                           target_exp=D("4")),
    ])

    def cand(name, email, exp, skills, cv=True, phone="9"):
        c = Candidate(first_name=name, email=email,
                      experience_years=D(str(exp)) if exp is not None else None,
                      cv_url="cv.pdf" if cv else None, phone=phone)
        db.add(c)
        db.flush()
        for sk in skills:
            db.add(CandidateSkill(candidate_id=c.id, skill_id=sk.id))
        return c

    perfect = cand("Perfect", "p@x.in", 3.5, [java, sel, autosar])
    partial = cand("Partial", "q@x.in", 3, [java])          # missing Selenium
    stale = cand("NoSkills", "r@x.in", 10, [])              # nothing relevant
    joined = cand("Engaged", "s@x.in", 3.2, [java, sel])
    applied = cand("Applied", "t@x.in", 3.5, [java, sel, autosar])

    db.add_all([
        # History: Perfect reached Shortlisted on the SAME customer's old opp.
        CandidateProfile(candidate_id=perfect.id, opportunity_id=old_opp.id,
                         pipeline_status=PipelineStatus.SHORTLISTED),
        # Engaged: currently Joined elsewhere.
        CandidateProfile(candidate_id=joined.id, opportunity_id=other_opp.id,
                         pipeline_status=PipelineStatus.JOINED),
        # Already applied to THIS opportunity → excluded.
        CandidateProfile(candidate_id=applied.id, opportunity_id=opp.id,
                         pipeline_status=PipelineStatus.SOURCING),
    ])
    db.commit()
    return opp, perfect, partial, stale, joined, applied


def test_scoring_orders_and_explains(db):
    opp, perfect, partial, stale, joined, applied = _seed(db)
    out = suggest_candidates(db, opp)
    ids = [r["candidate_id"] for r in out]

    assert applied.id not in ids, "already applied → excluded"
    assert stale.id not in ids, "no signal at all → not suggested"
    assert ids[0] == perfect.id, "full skills + band + history wins"

    top = out[0]
    # 35 mand + 15 opt + 20 exp + 10 late-stage + 10 same-customer + 5 contact = 95
    assert top["score"] == pytest.approx(95.0, abs=0.2)
    assert top["missing_mandatory_skills"] == []
    assert "Java" in top["matched_skills"] and "AUTOSAR" in top["matched_skills"]
    assert any("late pipeline stage" in r for r in top["reasons"])
    assert any("this customer" in r for r in top["reasons"])
    assert top["engaged"] is False

    part = next(r for r in out if r["candidate_id"] == partial.id)
    assert part["missing_mandatory_skills"] == ["Selenium"]
    assert part["score"] < top["score"]

    eng = next(r for r in out if r["candidate_id"] == joined.id)
    assert eng["engaged"] is True, "Joined elsewhere is flagged, not hidden"


def test_no_skills_falls_back_to_band_and_history(db):
    opp, perfect, *_ = _seed(db)
    # Strip the opportunity's skills: matching falls back to band + history.
    from sqlalchemy import delete
    db.execute(delete(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id))
    db.commit()
    out = suggest_candidates(db, opp)
    assert out, "band/history still yields suggestions"
    assert out[0]["candidate_id"] == perfect.id
    assert out[0]["matched_skills"] == []
