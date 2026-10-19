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


def unbook(db, record: TdsRecord) -> None:
    """Remove a TDS record and put its amount back on the invoice balance.
    409 once any of it was marked received. No commit."""
    if _d(record.tds_paid) > 0 or list(record.payments or []):
        num = record.invoice.invoice_number if record.invoice else f"#{record.invoice_id}"
        raise HTTPException(status_code=409,
                            detail=f"TDS on {num} was already marked received — undo that first")
    invoice = record.invoice
    if invoice is not None:
        tax.apply_tds_deduction(invoice, -_d(record.tds_amount))
        invoice.tds_record = None
    db.delete(record)
