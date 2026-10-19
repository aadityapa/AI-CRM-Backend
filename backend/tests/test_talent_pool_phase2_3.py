"""Talent Pool Phases 2 + 3 + retention / opt-out (17 Oct 2026).

Run:  cd backend && python -m pytest tests/test_talent_pool_phase2_3.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
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


import models  # noqa: E402,F401 — every table
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateOutreach, CandidateSkill, Customer, Opportunity, OppType,
    Requirement, RequirementSkill, Resume, Skill, SkillSynonym, TalentSavedSearch, TalentSkillIndex,
)
from services import talent_pool as tp  # noqa: E402
from services import talent_retention as tr  # noqa: E402
from services import talent_saved_searches as ss  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    tp._CATALOGUE_CACHE.clear()
    tp.invalidate_synonyms()
    monkeypatch.setattr(tr, "retention_months", lambda: 24)
    try:
        yield s
    finally:
        s.close()


def _seed(db):
    cust = Customer(name="Magna")
    db.add(cust)
    db.flush()
    opp = Opportunity(opp_id="OPP-1", title="AUTOSAR", customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    req = Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=cust.id, title="AUTOSAR", created_by=1)
    db.add(req)
    classic = Skill(name="AUTOSAR Classic", category="Automotive")
    python = Skill(name="Python", category="Programming")
    db.add_all([classic, python])
    db.flush()

    def cand(name, email, **kw):
        c = Candidate(first_name=name, email=email, created_by_id=1, **kw)
        db.add(c)
        db.flush()
        return c

    cp = cand("Cp", "cp@x.in", experience_years=D("5"))           # typed "AUTOSAR CP" on a CV form
    db.add(Resume(requirement_id=req.id, candidate_id=cp.id, candidate_name="Cp", resume_file_url="cp.pdf",
                  application_details={"skills": "AUTOSAR CP, CAN"}))
    typed = cand("Typed", "typed@x.in")
    db.add(CandidateSkill(candidate_id=typed.id, skill_id=classic.id))
    py = cand("Py", "py@x.in")
    db.add(CandidateSkill(candidate_id=py.id, skill_id=python.id))
    db.commit()
    return {"cp": cp, "typed": typed, "py": py, "classic": classic, "python": python, "req": req, "opp": opp}


def _names(rows):
    return {r["name"] for r in rows}


# ---------------------------------------------------------------------------
# Phase 2 — synonyms
# ---------------------------------------------------------------------------

def test_synonym_groups_merge_and_match():
    syn = tp.build_synonym_map([("AUTOSAR Classic", "AUTOSAR CP"), ("AUTOSAR CP", "AUTOSAR R4")])
    assert set(syn["autosar r4"]) == {"AUTOSAR Classic", "AUTOSAR CP"}
    assert tp.skill_matches("AUTOSAR Classic", "AUTOSAR CP")
    assert tp.skill_matches("JavaScript", "JS") and tp.skill_matches("JS", "JavaScript")
    assert not tp.skill_matches("C", "Embedded C")
    assert not tp.skill_matches("Java", "JavaScript")
    assert tp.expand_term("K8s")[0] == "K8s" and "Kubernetes" in tp.expand_term("K8s")


def test_search_finds_a_synonym_and_admin_pairs_join(db):
    s = _seed(db)
    rows, meta = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert {"Cp", "Typed"} <= _names(rows)
    assert "AUTOSAR CP" in meta["synonyms"]["AUTOSAR Classic"]
    # admin adds "Py3" ≈ Python
    tp.add_synonym(db, "Python", "Py-three", 1)
    db.commit()
    db.add(Resume(requirement_id=s["req"].id, candidate_id=s["typed"].id, candidate_name="Typed",
                  resume_file_url="t.pdf", application_details={"skills": "Py-three"}))
    db.commit()
    assert s["typed"].id in tp.skill_holders(db, "Python")
    with pytest.raises(ValueError):
        tp.add_synonym(db, "Python", "Py-three", 1)      # already attached
    with pytest.raises(ValueError):
        tp.add_synonym(db, "Python", "python", 1)        # itself
    with pytest.raises(ValueError):
        tp.add_synonym(db, "AUTOSAR Classic", "AUTOSAR CP", 1)   # already the same skill (built-in)
    row = db.execute(select(SkillSynonym)).scalar_one()
    assert tp.remove_synonym(db, row.id)


# ---------------------------------------------------------------------------
# Phase 2 — the index
# ---------------------------------------------------------------------------

def test_index_answers_the_search_and_dirty_candidates_are_read_live(db):
    s = _seed(db)
    live, _ = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert not tp.index_state(db).ready
    out = tp.rebuild_index(db)
    assert out["mode"] == "full" and out["rows"] > 0
    state = tp.index_state(db)
    assert state.ready
    rows, meta = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert meta["index"]["used"] is True
    assert _names(rows) == _names(live)
    # A typed skill added AFTER the build is found at once (read live), before the job runs.
    db.add(CandidateSkill(candidate_id=s["py"].id, skill_id=s["classic"].id))
    db.commit()
    rows, meta = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert "Py" in _names(rows) and meta["index"]["live_candidates"] >= 1
    # The incremental job folds it in; nothing is dirty afterwards.
    out = tp.update_index(db)
    assert out["mode"] == "incremental" and out["candidates"] >= 1
    assert db.execute(select(TalentSkillIndex).where(TalentSkillIndex.candidate_id == s["py"].id,
                                                     TalentSkillIndex.skill_key == "autosar classic")).first()


def test_index_job_rebuilds_once_a_day(db):
    _seed(db)
    assert tp.run_index_job(db, today=date.today())["mode"] == "full"
    assert tp.run_index_job(db, today=date.today())["mode"] == "incremental"
    assert tp.run_index_job(db, today=date.today() + timedelta(days=1))["mode"] == "full"


def test_catalogue_counts_cv_skills_once_indexed(db):
    _seed(db)
    tp.rebuild_index(db)
    tp._CATALOGUE_CACHE.clear()
    cat = tp.skill_catalogue(db)
    cv_only = {c["name"] for c in cat if c["category"] == tp.CV_ONLY_CATEGORY}
    assert all(c["id"] is None for c in cat if c["category"] == tp.CV_ONLY_CATEGORY)
    assert "CAN" not in cv_only   # held by one candidate only — below the floor of 2


# ---------------------------------------------------------------------------
# Phase 3 — saved searches
# ---------------------------------------------------------------------------

def test_filters_round_trip_and_summary():
    f = tp.filters_from_params({"skills": "Python, RTOS", "nice": "CAN,python", "exp_min": "3",
                                "proven_only": "true", "availability": "available"})
    assert f.skills == ["Python", "RTOS"] and f.nice == ["CAN"]
    params = tp.filters_to_params(f)
    assert params == {"skills": ["Python", "RTOS"], "nice": ["CAN"], "exp_min": 3.0,
                      "availability": "available", "proven_only": True}
    assert tp.filters_from_params(params).skills == ["Python", "RTOS"]
    assert "Python + RTOS" in tp.describe_filters(f)
    with pytest.raises(ValueError):
        tp.filters_from_params({"exp_min": "lots"})


def test_saved_search_alerts_only_on_new_matches(db, monkeypatch):
    s = _seed(db)
    told = []
    monkeypatch.setattr("services.notify.notify_user", lambda *a, **k: told.append((a, k)))
    search = ss.create(db, 1, "AUTOSAR people", {"skills": ["AUTOSAR Classic"]})
    db.commit()
    assert search.new_keys == [] and f"c{s['cp'].id}" in search.seen_keys     # no flood on save
    with pytest.raises(ValueError):
        ss.create(db, 1, "autosar PEOPLE", {"skills": ["X"]})                 # same name
    with pytest.raises(ValueError):
        ss.create(db, 1, "Empty", {})                                           # no criterion
    assert ss.run_alerts(db)["alerted"] == 0 and told == []
    # Someone new matches
    db.add(CandidateSkill(candidate_id=s["py"].id, skill_id=s["classic"].id))
    db.commit()
    out = ss.run_alerts(db)
    assert out == {"checked": 1, "alerted": 1, "new": 1}
    (args, kwargs), = told
    assert args[1] == 1 and "1 new match" in args[2] and kwargs["event"] == ss.ALERT_EVENT
    db.refresh(search)
    assert search.new_keys == [f"c{s['py'].id}"]
    # Not new twice; seen clears the marks
    assert ss.run_alerts(db)["alerted"] == 1 - 1
    ss.mark_seen(search)
    assert search.new_keys == []
    # Owner only
    assert ss.get_owned(db, 2, search.id) is None and ss.get_owned(db, 1, search.id) is search
    # Changing the criteria resets the baseline
    ss.update(db, search, filters={"skills": ["Python"]})
    assert f"c{s['py'].id}" in search.seen_keys and search.new_keys == []


# ---------------------------------------------------------------------------
# Retention + opt-out
# ---------------------------------------------------------------------------

def test_retention_rules():
    assert tr.add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert tr.add_months(date(2024, 5, 15), 24) == date(2026, 5, 15)
    today = date(2026, 10, 17)
    assert tr.retention_state(None, 24, today)[0] == "active"
    assert tr.retention_state(date(2024, 10, 1), 24, today) == ("expired", date(2026, 10, 1))
    assert tr.retention_state(date(2024, 11, 1), 24, today)[0] == "expiring"
    assert tr.retention_state(date(2025, 6, 1), 24, today)[0] == "active"
    assert tr.validate_months(3) and tr.validate_months("x") and tr.validate_months(24) is None


def _age(db, cand, when=OLD):
    from sqlalchemy import update
    db.execute(update(Candidate).where(Candidate.id == cand.id).values(created_at=when, updated_at=when))
    db.execute(update(Resume).where(Resume.candidate_id == cand.id).values(created_at=when))
    db.commit()


def _warned_long_ago(db, cand):
    from sqlalchemy import update
    db.execute(update(Candidate).where(Candidate.id == cand.id)
               .values(updated_at=Candidate.updated_at,
                       pool_expiry_warned_at=datetime.now(timezone.utc) - timedelta(days=tr.WARN_DAYS + 1)))
    db.commit()


def test_retention_warns_first_then_expires_keeps_and_revives(db, monkeypatch):
    s = _seed(db)
    warned = []
    monkeypatch.setattr("services.notify.notify_user", lambda *a, **k: warned.append(a))
    _age(db, s["cp"])                                              # 2020 → long past the period
    _age(db, s["typed"], datetime.now(timezone.utc) - timedelta(days=715))  # ends in ~15 days
    out = tr.run_retention(db)
    # Warn first: nobody leaves on the first run, both are warned in ONE digest to their TA.
    assert out["expired"] == 0 and out["warned"] == 2 and out["owners_told"] == 1
    assert "2 candidates leave the talent pool" in warned[0][2]
    rows, _ = tr.retention_list(db, "expiring")
    assert {r["name"] for r in rows} == {"Cp", "Typed"}
    assert tr.run_retention(db)["warned"] == 0                     # warned once only
    # 30 days after the warning, the one past the period leaves
    _warned_long_ago(db, s["cp"])
    out = tr.run_retention(db)
    assert out["expired"] == 1
    db.refresh(s["cp"])
    before = s["cp"].updated_at
    assert s["cp"].pool_expired_at is not None and s["cp"].updated_at == before
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert "Cp" not in _names(rows) and "Typed" in _names(rows)
    assert s["cp"].id in tp.hidden_candidate_ids(db)
    rows, meta = tr.retention_list(db, "expired")
    assert [r["name"] for r in rows] == ["Cp"] and meta["counts"]["expired"] == 1
    # Keep brings them back; new activity revives too
    tr.keep_candidate(db, s["cp"], 1, "spoke today, still interested")
    db.commit()
    assert s["cp"].pool_expired_at is None
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert "Cp" in _names(rows)
    _age(db, s["py"])
    tr.run_retention(db)
    _warned_long_ago(db, s["py"])
    tr.run_retention(db)
    db.refresh(s["py"])
    assert s["py"].pool_expired_at is not None
    db.add(CandidateOutreach(candidate_id=s["py"].id, channel="Call", note="called", user_id=1))
    db.commit()
    assert tr.run_retention(db)["revived"] == 1


def test_opt_out_token_link_and_mail_guard(db):
    s = _seed(db)
    token = tr.opt_out_token(s["cp"].id)
    assert tr.parse_opt_out_token(token) == s["cp"].id
    assert tr.parse_opt_out_token(f"{s['cp'].id}.deadbeef") is None
    assert tr.parse_opt_out_token("x") is None
    text, html = tr.email_footer(s["cp"].id)
    assert token in text and "Remove me from your talent pool" in html
    # in the pool: nothing refused
    assert tr.mail_guard(db, None, "CP@x.in", "candidate.opening_interest") == (s["cp"].id, False, False)
    with pytest.raises(ValueError):
        tr.opt_out(db, s["cp"], source="staff", reason="", user_id=1)
    assert tr.opt_out(db, s["cp"], source="staff", reason="asked on the phone", user_id=1)
    assert not tr.opt_out(db, s["cp"], source="candidate", reason=None, user_id=None)   # idempotent
    db.commit()
    assert tr.mail_guard(db, s["cp"].id, "", "candidate.opening_interest")[2] is True
    assert tr.mail_guard(db, s["cp"].id, "", "candidate.hiring_interest")[2] is True
    assert tr.mail_guard(db, s["cp"].id, "", "candidate.ai_invite")[2] is False   # an application they are in
    rows, _ = tp.search_pool(db, tp.PoolFilters(skills=["AUTOSAR Classic"]))
    assert "Cp" not in _names(rows)
    with pytest.raises(ValueError):
        tr.keep_candidate(db, s["cp"], 1, "")
    with pytest.raises(ValueError):
        tr.opt_in(db, s["cp"], reason="short", user_id=1)
    tr.opt_in(db, s["cp"], reason="Candidate emailed asking to be considered again", user_id=1)
    db.commit()
    assert s["cp"].pool_opt_out_at is None


def test_opted_out_candidate_is_not_suggested(db):
    s = _seed(db)
    db.add(RequirementSkill(requirement_id=s["req"].id, skill_id=s["classic"].id, is_mandatory=True))
    db.commit()
    from services.candidate_match import suggest_candidates
    assert s["typed"].id in {r["candidate_id"] for r in suggest_candidates(db, s["opp"])}
    tr.opt_out(db, s["typed"], source="candidate", reason=None, user_id=None)
    db.commit()
    assert s["typed"].id not in {r["candidate_id"] for r in suggest_candidates(db, s["opp"])}


def test_public_opt_out_page(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from crm_deps import get_crm_db
    from routers.crm import public_talent_pool

    s = _seed(db)
    monkeypatch.setattr(tr, "notify_opt_out", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(public_talent_pool.router)
    app.dependency_overrides[get_crm_db] = lambda: db
    client = TestClient(app)
    url = f"/api/public/talent-pool/opt-out/{tr.opt_out_token(s['cp'].id)}"
    page = client.get(url)
    assert page.status_code == 200 and "Hello Cp" in page.text and "cp@x.in" not in page.text
    db.refresh(s["cp"])
    assert s["cp"].pool_opt_out_at is None                     # GET never changes anything
    done = client.post(url)
    assert done.status_code == 200 and "removed" in done.text.lower()
    db.refresh(s["cp"])
    assert s["cp"].pool_opt_out_source == "candidate"
    assert client.get("/api/public/talent-pool/opt-out/1.bad").status_code == 404


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_wiring():
    from routers.crm.email_flows import EVENTS
    from services.scheduler import EVERY_PASS_JOBS, JOBS
    events = {e["event"] for e in EVENTS}
    assert {ss.ALERT_EVENT, tr.WARNING_EVENT, tr.OPT_OUT_EVENT} <= events
    assert {"talent_pool_index", "talent_pool_alerts", "talent_pool_retention"} <= set(JOBS)
    assert "talent_pool_index" in EVERY_PASS_JOBS and "talent_pool_alerts" not in EVERY_PASS_JOBS
    init = (BACKEND / "routers" / "crm" / "__init__.py").read_text(encoding="utf-8")
    assert '"public_talent_pool"' in init
    src = (BACKEND / "routers" / "crm" / "talent_pool.py").read_text(encoding="utf-8")
    # literal routes are declared before the parametric /positions/{id} and the bare search
    assert src.index('"/saved-searches"') < src.index('"/positions/{requirement_id}"')
    assert src.index('"/retention"') < src.index('@router.get("")')
    spec = importlib.util.spec_from_file_location("m0139", BACKEND / "alembic" / "versions" / "0139_talent_pool_phase2_3.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (mod.revision, mod.down_revision) == ("0139", "0138")
    cols = {c.name for c in Candidate.__table__.columns}
    assert {name for name, _ in mod._CANDIDATE_COLUMNS} <= cols
    # the sourcing mails are the only ones refused for an opted-out candidate
    assert tr.SOURCING_EVENTS == {"candidate.opening_interest", "candidate.hiring_interest"}
    assert TalentSavedSearch.__tablename__ == "talent_saved_searches"
