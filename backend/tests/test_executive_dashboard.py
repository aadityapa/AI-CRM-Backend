"""CEO dashboard tabs (28 Sep 2026) — `services.executive_dashboard`.

One small book on in-memory SQLite: two customers, two projects, three people
(one redeployed internal, one fresh external, one bench), Tax + Proforma
invoices, a payment, requirements with Joined profiles linked to employees,
one resignation. Pins the definitions each tab prints and that the tabs are
COMPOSED from the report modules (same numbers, no second rule).
"""
from __future__ import annotations

import importlib
import inspect
from datetime import date, datetime, timedelta, timezone

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
    Candidate, CandidateProfile, CandidateProfileActivityLog, Customer, Employee, Invoice,
    InvoicePayment, Opportunity, OpportunityApprovalStatus, OppType, POStatus, PaymentStatus,
    PipelineStage, PipelineStatus, Project, ProjectEmployee, PurchaseOrder, Requirement,
    RequirementStatus,
)
from models.finance import InvoiceKind  # noqa: E402
from services import executive_dashboard as ed  # noqa: E402
from services.hiring_dashboard import hiring_dashboard  # noqa: E402
from services.revenue_report import revenue_report  # noqa: E402

TODAY = date(2026, 9, 28)      # inside FY 2026-27 (Apr 2026 – Mar 2027)


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc)


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


def _invoice(db, project, number, on, sub_total, tax=0.0, paid=0.0, kind=InvoiceKind.TAX,
             timesheet=None, due=None):
    inv = Invoice(invoice_number=number, project_id=project.id, invoice_date=on, due_date=due,
                  sub_total=sub_total, tax_amount=tax, grand_total=sub_total + tax,
                  paid_amount=paid, balance_amount=sub_total + tax - paid, kind=kind.value,
                  timesheet_id=timesheet.id if timesheet else None,
                  payment_status=PaymentStatus.PAID if paid >= sub_total + tax else PaymentStatus.UNPAID)
    db.add(inv); db.flush()
    return inv


def _joined(db, opp, *, on: date, name: str, employee: Employee | None = None):
    cand = Candidate(first_name=name, email=f"{name.lower()}@x.in")
    db.add(cand); db.flush()
    p = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                         pipeline_status=PipelineStatus.JOINED,
                         created_at=_ts(on - timedelta(days=30)), updated_at=_ts(on))
    db.add(p); db.flush()
    db.add(CandidateProfileActivityLog(profile_id=p.id, user_id=1, action_type="STATUS_CHANGE",
                                       comment="Preboarding -> Joined", timestamp=_ts(on)))
    if employee is not None:
        employee.candidate_profile_id = p.id
    db.flush()
    return p


