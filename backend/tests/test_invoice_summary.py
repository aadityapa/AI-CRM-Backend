"""Reports ▸ Invoice Summary (23 Oct 2026, user ask) — `services/invoice_summary.py`.

Pinned: one table per customer for a month, issued tax invoices only (never a
Proforma), the description reads "Contract Staffing Service <employee> - <work
month>", "invoice" basis = dated in the month, "work" basis = the billed
timesheet's month, and the route is Finance / Sales behind its Reports sub-tab.

Run:  cd backend && python -m pytest tests/test_invoice_summary.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from pathlib import Path

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
from services import invoice_summary as isum  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db(monkeypatch):
    import services.org_settings as org
    monkeypatch.setattr(org, "setting", lambda *a, **k: None)
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


def _seed(db):
    from models import Customer, Employee, Invoice, Project, Timesheet, TimesheetStatus
    harman, uno = Customer(name="Harman"), Customer(name="Uno Minda")
    db.add_all([harman, uno]); db.flush()
    ph, pu = Project(name="ADAS", customer_id=harman.id), Project(name="CREAT", customer_id=uno.id)
    emp = Employee(first_name="Anamalamudi", last_name="Gowtham", email="g@karnex.in")
    db.add_all([ph, pu, emp]); db.flush()
    sep = Timesheet(project_id=ph.id, employee_id=emp.id, year=2026, month=9,
                    status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc))
    db.add(sep); db.flush()
    db.add_all([
        Invoice(invoice_number="KRSW26-27-61", kind="Tax", project_id=ph.id, timesheet_id=sep.id,
                invoice_date=date(2026, 10, 15), sub_total=191866.93, tax_amount=34536.05,
                grand_total=226403),
        Invoice(invoice_number="PI-2026-009", kind="Proforma", project_id=ph.id,
                invoice_date=date(2026, 10, 16), sub_total=5, tax_amount=1, grand_total=6),
        Invoice(invoice_number="KRSW26-27-62", kind="Tax", project_id=pu.id,
                invoice_date=date(2026, 10, 20), sub_total=100, tax_amount=18, grand_total=118),
        Invoice(invoice_number="KRSW26-27-40", kind="Tax", project_id=pu.id,
                invoice_date=date(2026, 9, 2), sub_total=50, tax_amount=9, grand_total=59),
    ])
    db.commit()
    return harman, uno


def test_pure_helpers():
    assert isum.parse_month("2026-10") == (2026, 10)
    assert isum.parse_month(None, date(2026, 3, 9)) == (2026, 3)
    with pytest.raises(ValueError):
        isum.parse_month("Oct 2026")
    assert isum.describe("Contract Staffing Service", "Anamalamudi Gowtham", "Sep 2026") == \
        "Contract Staffing Service Anamalamudi Gowtham - Sep 2026"
    assert isum.describe("Contract Staffing Service", None, None) == "Contract Staffing Service"


def test_one_table_per_customer_tax_invoices_only(db):
    harman, uno = _seed(db)
    out = isum.invoice_summary(db, month="2026-10")
    assert [g["customer"] for g in out["groups"]] == ["Harman", "Uno Minda"]
    rows = out["groups"][0]["rows"]
    assert [r["invoice_number"] for r in rows] == ["KRSW26-27-61"]          # the Proforma is not a bill
    assert rows[0]["sr"] == 1 and rows[0]["total_amount"] == 226403.0
    assert rows[0]["description"] == "Contract Staffing Service Anamalamudi Gowtham - Sep 2026"
    assert out["totals"]["count"] == 2
    only = isum.invoice_summary(db, month="2026-10", customer_id=uno.id)
    assert [g["customer"] for g in only["groups"]] == ["Uno Minda"]


def test_work_basis_follows_the_timesheet_month(db):
    _seed(db)
    sep = isum.invoice_summary(db, month="2026-09", basis="work")
    numbers = sorted(r["invoice_number"] for g in sep["groups"] for r in g["rows"])
    # The Sep timesheet's invoice (dated Oct) + the manual one dated Sep.
    assert numbers == ["KRSW26-27-40", "KRSW26-27-61"]
    with pytest.raises(ValueError):
        isum.invoice_summary(db, month="2026-09", basis="weekly")


def test_route_is_behind_its_reports_sub_tab():
    src = (BACKEND / "routers" / "crm" / "reports.py").read_text(encoding="utf-8")
    assert 'subtab_gate("reports", "invoice_summary", FINANCE_VIEW)' in src
    assert '"/invoice-summary"' in src
