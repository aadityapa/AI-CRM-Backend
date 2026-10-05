"""The work desk — each login's daily tasks as Dashboard tabs (28 Sep 2026,
`services/work_desk.py`, `GET /api/dashboard/desk`).

Pinned: a TA gets feedback / to schedule / awaiting your call / upcoming /
my queues, over their OWN candidates; a screener (RMG / GM) gets to screen and
choose route instead; a round already booked or a route already chosen is not a
task; Finance gets no interview tabs; one broken tab never blanks the rest.

Run:  cd backend && python -m pytest tests/test_work_desk.py -q
"""
from __future__ import annotations

import importlib

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from datetime import datetime, timedelta, timezone


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
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity, OppType,
    InterviewEvent, PipelineStatus as PS, Requirement, RequirementStatus,
)
import services.work_desk as wd  # noqa: E402
from services.resumes import AI_L1_REQUESTED  # noqa: E402

TA = CurrentUser(id=1, username="ta", full_name="Gargee Joshi", roles={"TA"})
RMG = CurrentUser(id=2, username="rmg", full_name="Rita RMG", roles={"RMG"})
FINANCE = CurrentUser(id=3, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    # The two role-shaped tabs read other modules' answers; they have their own
    # tests. Here they only need to come back as lists.
    import services.dashboard_desk as dd
    import services.dashboards as dsh
    monkeypatch.setattr(dd, "upcoming", lambda _db, _u, days=7: {"days": days, "items": []})
    monkeypatch.setattr(dsh, "my_work", lambda _db, _u: {"items": [
        {"key": "leave_to_decide", "count": 2, "label": "leave applications", "path": "leave-applications",
         "urgency": "warning"}], "all_clear": False})
    try:
        yield s
    finally:
        s.close()


def _profile(db, stage=PS.SOURCING, *, owner=TA.id, screening=None, name="Ravi"):
    n = len(db.execute(select(Customer.id)).all())
    cust = Customer(name=f"C{n}")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id=f"OPP-{n}", title="Tester", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    cand = Candidate(first_name=name, email=f"r{n}@mail.com", phone="9999999999")
    db.add_all([opp, cand]); db.flush()
    db.add(Requirement(req_number=f"REQ-{n}", opportunity_id=opp.id, customer_id=cust.id,
                       title="Tester", no_of_positions=1, status=RequirementStatus.IN_PROGRESS,
                       created_by=1))
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=stage,
                         rmg_screening_status=screening, ta_owner_id=owner)
    db.add(p); db.flush()
    return p


def _tabs(db, user):
    return {t["key"]: t for t in wd.desk(db, user)["tabs"]}


def test_a_ta_gets_their_own_daily_tasks(db):
    fresh = _profile(db, name="Fresh")                                   # at Sourcing, never sent
    _profile(db, name="Someone else's", owner=99)                        # not mine
    asked = _profile(db, PS.RMG_REVIEW, screening="Shortlisted", name="Asked")
    db.add(CandidateProfileActivityLog(profile_id=asked.id, user_id=1, action_type="L1_REQUESTED"))
    booked = _profile(db, PS.RMG_REVIEW, screening="Shortlisted", name="Booked")
    db.add(CandidateProfileActivityLog(profile_id=booked.id, user_id=1, action_type="L1_REQUESTED"))
    db.add(InterviewEvent(profile_id=booked.id, kind="L1_Interview", status="Scheduled",
                          scheduled_at=datetime.now(timezone.utc) + timedelta(days=2)))
    ai = _profile(db, screening="Shortlisted", name="Ai")
    db.add(CandidateProfileActivityLog(profile_id=ai.id, user_id=1, action_type=AI_L1_REQUESTED))
    db.flush()
    tabs = _tabs(db, TA)
    assert list(tabs) == ["feedback", "ta_pending", "schedule_customer", "schedule_internal", "schedule_hr",
                          "sourcing", "upcoming", "queues"]
    assert [i["title"] for i in tabs["sourcing"]["items"]] == ["Fresh"]
    # "Pending activities" (30 Sep 2026): every schedule + sourcing item in ONE tab,
    # each saying which activity it is and the position it is for (My Tasks groups by it).
    pend = tabs["ta_pending"]["items"]
    assert sorted(i["title"] for i in pend) == ["Ai", "Asked", "Fresh"]
    assert {i["activity"] for i in pend} == {"L1 / L2 interviews", "Awaiting your call"}
    assert all(i["section"] and i["key"].startswith("pend:") for i in pend)
    assert tabs["ta_pending"]["count"] == 3
    assert tabs["sourcing"]["items"][0]["path"].startswith("requirements/")
    chips = sorted((i["title"], i["chip"], i["round_kind"]) for i in tabs["schedule_internal"]["items"])
    assert chips == [("Ai", "AI L1", "AI_L1"), ("Asked", "Technical L1", "L1_Interview")]   # booked L1 is not a task
    assert tabs["queues"]["count"] == 2 and tabs["queues"]["items"][0]["tone"] == "warn"
    assert fresh.id
    # The Dashboard shows tiles only; each opens the My Tasks page on its tab.
    assert tabs["schedule_internal"]["link"] == "my-tasks?tab=schedule_internal"


