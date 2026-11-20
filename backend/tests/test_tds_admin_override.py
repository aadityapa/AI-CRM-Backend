"""20 Nov 2026 (Admin report with screenshots) — a partly paid invoice whose TDS
was marked received (Form 16A) could not be unmapped or deleted, not even by
Admin, and "undo that first" named no place to undo it (there was none).

Now: Finance / Admin can **Undo "received"** on the invoice's TDS; Admin / CEO
unmap, delete the receipt or delete the invoice in ONE step (the received TDS
is undone with it); Finance gets a message that says where to undo it.

Run:  cd backend && python -m pytest tests/test_tds_admin_override.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from crm_deps import CurrentUser
from tests.test_customer_receipts import FIN, _open_invoice, db  # noqa: F401 — fixture + helpers

ADMIN = CurrentUser(id=2, username="admin", full_name="Admin", roles={"Admin"})


def _received_receipt(db, number):  # noqa: F811
    from models.finance import TdsPayment
    from routers.crm.customer_receipts import ReceiptIn, ReceiptTdsIn, create_receipt
    from services import tax

    cust, inv = _open_invoice(db, number, 100000, 18000)
    # Partly paid: 50,000 cash + 10 % TDS on the value before GST.
    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal("50000"),
                                   invoice_ids=[inv.id],
                                   tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                         db=db, user=FIN)
    rec = inv.tds_record
    db.add(TdsPayment(tds_record_id=rec.id, payment_date=date(2026, 11, 1), amount=rec.tds_amount))
    tax.apply_tds_payment(rec, rec.tds_amount)
    db.commit()
    db.refresh(inv)
    assert getattr(inv.payment_status, "value", inv.payment_status) == "Partially_Paid"
    return res["data"]["id"], inv


def test_finance_is_told_where_to_undo_and_admin_unmaps_in_one_step(db):  # noqa: F811
    from routers.crm.customer_receipts import unmap_invoice

    rid, inv = _received_receipt(db, "KRNX-1")
    with pytest.raises(HTTPException) as err:
        unmap_invoice(rid, inv.id, db=db, user=FIN)
    assert err.value.status_code == 409
    assert "Undo 'received'" in err.value.detail and "Admin" in err.value.detail
    db.rollback()

    out = unmap_invoice(rid, inv.id, db=db, user=ADMIN)
    db.refresh(inv)
    assert inv.tds_record is None and float(inv.tds_deducted) == 0
    assert float(inv.paid_amount) == 0 and float(inv.balance_amount) == 118000
    assert out["data"]["unallocated_amount"] == 50000 and out["data"]["tds_total"] == 0
    assert "marked received" in out["message"]


def test_finance_can_undo_received_with_a_reason_then_unmap(db):  # noqa: F811
    from routers.crm.customer_receipts import unmap_invoice
    from routers.crm.finance import TdsUndoReceivedIn, undo_tds_received

    rid, inv = _received_receipt(db, "KRNX-2")
    with pytest.raises(HTTPException) as err:
        undo_tds_received(inv.id, TdsUndoReceivedIn(reason="oops"), db=db, user=FIN)
    assert err.value.status_code == 400
    db.rollback()
    res = undo_tds_received(inv.id, TdsUndoReceivedIn(reason="Marked on the wrong invoice"), db=db, user=FIN)
    assert res["data"]["tds_paid"] == 0 and "pending from client again" in res["message"]
    db.refresh(inv)
    assert inv.tds_record.payments == [] and float(inv.tds_deducted) == 10000  # the deduction stays
    unmap_invoice(rid, inv.id, db=db, user=FIN)   # now Finance can unmap
    db.refresh(inv)
    assert float(inv.balance_amount) == 118000


def test_admin_deletes_a_paid_invoice_and_the_cash_goes_back_on_account(db):  # noqa: F811
    from models import CustomerReceipt
    from routers.crm.finance import delete_invoice

    rid, inv = _received_receipt(db, "KRNX-3")
    inv_id = inv.id
    with pytest.raises(HTTPException) as err:
        delete_invoice(inv_id, db=db, user=FIN)
    assert err.value.status_code == 409 and "Undo 'received'" in err.value.detail
    db.rollback()

    res = delete_invoice(inv_id, db=db, user=ADMIN)
    assert "back on account" in res["message"]
    receipt = db.get(CustomerReceipt, rid)
    assert float(receipt.unallocated_amount) == 50000 and float(receipt.tds_total) == 0


def test_admin_deletes_the_receipt_with_received_tds(db):  # noqa: F811
    from routers.crm.customer_receipts import delete_receipt

    rid, inv = _received_receipt(db, "KRNX-4")
    with pytest.raises(HTTPException):
        delete_receipt(rid, db=db, user=FIN)
    db.rollback()
    delete_receipt(rid, db=db, user=ADMIN)
    db.refresh(inv)
    assert inv.tds_record is None and float(inv.balance_amount) == 118000
