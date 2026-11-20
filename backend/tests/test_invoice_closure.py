"""Admin / CEO final approval of an invoice (20 Nov 2026): step 4 of the journey
is the sign-off once the invoice is collected in full and its TDS received.

Run:  cd backend && python -m pytest tests/test_invoice_closure.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException

from crm_deps import CurrentUser
from tests.test_customer_receipts import FIN, _open_invoice, db  # noqa: F401 — fixture
from routers.crm.customer_receipts import ReceiptIn, ReceiptTdsIn, create_receipt
from routers.crm.tds_receipts import TdsMapItem, TdsReceiptIn, create_tds_receipt, unmap_tds_invoice

ADMIN = CurrentUser(id=2, username="admin", full_name="Admin", roles={"Admin"})


def _paid_with_tds_pending(db, number):  # noqa: F811
    cust, inv = _open_invoice(db, number, 1000, 180)
    create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("1080"),
                             invoice_ids=[inv.id], tds=[ReceiptTdsIn(invoice_id=inv.id, tds_rate=Decimal("10"))]),
                   db=db, user=FIN)
    db.refresh(inv)
    return cust, inv


def test_an_invoice_waits_until_the_tds_is_received_then_admin_approves(db):  # noqa: F811
    from routers.crm.finance import ClosureIn, ClosureUndoIn, approve_invoice_closure, undo_invoice_closure  # noqa: E501
    from services import invoice_closure

    cust, inv = _paid_with_tds_pending(db, "INV-C1")
    assert invoice_closure.state_of(inv) == "waiting"
    missing = [c["key"] for c in invoice_closure.closure_checks(inv) if not c["ok"]]
    assert missing == ["tds"]
    with pytest.raises(HTTPException) as err:
        approve_invoice_closure(inv.id, ClosureIn(), db=db, user=ADMIN)
    assert err.value.status_code == 409 and "TDS" in err.value.detail
    db.rollback()

    tr = create_tds_receipt(TdsReceiptIn(customer_id=cust.id, received_date=date(2026, 11, 15), amount=Decimal("100"),
                                         items=[TdsMapItem(invoice_id=inv.id)]), db=db, user=FIN)["data"]
    db.refresh(inv)
    assert invoice_closure.state_of(inv) == "ready"

    # Finance cannot approve — Admin / CEO only.
    with pytest.raises(HTTPException) as err:
        invoice_closure.approve(db, inv, FIN)
    assert err.value.status_code == 403

    res = approve_invoice_closure(inv.id, ClosureIn(note="Checked against the bank statement"), db=db, user=ADMIN)
    block = res["data"]["closure"]
    assert block["approved"] is True and block["state"] == "approved" and block["can_undo"] is True
    assert block["approved_by_name"] in ("Admin", None)

    # The TDS taken back after approval → "changed", never silently dropped.
    unmap_tds_invoice(tr["id"], inv.id, db=db, user=FIN)
    db.refresh(inv)
    assert invoice_closure.state_of(inv) == "changed"
    with pytest.raises(HTTPException):
        undo_invoice_closure(inv.id, ClosureUndoIn(reason="short"), db=db, user=ADMIN)
    db.rollback()
    undo_invoice_closure(inv.id, ClosureUndoIn(reason="TDS mapping was wrong, re-check"), db=db, user=ADMIN)
    db.refresh(inv)
    assert inv.closure_approved_at is None and invoice_closure.state_of(inv) == "waiting"


def test_the_dashboard_queue_counts_each_state_and_bulk_approves(db):  # noqa: F811
    from routers.crm.dashboards import invoice_approvals
    from routers.crm.finance import BulkClosureIn, approve_invoice_closure_bulk

    cust, a = _open_invoice(db, "INV-C2", 1000, 180)
    create_receipt(ReceiptIn(customer_id=cust.id, received_date=date(2026, 10, 1), amount=Decimal("1180"),
                             invoice_ids=[a.id]), db=db, user=FIN)
    _c, b = _open_invoice(db, "INV-C3", 2000, 360)          # nothing received yet
    out = invoice_approvals(state="ready", customer_id=None, month=None, search=None, page=1, limit=25,
                            db=db, user=ADMIN)
    meta = out["meta"]
    assert [r["invoice_number"] for r in out["data"]] == ["INV-C2"]
    assert meta["counts"]["ready"] == 1 and meta["counts"]["waiting"] == 1
    assert meta["summary"]["missing"][0]["label"] == "Payment collected"

    res = approve_invoice_closure_bulk(BulkClosureIn(invoice_ids=[a.id, b.id]), db=db, user=ADMIN)["data"]
    assert res["approved"] == [a.id] and res["skipped"][0]["invoice_id"] == b.id
    out = invoice_approvals(state="approved", customer_id=None, month=None, search=None, page=1, limit=25,
                            db=db, user=ADMIN)
    assert [r["invoice_number"] for r in out["data"]] == ["INV-C2"]
    assert out["meta"]["summary"]["approved_this_month"] == 1
