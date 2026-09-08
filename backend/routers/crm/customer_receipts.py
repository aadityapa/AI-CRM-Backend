"""Customer received amounts (7 Sep 2026, user request).

Finance records a bank credit from a customer once — received date, amount,
payment mode, reference number, notes — and ticks the employees' invoices it
covers. The amount is allocated across the chosen invoices oldest-first, up to
each invoice's balance; whatever is left stays on the receipt as
`unallocated_amount` (advance / on-account). Each allocation is written as an
`InvoicePayment` with `receipt_id`, so invoice paid / balance / status keep
working exactly as before; deleting a receipt reverses those payments.

Endpoints (all enveloped):
  GET    /api/customer-receipts                  list, filters customer_id / project_id / from / to / search
  GET    /api/customer-receipts/invoice-options  open invoices for the picker (customer_id required)
  POST   /api/customer-receipts                  create + allocate
  DELETE /api/customer-receipts/{id}             reverse allocations, delete (Finance / Admin)
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, gated_read, gated_write_action, get_crm_db, page_params
from models import (
    Customer, CustomerReceipt, Employee, Invoice, InvoicePayment, Project, ProjectEmployee, Timesheet,
)
from schemas.common import envelope
from services import tax
from services.crm_common import paginate

router = APIRouter(prefix="/api/customer-receipts", tags=["customer-receipts"])

RC_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
RC_WRITE = gated_write_action("invoice.manage", "invoices", "Finance")

PAYMENT_MODES = ["NEFT", "RTGS", "IMPS", "UPI", "Cheque", "Cash", "Wire", "Other"]
TWO = Decimal("0.01")


class ReceiptIn(BaseModel):
    customer_id: int
    received_date: date
    amount: Decimal = Field(gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    #: Invoices this money covers — allocated oldest-first up to each balance.
    invoice_ids: list[int] = Field(default_factory=list, max_length=100)


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def _employee_names_by_invoice(db: Session, invoices: list[Invoice]) -> dict[int, str | None]:
    """invoice → timesheet → employee, batched (the invoice is 'for' the
    employee whose timesheet it bills)."""
    ts_ids = [i.timesheet_id for i in invoices if i.timesheet_id]
    if not ts_ids:
        return {}
    rows = db.execute(
        select(Timesheet.id, Employee.first_name, Employee.last_name)
        .join(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.id.in_(ts_ids))
    ).all()
    by_ts = {r[0]: " ".join(p for p in (r[1], r[2]) if p) for r in rows}
    return {i.id: by_ts.get(i.timesheet_id) for i in invoices if i.timesheet_id}


def _invoice_brief(db: Session, invoices: list[Invoice]) -> list[dict]:
    names = _employee_names_by_invoice(db, invoices)
    proj = {p.id: p for p in db.execute(
        select(Project).where(Project.id.in_({i.project_id for i in invoices}))
    ).scalars().all()} if invoices else {}
    return [{
        "id": i.id,
        "invoice_number": i.invoice_number,
        "invoice_date": i.invoice_date.isoformat() if i.invoice_date else None,
        "project_id": i.project_id,
        "project_name": proj.get(i.project_id).name if proj.get(i.project_id) else None,
        "employee_name": names.get(i.id),
        "grand_total": float(i.grand_total or 0),
        "paid_amount": float(i.paid_amount or 0),
        "balance_amount": float(i.balance_amount or 0),
        "payment_status": getattr(i.payment_status, "value", i.payment_status),
    } for i in invoices]


def _serialize(db: Session, r: CustomerReceipt, customer_name: str | None = None) -> dict:
    payments = list(r.payments or [])
    inv_ids = [p.invoice_id for p in payments]
    invoices = db.execute(select(Invoice).where(Invoice.id.in_(inv_ids))).scalars().all() if inv_ids else []
    briefs = {b["id"]: b for b in _invoice_brief(db, invoices)}
    return {
        "id": r.id,
        "customer_id": r.customer_id,
        "customer_name": customer_name,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "amount": float(r.amount or 0),
        "payment_mode": r.payment_mode,
        "reference_number": r.reference_number,
        "notes": r.notes,
        "unallocated_amount": float(r.unallocated_amount or 0),
        "allocated_amount": float(sum((_d(p.amount) for p in payments), Decimal("0"))),
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "allocations": [{
            "payment_id": p.id, "invoice_id": p.invoice_id, "amount": float(p.amount or 0),
            **{k: v for k, v in (briefs.get(p.invoice_id) or {}).items() if k != "id"},
        } for p in payments],
    }


@router.get("/invoice-options")
def invoice_options(customer_id: int, include_paid: bool = False,
                    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """Invoices of this customer's projects for the picker — open ones by
    default, with the employee each one bills, oldest first."""
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = (select(Invoice).join(Project, Project.id == Invoice.project_id)
            .where(Project.customer_id == customer_id))
    if not include_paid:
        stmt = stmt.where(Invoice.balance_amount > 0)
    invoices = db.execute(stmt.order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).scalars().all()
    return envelope(data=_invoice_brief(db, invoices), message="Invoices")


@router.get("")
def list_receipts(customer_id: int | None = None, project_id: int | None = None,
                  received_from: date | None = None, received_to: date | None = None,
                  payment_mode: str | None = None,
                  p: PageParams = Depends(page_params),
                  db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    stmt = select(CustomerReceipt).join(Customer, Customer.id == CustomerReceipt.customer_id)
    if customer_id is not None:
        stmt = stmt.where(CustomerReceipt.customer_id == customer_id)
    if project_id is not None:
        sub = (select(InvoicePayment.receipt_id).join(Invoice, Invoice.id == InvoicePayment.invoice_id)
               .where(Invoice.project_id == project_id, InvoicePayment.receipt_id.isnot(None)))
        stmt = stmt.where(CustomerReceipt.id.in_(sub))
    if received_from is not None:
        stmt = stmt.where(CustomerReceipt.received_date >= received_from)
    if received_to is not None:
        stmt = stmt.where(CustomerReceipt.received_date <= received_to)
    if payment_mode:
        stmt = stmt.where(CustomerReceipt.payment_mode == payment_mode)
    if p.search:
        like = f"%{p.search}%"
        stmt = stmt.where(or_(CustomerReceipt.reference_number.ilike(like),
                              CustomerReceipt.notes.ilike(like), Customer.name.ilike(like)))
    sort_col = {"received_date": CustomerReceipt.received_date, "amount": CustomerReceipt.amount,
                "customer_name": Customer.name}.get(p.sort_by or "", CustomerReceipt.received_date)
    stmt = stmt.order_by(sort_col.asc() if p.sort_dir == "asc" else sort_col.desc(),
                         CustomerReceipt.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    names = {c.id: c.name for c in db.execute(
        select(Customer).where(Customer.id.in_({r.customer_id for r in items}))
    ).scalars().all()} if items else {}
    data = [_serialize(db, r, names.get(r.customer_id)) for r in items]
    total = db.execute(select(func.coalesce(func.sum(CustomerReceipt.amount), 0))
                       .where(CustomerReceipt.customer_id == customer_id) if customer_id
                       else select(func.coalesce(func.sum(CustomerReceipt.amount), 0))).scalar()
    meta = {**(meta or {}), "total_received": float(total or 0)}
    return envelope(data=data, message="Customer receipts", meta=meta)


def allocate(amount: Decimal, invoices: list[Invoice]) -> tuple[list[tuple[Invoice, Decimal]], Decimal]:
    """Oldest-first allocation up to each invoice's balance. Pure — tested."""
    left = _d(amount)
    out: list[tuple[Invoice, Decimal]] = []
    for inv in sorted(invoices, key=lambda i: (i.invoice_date or date.min, i.id)):
        if left <= 0:
            break
        bal = _d(inv.balance_amount)
        if bal <= 0:
            continue
        take = min(bal, left)
        out.append((inv, take))
        left = (left - take).quantize(TWO)
    return out, left


