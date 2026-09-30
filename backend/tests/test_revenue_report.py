"""CEO revenue report (18 Sep 2026) — `services.revenue_report`.

Builds a small ledger on in-memory SQLite (two customers, three months of
invoices, part payments, an unbilled approved timesheet, an active PO) and
checks every section the CEO page renders, plus the admin-only gate.
"""
from __future__ import annotations

import importlib
from datetime import date

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
    Customer, Employee, Invoice, InvoicePayment, POStatus, PaymentStatus, Project, ProjectEmployee,
    PurchaseOrder, Timesheet, TimesheetStatus,
)
from services.revenue_report import Month, Period, revenue_report  # noqa: E402

TODAY = date(2026, 9, 18)


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


def _invoice(db, project, number, on, sub_total, tax, paid=0.0, due=None):
    inv = Invoice(invoice_number=number, project_id=project.id, invoice_date=on, due_date=due,
                  sub_total=sub_total, tax_amount=tax, grand_total=sub_total + tax,
                  paid_amount=paid, balance_amount=sub_total + tax - paid,
                  payment_status=PaymentStatus.PAID if paid >= sub_total + tax else PaymentStatus.UNPAID)
    db.add(inv); db.flush()
    return inv


@pytest.fixture()
def ledger(db):
    harman = Customer(name="HARMAN"); visteon = Customer(name="VISTEON")
    db.add_all([harman, visteon]); db.flush()
    p1 = Project(customer_id=harman.id, name="Infotainment"); p2 = Project(customer_id=visteon.id, name="Cluster")
    db.add_all([p1, p2]); db.flush()
    emp = Employee(first_name="Dev", email="dev@karnex.in"); db.add(emp); db.flush()
    db.add(ProjectEmployee(project_id=p1.id, employee_id=emp.id, billing_rate=100000))
    db.add(ProjectEmployee(project_id=p2.id, employee_id=emp.id, billing_rate=100000))

    # September 2026 (selected month): HARMAN 600k + VISTEON 200k = 800k billed.
    i1 = _invoice(db, p1, "INV-1", date(2026, 9, 5), 600000, 108000, due=date(2026, 10, 5))
    i2 = _invoice(db, p2, "INV-2", date(2026, 9, 10), 200000, 36000, due=date(2026, 8, 1))  # overdue 48 d
    # August 2026: 500k. September 2025: 400k (YoY).
    i3 = _invoice(db, p1, "INV-3", date(2026, 8, 12), 500000, 90000, paid=590000)
    _invoice(db, p2, "INV-4", date(2025, 9, 3), 400000, 72000, paid=472000)
    # Cash: 300k received in September against INV-1, plus INV-3 fully paid in August.
    db.add(InvoicePayment(invoice_id=i1.id, payment_date=date(2026, 9, 15), amount=300000))
    db.add(InvoicePayment(invoice_id=i3.id, payment_date=date(2026, 8, 30), amount=590000))
    i1.paid_amount = 300000; i1.balance_amount = 708000 - 300000
    # An approved-but-uninvoiced sheet (frozen 150k) with LOP leakage, and a live PO.
    db.add(Timesheet(project_id=p1.id, employee_id=emp.id, month=9, year=2026,
                     status=TimesheetStatus.APPROVED,
                     approved_figures={"line_items": [{"loss_of_pay_days": 2, "lop_covered_days": 1,
                                                       "no_billing_days_excluded": 0, "amount": 150000}],
                                       "totals": {"sub_total": 150000}}))
    db.add(PurchaseOrder(po_number="PO-1", customer_id=harman.id, total_value=3000000,
                         consumed_value=1200000, balance_value=1800000, status=POStatus.ACTIVE))
    db.commit()
    return db


def test_month_parsing():
    assert Month.parse("2026-09", TODAY).key == "2026-09"
    assert Month.parse("", TODAY).key == "2026-09"
    assert Month.parse("2026-01", TODAY).shift(-1).key == "2025-12"
    with pytest.raises(ValueError):
        Month.parse("Sept 2026", TODAY)
    with pytest.raises(ValueError):
        Month.parse("2026-13", TODAY)


