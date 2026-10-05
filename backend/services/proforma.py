"""Proforma → Tax invoice lifecycle (23 Sep 2026).

The flow the business asked for:

    Sales fills the timesheet and submits it
      → GM (custom role) verifies and approves it, then raises a PROFORMA
        (`POST /api/timesheets/{id}/generate-invoice`, confirming the
        customer's column format first)
      → Finance reviews the Proforma on the invoice page: corrects header
        fields, then either CONVERTS it to the original tax invoice
        (`convert_to_tax_invoice`) or RETURNS it to the GM with a reason
        (`return_to_gm`)
      → a returned Proforma is fixed by the GM and raised again through the
        same generate route (the returned document is replaced, keeping its
        PI number)
      → conversion notifies the Sales Manager, who carries on with the
        customer.

Money only moves at conversion: a Proforma never draws on a PO, takes no
payments and has no TDS (`services.finance.require_tax_invoice`).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CustomerBillingPolicy, Invoice, Project, PurchaseOrder, Timesheet
from models.finance import InvoiceKind
from services.crm_common import next_sequence_number
from services.finance import (
    apply_round_off, consume_po_for_invoice, ensure_po_covers, ensure_unique_invoice_number,
    po_draw_amount,
)
from services.invoice_format import format_summary, normalize_invoice_format
from services.notify import notify_role, notify_roles

PROFORMA_PREFIX = "PI"
TAX_PREFIX = "INV"

#: Who is told what. Each is an admin-editable event (routers/crm/email_flows.py);
#: the role here is only the default route.
EVENT_PROFORMA_READY = "invoice.proforma_ready"        # → Finance
EVENT_PROFORMA_RETURNED = "invoice.proforma_returned"  # → GM
EVENT_INVOICE_GENERATED = "invoice.generated"          # → Sales Manager + Sales Head

ROLE_GM = "GM"
ROLE_SALES_MANAGER = "Sales Manager"
ROLE_FINANCE = "Finance"

MIN_RETURN_REASON = 10


# ------------------------------------------------------------------ helpers

def po_credit_days(po: PurchaseOrder | None, default: int = 30) -> int:
    """Credit days from the PO's payment terms ("Net 30 Days" → 30)."""
    if po is not None and po.payment_terms:
        m = re.search(r"(\d+)", str(po.payment_terms))
        if m:
            return max(0, min(int(m.group(1)), 365))
    return default


def invoice_credit_days(db: Session, project_id: int | None,
                        po: PurchaseOrder | None, default: int = 30) -> tuple[int, str]:
    """(credit days, where they came from) for a new / converted invoice.

    5 Oct 2026, user ask: the due date follows the CUSTOMER BRANCH. The
    project's delivery branch (else the PO's billing branch) wins when it has
    `invoice_due_days`; then the PO's payment terms; then 30. Source is
    "branch" | "po" | "default" — the invoice editor prints it.
    """
    from models import CustomerBranch

    project = db.get(Project, project_id) if project_id else None
    branch_id = getattr(project, "branch_id", None) or getattr(po, "billing_branch_id", None)
    branch = db.get(CustomerBranch, branch_id) if branch_id else None
    if branch is not None and branch.invoice_due_days is not None:
        return max(0, min(int(branch.invoice_due_days), 365)), "branch"
    if po is not None and po.payment_terms and re.search(r"\d", str(po.payment_terms)):
        return po_credit_days(po, default), "po"
    return default, "default"


def _billing_policy_for_project(db: Session, project_id: int) -> CustomerBillingPolicy | None:
    project = db.get(Project, project_id)
    if project is None or project.customer_id is None:
        return None
    return db.execute(
        select(CustomerBillingPolicy).where(CustomerBillingPolicy.customer_id == project.customer_id)
    ).scalars().first()


def resolve_invoice_format(db: Session, project_id: int, posted: dict | None) -> dict[str, bool]:
    """The format frozen on a new Proforma.

    A posted choice wins AND is remembered on the customer's billing policy
    (the GM confirmed it, so next month's dialog is pre-filled). With nothing
    posted, the customer's saved format applies; with neither, every column.
    """
    policy = _billing_policy_for_project(db, project_id)
    if posted is not None:
        fmt = normalize_invoice_format(posted)
        if policy is not None and normalize_invoice_format(policy.invoice_format) != fmt:
            policy.invoice_format = dict(fmt)   # full reassignment — JSONB is not tracked in place
        return fmt
    return normalize_invoice_format(policy.invoice_format if policy is not None else None)


def customer_invoice_format(db: Session, project_id: int) -> dict[str, bool]:
    """What the GM's dialog is pre-filled with (the customer's saved choice)."""
    policy = _billing_policy_for_project(db, project_id)
    return normalize_invoice_format(policy.invoice_format if policy is not None else None)


def replaceable_proforma(db: Session, ts: Timesheet) -> Invoice | None:
    """A RETURNED Proforma on this sheet may be raised again — the old document
    is replaced (same PI number) rather than sitting beside the new one."""
    from services.timesheets import linked_invoice_for
    inv = linked_invoice_for(db, ts)
    if inv is not None and inv.is_proforma and inv.returned_at is not None:
        return inv
    return None


def _timesheet_line(ts: Timesheet) -> str:
    return f"timesheet {ts.year}-{ts.month:02d} (project #{ts.project_id}, employee #{ts.employee_id})"


# ------------------------------------------------------------ notifications

