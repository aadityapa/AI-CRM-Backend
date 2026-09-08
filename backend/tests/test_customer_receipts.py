"""Customer received amounts (7 Sep 2026): allocation maths + the endpoint
round trip on SQLite — create allocates oldest-first, invoice balances move,
delete restores them.

Run:  cd backend && python -m pytest tests/test_customer_receipts.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

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


for _m in ("base", "rbac", "customers", "opportunities", "requirements", "candidates", "profiles",
           "projects", "timesheets", "finance", "hr", "leave", "masters", "scheduling",
           "resumes", "ai_links", "email_outbox", "notify_routes", "template_requests",
           "user_profiles", "access_templates"):
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import Customer, Invoice, Project  # noqa: E402
from routers.crm.customer_receipts import ReceiptIn, allocate, create_receipt, delete_receipt  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402

FIN = CurrentUser(id=1, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _inv(id_, day, total, paid=0):
    return SimpleNamespace(id=id_, invoice_date=date(2026, 8, day), balance_amount=Decimal(total) - Decimal(paid))


def test_allocation_is_oldest_first_up_to_balance():
    plan, left = allocate(Decimal("150"), [_inv(2, 10, 100), _inv(1, 5, 80), _inv(3, 20, 500, 490)])
    assert [(i.id, float(a)) for i, a in plan] == [(1, 80.0), (2, 70.0)]
    assert left == 0


def test_leftover_stays_unallocated():
    plan, left = allocate(Decimal("100"), [_inv(1, 5, 30)])
    assert [(i.id, float(a)) for i, a in plan] == [(1, 30.0)] and left == Decimal("70.00")


def test_receipt_settles_invoices_and_delete_restores(db):
    cust = Customer(name="APTIV")
    db.add(cust); db.flush()
    proj = Project(name="P1", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    a = Invoice(invoice_number="INV-1", project_id=proj.id, invoice_date=date(2026, 8, 1),
                sub_total=100, tax_amount=0, grand_total=100, paid_amount=0, balance_amount=100,
                payment_status="Unpaid")
    b = Invoice(invoice_number="INV-2", project_id=proj.id, invoice_date=date(2026, 8, 15),
                sub_total=200, tax_amount=0, grand_total=200, paid_amount=0, balance_amount=200,
                payment_status="Unpaid")
    db.add_all([a, b]); db.commit()

    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 9, 7), amount=Decimal("250"),
                                   payment_mode="NEFT", reference_number="UTR1", invoice_ids=[a.id, b.id]),
                         db=db, user=FIN)
    body = res["data"]
    db.refresh(a); db.refresh(b)
    assert str(a.payment_status) in ("Paid", "PaymentStatus.PAID") or getattr(a.payment_status, "value", a.payment_status) == "Paid"
    assert float(a.balance_amount) == 0 and float(b.balance_amount) == 50
    assert body["allocated_amount"] == 250 and body["unallocated_amount"] == 0
    assert [x["invoice_number"] for x in body["allocations"]] == ["INV-1", "INV-2"]

    delete_receipt(body["id"], db=db, user=FIN)
    db.refresh(a); db.refresh(b)
    assert float(a.balance_amount) == 100 and float(b.balance_amount) == 200
    assert getattr(a.payment_status, "value", a.payment_status) == "Unpaid"
