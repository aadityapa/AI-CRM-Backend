"""Indian tax compliance layer (GST + TDS) — deliberately decoupled from core
business logic per spec. Pure functions; no DB access here.

GST: intra-state → SGST + CGST (each = slab/2); inter-state → IGST = slab.
TDS: Tax Deducted at Source on invoices (default rate via TDS_RATE_PERCENT env, 10% = Sec 194J).
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP

TWO_PLACES = Decimal("0.01")


def _d(value) -> Decimal:
    return Decimal(str(value or 0))


def split_gst(tax_slab, inter_state: bool = False) -> dict:
    """Split a GST slab %% into SGST/CGST/IGST components."""
    slab = _d(tax_slab)
    if inter_state:
        return {"sgst": Decimal("0"), "cgst": Decimal("0"), "igst": slab}
    half = (slab / 2).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    return {"sgst": half, "cgst": half, "igst": Decimal("0")}


def gst_amount(sub_total, tax_slab) -> Decimal:
    return (_d(sub_total) * _d(tax_slab) / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def gst_component_amounts(base, sgst_pct=None, cgst_pct=None, igst_pct=None) -> dict:
    """GST component *amounts* on a taxable base from stored percentage rates.

    Each amount = pct x base / 100 (half-up, 2 places); tax_amount is their sum.
    Returns {"sgst_amount", "cgst_amount", "igst_amount", "tax_amount"}.
    """
    b = _d(base)

    def _amt(pct) -> Decimal:
        return (b * _d(pct) / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    sgst_amount = _amt(sgst_pct)
    cgst_amount = _amt(cgst_pct)
    igst_amount = _amt(igst_pct)
    return {
        "sgst_amount": sgst_amount,
        "cgst_amount": cgst_amount,
        "igst_amount": igst_amount,
        "tax_amount": sgst_amount + cgst_amount + igst_amount,
    }


def default_tds_rate() -> Decimal:
    """Settings row → TDS_RATE_PERCENT env → 10 (Sec 194J).

    Admin-editable (Settings → Organisation → Finance) because a statutory
    rate change should be a form edit, not a deploy. Falls back hard to 10 on
    any bad value — a typo must never zero the deduction.
    """
    try:
        from services.org_settings import setting

        return _d(setting("finance.tds_rate_percent") or "10")
    except Exception:
        return _d(os.getenv("TDS_RATE_PERCENT", "10"))


def tds_amount(sub_total, rate=None) -> Decimal:
    r = _d(rate) if rate is not None else default_tds_rate()
    return (_d(sub_total) * r / 100).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# --- Balance recomputation (pure; models passed in, caller commits) ---

def apply_invoice_payment(invoice, amount) -> None:
    """Record a payment amount on an Invoice: updates paid/balance/status."""
    invoice.paid_amount = (_d(invoice.paid_amount) + _d(amount)).quantize(TWO_PLACES)
    invoice.balance_amount = (_d(invoice.grand_total) - _d(invoice.paid_amount)).quantize(TWO_PLACES)
    if invoice.balance_amount <= 0:
        invoice.payment_status = "Paid"
        invoice.balance_amount = max(invoice.balance_amount, Decimal("0"))
    elif _d(invoice.paid_amount) > 0:
        invoice.payment_status = "Partially_Paid"
    else:
        invoice.payment_status = "Unpaid"


def apply_tds_payment(tds_record, amount) -> None:
    tds_record.tds_paid = (_d(tds_record.tds_paid) + _d(amount)).quantize(TWO_PLACES)
    tds_record.tds_balance = (_d(tds_record.tds_amount) - _d(tds_record.tds_paid)).quantize(TWO_PLACES)
    if tds_record.tds_balance <= 0:
        tds_record.tds_status = "Paid"
        tds_record.tds_balance = max(tds_record.tds_balance, Decimal("0"))
    elif _d(tds_record.tds_paid) > 0:
        tds_record.tds_status = "Partially_Paid"
    else:
        tds_record.tds_status = "Pending"


def apply_po_consumption(po, delta) -> None:
    """Adjust PO consumed/balance when an invoice is created (+) or cancelled (-)."""
    po.consumed_value = (_d(po.consumed_value) + _d(delta)).quantize(TWO_PLACES)
    po.balance_value = (_d(po.total_value) - _d(po.consumed_value)).quantize(TWO_PLACES)
    if po.balance_value <= 0:
        po.status = "Exhausted"
        po.balance_value = max(po.balance_value, Decimal("0"))
    elif str(po.status) != "Cancelled":
        po.status = "Active"
