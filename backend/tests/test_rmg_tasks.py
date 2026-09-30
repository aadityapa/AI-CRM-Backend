"""RMG / GM pending work (28 Sep 2026, `services/rmg_tasks.py`).

Pinned: every candidate category comes from the desk's own `next_step` and
opens the desk AT that candidate; "Results to review" holds a finished AI L1
or a verdict someone else recorded until a screener marks it reviewed; a
screener's own verdict is reviewed at once and notifies nobody; `?task=`
narrows the desk queue to the category's rows; the Dashboard work desk shows
the same categories; the new endpoints sit behind the desk's approval gate.

Run:  cd backend && python -m pytest tests/test_rmg_tasks.py -q
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
]:
    importlib.import_module(f"models.{_m}")

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, InterviewEvent, Opportunity,
    OppType, PipelineStatus as PS, Requirement, RequirementStatus,
)
from models.ai_links import AiInterviewLink  # noqa: E402
import services.rmg_tasks as rt  # noqa: E402
from services import screening_desk as desk  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
RMG = CurrentUser(id=1, username="rmg", full_name="Rita RMG", roles={"RMG"})
SALES = CurrentUser(id=2, username="sales", full_name="Sam Sales", roles={"Sales"})
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.execute(users_table_stub.insert().values(id=2))
    s.commit()
    told: list[tuple] = []
    monkeypatch.setattr(rt, "_notify_result", lambda db, profile, headline, message, **kw:
                        told.append((profile.id, headline)))
    s.info["told"] = told
    try:
        yield s
    finally:
        s.close()


def _profile(db, name, *, stage=PS.SOURCING, screening="Pending", jd="Embedded C, AUTOSAR"):
    n = len(db.execute(select(Customer.id)).all())
    cust = Customer(name=f"C{n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{n}", title="Embedded", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name=name, email=f"{name.lower()}@mail.com", phone="9999999999")
    db.add_all([opp, cand]); db.flush()
    db.add(Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id,
                       title="Embedded C Developer", no_of_positions=1, rmg_jd_text=jd,
                       status=RequirementStatus.IN_PROGRESS, created_by=1))
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         rmg_screening_status=screening, ta_owner_id=2, ta_owner_name="Tara",
                         applied_on=NOW - timedelta(days=1))
    db.add(p); db.flush()
    return p


def _cats(db, user=RMG):
    return {c["key"]: c for c in rt.screener_tasks(db, user)["categories"]}


def test_candidate_categories_follow_the_desks_next_step(db):
    waiting = _profile(db, "Waiting")
    undecided = _profile(db, "Undecided", screening="Shortlisted")
    asked = _profile(db, "Asked", screening="Shortlisted")
    db.add(CandidateProfileActivityLog(profile_id=asked.id, user_id=1, action_type="L1_REQUESTED"))
    review = _profile(db, "Review", stage=PS.RMG_REVIEW, screening="Shortlisted")
    failed = _profile(db, "Failed", screening="Shortlisted")
    db.add(AiInterviewLink(invite_token="t-f", candidate_id=failed.candidate_id,
                           opportunity_id=failed.opportunity_id, profile_id=failed.id,
                           result="Failed", overall_score_percent=41, completed_at=NOW))
    db.flush()
    cats = _cats(db)
    titles = {k: [i["title"] for i in c["items"]] for k, c in cats.items()}
    assert titles["screening"] == ["Waiting"]
    assert titles["route"] == ["Undecided"]
    assert titles["booking"] == ["Asked"] and cats["booking"]["items"][0]["chip"] == "Technical L1"
    assert titles["decide"] == ["Review"]
    assert titles["ai_failed"] == ["Failed"]
    # every candidate item opens the desk AT that candidate, on its own task
    assert cats["route"]["items"][0]["path"] == f"screening-desk?task=route&focus={undecided.id}"
    assert cats["screening"]["desk_ids"] == [waiting.id]
    assert list(cats)[:2] == ["feedback", "results"]           # reading order: the overdue first
    assert review.id in cats["decide"]["desk_ids"]


def test_feedback_items_carry_the_position_round_and_interviewer_facets(db):
    """30 Sep 2026: the desk's Feedback-due panel groups by position or by day
    and filters by round — the facets ride on every item."""
    from models import InterviewEvent

    p = _profile(db, "Overdue", stage=PS.RMG_REVIEW, screening="Shortlisted")
    db.add(InterviewEvent(profile_id=p.id, kind="L1_Interview", status="Scheduled", interviewer="Ravi",
                          scheduled_at=NOW - timedelta(hours=5)))
    db.flush()
    [item] = _cats(db)["feedback"]["items"]
    assert item["section"] == "OPP-0 · Embedded" and item["round_kind"] == "L1_Interview"
    assert item["interviewer"] == "Ravi" and item["overdue_hours"] >= 3


def test_a_finished_ai_l1_waits_in_results_until_marked_reviewed(db):
    p = _profile(db, "Asha", screening="Shortlisted")
    db.add(AiInterviewLink(invite_token="t-a", candidate_id=p.candidate_id, opportunity_id=p.opportunity_id,
                           profile_id=p.id, result="Passed", overall_score_percent=78.4, completed_at=NOW))
    old = _profile(db, "Old", screening="Shortlisted")
    db.add(AiInterviewLink(invite_token="t-o", candidate_id=old.candidate_id, opportunity_id=old.opportunity_id,
                           profile_id=old.id, result="Passed", completed_at=NOW - timedelta(days=60)))
    db.flush()
    items = _cats(db)["results"]["items"]
    assert [i["title"] for i in items] == ["Asha"]                   # outside the window: not a task
    assert items[0]["chip"] == "AI L1: Passed · 78%" and items[0]["tone"] == "ok"
    assert items[0]["path"] == f"screening-desk?task=results&focus={p.id}"
    # the desk row carries the highlight
    rows, _ = desk.desk_queue(db, desk.DeskFilters(screening="all"))
    row = next(r for r in rows if r["profile_id"] == p.id)
    assert [(r["label"], r["result"]) for r in row["new_results"]] == [("AI L1", "Passed")]
    assert row["new_results"][0]["key"].startswith("ai:")
    assert rt.mark_reviewed(db, p, RMG) == 1
    assert _cats(db)["results"]["count"] == 0
    assert rt.mark_reviewed(db, p, RMG) == 0                          # idempotent


def test_a_verdict_by_someone_else_is_told_and_highlighted_a_screeners_own_is_not(db):
    p = _profile(db, "Kiran", stage=PS.CUSTOMER_INTERVIEW, screening="Shortlisted")
    ev = InterviewEvent(profile_id=p.id, kind="Customer_Interview", status="Completed",
                        scheduled_at=NOW - timedelta(hours=3))
    db.add(ev); db.flush()
    ev.result = "Hire"
    rt.record_round_result(db, p, ev, SALES, None)
    assert db.info["told"] == [(p.id, "Customer L1 - Interview — Hire")]
    item = _cats(db)["results"]["items"][0]
    # not on the desk (with Sales now) → the profile's Interviews tab
    assert item["path"] == f"profiles/{p.id}?tab=interviews" and item["chip"] == "Customer L1: Hire"
    rt.record_round_result(db, p, ev, SALES, "Hire")                  # unchanged verdict: nothing new
    assert len(db.info["told"]) == 1

    mine = _profile(db, "Mine", stage=PS.RMG_REVIEW, screening="Shortlisted")
    ev2 = InterviewEvent(profile_id=mine.id, kind="L1_Interview", status="Completed", result="Hire")
    db.add(ev2); db.flush()
    rt.record_round_result(db, mine, ev2, RMG, None)
    assert len(db.info["told"]) == 1                                   # a screener's own verdict
    assert mine.id not in rt.unreviewed_results(db)

    hr = InterviewEvent(profile_id=p.id, kind="HR_Interview", result="Hire")
    db.add(hr); db.flush()
    rt.record_round_result(db, p, hr, SALES, None)
    assert len(db.info["told"]) == 1                                   # HR's verdict is HR's business


def test_the_task_filter_narrows_the_desk_queue(db):
    _profile(db, "Pending one")
    route = _profile(db, "Route one", screening="Shortlisted")
    ids = rt.task_profile_ids(db, RMG, "route")
    rows, meta = desk.desk_queue(db, desk.DeskFilters(screening="all", profile_ids=tuple(ids)))
    assert [r["profile_id"] for r in rows] == [route.id] and meta["total"] == 1
    rows, _ = desk.desk_queue(db, desk.DeskFilters(screening="all", profile_ids=()))
    assert rows == []                                                  # an empty task matches nothing
    with pytest.raises(HTTPException):
        rt.task_profile_ids(db, RMG, "nonsense")


def test_positions_without_a_jd_or_skills_are_a_task(db):
    _profile(db, "Any", jd=None)
    items = _cats(db)["jd"]["items"]
    assert len(items) == 1 and items[0]["path"].endswith("?tab=details")
    assert items[0]["action"] == "Add JD & skills"


def test_the_dashboard_desk_shows_the_same_categories(db, monkeypatch):
    import services.dashboard_desk as dd
    import services.dashboards as dsh
    import services.work_desk as wd
    monkeypatch.setattr(dd, "upcoming", lambda _db, _u, days=7: {"days": days, "items": []})
    monkeypatch.setattr(dsh, "my_work", lambda _db, _u: {"items": [], "all_clear": True})
    _profile(db, "Waiting")
    _profile(db, "Undecided", screening="Shortlisted")
    keys = [t["key"] for t in wd.desk(db, RMG)["tabs"]]
    assert keys[:4] == ["feedback", "results", "screening", "route"]
    assert "booking" not in keys                                       # empty extras stay out
    board = _cats(db)
    tabs = {t["key"]: t for t in wd.desk(db, RMG)["tabs"]}
    # The desk adds only the filter facets (customer · month) on top.
    facets = {"customer", "month"}
    assert [{k: v for k, v in it.items() if k not in facets} for it in tabs["route"]["items"]] \
        == board["route"]["items"]
    assert all("customer" in it for it in tabs["route"]["items"])


def test_the_new_endpoints_sit_behind_the_desk_gate_and_the_routes_record_results():
    src = (BACKEND / "routers" / "crm" / "screening_desk.py").read_text(encoding="utf-8")
    for route in ('@router.get("/tasks")', '@router.post("/results-reviewed")', '@router.get("/handed-over")'):
        body = src.split(route, 1)[1].split("@router.", 1)[0]
        assert "Depends(desk_gate)" in body, route
    cp = (BACKEND / "routers" / "crm" / "candidate_profiles.py").read_text(encoding="utf-8")
    assert cp.count("record_round_result(db, profile, event, user,") == 2   # create + update
    bridge = (BACKEND / "services" / "ai_interview_bridge.py").read_text(encoding="utf-8")
    assert bridge.count("_review_link(link.profile_id)") == 2
    flows = (BACKEND / "routers" / "crm" / "email_flows.py").read_text(encoding="utf-8")
    assert '"interview.result_recorded"' in flows


def test_submit_to_sales_keeps_a_history_of_hand_overs(db):
    """29 Sep 2026 report: "Submit to Sales" showed nothing right after a
    submission. The to-do list empties when the job is done; the hand-overs
    (RMG_Review -> Sales_Screening, or a fast-track) are listed with where the
    candidate is now — newest first, old ones out of the window."""
    sent = _profile(db, "Pushpa", stage=PS.SALES_SCREENING, screening="Shortlisted")
    fast = _profile(db, "Fast", stage=PS.SALES_SCREENING, screening="Shortlisted")
    old = _profile(db, "Old", stage=PS.SALES_SCREENING, screening="Shortlisted")
    db.add_all([
        CandidateProfileActivityLog(profile_id=sent.id, user_id=1, action_type="STATUS_CHANGE",
                                    comment="RMG_Review -> Sales_Screening: strong on pytest",
                                    timestamp=NOW - timedelta(hours=2)),
        CandidateProfileActivityLog(profile_id=fast.id, user_id=1, action_type="FAST_TRACKED",
                                    comment="internal, deployed", timestamp=NOW - timedelta(hours=1)),
        CandidateProfileActivityLog(profile_id=old.id, user_id=1, action_type="STATUS_CHANGE",
                                    comment="RMG_Review -> Sales_Screening: long ago",
                                    timestamp=NOW - timedelta(days=90)),
    ])
    db.flush()
    rows = rt.recent_handovers(db, now=NOW)
    assert [r["candidate_name"] for r in rows] == ["Fast", "Pushpa"]
    assert rows[0]["fast_track"] is True and rows[1]["note"] == "strong on pytest"
    assert rows[1]["status"] and rows[1]["path"] == f"profiles/{sent.id}"


def test_the_gm_billing_chain_is_on_the_board_only_for_whoever_may_bill(db, monkeypatch):
    """29 Sep 2026 (user ask): the GM approves timesheets and raises the Proforma,
    so the Screening Desk board and the Dashboard carry Timesheets to approve ·
    Proformas to raise · Proformas with Finance · Tax invoices issued — each row a
    link to the timesheet / invoice. A login without those approvals (RMG) gets
    none of them, and the two info tiles never count as pending."""
    from datetime import date
    from models import Employee, Invoice, Project, Timesheet, TimesheetStatus
    import services.action_permissions as ap

    GM = CurrentUser(id=1, username="gm", full_name="Gita GM", roles={"GM"})
    real = ap.user_may
    monkeypatch.setattr(ap, "user_may", lambda db_, user, action: (
        user is GM and action in ("timesheet.approve", "timesheet.generate_invoice", "profile.rmg_screening"))
        or (user is not GM and real(db_, user, action)))

    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Cluster")
    db.add(proj); db.flush()
    sheets = []
    for i, status in enumerate((TimesheetStatus.SUBMITTED, TimesheetStatus.APPROVED, TimesheetStatus.APPROVED)):
        emp = Employee(first_name=f"E{i}", email=f"e{i}@k.in")
        db.add(emp); db.flush()
        ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=2026, month=9, status=status,
                       submitted_at=NOW - timedelta(days=5),
                       approved_at=NOW if status == TimesheetStatus.APPROVED else None)
        db.add(ts); db.flush()
        sheets.append(ts)
    pi = Invoice(invoice_number="PI-2026-001", proforma_number="PI-2026-001", project_id=proj.id,
                 timesheet_id=sheets[2].id, invoice_date=date.today(), sub_total=1, grand_total=1, kind="Proforma")
    tax = Invoice(invoice_number="KR-1", project_id=proj.id, invoice_date=date.today(), sub_total=1,
                  grand_total=1, kind="Tax")
    db.add_all([pi, tax]); db.flush()

    board = rt.screener_tasks(db, GM)
    cats = {c["key"]: c for c in board["categories"]}
    assert [i["path"] for i in cats["ts_approve"]["items"]] == [f"timesheets/{sheets[0].id}"]
    assert cats["ts_approve"]["items"][0]["action"] == "Review & approve"
    assert [i["path"] for i in cats["proforma_raise"]["items"]] == [f"timesheets/{sheets[1].id}"]
    assert cats["proforma_raise"]["items"][0]["action"] == "Raise Proforma"
    assert [i["path"] for i in cats["proforma_finance"]["items"]] == [f"invoices/{pi.id}"]
    assert [i["path"] for i in cats["invoices_issued"]["items"]] == [f"invoices/{tax.id}"]
    assert cats["proforma_finance"]["info"] and cats["invoices_issued"]["info"]
    info = cats["proforma_finance"]["count"] + cats["invoices_issued"]["count"]
    assert board["total"] == sum(c["count"] for c in board["categories"]) - info

    # The Dashboard work desk shows the same tiles (captions + info flag).
    import services.work_desk as wd
    tabs = {t["key"]: t for t in wd._screener_tabs(db, GM)}
    assert tabs["ts_approve"]["stage"] == "Your move" and tabs["invoices_issued"].get("info") is True
    assert tabs["ts_approve"]["link"] == "screening-desk?task=ts_approve"

    # RMG may not approve timesheets: none of the billing categories.
    assert not set(_cats(db)) & set(rt.BILLING_CATEGORIES)