def test_a_screener_gets_the_screening_tabs(db):
    _profile(db, screening="Pending", name="Waiting", owner=None)
    _profile(db, screening="Shortlisted", name="Undecided", owner=None)
    chosen = _profile(db, screening="Shortlisted", name="Chosen", owner=None)
    db.add(CandidateProfileActivityLog(profile_id=chosen.id, user_id=1, action_type="L1_REQUESTED"))
    db.flush()
    tabs = _tabs(db, RMG)
    assert "screening" in tabs and "route" in tabs and "sourcing" not in tabs
    assert [i["title"] for i in tabs["screening"]["items"]] == ["Waiting"]
    assert [i["title"] for i in tabs["route"]["items"]] == ["Undecided"]
    # RMG / GM tiles open the Screening Desk on that task; the rest open My Tasks.
    assert tabs["screening"]["link"] == "screening-desk?task=screening"
    assert tabs["upcoming"]["link"] == "my-tasks?tab=upcoming"


def test_finance_gets_no_interview_tabs(db):
    assert list(_tabs(db, FINANCE)) == ["fin_timesheets", "fin_proformas", "fin_invoices",
                                    "fin_customer_approved", "upcoming", "queues"]


def test_one_broken_tab_never_blanks_the_desk(db, monkeypatch):
    monkeypatch.setattr(wd, "_sourcing_tab", lambda *a, **k: 1 / 0)
    assert "sourcing" not in _tabs(db, TA) and "schedule_customer" in _tabs(db, TA)


def test_customer_rounds_have_their_own_tab_with_the_sales_slots(db):
    """29 Sep 2026 report: a TA did not see a customer interview to book. The
    candidate was a colleague's that THIS TA sent for screening, and customer
    rounds were mixed into one list. Now: its own tab, the Sales slots on the
    item, and the TA who sent the candidate sees it too."""
    from services.candidate_profiles import CUSTOMER_SLOTS_PROPOSED, SENT_FOR_SCREENING
    p = _profile(db, PS.CUSTOMER_INTERVIEW, owner=99, name="Pushpa")
    db.add_all([
        CandidateProfileActivityLog(profile_id=p.id, user_id=TA.id, action_type=SENT_FOR_SCREENING),
        CandidateProfileActivityLog(profile_id=p.id, user_id=5, action_type=CUSTOMER_SLOTS_PROPOSED,
                                    comment="Customer L1 - Interview — slots offered by the customer:\n"
                                            "1. 30 Sep 2026, 10:00 AM IST — https://t/x\n(panel Anup, 45 min)"),
    ])
    hr = _profile(db, PS.HR_SCREENING, name="Hr")
    db.add(CandidateProfileActivityLog(profile_id=hr.id, user_id=7, action_type="HR_REQUESTED"))
    db.flush()
    tabs = _tabs(db, TA)
    [item] = tabs["schedule_customer"]["items"]
    assert (item["title"], item["round_kind"]) == ("Pushpa", "Customer_Interview")
    assert item["customer_slots"]["slots"][0]["scheduled_at"] == "2026-09-30T10:00"
    assert "customer offered 30 Sep 2026, 10:00 AM IST" in item["subtitle"]
    assert tabs["schedule_customer"]["label"] == "Customer interviews"
    assert [i["round_kind"] for i in tabs["schedule_hr"]["items"]] == ["HR_Interview"]
    # Another TA who neither applied nor sent the candidate does not see it.
    other = CurrentUser(id=42, username="t2", full_name="Other", roles={"TA"})
    assert not _tabs(db, other)["schedule_customer"]["items"]


def test_customer_l2_is_to_schedule_at_either_l2_stage():
    """29 Sep 2026: Sales asks for the L2 by moving to "Customer L2 Interview"
    (L2_Feedback) as well as from "Customer L1 Interview" (L1_Feedback)."""
    from services.work_desk import rounds_to_schedule
    for stage in ("L1_Feedback", "L2_Feedback"):
        assert rounds_to_schedule({}, stage, False) == ["Customer L2"]
        assert rounds_to_schedule({"cust_l2_scheduled": True}, stage, False) == []