def test_headline_billed_collected_and_trends(ledger):
    r = revenue_report(ledger, "2026-09", today=TODAY)
    h = r["headline"]
    assert h["billed"] == 800000 and h["gst"] == 144000 and h["invoices"] == 2
    assert h["collected"] == 300000
    assert h["collection_rate_pct"] == 37.5
    assert h["billed_prev"] == 500000 and h["billed_mom_pct"] == 60.0
    assert h["billed_yoy"] == 400000 and h["billed_yoy_pct"] == 100.0
    # Outstanding = INV-1 (408k) + INV-2 (236k); paid invoices excluded.
    assert h["outstanding"] == 644000 and h["open_invoices"] == 2


def test_series_is_twelve_months_ending_on_the_selected_month(ledger):
    r = revenue_report(ledger, "2026-09", today=TODAY)
    s = r["series"]
    assert len(s) == 12 and s[0]["month"] == "2025-10" and s[-1]["month"] == "2026-09"
    assert s[-1]["billed"] == 800000 and s[-2]["billed"] == 500000 and s[-2]["collected"] == 590000


def test_customer_split_and_concentration_flag(ledger):
    c = revenue_report(ledger, "2026-09", today=TODAY)["by_customer"]
    assert [x["customer"] for x in c["rows"]] == ["HARMAN", "VISTEON"]
    assert c["rows"][0]["share_pct"] == 75.0 and c["rows"][0]["collected"] == 300000
    assert c["top1_share_pct"] == 75.0 and c["concentration_risk"] is True
    assert c["top_customer"] == "HARMAN"


def test_ageing_buckets_use_due_date(ledger):
    a = revenue_report(ledger, "2026-09", today=TODAY)["ageing"]
    by = {b["label"]: b for b in a["buckets"]}
    assert by["Not yet due"]["amount"] == 408000          # INV-1 due 5 Oct
    assert by["31–60 days"]["amount"] == 236000           # INV-2 due 1 Aug → 48 days
    assert a["overdue_total"] == 236000
    assert a["top_overdue_customers"][0] == {"customer": "VISTEON", "overdue": 236000}


def test_pipeline_efficiency_and_leakage(ledger):
    r = revenue_report(ledger, "2026-09", today=TODAY)
    p = r["pipeline"]
    assert p["approved_uninvoiced"] == {"count": 1, "amount": 150000}
    assert p["active_po_balance"] == 1800000
    # Trailing 3 months billed: Jul 0, Aug 500k, Sep 800k → avg 433,333.33
    assert p["avg_monthly_billed"] == 433333.33 and p["po_cover_months"] == 4.2
    assert r["efficiency"] == {"deployed_heads": 2, "revenue_per_head": 400000,
                               # v3: a quarter/FY view needs the per-month figure
                               # for the month view's number to stay comparable.
                               "revenue_per_head_per_month": 400000}
    assert r["leakage"]["lop_days"] == 2 and r["leakage"]["lop_covered_by_weekend_work_days"] == 1


def test_empty_month_is_all_zeros_not_an_error(ledger):
    r = revenue_report(ledger, "2024-02", today=TODAY)
    assert r["headline"]["billed"] == 0 and r["headline"]["billed_mom_pct"] is None
    assert r["by_customer"]["rows"] == [] and r["by_customer"]["concentration_risk"] is False


def test_route_is_admin_only():
    """`role_required()` with no roles = Admin/CEO only; nothing else must widen it."""
    from routers.crm import reports
    import inspect
    src = inspect.getsource(reports.revenue)
    assert "role_required()" in src


# --------------------------------------------------------------------------- v2 (targets, margin, forecast, …)


def _set(db, key, value):
    from models import AppSetting
    db.add(AppSetting(key=key, value=str(value)))
    db.commit()


