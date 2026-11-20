"""Received TDS (20 Nov 2026, Finance ask) — the TDS twin of Customer Received Amount.

The customer withholds TDS on our invoices and deposits it with the tax
department; the credit shows on Form 16A / 26AS. Finance records that TDS ONCE
here — date, amount, certificate no., quarter, proof — and maps it onto the
invoices it covers, the way a bank credit is mapped in Received Amount.

Mapping an invoice:
* the invoice already has a TDS deduction (booked with a received amount or on
  the invoice) → the mapped amount marks that TDS received (a `TdsPayment` with
  `tds_receipt_id`), up to what is still pending from the client;
* it has none yet → the deduction is booked by the mapping itself (it settles
  the invoice beside the cash, `services/tds.book`, `tds_records.tds_receipt_id`)
  and marked received in the same step. Unmapping removes both again.
What is not mapped stays on the entry as `unallocated_amount`.

Linked to Received Amount both ways: an entry may name the received amount it
goes with (`receipt_id`), and every entry lists the received amounts that paid
the same invoices (`linked_receipts`); a received amount lists its TDS entries
(`customer_receipts._serialize` → `tds_receipts`).

Endpoints (enveloped; read = the Received Amount gate, write = Finance):
  GET    /api/tds-receipts                          list (customer_id · branch_id · from / to · search)
  GET    /api/tds-receipts/invoice-options          invoices of a customer whose TDS can be mapped
  GET    /api/tds-receipts/receipt-options          the customer's received amounts, to link one
  GET    /api/tds-receipts/{id}
  POST   /api/tds-receipts                          record (+ map at once with `items`)
  PUT    /api/tds-receipts/{id}                     edit (amount never below what is mapped)
  POST   /api/tds-receipts/{id}/allocate            map onto invoices (amount per invoice editable)
  DELETE /api/tds-receipts/{id}/allocations/{inv}   unmap one invoice
  DELETE /api/tds-receipts/{id}                     unmap everything and remove
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, get_crm_db, page_params
from models import Customer, CustomerBranch, CustomerReceipt, Invoice, InvoicePayment
from models.finance import InvoiceKind, TdsPayment, TdsReceipt, TdsRecord
from routers.crm.customer_receipts import (
    RC_READ, RC_WRITE, _invoice_brief, clean_proof_url, customer_invoice_clause,
)
from schemas.common import envelope
from services import tax
from services import tds as tds_svc
from services.crm_common import paginate

router = APIRouter(prefix="/api/tds-receipts", tags=["tds-receipts"])

TWO = Decimal("0.01")


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


class TdsMapItem(BaseModel):
    invoice_id: int
    #: TDS to mark received on this invoice; None = what is pending (or the
    #: default rate when the invoice has no TDS yet), from what is left.
    amount: Decimal | None = Field(default=None, gt=0)


class TdsReceiptIn(BaseModel):
    customer_id: int
    branch_id: int | None = None
    received_date: date
    amount: Decimal = Field(gt=0)
    certificate_number: str | None = Field(default=None, max_length=64)
    quarter: str | None = Field(default=None, max_length=16)
    section: str | None = Field(default=None, max_length=16)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    receipt_id: int | None = None
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)
    items: list[TdsMapItem] = Field(default_factory=list, max_length=200)


class TdsReceiptUpdate(BaseModel):
    received_date: date | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    certificate_number: str | None = Field(default=None, max_length=64)
    quarter: str | None = Field(default=None, max_length=16)
    section: str | None = Field(default=None, max_length=16)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    #: The received amount it goes with; 0 clears it.
    receipt_id: int | None = None
    branch_id: int | None = None
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)


class TdsAllocateIn(BaseModel):
    items: list[TdsMapItem] = Field(default_factory=list, max_length=200)
    branch_id: int | None = None


# ---------------------------------------------------------------------------
# PURE: how much TDS each invoice takes
# ---------------------------------------------------------------------------

def open_tds(invoice) -> Decimal:
    """What can still be marked received on this invoice: the TDS pending from
    the client, or — no TDS booked yet — the default rate on the value before
    GST, capped at the open balance."""
    rec = getattr(invoice, "tds_record", None)
    if rec is not None:
        return max(_d(rec.tds_balance), Decimal("0"))
    bal = _d(invoice.balance_amount)
    if bal <= 0:
        return Decimal("0")
    _, amt = tax.tds_on(invoice, tax.default_tds_rate(), "taxable")
    return min(_d(amt), bal)


def cap_for(invoice) -> Decimal:
    """The most that may be mapped onto this invoice: the pending TDS when one
    is booked; otherwise anything up to the open balance (the customer may have
    deducted an odd figure — Finance types it)."""
    rec = getattr(invoice, "tds_record", None)
    if rec is not None:
        return max(_d(rec.tds_balance), Decimal("0"))
    return max(_d(invoice.balance_amount), Decimal("0"))


def plan_tds(available, items: list[tuple[object, Decimal | None]]) -> tuple[list[tuple[object, Decimal]], Decimal]:
    """(invoice, amount) to mark received, and what is left (PURE, tested).
    Typed amounts first (each ≤ `cap_for`), then the rest oldest first, each
    taking `open_tds`. ValueError with the reason."""
    left = _d(available)
    out: dict[int, Decimal] = {}
    for inv, amt in items:
        if amt is None:
            continue
        amt = _d(amt)
        num = getattr(inv, "invoice_number", None) or f"#{inv.id}"
        cap = cap_for(inv)
        if amt > cap:
            what = "the TDS pending on it" if getattr(inv, "tds_record", None) is not None else "its open balance"
            raise ValueError(f"{float(amt):,.2f} on {num} is more than {what} ({float(cap):,.2f})")
        if amt > left:
            raise ValueError(f"Only {float(left):,.2f} of this TDS is left to map")
        out[inv.id] = amt
        left = (left - amt).quantize(TWO)
    auto = sorted([inv for inv, a in items if a is None],
                  key=lambda i: (getattr(i, "invoice_date", None) or date.min, i.id))
    for inv in auto:
        if left <= 0:
            break
        take = min(open_tds(inv), left)
        if take > 0:
            out[inv.id] = take
            left = (left - take).quantize(TWO)
    by_id = {inv.id: inv for inv, _ in items}
    ordered = sorted(out.items(), key=lambda kv: (getattr(by_id[kv[0]], "invoice_date", None) or date.min, kv[0]))
    return [(by_id[i], a) for i, a in ordered if a > 0], left


# ---------------------------------------------------------------------------
# Serialize
# ---------------------------------------------------------------------------

def _payments_of(db: Session, tr_id: int) -> list[TdsPayment]:
    return db.execute(select(TdsPayment).where(TdsPayment.tds_receipt_id == tr_id)).scalars().all()


def _serialize(db: Session, tr: TdsReceipt, customer_name: str | None = None,
               branch_name: str | None = None) -> dict:
    pays = _payments_of(db, tr.id)
    rec_ids = {p.tds_record_id for p in pays}
    recs = {r.id: r for r in db.execute(select(TdsRecord).where(TdsRecord.id.in_(rec_ids))).scalars().all()} if rec_ids else {}
    by_inv: dict[int, Decimal] = {}
    for p in pays:
        r = recs.get(p.tds_record_id)
        if r is not None:
            by_inv[r.invoice_id] = by_inv.get(r.invoice_id, Decimal("0")) + _d(p.amount)
    inv_ids = list(by_inv)
    invoices = db.execute(select(Invoice).where(Invoice.id.in_(inv_ids))).scalars().all() if inv_ids else []
    briefs = {b["id"]: b for b in _invoice_brief(db, invoices)}
    booked_here = {r.invoice_id for r in recs.values() if getattr(r, "tds_receipt_id", None) == tr.id}
    # Received amounts that go with this TDS: the one named + those that paid the same invoices.
    linked: dict[int, bool] = {}
    if tr.receipt_id:
        linked[tr.receipt_id] = True
    if inv_ids:
        for rid in db.execute(select(InvoicePayment.receipt_id).where(
                InvoicePayment.invoice_id.in_(inv_ids), InvoicePayment.receipt_id.isnot(None)).distinct()).scalars():
            linked.setdefault(rid, False)
        for rid in db.execute(select(TdsRecord.receipt_id).where(
                TdsRecord.invoice_id.in_(inv_ids), TdsRecord.receipt_id.isnot(None)).distinct()).scalars():
            linked.setdefault(rid, False)
    receipts = {r.id: r for r in db.execute(select(CustomerReceipt).where(
        CustomerReceipt.id.in_(list(linked)))).scalars().all()} if linked else {}
    mapped = sum(by_inv.values(), Decimal("0"))
    return {
        "id": tr.id,
        "customer_id": tr.customer_id,
        "customer_name": customer_name,
        "branch_id": tr.branch_id,
        "branch_name": branch_name,
        "received_date": tr.received_date.isoformat() if tr.received_date else None,
        "amount": float(tr.amount or 0),
        "certificate_number": tr.certificate_number,
        "quarter": tr.quarter,
        "section": tr.section,
        "reference_number": tr.reference_number,
        "notes": tr.notes,
        "unallocated_amount": float(tr.unallocated_amount or 0),
        "allocated_amount": float(mapped),
        "receipt_id": tr.receipt_id,
        "proof_url": tr.proof_url,
        "proof_name": tr.proof_name,
        "created_by": tr.created_by,
        "created_at": tr.created_at.isoformat() if tr.created_at else None,
        "allocations": [{
            **{k: v for k, v in (briefs.get(iid) or {}).items() if k != "id"},
            "invoice_id": iid, "amount": float(by_inv[iid]), "booked_here": iid in booked_here,
        } for iid in sorted(by_inv, key=lambda i: ((briefs.get(i) or {}).get("invoice_date") or "", i))],
        "linked_receipts": [{
            "id": rid, "received_date": r.received_date.isoformat() if r.received_date else None,
            "amount": float(r.amount or 0), "reference_number": r.reference_number,
            "payment_mode": r.payment_mode, "named": linked[rid],
        } for rid, r in sorted(receipts.items(), key=lambda kv: (kv[1].received_date or date.min, kv[0]))],
    }


def _payload(db: Session, tr: TdsReceipt) -> dict:
    customer = db.get(Customer, tr.customer_id)
    branch = db.get(CustomerBranch, tr.branch_id) if tr.branch_id else None
    return _serialize(db, tr, getattr(customer, "name", None), getattr(branch, "branch_name", None))


def tds_receipts_for_receipt(db: Session, receipt: CustomerReceipt, invoice_ids) -> list[dict]:
    """The Received TDS entries that go with a received amount: named on it, or
    mapped to the invoices it paid (used by customer_receipts._serialize)."""
    ids = set(db.execute(select(TdsReceipt.id).where(TdsReceipt.receipt_id == receipt.id)).scalars())
    inv_ids = [i for i in invoice_ids if i]
    if inv_ids:
        ids |= set(db.execute(
            select(TdsPayment.tds_receipt_id).join(TdsRecord, TdsRecord.id == TdsPayment.tds_record_id)
            .where(TdsRecord.invoice_id.in_(inv_ids), TdsPayment.tds_receipt_id.isnot(None)).distinct()
        ).scalars())
    if not ids:
        return []
    rows = db.execute(select(TdsReceipt).where(TdsReceipt.id.in_(ids))
                      .order_by(TdsReceipt.received_date, TdsReceipt.id)).scalars().all()
    return [{"id": t.id, "received_date": t.received_date.isoformat() if t.received_date else None,
             "amount": float(t.amount or 0), "certificate_number": t.certificate_number,
             "quarter": t.quarter, "named": t.receipt_id == receipt.id} for t in rows]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def _check_invoices(db: Session, customer_id: int, ids: list[int], branch_id: int | None) -> list[Invoice]:
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="An invoice is listed twice")
    customer = db.get(Customer, customer_id)
    invoices = db.execute(select(Invoice).where(Invoice.id.in_(set(ids)),
                                                customer_invoice_clause(customer_id))).scalars().all()
    missing = set(ids) - {i.id for i in invoices}
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Invoice(s) {sorted(missing)} do not belong to {getattr(customer, 'name', 'this customer')}")
    proforma = [i.invoice_number or f"#{i.id}" for i in invoices
                if getattr(i.kind, "value", i.kind) == InvoiceKind.PROFORMA.value]
    if proforma:
        raise HTTPException(status_code=400, detail=f"A Proforma takes no TDS: {', '.join(proforma)}")
    if branch_id is not None:
        from services.invoice_facets import invoice_facets
        facets = invoice_facets(db, invoices)
        other = [i.invoice_number or f"#{i.id}" for i in invoices
                 if (facets.get(i.id) or {}).get("branch_id") not in (None, branch_id)]
        if other:
            bname = getattr(db.get(CustomerBranch, branch_id), "branch_name", "this branch")
            raise HTTPException(status_code=400, detail=f"Not invoices of {bname}: {', '.join(other)}")
    return invoices


def _infer_branch(db: Session, invoices: list[Invoice]) -> int | None:
    if not invoices:
        return None
    from services.invoice_facets import invoice_facets
    facets = invoice_facets(db, invoices)
    ids = {(facets.get(i.id) or {}).get("branch_id") for i in invoices}
    return next(iter(ids)) if len(ids) == 1 and None not in ids else None


def _apply(db: Session, tr: TdsReceipt, invoices: list[Invoice], items: list[TdsMapItem],
           user: CurrentUser) -> tuple[list[tuple[Invoice, Decimal]], Decimal]:
    by_id = {i.id: i for i in invoices}
    try:
        plan, left = plan_tds(_d(tr.unallocated_amount), [(by_id[it.invoice_id], it.amount) for it in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for inv, amt in plan:
        rec = inv.tds_record
        if rec is None:
            figures = tds_svc.work_out(inv, amount=amt)
            rec = tds_svc.book(db, inv, figures, section=tr.section, user_id=user.id)
            rec.tds_receipt_id = tr.id
            db.flush()
        db.add(TdsPayment(tds_record_id=rec.id, payment_date=tr.received_date, amount=amt,
                          transaction_id=(tr.certificate_number or tr.reference_number or None),
                          attachment_url=tr.proof_url, notes=f"From Received TDS #{tr.id}",
                          tds_receipt_id=tr.id))
        tax.apply_tds_payment(rec, amt)
    tr.unallocated_amount = left
    return plan, left


def _unmap(db: Session, tr: TdsReceipt, invoice_id: int) -> Decimal:
    rec = db.execute(select(TdsRecord).where(TdsRecord.invoice_id == invoice_id)).scalars().first()
    if rec is None:
        return Decimal("0")
    mine = [p for p in list(rec.payments or []) if p.tds_receipt_id == tr.id]
    back = sum((_d(p.amount) for p in mine), Decimal("0"))
    for p in mine:
        rec.payments.remove(p)
        db.delete(p)
    if back:
        tax.apply_tds_payment(rec, -back)
    db.flush()
    if getattr(rec, "tds_receipt_id", None) == tr.id and not list(rec.payments or []):
        tds_svc.unbook(db, rec)
    tr.unallocated_amount = (_d(tr.unallocated_amount) + back).quantize(TWO)
    return back


# ---------------------------------------------------------------------------
# Routes (literal paths before /{id})
# ---------------------------------------------------------------------------

@router.get("/invoice-options")
def invoice_options(customer_id: int, branch_id: int | None = None,
                    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """Invoices of this customer whose TDS can still be marked received: TDS
    pending from the client, or no TDS yet and money still open. Every issued
    tax invoice is offered — the approval / IRN note is information only."""
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    invoices = db.execute(select(Invoice).where(customer_invoice_clause(customer_id),
                                                Invoice.kind != InvoiceKind.PROFORMA.value)
                          .order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).scalars().all()
    invoices = [i for i in invoices if open_tds(i) > 0 or cap_for(i) > 0]
    rows = _invoice_brief(db, invoices)
    by_id = {i.id: i for i in invoices}
    for r in rows:
        inv = by_id[r["id"]]
        r["tds_open"] = float(open_tds(inv))
        r["tds_cap"] = float(cap_for(inv))
        r["has_tds"] = inv.tds_record is not None
    rows = [r for r in rows if r["tds_cap"] > 0]
    if branch_id is not None:
        rows = [r for r in rows if (r["branch_id"] or 0) == branch_id]
    branches = db.execute(select(CustomerBranch.id, CustomerBranch.branch_name)
                          .where(CustomerBranch.customer_id == customer_id)
                          .order_by(CustomerBranch.branch_name)).all()
    return envelope(data=rows, message="Invoices", meta={
        "branches": [{"id": b.id, "name": b.branch_name} for b in branches],
        "default_tds_rate": float(tax.default_tds_rate())})


@router.get("/receipt-options")
def receipt_options(customer_id: int, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(RC_READ)):
    rows = db.execute(select(CustomerReceipt).where(CustomerReceipt.customer_id == customer_id)
                      .order_by(CustomerReceipt.received_date.desc(), CustomerReceipt.id.desc())
                      .limit(200)).scalars().all()
    bn = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name).where(
        CustomerBranch.customer_id == customer_id)).all())
    return envelope(data=[{
        "id": r.id, "received_date": r.received_date.isoformat() if r.received_date else None,
        "amount": float(r.amount or 0), "reference_number": r.reference_number,
        "payment_mode": r.payment_mode, "branch_id": r.branch_id, "branch_name": bn.get(r.branch_id),
        "tds_total": float(getattr(r, "tds_total", 0) or 0),
    } for r in rows], message="Received amounts")


@router.get("")
def list_tds_receipts(customer_id: int | None = None, branch_id: int | None = None,
                      received_from: date | None = None, received_to: date | None = None,
                      receipt_id: int | None = None,
                      p: PageParams = Depends(page_params),
                      db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    stmt = (select(TdsReceipt).join(Customer, Customer.id == TdsReceipt.customer_id)
            .join(CustomerBranch, CustomerBranch.id == TdsReceipt.branch_id, isouter=True))
    filters = []
    if customer_id is not None:
        filters.append(TdsReceipt.customer_id == customer_id)
    if branch_id is not None:
        filters.append(TdsReceipt.branch_id.is_(None) if branch_id == 0 else TdsReceipt.branch_id == branch_id)
    if received_from is not None:
        filters.append(TdsReceipt.received_date >= received_from)
    if received_to is not None:
        filters.append(TdsReceipt.received_date <= received_to)
    if receipt_id is not None:
        filters.append(TdsReceipt.receipt_id == receipt_id)
    if p.search:
        like = f"%{p.search}%"
        inv_ids = select(Invoice.id).where(Invoice.invoice_number.ilike(like))
        by_inv = (select(TdsPayment.tds_receipt_id).join(TdsRecord, TdsRecord.id == TdsPayment.tds_record_id)
                  .where(TdsRecord.invoice_id.in_(inv_ids), TdsPayment.tds_receipt_id.isnot(None)))
        filters.append(or_(TdsReceipt.certificate_number.ilike(like), TdsReceipt.reference_number.ilike(like),
                           TdsReceipt.notes.ilike(like), TdsReceipt.quarter.ilike(like),
                           Customer.name.ilike(like), Customer.legal_entity_name.ilike(like),
                           CustomerBranch.branch_name.ilike(like), TdsReceipt.id.in_(by_inv)))
    if filters:
        stmt = stmt.where(*filters)
    sort_col = {"received_date": TdsReceipt.received_date, "amount": TdsReceipt.amount,
                "customer_name": Customer.name, "branch_name": CustomerBranch.branch_name,
                }.get(p.sort_by or "", TdsReceipt.received_date)
    stmt = stmt.order_by(sort_col.asc() if p.sort_dir == "asc" else sort_col.desc(), TdsReceipt.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    names = {c.id: c.name for c in db.execute(select(Customer).where(
        Customer.id.in_({r.customer_id for r in items}))).scalars().all()} if items else {}
    bnames = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name).where(
        CustomerBranch.id.in_({r.branch_id for r in items if r.branch_id})
    )).all()) if any(r.branch_id for r in items) else {}
    tot = select(func.coalesce(func.sum(TdsReceipt.amount), 0), func.coalesce(func.sum(TdsReceipt.unallocated_amount), 0))
    if customer_id is not None:
        tot = tot.where(TdsReceipt.customer_id == customer_id)
    total, unmapped = db.execute(tot).one()
    return envelope(data=[_serialize(db, r, names.get(r.customer_id), bnames.get(r.branch_id)) for r in items],
                    message="Received TDS",
                    meta={**(meta or {}), "total_received": float(total or 0), "total_unmapped": float(unmapped or 0)})


@router.get("/{tr_id}")
def get_tds_receipt(tr_id: int, db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    tr = db.get(TdsReceipt, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Received TDS not found")
    return envelope(_payload(db, tr))


def _check_receipt_link(db: Session, receipt_id: int | None, customer_id: int) -> int | None:
    if not receipt_id:
        return None
    r = db.get(CustomerReceipt, receipt_id)
    if r is None or r.customer_id != customer_id:
        raise HTTPException(status_code=400, detail="That received amount is not this customer's")
    return r.id


@router.post("")
def create_tds_receipt(body: TdsReceiptIn, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(RC_WRITE)):
    customer = db.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    branch_id = body.branch_id
    if branch_id is not None:
        b = db.get(CustomerBranch, branch_id)
        if b is None or b.customer_id != body.customer_id:
            raise HTTPException(status_code=400, detail=f"That branch does not belong to {customer.name}")
    invoices = _check_invoices(db, body.customer_id, [it.invoice_id for it in body.items], branch_id) \
        if body.items else []
    if branch_id is None:
        branch_id = _infer_branch(db, invoices)
    tr = TdsReceipt(
        customer_id=body.customer_id, branch_id=branch_id, received_date=body.received_date,
        amount=_d(body.amount), unallocated_amount=_d(body.amount),
        certificate_number=(body.certificate_number or "").strip() or None,
        quarter=(body.quarter or "").strip() or None,
        section=(body.section or "").strip().upper() or None,
        reference_number=(body.reference_number or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        receipt_id=_check_receipt_link(db, body.receipt_id, body.customer_id),
        proof_url=clean_proof_url(body.proof_url), proof_name=(body.proof_name or "").strip()[:255] or None,
        created_by=user.id,
    )
    db.add(tr)
    db.flush()
    plan, left = ([], _d(body.amount))
    if invoices:
        plan, left = _apply(db, tr, invoices, body.items, user)
    db.commit()
    db.refresh(tr)
    msg = f"Received TDS {float(tr.amount):,.2f} recorded"
    if plan:
        msg += f" — mapped to {len(plan)} invoice(s)"
    if left > 0:
        msg += f", {float(left):,.2f} not mapped yet"
    return envelope(_payload(db, tr), message=msg)


@router.put("/{tr_id}")
def update_tds_receipt(tr_id: int, body: TdsReceiptUpdate, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(RC_WRITE)):
    tr = db.get(TdsReceipt, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Received TDS not found")
    sent = body.model_fields_set
    mapped = (_d(tr.amount) - _d(tr.unallocated_amount)).quantize(TWO)
    if "amount" in sent and body.amount is not None:
        new = _d(body.amount)
        if new < mapped:
            raise HTTPException(status_code=400,
                                detail=f"{float(mapped):,.2f} is already mapped to invoices — unmap an invoice first")
        tr.amount = new
        tr.unallocated_amount = (new - mapped).quantize(TWO)
    if "received_date" in sent and body.received_date is not None:
        tr.received_date = body.received_date
    for key in ("certificate_number", "quarter", "reference_number", "notes"):
        if key in sent:
            setattr(tr, key, (getattr(body, key) or "").strip() or None)
    if "section" in sent:
        tr.section = (body.section or "").strip().upper() or None
    if "receipt_id" in sent:
        tr.receipt_id = _check_receipt_link(db, body.receipt_id, tr.customer_id)
    if "branch_id" in sent:
        if body.branch_id:
            b = db.get(CustomerBranch, body.branch_id)
            if b is None or b.customer_id != tr.customer_id:
                raise HTTPException(status_code=400, detail="That branch does not belong to this customer")
        tr.branch_id = body.branch_id or None
    if "proof_url" in sent:
        tr.proof_url = clean_proof_url(body.proof_url)
        tr.proof_name = (body.proof_name or "").strip()[:255] or None if tr.proof_url else None
    db.commit()
    db.refresh(tr)
    return envelope(_payload(db, tr), message="Received TDS updated")


@router.post("/{tr_id}/allocate")
def allocate_tds_receipt(tr_id: int, body: TdsAllocateIn, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(RC_WRITE)):
    tr = db.get(TdsReceipt, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Received TDS not found")
    if not body.items:
        raise HTTPException(status_code=400, detail="Tick at least one invoice to map")
    branch_id = tr.branch_id
    if branch_id is None and body.branch_id:
        b = db.get(CustomerBranch, body.branch_id)
        if b is None or b.customer_id != tr.customer_id:
            raise HTTPException(status_code=400, detail="That branch does not belong to this customer")
        branch_id = b.id
    invoices = _check_invoices(db, tr.customer_id, [it.invoice_id for it in body.items], branch_id)
    if branch_id is None:
        branch_id = _infer_branch(db, invoices)
    tr.branch_id = branch_id
    plan, left = _apply(db, tr, invoices, body.items, user)
    if not plan:
        db.rollback()
        raise HTTPException(status_code=400,
                            detail="Nothing to map — nothing is left on this TDS, or the ticked invoices have no TDS open")
    db.commit()
    db.refresh(tr)
    applied = sum((a for _, a in plan), Decimal("0"))
    msg = f"Mapped TDS {float(applied):,.2f} to {len(plan)} invoice(s)"
    msg += f" — {float(left):,.2f} not mapped yet" if left > 0 else " — all of it mapped"
    return envelope(_payload(db, tr), message=msg)


@router.delete("/{tr_id}/allocations/{invoice_id}")
def unmap_tds_invoice(tr_id: int, invoice_id: int, db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(RC_WRITE)):
    tr = db.get(TdsReceipt, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Received TDS not found")
    back = _unmap(db, tr, invoice_id)
    if back <= 0:
        raise HTTPException(status_code=404, detail="That invoice is not mapped to this TDS")
    num = getattr(db.get(Invoice, invoice_id), "invoice_number", None) or f"#{invoice_id}"
    db.commit()
    db.refresh(tr)
    return envelope(_payload(db, tr), message=f"{num} unmapped — {float(back):,.2f} back to map")


@router.delete("/{tr_id}")
def delete_tds_receipt(tr_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(RC_WRITE)):
    tr = db.get(TdsReceipt, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Received TDS not found")
    inv_ids = {r.invoice_id for r in db.execute(
        select(TdsRecord).join(TdsPayment, TdsPayment.tds_record_id == TdsRecord.id)
        .where(TdsPayment.tds_receipt_id == tr.id)).scalars().all()}
    for iid in inv_ids:
        _unmap(db, tr, iid)
    db.delete(tr)
    db.commit()
    return envelope(data={"id": tr_id},
                    message=f"Received TDS removed — {len(inv_ids)} invoice(s) read TDS pending from client again")