def test_finance_sees_the_timesheet_proforma_invoice_chain(db):
    """29 Sep 2026: Finance's desk is the billing chain — approved sheets still
    waiting on the GM (coming up), Proformas to convert (your move), tax
    invoices issued in the last 30 days (done)."""
    from datetime import date
    from models import Employee, Invoice, Project, Timesheet, TimesheetStatus

    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Cluster")
    emp = Employee(first_name="Asha", last_name="Rao", email="asha@k.in")
    db.add_all([proj, emp]); db.flush()
    now = datetime.now(timezone.utc)

    def sheet(month):
        ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=2026, month=month,
                       status=TimesheetStatus.APPROVED, approved_at=now)
        db.add(ts); db.flush()
        return ts

    bare, returned, raised, issued = sheet(5), sheet(6), sheet(7), sheet(8)
    old = sheet(4)
    old.approved_at = now - timedelta(days=200)
    today = date.today()

    def doc(ts, number, kind, **kw):
        inv = Invoice(invoice_number=number, project_id=proj.id, timesheet_id=ts.id, invoice_date=today,
                      sub_total=100000, grand_total=118000, kind=kind, **kw)
        db.add(inv); db.flush()
        return inv

    doc(returned, "PI-2026-001", "Proforma", proforma_number="PI-2026-001", returned_at=now,
        returned_reason="wrong rate")
    pi = doc(raised, "PI-2026-002", "Proforma", proforma_number="PI-2026-002")
    doc(issued, "KRSW-26-27-10", "Tax", proforma_number="PI-2026-000")
    tabs = {t["key"]: t for t in wd.desk(db, FINANCE)["tabs"]}

    ts_tab = tabs["fin_timesheets"]
    assert sorted(i["path"] for i in ts_tab["items"]) == sorted(
        [f"timesheets/{bare.id}", f"timesheets/{returned.id}"])   # raised / issued sheets are further along
    chips = {i["path"]: i["chip"] for i in ts_tab["items"]}
    assert chips[f"timesheets/{returned.id}"] == "Returned to GM"
    assert chips[f"timesheets/{bare.id}"] == "Waiting for GM's Proforma"
    assert ts_tab["info"] is True and ts_tab["stage"] == "Coming up"      # the 200-day-old sheet is not listed

    pis = tabs["fin_proformas"]
    assert [i["path"] for i in pis["items"]] == [f"invoices/{pi.id}"]    # the returned one is with the GM
    assert pis["items"][0]["title"] == "PI-2026-002 · ₹1,18,000" and pis["info"] is False
    assert pis["items"][0]["action"] == "Generate tax invoice"

    done = tabs["fin_invoices"]
    assert [i["title"] for i in done["items"]] == ["KRSW-26-27-10 · ₹1,18,000"]
    assert "from PI-2026-000" in done["items"][0]["subtitle"] and done["stage"] == "Done"


def test_rupees_uses_indian_grouping():
    assert wd.rupees(123456789.4) == "₹12,34,56,789"
    assert wd.rupees(950) == "₹950"


SALES = CurrentUser(id=1, username="sanjana", full_name="Sanjana", roles={"Sales"})
SALES_HEAD = CurrentUser(id=5, username="sh", full_name="Head", roles={"Sales", "Sales_Head"})