def test_targets_attainment_and_fy_projection(ledger):
    from services.revenue_report import TARGET_FY_KEY, TARGET_MONTH_KEY, TARGET_MONTH_PREFIX
    _set(ledger, TARGET_MONTH_KEY, "1000000")
    _set(ledger, TARGET_FY_KEY, "12000000")
    t = revenue_report(ledger, "2026-09", today=TODAY)["targets"]
    assert t["month_target"] == 1000000 and t["month_attainment_pct"] == 80.0 and t["month_gap"] == 200000
    # Current month: 800k in 18 of 30 days → run-rate 1,333,333.33 (133.3 %).
    assert t["is_current_month"] and t["run_rate"] == 1333333.33 and t["run_rate_attainment_pct"] == 133.3
    assert t["fy_label"] == "FY 2026-27" and t["fy_start"] == "2026-04" and t["fy_end"] == "2027-03"
    # FY to date = Aug 500k + Sep 800k; 6 months elapsed, 6 remaining at avg(Jul 0, Aug 500k, Sep 800k).
    assert t["fy_billed_to_date"] == 1300000 and t["fy_months_elapsed"] == 6
    assert t["fy_projection"] == 3900000.0   # 1.3 M + (1.3 M / 3) × 6
    assert t["fy_required_monthly"] == round((12000000 - 1300000) / 6, 2)
    # A per-month override beats the default.
    _set(ledger, TARGET_MONTH_PREFIX + "2026-09", "800000")
    t = revenue_report(ledger, "2026-09", today=TODAY)["targets"]
    assert t["month_attainment_pct"] == 100.0 and t["month_target_is_default"] is False


def test_targets_absent_means_no_percentages(ledger):
    t = revenue_report(ledger, "2026-09", today=TODAY)["targets"]
    assert t["month_target"] is None and t["month_attainment_pct"] is None and t["fy_target"] is None


def test_margin_uses_annual_ctc_over_twelve_and_reports_missing_ctc(ledger):
    m = revenue_report(ledger, "2026-09", today=TODAY)["margin"]
    # Dev has no CTC yet → cost 0, counted as missing (2 assignments).
    assert m["cost"] == 0 and m["heads_without_ctc"] == 2 and m["gross_margin"] == 800000
    emp = ledger.query(Employee).first()
    emp.current_ctc = 1200000   # ₹12 L p.a. → ₹1 L / month per assignment
    ledger.commit()
    m = revenue_report(ledger, "2026-09", today=TODAY)["margin"]
    assert m["cost"] == 200000 and m["gross_margin"] == 600000 and m["gross_margin_pct"] == 75.0
    by = {r["customer"]: r for r in m["by_customer"]}
    assert by["HARMAN"]["margin"] == 500000 and by["VISTEON"]["margin"] == 100000
    assert m["loss_making"] == []


def test_forecast_prorates_exits_and_flags_po_shortfall(ledger):
    pe = ledger.query(ProjectEmployee).order_by(ProjectEmployee.id).all()
    pe[0].billing_unit = "Monthly"; pe[0].billing_rate = 300000
    pe[1].billing_unit = "Daily"; pe[1].billing_rate = 10000          # × 21 = 210,000 / month
    pe[1].exit_date = date(2026, 10, 15); pe[1].is_exit = True         # half of October
    ledger.commit()
    f = revenue_report(ledger, "2026-09", today=TODAY)["forecast"]
    oct_, nov, dec = f["months"]
    assert oct_["month"] == "2026-10" and oct_["heads"] == 2 and oct_["roll_off_count"] == 1
    assert oct_["amount"] == round(300000 + 210000 * 15 / 31, 2)
    assert nov["amount"] == 300000 and dec["amount"] == 300000 and nov["heads"] == 1
    assert f["total"] == round(oct_["amount"] + 600000, 2)
    assert f["po_shortfall"] == 0            # PO balance 1.8 M covers it
    assert all(m["po_covered"] for m in f["months"])


def test_collections_dso_and_days_to_pay(ledger):
    c = revenue_report(ledger, "2026-09", today=TODAY)["collections"]
    # Trailing 90 days incl. GST: Aug 590k + Sep 944k = 1,534,000; outstanding 644k → DSO 37.9
    assert c["dso_days"] == round(644000 / 1534000 * 90, 1)
    # One receipt in Sep: 300k on 15 Sep against INV-1 dated 5 Sep → 10 days
    assert c["avg_days_to_pay"] == 10.0 and c["receipts"] == 1


