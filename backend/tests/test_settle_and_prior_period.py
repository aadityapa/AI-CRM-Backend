"""20 Nov 2026 (Finance ask): (1) Finance / Admin settle a partly paid invoice in
full — the balance is adjusted, not received; (2) a received amount may set aside
the part that pays an earlier financial year — only the rest is mapped / on
account; the customer ledger shows the adjustment.

Run:  cd backend && python -m pytest tests/test_settle_and_prior_period.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from crm_deps import CurrentUser
from tests.test_customer_receipts import FIN, _open_invoice, db  # noqa: F401 — fixture + helpers

ADMIN = CurrentUser(id=2, username="admin", full_name="Admin", roles={"Admin"})


def _partly_paid(db, number, cash):  # noqa: F811
    from routers.crm.customer_receipts import ReceiptIn, create_receipt
    cust, inv = _open_invoice(db, number, 100000, 18000)
    create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 5), amount=Decimal(cash),
                             invoice_ids=[inv.id]), db=db, user=FIN)
    db.refresh(inv)
    return cust, inv


def test_settle_in_full_marks_paid_without_counting_cash_and_can_be_undone(db):  # noqa: F811
    from routers.crm.finance import SettleIn, settle_invoice_in_full, undo_invoice_settlement
    from services import invoice_settlement

    cust, inv = _partly_paid(db, "KRNX-S1", "117950")
    assert getattr(inv.payment_status, "value", inv.payment_status) == "Partially_Paid"
    with pytest.raises(HTTPException) as err:
        settle_invoice_in_full(inv.id, SettleIn(reason="short"), db=db, user=FIN)
    assert err.value.status_code == 400
    db.rollback()

    res = settle_invoice_in_full(inv.id, SettleIn(reason="Bank charges deducted by the customer"), db=db, user=FIN)
    db.refresh(inv)
    assert getattr(inv.payment_status, "value", inv.payment_status) == "Paid"
    assert float(inv.balance_amount) == 0 and float(inv.settled_adjustment) == 50
    assert float(inv.paid_amount) == 117950          # cash is untouched
    assert res["data"]["settlement"]["amount"] == 50 and res["data"]["can_undo_settlement"] is True
    assert invoice_settlement.settle_block(inv)       # cannot settle twice

    undo_invoice_settlement(inv.id, SettleIn(reason="Customer will pay the rest"), db=db, user=ADMIN)
    db.refresh(inv)
    assert float(inv.balance_amount) == 50 and float(inv.settled_adjustment) == 0
    assert getattr(inv.payment_status, "value", inv.payment_status) == "Partially_Paid"


def test_an_unpaid_invoice_cannot_be_settled_in_full(db):  # noqa: F811
    from services import invoice_settlement
    _cust, inv = _open_invoice(db, "KRNX-S2", 100000, 18000)
    assert "partly paid" in invoice_settlement.settle_block(inv)


def test_the_ledger_shows_the_adjustment_and_closes_at_zero(db):  # noqa: F811
    from routers.crm.finance import SettleIn, settle_invoice_in_full
    from routers.crm.finance_reports import customer_ledger

    cust, inv = _partly_paid(db, "KRNX-S3", "117900")
    settle_invoice_in_full(inv.id, SettleIn(reason="Rounding difference on the payment"), db=db, user=FIN)
    led = customer_ledger(customer_id=cust.id, db=db, user=FIN)["data"]
    assert [e["type"] for e in led["entries"]] == ["Invoice", "Payment", "Adjustment"]
    assert led["closing_balance"] == 0


def test_the_earlier_year_part_is_set_aside_and_never_on_account(db):  # noqa: F811
    from models import CustomerReceipt
    from routers.crm.customer_receipts import ReceiptIn, ReceiptUpdate, create_receipt, update_receipt

    cust, inv = _open_invoice(db, "KRNX-P1", 100000, 18000)
    with pytest.raises(HTTPException):
        create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 4, 5), amount=Decimal("1000"),
                                 prior_period_amount=Decimal("1000")), db=db, user=FIN)
    db.rollback()
    # 2,00,000 credit: 50,000 paid March 2026 invoices kept outside Orbit.
    res = create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 4, 5), amount=Decimal("200000"),
                                   invoice_ids=[inv.id], prior_period_amount=Decimal("50000"),
                                   prior_period_note="FY 2025-26 invoices"), db=db, user=FIN)
    d = res["data"]
    assert d["prior_period_amount"] == 50000 and d["counted_amount"] == 150000
    assert d["allocated_amount"] == 118000 and d["unallocated_amount"] == 32000
    assert "earlier financial year" in res["message"]

    # Set it on an existing receipt later; it cannot exceed what is not mapped.
    with pytest.raises(HTTPException):
        update_receipt(d["id"], ReceiptUpdate(prior_period_amount=Decimal("90000")), db=db, user=FIN)
    db.rollback()
    update_receipt(d["id"], ReceiptUpdate(prior_period_amount=Decimal("82000")), db=db, user=FIN)
    r = db.get(CustomerReceipt, d["id"])
    assert float(r.unallocated_amount) == 0 and float(r.prior_period_amount) == 82000
