"""Settle a partly paid invoice in full (20 Nov 2026, Finance ask).

A customer often pays a few rupees short (bank charges, rounding, a deduction
they will not make good). Finance / Admin may then close the invoice as Paid:
the balance left is stored as `invoices.settled_adjustment` (0154) — it settles
the invoice beside cash and TDS (`tax.refresh_invoice_balance`) but it is NOT
money received, so Collected / Received Bank never count it. The reason is
required and kept on the customer's Activity Log; it can be undone.

Who: Finance by role, Admin / CEO (the route is `role_required("Finance")`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException

from models.finance import Invoice, InvoiceKind
from services import tax

TWO = Decimal("0.01")
MIN_REASON = 10


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def settle_block(invoice: Invoice) -> str | None:
    """Why this invoice cannot be settled in full now (None = it can). PURE."""
    if (invoice.kind or InvoiceKind.TAX.value) == InvoiceKind.PROFORMA.value:
        return "A Proforma is not an issued invoice — generate the tax invoice first"
    if _d(invoice.settled_adjustment) > 0:
        return "Already settled in full — undo the settlement first to change it"
    if _d(invoice.balance_amount) <= 0:
        return "Nothing is left to settle — the invoice is already paid"
    if _d(invoice.paid_amount) <= 0 and _d(getattr(invoice, "tds_deducted", 0)) <= 0:
        return "Only a partly paid invoice can be settled in full — record the payment received first"
    return None


def _log(db, invoice: Invoice, action: str, text: str, user) -> None:
    try:
        from services import customer_activity
        from services.invoice_payments import invoice_customer_ids

        cid, _ = invoice_customer_ids(db, invoice)
        if cid is not None:
            customer_activity.record(db, cid, action, text, entity="invoice", entity_id=invoice.id,
                                     subject=invoice.invoice_number or f"#{invoice.id}", user=user)
    except Exception:  # noqa: BLE001 — the log never blocks the money
        pass


def settle_in_full(db, invoice: Invoice, *, reason: str, user=None) -> Decimal:
    """Adjust the open balance and mark the invoice Paid. Returns the adjustment. No commit."""
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why the balance is settled (at least {MIN_REASON} characters)")
    block = settle_block(invoice)
    if block:
        raise HTTPException(status_code=409, detail=block)
    adj = _d(invoice.balance_amount)
    invoice.settled_adjustment = adj
    invoice.settled_reason = reason[:1000]
    invoice.settled_by = getattr(user, "id", None)
    invoice.settled_at = datetime.now(timezone.utc)
    tax.refresh_invoice_balance(invoice)
    num = invoice.invoice_number or f"#{invoice.id}"
    _log(db, invoice, "INVOICE_SETTLED_IN_FULL",
         f"{num} settled in full — balance of {float(adj):,.2f} adjusted (not received). Reason: {reason}", user)
    return adj


def undo_settlement(db, invoice: Invoice, *, reason: str = "", user=None) -> Decimal:
    """Take the adjustment back — the balance is open again. No commit."""
    adj = _d(invoice.settled_adjustment)
    if adj <= 0:
        raise HTTPException(status_code=409, detail="This invoice was not settled in full")
    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why the settlement is undone (at least {MIN_REASON} characters)")
    invoice.settled_adjustment = Decimal("0")
    invoice.settled_reason = None
    invoice.settled_by = None
    invoice.settled_at = None
    tax.refresh_invoice_balance(invoice)
    num = invoice.invoice_number or f"#{invoice.id}"
    _log(db, invoice, "INVOICE_SETTLEMENT_UNDONE",
         f"Settlement of {num} undone — {float(adj):,.2f} is open again. Reason: {reason}", user)
    return adj


def payload(invoice: Invoice, names: dict | None = None) -> dict | None:
    """The `settlement` block of the invoice page (None = not settled). PURE."""
    adj = _d(getattr(invoice, "settled_adjustment", 0))
    if adj <= 0:
        return None
    at = getattr(invoice, "settled_at", None)
    by = getattr(invoice, "settled_by", None)
    return {"amount": float(adj), "reason": invoice.settled_reason,
            "by": by, "by_name": (names or {}).get(by or 0),
            "at": at.isoformat() if at is not None else None}
