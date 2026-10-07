"""Reports ▸ Opportunities / Candidate Profiles (7 Oct 2026): the rows carry
the figures the lists print (creator NAME, positions, approval, the derived
status words), a customer + date window narrows them, the summary strip is
pure, and the three operational reports follow the Reports tab grant.

Run:  cd backend && python -m pytest tests/test_operational_reports.py -q
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from tests.test_ta_decision import db, _profile  # noqa: F401
from models import Opportunity, PipelineStatus as PS
from services import reports as svc

BACKEND = Path(__file__).resolve().parents[1]


def test_opportunities_report_carries_names_positions_and_a_summary(db):
    p = _profile(db, PS.JOINED)
    _profile(db, PS.SOURCING)
    rows = svc.opportunities_report(db)
    assert len(rows) == 2
    row = next(r for r in rows if r["opp_id"] == db.get(Opportunity, p.opportunity_id).opp_id)
    assert row["customer"].startswith("VISTEON") and row["created_by"]      # a name or a fallback, never missing
    assert "positions_total" in row and "approval_status" in row and "position_status" in row
    summary = svc.opportunities_summary(rows)
    assert summary["count"] == 2 and summary["by_type"] == {"T&M": 2}
    # Filters: one customer, a window that excludes everything.
    only = svc.opportunities_report(db, customer_id=db.get(Opportunity, p.opportunity_id).customer_id)
    assert [r["opp_id"] for r in only] == [row["opp_id"]]
    assert svc.opportunities_report(db, date_from=date.today() + timedelta(days=2)) == []


def test_candidate_profiles_report_window_summary_and_words(db):
    p = _profile(db, PS.JOINED)
    q = _profile(db, PS.SOURCING)
    q.applied_on = date.today() - timedelta(days=40)
    db.flush()
    rows = svc.candidate_profiles_report(db)
    assert {r["pipeline_status"] for r in rows} == {"Joined", "Sourcing"}
    assert all(r["opp_id"] and r["candidate_email"] for r in rows)
    recent = svc.candidate_profiles_report(db, date_from=date.today() - timedelta(days=7))
    assert [r["pipeline_status"] for r in recent] == ["Joined"]       # q applied 40 days ago
    summary = svc.candidate_profiles_summary(rows)
    assert summary["count"] == 2 and summary["joined"] == 1 and summary["live"] == 1 and summary["closed"] == 0
    assert sum(summary["by_stage"].values()) == 2
    assert svc.candidate_profiles_report(db, customer_id=db.get(Opportunity, p.opportunity_id).customer_id)[0]["opp_id"]


def test_the_three_operational_reports_follow_the_reports_tab_grant():
    src = (BACKEND / "routers" / "crm" / "reports.py").read_text(encoding="utf-8")
    assert 'REPORTS_READ = gated_read("reports")' in src
    for route in ('@router.get("/opportunities")', '@router.get("/candidate-profiles")', '@router.get("/recruiter-productivity")'):
        body = src.split(route, 1)[1].split("@router.", 1)[0]
        assert "Depends(REPORTS_READ)" in body, route
    # The CEO reports stay Admin/CEO only — no template can widen them.
    body = src.split('@router.get("/revenue")', 1)[1].split("@router.", 1)[0]
    assert "role_required()" in body