@pytest.fixture()
def book(db):
    from models import Timesheet, TimesheetStatus

    harman = Customer(name="HARMAN"); aptiv = Customer(name="APTIV")
    db.add_all([harman, aptiv]); db.flush()
    info = Project(customer_id=harman.id, name="Infotainment")
    adas = Project(customer_id=aptiv.id, name="ADAS")
    db.add_all([info, adas]); db.flush()

    # Asha: on Karnex rolls since 2023, placed on ADAS then redeployed to Infotainment → INTERNAL.
    asha = Employee(first_name="Asha", last_name="Rao", email="asha@karnex.in", employee_code="K001",
                    date_of_joining=date(2023, 1, 10), current_ctc=1_200_000)
    # Bala: joined Karnex the day he was placed → EXTERNAL.
    bala = Employee(first_name="Bala", email="bala@karnex.in", employee_code="K002",
                    date_of_joining=date(2026, 6, 1), current_ctc=600_000)
    # Chitra: on the bench (no live assignment), resigned with a last day next month.
    chitra = Employee(first_name="Chitra", email="chitra@karnex.in", employee_code="K003",
                      date_of_joining=date(2025, 4, 1), current_ctc=900_000,
                      is_resigned=True, date_of_resignation=date(2026, 9, 15),
                      last_working_day=date(2026, 10, 14))
    # Dev: left in June this FY.
    dev = Employee(first_name="Dev", email="dev@karnex.in", employee_code="K004",
                   date_of_joining=date(2024, 2, 1), current_ctc=800_000, is_active=False,
                   is_resigned=True, last_working_day=date(2026, 6, 30))
    db.add_all([asha, bala, chitra, dev]); db.flush()

    pe_asha_old = ProjectEmployee(project_id=adas.id, employee_id=asha.id, billing_rate=150_000,
                                  billing_unit="Monthly", onboarding_date=date(2024, 1, 1),
                                  exit_date=date(2026, 3, 31), is_exit=True)
    pe_asha = ProjectEmployee(project_id=info.id, employee_id=asha.id, billing_rate=200_000,
                              billing_unit="Monthly", onboarding_date=date(2026, 4, 1))
    pe_bala = ProjectEmployee(project_id=info.id, employee_id=bala.id, billing_rate=1_000,
                              billing_unit="Hourly", onboarding_date=date(2026, 6, 1))
    db.add_all([pe_asha_old, pe_asha, pe_bala]); db.flush()

    ts_asha = Timesheet(project_id=info.id, employee_id=asha.id, month=8, year=2026,
                        status=TimesheetStatus.APPROVED)
    ts_bala = Timesheet(project_id=info.id, employee_id=bala.id, month=8, year=2026,
                        status=TimesheetStatus.APPROVED)
    db.add_all([ts_asha, ts_bala]); db.flush()

    i1 = _invoice(db, info, "INV-1", date(2026, 8, 31), 200_000, 36_000, timesheet=ts_asha,
                  due=date(2026, 9, 30))
    i2 = _invoice(db, info, "INV-2", date(2026, 8, 31), 160_000, 28_800, timesheet=ts_bala,
                  paid=188_800, due=date(2026, 9, 30))
    db.add(InvoicePayment(invoice_id=i2.id, payment_date=date(2026, 9, 5), amount=188_800))
    _invoice(db, adas, "INV-3", date(2026, 5, 15), 100_000, 18_000, due=date(2026, 6, 15))  # manual, overdue
    _invoice(db, adas, "INV-OLD", date(2026, 2, 10), 300_000, 54_000, paid=354_000)          # previous FY
    _invoice(db, info, "PI-9", date(2026, 9, 20), 999_999, 0, kind=InvoiceKind.PROFORMA)     # never counted
    db.add(InvoicePayment(invoice_id=i1.id, payment_date=date(2026, 9, 10), amount=100_000))
    i1.paid_amount = 100_000; i1.balance_amount = 236_000 - 100_000
    db.add(PurchaseOrder(po_number="PO-1", customer_id=harman.id, total_value=3_000_000,
                         consumed_value=360_000, balance_value=2_640_000, status=POStatus.ACTIVE))

    opp = Opportunity(opp_id="OPP-1", title="Infotainment engineers", customer_id=harman.id,
                      opp_type=OppType.T_AND_M, created_by=1, pipeline_stage=PipelineStage.ACTIVE,
                      approval_status=OpportunityApprovalStatus.APPROVED,
                      created_at=_ts(date(2026, 4, 20)), updated_at=_ts(date(2026, 4, 20)))
    opp2 = Opportunity(opp_id="OPP-2", title="ADAS lead", customer_id=aptiv.id,
                       opp_type=OppType.T_AND_M, created_by=1, pipeline_stage=PipelineStage.ACTIVE,
                       approval_status=OpportunityApprovalStatus.APPROVED,
                       created_at=_ts(date(2025, 11, 1)), updated_at=_ts(date(2025, 11, 1)))
    db.add_all([opp, opp2]); db.flush()
    db.add(Requirement(req_number="REQ-1", opportunity_id=opp.id, customer_id=harman.id,
                       title=opp.title, no_of_positions=3, status=RequirementStatus.IN_PROGRESS,
                       created_by=1, created_at=_ts(date(2026, 4, 22)), updated_at=_ts(date(2026, 4, 22))))
    db.add(Requirement(req_number="REQ-2", opportunity_id=opp2.id, customer_id=aptiv.id,
                       title=opp2.title, no_of_positions=1, status=RequirementStatus.OPEN_FOR_SOURCING,
                       created_by=1, created_at=_ts(date(2025, 11, 3)), updated_at=_ts(date(2025, 11, 3))))
    db.flush()
    _joined(db, opp, on=date(2026, 4, 1), name="Asha", employee=asha)
    _joined(db, opp, on=date(2026, 6, 1), name="Bala", employee=bala)
    db.commit()
    return db


