"""TDS the customer withholds on an invoice (16 Oct 2026, Finance ask).

ONE way to book it, used by Customer Received Amount and by Record TDS on the
invoice page:

* the TDS is worked out from a rate on the value BEFORE GST (default — CBDT
  circular 23/2017: when GST is shown separately, TDS is on the taxable value)
  or on the invoice total; a typed amount is accepted for a customer who
  deducted an odd figure;
* it SETTLES the invoice beside the cash (`invoices.tds_deducted`; balance =
  grand_total − paid − TDS) — so an invoice the customer paid net of TDS reads
  Paid, not "Partially paid" for ever;
* and it is followed on its own `TdsRecord` as **pending from client** until
  Finance marks it received (Form 16A / 26AS), the existing "TDS payment".
"""
from __future__ import annotations

from decimal import Decimal

from fastapi import HTTPException

from models.finance import Invoice, TdsRecord, TdsStatus
from services import tax

TWO = Decimal("0.01")
MAX_RATE = Decimal("30")


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def work_out(invoice: Invoice, *, rate=None, base: str | None = None, amount=None) -> dict:
    """PURE (the invoice is only read). {rate, base, base_amount, amount}.

    A rate wins over a typed amount; with neither, the Settings default rate
    on the taxable value. 400 on a bad rate / base / amount."""
    base = (base or "taxable").lower()
    if base not in tax.TDS_BASES:
        raise HTTPException(status_code=400, detail="TDS base must be 'taxable' (before GST) or 'total'")
    if rate is not None:
        r = Decimal(str(rate))
        if r <= 0 or r > MAX_RATE:
            raise HTTPException(status_code=400, detail=f"TDS rate must be between 0 and {MAX_RATE}%")
        base_amt, amt = tax.tds_on(invoice, r, base)
        return {"rate": r.quantize(TWO), "base": base, "base_amount": base_amt, "amount": amt}
    if amount is not None:
        amt = _d(amount)
        if amt <= 0:
            raise HTTPException(status_code=400, detail="TDS amount must be more than zero")
        base_amt = _d(invoice.grand_total if base == "total" else invoice.sub_total)
        return {"rate": None, "base": base, "base_amount": base_amt, "amount": amt}
    r = tax.default_tds_rate()
    base_amt, amt = tax.tds_on(invoice, r, base)
    return {"rate": _d(r), "base": base, "base_amount": base_amt, "amount": amt}


def book(db, invoice: Invoice, figures: dict, *, section: str | None = None,
         receipt_id: int | None = None, user_id: int | None = None) -> TdsRecord:
    """Create the invoice's TdsRecord (Pending = pending from client) and take
    the TDS off its balance. 409 when one exists; 400 when the TDS is more than
    what is still open on the invoice. No commit."""
    num = invoice.invoice_number or f"#{invoice.id}"
    if invoice.tds_record is not None:
        raise HTTPException(status_code=409, detail=f"TDS is already recorded on {num}")
    amt = _d(figures["amount"])
    if amt > _d(invoice.balance_amount):
        raise HTTPException(status_code=400,
                            detail=f"TDS {float(amt):,.2f} on {num} is more than its open balance "
                                   f"{float(invoice.balance_amount or 0):,.2f}")
    record = TdsRecord(
        invoice_id=invoice.id, tds_amount=amt, tds_paid=Decimal("0"), tds_balance=amt,
        tds_status=TdsStatus.PENDING, tds_rate=figures.get("rate"), tds_base=figures.get("base"),
        base_amount=figures.get("base_amount"),
        section=(section or "").strip().upper()[:16] or None,
        receipt_id=receipt_id, created_by=user_id,
    )
    db.add(record)
    invoice.tds_record = record
    tax.apply_tds_deduction(invoice, amt)
    return record


MIN_UNDO_REASON = 10


def is_received(record: TdsRecord) -> bool:
    """Any of the TDS marked received (Form 16A in)?"""
    return _d(record.tds_paid) > 0 or bool(list(record.payments or []))


def received_block(record: TdsRecord) -> str:
    """The words for a 409 when received TDS stands in the way (20 Nov 2026 —
    the old "undo that first" named no place to undo it, and there was none)."""
    num = record.invoice.invoice_number if record.invoice else f"#{record.invoice_id}"
    return (f"TDS on {num} is already marked received (Form 16A). Undo it first: open {num} ▸ "
            f"TDS ▸ Undo 'received' — or ask Admin / CEO, who can do this in one step")


def undo_received(db, record: TdsRecord, *, reason: str = "", user=None,
                  require_reason: bool = True) -> Decimal:
    """Take back every "TDS received" (Form 16A) entry on the record: the TDS
    reads Pending from client again. Returns the amount taken back. No commit.

    A reason (≥ MIN_UNDO_REASON) is asked for and kept on the customer's
    Activity Log; the Admin one-step paths pass `require_reason=False` and say
    why themselves."""
    reason = (reason or "").strip()
    if require_reason and len(reason) < MIN_UNDO_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why the TDS receipt is undone (at least {MIN_UNDO_REASON} characters)")
    paid = _d(record.tds_paid)
    for pay in list(record.payments or []):
        record.payments.remove(pay)  # off the collection, so a later record delete cannot cascade it twice
        db.delete(pay)
    if paid:
        tax.apply_tds_payment(record, -paid)
    record.tds_status = TdsStatus.PENDING
    record.tds_paid = Decimal("0")
    record.tds_balance = _d(record.tds_amount)
    db.flush()
    invoice = record.invoice
    if invoice is not None and paid > 0 and require_reason:  # the Admin one-step paths log themselves
        try:
            from services import customer_activity
            from services.invoice_payments import invoice_customer_ids

            cid, _ = invoice_customer_ids(db, invoice)
            if cid is not None:
                num = invoice.invoice_number or f"#{invoice.id}"
                customer_activity.record(
                    db, cid, "TDS_RECEIPT_UNDONE",
                    f"TDS received of {float(paid):,.2f} on {num} undone — pending from client again"
                    + (f". Reason: {reason}" if reason else ""),
                    entity="invoice", entity_id=invoice.id, subject=num, user=user)
        except Exception:  # noqa: BLE001 — the log never blocks the undo
            pass
    return paid


def unbook(db, record: TdsRecord, *, force: bool = False, user=None, reason: str = "") -> Decimal:
    """Remove a TDS record and put its amount back on the invoice balance.
    Returns the "received" amount that was taken back with it (0 normally).

    409 once any of it was marked received — unless `force` (Admin / CEO),
    which undoes the received entries first. No commit."""
    taken_back = Decimal("0")
    if is_received(record):
        if not force:
            raise HTTPException(status_code=409, detail=received_block(record))
        taken_back = undo_received(db, record, reason=reason, user=user, require_reason=False)
    invoice = record.invoice
    if invoice is not None:
        tax.apply_tds_deduction(invoice, -_d(record.tds_amount))
        invoice.tds_record = None
    db.delete(record)
    return taken_back