@router.post("")
def create_receipt(body: ReceiptIn, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    customer = db.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if body.payment_mode and body.payment_mode not in PAYMENT_MODES:
        raise HTTPException(status_code=400, detail=f"Payment mode must be one of: {', '.join(PAYMENT_MODES)}")
    invoices: list[Invoice] = []
    if body.invoice_ids:
        invoices = db.execute(
            select(Invoice).join(Project, Project.id == Invoice.project_id)
            .where(Invoice.id.in_(set(body.invoice_ids)), Project.customer_id == body.customer_id)
        ).scalars().all()
        missing = set(body.invoice_ids) - {i.id for i in invoices}
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"Invoice(s) {sorted(missing)} do not belong to {customer.name}")
    plan, left = allocate(body.amount, invoices)
    receipt = CustomerReceipt(
        customer_id=body.customer_id, received_date=body.received_date, amount=_d(body.amount),
        payment_mode=body.payment_mode or None,
        reference_number=(body.reference_number or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        unallocated_amount=left, created_by=user.id,
    )
    db.add(receipt)
    db.flush()
    for inv, take in plan:
        db.add(InvoicePayment(
            invoice_id=inv.id, payment_date=body.received_date, amount=take,
            payment_mode=body.payment_mode or None,
            reference_number=(body.reference_number or "").strip() or None,
            notes=f"From customer receipt #{receipt.id}", receipt_id=receipt.id,
        ))
        tax.apply_invoice_payment(inv, take)
    db.commit()
    db.refresh(receipt)
    msg = f"Received {float(receipt.amount):,.2f} recorded — {len(plan)} invoice(s) settled"
    if left > 0:
        msg += f", {float(left):,.2f} left unallocated"
    return envelope(data=_serialize(db, receipt, customer.name), message=msg)


@router.delete("/{receipt_id}")
def delete_receipt(receipt_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    """Reverse the receipt: its payments come off the invoices, then the row goes."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    for pay in list(receipt.payments or []):
        inv = db.get(Invoice, pay.invoice_id)
        if inv is not None:
            tax.apply_invoice_payment(inv, -_d(pay.amount))
        db.delete(pay)
    db.delete(receipt)
    db.commit()
    return envelope(data={"id": receipt_id}, message="Receipt removed and invoice balances restored")