def test_dimensions_split_the_month(ledger):
    d = revenue_report(ledger, "2026-09", today=TODAY)["dimensions"]
    assert d["by_type"] == [{"label": "Unlinked", "billed": 800000, "invoices": 2, "share_pct": 100.0}]
    assert {r["label"] for r in d["by_branch"]} == {"HARMAN (no branch)", "VISTEON (no branch)"}
    assert d["by_owner"][0]["label"] == "Unassigned"


def test_alerts_surface_the_exceptions(ledger):
    from services.revenue_report import TARGET_MONTH_KEY, alert_level
    _set(ledger, TARGET_MONTH_KEY, "2000000")
    r = revenue_report(ledger, "2026-09", today=TODAY)
    keys = {a["key"] for a in r["alerts"]}
    assert "target_month" in keys and "concentration" in keys
    assert "overdue_90" not in keys              # INV-2 is 48 days late, not 90
    assert alert_level(r["alerts"]) in {"warn", "bad"}
    # An empty month has nothing to shout about.
    assert revenue_report(ledger, "2024-02", today=TODAY)["alerts"] == [] or all(
        a["key"] == "target_month" for a in revenue_report(ledger, "2024-02", today=TODAY)["alerts"])


def test_workbook_has_one_sheet_per_section(ledger):
    from io import BytesIO
    from openpyxl import load_workbook
    from services.revenue_export import build_revenue_workbook
    wb = load_workbook(BytesIO(build_revenue_workbook(revenue_report(ledger, "2026-09", today=TODAY))))
    assert wb.sheetnames == ["Summary", "Alerts", "12-month trend", "By customer", "By project",
                             "By employee", "Margin", "Ageing", "Forecast", "Dimensions",
                             "Cash flow", "Expected inflows", "Leakage"]
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(min_row=2, values_only=True)}
    assert summary["Revenue billed (excl. GST)"] == 800000 and summary["Cash collected"] == 300000


def test_targets_and_export_routes_are_admin_only():
    import inspect
    from routers.crm import reports
    assert "role_required()" in inspect.getsource(reports.revenue_targets)
    assert "role_required()" in inspect.getsource(reports.revenue_export)


def test_month_close_job_mails_admin_and_ceo_once(ledger, monkeypatch):
    from services import scheduler
    sent = []
    monkeypatch.setattr("services.notify.notify_roles",
                        lambda db, roles, title, message="", link="", **kw: sent.append((roles, title, link, kw)) or 2)
    assert scheduler.run_revenue_month_close(ledger, today=date(2026, 10, 1)) == {"sent": 1, "month": "2026-09"}
    roles, title, link, kw = sent[0]
    assert roles == ["Admin", "CEO"] and title == "Revenue close — September 2026"
    assert link == "/admin/?view=crm&p=reports&tab=revenue&month=2026-09"
    assert kw["dedupe_prefix"] == "revenue_close:2026-09" and kw["event"] == "reports.revenue_month_close"
    assert any(label == "Revenue billed (excl. GST)" and value == "₹800,000" for label, value in kw["rows"])
    # Outside the window the job stays quiet.
    assert scheduler.run_revenue_month_close(ledger, today=date(2026, 10, 20))["sent"] == 0
    assert "revenue_month_close" in scheduler.JOBS


# --------------------------------------------------------------------------- v3
# Month / Quarter / FY zooms, customer + project filters, project- and
# employee-wise revenue (21 Sep 2026, CEO ask).


def test_period_parsing_follows_the_indian_financial_year():
    q = Period.parse("quarter", "2026-09", TODAY)
    assert (q.first_month.key, q.last_month.key) == ("2026-07", "2026-09")
    assert q.quarter_index == 2 and q.key == "2026-Q2" and q.fy_label == "FY 2026-27"
    # January belongs to Q4 of the FY that started the PREVIOUS April.
    jan = Period.parse("quarter", "2026-01", TODAY)
    assert (jan.first_month.key, jan.last_month.key) == ("2026-01", "2026-03")
    assert jan.quarter_index == 4 and jan.fy_start_year == 2025
    fy = Period.parse("fy", "2026-09", TODAY)
    assert (fy.first_month.key, fy.last_month.key, fy.months) == ("2026-04", "2027-03", 12)
    # A blank period is the month view, so every existing ?month= link still works.
    assert Period.parse(None, "2026-09", TODAY).kind == "month"
    with pytest.raises(ValueError):
        Period.parse("weekly", "2026-09", TODAY)