def _tab(db, tab, **kw):
    return ed.executive_dashboard(db, tab, "2026-09", kw.pop("period", "fy"), today=TODAY)


# --------------------------------------------------------------------------- contract


def test_period_defaults_to_the_financial_year_and_month_is_the_anchor(book):
    r = _tab(book, "finance", period=None)
    assert r["period"]["kind"] == "fy" and r["period"]["key"] == "FY2026"
    assert r["period"]["start"] == "2026-04-01" and r["period"]["end"] == "2027-03-31"
    assert r["period"]["is_current"] is True and r["period"]["fy_label"] == "FY 2026-27"
    q = _tab(book, "finance", period="quarter")
    assert q["period"]["key"] == "2026-Q2"


def test_unknown_tab_is_a_value_error(book):
    with pytest.raises(ValueError):
        ed.executive_dashboard(book, "hr", today=TODAY)


def test_route_is_admin_only_and_tab_driven():
    from routers.crm import dashboards
    src = inspect.getsource(dashboards.ceo_dashboard)
    assert "role_required()" in src and 'Query("finance"' in src


def test_the_people_route_is_hrs_desk_and_serves_the_people_tab_only():
    """28 Sep 2026 — HR gets the CEO's People tab on its own dashboard, behind the
    Employees tab grant; it can never reach Finance / Customer / Sales."""
    from routers.crm import dashboards
    src = inspect.getsource(dashboards.people_dashboard)
    assert 'gated_read("employees", "HR")' in src
    assert 'executive_dashboard(db, "people"' in src and "tab" not in src.split("def people_dashboard")[1].split("):")[0]


# --------------------------------------------------------------------------- finance


def test_finance_tab_is_the_revenue_report_plus_a_month_by_month_cut(book):
    r = _tab(book, "finance")["data"]
    rep = revenue_report(book, "2026-09", TODAY, "fy")
    assert r["headline"] == rep["headline"], "same module, same numbers"
    # FY billed = INV-1 200k + INV-2 160k + INV-3 100k; the Proforma and last FY are out.
    assert r["headline"]["billed"] == 460_000 and r["headline"]["collected"] == 288_800
    assert len(r["monthly"]) == 12 and r["monthly"][0]["key"] == "2026-04"
    aug = next(m for m in r["monthly"] if m["key"] == "2026-08")
    assert aug["billed"] == 360_000 and aug["collected"] == 0
    assert aug["cost"] == pytest.approx((1_200_000 + 600_000) / 12, abs=1)
    assert aug["margin"] == pytest.approx(360_000 - 150_000, abs=1)
    sep = next(m for m in r["monthly"] if m["key"] == "2026-09")
    assert sep["billed"] == 0 and sep["collected"] == 288_800, "a Proforma is not billing"
    assert all(m["future"] for m in r["monthly"] if m["key"] >= "2026-10")
    assert r["top_customers"][0]["customer"] == "HARMAN" and r["concentration"]["risk"] is True
    assert {"ageing", "cashflow", "targets", "collections", "alerts", "by_type"} <= set(r)


def test_fy_zoom_counts_only_the_months_that_have_happened(book):
    """At the FY zoom the period ends next March; months after today are NOT elapsed."""
    t = revenue_report(book, "2026-09", TODAY, "fy")["targets"]
    assert (t["fy_months_elapsed"], t["fy_months_remaining"]) == (6, 6)
    assert t["fy_billed_to_date"] == 460_000
    assert t["fy_projection"] > t["fy_billed_to_date"], "trailing average × the 6 months left"
    past = revenue_report(book, "2025-09", TODAY, "fy")["targets"]
    assert (past["fy_months_elapsed"], past["fy_months_remaining"]) == (12, 0)


