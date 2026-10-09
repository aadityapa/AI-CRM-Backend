"""Customer approval of a tax invoice + the GST e-invoice IRN (5 Oct 2026).

The flow the business asked for, after Finance generates the ORIGINAL invoice
(`services/proforma.convert_to_tax_invoice`):

    Sales Manager / Sales Head sends the invoice to the customer
      → the customer accepts it unchanged → the Sales Manager / Sales Head
        CONFIRMS that here (`confirm_customer_approval`), and Finance is told
        (a change the customer wants goes through an invoice change request
        instead — `routers/crm/invoice_revisions.py` — never through here)
      → Finance records the e-invoice IRN and Acknowledgement No. on the
        invoice (`record_irn`); the invoice then sits in Finance's
        "Customer approved invoices".

Who may do what (role based on purpose — a template grant never widens it):

* confirm — `CONFIRM_ROLES` (Sales Head, the Sales Manager custom role) + Admin/CEO;
* see / record the IRN — `IRN_ROLES` (Finance) + Admin/CEO. Nobody else ever
  receives the IRN fields (`payload` leaves the key out).
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from models import Invoice

EVENT_CUSTOMER_APPROVED = "invoice.customer_approved"   # → Finance

CONFIRM_ROLES = ("Sales_Head", "Sales Manager")
IRN_ROLES = ("Finance",)
ADMIN_ROLES = ("Admin", "CEO")

#: A GST e-invoice IRN is the 64-character SHA-256 hex digest the IRP returns.
IRN_RE = re.compile(r"^[0-9a-f]{64}$")
#: The IRP Acknowledgement No. is numeric (15 digits today); allow 10–20.
ACK_RE = re.compile(r"^\d{10,20}$")
MAX_NOTE = 1000


def _roles(user) -> set[str]:
    """Role names, lower-cased — a custom role ("Sales Manager") is matched by
    name whatever its capitalisation."""
    return {str(r).lower() for r in (getattr(user, "roles", None) or ())}


def _has(user, names) -> bool:
    return bool(_roles(user) & {n.lower() for n in names})


def may_confirm(user) -> bool:
    """Sales Head / Sales Manager / Admin / CEO — by role."""
    return _has(user, CONFIRM_ROLES + ADMIN_ROLES)


def may_see_irn(user) -> bool:
    """Finance / Admin / CEO — by role. Everyone else never gets the IRN."""
    return _has(user, IRN_ROLES + ADMIN_ROLES)


def is_approved(invoice: Invoice) -> bool:
    return invoice.customer_approved_at is not None


def irn_recorded(invoice: Invoice) -> bool:
    return bool(invoice.irn_number)


def confirm_block(invoice: Invoice) -> str | None:
    """Why this invoice cannot be confirmed now (None = it can). PURE."""
    if invoice.is_proforma:
        return "A Proforma is not sent to the customer — Finance generates the original invoice first."
    if is_approved(invoice):
        return "The customer approval is already confirmed."
    return None


def clean_irn(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def clean_ack(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def irn_error(irn: str, ack: str, ack_date: date | None, today: date | None = None) -> str | None:
    """Validation message for an IRN entry, or None. PURE."""
    if not IRN_RE.match(irn):
        return "The IRN is the 64-character code from the e-invoice portal (letters a–f and digits)."
    if not ACK_RE.match(ack):
        return "The Acknowledgement No. is the number from the e-invoice portal (digits only)."
    if ack_date is not None and ack_date > (today or date.today()):
        return "The acknowledgement date cannot be in the future."
    return None


def payment_block(invoice: Invoice) -> str | None:
    """Why a payment / TDS cannot be recorded yet (None = it can). PURE.

    The flow (8 Oct 2026, user rule): the customer approves the invoice → the
    Sales Manager / Sales Head confirms it → Finance records the IRN and Ack
    No. → THEN money is recorded against the e-invoice. An invoice that already
    has a payment or a TDS record is grandfathered — it was mid-collection
    before this rule, and a half-recorded receipt must be finishable."""
    if invoice.is_proforma:
        return None   # `require_tax_invoice` answers that one
    if list(getattr(invoice, "payments", None) or []) or getattr(invoice, "tds_record", None) is not None:
        return None
    if not is_approved(invoice):
        return ("Waiting for the customer's approval — the Sales Manager / Sales Head confirms it, "
                "then Finance adds the IRN. Payments open after that.")
    if not irn_recorded(invoice):
        return "Add the e-invoice IRN and Ack No. first — payments are recorded against the e-invoice."
    return None


def require_payment_open(invoice: Invoice) -> None:
    block = payment_block(invoice)
    if block:
        raise HTTPException(status_code=409, detail=block)


def _log(db: Session, invoice: Invoice, user_id: int | None, action: str, note: str) -> None:
    """On the source timesheet's activity log (where invoice events already go)."""
    if not invoice.timesheet_id:
        return
    from models import TimesheetActivityLog
    from services.crm_common import log_activity

    log_activity(db, TimesheetActivityLog, "timesheet_id", invoice.timesheet_id, user_id or 0, action, note)