def test_period_navigation_is_like_for_like():
    q = Period.parse("quarter", "2026-09", TODAY)
    assert q.previous.key == "2026-Q1" and q.year_ago.key == "2025-Q2"
    assert q.shift(-4).key == "2025-Q2"
    fy = Period.parse("fy", "2026-09", TODAY)
    assert fy.previous.key == "FY2025" and fy.year_ago.key == "FY2025"


def test_quarter_and_fy_roll_the_months_up(ledger):
    q = revenue_report(ledger, "2026-09", today=TODAY, period_raw="quarter")
    # Jul 0 + Aug 500k + Sep 800k.
    assert q["headline"]["billed"] == 1300000
    assert q["headline"]["months_in_period"] == 3
    assert q["headline"]["billed_per_month"] == round(1300000 / 3, 2)
    assert q["headline"]["billed_prev"] == 0              # Apr-Jun 2026 was empty
    assert q["headline"]["billed_yoy"] == 400000          # Jul-Sep 2025
    assert q["period"] == "quarter" and q["period_key"] == "2026-Q2"
    assert q["period_start"] == "2026-07-01" and q["period_end"] == "2026-09-30"
    assert q["comparison_label"] == "vs last quarter"
    # The anchor month is echoed back so the UI's one date control round-trips.
    assert q["month"] == "2026-09"
    assert len(q["series"]) == 8 and q["series"][-1]["month"] == "2026-Q2"

    fy = revenue_report(ledger, "2026-09", today=TODAY, period_raw="fy")
    assert fy["headline"]["billed"] == 1300000 and fy["months_in_period"] == 12
    assert fy["period_key"] == "FY2026" and len(fy["series"]) == 5
    assert fy["headline"]["billed_yoy"] == 400000        # FY2025 held Sep 2025


def test_a_period_target_is_the_sum_of_its_months(ledger):
    from services.revenue_report import TARGET_MONTH_KEY, TARGET_MONTH_PREFIX
    _set(ledger, TARGET_MONTH_KEY, "1000000")
    month = revenue_report(ledger, "2026-09", today=TODAY)["targets"]
    assert month["month_target"] == 1000000 and month["month_target_is_default"] is True
    quarter = revenue_report(ledger, "2026-09", today=TODAY, period_raw="quarter")["targets"]
    assert quarter["month_target"] == 3000000 and quarter["months_with_target"] == 3
    # One month overridden: the quarter follows it, so the two views agree.
    _set(ledger, TARGET_MONTH_PREFIX + "2026-09", "1500000")
    quarter = revenue_report(ledger, "2026-09", today=TODAY, period_raw="quarter")["targets"]
    assert quarter["month_target"] == 3500000 and quarter["month_target_is_default"] is False


def test_filters_narrow_the_whole_report(ledger):
    harman = ledger.query(Customer).filter_by(name="HARMAN").one()
    project = ledger.query(Project).filter_by(name="Cluster").one()
    r = revenue_report(ledger, "2026-09", today=TODAY, customer_id=harman.id)
    assert r["headline"]["billed"] == 600000 and r["by_customer"]["customers"] == 1
    assert r["filters"]["customer_id"] == harman.id
    # Ageing reads the same filtered set — VISTEON's overdue invoice is gone.
    assert r["ageing"]["overdue_total"] == 0
    p = revenue_report(ledger, "2026-09", today=TODAY, project_id=project.id)
    assert p["headline"]["billed"] == 200000
    assert [row["project"] for row in p["by_project"]["rows"]] == ["Cluster"]
    # The dropdowns list EVERY customer/project, flagged by whether it billed —
    # hiding a quiet account read as a bug, and "why did they bill nothing?" is
    # a question this page has to be able to answer.
    quiet = Customer(name="DORMANT CO")
    ledger.add(quiet); ledger.flush()
    ledger.add(Project(customer_id=quiet.id, name="Nothing Doing"))
    ledger.commit()
    options = revenue_report(ledger, "2026-09", today=TODAY)["filters"]["options"]
    assert {c["name"] for c in options["customers"]} == {"HARMAN", "VISTEON", "DORMANT CO"}
    assert {pr["name"] for pr in options["projects"]} == {"Infotainment", "Cluster", "Nothing Doing"}
    billed = {c["name"]: c["billed"] for c in options["customers"]}
    assert billed == {"HARMAN": True, "VISTEON": True, "DORMANT CO": False}
    assert all(c["active"] for c in options["customers"])


