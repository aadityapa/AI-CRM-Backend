"""The Interviews tab shows the AI verdict inline (23 Sep 2026).

Reported: the AI L1 round on a profile's Interviews tab was a date and a
"Full report" link — every reviewer had to leave the page. These pin the
compact overview that now renders in the card, and that it reads the SAME
numbers the full report page does.

Run:  cd backend && python -m pytest tests/test_ai_interview_summary.py -q
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from services.ai_interview_summary import (
    MAX_BULLETS, MAX_SKILLS, summarize_interview_record,
)


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
from models import AiInterviewLink, Candidate, CandidateProfile, Customer, Opportunity, OppType  # noqa: E402
from models import PipelineStatus as PS  # noqa: E402


def _record(**over) -> dict:
    base = {
        "job_title": "API Test Framework Developer",
        "final_status": "completed",
        "report_status": "ready",
        "updated_at_ist": "2026-09-18T11:23:00+05:30",
        "questions": ["Please introduce yourself.", "Q1", "Q2", "Q3"],
        "answers": ["Hi", "a1", "skip", "a3"],
        "answered_questions": 2,
        "skipped_questions": 1,
        "report": {
            "overall_score": 8.39,               # 0-10 scale, as ai.py writes it
            "overall_score_percent": 83.9,
            "technical_score": 8.0,
            "problem_solving_score": 7.5,
            "recommendation": "Hire",
            "overall_fitment": "Strong Fit",
            "summary": "Solid API automation fundamentals; clear on framework design.",
            "strengths": ["REST test design", "Pytest fixtures", "CI integration", "Mocking", "Extra"],
            "weaknesses": ["Contract testing depth"],
            "skill_scores": [{"skill": "Python", "score": 8.5}, {"skill": "REST", "score": 8.0}],
            "communication_evaluation": {"communication_score": 78},
            "scoring_summary": {"total_questions": 3, "excluded_questions": 0},
        },
    }
    base.update(over)
    return base


# ------------------------------------------------------------- the summariser

def test_scores_land_on_one_scale_whatever_the_report_used():
    out = summarize_interview_record(_record())
    assert out["available"] is True
    assert out["overall_score_percent"] == 83.9
    assert out["technical_score_percent"] == 80.0          # 8.0 on 0-10 → 80
    assert out["problem_solving_score_percent"] == 75.0
    assert out["communication_score_percent"] == 78.0      # already a percent
    assert out["skills"] == [{"skill": "Python", "score": 85.0}, {"skill": "REST", "score": 80.0}]


def test_score_reasons_win_like_they_do_on_the_report_page():
    rec = _record()
    rec["report"]["score_reasons"] = {"overall": {"score": 9.1}, "technical": {"score": 92}}
    out = summarize_interview_record(rec)
    assert out["overall_score_percent"] == 91.0
    assert out["technical_score_percent"] == 92.0


def test_verdict_text_and_counts():
    out = summarize_interview_record(_record())
    assert out["recommendation"] == "Hire"
    assert out["fitment"] == "Strong Fit"
    assert out["summary"].startswith("Solid API")
    assert out["questions"] == {"total": 3, "answered": 2, "skipped": 1, "excluded": 0}
    assert out["job_title"] == "API Test Framework Developer"
    assert out["terminated"] is False


def test_bullets_are_capped_and_the_persisted_analysis_wins():
    rec = _record()
    assert len(summarize_interview_record(rec)["strengths"]) == MAX_BULLETS
    rec["report"]["strengths_weaknesses_analysis"] = {
        "complete": True, "strengths": ["From analysis"], "weaknesses": ["Gap from analysis"],
    }
    out = summarize_interview_record(rec)
    assert out["strengths"] == ["From analysis"]
    assert out["improvements"] == ["Gap from analysis"]


def test_communication_hidden_when_the_template_did_not_assess_it():
    rec = _record()
    rec["report"]["communication_required"] = False
    assert summarize_interview_record(rec)["communication_score_percent"] is None


def test_not_attempted_and_terminated_are_flagged():
    rec = _record(final_status="terminated")
    rec["report"]["verdict_suppressed_reason"] = "no_scored_answers"
    out = summarize_interview_record(rec)
    assert out["terminated"] is True
    assert out["not_attempted"] is True


def test_a_missing_or_partial_record_never_raises():
    assert summarize_interview_record(None)["available"] is False
    out = summarize_interview_record({"report": {"skill_scores": "garbage", "overall_score": "nan"}})
    assert out["available"] is True
    assert out["skills"] == []
    assert out["overall_score_percent"] is None
    assert out["questions"]["total"] == 0


def test_skills_are_capped():
    rec = _record()
    rec["report"]["skill_scores"] = [{"skill": f"S{i}", "score": 5} for i in range(20)]
    assert len(summarize_interview_record(rec)["skills"]) == MAX_SKILLS


# ------------------------------------------------------------------ the route

@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _link(db, record_id="rec-1"):
    cust = Customer(name="VISTEON summary")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="OPP-S1", title="API Test Framework Developer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Rajesh", last_name="Pradhan", email="rajesh.summary@example.com")
    db.add(cand); db.flush()
    prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=PS.RMG_REVIEW)
    db.add(prof); db.flush()
    link = AiInterviewLink(invite_token="tok-summary-1", candidate_id=cand.id, opportunity_id=opp.id,
                           profile_id=prof.id, level="L1", overall_score_percent=83.9, result="Passed",
                           interview_record_id=record_id)
    db.add(link); db.commit()
    return prof, link


def test_route_returns_the_overview_for_the_profiles_link(db, monkeypatch):
    from routers.crm import ai_interviews as mod
    prof, link = _link(db)
    monkeypatch.setattr(mod, "_interview_record", lambda rid: _record() if rid == "rec-1" else None)
    out = mod.ai_interview_summary(prof.id, link.id, db=db, user=None)
    data = out["data"]
    assert out["success"] is True
    assert data["overall_score_percent"] == 83.9
    assert data["result"] == "Passed"
    assert data["level"] == "L1"
    assert data["recommendation"] == "Hire"


def test_route_falls_back_to_the_link_score_when_the_record_lags(db, monkeypatch):
    from routers.crm import ai_interviews as mod
    prof, link = _link(db, record_id="rec-missing")
    monkeypatch.setattr(mod, "_interview_record", lambda rid: None)
    data = mod.ai_interview_summary(prof.id, link.id, db=db, user=None)["data"]
    assert data["available"] is False
    assert data["overall_score_percent"] == 83.9   # the CRM's own headline still shows
    assert data["result"] == "Passed"


def test_route_404s_on_another_profiles_link(db, monkeypatch):
    from fastapi import HTTPException
    from routers.crm import ai_interviews as mod
    prof, link = _link(db)
    with pytest.raises(HTTPException) as exc:
        mod.ai_interview_summary(prof.id + 999, link.id, db=db, user=None)
    assert exc.value.status_code == 404


def test_summary_route_is_declared_before_the_bare_link_routes():
    """`/{link_id}/summary` is three segments against PUT/DELETE's two, so
    shape alone keeps it safe — but pin the declaration order anyway so a
    future GET `/{profile_id}/ai-interviews/{link_id}` cannot shadow it."""
    from routers.crm import ai_interviews as mod
    paths = [r.path for r in mod.router.routes]
    summary = next(i for i, p in enumerate(paths) if p.endswith("/ai-interviews/{link_id}/summary"))
    bare = next(i for i, p in enumerate(paths) if p.endswith("/ai-interviews/{link_id}"))
    assert summary < bare