def test_sales_get_a_tab_per_stage_with_the_moves_they_may_make(db, monkeypatch):
    """29 Sep 2026 (user ask): after Feedback due, one tab per stage Sales owns,
    each item carrying the moves the profile's Next-step bar would offer, so
    the work is done from the Dashboard / My Tasks without opening the profile."""
    import services.action_permissions as ap
    monkeypatch.setattr(ap, "user_may", lambda _db, u, action: (
        "Sales_Head" in u.roles if action == "profile.sales_head_decision"
        else action == "profile.budget_resolve"))
    from models import OfferHistory, OfferStatus
    _profile(db, PS.SALES_SCREENING, name="Cleared")
    _profile(db, PS.CUSTOMER_SCREENING, name="WithCustomer")
    passed = _profile(db, PS.L2_FEEDBACK, name="Passed")
    db.add(InterviewEvent(profile_id=passed.id, kind="Customer_L2", status="Completed", result="Hire"))
    booked = _profile(db, PS.L2_FEEDBACK, name="Booked")               # a round still to happen: not a decision
    db.add(InterviewEvent(profile_id=booked.id, kind="Customer_L2", status="Scheduled",
                          scheduled_at=datetime.now(timezone.utc) + timedelta(days=1)))
    short = _profile(db, PS.SHORTLISTED, name="Shortlisted")
    sent_back = _profile(db, PS.SHORTLISTED, name="SentBack")
    db.add(CandidateProfileActivityLog(profile_id=sent_back.id, user_id=5, action_type="OFFER_SENT_BACK"))
    approval = _profile(db, PS.CUSTOMER_APPROVAL, name="Approval")
    from datetime import date
    db.add(OfferHistory(profile_id=approval.id, status=OfferStatus.PENDING, ctc=1150000, offer_date=date.today(),
                        rate_unit="Yearly", rate_value=1150000))
    budget = _profile(db, PS.PREBOARDING, name="Budget")
    budget.budget_status, budget.budget_note = "Out_of_Budget", "Expects 14 L"
    db.flush()

    tabs = _tabs(db, SALES)
    keys = list(tabs)
    assert keys == ["feedback", "sales_submit", "sales_response", "sales_decide", "sales_terms",
                    "sales_budget", "sales_timesheets", "sales_invoices_pending", "sales_proformas",
                    "sales_invoices", "sales_collections", "renewals", "joining_soon"]
    # No generic Upcoming / My queues for a Sales login (29 Sep 2026, user ask).
    assert [i["title"] for i in tabs["sales_submit"]["items"]] == ["Cleared"]
    assert "Customer_Screening" in tabs["sales_submit"]["items"][0]["allowed"]
    decide = tabs["sales_decide"]["items"]
    assert [i["title"] for i in decide] == ["Passed"]
    assert "Shortlisted" in decide[0]["allowed"] and "Self_Withdrawn" not in decide[0]["allowed"]
    terms = {i["title"]: i for i in tabs["sales_terms"]["items"]}
    assert terms["SentBack"]["chip"] == "Sent back by Sales Head" and terms["SentBack"]["allowed"] == []
    assert terms["Shortlisted"]["status_label"] == "Customer Shortlisted"
    # "Submit to Sales Head" holds EVERYTHING Sales sends Sales Head, in sections (29 Sep 2026).
    assert tabs["sales_terms"]["label"] == "Submit to Sales Head"
    assert [i["section"] for i in tabs["sales_terms"]["items"]] == [
        wd.TERMS_SENT_BACK, wd.TERMS_TO_SUBMIT, wd.TERMS_WAITING]
    assert terms["Approval"]["action"] == "Open"
    assert tabs["sales_terms"]["count"] == 2                            # waiting is not Sales' move
    assert "sales_waiting" not in tabs
    assert tabs["sales_budget"]["items"][0]["hr_note"] == "Expects 14 L"
    assert short.id

    head = _tabs(db, SALES_HEAD)
    assert "sales_approval" in head and "sales_waiting" not in head
    # A real Sales Head approves terms; "Submit to Sales Head" is for Sales / Sales Manager.
    assert "sales_terms" not in head and "upcoming" not in head and "queues" not in head
    manager = CurrentUser(id=6, username="bs", full_name="Balasaheb", roles={"Sales", "Sales Manager"},
                          held_roles={"Sales Manager"})
    assert "sales_terms" in _tabs(db, manager)
    item = head["sales_approval"]["items"][0]
    assert item["offer"]["rate_unit"] == "Yearly" and item["action"] == "Review terms"


def test_terms_sent_back_and_budget_statuses():
    from services.candidate_status import StatusFacts, derive_status
    import services.candidate_profiles as cp
    import services.candidate_status as cs
    assert (cs.BUDGET_CONCERN, cs.BUDGET_OUT, cs.BUDGET_RESOLVED) == (
        cp.BUDGET_CONCERN, cp.BUDGET_OUT, cp.BUDGET_RESOLVED)
    assert derive_status(StatusFacts(pipeline_status="Shortlisted", terms_sent_back=True)).label == "Terms Sent Back"
    assert derive_status(StatusFacts(pipeline_status="Shortlisted")).label == "Customer Shortlisted"
    assert derive_status(StatusFacts(pipeline_status="Preboarding", budget_status="Out_of_Budget")).label \
        == "Out of Budget – With Sales"
    assert derive_status(StatusFacts(pipeline_status="Preboarding", budget_status="Resolved")).label \
        == "Budget Reply – With HR"


