"""Recording and reversing a payment on ONE invoice (2 Nov 2026, Finance / Admin ask).

Record Payment on the invoice page used to write a stand-alone `InvoicePayment`
that the Received Amount register never saw, and nothing could take it back.

  * `record_payment` — the optional link to Received Amount:
      - `receipt_id`        take the money from a received amount already on
                            account for this customer (the receipt's date / mode
                            / reference are the payment's — it is the same credit);
      - `add_to_received`   also write the payment as a new received amount,
                            mapped to this invoice, so the register is complete.
    Neither = the old stand-alone payment.
  * `reverse_payment` — undo one payment with a reason (≥ 10 chars). A payment
    that came from a received amount goes back ON ACCOUNT on that receipt (the
    bank credit is real; only the mapping was wrong); `remove_receipt` also
    deletes the receipt when nothing else is left on it. The invoice balance and
    status are recomputed; the event is written on the customer's Activity Log.

Callers commit.
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CustomerBranch, CustomerReceipt, Invoice, InvoicePayment, Project
from models.finance import TdsRecord
from services import tax

TWO = Decimal("0.01")
MIN_REVERSE_REASON = 10


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def invoice_customer_ids(db: Session, invoice: Invoice) -> tuple[int | None, set[int]]:
    """(the invoice's customer — PO's else project's, every customer it can
    take money from — project's and PO's)."""
    from models import PurchaseOrder
    from services.billing_rules import invoice_customer_id

    proj_cid = db.execute(select(Project.customer_id).where(Project.id == invoice.project_id)).scalar() \
        if invoice.project_id else None
    po_cid = db.execute(select(PurchaseOrder.customer_id).where(PurchaseOrder.id == invoice.po_id)).scalar() \
        if invoice.po_id else None
    return invoice_customer_id(proj_cid, po_cid), {c for c in (proj_cid, po_cid) if c}


def invoice_branch(db: Session, invoice: Invoice) -> tuple[int | None, str | None]:
    from services.invoice_facets import invoice_facets

    f = invoice_facets(db, [invoice]).get(invoice.id) or {}
    return f.get("branch_id"), f.get("branch_name")


def receipts_on_account(db: Session, invoice: Invoice) -> list[dict]:
    """Received amounts of this invoice's customer that still have money on
    account and may pay it (the receipt's branch is the invoice's, or either
    has none). Newest first."""
    _, cids = invoice_customer_ids(db, invoice)
    if not cids:
        return []
    branch_id, _ = invoice_branch(db, invoice)
    rows = db.execute(
        select(CustomerReceipt).where(CustomerReceipt.customer_id.in_(cids),
                                      CustomerReceipt.unallocated_amount > 0)
        .order_by(CustomerReceipt.received_date.desc(), CustomerReceipt.id.desc())
    ).scalars().all()
    rows = [r for r in rows if branch_id is None or r.branch_id in (None, branch_id)]
    names = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name).where(
        CustomerBranch.id.in_({r.branch_id for r in rows if r.branch_id}))).all()) if rows else {}
    return [{
        "id": r.id,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "amount": float(r.amount or 0),
        "unallocated_amount": float(r.unallocated_amount or 0),
        "payment_mode": r.payment_mode,
        "reference_number": r.reference_number,
        "branch_id": r.branch_id,
        "branch_name": names.get(r.branch_id),
    } for r in rows]


def _branch_ok(receipt: CustomerReceipt, inv_branch_id) -> bool:
    return receipt.branch_id is None or inv_branch_id is None or receipt.branch_id == inv_branch_id


def record_payment(db: Session, invoice: Invoice, *, payment_date, amount, payment_mode=None,
                   reference_number=None, notes=None, attachment_url=None,
                   receipt_id: int | None = None, add_to_received: bool = False,
                   user=None) -> InvoicePayment:
    """Write one payment on the invoice (amount already validated against the
    balance by the caller). See the module docstring for the two links."""
    if receipt_id is not None and add_to_received:
        raise HTTPException(status_code=400,
                            detail="Choose one: take it from a received amount, or add it as a new one")
    amount = _d(amount)
    uid = getattr(user, "id", None)
    ref = (reference_number or "").strip() or None
    note = (notes or "").strip() or None
    receipt: CustomerReceipt | None = None

    if receipt_id is not None:
        receipt = db.get(CustomerReceipt, receipt_id)
        if receipt is None:
            raise HTTPException(status_code=404, detail="Received amount not found")
        _, cids = invoice_customer_ids(db, invoice)
        if receipt.customer_id not in cids:
            raise HTTPException(status_code=400, detail="That received amount is from another customer")
        inv_branch_id, inv_branch_name = invoice_branch(db, invoice)
        if not _branch_ok(receipt, inv_branch_id):
            raise HTTPException(status_code=400,
                                detail=f"That received amount was paid by another branch, not {inv_branch_name}")
        if amount > _d(receipt.unallocated_amount):
            raise HTTPException(status_code=400,
                                detail=f"Only {float(_d(receipt.unallocated_amount)):,.2f} of that received "
                                       f"amount is still on account")
        receipt.unallocated_amount = (_d(receipt.unallocated_amount) - amount).quantize(TWO)
        if receipt.branch_id is None and inv_branch_id is not None:
            receipt.branch_id = inv_branch_id
        # The payment IS that bank credit: its date / mode / reference.
        payment_date = receipt.received_date
        payment_mode = receipt.payment_mode
        ref = receipt.reference_number
        note = note or f"From customer receipt #{receipt.id}"
        attachment_url = attachment_url or receipt.proof_url

    elif add_to_received:
        cid, _ = invoice_customer_ids(db, invoice)
        if cid is None:
            raise HTTPException(status_code=400,
                                detail="This invoice has no customer on record — it cannot go to Received Amount")
        inv_branch_id, _ = invoice_branch(db, invoice)
        proof = (attachment_url or "").strip() or None
        if proof and (not proof.startswith("/api/crm-files/") or ".." in proof):
            proof = None
        receipt = CustomerReceipt(
            customer_id=cid, branch_id=inv_branch_id, received_date=payment_date, amount=amount,
            payment_mode=payment_mode or None, reference_number=ref, notes=note,
            unallocated_amount=Decimal("0"), tds_total=Decimal("0"), created_by=uid,
            proof_url=proof, proof_name=(proof.rsplit("/", 1)[-1][:255] if proof else None),
        )
        db.add(receipt)
        db.flush()
        note = note or f"From customer receipt #{receipt.id}"

    payment = InvoicePayment(
        invoice_id=invoice.id, payment_date=payment_date, amount=amount,
        payment_mode=payment_mode or None, reference_number=ref, notes=note,
        attachment_url=attachment_url, receipt_id=getattr(receipt, "id", None), created_by=uid,
    )
    db.add(payment)
    tax.apply_invoice_payment(invoice, amount)
    db.flush()
    return payment


def reverse_payment(db: Session, invoice: Invoice, payment: InvoicePayment, *, reason: str,
                    remove_receipt: bool = False, user=None) -> dict:
    """Undo one payment. Returns what happened (for the message)."""
    reason = (reason or "").strip()
    if len(reason) < MIN_REVERSE_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why the payment is reversed (at least {MIN_REVERSE_REASON} characters)")
    if payment.invoice_id != invoice.id:
        raise HTTPException(status_code=404, detail="That payment is not on this invoice")
    amount = _d(payment.amount)
    receipt = db.get(CustomerReceipt, payment.receipt_id) if payment.receipt_id else None
    out = {"amount": float(amount), "receipt_id": getattr(receipt, "id", None),
           "back_on_account": False, "receipt_removed": False}

    tax.apply_invoice_payment(invoice, -amount)
    db.delete(payment)
    db.flush()

    if receipt is not None:
        receipt.unallocated_amount = (_d(receipt.unallocated_amount) + amount).quantize(TWO)
        out["back_on_account"] = True
        if remove_receipt:
            others = db.execute(select(InvoicePayment.id).where(
                InvoicePayment.receipt_id == receipt.id).limit(1)).first()
            tds = db.execute(select(TdsRecord.id).where(TdsRecord.receipt_id == receipt.id).limit(1)).first()
            if others or tds:
                raise HTTPException(
                    status_code=409,
                    detail=f"Received amount #{receipt.id} still pays other invoices — the payment can be "
                           f"reversed, but the received amount itself stays (untick 'remove it too')")
            db.delete(receipt)
            out["receipt_removed"] = True
            out["back_on_account"] = False

    cid, _ = invoice_customer_ids(db, invoice)
    if cid is not None:
        from services import customer_activity

        num = invoice.invoice_number or f"#{invoice.id}"
        where = (" — received amount removed" if out["receipt_removed"]
                 else f" — back on account on received amount #{receipt.id}" if receipt is not None else "")
        customer_activity.record(
            db, cid, "PAYMENT_REVERSED",
            f"Payment of {float(amount):,.2f} on {num} reversed{where}. Reason: {reason}",
            entity="invoice", entity_id=invoice.id, subject=num, user=user)
    return out