def test_by_project_splits_billing_and_cost(ledger):
    bp = revenue_report(ledger, "2026-09", today=TODAY)["by_project"]
    rows = {r["project"]: r for r in bp["rows"]}
    assert rows["Infotainment"]["billed"] == 600000 and rows["Infotainment"]["customer"] == "HARMAN"
    assert rows["Infotainment"]["share_pct"] == 75.0
    assert rows["Cluster"]["billed"] == 200000 and rows["Cluster"]["share_pct"] == 25.0
    # The one employee carries no CTC, so cost is 0 and the gap is REPORTED,
    # never treated as free delivery.
    assert bp["heads_without_ctc"] == 2 and bp["loss_making"] == []
    assert bp["projects"] == 2 and bp["billed"] == 800000


def test_by_employee_reports_its_own_coverage(ledger):
    be = revenue_report(ledger, "2026-09", today=TODAY)["by_employee"]
    # Nothing in this ledger is timesheet-driven, so per-head revenue covers 0 %
    # of the month — and says so rather than showing an empty table.
    assert be["linked_billed"] == 0 and be["unlinked_billed"] == 800000
    assert be["unlinked_invoices"] == 2 and be["coverage_pct"] == 0.0
    assert be["billing_heads"] == 0 and be["idle_count"] == 1
    assert be["rows"][0]["employee"] == "Dev" and be["rows"][0]["deployed"] is True


def test_a_timesheet_driven_invoice_is_attributed_to_its_employee(ledger):
    emp = ledger.query(Employee).one()
    emp.current_ctc = 1200000          # ₹12 L a year → ₹1 L a month
    # Cluster, because the ledger's Infotainment sheet already occupies
    # (project, employee, month, year) — the timesheets table is unique on it.
    project = ledger.query(Project).filter_by(name="Cluster").one()
    sheet = Timesheet(project_id=project.id, employee_id=emp.id, month=9, year=2026,
                      status=TimesheetStatus.APPROVED, approved_figures={"totals": {"sub_total": 300000}})
    ledger.add(sheet); ledger.flush()
    inv = _invoice(ledger, project, "INV-TS", date(2026, 9, 20), 300000, 54000)
    inv.timesheet_id = sheet.id
    ledger.commit()

    be = revenue_report(ledger, "2026-09", today=TODAY)["by_employee"]
    row = be["rows"][0]
    assert row["employee"] == "Dev" and row["billed"] == 300000 and row["invoices"] == 1
    # Two active assignments × ₹1 L a month = ₹2 L of cost against ₹3 L billed.
    assert row["cost"] == 200000 and row["margin"] == 100000
    assert be["linked_billed"] == 300000 and be["unlinked_billed"] == 800000
    assert be["coverage_pct"] == round(300000 / 1100000 * 100, 1)
    assert be["billing_heads"] == 1 and be["idle_count"] == 0
    # A quarter costs three months of CTC, not one.
    q = revenue_report(ledger, "2026-09", today=TODAY, period_raw="quarter")["by_employee"]
    assert q["rows"][0]["cost"] == 600000
    assert q["rows"][0]["billed_per_month"] == 100000


def test_route_accepts_the_new_parameters():
    import inspect
    from routers.crm import reports
    src = inspect.getsource(reports.revenue)
    for name in ("period", "customer_id", "project_id"):
        assert f"{name}:" in src
    assert "role_required()" in src          # still Admin/CEO only
    with pytest.raises(ValueError):
        Period.parse("decade", None, TODAY)