def test_pending_sales_head_approval_reaches_everyone_who_may_approve(db, monkeypatch):
    """29 Sep 2026: the arrival notice at Customer Approval goes to the Sales
    Head role AND every login whose template / custom role holds the approval
    (a Sales Manager / GM), never to the person who submitted."""
    import services.action_permissions as ap
    import services.candidate_profiles as cp
    import services.notify as nt
    sent = []
    monkeypatch.setattr(ap, "user_ids_who_may", lambda _db, action: [1, 7, 9]
                        if action == "profile.sales_head_decision" else [])
    monkeypatch.setattr(nt, "notify_role", lambda _db, role, *a, **k: sent.append((role, k.get("user_ids"))))
    p = _profile(db, PS.SHORTLISTED)
    cp._notify_stage_owner(db, p, PS.SHORTLISTED.value, PS.CUSTOMER_APPROVAL.value, "terms", SALES)
    assert sent == [("Sales_Head", [7, 9])]


HR = CurrentUser(id=1, username="vishal", full_name="Vishal", roles={"HR"})


def test_hr_gets_the_candidate_tail_and_the_people_tabs(db):
    """29 Sep 2026 (user ask): HR's desk is HR's work — HR Discussion, HR interviews,
    Pre-Onboarding, joining soon, joined recently, exits — not TA's or Sales' lists."""
    from datetime import date
    from models import Employee
    today = date.today()
    disc = _profile(db, PS.HR_SCREENING, name="Discuss")
    booked = _profile(db, PS.HR_INTERVIEWING, name="Booked")
    db.add(InterviewEvent(profile_id=booked.id, kind="HR_Interview", status="Scheduled",
                          scheduled_at=datetime.now(timezone.utc) + timedelta(hours=3),
                          meeting_link="https://teams/x"))
    pre = _profile(db, PS.PREBOARDING, name="Onboard")
    pre.customer_onboarding_date = today + timedelta(days=5)
    flagged = _profile(db, PS.PREBOARDING, name="Budget")
    flagged.budget_status = "Resolved"
    newbie = Employee(first_name="Newbie", email="n@k.in", date_of_joining=today - timedelta(days=3),
                      date_of_birth=(today + timedelta(days=2)).replace(year=1995))
    complete = Employee(first_name="Complete", email="c@k.in", date_of_joining=today - timedelta(days=400),
                        employee_code="K9", current_ctc=900000, role_title="Dev", reporting_manager_id=None,
                        date_of_birth=date(1990, 1, 1))
    db.add_all([
        newbie, complete,
        Employee(first_name="Future", email="f@k.in", date_of_joining=today + timedelta(days=10)),
        Employee(first_name="Leaving", email="l@k.in", date_of_joining=today - timedelta(days=900),
                 last_working_day=today + timedelta(days=4)),
    ])
    db.flush()
    complete.reporting_manager_id = newbie.id
    from models.leave import LeaveApplication
    from models.masters import LeavePolicyType
    lt = LeavePolicyType(name="Sick Leave"); db.add(lt); db.flush()
    db.add(LeaveApplication(employee_id=newbie.id, leave_type_id=lt.id, from_date=today + timedelta(days=1),
                            to_date=today + timedelta(days=2), days=2, status="Pending"))
    db.flush()
    tabs = _tabs(db, HR)
    assert list(tabs) == ["feedback", "hr_discussion", "hr_interviews", "hr_onboarding", "hr_leave",
                          "hr_records", "hr_joining", "hr_joined", "hr_exits", "hr_bench", "hr_celebrations"]
    # No generic Upcoming / My queues for an HR-only login (29 Sep 2026, user ask).
    assert "upcoming" not in tabs and "queues" not in tabs
    assert tabs["hr_leave"]["items"][0]["chip"] == "Starts in 1 d"
    assert "Sick Leave" in tabs["hr_leave"]["items"][0]["subtitle"]
    missing = {i["title"]: i["missing"] for i in tabs["hr_records"]["items"]}
    assert "Complete" not in missing and "CTC" in missing["Newbie"]
    assert {i["title"] for i in tabs["hr_bench"]["items"]} >= {"Newbie", "Complete"}
    assert [i["subtitle"] for i in tabs["hr_celebrations"]["items"]] == ["Birthday"]
    assert tabs["hr_discussion"]["items"][0]["action"] == "Request HR round"
    assert tabs["hr_interviews"]["items"][0]["meeting_link"] == "https://teams/x"
    chips = {i["title"]: i["chip"] for i in tabs["hr_onboarding"]["items"]}
    assert chips == {"Onboard": "Complete onboarding", "Budget": "Sales replied — your call"}
    assert [i["title"] for i in tabs["hr_joining"]["items"]] == ["Onboard", "Future"]
    assert [i["title"] for i in tabs["hr_joined"]["items"]] == ["Newbie"]
    assert tabs["hr_exits"]["items"][0]["chip"] == "Last day in 4 days"
    assert tabs["hr_joined"]["info"] is True and tabs["hr_discussion"].get("info") is False
    assert disc.id
    assert "sourcing" not in tabs and "schedule_customer" not in tabs


