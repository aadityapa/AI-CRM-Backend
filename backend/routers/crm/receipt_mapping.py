"""Map money onto invoices straight from the Invoices list (20 Nov 2026, Finance ask).

"Unpaid 119 invoices — give me a shortcut to map these to the customer's
Received Amount and Received TDS from here." Finance ticks invoices on the
Invoices ▸ Unpaid / Partially paid tab (or presses Map on one row) and, in ONE
dialog and ONE transaction, says per invoice:

* cash ₹ — taken from a received amount still ON ACCOUNT, or from a new received
  amount recorded in the same step (what is not mapped stays on account);
* TDS ₹ — either *received* (Form 16A in) from a Received TDS entry — existing
  or new — or *deducted, certificate pending* (booked as "pending from client").

The rules are the existing ones (nothing new is invented):
* the invoices belong to ONE customer (`customer_invoice_clause` — the project's
  or the PO's customer); Proformas take no money;
* TDS goes first (it settles part of the invoice), then the cash, each ≤ what is
  open (`plan_tds` / `plan_mapping`);
* invoices of several branches may share one received amount here — the
  receipt keeps its branch (a new one takes the invoices' most common branch),
  exactly like Map invoices' "map anyway".

Endpoints (Received Amount gates):
  GET  /api/receipt-mapping/options?invoice_ids=1,2,3[&customer_id=]
  POST /api/receipt-mapping
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db
from models import Customer, CustomerBranch, CustomerReceipt, Invoice, InvoicePayment
from models.finance import InvoiceKind, TdsReceipt
from routers.crm.customer_receipts import (
    PAYMENT_MODES, RC_READ, RC_WRITE, _customers_of, _invoice_brief, clean_proof_url,
    customer_invoice_clause, likely_match, plan_mapping,
)
from routers.crm.tds_receipts import TdsMapItem, _apply as apply_tds, cap_for, open_tds
from schemas.common import envelope
from services import tax
from services import tds as tds_svc

router = APIRouter(prefix="/api/receipt-mapping", tags=["receipt-mapping"])

TWO = Decimal("0.01")
MAX_INVOICES = 100


def _d(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(TWO)


def common_customer(per_invoice: dict[int, tuple[int | None, set[int]]],
                    wanted: int | None = None) -> int | None:
    """The one customer every invoice may take money from (PURE). `wanted`
    wins when every invoice allows it; else the invoices' own customer when
    they all share it; else any customer they all allow; else None."""
    if not per_invoice:
        return None
    allowed = None
    for _own, cids in per_invoice.values():
        allowed = set(cids) if allowed is None else allowed & set(cids)
    allowed = allowed or set()
    if wanted is not None:
        return wanted if wanted in allowed else None
    owns = {own for own, _ in per_invoice.values()}
    if len(owns) == 1 and next(iter(owns)) in allowed:
        return next(iter(owns))
    return min(allowed) if allowed else None


def _load_invoices(db: Session, ids: list[int]) -> list[Invoice]:
    if not ids:
        raise HTTPException(status_code=400, detail="Tick at least one invoice")
    if len(ids) > MAX_INVOICES:
        raise HTTPException(status_code=400, detail=f"Map at most {MAX_INVOICES} invoices at a time")
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="An invoice is listed twice")
    invoices = db.execute(select(Invoice).where(Invoice.id.in_(set(ids)))).scalars().all()
    missing = set(ids) - {i.id for i in invoices}
    if missing:
        raise HTTPException(status_code=404, detail=f"Invoice(s) {sorted(missing)} not found")
    proforma = [i.invoice_number or f"#{i.id}" for i in invoices
                if getattr(i.kind, "value", i.kind) == InvoiceKind.PROFORMA.value]
    if proforma:
        raise HTTPException(status_code=400, detail=f"A Proforma takes no money: {', '.join(proforma)}")
    return sorted(invoices, key=lambda i: (i.invoice_date or date.min, i.id))


def _customer_for(db: Session, invoices: list[Invoice], wanted: int | None) -> Customer:
    per = _customers_of(db, invoices)
    cid = common_customer(per, wanted)
    if cid is None:
        names = {c.id: c.name for c in db.execute(select(Customer).where(
            Customer.id.in_({own for own, _ in per.values() if own}))).scalars().all()}
        listed = ", ".join(sorted({names.get(own, f"#{own}") for own, _ in per.values() if own}))
        raise HTTPException(status_code=400,
                            detail=f"Tick invoices of one customer — these belong to {listed or 'different customers'}")
    return db.get(Customer, cid)


def _parse_ids(raw: str) -> list[int]:
    try:
        return [int(x) for x in (raw or "").split(",") if x.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invoice_ids must be numbers") from exc


@router.get("/options")
def options(invoice_ids: str, customer_id: int | None = None,
            db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """The ticked invoices (balance, TDS open) and the customer's money that can
    pay them: received amounts still on account and Received TDS not mapped yet."""
    invoices = _load_invoices(db, _parse_ids(invoice_ids))
    customer = _customer_for(db, invoices, customer_id)
    rows = _invoice_brief(db, invoices)
    by_id = {i.id: i for i in invoices}
    for r in rows:
        inv = by_id[r["id"]]
        r["tds_open"] = float(open_tds(inv))
        r["tds_cap"] = float(cap_for(inv))
        r["has_tds"] = inv.tds_record is not None
    bnames = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name)
                             .where(CustomerBranch.customer_id == customer.id)).all())
    receipts = db.execute(select(CustomerReceipt).where(
        CustomerReceipt.customer_id == customer.id, CustomerReceipt.unallocated_amount > 0)
        .order_by(CustomerReceipt.received_date.desc(), CustomerReceipt.id.desc()).limit(100)).scalars().all()
    total_open = sum((_d(i.balance_amount) for i in invoices), Decimal("0"))
    tds_entries = []
    try:
        with db.begin_nested():
            tds_entries = db.execute(select(TdsReceipt).where(
                TdsReceipt.customer_id == customer.id, TdsReceipt.unallocated_amount > 0)
                .order_by(TdsReceipt.received_date.desc(), TdsReceipt.id.desc()).limit(100)).scalars().all()
    except Exception:  # noqa: BLE001 — a database before 0155 still maps cash
        tds_entries = []
    return envelope({
        "customer": {"id": customer.id, "name": customer.name},
        "invoices": rows,
        "receipts": [{
            "id": r.id, "received_date": r.received_date.isoformat() if r.received_date else None,
            "amount": float(r.amount or 0), "unallocated_amount": float(r.unallocated_amount or 0),
            "payment_mode": r.payment_mode, "reference_number": r.reference_number,
            "branch_id": r.branch_id, "branch_name": bnames.get(r.branch_id),
            "likely_match": likely_match(total_open, r.unallocated_amount),
        } for r in receipts],
        "tds_entries": [{
            "id": t.id, "received_date": t.received_date.isoformat() if t.received_date else None,
            "amount": float(t.amount or 0), "unallocated_amount": float(t.unallocated_amount or 0),
            "certificate_number": t.certificate_number, "quarter": t.quarter, "section": t.section,
            "branch_name": bnames.get(t.branch_id),
        } for t in tds_entries],
        "payment_modes": PAYMENT_MODES,
        "default_tds_rate": float(tax.default_tds_rate()),
    }, message="Mapping options")


class MapItem(BaseModel):
    invoice_id: int
    cash: Decimal | None = Field(default=None, gt=0)
    tds: Decimal | None = Field(default=None, gt=0)


class NewReceiptIn(BaseModel):
    received_date: date
    amount: Decimal = Field(gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)


class NewTdsIn(BaseModel):
    received_date: date
    amount: Decimal = Field(gt=0)
    certificate_number: str | None = Field(default=None, max_length=64)
    quarter: str | None = Field(default=None, max_length=16)
    section: str | None = Field(default=None, max_length=16)
    reference_number: str | None = Field(default=None, max_length=128)


class MapIn(BaseModel):
    customer_id: int | None = None
    items: list[MapItem] = Field(min_length=1, max_length=MAX_INVOICES)
    #: Cash source: an existing received amount on account, or a new one.
    receipt_id: int | None = None
    new_receipt: NewReceiptIn | None = None
    #: TDS: "received" (Form 16A in — needs a Received TDS entry, existing or new)
    #: or "pending" (the customer deducted it; certificate not in yet).
    tds_mode: str = Field(default="received", pattern="^(received|pending)$")
    tds_receipt_id: int | None = None
    new_tds: NewTdsIn | None = None


def _usual_branch(db: Session, invoices: list[Invoice]) -> int | None:
    from services.invoice_facets import invoice_facets
    facets = invoice_facets(db, invoices)
    counts = Counter((facets.get(i.id) or {}).get("branch_id") for i in invoices)
    counts.pop(None, None)
    return counts.most_common(1)[0][0] if counts else None


@router.post("")
def map_money(body: MapIn, db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_WRITE)):
    ids = [it.invoice_id for it in body.items]
    invoices = _load_invoices(db, ids)
    customer = _customer_for(db, invoices, body.customer_id)
    # Belt and braces — the customer rule the receipts use.
    ok = set(db.execute(select(Invoice.id).where(Invoice.id.in_(ids),
                                                 customer_invoice_clause(customer.id))).scalars())
    if set(ids) - ok:
        raise HTTPException(status_code=400, detail=f"Not invoices of {customer.name}")
    by_id = {i.id: i for i in invoices}
    cash_items = [it for it in body.items if it.cash]
    tds_items = [it for it in body.items if it.tds]
    if not cash_items and not tds_items:
        raise HTTPException(status_code=400, detail="Type the cash or TDS to put on at least one invoice")
    if body.receipt_id and body.new_receipt:
        raise HTTPException(status_code=400, detail="Choose a received amount OR record a new one, not both")
    if body.tds_receipt_id and body.new_tds:
        raise HTTPException(status_code=400, detail="Choose a Received TDS entry OR record a new one, not both")
    branch_id = _usual_branch(db, invoices)
    notes: list[str] = []

    # ---- TDS first: it settles part of each invoice ----
    tds_total = Decimal("0")
    tr = None
    if tds_items and body.tds_mode == "pending":
        for it in tds_items:
            inv = by_id[it.invoice_id]
            if inv.tds_record is not None:
                db.rollback()
                raise HTTPException(status_code=409,
                                    detail=f"TDS is already booked on {inv.invoice_number or inv.id} — "
                                           f"map its Form 16A as received instead")
            tds_svc.book(db, inv, tds_svc.work_out(inv, amount=it.tds), user_id=user.id)
            tds_total += _d(it.tds)
        notes.append(f"TDS {float(tds_total):,.2f} booked as pending from client")
    elif tds_items:
        need = sum((_d(it.tds) for it in tds_items), Decimal("0"))
        if body.tds_receipt_id:
            tr = db.get(TdsReceipt, body.tds_receipt_id)
            if tr is None or tr.customer_id != customer.id:
                raise HTTPException(status_code=400, detail=f"That Received TDS entry is not {customer.name}'s")
        elif body.new_tds:
            if _d(body.new_tds.amount) < need:
                raise HTTPException(status_code=400,
                                    detail=f"The new Received TDS ({float(body.new_tds.amount):,.2f}) is less than "
                                           f"the TDS typed on the invoices ({float(need):,.2f})")
            nt = body.new_tds
            tr = TdsReceipt(customer_id=customer.id, branch_id=branch_id, received_date=nt.received_date,
                            amount=_d(nt.amount), unallocated_amount=_d(nt.amount),
                            certificate_number=(nt.certificate_number or "").strip() or None,
                            quarter=(nt.quarter or "").strip() or None,
                            section=(nt.section or "").strip().upper() or None,
                            reference_number=(nt.reference_number or "").strip() or None,
                            created_by=user.id)
            db.add(tr)
            db.flush()
        else:
            raise HTTPException(status_code=400,
                                detail="Pick the Received TDS entry (Form 16A) the TDS comes from, record a new one, "
                                       "or say the TDS is deducted and the certificate is pending")
        if tr.branch_id is None:
            tr.branch_id = branch_id
        plan, _left = apply_tds(db, tr, [by_id[it.invoice_id] for it in tds_items],
                                [TdsMapItem(invoice_id=it.invoice_id, amount=it.tds) for it in tds_items], user)
        tds_total = sum((a for _, a in plan), Decimal("0"))
        notes.append(f"TDS {float(tds_total):,.2f} marked received from Received TDS #{tr.id}")

    # ---- then the cash ----
    receipt = None
    cash_total = Decimal("0")
    if cash_items:
        need = sum((_d(it.cash) for it in cash_items), Decimal("0"))
        if body.receipt_id:
            receipt = db.get(CustomerReceipt, body.receipt_id)
            if receipt is None or receipt.customer_id != customer.id:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"That received amount is not {customer.name}'s")
        elif body.new_receipt:
            nr = body.new_receipt
            if nr.payment_mode and nr.payment_mode not in PAYMENT_MODES:
                db.rollback()
                raise HTTPException(status_code=400, detail=f"Payment mode must be one of: {', '.join(PAYMENT_MODES)}")
            if _d(nr.amount) < need:
                db.rollback()
                raise HTTPException(status_code=400,
                                    detail=f"The new received amount ({float(nr.amount):,.2f}) is less than the "
                                           f"cash typed on the invoices ({float(need):,.2f})")
            receipt = CustomerReceipt(
                customer_id=customer.id, branch_id=branch_id, received_date=nr.received_date,
                amount=_d(nr.amount), unallocated_amount=_d(nr.amount),
                payment_mode=nr.payment_mode or None,
                reference_number=(nr.reference_number or "").strip() or None,
                notes=(nr.notes or "").strip() or None, created_by=user.id,
                proof_url=clean_proof_url(nr.proof_url), proof_name=(nr.proof_name or "").strip()[:255] or None,
            )
            db.add(receipt)
            db.flush()
        else:
            db.rollback()
            raise HTTPException(status_code=400,
                                detail="Pick the received amount the cash comes from, or record a new one")
        try:
            plan, left = plan_mapping(_d(receipt.unallocated_amount),
                                      [(by_id[it.invoice_id], it.cash) for it in cash_items])
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        for inv, take in plan:
            db.add(InvoicePayment(
                invoice_id=inv.id, payment_date=receipt.received_date, amount=take,
                payment_mode=receipt.payment_mode, reference_number=receipt.reference_number,
                notes=f"From customer receipt #{receipt.id}", receipt_id=receipt.id,
                attachment_url=receipt.proof_url, created_by=user.id,
            ))
            tax.apply_invoice_payment(inv, take)
            cash_total += take
        receipt.unallocated_amount = left
        if receipt.branch_id is None:
            receipt.branch_id = branch_id
        msg = f"{float(cash_total):,.2f} mapped from received amount #{receipt.id}"
        if left > 0:
            msg += f" ({float(left):,.2f} still on account)"
        notes.insert(0, msg)
        if tr is not None and not tr.receipt_id:
            tr.receipt_id = receipt.id  # the TDS goes with this credit
    db.commit()
    for inv in invoices:
        db.refresh(inv)
    paid = sum(1 for i in invoices if _d(i.balance_amount) <= 0)
    message = " · ".join(notes) + f" — {paid} of {len(invoices)} invoice(s) now fully settled"
    return envelope({
        "customer_id": customer.id, "receipt_id": getattr(receipt, "id", None),
        "tds_receipt_id": getattr(tr, "id", None), "cash": float(cash_total), "tds": float(tds_total),
        "invoices": [{"id": i.id, "invoice_number": i.invoice_number, "balance_amount": float(i.balance_amount or 0),
                      "payment_status": getattr(i.payment_status, "value", i.payment_status)} for i in invoices],
    }, message=message)