def notify_proforma_ready(db: Session, invoice: Invoice, ts: Timesheet, actor) -> None:
    notify_role(
        db, ROLE_FINANCE,
        f"Proforma invoice {invoice.invoice_number} ready for review",
        f"The GM raised proforma {invoice.invoice_number} for {_timesheet_line(ts)}: "
        f"₹{float(invoice.grand_total or 0):,.2f} incl. GST. Review it and generate the original "
        f"invoice, or return it with a note.",
        f"/invoices/{invoice.id}", exclude_user_id=getattr(actor, "id", None),
        event=EVENT_PROFORMA_READY, actor=actor,
        dedupe_prefix=f"{EVENT_PROFORMA_READY}:{invoice.id}:{invoice.returned_at or ''}",
        related_type="invoice", related_id=invoice.id,
        rows=[("Proforma", invoice.invoice_number), ("Amount", f"₹{float(invoice.grand_total or 0):,.2f}"),
              ("Format", format_summary(invoice.invoice_format))],
    )


def _notify_returned(db: Session, invoice: Invoice, actor, reason: str) -> None:
    notify_role(
        db, ROLE_GM,
        f"Proforma {invoice.invoice_number} returned by Finance",
        f"Finance sent proforma {invoice.invoice_number} back: {reason}",
        f"/invoices/{invoice.id}", exclude_user_id=getattr(actor, "id", None),
        event=EVENT_PROFORMA_RETURNED, actor=actor,
        dedupe_prefix=f"{EVENT_PROFORMA_RETURNED}:{invoice.id}:{invoice.returned_at.isoformat() if invoice.returned_at else ''}",
        related_type="invoice", related_id=invoice.id,
        rows=[("Proforma", invoice.invoice_number), ("Reason", reason)],
    )


def _notify_generated(db: Session, invoice: Invoice, actor) -> None:
    # 5 Oct 2026: the Sales Manager OR the Sales Head sends it to the customer
    # and confirms the customer's approval back to Finance (IRN next).
    notify_roles(
        db, [ROLE_SALES_MANAGER, "Sales_Head"],
        f"Invoice {invoice.invoice_number} generated — send it to the customer",
        f"Finance generated the original invoice {invoice.invoice_number} "
        f"(from proforma {invoice.proforma_number}) for ₹{float(invoice.grand_total or 0):,.2f} incl. GST. "
        f"Due {invoice.due_date.isoformat() if invoice.due_date else '—'}. Send it to the customer; once "
        "they accept it unchanged, press \"Confirm customer approval\" on the invoice so Finance adds the IRN.",
        f"/invoices/{invoice.id}", exclude_user_id=getattr(actor, "id", None),
        event=EVENT_INVOICE_GENERATED, actor=actor,
        dedupe_prefix=f"{EVENT_INVOICE_GENERATED}:{invoice.id}",
        related_type="invoice", related_id=invoice.id,
        rows=[("Invoice", invoice.invoice_number), ("Proforma", invoice.proforma_number or "—"),
              ("Amount", f"₹{float(invoice.grand_total or 0):,.2f}")],
    )


# --------------------------------------------------------------- transitions

def _require_proforma(invoice: Invoice, action: str) -> None:
    if not invoice.is_proforma:
        raise HTTPException(status_code=400,
                            detail=f"{invoice.invoice_number} is already a tax invoice — cannot {action}")


def return_to_gm(db: Session, invoice: Invoice, actor, reason: str) -> Invoice:
    """Finance sends the Proforma back. The reason is mandatory (it is what the
    GM reads) and the document stays on the timesheet until the GM reissues it."""
    _require_proforma(invoice, "return it")
    reason = " ".join((reason or "").split())
    if len(reason) < MIN_RETURN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why it is returned (at least {MIN_RETURN_REASON} characters)")
    invoice.returned_reason = reason
    invoice.returned_at = datetime.now(timezone.utc)
    invoice.returned_by = getattr(actor, "id", None)
    db.flush()
    _notify_returned(db, invoice, actor, reason)
    return invoice


def convert_to_tax_invoice(db: Session, invoice: Invoice, actor, *,
                           invoice_number: str | None = None,
                           invoice_date: date | None = None,
                           round_off: bool | None = None) -> Invoice:
    """Finance turns the reviewed Proforma into the original tax invoice.

    In place, not a copy: the id, lines, GST and PO link stay, so every link
    already sent (bell, email, share QR) keeps opening the same document. The
    PI number moves to `proforma_number`; the tax number is what Finance typed
    or the next INV-YYYY-NNN. The PO is drawn down NOW, re-checking balance,
    because other invoices may have consumed it since the Proforma was raised.
    """
    _require_proforma(invoice, "convert it")
    if invoice.returned_at is not None:
        raise HTTPException(status_code=400,
                            detail=f"{invoice.invoice_number} was returned to the GM — wait for it to be reissued")
    number = (invoice_number or "").strip() or next_sequence_number(db, Invoice, Invoice.invoice_number, TAX_PREFIX)
    if number != invoice.invoice_number:
        ensure_unique_invoice_number(db, number)
    po = db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None
    # Every check BEFORE the first write: a refused conversion must leave the
    # Proforma exactly as it was, even inside a session nobody rolls back.
    ensure_po_covers(po, po_draw_amount(invoice.sub_total))   # base value, not incl. GST

    invoice.proforma_number = invoice.proforma_number or invoice.invoice_number
    invoice.invoice_number = number
    invoice.kind = InvoiceKind.TAX.value
    if invoice_date is not None:
        invoice.invoice_date = invoice_date
    invoice.due_date = (invoice.invoice_date or date.today()) + timedelta(
        days=invoice_credit_days(db, invoice.project_id, po)[0])
    if round_off is not None:
        apply_round_off(invoice, round_off)
    db.flush()
    consume_po_for_invoice(db, invoice, po, getattr(actor, "id", None))
    _notify_generated(db, invoice, actor)
    return invoice