def test_sales_see_the_billing_chain_by_customer(db):
    """29 Sep 2026 (later), user ask: Sales' Dashboard gets "Timesheets pending",
    "Invoices pending" and "Invoices generated" (+ Payments to chase), every item
    sectioned by CUSTOMER. A plain Sales user sees their own deals' projects."""
    from datetime import date
    from models import Employee, Invoice, Project, Timesheet, TimesheetStatus

    mine = Opportunity(opp_id="OPP-B1", title="Mine", customer_id=None, opp_type="T&M", created_by=SALES.id)
    theirs = Opportunity(opp_id="OPP-B2", title="Theirs", customer_id=None, opp_type="T&M", created_by=99)
    harman, visteon = Customer(name="HARMAN"), Customer(name="VISTEON")
    db.add_all([harman, visteon]); db.flush()
    mine.customer_id, theirs.customer_id = harman.id, visteon.id
    db.add_all([mine, theirs]); db.flush()
    p1 = Project(customer_id=harman.id, name="Cluster", opportunity_id=mine.id)
    p2 = Project(customer_id=visteon.id, name="Radio", opportunity_id=theirs.id)
    emp = Employee(first_name="Asha", last_name="Rao", email="asha2@k.in")
    db.add_all([p1, p2, emp]); db.flush()
    today = date.today()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)

    def sheet(proj, status, month=m):
        ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=y, month=month, status=status,
                       approved_at=datetime.now(timezone.utc) if status == TimesheetStatus.APPROVED else None)
        db.add(ts); db.flush()
        return ts

    draft = sheet(p1, TimesheetStatus.DRAFT)
    sheet(p2, TimesheetStatus.DRAFT)                                   # a colleague's deal
    approved = sheet(p1, TimesheetStatus.APPROVED, month=max(1, m - 1))
    db.add(Invoice(invoice_number="KR-1", project_id=p1.id, invoice_date=today - timedelta(days=40),
                   due_date=today - timedelta(days=10), sub_total=100000, grand_total=118000,
                   balance_amount=118000, kind="Tax"))
    db.flush()

    # A Proforma with Finance on a colleague's deal, from the colleague's sheet.
    pe_sheet = sheet(p2, TimesheetStatus.APPROVED, month=max(1, m - 1))
    db.add(Invoice(invoice_number="PI-2026-001", proforma_number="PI-2026-001", project_id=p2.id,
                   timesheet_id=pe_sheet.id, invoice_date=today - timedelta(days=2),
                   sub_total=50000, grand_total=59000, balance_amount=59000, kind="Proforma"))
    db.flush()

    tabs = _tabs(db, SALES)
    # Every Sales rung has the three invoice tabs (30 Sep 2026, user ask) —
    # a plain Sales user's own deals; every item names the EMPLOYEE too.
    ts = tabs["sales_timesheets"]
    assert [i["path"] for i in ts["items"]] == [f"timesheets/{draft.id}"]
    assert ts["items"][0]["section"] == "HARMAN" and ts["count"] == 1
    assert ts["items"][0]["employee"] == "Asha Rao"
    assert [i["path"] for i in tabs["sales_invoices_pending"]["items"]] == [f"timesheets/{approved.id}"]
    assert tabs["sales_invoices_pending"]["items"][0]["employee"] == "Asha Rao"
    assert tabs["sales_proformas"]["items"] == []                       # the colleague's deal
    [iv] = tabs["sales_invoices"]["items"]
    assert iv["section"] == "HARMAN" and iv["chip"] == "Overdue" and iv["employee"] is None
    [due] = tabs["sales_collections"]["items"]
    assert due["chip"] == "10 days overdue"
    # The Sales Head (and a Sales Manager) see every customer's invoices.
    head = _tabs(db, SALES_HEAD)
    assert {i["section"] for i in head["sales_timesheets"]["items"]} == {"HARMAN", "VISTEON"}
    [pi] = head["sales_proformas"]["items"]
    assert pi["section"] == "VISTEON" and pi["employee"] == "Asha Rao" and pi["chip"].startswith("With Finance")
    assert pi["subtitle"] == "Radio · Asha Rao"
    manager = CurrentUser(id=6, username="bs", full_name="Bala", roles={"Sales", "Sales Manager"},
                          held_roles={"Sales Manager"})
    mgr = _tabs(db, manager)
    assert {i["section"] for i in mgr["sales_invoices"]["items"]} == {"HARMAN"}
    assert [i["section"] for i in mgr["sales_proformas"]["items"]] == ["VISTEON"]