def test_revenue_report_no_longer_counts_proformas(book):
    """The 25 Sep note left this open; decided 28 Sep 2026 — every figure is over ISSUED Tax invoices."""
    rep = revenue_report(book, "2026-09", TODAY, "month")
    assert rep["headline"]["billed"] == 0 and rep["headline"]["outstanding"] == 136_000 + 118_000


# --------------------------------------------------------------------------- customer


def test_customer_tab_drills_customer_to_project_to_employee(book):
    r = _tab(book, "customer")["data"]
    assert [c["customer"] for c in r["customers"]] == ["HARMAN", "APTIV"]
    h = r["customers"][0]
    assert h["billed"] == 360_000 and h["share_pct"] == pytest.approx(78.3, abs=0.1)
    assert h["collected"] == 288_800 and h["outstanding"] == 136_000 and h["overdue"] == 0
    assert h["po_balance"] == 2_640_000
    assert h["open_positions"] == 1 and h["total_positions"] == 3 and h["joined_positions"] == 2
    assert h["heads"] == 2 and h["heads_today"] == 2 and h["internal"] == 1 and h["external"] == 1
    p = h["projects"][0]
    assert p["project"] == "Infotainment" and p["billed"] == 360_000 and p["share_pct"] == 100.0
    assert p["unlinked_billed"] == 0
    emps = {e["employee"]: e for e in p["employees"]}
    assert emps["Asha Rao"]["billed"] == 200_000 and emps["Asha Rao"]["kind"] == "internal"
    assert "Redeployed" in emps["Asha Rao"]["kind_reason"]
    assert emps["Bala"]["billed"] == 160_000 and emps["Bala"]["kind"] == "external"
    assert emps["Bala"]["monthly_rate"] == 1_000 * 21 * 8 and emps["Bala"]["unit"] == "Hourly"
    a = r["customers"][1]
    assert a["billed"] == 100_000 and a["overdue"] == 118_000 and a["previous"] == 300_000
    assert a["change_pct"] == pytest.approx(-66.7, abs=0.1)
    ap = a["projects"][0]
    assert ap["unlinked_billed"] == 100_000, "a manual invoice stays on the project, never dropped"
    assert ap["heads"] == 0, "Asha's ADAS assignment ended before this FY"


def test_customer_summary_and_donut(book):
    r = _tab(book, "customer")["data"]
    s = r["summary"]
    assert s["billed"] == 460_000 and s["customers_billing"] == 2
    assert s["top1_share_pct"] == pytest.approx(78.3, abs=0.1) and s["concentration_risk"] is True
    assert s["heads"] == 2 and s["heads_today"] == 2
    assert [d["label"] for d in r["donut"]] == ["HARMAN", "APTIV"]


# --------------------------------------------------------------------------- sales


def test_sales_tab_reuses_the_hiring_tower_and_adds_positions_with_the_mix(book):
    r = _tab(book, "sales")["data"]
    tower = hiring_dashboard(book, "2026-09", "fy", TODAY)
    assert r["kpis"] == tower["kpis"] and r["pace"] == tower["pace"], "same module, same numbers"
    assert r["kpis"]["pipeline_positions"] == 3 and r["kpis"]["onboardings"] == 2
    assert r["onboarding_mix"] == {"internal": 1, "external": 1, "unknown": 0, "total": 2,
                                   "internal_pct": 50.0}
    pos = {p["opp_id"]: p for p in r["positions"]}
    harman = pos["OPP-1"]
    assert harman["positions"] == 3 and harman["joined"] == 2 and harman["open"] == 1
    assert harman["internal"] == 1 and harman["external"] == 1 and harman["joined_in_period"] == 2
    assert [c["name"] for c in harman["candidates"]] == ["Asha", "Bala"]
    assert harman["candidates"][0]["kind"] == "internal" and harman["candidates"][1]["kind"] == "external"
    assert harman["fill_pct"] == pytest.approx(66.7, abs=0.1) and harman["live"] is True
    aptiv = pos["OPP-2"]
    assert aptiv["open"] == 1 and aptiv["created_in_period"] is False, "open today → shown even if older"
    assert aptiv["workable"] is True and aptiv["age_days"] > 300
    assert r["positions_summary"] == {"shown": 2, "live": 2, "open": 2, "total": 4, "joined": 2,
                                      "stale_30": 2}
    jun = next(m for m in r["monthly"] if m["key"] == "2026-06")
    assert jun["onboardings"] == 1 and jun["external"] == 1
    apr = next(m for m in r["monthly"] if m["key"] == "2026-04")
    assert apr["positions_in"] == 3 and apr["internal"] == 1