def confirm_customer_approval(db: Session, invoice: Invoice, user, note: str | None = None) -> Invoice:
    """The Sales Manager / Sales Head confirms the customer accepted the invoice
    as issued. Stamps it, logs it and tells Finance. Idempotence: a second
    confirmation is a 409 (the first one stands)."""
    if not may_confirm(user):
        raise HTTPException(status_code=403,
                            detail="Only the Sales Manager or Sales Head confirms a customer's approval.")
    block = confirm_block(invoice)
    if block:
        raise HTTPException(status_code=409 if is_approved(invoice) else 400, detail=block)
    note = " ".join((note or "").split())[:MAX_NOTE] or None
    invoice.customer_approved_at = datetime.now(timezone.utc)
    invoice.customer_approved_by = getattr(user, "id", None)
    invoice.customer_approval_note = note
    db.flush()
    _log(db, invoice, getattr(user, "id", None), "INVOICE_CUSTOMER_APPROVED",
         f"Customer approved invoice {invoice.invoice_number}" + (f": {note}" if note else ""))
    _notify_finance(db, invoice, user, note)
    return invoice


def withdraw_customer_approval(db: Session, invoice: Invoice, user) -> Invoice:
    """Undo a confirmation made by mistake — only while Finance has not recorded
    the IRN (after that the invoice is registered with GST)."""
    if not may_confirm(user):
        raise HTTPException(status_code=403,
                            detail="Only the Sales Manager or Sales Head changes a customer's approval.")
    if not is_approved(invoice):
        raise HTTPException(status_code=409, detail="The customer approval is not confirmed yet.")
    if irn_recorded(invoice):
        raise HTTPException(status_code=409,
                            detail="Finance has already recorded the IRN — ask Finance before changing it.")
    invoice.customer_approved_at = None
    invoice.customer_approved_by = None
    invoice.customer_approval_note = None
    db.flush()
    _log(db, invoice, getattr(user, "id", None), "INVOICE_CUSTOMER_APPROVAL_WITHDRAWN",
         f"Customer approval of {invoice.invoice_number} withdrawn")
    return invoice