def test_every_item_carries_filter_facets_and_my_tasks_gets_the_whole_list(db):
    """29 Sep 2026 (night), user ask: "filters in every tab for every role —
    customer wise, month wise, search". Billing items carry customer · project ·
    the month the work belongs to (a timesheet's PERIOD, an invoice's date);
    candidate items take their opportunity's customer; the Dashboard keeps 50
    per tab while My Tasks (`full`) gets everything, and the count is the real
    total either way."""
    from datetime import date
    from models import Employee, Invoice, Project, Timesheet, TimesheetStatus

    cust = Customer(name="APTIV")
    db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Radar")
    db.add(proj); db.flush()
    now = datetime.now(timezone.utc)
    sheets = []
    for i in range(60):                                         # more than one page of items
        emp = Employee(first_name=f"Mira{i}", email=f"mira{i}@k.in")
        db.add(emp); db.flush()
        ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=2026, month=(i % 12) + 1,
                       status=TimesheetStatus.APPROVED, approved_at=now)
        db.add(ts); sheets.append(ts)
    db.flush()
    db.add(Invoice(invoice_number="KR-9", project_id=proj.id, invoice_date=date.today(),
                   sub_total=1, grand_total=1, kind="Tax"))
    db.flush()

    short = {t["key"]: t for t in wd.desk(db, FINANCE)["tabs"]}["fin_timesheets"]
    assert short["count"] == 60 and len(short["items"]) == wd.MAX_ITEMS
    full = {t["key"]: t for t in wd.desk(db, FINANCE, max_items=wd.FULL_MAX_ITEMS)["tabs"]}
    items = full["fin_timesheets"]["items"]
    assert len(items) == 60
    first = next(i for i in items if i["path"] == f"timesheets/{sheets[2].id}")
    assert (first["customer"], first["project"], first["month"]) == ("APTIV", "Radar", "2026-03")
    [iv] = full["fin_invoices"]["items"]
    assert iv["customer"] == "APTIV" and iv["month"] == date.today().strftime("%Y-%m")

    # A candidate item takes the customer of its opportunity.
    p = _profile(db, PS.SOURCING, name="Kiran")
    src = {i["profile_id"]: i for i in _tabs(db, TA)["sourcing"]["items"]}[p.id]
    assert src["customer"] and src["customer"].startswith("C")


