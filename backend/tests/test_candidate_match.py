"""Suggested candidates for an opportunity (services/candidate_match.py).

Run:  cd backend && python -m pytest tests/test_candidate_match.py -q
"""
from __future__ import annotations

import importlib
from decimal import Decimal as D

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
    # Every configured pool is full (no JD on this deal, so no JD pool):
    # 30 mand + 15 opt + 20 exp + (6 late + 6 same customer + 3 any) + 5 = 85/85.
    assert top["score"] == pytest.approx(100.0, abs=0.2)
    assert top["missing_mandatory_skills"] == []
    assert "Java" in top["matched_skills"] and "AUTOSAR" in top["matched_skills"]
    assert any("late pipeline stage" in r for r in top["reasons"])
    assert any("this customer" in r for r in top["reasons"])
    assert {s["area"] for s in top["strengths"]} >= {"Skills", "Experience", "History"}
    assert top["gaps"] == [], "nothing lacking on the perfect candidate"
    assert top["engaged"] is False

    part = next(r for r in out if r["candidate_id"] == partial.id)
    assert part["missing_mandatory_skills"] == ["Selenium"]
    assert any(g["area"] == "Skills" and "Selenium" in g["detail"] for g in part["gaps"])
    assert any(g["area"] == "History" for g in part["gaps"]), "no history is a named gap"
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


def _requirement(db, opp, jd, skills):
    """A requirement carrying RMG's JD + Skill Evaluation Details for `opp`."""
    from models import Requirement, RequirementSkill, RequirementStatus
    req = Requirement(req_number=f"REQ-{opp.id}", title=opp.title, opportunity_id=opp.id,
                      status=RequirementStatus.IN_PROGRESS, created_by=1, customer_id=opp.customer_id,
                      rmg_jd_text=jd, no_of_positions=1)
    db.add(req)
    db.flush()
    for sk, mandatory in skills:
        db.add(RequirementSkill(requirement_id=req.id, skill_id=sk.id, is_mandatory=mandatory))
    db.commit()
    return req


def test_the_basis_is_the_rmg_jd_and_the_requirement_skills(db):
    from services.candidate_match import suggestion_basis
    opp, perfect, partial, *_ = _seed(db)
    docker = Skill(name="Docker")
    db.add(docker)
    db.flush()
    # RMG's reviewed list: Java mandatory, Docker optional. AUTOSAR (opportunity
    # optional) stays; Selenium is now what the requirement says: optional.
    sel = db.execute(select(Skill).where(Skill.name == "Selenium")).scalar_one()
    java = db.execute(select(Skill).where(Skill.name == "Java")).scalar_one()
    req = _requirement(db, opp, "Automation engineer: Selenium WebDriver, Jenkins CI, "
                                "Docker containers and Kubernetes. Jenkins pipelines daily.",
                       [(java, True), (sel, False), (docker, False)])
    basis = suggestion_basis(db, opp)
    assert basis["req_number"] == req.req_number
    assert basis["jd_source"] == "rmg_jd"
    assert basis["mandatory_skills"] == ["Java"]
    assert set(basis["optional_skills"]) == {"AUTOSAR", "Docker", "Selenium"}
    assert "Jenkins" in basis["jd_keywords"] and "Kubernetes" in basis["jd_keywords"]
    assert basis["experience_band"] == {"min": 3.0, "max": 4.0}

    out = suggest_candidates(db, opp, basis)
    part = next(r for r in out if r["candidate_id"] == partial.id)
    # Selenium is optional under the requirement, so nothing mandatory is missing.
    assert part["missing_mandatory_skills"] == []
    assert "Selenium" in part["missing_optional_skills"]
    jd_gap = next(g for g in part["gaps"] if g["area"] == "JD fit")
    assert "Jenkins" in jd_gap["detail"]
    assert part["jd_terms_matched"] == []


def test_jd_terms_are_read_from_experience_education_and_scanned_resumes(db):
    from models import Resume
    from models.candidates import CandidateExperience
    from services.candidate_match import suggestion_basis
    opp, perfect, partial, *_ = _seed(db)
    java = db.execute(select(Skill).where(Skill.name == "Java")).scalar_one()
    req = _requirement(db, opp, "Jenkins, Kubernetes and Terraform.",
                       [(java, True)])
    # Partial never typed Kubernetes as a skill — but their job title says so and
    # the ATS found Terraform in a resume they sent for another position.
    db.add(CandidateExperience(candidate_id=partial.id, company_name="Acme",
                               job_title="Kubernetes platform engineer"))
    db.add(Resume(requirement_id=req.id, candidate_id=partial.id, candidate_name="Partial",
                  resume_file_url="x.pdf", ats_score=D("61"),
                  ats_score_breakdown={"matched": ["Java"], "jd_keywords_matched": ["Terraform"]}))
    db.commit()
    out = suggest_candidates(db, opp, suggestion_basis(db, opp))
    part = next(r for r in out if r["candidate_id"] == partial.id)
    assert set(part["jd_terms_matched"]) == {"Kubernetes", "Terraform"}
    assert part["jd_terms_missing"] == ["Jenkins"]
    assert part["skills_from_ats"] is False, "they HAVE evidence; no fallback needed"