def record_irn(db: Session, invoice: Invoice, user, *, irn: str, ack_number: str,
               ack_date: date | None = None) -> Invoice:
    """Finance records (or corrects) the e-invoice IRN + Ack No. on a
    customer-approved tax invoice."""
    if not may_see_irn(user):
        raise HTTPException(status_code=403, detail="Only Finance records the IRN.")
    if invoice.is_proforma:
        raise HTTPException(status_code=400, detail="A Proforma has no IRN — generate the original invoice first.")
    if not is_approved(invoice):
        raise HTTPException(status_code=409,
                            detail="Waiting for the Sales Manager / Sales Head to confirm the customer's approval.")
    irn, ack = clean_irn(irn), clean_ack(ack_number)
    err = irn_error(irn, ack, ack_date)
    if err:
        raise HTTPException(status_code=400, detail=err)
    previous = invoice.irn_number
    invoice.irn_number = irn
    invoice.ack_number = ack
    invoice.ack_date = ack_date
    invoice.irn_recorded_at = datetime.now(timezone.utc)
    invoice.irn_recorded_by = getattr(user, "id", None)
    db.flush()
    _log(db, invoice, getattr(user, "id", None), "INVOICE_IRN_RECORDED",
         f"{'Corrected' if previous else 'Recorded'} the e-invoice IRN / Ack No. {ack} "
         f"on {invoice.invoice_number}")
    return invoice


def _notify_finance(db: Session, invoice: Invoice, user, note: str | None) -> None:
    from services.notify import notify_role

    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Sales"
    try:
        with db.begin_nested():
            notify_role(
                db, "Finance",
                f"Customer approved invoice {invoice.invoice_number} — add the IRN",
                f"{who} confirmed the customer accepted invoice {invoice.invoice_number} "
                f"(₹{float(invoice.grand_total or 0):,.2f} incl. GST) without changes. "
                "Record the e-invoice IRN and Acknowledgement No. on the invoice."
                + (f" Note: {note}" if note else ""),
                f"/invoices/{invoice.id}", exclude_user_id=getattr(user, "id", None),
                event=EVENT_CUSTOMER_APPROVED, actor=user,
                dedupe_prefix=f"{EVENT_CUSTOMER_APPROVED}:{invoice.id}",
                related_type="invoice", related_id=invoice.id,
                rows=[("Invoice", invoice.invoice_number),
                      ("Amount", f"₹{float(invoice.grand_total or 0):,.2f}"),
                      ("Confirmed by", who)],
            )
    except Exception:  # noqa: BLE001 — a mail problem never undoes the confirmation
        import logging

        logging.getLogger("karnex.crm.invoices").warning("customer-approval notice failed", exc_info=True)


def payload(invoice: Invoice, user, names: dict[int, str] | None = None) -> dict:
    """What the invoice page needs. `einvoice` is present ONLY for Finance /
    Admin / CEO — every other login never receives the key."""
    names = names or {}

    def iso(v):
        return v.isoformat() if v is not None else None

    out = {
        "customer_approval": {
            "approved": is_approved(invoice),
            "approved_at": iso(invoice.customer_approved_at),
            "approved_by": invoice.customer_approved_by,
            "approved_by_name": names.get(invoice.customer_approved_by or 0),
            "note": invoice.customer_approval_note,
            "can_confirm": may_confirm(user) and confirm_block(invoice) is None,
            "can_withdraw": may_confirm(user) and is_approved(invoice) and not irn_recorded(invoice),
            "block": confirm_block(invoice),
        },
    }
    if may_see_irn(user):
        out["einvoice"] = {
            "irn": invoice.irn_number,
            "ack_number": invoice.ack_number,
            "ack_date": iso(invoice.ack_date),
            "recorded_at": iso(invoice.irn_recorded_at),
            "recorded_by": invoice.irn_recorded_by,
            "recorded_by_name": names.get(invoice.irn_recorded_by or 0),
            "can_edit": (not invoice.is_proforma) and is_approved(invoice),
        }
    # Whether the e-invoice exists — a yes/no for every reader (the IRN itself
    # stays Finance / Admin / CEO only); drives the invoice page's journey.
    out["einvoice_ready"] = (not invoice.is_proforma) and irn_recorded(invoice)
    # Payments / TDS open only once the e-invoice exists (8 Oct 2026).
    out["payments_open"] = payment_block(invoice) is None
    out["payment_block"] = payment_block(invoice)
    return out


def user_ids(invoice: Invoice) -> set[int]:
    return {i for i in (invoice.customer_approved_by, invoice.irn_recorded_by) if i}
