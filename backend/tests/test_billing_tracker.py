"""The Dashboard's billing tracker (20 Oct 2026, user report: "invoice approved
but not showing there, same for timesheet").

Pinned: every approved timesheet and every invoice is a row — no time window,
no page cut — the stage follows the chain, the month is the WORK month, the
customer filter finds an invoice through its PO too, and the filter choices
never shrink to the current filter.

Run:  cd backend && python -m pytest tests/test_billing_tracker.py -q
"""
from __future__ import annotations

import importlib
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
from services.billing_tracker import (  # noqa: E402
    TrackerFilters, billing_tracker, period_label, stage_of,
)


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


def test_stage_and_period_rules_are_plain():
    assert stage_of(kind=None, returned=False, customer_approved=False, irn=False,
                    payment_status=None) == "awaiting_proforma"
    assert stage_of(kind="Proforma", returned=True, customer_approved=False, irn=False,
                    payment_status="Unpaid") == "returned"
    assert stage_of(kind="Tax", returned=False, customer_approved=False, irn=False,
                    payment_status="Unpaid") == "awaiting_customer"
    assert stage_of(kind="Tax", returned=False, customer_approved=True, irn=False,
                    payment_status="Unpaid") == "irn_pending"
    assert stage_of(kind="Tax", returned=False, customer_approved=True, irn=True,
                    payment_status="Partially_Paid") == "unpaid"
    assert stage_of(kind="Tax", returned=False, customer_approved=False, irn=False,
                    payment_status="Paid") == "paid"
    assert period_label(2026, 5, date(2026, 5, 1), date(2026, 5, 31)) is None
    assert period_label(2026, 5, date(2026, 4, 21), date(2026, 5, 30)) == "21 Apr – 30 May 2026"
    assert period_label(2027, 1, date(2026, 12, 21), date(2027, 1, 20)) == "21 Dec 2026 – 20 Jan 2027"


def _seed(db):
    from models import (
        Customer, Employee, Invoice, Project, PurchaseOrder, Timesheet, TimesheetStatus,
    )
    uno, other = Customer(name="Uno Minda"), Customer(name="Harman")
    db.add_all([uno, other])
    db.flush()
    p_uno = Project(name="CREAT T&M", customer_id=uno.id)
    p_other = Project(name="Harman ADAS", customer_id=other.id)
    a = Employee(first_name="Aakash", last_name="Malwade", email="a@karnex.in")
    b = Employee(first_name="Bina", last_name="Rao", email="b@karnex.in")
    db.add_all([p_uno, p_other, a, b])
    db.flush()
    old = datetime.now(timezone.utc) - timedelta(days=400)   # far outside any desk window
    s1 = Timesheet(project_id=p_uno.id, employee_id=a.id, year=2025, month=6,
                   status=TimesheetStatus.APPROVED, approved_at=old)
    s2 = Timesheet(project_id=p_uno.id, employee_id=a.id, year=2026, month=5,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc),
                   period_start_date=date(2026, 4, 21), period_end_date=date(2026, 5, 30))
    s3 = Timesheet(project_id=p_other.id, employee_id=b.id, year=2026, month=5,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc))
    draft = Timesheet(project_id=p_uno.id, employee_id=b.id, year=2026, month=6,
                      status=TimesheetStatus.DRAFT)
    db.add_all([s1, s2, s3, draft])
    db.flush()
    # s2 → a Proforma with Finance; s3 → a customer-approved tax invoice (IRN pending).
    db.add_all([
        Invoice(invoice_number="PI-2026-005", proforma_number="PI-2026-005", kind="Proforma",
                project_id=p_uno.id, timesheet_id=s2.id, invoice_date=date(2026, 10, 19),
                sub_total=273340, tax_amount=49201.2, grand_total=322541),
        Invoice(invoice_number="INV-2026-001", kind="Tax", project_id=p_other.id, timesheet_id=s3.id,
                invoice_date=date(2026, 6, 3), sub_total=100, tax_amount=18, grand_total=118,
                customer_approved_at=datetime.now(timezone.utc)),
    ])
    # A manual invoice whose PROJECT belongs to Harman but whose PO is Uno Minda's.
    po = PurchaseOrder(po_number="4300472106", customer_id=uno.id, total_value=1000,
                       balance_value=1000, consumed_value=0)
    db.add(po)
    db.flush()
    db.add(Invoice(invoice_number="INV-2026-002", kind="Tax", project_id=p_other.id, po_id=po.id,
                   invoice_date=date(2026, 7, 9), sub_total=10, tax_amount=1.8, grand_total=11.8))
    db.commit()
    return uno, other