def test_onboarding_trend_at_three_zooms(book):
    t = _tab(book, "sales")["data"]["onboarding_trend"]
    assert [m["key"] for m in t["month"]][:2] == ["2026-04", "2026-05"] and len(t["month"]) == 12
    assert next(m for m in t["month"] if m["key"] == "2026-04") == {
        "key": "2026-04", "label": "Apr 26", "onboardings": 1, "positions_in": 3, "internal": 1, "external": 0,
        "unknown": 0, "future": False}
    assert len(t["quarter"]) == 8 and t["quarter"][-1]["key"] == "2026-Q2"      # Jul–Sep holds the anchor
    q1 = next(q for q in t["quarter"] if q["key"] == "2026-Q1")                # Apr–Jun: Asha + Bala
    assert (q1["onboardings"], q1["internal"], q1["external"]) == (2, 1, 1)
    assert len(t["fy"]) == 5 and t["fy"][-1]["key"] == "FY2026"
    assert t["fy"][-1]["onboardings"] == 2 and t["fy"][-2]["onboardings"] == 0
    assert all(not b["future"] for b in t["fy"]) and any(m["future"] for m in t["month"])


def test_a_joined_candidate_without_an_employee_record_is_unknown_not_guessed(book):
    opp = book.query(Opportunity).filter_by(opp_id="OPP-2").one()
    _joined(book, opp, on=date(2026, 9, 1), name="Ghost")
    book.commit()
    r = _tab(book, "sales")["data"]
    assert r["onboarding_mix"]["unknown"] == 1 and r["onboarding_mix"]["internal_pct"] == 50.0
    ghost = next(c for p in r["positions"] for c in p["candidates"] if c["name"] == "Ghost")
    assert ghost["kind"] == "unknown" and "No employee record" in ghost["reason"]


# --------------------------------------------------------------------------- people


def test_people_tab_headcount_bench_and_movements(book):
    r = _tab(book, "people")["data"]
    h = r["headline"]
    # On the rolls today: Asha, Bala, Chitra (last day 14 Oct). Dev left in June.
    assert h["headcount"] == 3 and h["deployed"] == 2 and h["bench"] == 1
    assert h["utilisation_pct"] == pytest.approx(66.7, abs=0.1)
    assert h["bench_cost_month"] == 75_000 and h["on_notice"] == 1
    assert h["joiners"] == 1 and h["exits"] == 1 and h["net_change"] == 0   # Bala in, Dev out
    assert h["opening_headcount"] == 3                                        # Asha, Chitra, Dev on 1 Apr
    assert h["attrition_pct"] == pytest.approx(33.3, abs=0.1)
    assert h["billed"] == 460_000 and h["revenue_per_deployed_head"] == 230_000
    assert [b["employee"] for b in r["bench"]] == ["Chitra"]
    assert r["on_notice"][0]["exit_on"] == "2026-10-14"
    assert [e["employee"] for e in r["exits"]] == ["Dev"] and [j["employee"] for j in r["joiners"]] == ["Bala"]
    jun = next(m for m in r["monthly"] if m["key"] == "2026-06")
    assert jun["joiners"] == 1 and jun["exits"] == 1
    assert jun["headcount"] == 4, "Dev's LAST WORKING DAY is 30 Jun — still on the rolls that day"
    assert next(m for m in r["monthly"] if m["key"] == "2026-07")["headcount"] == 3
    assert next(m for m in r["monthly"] if m["key"] == "2027-01")["headcount"] is None
    oct_ = next(m for m in r["monthly"] if m["key"] == "2026-10")
    assert oct_["exits"] == 1 and oct_["future"] is True, "Chitra's planned last day shows in its month"