# --------------------------------------------------------------------------- cash flow
# "When does the money land, and does it cover payroll until it does."


def test_cashflow_places_open_invoices_on_the_calendar(ledger):
    cf = revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]
    # Always as of TODAY, never the selected period — a forecast of a closed
    # month is not a forecast.
    assert cf["as_of"] == TODAY.isoformat()
    assert [m["month"] for m in cf["months"]] == ["2026-09", "2026-10", "2026-11"]

    # INV-2 (₹236k) was due 1 Aug 2026 — overdue as of 18 Sep.
    assert cf["overdue"]["count"] == 1 and cf["overdue"]["amount"] == 236000
    # INV-1 has ₹408k open, due 5 Oct 2026 → the October bucket.
    october = next(m for m in cf["months"] if m["month"] == "2026-10")
    assert october["expected"] == 408000 and october["invoices"] == 1
    # The overdue one is still cash we expect, so it lands in the current month
    # rather than dropping out of the forecast entirely.
    september = next(m for m in cf["months"] if m["month"] == "2026-09")
    assert september["expected"] == 236000
    assert cf["expected_total"] == 644000


def test_cashflow_nets_collections_against_people_cost(ledger):
    emp = ledger.query(Employee).one()
    emp.current_ctc = 2400000          # ₹24 L a year → ₹2 L a month
    ledger.commit()
    cf = revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]
    # One employee on two active assignments = 2 × ₹2 L a month × 3 months.
    assert cf["people_cost_total"] == 1200000
    assert cf["net_total"] == round(644000 - 1200000, 2)
    assert cf["covers_cost"] is False
    assert cf["heads_costed"] == 2 and cf["heads_without_ctc"] == 0
    # Approved-but-uninvoiced work is the cheapest lever, so it is reported.
    assert cf["unbilled_ready"] == {"amount": 150000, "count": 1}
    # ...and the shortfall raises a `bad` alert, ranked above a soft month.
    keys = {a["key"]: a["level"] for a in revenue_report(ledger, "2026-09", today=TODAY)["alerts"]}
    assert keys.get("cash_shortfall") == "bad"


def test_cashflow_uses_the_pace_we_actually_get_paid_at(ledger):
    # INV-3 was issued 12 Aug with no due date (→ due 11 Sep on 30-day terms)
    # and paid 30 Aug, i.e. EARLY. Paying early must not pull the forecast
    # forward — that would be planning on a favour.
    cf = revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]
    assert cf["slip_days"] == 0.0
    assert cf["expected_total_at_pace"] == cf["expected_total"]

    # A SMALL invoice paid 40 days late barely moves the pace, because the
    # average is weighted by amount — ₹1 L at 40 days late against ₹8.9 L paid
    # on time is 4 days, not 40. That is the whole point of weighting it.
    inv2 = ledger.query(Invoice).filter_by(invoice_number="INV-2").one()
    ledger.add(InvoicePayment(invoice_id=inv2.id, payment_date=date(2026, 9, 10), amount=100000))
    ledger.commit()
    assert revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]["slip_days"] == 4.0

    # A LARGE one does. INV-4 fell due 3 Oct 2025; ₹10 L settled 2 Dec 2025 is
    # 60 days late, and now dominates the weighted average.
    inv4 = ledger.query(Invoice).filter_by(invoice_number="INV-4").one()
    ledger.add(InvoicePayment(invoice_id=inv4.id, payment_date=date(2025, 12, 2), amount=1000000))
    ledger.commit()
    cf = revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]
    assert cf["slip_days"] == 32.2
    # INV-1 is due 5 Oct; +32 days pushes the cash into November.
    october = next(m for m in cf["months"] if m["month"] == "2026-10")
    november = next(m for m in cf["months"] if m["month"] == "2026-11")
    assert october["expected"] == 408000 and october["expected_at_pace"] == 0
    assert november["expected_at_pace"] == 408000


