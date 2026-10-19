"""Talent Pool, Phase 1 (services/talent_pool.py, routers/crm/talent_pool.py).

Run:  cd backend && python -m pytest tests/test_talent_pool.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest
import sqlalchemy as sa
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
    "candidate_resumes",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateOutreach, CandidateProfile, CandidateSkill, Customer, Employee,
    Opportunity, OppType, PipelineStatus, Requirement, Resume, ResumeParseCache, Skill,
    SkillEvaluation,
)
from services import talent_pool as tp  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def test_skill_key_and_matching():
    assert tp.skill_key("  C++ ") == "c++"
    assert tp.skill_key("Python.") == "python"
    assert tp.skill_key("Node.js") == "node.js"
    assert tp.skill_matches("AUTOSAR", "AUTOSAR Classic")
    assert tp.skill_matches("autosar", "AUTOSAR")
    # a one-letter skill never matches inside a longer name
    assert not tp.skill_matches("C", "Embedded C")
    assert tp.skill_matches("C", "c")
    assert not tp.skill_matches("Java", "JavaScript")


def test_split_skill_text_reads_every_stored_shape():
    assert tp.split_skill_text('["Python", "C++"]') == ["Python", "C++"]
    assert tp.split_skill_text("Python, C++; RTOS | CAN") == ["Python", "C++", "RTOS", "CAN"]
    assert tp.split_skill_text(["A", {"name": "B"}]) == ["A", "B"]
    assert tp.split_skill_text(None) == []


def test_merge_keeps_every_source_and_ranks_by_evidence():
    rows = [("python", "cv", None), ("Python", "profile", None), ("Python", "interview", 4),
            ("RTOS", "ats", None), ("CAN", "interview_low", 2)]
    merged = tp.merge_skill_rows(rows)
    py = next(s for s in merged if s["key"] == "python")
    assert py["name"] == "Python"                      # the record's spelling
    assert py["best"] == "interview" and py["rating"] == 4
    assert py["evidence"][0] == "interview"
    assert [s["key"] for s in merged][0] == "python"   # strongest evidence first
    can = next(s for s in merged if s["key"] == "can")
    assert can["best"] == "interview_low"


def test_availability_and_score():
    assert tp.availability_for(live=True, joined=False, employee_status="Bench") == tp.IN_PROCESS
    assert tp.availability_for(live=False, joined=True, employee_status="Bench") == tp.BENCH
    assert tp.availability_for(live=False, joined=True, employee_status="Deployed") == tp.DEPLOYED
    assert tp.availability_for(live=False, joined=True, employee_status=None) == tp.AVAILABLE
    assert tp.match_score(0, 0, 0) is None
    assert tp.match_score(1, 2, 1) == 75.0          # (2 + 1) / 4
    assert tp.match_score(2, 0, 0) == 100.0


def test_filters_are_validated():
    assert tp.validate_filters(tp.PoolFilters(availability="nope"))
    assert tp.validate_filters(tp.PoolFilters(exp_min=5, exp_max=2))
    assert tp.validate_filters(tp.PoolFilters(sort="random"))
    assert tp.validate_filters(tp.PoolFilters()) is None


# ---------------------------------------------------------------------------
# The search over a seeded book
# ---------------------------------------------------------------------------

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    tp._CATALOGUE_CACHE.clear()
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    cust = Customer(name="Magna")
    db.add(cust)
    db.flush()
    opp = Opportunity(opp_id="OPP-1", title="Embedded", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id,
                      title="Embedded", created_by=1)
    db.add(req)
    python = Skill(name="Python", category="Programming")
    rtos = Skill(name="RTOS", category="Embedded")
    db.add_all([python, rtos])
    db.flush()

    def cand(name, email, **kw):
        c = Candidate(first_name=name, email=email, **kw)
        db.add(c)
        db.flush()
        return c

    typed = cand("Typed", "typed@x.in", experience_years=D("4"), expected_ctc=D("800000"),
                 notice_period="30 days", city="Pune")
    db.add(CandidateSkill(candidate_id=typed.id, skill_id=python.id))

    ats = cand("Ats", "ats@x.in", experience_years=D("6"), expected_ctc=D("2000000"))
    db.add(Resume(requirement_id=req.id, candidate_id=ats.id, candidate_name="Ats",
                  resume_file_url="a.pdf", ats_score=D("70"),
                  ats_score_breakdown={"matched": ["Python", "RTOS"], "missing": ["CAN"]}))

    parsed = cand("Parsed", "parsed@x.in", experience_years=D("3"))
    db.add(Resume(requirement_id=req.id, candidate_id=parsed.id, candidate_name="Parsed",
                  resume_file_url="p.pdf", file_sha256="sha-p"))
    db.add(ResumeParseCache(file_sha256="sha-p", parsed={"skills": ["Python", "Linux"]}))

    form = cand("Form", "form@x.in")
    db.add(Resume(requirement_id=req.id, candidate_id=form.id, candidate_name="Form",
                  resume_file_url="f.pdf", application_details={"skills": "RTOS, Python"}))

    proven = cand("Proven", "proven@x.in", experience_years=D("5"))
    prof = CandidateProfile(candidate_id=proven.id, opportunity_id=opp.id,
                            pipeline_status=PipelineStatus.RMG_REJECTED)
    db.add(prof)
    db.flush()
    db.add(SkillEvaluation(profile_id=prof.id, skill_id=python.id, reviewer_rated=4))
    db.add(SkillEvaluation(profile_id=prof.id, skill_id=rtos.id, reviewer_rated=2))

    # Missing "C": only lists "Embedded C"
    cand("NotC", "notc@x.in")

    # In a live candidacy
    busy = cand("Busy", "busy@x.in")
    db.add(CandidateSkill(candidate_id=busy.id, skill_id=python.id))
    db.add(CandidateProfile(candidate_id=busy.id, opportunity_id=opp.id,
                            pipeline_status=PipelineStatus.SALES_SCREENING))

    # Bench employees: one also a candidate (no duplicate entry), one employee-only
    db.add(Employee(first_name="Bench", last_name="Only", email="bench@karnex.in",
                    skills=["Python", "RTOS"], experience_years=D("7")))
    db.add(Employee(first_name="Typed", email="typed.emp@karnex.in", personal_email="typed@x.in",
                    skills=["Python"]))
    db.commit()
    return {"typed": typed, "ats": ats, "parsed": parsed, "form": form, "proven": proven,
            "busy": busy, "python": python, "req": req}


def _keys(rows):
    return {r["name"] for r in rows}


def test_every_source_finds_the_skill_with_its_evidence(db):
    s = _seed(db)
    holders = tp.skill_holders(db, "python")
    assert holders[s["typed"].id] == {"profile"}
    assert holders[s["ats"].id] == {"ats"}
    assert holders[s["parsed"].id] == {"cv"}
    assert holders[s["form"].id] == {"cv"}
    assert holders[s["proven"].id] == {"interview"}
    # "missing" in the ATS breakdown is NOT evidence
    assert s["ats"].id not in tp.skill_holders(db, "CAN")
    # a low interview rating is kept, but as low
    assert tp.skill_holders(db, "RTOS")[s["proven"].id] == {"interview_low"}


def test_search_by_skill_ranks_and_shows_evidence(db):
    s = _seed(db)
    rows, meta = tp.search_pool(db, tp.PoolFilters(skills=["Python"], nice=["RTOS"]))
    names = _keys(rows)
    assert {"Typed", "Ats", "Parsed", "Form", "Proven", "Busy", "Bench Only"} <= names
    assert meta["holders"]["Python"] >= 6
    # employee who is also a candidate appears once, as the candidate, with the internal chip
    typed = next(r for r in rows if r["name"] == "Typed")
    assert typed["kind"] == "candidate" and typed["internal"]["deployment"] == "Bench"
    assert typed["availability"] == tp.BENCH
    assert sum(1 for r in rows if r["name"].startswith("Typed")) == 1
    # nice-to-have raises the score
    ats = next(r for r in rows if r["name"] == "Ats")
    assert ats["score"] == 100.0 and typed["score"] == pytest.approx(66.7)
    assert ats["matched"][0]["skill"] == "Python" and ats["matched"][0]["best"] == "ats"
    bench = next(r for r in rows if r["kind"] == "employee")
    assert bench["availability"] == tp.BENCH and bench["applications"] is None
    # CTC is on the card (user decision)
    assert ats["expected_ctc"] == 2000000.0


def test_must_haves_intersect_and_proven_only(db):
    _seed(db)
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python", "RTOS"]))
    assert _keys(rows) >= {"Ats", "Form", "Proven", "Bench Only"}
    assert "Typed" not in _keys(rows) and "Parsed" not in _keys(rows)
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], proven_only=True))
    assert _keys(rows) == {"Proven"}
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["C"]))
    assert rows == []


def test_derived_filters(db):
    s = _seed(db)
    rows, meta = tp.search_pool(db, tp.PoolFilters(skills=["Python"], availability="available"))
    assert "Busy" not in _keys(rows)
    assert meta["availability_counts"][tp.IN_PROCESS] == 0
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], availability="in_process"))
    assert _keys(rows) == {"Busy"}
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], ctc_max_lakh=10))
    assert _keys(rows) == {"Typed"}
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], notice="30"))
    assert _keys(rows) == {"Typed"}
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], location="pune"))
    assert "Typed" in _keys(rows) and "Ats" not in _keys(rows)
    # a call note yesterday → not "untouched for 90 days"
    db.add(CandidateOutreach(candidate_id=s["ats"].id, channel="Call", note="spoke", user_id=1,
                             created_at=datetime.now(timezone.utc) - timedelta(days=1)))
    db.commit()
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["Python"], untouched_days=90))
    assert "Ats" not in _keys(rows) and "Parsed" in _keys(rows)
    ats_row = next(r for r in tp.search_pool(db, tp.PoolFilters(skills=["Python"]))[0] if r["name"] == "Ats")
    assert ats_row["last_note"]["note"] == "spoke"


def test_no_skill_search_lists_the_pool_and_pages(db):
    _seed(db)
    rows, meta = tp.search_pool(db, tp.PoolFilters(limit=3))
    assert len(rows) == 3 and meta["total"] >= 8
    assert all(r["score"] is None for r in rows)


def test_catalogue_counts_record_and_interview(db):
    s = _seed(db)
    cat = {c["name"]: c for c in tp.skill_catalogue(db)}
    assert cat["Python"]["count"] == 3          # Typed, Busy (record) + Proven (interview)
    assert cat["Python"]["proven"] == 1
    assert cat["RTOS"]["category"] == "Embedded"


def test_position_matches_use_the_suggestion_engine(db):
    s = _seed(db)
    from models import RequirementSkill
    db.add(RequirementSkill(requirement_id=s["req"].id, skill_id=s["python"].id, is_mandatory=True))
    db.commit()
    rows, meta = tp.position_matches(db, s["req"])
    assert meta["basis"]["mandatory_skills"] == ["Python"]
    assert "_skills" not in meta["basis"]
    names = _keys(rows)
    assert "Typed" in names and "Bench Only" in names
    # already applied to this opportunity → excluded (suggest_candidates' rule)
    assert "Busy" not in names and "Proven" not in names


def test_suggestions_count_interview_proof_and_cv_skills(db):
    """A skill rated in an interview (and one the parser read off a CV) is a skill
    the Suggested Candidates engine sees too — one engine, one answer."""
    s = _seed(db)
    from models import RequirementSkill
    opp2 = Opportunity(opp_id="OPP-2", title="Python Dev", customer_id=s["req"].customer_id,
                       opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp2)
    db.flush()
    req2 = Requirement(req_number="REQ-2", opportunity_id=opp2.id, customer_id=s["req"].customer_id,
                       title="Python Dev", created_by=1)
    db.add(req2)
    db.flush()
    db.add(RequirementSkill(requirement_id=req2.id, skill_id=s["python"].id, is_mandatory=True))
    db.commit()
    rows, _ = tp.position_matches(db, req2)
    by_name = {r["name"]: r for r in rows}
    for name in ("Proven", "Parsed"):
        assert name in by_name
        assert not any("Missing mandatory" in g["detail"] for g in by_name[name]["gaps"]), name


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_route_gate_registry_and_defaults():
    src = (BACKEND / "routers" / "crm" / "talent_pool.py").read_text(encoding="utf-8")
    assert 'screener_or(gated_read("talent-pool", "TA", "RMG", "Sales_Head"))' in src
    for fn in ("talent_pool_meta", "talent_pool_for_position", "search_talent_pool"):
        assert "Depends(POOL_READ)" in inspect.getsource(getattr(importlib.import_module("routers.crm.talent_pool"), fn))
    init = (BACKEND / "routers" / "crm" / "__init__.py").read_text(encoding="utf-8")
    assert '"talent_pool"' in init
    from services.access_registry import TABS
    from services.role_template_defaults import ROLE_TEMPLATE_DEFAULTS
    assert "talent-pool" in TABS
    holders = {r for r, tabs in ROLE_TEMPLATE_DEFAULTS.items() if "talent-pool" in tabs}
    assert holders == {"TA", "RMG", "Sales_Head"}


def _migration():
    spec = importlib.util.spec_from_file_location(
        "m0138", BACKEND / "alembic" / "versions" / "0138_talent_pool_tab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_0138_snapshot_and_backfill():
    import importlib.util  # noqa: F401
    m = _migration()
    from services.access_registry import IV_ROLE_ALIASES
    assert m._ROLE_ALIASES == IV_ROLE_ALIASES
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text("CREATE TABLE access_templates (id INTEGER PRIMARY KEY, role TEXT, tab_access TEXT)"))
        c.execute(sa.text("CREATE TABLE custom_roles (id INTEGER PRIMARY KEY, name TEXT, tab_access TEXT)"))
        c.execute(sa.text("INSERT INTO access_templates VALUES (1,'TA','{\"candidates\":\"edit\"}'),"
                          "(2,'Sales','{\"customers\":\"edit\"}'),"
                          "(3,'RMG','{\"talent-pool\":\"edit\"}')"))
        c.execute(sa.text("INSERT INTO custom_roles VALUES (1,'GM','{\"profiles\":\"edit\"}'),"
                          "(2,'Sales Manager','{\"customers\":\"view\"}')"))
        m._grant_templates(c)
        m._grant_custom_roles(c)
        t = {r[0]: json.loads(r[1]) for r in c.execute(sa.text("SELECT id, tab_access FROM access_templates"))}
        r = {x[0]: json.loads(x[1]) for x in c.execute(sa.text("SELECT id, tab_access FROM custom_roles"))}
    assert t[1]["talent-pool"] == "view"
    assert "talent-pool" not in t[2]
    assert t[3]["talent-pool"] == "edit"          # never lowered
    assert r[1]["talent-pool"] == "view"
    assert "talent-pool" not in r[2]