def test_every_tab_answers_on_an_empty_book(db):
    for tab in ed.TABS:
        r = ed.executive_dashboard(db, tab, "2026-09", "fy", today=TODAY)
        assert r["tab"] == tab and isinstance(r["data"], dict)
    assert ed.executive_dashboard(db, "customer", "2026-09", "fy", today=TODAY)["data"]["customers"] == []
    assert ed.executive_dashboard(db, "people", "2026-09", "fy", today=TODAY)["data"]["headline"]["headcount"] == 0


# --------------------------------------------------------------------------- "same view as Finance" (28 Sep, later)


def test_customer_tab_unfolds_month_by_month_with_the_top_customers_as_series(book):
    r = _tab(book, "customer")["data"]
    assert r["monthly_series"] == ["HARMAN", "APTIV"], "top billers are their own series, no Others when all fit"
    aug = next(m for m in r["monthly"] if m["key"] == "2026-08")
    assert aug["billed"] == 360_000 and aug["customers"] == {"HARMAN": 360_000}
    may = next(m for m in r["monthly"] if m["key"] == "2026-05")
    assert may["customers"] == {"APTIV": 100_000} and may["cost"] == pytest.approx(100_000, abs=1)
    sep = next(m for m in r["monthly"] if m["key"] == "2026-09")
    assert sep["billed"] == 0 and sep["collected"] == 288_800 and sep["customers"] == {}
    h = r["customers"][0]
    assert h["monthly_burn"] == 200_000 + 1_000 * 21 * 8, "today's deployed heads at their monthly rate"
    assert h["po_cover_months"] == pytest.approx(2_640_000 / 368_000, abs=0.05)
    assert r["customers"][1]["monthly_burn"] == 0 and r["customers"][1]["po_cover_months"] is None
    keys = {a["key"] for a in r["alerts"]}
    assert "concentration" in keys and "overdue:%d" % r["customers"][1]["customer_id"] in keys
    assert "drop:%d" % r["customers"][1]["customer_id"] in keys, "APTIV is down 67% on the previous FY"
    assert not any(k.startswith("po_cover") for k in keys), "HARMAN's PO covers 7 months"


def test_sales_tab_trend_carries_positions_in_and_rolls_live_positions_up_by_customer(book):
    r = _tab(book, "sales")["data"]
    apr = next(m for m in r["onboarding_trend"]["month"] if m["key"] == "2026-04")
    assert apr["positions_in"] == 3 and apr["onboardings"] == 1
    assert r["onboarding_trend"]["fy"][-1]["positions_in"] == 3
    assert r["onboarding_trend"]["quarter"][-2]["positions_in"] == 3, "Apr–Jun holds REQ-1"
    by = {c["customer"]: c for c in r["by_customer"]}
    assert by["HARMAN"] == {"customer_id": by["HARMAN"]["customer_id"], "customer": "HARMAN", "positions": 3, "open": 1,
                            "joined": 2, "internal": 1, "external": 1, "opportunities": 1, "stale": 1}
    assert by["APTIV"]["open"] == 1 and by["APTIV"]["stale"] == 1
    keys = {a["key"] for a in r["alerts"]}
    assert "stale_positions" in keys and "unknown_kind" not in keys


def test_people_tab_unfolds_deployed_bench_and_cost_by_month(book):
    r = _tab(book, "people")["data"]
    jun = next(m for m in r["monthly"] if m["key"] == "2026-06")
    assert (jun["headcount"], jun["deployed"], jun["bench"]) == (4, 2, 2), "Asha + Bala deployed; Chitra, Dev on the bench"
    assert jun["cost"] == pytest.approx((1_200_000 + 600_000 + 900_000 + 800_000) / 12, abs=1)
    assert jun["bench_cost"] == pytest.approx((900_000 + 800_000) / 12, abs=1)
    sep = next(m for m in r["monthly"] if m["key"] == "2026-09")
    assert (sep["headcount"], sep["deployed"], sep["bench"]) == (3, 2, 1)
    assert next(m for m in r["monthly"] if m["key"] == "2027-01")["deployed"] is None
    h = r["headline"]
    assert h["people_cost_month"] == pytest.approx(2_700_000 / 12, abs=1)
    assert h["bench_cost_pct"] == pytest.approx(75_000 * 100 / 225_000, abs=0.1)
    assert r["by_role"] == [{"role": "No designation", "deployed": 2, "bench": 1, "on_notice": 1, "cost": 225_000.0}]
    assert [t["label"] for t in r["tenure"]] == ["< 1 yr", "1–2 yrs", "2–4 yrs", "4+ yrs"]
    assert r["tenure"][0] == {"label": "< 1 yr", "deployed": 1, "bench": 0}      # Bala
    assert r["tenure"][1] == {"label": "1–2 yrs", "deployed": 0, "bench": 1}     # Chitra
    assert r["tenure"][2] == {"label": "2–4 yrs", "deployed": 1, "bench": 0}     # Asha
    keys = {a["key"] for a in r["alerts"]}
    assert {"bench_cost", "attrition", "on_notice"} <= keys and "no_ctc" not in keys
    assert isinstance(r["rolloffs_by_month"], list)


