"""Received TDS (20 Nov 2026): record the TDS a customer deposited once, map it
onto invoices (amount per invoice editable), link it with Received Amount both
ways; plus editable amounts on Add received amount and bulk Settle in full.

Run:  cd backend && python -m pytest tests/test_tds_receipts.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from tests.test_customer_receipts import FIN, _open_invoice, db  # noqa: F401 — fixture
from routers.crm.customer_receipts import AllocateItemIn, ReceiptIn, ReceiptTdsIn, create_receipt
from routers.crm.tds_receipts import (
    TdsAllocateIn, TdsMapItem, TdsReceiptIn, allocate_tds_receipt, create_tds_receipt,
    delete_tds_receipt, invoice_options, list_tds_receipts, plan_tds, unmap_tds_invoice,
)


def test_mapping_marks_booked_tds_received_and_links_the_received_amount(db):  # noqa: F811
    cust, inv = _open_invoice(db, "INV-R1", 1000, 180)
    rc = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("1080"),
                                  invoice_ids=[inv.id], tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                        db=db, user=FIN)["data"]
    db.refresh(inv)
    assert float(inv.tds_record.tds_balance) == 100 and float(inv.balance_amount) == 0
    out = create_tds_receipt(TdsReceiptIn(customer_id=cust.id, received_date=date(2026, 11, 15), amount=Decimal("100"),
                                          certificate_number="16A-001", items=[TdsMapItem(invoice_id=inv.id)]),
                             db=db, user=FIN)["data"]
    db.refresh(inv)
    assert float(inv.tds_record.tds_balance) == 0 and out["unallocated_amount"] == 0
    assert [r["id"] for r in out["linked_receipts"]] == [rc["id"]]
    # …and the received amount names the TDS entry.
    from routers.crm.customer_receipts import get_receipt
    assert [t["id"] for t in get_receipt(rc["id"], db=db, user=FIN)["data"]["tds_receipts"]] == [out["id"]]
    # Unmap: the TDS reads pending from client again, the booking stays (it was the receipt's).
    unmap_tds_invoice(out["id"], inv.id, db=db, user=FIN)
    db.refresh(inv)
    assert float(inv.tds_record.tds_balance) == 100 and inv.tds_record.tds_status in ("Pending", "Pending")


def test_an_invoice_with_no_tds_is_booked_by_the_mapping_and_unmapping_removes_it(db):  # noqa: F811
    cust, inv = _open_invoice(db, "INV-R2", 1000, 180)
    tr = create_tds_receipt(TdsReceiptIn(customer_id=cust.id, received_date=date(2026, 11, 15), amount=Decimal("150")),
                            db=db, user=FIN)["data"]
    assert tr["unallocated_amount"] == 150
    opts = invoice_options(cust.id, db=db, user=FIN)["data"]
    row = next(r for r in opts if r["id"] == inv.id)
    assert row["has_tds"] is False and row["tds_cap"] == 1180
    # Finance edits the amount at mapping: 120 (not the default 10 % = 100).
    out = allocate_tds_receipt(tr["id"], TdsAllocateIn(items=[TdsMapItem(invoice_id=inv.id, amount=Decimal("120"))]),
                               db=db, user=FIN)["data"]
    db.refresh(inv)
    assert float(inv.tds_deducted) == 120 and float(inv.balance_amount) == 1060
    assert inv.tds_record.tds_status in ("Paid",) and out["unallocated_amount"] == 30
    assert out["allocations"][0]["booked_here"] is True
    delete_tds_receipt(tr["id"], db=db, user=FIN)
    db.refresh(inv)
    assert inv.tds_record is None and float(inv.balance_amount) == 1180


def test_plan_refuses_more_than_pending_or_left(db):  # noqa: F811
    cust, inv = _open_invoice(db, "INV-R3", 1000, 180)
    with pytest.raises(ValueError):
        plan_tds(Decimal("50"), [(inv, Decimal("60"))])
    with pytest.raises(ValueError):
        plan_tds(Decimal("5000"), [(inv, Decimal("2000"))])
    plan, left = plan_tds(Decimal("500"), [(inv, None)])
    assert float(plan[0][1]) == 100 and float(left) == 400
    assert list_tds_receipts(customer_id=cust.id, p=_pp(), db=db, user=FIN)["meta"]["total_received"] == 0


def _pp():
    from crm_deps import PageParams
    return PageParams(page=1, limit=25, search=None, sort_by=None, sort_dir="desc")


def test_add_received_amount_takes_an_amount_per_invoice_and_maps_unapproved_invoices(db):  # noqa: F811
    cust, inv = _open_invoice(db, "INV-R4", 1000, 0)
    inv.customer_approved_at = None
    inv.irn_number = None
    db.commit()
    out = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("1000"),
                                   items=[AllocateItemIn(invoice_id=inv.id, amount=Decimal("700"))]),
                         db=db, user=FIN)["data"]
    db.refresh(inv)
    assert float(inv.paid_amount) == 700 and out["unallocated_amount"] == 300
    with pytest.raises(HTTPException):
        create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 2), amount=Decimal("1000"),
                                 items=[AllocateItemIn(invoice_id=inv.id, amount=Decimal("900"))]),
                       db=db, user=FIN)


def test_bulk_settle_in_full(db):  # noqa: F811
    from routers.crm.finance import BulkSettleIn, settle_invoices_in_full_bulk
    cust, a = _open_invoice(db, "INV-S1", 100, 0)
    _, b = _open_invoice(db, "INV-S2", 100, 0)
    create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("99.50"),
                             invoice_ids=[a.id]), db=db, user=FIN)
    with pytest.raises(HTTPException):
        settle_invoices_in_full_bulk(BulkSettleIn(invoice_ids=[a.id], reason="x"), db=db, user=FIN)
    out = settle_invoices_in_full_bulk(BulkSettleIn(invoice_ids=[a.id, b.id], reason="Rounding left by the bank"),
                                       db=db, user=FIN)["data"]
    assert out["settled"] == ["INV-S1"] and out["skipped"][0]["invoice_number"] == "INV-S2"
    db.refresh(a)
    assert float(a.balance_amount) == 0
