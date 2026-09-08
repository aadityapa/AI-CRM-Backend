"""Credit notes: raised against invoices (Full / Partial / Adjustment).

Lifecycle: Draft -> Approved -> Settled (Refund | Adjust_Balance), with
Draft/Approved -> Cancelled. Writes: Finance (Admin/CEO implicit). Reads: any
authenticated user. All totals are server-computed from the lines using the
same Decimal half-up 2dp rounding as invoices (services/tax.py style).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, get_crm_db, get_current_user, page_params, role_required,
)
from models import CreditNote, CreditNoteLine
from schemas.common import envelope
from schemas.finance import CreditNoteCreate, CreditNoteSettleIn
from services import tax
from services.crm_common import next_sequence_number, paginate
from services.finance import (
    get_credit_note_or_404,
    get_invoice_or_404,
    serialize_credit_note,
    serialize_invoice,
)

router = APIRouter(prefix="/api/credit-notes", tags=["CRM: Credit Notes"])

WRITE = role_required("Finance")

CREDIT_TYPES = ("Full", "Partial", "Adjustment")
STATUSES = ("Draft", "Approved", "Settled", "Cancelled")
SETTLED_AGAINST = ("Refund", "Adjust_Balance")

TWO_PLACES = Decimal("0.01")


def _choice_or_400(value: str, allowed: tuple[str, ...], field: str) -> str:
    if value not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid {field} '{value}'. Allowed: {', '.join(allowed)}")
    return value


@router.post("")
def create_credit_note(body: CreditNoteCreate, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(WRITE)):
    """Create a Draft credit note. Totals are server-computed from the lines:
    sub_total = sum(qty x unit_price), tax_amount = sum(line GST), and each
    line_total = qty x unit_price x (1 + gst_percent/100) — every component
    rounded half-up to 2 decimal places (services/tax.py convention)."""
    invoice = get_invoice_or_404(db, body.invoice_id)
    _choice_or_400(body.credit_type, CREDIT_TYPES, "credit_type")

    line_rows: list[CreditNoteLine] = []
    sub_total = Decimal("0")
    tax_amount = Decimal("0")
    for i, line in enumerate(body.lines, start=1):
        base = (line.qty * line.unit_price).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
        gst = tax.gst_amount(base, line.gst_percent)
        sub_total += base
        tax_amount += gst
        line_rows.append(CreditNoteLine(
            s_no=i,
            item=line.item,
            hsn_sac=line.hsn_sac,
            description=line.description,
            qty=line.qty,
            unit_price=line.unit_price,
            gst_percent=line.gst_percent,
            line_total=base + gst,
        ))
    total_amount = sub_total + tax_amount
    if total_amount <= 0:
        raise HTTPException(status_code=400, detail="Credit note total must be positive")
    # Cap at the invoice's grand_total (= balance_amount + paid_amount).
    if total_amount > Decimal(str(invoice.grand_total)):
        raise HTTPException(
            status_code=400,
            detail="Credit note total cannot exceed the invoice grand total")

    note = CreditNote(
        credit_note_number=next_sequence_number(
            db, CreditNote, CreditNote.credit_note_number, "CN"),
        invoice_id=invoice.id,
        credit_type=body.credit_type,
        credit_date=body.credit_date or date.today(),
        reason=body.reason,
        sub_total=sub_total,
        tax_amount=tax_amount,
        total_amount=total_amount,
        status="Draft",
        created_by=user.id,
        lines=line_rows,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return envelope(serialize_credit_note(note, detail=True, db=db), "Credit note created")


@router.get("")
def list_credit_notes(invoice_id: int | None = None, status: str | None = None,
                      pp: PageParams = Depends(page_params),
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(get_current_user)):
    stmt = select(CreditNote)
    if invoice_id is not None:
        stmt = stmt.where(CreditNote.invoice_id == invoice_id)
    if status:
        stmt = stmt.where(CreditNote.status == _choice_or_400(status, STATUSES, "status"))
    if pp.search:
        stmt = stmt.where(CreditNote.credit_note_number.ilike(f"%{pp.search}%"))
    stmt = stmt.order_by(CreditNote.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope([serialize_credit_note(n) for n in items], meta=meta)


@router.get("/{credit_note_id}")
def get_credit_note(credit_note_id: int, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(get_current_user)):
    note = get_credit_note_or_404(db, credit_note_id)
    return envelope(serialize_credit_note(note, detail=True, db=db))


@router.post("/{credit_note_id}/approve")
def approve_credit_note(credit_note_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(WRITE)):
    note = get_credit_note_or_404(db, credit_note_id)
    if note.status != "Draft":
        raise HTTPException(status_code=400,
                            detail=f"Only Draft credit notes can be approved (current: {note.status})")
    note.status = "Approved"
    note.approved_by = user.id
    note.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(note)
    return envelope(serialize_credit_note(note, detail=True, db=db), "Credit note approved")


@router.post("/{credit_note_id}/settle")
def settle_credit_note(credit_note_id: int, body: CreditNoteSettleIn,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(WRITE)):
    """Settle an Approved credit note. settled_against="Adjust_Balance" reduces
    the invoice balance_amount by the credit note total (never below 0); when
    the balance hits 0 the invoice payment_status flips to "Paid".
    "Refund" records the settlement only (money movement is out of scope)."""
    note = get_credit_note_or_404(db, credit_note_id)
    if note.status != "Approved":
        raise HTTPException(status_code=400,
                            detail=f"Only Approved credit notes can be settled (current: {note.status})")
    _choice_or_400(body.settled_against, SETTLED_AGAINST, "settled_against")

    invoice = get_invoice_or_404(db, note.invoice_id)
    if body.settled_against == "Adjust_Balance":
        new_balance = (Decimal(str(invoice.balance_amount))
                       - Decimal(str(note.total_amount))).quantize(TWO_PLACES)
        invoice.balance_amount = max(new_balance, Decimal("0"))
        if invoice.balance_amount <= 0:
            invoice.payment_status = "Paid"

    note.status = "Settled"
    note.settled_against = body.settled_against
    db.commit()
    db.refresh(note)
    return envelope(
        {"credit_note": serialize_credit_note(note, detail=True, db=db),
         "invoice": serialize_invoice(invoice)},
        "Credit note settled",
    )


@router.post("/{credit_note_id}/cancel")
def cancel_credit_note(credit_note_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(WRITE)):
    note = get_credit_note_or_404(db, credit_note_id)
    if note.status not in ("Draft", "Approved"):
        raise HTTPException(status_code=400,
                            detail=f"Only Draft/Approved credit notes can be cancelled (current: {note.status})")
    note.status = "Cancelled"
    db.commit()
    db.refresh(note)
    return envelope(serialize_credit_note(note, detail=True, db=db), "Credit note cancelled")
