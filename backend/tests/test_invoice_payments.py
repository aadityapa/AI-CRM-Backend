"""Record Payment ⇄ Received Amount, reversing a payment, and "collected"
counted in the invoice's month (2 Nov 2026).

Run:  cd backend && python -m pytest tests/test_invoice_payments.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timezone
from decimal import Decimal

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
from models.finance import CustomerReceipt, InvoicePayment  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from services import invoice_payments  # noqa: E402
from services.revenue_report import _payments_in_window  # noqa: E402

FIN = CurrentUser(id=1, username="fin", full_name="Fin", roles={"Finance"})
NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _book(db):
    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    proj = Project(name="P1", customer_id=cust.id, status="Active", billing_frequency="Monthly")
    db.add(proj); db.flush()
    jun = Invoice(invoice_number="INV-JUN", project_id=proj.id, invoice_date=date(2026, 6, 30),
                  sub_total=100_000, tax_amount=0, grand_total=100_000, paid_amount=0, balance_amount=100_000,
                  payment_status="Unpaid", customer_approved_at=NOW, irn_number="a" * 64)
    jul = Invoice(invoice_number="INV-JUL", project_id=proj.id, invoice_date=date(2026, 7, 31),
                  sub_total=80_000, tax_amount=0, grand_total=80_000, paid_amount=0, balance_amount=80_000,
                  payment_status="Unpaid", customer_approved_at=NOW, irn_number="b" * 64)
    db.add_all([jun, jul]); db.commit()
    return cust, proj, jun, jul


def _receipt(db, cust, amount, unallocated, day=date(2026, 9, 10)):
    r = CustomerReceipt(customer_id=cust.id, received_date=day, amount=Decimal(amount),
                        payment_mode="NEFT", reference_number="UTR9", unallocated_amount=Decimal(unallocated),
                        tds_total=Decimal("0"))
    db.add(r); db.flush()
    return r


def test_take_a_payment_from_a_received_amount_on_account(db):
    cust, _, jun, _ = _book(db)
    r = _receipt(db, cust, 150_000, 150_000)
    assert [x["id"] for x in invoice_payments.receipts_on_account(db, jun)] == [r.id]
    pay = invoice_payments.record_payment(db, jun, payment_date=date(2026, 10, 1), amount=100_000,
                                          payment_mode="Cash", receipt_id=r.id, user=FIN)
    assert pay.receipt_id == r.id
    assert pay.payment_date == date(2026, 9, 10) and pay.payment_mode == "NEFT", "the payment IS that bank credit"
    assert float(r.unallocated_amount) == 50_000 and float(jun.balance_amount) == 0
    with pytest.raises(HTTPException) as err:
        invoice_payments.record_payment(db, jun, payment_date=date(2026, 9, 10), amount=60_000, receipt_id=r.id)
    assert err.value.status_code == 400, "more than is left on account"


def test_add_to_received_creates_a_mapped_receipt_and_both_links_together_are_refused(db):
    cust, _, _, jul = _book(db)
    pay = invoice_payments.record_payment(db, jul, payment_date=date(2026, 9, 12), amount=80_000,
                                          payment_mode="RTGS", reference_number="R1", add_to_received=True, user=FIN)
    rc = db.get(CustomerReceipt, pay.receipt_id)
    assert rc.customer_id == cust.id and float(rc.amount) == 80_000 and float(rc.unallocated_amount) == 0
    with pytest.raises(HTTPException):
        invoice_payments.record_payment(db, jul, payment_date=date(2026, 9, 12), amount=1,
                                        receipt_id=rc.id, add_to_received=True)


def test_reverse_puts_the_money_back_on_account_or_removes_the_receipt(db):
    cust, _, jun, jul = _book(db)
    r = _receipt(db, cust, 100_000, 100_000)
    pay = invoice_payments.record_payment(db, jun, payment_date=None, amount=100_000, receipt_id=r.id)
    with pytest.raises(HTTPException) as err:
        invoice_payments.reverse_payment(db, jun, pay, reason="oops")
    assert err.value.status_code == 400, "a reason of at least 10 characters"
    out = invoice_payments.reverse_payment(db, jun, pay, reason="Recorded on the wrong invoice", user=FIN)
    assert out["back_on_account"] and not out["receipt_removed"]
    assert float(r.unallocated_amount) == 100_000 and float(jun.balance_amount) == 100_000
    assert db.execute(select(InvoicePayment)).first() is None

    direct = invoice_payments.record_payment(db, jul, payment_date=date(2026, 9, 1), amount=80_000,
                                             add_to_received=True)
    rid = direct.receipt_id
    out = invoice_payments.reverse_payment(db, jul, direct, reason="Payment bounced by the bank",
                                           remove_receipt=True)
    assert out["receipt_removed"] and db.get(CustomerReceipt, rid) is None


def test_collected_counts_in_the_invoice_month_and_on_account_in_the_received_month(db):
    cust, _, jun, jul = _book(db)
    r = _receipt(db, cust, 200_000, 200_000)
    invoice_payments.record_payment(db, jun, payment_date=None, amount=100_000, receipt_id=r.id)
    invoice_payments.record_payment(db, jul, payment_date=None, amount=80_000, receipt_id=r.id)
    db.commit()
    rows = _payments_in_window(db, date(2026, 4, 1), date(2027, 3, 31))
    by_month: dict[str, float] = {}
    for x in rows:
        k = x["date"].strftime("%Y-%m")
        by_month[k] = by_month.get(k, 0) + float(x["amount"])
    # 5 Nov 2026 (user rule): the 20k still on account is NOT collected anywhere —
    # it waits in Received Amount (the Dashboard shows it on its own) until mapped.
    assert by_month == {"2026-06": 100_000, "2026-07": 80_000}, \
        "Sept cash moves to Jun / Jul once mapped; money still on account is not collected"
    assert _payments_in_window(db, date(2026, 9, 1), date(2026, 9, 30)) == []
    # By the day the cash arrived (the "Cash received month" switch) all 200k is September's.
    from services import cash_month
    sep = [type("M", (), {"key": "2026-09", "start": date(2026, 9, 1), "end": date(2026, 9, 30)})()]
    paid = cash_month.cash_payments(db, date(2026, 9, 1), date(2026, 9, 30), [])
    held = cash_month.cash_on_account(db, date(2026, 9, 1), date(2026, 9, 30))
    assert cash_month.cash_by_month(sep, paid, held)["2026-09"]["cash_in"] == 200_000