def test_every_approved_sheet_and_invoice_is_a_row(db):
    uno, _ = _seed(db)
    out = billing_tracker(db, TrackerFilters(limit=100))
    rows = out["rows"]
    by_key = {r["key"]: r for r in rows}
    assert out["meta"]["total"] == 4                       # 3 approved sheets + 1 manual invoice; no draft
    stages = sorted(r["stage"] for r in rows)
    assert stages == ["awaiting_customer", "awaiting_proforma", "irn_pending", "proforma"]
    # The 400-day-old approved sheet is still there (the desk's window hid it).
    assert any(r["month"] == "2025-06" and r["stage"] == "awaiting_proforma" for r in rows)
    # The cross-month sheet says its period; the Proforma row says its work month.
    pi = next(r for r in rows if r["invoice_number"] == "PI-2026-005")
    assert (pi["month_label"], pi["period_label"]) == ("May 2026", "21 Apr – 30 May 2026")
    assert pi["customer"] == "Uno Minda"
    # Counts per stage + "open".
    assert out["meta"]["counts"]["all"] == 4 and out["meta"]["counts"]["open"] == 4
    assert by_key  # keys are unique


def test_filters_narrow_rows_but_never_the_choices(db):
    uno, other = _seed(db)
    may = billing_tracker(db, TrackerFilters(month="2026-05"))
    assert {r["employee"] for r in may["rows"]} == {"Aakash Malwade", "Bina Rao"}
    # A chosen month does not shrink the month dropdown.
    assert {m["key"] for m in may["meta"]["options"]["months"]} >= {"2025-06", "2026-05", "2026-07"}
    # The manual invoice on a Harman project PRINTS Harman (the project's customer).
    harman = billing_tracker(db, TrackerFilters(customer_id=other.id))
    assert {r["invoice_number"] for r in harman["rows"]} == {"INV-2026-001", "INV-2026-002"}
    # Stage filter narrows the rows, not the counts.
    irn = billing_tracker(db, TrackerFilters(stage="irn_pending"))
    assert [r["invoice_number"] for r in irn["rows"]] == ["INV-2026-001"]
    assert irn["meta"]["counts"]["all"] == 4
    # Search finds the employee and the PI number.
    assert billing_tracker(db, TrackerFilters(search="pi-2026-005"))["meta"]["total"] == 1
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(month="May"))
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(stage="nope"))


def test_invoice_list_customer_filter_finds_an_invoice_by_its_po(db):
    """`GET /api/invoices?customer_id=` used to join the PROJECT's customer only,
    while the row printed the PO's — an invoice could be shown as Uno Minda's
    and still be missing from Uno Minda's list."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.finance as fin

    uno, _ = _seed(db)
    app = FastAPI()
    app.include_router(fin.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="fin", roles={"Finance"})
    r = TestClient(app).get(f"/api/invoices?customer_id={uno.id}")
    assert r.status_code == 200, r.text
    assert "INV-2026-002" in {x["invoice_number"] for x in r.json()["data"]}


def test_the_route_is_finance_admin_ceo_only():
    import inspect

    import routers.crm.dashboards as dash
    src = inspect.getsource(dash.billing_tracker)
    assert 'role_required("Finance")' in src