def test_customer_tab_reports_deployed_heads_over_time_by_customer_and_location(book):
    from models import CustomerBranch
    r = _tab(book, "customer")["data"]
    t = r["deployed_trend"]
    assert [m["key"] for m in t["month"]][:2] == ["2026-04", "2026-05"] and len(t["month"]) == 12
    apr = next(m for m in t["month"] if m["key"] == "2026-04")
    assert apr == {"key": "2026-04", "label": "Apr 26", "future": False, "total": 1,
                   "customers": {"HARMAN": 1}, "locations": {"Unspecified": 1}}, "Asha alone until Bala joins in June"
    jun = next(m for m in t["month"] if m["key"] == "2026-06")
    assert jun["total"] == 2 and jun["customers"] == {"HARMAN": 2}
    assert next(m for m in t["month"] if m["key"] == "2027-01")["future"] is True
    # Quarter / FY zooms look BACK: Asha was on ADAS (APTIV) until 31 Mar 2026.
    q4_last_fy = next(q for q in t["quarter"] if q["key"] == "2025-Q4")
    assert q4_last_fy["customers"] == {"APTIV": 1} and q4_last_fy["total"] == 1
    assert t["fy"][-1]["total"] == 2 and t["fy"][-2]["customers"] == {"APTIV": 1}
    assert r["deployed_series"] == {"customers": ["HARMAN", "APTIV"], "locations": ["Unspecified"]}
    # A delivery branch gives the location; the opportunity's work location is the fallback.
    branch = CustomerBranch(customer_id=r["customers"][0]["customer_id"], branch_name="Whitefield", city="Bengaluru")
    book.add(branch); book.flush()
    proj = book.get(Project, r["customers"][0]["projects"][0]["project_id"]); proj.branch_id = branch.id
    book.commit()
    t2 = _tab(book, "customer")["data"]
    assert t2["deployed_trend"]["month"][2]["locations"] == {"Bengaluru": 2}
    assert t2["deployed_series"]["locations"] == ["Bengaluru", "Unspecified"]


def test_the_people_tab_carries_internal_vs_external_placements_without_money():
    """29 Sep 2026 (user ask): HR sees internal vs external placements as bars on its
    own dashboard — the Revenue page's rule, minus every billed figure."""
    import inspect
    import services.executive_dashboard as ed
    src = inspect.getsource(ed._people_placements)
    assert "placements_report(" in src and "billed_" in src
    rep = {"window": {}, "headline": {"internal": 2}, "series": [], "rules": {"revenue": "x", "internal": "y"},
           "by_customer": [{"customer": "V", "internal": 1, "billed_internal": 9.0}],
           "rows": [{"employee": "A", "kind": "internal", "billed": 5.0}], "rows_truncated": False}
    import services.executive_dashboard as mod
    orig = mod.placements_report
    try:
        mod.placements_report = lambda *a, **k: rep
        from services.revenue_report import Period
        from datetime import date
        out = mod._people_placements(None, Period.parse("fy", "2026-09", date(2026, 9, 29)), date(2026, 9, 29))
    finally:
        mod.placements_report = orig
    assert out["by_customer"] == [{"customer": "V", "internal": 1}]
    assert out["rows"] == [{"employee": "A", "kind": "internal"}]
    assert "revenue" not in out["rules"]
