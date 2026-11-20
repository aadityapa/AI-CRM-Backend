"""Map money from the Invoices list (20 Nov 2026, Finance ask): ticked invoices →
cash from a received amount (existing on account, or new) + TDS from Received TDS
(existing / new) or booked as pending — one transaction. Plus the CEO money words
on the Ex. Tax basis (Received + Due = Revenue (Ex. Tax)).

Run:  cd backend && python -m pytest tests/test_receipt_mapping.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from tests.test_customer_receipts import FIN, _open_invoice, db  # noqa: F401 — fixture
from routers.crm.customer_receipts import ReceiptIn, create_receipt
from routers.crm.receipt_mapping import (
    MapIn, MapItem, NewReceiptIn, NewTdsIn, common_customer, map_money, options,
)
from services.money_split import ex_tax_split, invoice_money


def test_common_customer_is_pure():
    per = {1: (5, {5, 7}), 2: (5, {5})}
    assert common_customer(per) == 5
    assert common_customer(per, wanted=7) is None
    assert common_customer({1: (5, {5}), 2: (6, {6})}) is None
    assert common_customer({1: (5, {5, 7}), 2: (7, {5, 7})}) == 5


def test_new_receipt_and_new_tds_settle_two_invoices_in_one_step(db):  # noqa: F811
    cust, a = _open_invoice(db, "INV-Q1", 1000, 180, day=3)
    _, b = _open_invoice(db, "INV-Q2", 2000, 360, day=4)
    opts = options(f"{a.id},{b.id}", db=db, user=FIN)["data"]
    assert opts["customer"]["id"] == cust.id and len(opts["invoices"]) == 2
    out = map_money(MapIn(
        items=[MapItem(invoice_id=a.id, cash=Decimal("1080"), tds=Decimal("100")),
               MapItem(invoice_id=b.id, cash=Decimal("1000"))],
        new_receipt=NewReceiptIn(received_date=date(2026, 11, 18), amount=Decimal("2500"), payment_mode="NEFT"),
        new_tds=NewTdsIn(received_date=date(2026, 11, 18), amount=Decimal("100"), certificate_number="16A-9"),
    ), db=db, user=FIN)
    db.refresh(a); db.refresh(b)
    assert float(a.balance_amount) == 0 and float(a.tds_record.tds_balance) == 0
    assert float(b.balance_amount) == 1360
    assert out["data"]["cash"] == 2080 and out["data"]["tds"] == 100
    assert "420.00 still on account" in out["message"]


def test_existing_on_account_receipt_and_pending_tds(db):  # noqa: F811
    cust, a = _open_invoice(db, "INV-Q3", 1000, 180)
    rc = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1),
                                  amount=Decimal("1080")), db=db, user=FIN)["data"]
    assert options(str(a.id), db=db, user=FIN)["data"]["receipts"][0]["id"] == rc["id"]
    map_money(MapIn(items=[MapItem(invoice_id=a.id, cash=Decimal("1080"), tds=Decimal("100"))],
                    receipt_id=rc["id"], tds_mode="pending"), db=db, user=FIN)
    db.refresh(a)
    assert float(a.balance_amount) == 0 and float(a.tds_record.tds_balance) == 100  # pending from client


def test_refuses_without_a_source_and_more_than_the_balance(db):  # noqa: F811
    _, a = _open_invoice(db, "INV-Q4", 1000, 180)
    with pytest.raises(HTTPException):
        map_money(MapIn(items=[MapItem(invoice_id=a.id, cash=Decimal("100"))]), db=db, user=FIN)
    with pytest.raises(HTTPException):
        map_money(MapIn(items=[MapItem(invoice_id=a.id, cash=Decimal("5000"))],
                        new_receipt=NewReceiptIn(received_date=date(2026, 11, 1), amount=Decimal("5000"))),
                  db=db, user=FIN)
    db.rollback(); db.refresh(a)
    assert float(a.balance_amount) == 1180  # nothing half-done


def test_received_and_due_ex_tax_add_up_to_revenue_ex_tax():
    assert ex_tax_split(1000, 1180, 0, True) == (1000, 0.0)
    assert ex_tax_split(1000, 1180, 590, False) == (500.0, 500.0)
    row = invoice_money({"sub_total": 1000, "grand_total": 1180, "balance": 0, "status": "Paid"}, 1080, None, 0.1)
    assert row["received_ex"] == 1000 and row["due_ex"] == 0
    row = invoice_money({"sub_total": 1000, "grand_total": 1180, "balance": 1180, "status": "Unpaid"}, 0, None, 0.1)
    assert row["received_ex"] == 0 and row["due_ex"] == 1000
