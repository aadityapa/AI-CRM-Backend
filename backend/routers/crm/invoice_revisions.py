"""Invoice change requests (11 Sep 2026, user flow).

A generated invoice has usually already reached the customer, so it is never
edited in place. Instead:

  POST /api/invoices/{id}/revisions               Sales / Finance request a change WITH a reason
  GET  /api/invoices/{id}/revisions               the invoice's change history (every request + decision)
  POST /api/invoices/{id}/revisions/{rid}/approve Sales / Sales Head (Admin/CEO always) — applies it
  POST /api/invoices/{id}/revisions/{rid}/reject  same approvers, note required
  POST /api/invoices/{id}/revisions/{rid}/cancel  the requester withdraws a pending request

Rules the customer-facing money depends on:
  * one pending request per invoice at a time;
  * the requester never approves their own request (Admin/CEO may — they are
    the escalation path; their own requests auto-approve but are still logged
    and announced);
  * approval recomputes line amounts, sub-total, GST and PO consumption, and
    refuses anything that would leave the PO over-consumed or the invoice
    below what the customer has already paid;
  * Admin, CEO and Sales Head are notified on request, approval and rejection
    with the exact before → after of every changed field.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write_action, get_crm_db
from models import Invoice, InvoiceLine, InvoicePayment, InvoiceRevision, Project, PurchaseOrder
from schemas.common import envelope
from services import tax
from services.finance import (
    apply_invoice_gst_totals, compute_karnex_gst, ensure_unique_invoice_number, get_invoice_or_404,
    normalize_buyer_state_code_input, resolve_billing_branch, serialize_invoice,
)

router = APIRouter(prefix="/api/invoices", tags=["CRM: Invoice revisions"])

REV_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
#: Who may REQUEST a change — Sales and Finance (the people the customer calls).
REV_REQUEST = gated_write_action("invoice.revision.request", "invoices", "Sales", "Finance", "Sales_Head")
#: Who may APPROVE — Sales / Sales Head; Admin/CEO always. Admin-editable in Action permissions.
REV_APPROVE = gated_write_action("invoice.revision.approve", "invoices", "Sales", "Sales_Head")

MIN_REASON = 10
WATCH_ROLES = ("Admin", "CEO", "Sales_Head")

TWO = Decimal("0.01")


class LineChangeIn(BaseModel):
    id: int
    qty: float | None = Field(default=None, gt=0)
    rate: float | None = Field(default=None, gt=0)
    description: str | None = Field(default=None, max_length=512)


class RevisionRequestIn(BaseModel):
    reason: str = Field(min_length=1)
    invoice_number: str | None = Field(default=None, max_length=64)
    invoice_date: date | None = None
    due_date: date | None = None
    buyer_state_code: str | None = None
    #: Move the invoice to another PO of the same customer (Sales' pick).
    po_id: int | None = None
    lines: list[LineChangeIn] = Field(default_factory=list)


class DecisionIn(BaseModel):
    note: str | None = None


# ------------------------------------------------------------------ helpers

def _iso(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _num(v):
    return float(v) if v is not None else None


def _snapshot(inv: Invoice) -> dict:
    return {
        "invoice_number": inv.invoice_number,
        "invoice_date": _iso(inv.invoice_date),
        "due_date": _iso(inv.due_date),
        "buyer_state_code": inv.buyer_state_code,
        "po_id": inv.po_id,
        "po_number": (inv.po.po_number if inv.po else None),
        "sub_total": _num(inv.sub_total),
        "tax_amount": _num(inv.tax_amount),
        "grand_total": _num(inv.grand_total),
        "lines": [{"id": l.id, "s_no": l.s_no, "description": l.description,
                   "qty": _num(l.qty), "rate": _num(l.rate), "amount": _num(l.amount)}
                  for l in inv.lines],
    }


def serialize_revision(r: InvoiceRevision) -> dict:
    return {
        "id": r.id,
        "invoice_id": r.invoice_id,
        "status": r.status,
        "reason": r.reason,
        "changes": r.changes or {},
        "snapshot_before": r.snapshot_before,
        "snapshot_after": r.snapshot_after,
        "requested_by": r.requested_by,
        "requested_by_name": r.requested_by_name,
        "requested_at": _iso(r.requested_at),
        "decided_by": r.decided_by,
        "decided_by_name": r.decided_by_name,
        "decided_at": _iso(r.decided_at),
        "decision_note": r.decision_note,
    }


def db_po_or_400(po_id: int, inv: Invoice) -> PurchaseOrder:
    from sqlalchemy.orm import object_session
    db = object_session(inv)
    po = db.get(PurchaseOrder, po_id) if db is not None else None
    if po is None:
        raise HTTPException(status_code=400, detail="Purchase order not found")
    project = inv.project or (db.get(Project, inv.project_id) if db is not None else None)
    if project is not None and po.customer_id != project.customer_id:
        raise HTTPException(status_code=400, detail=f"PO {po.po_number} belongs to another customer")
    if str(getattr(po.status, "value", po.status)) == "Cancelled":
        raise HTTPException(status_code=400, detail=f"PO {po.po_number} is cancelled")
    return po


def _diff(inv: Invoice, body: RevisionRequestIn) -> dict:
    """Only the fields that actually differ — an empty diff is a 400."""
    changes: dict = {}
    if body.invoice_number is not None:
        new_no = body.invoice_number.strip()
        if new_no and new_no != inv.invoice_number:
            changes["invoice_number"] = {"from": inv.invoice_number, "to": new_no}
    if body.invoice_date is not None and body.invoice_date != inv.invoice_date:
        changes["invoice_date"] = {"from": _iso(inv.invoice_date), "to": _iso(body.invoice_date)}
    if body.due_date is not None and body.due_date != inv.due_date:
        changes["due_date"] = {"from": _iso(inv.due_date), "to": _iso(body.due_date)}
    if "buyer_state_code" in body.model_fields_set:
        new_sc = normalize_buyer_state_code_input(body.buyer_state_code)
        if new_sc != inv.buyer_state_code:
            changes["buyer_state_code"] = {"from": inv.buyer_state_code, "to": new_sc}
    if body.po_id is not None and body.po_id != inv.po_id:
        new_po = db_po_or_400(body.po_id, inv)
        changes["po_id"] = {"from": inv.po_id, "to": new_po.id,
                            "from_label": inv.po.po_number if inv.po else None, "to_label": new_po.po_number}
    by_id = {l.id: l for l in inv.lines}
    line_changes = []
    for lc in body.lines:
        line = by_id.get(lc.id)
        if line is None:
            raise HTTPException(status_code=400, detail=f"Line #{lc.id} is not on this invoice")
        entry: dict = {"id": line.id, "s_no": line.s_no, "description": line.description}
        touched = False
        if lc.qty is not None and abs(float(line.qty or 0) - lc.qty) > 1e-9:
            entry["qty"] = {"from": _num(line.qty), "to": lc.qty}
            touched = True
        if lc.rate is not None and abs(float(line.rate or 0) - lc.rate) > 1e-9:
            entry["rate"] = {"from": _num(line.rate), "to": lc.rate}
            touched = True
        if lc.description is not None and lc.description.strip() and lc.description.strip() != line.description:
            entry["description_change"] = {"from": line.description, "to": lc.description.strip()}
            touched = True
        if touched:
            line_changes.append(entry)
    if line_changes:
        changes["lines"] = line_changes
    return changes


def _describe(changes: dict) -> str:
    bits = []
    labels = {"invoice_number": "invoice no.", "invoice_date": "invoice date", "due_date": "due date",
              "buyer_state_code": "buyer state code"}
    for k, lab in labels.items():
        if k in changes:
            bits.append(f"{lab} {changes[k]['from'] or '—'} → {changes[k]['to'] or '—'}")
    if "po_id" in changes:
        bits.append(f"PO {changes['po_id'].get('from_label') or '—'} → {changes['po_id'].get('to_label') or '—'}")
    for lc in changes.get("lines", []):
        parts = []
        if "qty" in lc:
            parts.append(f"qty {lc['qty']['from']:g} → {lc['qty']['to']:g}")
        if "rate" in lc:
            parts.append(f"rate ₹{lc['rate']['from']:,.2f} → ₹{lc['rate']['to']:,.2f}")
        if "description_change" in lc:
            parts.append("description reworded")
        bits.append(f"line {lc.get('s_no')}: " + ", ".join(parts))
    return "; ".join(bits) or "no field changes"


def _pending(db: Session, invoice_id: int) -> InvoiceRevision | None:
    return db.execute(select(InvoiceRevision).where(
        InvoiceRevision.invoice_id == invoice_id, InvoiceRevision.status == "Pending")).scalars().first()


def _apply(db: Session, inv: Invoice, changes: dict) -> None:
    """Write an APPROVED change set onto the invoice — the only place the
    invoice row changes after generation."""
    if "invoice_number" in changes:
        new_no = changes["invoice_number"]["to"]
        if new_no != inv.invoice_number:
            ensure_unique_invoice_number(db, new_no)
            inv.invoice_number = new_no
    if "invoice_date" in changes:
        inv.invoice_date = date.fromisoformat(changes["invoice_date"]["to"])
    if "due_date" in changes:
        v = changes["due_date"]["to"]
        inv.due_date = date.fromisoformat(v) if v else None
    if "buyer_state_code" in changes:
        inv.buyer_state_code = changes["buyer_state_code"]["to"]

    by_id = {l.id: l for l in inv.lines}
    for lc in changes.get("lines", []):
        line = by_id.get(lc["id"])
        if line is None:
            raise HTTPException(status_code=409, detail=f"Line {lc.get('s_no')} no longer exists on the invoice")
        if "qty" in lc:
            line.qty = Decimal(str(lc["qty"]["to"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        if "rate" in lc:
            line.rate = Decimal(str(lc["rate"]["to"])).quantize(TWO, rounding=ROUND_HALF_UP)
        if "description_change" in lc:
            line.description = lc["description_change"]["to"]
        line.amount = (Decimal(str(line.qty)) * Decimal(str(line.rate))).quantize(TWO, rounding=ROUND_HALF_UP)

    old_grand = Decimal(str(inv.grand_total or 0))
    if changes.get("lines"):
        inv.sub_total = sum((Decimal(str(l.amount or 0)) for l in inv.lines), Decimal("0")).quantize(TWO)
    # GST always recomputed (a state-code change alone flips CGST/SGST ↔ IGST).
    po = inv.po or (db.get(PurchaseOrder, inv.po_id) if inv.po_id else None)
    project = inv.project or db.get(Project, inv.project_id)
    gst_po = db.get(PurchaseOrder, changes["po_id"]["to"]) if "po_id" in changes else po
    branch = resolve_billing_branch(db, po=gst_po or po, project=project)
    gst = compute_karnex_gst(
        branch=branch,
        state_code_override=inv.buyer_state_code,
        items=[{"billing_hours": float(l.qty or 0), "rate_per_hour": float(l.rate or 0)} for l in inv.lines],
        subtotal=float(inv.sub_total or 0),
    )
    apply_invoice_gst_totals(inv, gst)
    new_grand = Decimal(str(inv.grand_total or 0))

    paid = Decimal(str(inv.paid_amount or 0))
    if paid > new_grand:
        raise HTTPException(status_code=400, detail=(
            f"Cannot reduce the invoice to ₹{new_grand:,.2f}: the customer has already paid "
            f"₹{paid:,.2f}. Record a credit note instead."))
    if "po_id" in changes:
        # Move to another PO: give the old one its money back, draw the full
        # (new) grand total from the new one — refused if it cannot cover it.
        new_po = db.get(PurchaseOrder, changes["po_id"]["to"])
        if new_po is None:
            raise HTTPException(status_code=409, detail="The requested PO no longer exists")
        if Decimal(str(new_po.balance_value or 0)) < new_grand:
            raise HTTPException(status_code=400, detail=(
                f"PO {new_po.po_number} balance ₹{Decimal(str(new_po.balance_value or 0)):,.2f} cannot cover "
                f"this invoice's ₹{new_grand:,.2f}"))
        if po is not None:
            tax.apply_po_consumption(po, -old_grand)
            db.add(po)
        tax.apply_po_consumption(new_po, new_grand)
        db.add(new_po)
        inv.po_id = new_po.id
        inv.po = new_po
    else:
        delta = new_grand - old_grand
        if po is not None and delta != 0:
            if delta > 0 and Decimal(str(po.balance_value or 0)) < delta:
                raise HTTPException(status_code=400, detail=(
                    f"PO {po.po_number} balance ₹{Decimal(str(po.balance_value or 0)):,.2f} cannot cover the "
                    f"₹{delta:,.2f} increase"))
            tax.apply_po_consumption(po, delta)
            db.add(po)
    db.add(inv)


def _notify(db: Session, inv: Invoice, rev: InvoiceRevision, user: CurrentUser, *,
            event: str, title: str, message: str, extra_roles=(), also_user_id: int | None = None) -> None:
    try:
        from services.notify import notify_roles, notify_user
        link = f"/admin?view=crm&p=invoices/{inv.id}"
        roles = list(dict.fromkeys(list(WATCH_ROLES) + list(extra_roles)))
        notify_roles(db, roles, title, message, link, exclude_user_id=user.id, actor=user, event=event,
                     dedupe_prefix=f"{event}:{rev.id}", related_type="invoice", related_id=inv.id)
        if also_user_id and also_user_id != user.id:
            notify_user(db, also_user_id, title, message, link, actor=user, event=event,
                        related_type="invoice", related_id=inv.id)
    except Exception:
        pass


def _money(v) -> str:
    return f"₹{Decimal(str(v or 0)):,.2f}"


# ---------------------------------------------------------------- endpoints

@router.get("/{invoice_id}/revisions")
def list_revisions(invoice_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(REV_READ)):
    inv = get_invoice_or_404(db, invoice_id)
    rows = db.execute(select(InvoiceRevision).where(InvoiceRevision.invoice_id == inv.id)
                      .order_by(InvoiceRevision.requested_at.desc(), InvoiceRevision.id.desc())).scalars().all()
    pending = next((r for r in rows if r.status == "Pending"), None)
    can_approve = user.is_admin or user.has_any("Sales", "Sales_Head")
    return envelope(data=[serialize_revision(r) for r in rows], meta={
        "pending_id": pending.id if pending else None,
        "can_request": True,
        "can_approve": bool(can_approve and (pending is None or pending.requested_by != user.id or user.is_admin)),
        "is_requester": bool(pending and pending.requested_by == user.id),
    })


@router.get("/{invoice_id}/po-options")
def invoice_po_options(invoice_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(REV_READ)):
    """POs of this invoice's customer a change request may move it to —
    tagged with the employee each was raised for, current one first."""
    inv = get_invoice_or_404(db, invoice_id)
    project = inv.project or db.get(Project, inv.project_id)
    if project is None:
        return envelope(data=[])
    from models import Employee
    pos = db.execute(select(PurchaseOrder).where(PurchaseOrder.customer_id == project.customer_id)
                     .order_by(PurchaseOrder.start_date.asc().nulls_last(), PurchaseOrder.id)).scalars().all()
    emp_ids = {getattr(p, "employee_id", None) for p in pos if getattr(p, "employee_id", None)}
    names = {}
    if emp_ids:
        for e in db.execute(select(Employee).where(Employee.id.in_(emp_ids))).scalars():
            names[e.id] = " ".join(x for x in (getattr(e, "first_name", None), getattr(e, "last_name", None)) if x) \
                or getattr(e, "full_name", None) or f"#{e.id}"
    ts_emp = None
    ts = inv.timesheet
    if ts is not None:
        ts_emp = ts.employee_id
    rows = []
    for p in pos:
        status = str(getattr(p.status, "value", p.status))
        if status == "Cancelled" and p.id != inv.po_id:
            continue
        rows.append({
            "id": p.id, "po_number": p.po_number, "status": status,
            "employee_id": getattr(p, "employee_id", None),
            "employee_name": names.get(getattr(p, "employee_id", None)),
            "for_this_employee": bool(ts_emp and getattr(p, "employee_id", None) == ts_emp),
            "total_value": _num(p.total_value), "balance_value": _num(p.balance_value),
            "start_date": _iso(p.start_date), "end_date": _iso(p.end_date),
            "is_current": p.id == inv.po_id,
        })
    rows.sort(key=lambda r: (not r["is_current"], not r["for_this_employee"], r["po_number"] or ""))
    return envelope(data=rows)


@router.post("/{invoice_id}/revisions")
def request_revision(invoice_id: int, body: RevisionRequestIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(REV_REQUEST)):
    inv = get_invoice_or_404(db, invoice_id)
    reason = (body.reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Please give a reason of at least {MIN_REASON} characters — it is shown to the approver and kept in the invoice history")
    if _pending(db, inv.id) is not None:
        raise HTTPException(status_code=409, detail="A change request is already awaiting approval on this invoice")
    changes = _diff(inv, body)
    if not changes:
        raise HTTPException(status_code=400, detail="Nothing changed — edit at least one field")
    if "invoice_number" in changes:
        ensure_unique_invoice_number(db, changes["invoice_number"]["to"])

    rev = InvoiceRevision(
        invoice_id=inv.id, status="Pending", reason=reason, changes=changes,
        snapshot_before=_snapshot(inv), requested_by=user.id,
        requested_by_name=user.full_name or user.username,
    )
    db.add(rev)
    db.flush()
    summary = _describe(changes)
    who = user.full_name or user.username

    if user.is_admin:
        # Admin/CEO are the escalation path — their own edits apply at once,
        # but are recorded and announced exactly like everyone else's.
        _apply(db, inv, changes)
        rev.status = "Approved"
        rev.decided_by = user.id
        rev.decided_by_name = who
        rev.decided_at = datetime.now(timezone.utc)
        rev.decision_note = "Applied directly by Admin/CEO"
        rev.snapshot_after = _snapshot(inv)
        db.commit()
        db.refresh(inv)
        _notify(db, inv, rev, user, event="invoice.revision_approved",
                title=f"Invoice {inv.invoice_number} changed by {who}",
                message=f"{who} (Admin/CEO) changed invoice {inv.invoice_number}: {summary}. Reason: {reason}. "
                        f"Grand total now {_money(inv.grand_total)}.",
                extra_roles=("Finance",))
        db.commit()
        return envelope(data={"revision": serialize_revision(rev),
                              "invoice": serialize_invoice(inv, detail=True, db=db)},
                        message="Change applied and recorded")

    db.commit()
    _notify(db, inv, rev, user, event="invoice.revision_requested",
            title=f"Invoice change request: {inv.invoice_number}",
            message=f"{who} asks to change invoice {inv.invoice_number}: {summary}. Reason: {reason}. "
                    f"Approve or reject it on the invoice page.",
            extra_roles=("Sales",))
    db.commit()
    return envelope(data={"revision": serialize_revision(rev)},
                    message="Change request sent for approval — Sales / Sales Head, Admin and CEO have been notified")


def _get_pending_or_404(db: Session, inv: Invoice, rid: int) -> InvoiceRevision:
    rev = db.get(InvoiceRevision, rid)
    if rev is None or rev.invoice_id != inv.id:
        raise HTTPException(status_code=404, detail="Change request not found")
    if rev.status != "Pending":
        raise HTTPException(status_code=409, detail=f"This change request is already {rev.status.lower()}")
    return rev


@router.post("/{invoice_id}/revisions/{rid}/approve")
def approve_revision(invoice_id: int, rid: int, body: DecisionIn | None = None,
                     db: Session = Depends(get_crm_db), user: CurrentUser = Depends(REV_APPROVE)):
    inv = get_invoice_or_404(db, invoice_id)
    rev = _get_pending_or_404(db, inv, rid)
    if rev.requested_by == user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="You cannot approve your own change request — another Sales approver, Sales Head, Admin or CEO must")
    _apply(db, inv, rev.changes or {})
    who = user.full_name or user.username
    rev.status = "Approved"
    rev.decided_by = user.id
    rev.decided_by_name = who
    rev.decided_at = datetime.now(timezone.utc)
    rev.decision_note = ((body.note if body else None) or "").strip() or None
    rev.snapshot_after = _snapshot(inv)
    db.commit()
    db.refresh(inv)
    summary = _describe(rev.changes or {})
    _notify(db, inv, rev, user, event="invoice.revision_approved",
            title=f"Invoice {inv.invoice_number} changed (approved by {who})",
            message=f"{who} approved {rev.requested_by_name or 'the'} change request on invoice "
                    f"{inv.invoice_number}: {summary}. Reason given: {rev.reason}. "
                    f"Grand total now {_money(inv.grand_total)}. Re-send the corrected invoice to the customer.",
            extra_roles=("Finance",), also_user_id=rev.requested_by)
    db.commit()
    return envelope(data={"revision": serialize_revision(rev),
                          "invoice": serialize_invoice(inv, detail=True, db=db)},
                    message="Change approved and applied to the invoice")


@router.post("/{invoice_id}/revisions/{rid}/reject")
def reject_revision(invoice_id: int, rid: int, body: DecisionIn,
                    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(REV_APPROVE)):
    inv = get_invoice_or_404(db, invoice_id)
    rev = _get_pending_or_404(db, inv, rid)
    note = (body.note or "").strip()
    if len(note) < MIN_REASON:
        raise HTTPException(status_code=400, detail=f"Please say why (at least {MIN_REASON} characters) — the requester sees it")
    who = user.full_name or user.username
    rev.status = "Rejected"
    rev.decided_by = user.id
    rev.decided_by_name = who
    rev.decided_at = datetime.now(timezone.utc)
    rev.decision_note = note
    db.commit()
    _notify(db, inv, rev, user, event="invoice.revision_rejected",
            title=f"Invoice change rejected: {inv.invoice_number}",
            message=f"{who} rejected {rev.requested_by_name or 'the'} change request on invoice "
                    f"{inv.invoice_number} ({_describe(rev.changes or {})}). Note: {note}",
            also_user_id=rev.requested_by)
    db.commit()
    return envelope(data={"revision": serialize_revision(rev)}, message="Change request rejected")


@router.post("/{invoice_id}/revisions/{rid}/cancel")
def cancel_revision(invoice_id: int, rid: int, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(REV_REQUEST)):
    inv = get_invoice_or_404(db, invoice_id)
    rev = _get_pending_or_404(db, inv, rid)
    if rev.requested_by != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the requester (or Admin/CEO) can withdraw this request")
    rev.status = "Rejected"
    rev.decided_by = user.id
    rev.decided_by_name = user.full_name or user.username
    rev.decided_at = datetime.now(timezone.utc)
    rev.decision_note = "Withdrawn by the requester"
    db.commit()
    return envelope(data={"revision": serialize_revision(rev)}, message="Change request withdrawn")