def test_cashflow_lists_the_biggest_inflows_and_follows_the_filter(ledger):
    cf = revenue_report(ledger, "2026-09", today=TODAY)["cashflow"]
    top = cf["top_expected"]
    assert [r["amount"] for r in top] == sorted((r["amount"] for r in top), reverse=True)
    assert top[0]["customer"] == "HARMAN" and top[0]["number"] == "INV-1"
    overdue_row = next(r for r in top if r["number"] == "INV-2")
    assert overdue_row["overdue_days"] == (TODAY - date(2026, 8, 1)).days

    harman = ledger.query(Customer).filter_by(name="HARMAN").one()
    scoped = revenue_report(ledger, "2026-09", today=TODAY, customer_id=harman.id)["cashflow"]
    assert {r["customer"] for r in scoped["top_expected"]} == {"HARMAN"}
    assert scoped["overdue"]["count"] == 0          # INV-2 is VISTEON's



def test_targets_carry_the_anchors_month_quarter_and_per_fy_values(ledger):
    """28 Sep 2026 — the CEO dashboard's Targets dialog edits three rungs (month ·
    quarter · FY) from one place, and an FY target belongs to ONE financial year."""
    from services.revenue_report import TARGET_FY_KEY, TARGET_FY_PREFIX, TARGET_MONTH_KEY, TARGET_MONTH_PREFIX
    _set(ledger, TARGET_MONTH_KEY, "1000000")
    _set(ledger, TARGET_MONTH_PREFIX + "2026-08", "1500000")
    _set(ledger, TARGET_FY_KEY, "12000000")
    t = revenue_report(ledger, "2026-09", today=TODAY, period_raw="fy")["targets"]
    assert t["anchor_month"] == "2026-09" and t["anchor_month_target"] == 1000000
    assert t["anchor_month_target_is_override"] is False
    assert t["quarter_key"] == "2026-Q2" and t["quarter_target"] == 3500000, "Jul 1M + Aug 1.5M + Sep 1M"
    assert t["quarter_months_with_target"] == 3
    assert t["fy_key"] == "2026" and t["fy_target"] == 12000000 and t["fy_target_is_default"] is True
    # A per-FY value beats the default — and only for ITS year.
    _set(ledger, TARGET_FY_PREFIX + "2026", "15000000")
    t = revenue_report(ledger, "2026-09", today=TODAY, period_raw="fy")["targets"]
    assert t["fy_target"] == 15000000 and t["fy_target_is_default"] is False
    last = revenue_report(ledger, "2025-09", today=TODAY, period_raw="fy")["targets"]
    assert last["fy_key"] == "2025" and last["fy_target"] == 12000000 and last["fy_target_is_default"] is True


def test_the_targets_route_spreads_a_quarter_over_its_months_and_scopes_the_fy(ledger):
    from models import AppSetting
    from routers.crm import reports
    from services.revenue_report import TARGET_FY_KEY, TARGET_FY_PREFIX, TARGET_MONTH_PREFIX

    body = reports.RevenueTargetsIn(month="2026-08", quarter_target=1000000, fy_target=9000000)
    reports.revenue_targets(body, ledger, None)
    keys = {k: ledger.get(AppSetting, TARGET_MONTH_PREFIX + k).value for k in ("2026-07", "2026-08", "2026-09")}
    assert keys == {"2026-07": "333333.33", "2026-08": "333333.33", "2026-09": "333333.34"}, "paise land on the last month"
    assert ledger.get(AppSetting, TARGET_FY_PREFIX + "2026").value == "9000000.00"
    assert ledger.get(AppSetting, TARGET_FY_KEY) is None, "with a month, the FY target is that year's, not the default"
    t = revenue_report(ledger, "2026-09", today=TODAY, period_raw="quarter")["targets"]
    assert t["month_target"] == 1000000 and t["fy_target"] == 9000000
    # Clearing the quarter removes the three overrides.
    reports.revenue_targets(reports.RevenueTargetsIn(month="2026-08", clear_quarter_target=True), ledger, None)
    assert all(ledger.get(AppSetting, TARGET_MONTH_PREFIX + k) is None for k in keys)
    # A quarter target with no month is refused.
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        reports.revenue_targets(reports.RevenueTargetsIn(quarter_target=5), ledger, None)