def test_no_recorded_skills_falls_back_to_the_latest_ats_score(db):
    from models import Resume
    from services.candidate_match import suggestion_basis
    opp, perfect, partial, stale, *_ = _seed(db)
    java = db.execute(select(Skill).where(Skill.name == "Java")).scalar_one()
    req = _requirement(db, opp, "", [(java, True)])
    # `stale` has no skills at all; the ATS scored their last resume 80 %.
    db.add(Resume(requirement_id=req.id, candidate_id=stale.id, candidate_name="NoSkills",
                  resume_file_url="x.pdf", ats_score=D("80"), ats_score_breakdown={}))
    # Put them in the pool through a (rejected, old) application elsewhere.
    other = db.execute(select(Opportunity).where(Opportunity.opp_id == "OPP-X")).scalar_one()
    db.add(CandidateProfile(candidate_id=stale.id, opportunity_id=other.id,
                            pipeline_status=PipelineStatus.SALES_REJECTED))
    db.commit()
    out = suggest_candidates(db, opp, suggestion_basis(db, opp))
    row = next(r for r in out if r["candidate_id"] == stale.id)
    assert row["skills_from_ats"] is True
    assert any("ATS score (80%)" in g["detail"] for g in row["gaps"])
    assert row["matched_skills"] == []
    # The opportunity's own Selenium (mandatory) + AUTOSAR (optional) still count,
    # so the skill pools are 30 + 15 = 45 and the ATS fallback earns 45 * 0.8 = 36,
    # + 3 history + 5 contact, over 45 + 20 (band) + 15 + 5.
    assert row["score"] == pytest.approx(100 * (36 + 3 + 5) / 85, abs=0.2)
    assert len(row["history"]) == 1 and row["history"][0]["outcome"] == "rejected"


def test_a_recent_rejection_at_this_customer_costs_points_and_is_named(db):
    from datetime import datetime, timedelta, timezone
    opp, perfect, partial, *_ = _seed(db)
    old_opp = db.execute(select(Opportunity).where(Opportunity.opp_id == "OPP-0")).scalar_one()
    prof = CandidateProfile(candidate_id=partial.id, opportunity_id=old_opp.id,
                            pipeline_status=PipelineStatus.CUSTOMER_REJECTED)
    db.add(prof)
    db.commit()
    prof.updated_at = datetime.now(timezone.utc) - timedelta(days=40)
    db.commit()
    before = next(r for r in suggest_candidates(db, opp) if r["candidate_id"] == partial.id)
    assert before["penalty"] == 15.0
    top_gap = before["gaps"][0]
    assert top_gap["area"] == "History" and "Rejected at this customer" in top_gap["detail"]
    assert "Old Position" in top_gap["detail"]
    # The same rejection a long time ago is history, not a penalty.
    prof.updated_at = datetime.now(timezone.utc) - timedelta(days=400)
    db.commit()
    after = next(r for r in suggest_candidates(db, opp) if r["candidate_id"] == partial.id)
    assert after["penalty"] == 0.0
    assert after["score"] == pytest.approx(before["score"] + 15.0, abs=0.2)


def test_history_lists_every_previous_application_with_its_outcome(db):
    opp, perfect, *_ = _seed(db)
    other = db.execute(select(Opportunity).where(Opportunity.opp_id == "OPP-X")).scalar_one()
    db.add(CandidateProfile(candidate_id=perfect.id, opportunity_id=other.id,
                            pipeline_status=PipelineStatus.SELF_WITHDRAWN,
                            withdrawn_from_status="Customer_Interview"))
    db.commit()
    top = suggest_candidates(db, opp)[0]
    assert top["candidate_id"] == perfect.id
    assert top["applications_count"] == 2
    hist = top["history"]
    assert [h["outcome"] for h in hist] == ["withdrawn", "in_progress"], "newest first"
    withdrawn = hist[0]
    assert withdrawn["customer_name"] == "Bosch" and withdrawn["this_customer"] is False
    assert withdrawn["withdrawn_from"] == "Customer_Interview"
    selected = hist[1]
    assert selected["customer_name"] == "Magna" and selected["this_customer"] is True
    assert selected["stage_label"] == "Customer Shortlisted"
    assert selected["opp_id"] == "OPP-0"
    assert {"ai_result", "ai_score", "ats_score", "applied_on"} <= set(selected)