def test_each_sales_rung_gets_its_own_tiles(db):
    """29 Sep 2026 (user ask): Sales — + Renewals · Joining soon (every rung keeps the
    three invoice tiles since 30 Sep 2026);
    Sales Manager — + Team stuck points · Team timesheets not submitted · Team
    overdue collections (sectioned by salesperson); Sales Head — Pace vs target ·
    Stuck across the team · Lost this month · Top overdue collections · PO
    renewals, and no Submit to customer / Customer's response."""
    from datetime import date
    from models import Employee, Invoice, Project, PurchaseOrder, Timesheet, TimesheetStatus

    today = date.today()
    mine_c, other_c = Customer(name="HARMAN"), Customer(name="VISTEON")
    db.add_all([mine_c, other_c]); db.flush()
    mine = Opportunity(opp_id="OPP-R1", title="Mine", customer_id=mine_c.id, opp_type="T&M", created_by=SALES.id)
    theirs = Opportunity(opp_id="OPP-R2", title="Theirs", customer_id=other_c.id, opp_type="T&M", created_by=99)
    db.add_all([mine, theirs]); db.flush()
    db.add_all([
        PurchaseOrder(po_number="PO-MINE", customer_id=mine_c.id, end_date=today + timedelta(days=10),
                      total_value=100, balance_value=40, status="Active"),
        PurchaseOrder(po_number="PO-THEIRS", customer_id=other_c.id, end_date=today + timedelta(days=10),
                      total_value=100, balance_value=40, status="Active"),
        PurchaseOrder(po_number="PO-LATER", customer_id=mine_c.id, end_date=today + timedelta(days=200),
                      total_value=100, balance_value=40, status="Active"),
    ])
    joiner = _profile(db, PS.PREBOARDING, name="Joiner")
    joiner.customer_onboarding_date = today + timedelta(days=5)
    joiner_opp = db.get(Opportunity, joiner.opportunity_id)
    joiner_opp.created_by = SALES.id
    stuck = _profile(db, PS.CUSTOMER_SCREENING, name="Waiting")
    db.add(CandidateProfileActivityLog(profile_id=stuck.id, user_id=1, action_type="STATUS_CHANGE",
                                       timestamp=datetime.now(timezone.utc) - timedelta(days=6)))
    proj = Project(customer_id=other_c.id, name="Radio", opportunity_id=theirs.id)
    emp = Employee(first_name="Asha", last_name="Rao", email="asha9@k.in")
    db.add_all([proj, emp]); db.flush()
    y, m = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    db.add(Timesheet(project_id=proj.id, employee_id=emp.id, year=y, month=m, status=TimesheetStatus.DRAFT))
    db.add(Invoice(invoice_number="KR-9", project_id=proj.id, invoice_date=today - timedelta(days=40),
                   due_date=today - timedelta(days=10), sub_total=100000, grand_total=118000,
                   balance_amount=118000, kind="Tax"))
    db.flush()

    sales = _tabs(db, SALES)
    assert [i["title"].split(" · ")[0] for i in sales["renewals"]["items"]] == ["PO PO-MINE"]
    assert [i["title"] for i in sales["joining_soon"]["items"]] == ["Joiner"]
    assert not {"team_stuck", "head_pace", "upcoming"} & set(sales)
    assert {"sales_invoices_pending", "sales_proformas", "sales_invoices"} <= set(sales)

    manager = CurrentUser(id=6, username="bs", full_name="Bala", roles={"Sales", "Sales Manager"},
                          held_roles={"Sales Manager"})
    mgr = _tabs(db, manager)
    assert {"renewals", "joining_soon", "team_stuck", "team_timesheets", "team_collections"} <= set(mgr)
    assert {"sales_invoices_pending", "sales_proformas", "sales_invoices"} <= set(mgr) and "head_pace" not in mgr
    assert {i["title"].split(" · ")[0] for i in mgr["renewals"]["items"]} >= {"PO PO-MINE", "PO PO-THEIRS"}
    assert [i["title"] for i in mgr["team_stuck"]["items"]] == ["Waiting"]
    [sheet] = mgr["team_timesheets"]["items"]
    assert sheet["chip"].startswith("Not submitted") and sheet["section"]      # the salesperson
    assert mgr["team_collections"]["items"][0]["chip"] == "10 days overdue"

    head = _tabs(db, SALES_HEAD)
    assert {"head_pace", "head_stuck", "head_lost", "head_collections", "head_po_renewals"} <= set(head)
    assert not {"sales_submit", "sales_response", "sales_collections", "sales_terms", "renewals"} & set(head)
    assert {"sales_invoices_pending", "sales_proformas", "sales_invoices"} <= set(head)
    assert head["head_collections"]["items"][0]["title"] == "VISTEON · ₹1,18,000"
    assert {i["title"].split(" · ")[0] for i in head["head_po_renewals"]["items"]} == {"PO PO-MINE", "PO PO-THEIRS"}


def test_admin_and_ceo_get_only_their_own_decisions(db, monkeypatch):
    """30 Sep 2026 (user ask, screenshot: "why every role's tab on the CEO
    dashboard — just his own"): Admin / CEO get `CEO_TABS` ONLY — the deals
    awaiting the Sales Head's approval and the candidate terms awaiting the
    top sign-off — never the TA / screener / Finance / HR / generic tabs."""
    import services.action_permissions as ap
    monkeypatch.setattr(ap, "user_may", lambda _db, u, action: True)
    monkeypatch.setattr(ap, "screens_as_rmg", lambda _db, u: True)
    from models import OpportunityApprovalStatus
    ceo = CurrentUser(id=8, username="ceo", full_name="Karan", roles={"CEO", "Admin"})
    _profile(db, name="Fresh")                                           # TA's work — not the CEO's
    _profile(db, screening="Pending", name="ToScreen")                   # RMG's work — not the CEO's
    terms = _profile(db, PS.CUSTOMER_APPROVAL, name="Terms")
    pending = db.get(Opportunity, terms.opportunity_id)
    pending.approval_status = OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL
    pending.updated_at = datetime.now(timezone.utc) - timedelta(days=4)
    db.flush()

    tabs = _tabs(db, ceo)
    assert tuple(tabs) == wd.CEO_TABS == ("opp_approvals", "sales_approval")
    [deal] = tabs["opp_approvals"]["items"]
    assert deal["path"] == f"opportunities/{pending.id}" and deal["action"] == "Review & approve"
    assert deal["chip"] == "Waiting 4 days" and deal["customer"] and tabs["opp_approvals"]["stage"] == "Your approval"
    assert [i["title"] for i in tabs["sales_approval"]["items"]] == ["Terms"]
    assert tabs["sales_approval"]["items"][0]["action"] == "Review terms"
