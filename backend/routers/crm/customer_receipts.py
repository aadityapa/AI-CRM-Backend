"""Customer received amounts (7 Sep 2026, user request).

Finance records a bank credit from a customer once — received date, amount,
payment mode, reference number, notes — and ticks the employees' invoices it
covers. The amount is allocated across the chosen invoices oldest-first, up to
each invoice's balance; whatever is left stays on the receipt as
`unallocated_amount` (advance / on-account). Each allocation is written as an
`InvoicePayment` with `receipt_id`, so invoice paid / balance / status keep
working exactly as before; deleting a receipt reverses those payments.

Endpoints (all enveloped):
  GET    /api/customer-receipts                  list, filters customer_id / branch_id / project_id / from / to / search
  GET    /api/customer-receipts/invoice-options  open invoices for the picker (customer_id required;
                                                 branch_id narrows to one branch, 0 = no branch)

16 Oct 2026 (user ask): a receipt belongs to ONE customer BRANCH — the branch
that paid — and only that branch's employee invoices are offered; every
invoice says the month it bills and the employee (`services/invoice_facets`).

16 Oct 2026 (later, Finance ask): TDS. For each ticked invoice Finance may say
the customer withheld TDS (`tds: [{invoice_id, tds_rate, tds_base}]`). The TDS
is booked first (`services/tds.book` — it settles the invoice beside the cash
and is followed as "pending from client"), then the cash is allocated on what
is left, so the amount Finance expects in the bank is the invoice balance less
the TDS. Deleting the receipt takes the TDS back off (409 once it was marked
received).
  POST   /api/customer-receipts                  create + allocate
  DELETE /api/customer-receipts/{id}             reverse allocations, delete (Finance / Admin)

19 Oct 2026 (user ask): the PROOF of the credit — a bank screenshot,
remittance advice or UTR PDF. Upload first, then pass the url with the receipt
(or attach it later):
  POST   /api/customer-receipts/proof            upload a proof file → {url, name}
  PUT    /api/customer-receipts/{id}/proof       attach / replace / clear the proof
Every invoice payment the receipt makes carries the same file in
`attachment_url`, so the proof also opens from the invoice.

22 Oct 2026 (Finance ask, screenshot): money often arrives BEFORE Finance
knows which invoices it pays — it is recorded "on account" first and mapped
later. A saved receipt can now be worked on:
  PUT    /api/customer-receipts/{id}                       edit date / amount / mode / reference / notes / branch
  POST   /api/customer-receipts/{id}/allocate              map the unallocated amount onto invoices (+ TDS)
  DELETE /api/customer-receipts/{id}/allocations/{inv}     unmap one invoice — its money goes back on account
The amount can never drop below what is already applied; mapping follows the
same rules as Add (same customer, the receipt's branch, customer approval + IRN
in); a receipt with no branch takes the branch of the invoices it is mapped to.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, gated_read, gated_write_action, get_crm_db, page_params
from models import (
    Customer, CustomerBranch, CustomerReceipt, Employee, Invoice, InvoicePayment, Project, Timesheet,
)
from models.finance import TdsRecord
from models.finance import InvoiceKind
from schemas.common import envelope
from services import tax
from services.crm_common import paginate, save_upload
from services import tds as tds_svc
from services.finance import serialize_tds
from services.invoice_customer_approval import payment_block

router = APIRouter(prefix="/api/customer-receipts", tags=["customer-receipts"])

RC_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
RC_WRITE = gated_write_action("invoice.manage", "invoices", "Finance")

PAYMENT_MODES = ["NEFT", "RTGS", "IMPS", "UPI", "Cheque", "Cash", "Wire", "Other"]
TWO = Decimal("0.01")


def customer_invoice_clause(customer_id: int):
    """An invoice is this customer's when its project OR its PO is theirs —
    the Invoices list / ledger rule (20–22 Oct 2026). 24 Oct 2026, user report:
    a Partially Paid MAHLE invoice could not take the customer's next payment
    advice — the picker only looked at the PROJECT's customer, while the list
    shows the invoice under the PO's customer."""
    from models import PurchaseOrder

    return or_(
        Invoice.project_id.in_(select(Project.id).where(Project.customer_id == customer_id)),
        Invoice.po_id.in_(select(PurchaseOrder.id).where(PurchaseOrder.customer_id == customer_id)),
    )


#: Two amounts "match" when they differ by less than this (round-off on the advice).
MATCH_TOLERANCE = Decimal("1.00")


def likely_match(balance, amount) -> bool:
    """PURE: does a payment of `amount` look like the balance of this invoice?
    24 Oct 2026 (user ask): a second payment advice often carries no invoice
    number — the invoice whose balance is that amount is the likely one."""
    if amount is None or balance is None:
        return False
    b, a = _d(balance), _d(amount)
    return b > 0 and abs(b - a) < MATCH_TOLERANCE


class ReceiptIn(BaseModel):
    customer_id: int
    #: The paying branch (16 Oct 2026). Every ticked invoice must belong to it.
    branch_id: int | None = None
    received_date: date
    amount: Decimal = Field(gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    #: Invoices this money covers — allocated oldest-first up to each balance.
    invoice_ids: list[int] = Field(default_factory=list, max_length=100)
    #: TDS the customer withheld, per ticked invoice (16 Oct 2026).
    tds: list["ReceiptTdsIn"] = Field(default_factory=list, max_length=100)
    #: Amount per invoice (20 Nov 2026) — Finance edits what goes on an invoice
    #: when the bank credit is less than its balance. Blank amounts take what
    #: is left, oldest first. When sent, it replaces `invoice_ids`.
    items: list["AllocateItemIn"] = Field(default_factory=list, max_length=100)
    #: Proof of the credit (19 Oct 2026) — the url `POST /proof` returned.
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)
    #: Part of this credit for invoices of an earlier financial year, not kept in
    #: Orbit (20 Nov 2026) — set aside; only the rest is mapped / on account.
    prior_period_amount: Decimal | None = Field(default=None, ge=0)
    prior_period_note: str | None = Field(default=None, max_length=1000)


class ProofIn(BaseModel):
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)


#: Proof files: images, PDFs, and the office / mail formats remittance advice
#: arrives in.
PROOF_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf", ".xlsx", ".xls", ".csv",
                    ".doc", ".docx", ".txt")
PROOF_MAX_BYTES = 15 * 1024 * 1024


def clean_proof_url(raw: str | None) -> str | None:
    """Only a file this application stored may be a proof. PURE."""
    url = (raw or "").strip()
    if not url:
        return None
    if not url.startswith("/api/crm-files/") or ".." in url:
        raise HTTPException(status_code=400, detail="Upload the proof first, then save the receipt")
    return url


class ReceiptTdsIn(BaseModel):
    invoice_id: int
    #: Percent the customer deducted; wins over tds_amount.
    tds_rate: Decimal | None = Field(default=None, gt=0, le=30)
    #: "taxable" (before GST — default) | "total".
    tds_base: str | None = Field(default=None, max_length=16)
    tds_amount: Decimal | None = Field(default=None, gt=0)
    section: str | None = Field(default=None, max_length=16)





class ReceiptUpdate(BaseModel):
    """Edit a saved receipt (22 Oct 2026). Only the fields sent change."""
    received_date: date | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    #: The paying branch; null clears it (only while nothing mapped belongs elsewhere).
    branch_id: int | None = None
    #: The earlier-financial-year part, set aside (0154); 0 clears it.
    prior_period_amount: Decimal | None = Field(default=None, ge=0)
    prior_period_note: str | None = Field(default=None, max_length=1000)


class AllocateItemIn(BaseModel):
    invoice_id: int
    #: Cash to put on this invoice; None = as much as is left, oldest first.
    amount: Decimal | None = Field(default=None, gt=0)


ReceiptIn.model_rebuild()


class AllocateIn(BaseModel):
    """Map a receipt's unallocated amount onto invoices (22 Oct 2026)."""
    items: list[AllocateItemIn] = Field(default_factory=list, max_length=100)
    tds: list[ReceiptTdsIn] = Field(default_factory=list, max_length=100)
    #: For a receipt with no branch yet: the branch these invoices belong to.
    branch_id: int | None = None
    #: 20 Nov 2026 (Finance report): map an invoice billed to ANOTHER branch of
    #: the customer — each invoice keeps its own branch; the receipt keeps its.
    other_branches: bool = False
    #: 20 Nov 2026: the receipt was recorded on the wrong customer / branch and
    #: pays nothing yet — move it to the ticked invoices' customer (+ branch).
    move_receipt: bool = False


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


def _tds_by_invoice(db: Session, invoice_ids) -> dict[int, TdsRecord]:
    ids = {i for i in invoice_ids if i}
    if not ids:
        return {}
    return {t.invoice_id: t for t in db.execute(
        select(TdsRecord).where(TdsRecord.invoice_id.in_(ids))).scalars().all()}


def _invoice_brief(db: Session, invoices: list[Invoice]) -> list[dict]:
    from services.invoice_facets import invoice_facets
    names = _employee_names_by_invoice(db, invoices)
    facets = invoice_facets(db, invoices)
    tds = _tds_by_invoice(db, [i.id for i in invoices])
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
        "sub_total": float(i.sub_total or 0),
        "tax_amount": float(i.tax_amount or 0),
        "grand_total": float(i.grand_total or 0),
        "paid_amount": float(i.paid_amount or 0),
        "tds_deducted": float(getattr(i, "tds_deducted", 0) or 0),
        # The invoice's TDS (16 Oct 2026) — None = none recorded yet.
        "tds": serialize_tds(tds[i.id]) if i.id in tds else None,
        "balance_amount": float(i.balance_amount or 0),
        "payment_status": getattr(i.payment_status, "value", i.payment_status),
        # Why money cannot be put on it yet — approval / IRN (8 Oct 2026); None = open.
        "payment_block": payment_block(i),
        "irn_recorded": bool(i.irn_number),
        # What the invoice is for (16 Oct 2026): branch + the month it bills.
        "branch_id": (facets.get(i.id) or {}).get("branch_id"),
        "branch_name": (facets.get(i.id) or {}).get("branch_name"),
        "month": (facets.get(i.id) or {}).get("month"),
        "month_label": (facets.get(i.id) or {}).get("month_label"),
        "due_date": i.due_date.isoformat() if getattr(i, "due_date", None) else None,
    } for i in invoices]


def _linked_tds(db: Session, r: CustomerReceipt, inv_ids) -> list[dict]:
    try:
        from routers.crm.tds_receipts import tds_receipts_for_receipt
        with db.begin_nested():
            return tds_receipts_for_receipt(db, r, inv_ids)
    except Exception:  # noqa: BLE001 — a table not migrated yet must not break the list
        return []


def _serialize(db: Session, r: CustomerReceipt, customer_name: str | None = None,
               branch_name: str | None = None) -> dict:
    payments = list(r.payments or [])
    booked = db.execute(select(TdsRecord).where(TdsRecord.receipt_id == r.id)).scalars().all()
    tds_here = {t.invoice_id: t for t in booked}
    inv_ids = list(dict.fromkeys([p.invoice_id for p in payments] + list(tds_here)))
    invoices = db.execute(select(Invoice).where(Invoice.id.in_(inv_ids))).scalars().all() if inv_ids else []
    briefs = {b["id"]: b for b in _invoice_brief(db, invoices)}
    cash = {}
    pay_id = {}
    for p in payments:
        cash[p.invoice_id] = cash.get(p.invoice_id, Decimal("0")) + _d(p.amount)
        pay_id.setdefault(p.invoice_id, p.id)
    return {
        "id": r.id,
        "customer_id": r.customer_id,
        "customer_name": customer_name,
        "branch_id": r.branch_id,
        "branch_name": branch_name,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "amount": float(r.amount or 0),
        "payment_mode": r.payment_mode,
        "reference_number": r.reference_number,
        "notes": r.notes,
        "unallocated_amount": float(r.unallocated_amount or 0),
        # The earlier-financial-year part set aside (0154) — never mapped, never counted.
        "prior_period_amount": float(getattr(r, "prior_period_amount", 0) or 0),
        "prior_period_note": getattr(r, "prior_period_note", None),
        "counted_amount": float(_d(r.amount) - _d(getattr(r, "prior_period_amount", 0))),
        "allocated_amount": float(sum((_d(p.amount) for p in payments), Decimal("0"))),
        "tds_total": float(getattr(r, "tds_total", 0) or 0),
        "tds_pending": float(sum((_d(t.tds_balance) for t in booked), Decimal("0"))),
        "proof_url": getattr(r, "proof_url", None),
        "proof_name": getattr(r, "proof_name", None),
        # The Received TDS entries that go with this money (20 Nov 2026).
        "tds_receipts": _linked_tds(db, r, inv_ids),
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        # One line per invoice: the cash it got from this receipt + the TDS the
        # customer withheld on it (booked with this receipt).
        "allocations": [{
            **{k: v for k, v in (briefs.get(iid) or {}).items() if k not in ("id", "tds")},
            "payment_id": pay_id.get(iid), "invoice_id": iid,
            "amount": float(cash.get(iid, Decimal("0"))),
            "tds": serialize_tds(tds_here[iid]) if iid in tds_here else (briefs.get(iid) or {}).get("tds"),
            "tds_booked_here": iid in tds_here,
        } for iid in inv_ids],
    }


@router.get("/invoice-options")
def invoice_options(customer_id: int, include_paid: bool = False, branch_id: int | None = None,
                    match_amount: Decimal | None = None,
                    db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """Invoices of this customer (project OR PO) for the picker — open ones by
    default (Unpaid AND Partially paid — a balance left after an earlier advice
    takes the next one), with the employee each one bills and its month,
    oldest first. `branch_id` keeps one branch's invoices (0 = invoices with no
    branch); `meta.branches` lists the customer's branches with their open count.
    `match_amount` (24 Oct 2026) flags `likely_match` on invoices whose balance
    is that amount — the advice that came with no invoice number."""
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    stmt = select(Invoice).where(customer_invoice_clause(customer_id),
                                 Invoice.kind != InvoiceKind.PROFORMA.value)
    if not include_paid:
        stmt = stmt.where(Invoice.balance_amount > 0)
    invoices = db.execute(stmt.order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).scalars().all()
    rows = _invoice_brief(db, invoices)
    for r in rows:
        r["likely_match"] = likely_match(r["balance_amount"], match_amount)
    branches = db.execute(select(CustomerBranch.id, CustomerBranch.branch_name)
                          .where(CustomerBranch.customer_id == customer_id)
                          .order_by(CustomerBranch.branch_name)).all()
    counts: dict[int, int] = {}
    balance: dict[int, float] = {}
    for r in rows:
        k = r["branch_id"] or 0
        counts[k] = counts.get(k, 0) + 1
        balance[k] = balance.get(k, 0.0) + r["balance_amount"]
    meta = {"branches": [{"id": b.id, "name": b.branch_name, "invoices": counts.get(b.id, 0),
                          "balance": round(balance.get(b.id, 0.0), 2)} for b in branches]}
    if counts.get(0):
        meta["no_branch"] = {"invoices": counts[0], "balance": round(balance.get(0, 0.0), 2)}
    if branch_id is not None:
        rows = [r for r in rows if (r["branch_id"] or 0) == branch_id]
    # The TDS dialog's starting rate (Settings ▸ Finance), and the usual sections.
    meta["default_tds_rate"] = float(tax.default_tds_rate())
    meta["tds_sections"] = [{"section": "194J", "rate": 10, "label": "194J · professional / technical services"},
                            {"section": "194J", "rate": 2, "label": "194J · technical services (2%)"},
                            {"section": "194C", "rate": 2, "label": "194C · contract work (company)"},
                            {"section": "194C", "rate": 1, "label": "194C · contract work (individual / HUF)"}]
    return envelope(data=rows, message="Invoices", meta=meta)


#: Invoices the Map dialog's search returns (20 Nov 2026).
FIND_LIMIT = 15


def invoice_fit(*, receipt_customer_id: int, receipt_branch_id: int | None, receipt_mapped: bool,
                receipt_customer_name: str | None, receipt_branch_name: str | None,
                inv_customer_id: int | None, inv_customer_ids: set[int], inv_customer_name: str | None,
                inv_branch_id: int | None, inv_branch_name: str | None,
                is_proforma: bool, balance) -> dict:
    """PURE. Can this receipt pay this invoice, and if not, why not?

    20 Nov 2026 (Finance report, screenshot): an invoice searched for by its
    number read "No invoice matches" in Map invoices — it was billed to another
    branch (or another customer record) than the receipt, so the picker never
    listed it and nothing said why. Every search result now carries the reason
    and what mapping it would do:
      here           — this customer, this branch (or either has none)
      other_branch   — this customer, another branch: mappable; a receipt that
                       pays nothing yet MOVES to that branch, else it stays
      other_customer — mappable only by MOVING a receipt that pays nothing yet
      paid / proforma — nothing to map
    """
    rc_name = receipt_customer_name or "this customer"
    if is_proforma:
        return {"fit": "proforma", "can_map": False, "move_receipt": False,
                "reason": "Still a Proforma — Finance generates the tax invoice first, then it takes money"}
    if _d(balance) <= 0:
        return {"fit": "paid", "can_map": False, "move_receipt": False,
                "reason": "Already fully paid — nothing left to map"}
    if receipt_customer_id in inv_customer_ids:
        if receipt_branch_id is None or inv_branch_id is None or inv_branch_id == receipt_branch_id:
            return {"fit": "here", "can_map": True, "move_receipt": False, "reason": None}
        b = inv_branch_name or "another branch"
        here = receipt_branch_name or "its branch"
        if not receipt_mapped:
            return {"fit": "other_branch", "can_map": True, "move_receipt": True,
                    "reason": f"Billed to {b} — this receipt is on {here}. Mapping moves the receipt to {b}."}
        return {"fit": "other_branch", "can_map": True, "move_receipt": False,
                "reason": f"Billed to {b} — this receipt is on {here} and already pays its invoices. "
                          f"It can still be mapped; each invoice keeps its own branch."}
    other = inv_customer_name or "another customer"
    where = f"{other}{f' · {inv_branch_name}' if inv_branch_name else ''}"
    if receipt_mapped:
        return {"fit": "other_customer", "can_map": False, "move_receipt": False,
                "reason": f"Billed to {other}, not {rc_name} — this receipt already pays {rc_name}'s invoices. "
                          f"Unmap them first, or record the money on {other}."}
    return {"fit": "other_customer", "can_map": True, "move_receipt": True,
            "reason": f"Billed to {other}, not {rc_name}. Mapping moves this receipt to {where}."}


def _receipt_mapped(db: Session, receipt: CustomerReceipt) -> bool:
    if receipt.payments:
        return True
    return db.execute(select(TdsRecord.id).where(TdsRecord.receipt_id == receipt.id).limit(1)).first() is not None


def _customers_of(db: Session, invoices: list[Invoice]) -> dict[int, tuple[int | None, set[int]]]:
    """invoice id → (its customer — PO's else project's, every customer it may
    take money from). Two queries for the batch."""
    from models import PurchaseOrder
    from services.billing_rules import invoice_customer_id

    pids = {i.project_id for i in invoices if i.project_id}
    poids = {i.po_id for i in invoices if i.po_id}
    pc = dict(db.execute(select(Project.id, Project.customer_id).where(Project.id.in_(pids))).all()) if pids else {}
    oc = dict(db.execute(select(PurchaseOrder.id, PurchaseOrder.customer_id)
                         .where(PurchaseOrder.id.in_(poids))).all()) if poids else {}
    out = {}
    for i in invoices:
        p, o = pc.get(i.project_id), oc.get(i.po_id)
        out[i.id] = (invoice_customer_id(p, o), {c for c in (p, o) if c})
    return out


@router.get("/{receipt_id}/find-invoices")
def find_invoices(receipt_id: int, q: str, branch_id: int | None = None,
                  db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """Search EVERY tax invoice (and Proforma, to say why not) by invoice / PI /
    PO number, project or employee — whatever customer or branch it is billed
    to — and say whether this receipt can pay it (`invoice_fit`). `branch_id` is
    the branch picked in the dialog for a receipt with none yet."""
    from models import PurchaseOrder
    from services.search_terms import clean_search

    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    term = clean_search(q)
    if not term or len(term) < 2:
        return envelope(data=[], message="Type at least 2 characters", meta={"term": term})
    like = f"%{term}%"
    emp_ts = (select(Timesheet.id).join(Employee, Employee.id == Timesheet.employee_id)
              .where(or_(Employee.first_name.ilike(like), Employee.last_name.ilike(like),
                         (Employee.first_name + " " + Employee.last_name).ilike(like),
                         Employee.employee_code.ilike(like))))
    stmt = (select(Invoice)
            .join(Project, Project.id == Invoice.project_id, isouter=True)
            .join(PurchaseOrder, PurchaseOrder.id == Invoice.po_id, isouter=True)
            .where(or_(Invoice.invoice_number.ilike(like), Invoice.proforma_number.ilike(like),
                       PurchaseOrder.po_number.ilike(like), Project.name.ilike(like),
                       Invoice.timesheet_id.in_(emp_ts)))
            .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
            .limit(FIND_LIMIT))
    invoices = db.execute(stmt).scalars().unique().all()
    briefs = _invoice_brief(db, invoices)
    custs = _customers_of(db, invoices)
    cids = {c for c, _ in custs.values() if c} | {receipt.customer_id}
    cnames = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cids))).all())
    rb_id = receipt.branch_id if receipt.branch_id is not None else branch_id
    rb_name = db.execute(select(CustomerBranch.branch_name).where(CustomerBranch.id == rb_id)).scalar() \
        if rb_id else None
    mapped = _receipt_mapped(db, receipt)
    by_id = {i.id: i for i in invoices}
    rows = []
    for b in briefs:
        inv = by_id[b["id"]]
        cid, cset = custs.get(inv.id, (None, set()))
        fit = invoice_fit(
            receipt_customer_id=receipt.customer_id, receipt_branch_id=rb_id, receipt_mapped=mapped,
            receipt_customer_name=cnames.get(receipt.customer_id), receipt_branch_name=rb_name,
            inv_customer_id=cid, inv_customer_ids=cset, inv_customer_name=cnames.get(cid),
            inv_branch_id=b["branch_id"], inv_branch_name=b["branch_name"],
            is_proforma=getattr(inv.kind, "value", inv.kind) == InvoiceKind.PROFORMA.value,
            balance=inv.balance_amount)
        rows.append({**b, "customer_id": cid, "customer_name": cnames.get(cid),
                     "proforma_number": getattr(inv, "proforma_number", None),
                     "likely_match": likely_match(b["balance_amount"], receipt.unallocated_amount), **fit})
    order = {"here": 0, "other_branch": 1, "other_customer": 2, "paid": 3, "proforma": 4}
    rows.sort(key=lambda r: order.get(r["fit"], 9))
    return envelope(data=rows, message=f"{len(rows)} invoice(s) match",
                    meta={"term": term, "limit": FIND_LIMIT, "receipt_mapped": mapped})


@router.get("")
def list_receipts(customer_id: int | None = None, project_id: int | None = None,
                  branch_id: int | None = None,
                  received_from: date | None = None, received_to: date | None = None,
                  payment_mode: str | None = None,
                  p: PageParams = Depends(page_params),
                  db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    stmt = (select(CustomerReceipt).join(Customer, Customer.id == CustomerReceipt.customer_id)
            .join(CustomerBranch, CustomerBranch.id == CustomerReceipt.branch_id, isouter=True))
    if customer_id is not None:
        stmt = stmt.where(CustomerReceipt.customer_id == customer_id)
    if branch_id is not None:
        # 23 Oct 2026: 0 = receipts with no branch named yet.
        stmt = stmt.where(CustomerReceipt.branch_id.is_(None) if branch_id == 0
                          else CustomerReceipt.branch_id == branch_id)
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
        # 2 Nov 2026 (screenshot): an INVOICE number found nothing here — the
        # search never looked at the invoices a receipt pays. It now matches the
        # tax / PI number of any invoice the receipt's cash or TDS went to, and
        # the customer's legal name.
        inv_ids = select(Invoice.id).where(or_(Invoice.invoice_number.ilike(like),
                                               Invoice.proforma_number.ilike(like)))
        paid_by = (select(InvoicePayment.receipt_id)
                   .where(InvoicePayment.invoice_id.in_(inv_ids), InvoicePayment.receipt_id.isnot(None)))
        tds_by = (select(TdsRecord.receipt_id)
                  .where(TdsRecord.invoice_id.in_(inv_ids), TdsRecord.receipt_id.isnot(None)))
        stmt = stmt.where(or_(CustomerReceipt.reference_number.ilike(like),
                              CustomerReceipt.notes.ilike(like), Customer.name.ilike(like),
                              Customer.legal_entity_name.ilike(like),
                              CustomerBranch.branch_name.ilike(like),
                              CustomerReceipt.id.in_(paid_by), CustomerReceipt.id.in_(tds_by)))
    sort_col = {"received_date": CustomerReceipt.received_date, "amount": CustomerReceipt.amount,
                "customer_name": Customer.name, "branch_name": CustomerBranch.branch_name,
                }.get(p.sort_by or "", CustomerReceipt.received_date)
    stmt = stmt.order_by(sort_col.asc() if p.sort_dir == "asc" else sort_col.desc(),
                         CustomerReceipt.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    names = {c.id: c.name for c in db.execute(
        select(Customer).where(Customer.id.in_({r.customer_id for r in items}))
    ).scalars().all()} if items else {}
    bnames = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name).where(
        CustomerBranch.id.in_({r.branch_id for r in items if r.branch_id})
    )).all()) if any(r.branch_id for r in items) else {}
    data = [_serialize(db, r, names.get(r.customer_id), bnames.get(r.branch_id)) for r in items]
    total = db.execute(select(func.coalesce(func.sum(CustomerReceipt.amount), 0))
                       .where(CustomerReceipt.customer_id == customer_id) if customer_id
                       else select(func.coalesce(func.sum(CustomerReceipt.amount), 0))).scalar()
    meta = {**(meta or {}), "total_received": float(total or 0)}
    if p.search:
        # The invoices the term names, with what was received on each — so a
        # search for an invoice that has NO received amount yet says so (and
        # offers the invoice) instead of reading "no amounts received".
        meta["invoice_hits"] = invoice_hits(db, p.search, customer_id=customer_id)
    return envelope(data=data, message="Customer receipts", meta=meta)


INVOICE_HITS_LIMIT = 5


def invoice_hits(db: Session, term: str, customer_id: int | None = None) -> list[dict]:
    """Up to `INVOICE_HITS_LIMIT` invoices whose tax / PI number contains `term`:
    number, customer, totals, what came in (cash + TDS), the stage, and the
    receipts that paid it. Never raises — a failure is an empty list."""
    from services import billing_rules
    like = f"%{term}%"
    try:
        with db.begin_nested():
            stmt = (select(Invoice).where(or_(Invoice.invoice_number.ilike(like),
                                              Invoice.proforma_number.ilike(like)))
                    .order_by(Invoice.id.desc()).limit(INVOICE_HITS_LIMIT))
            if customer_id is not None:
                stmt = stmt.where(customer_invoice_clause(customer_id))
            invs = list(db.execute(stmt).scalars())
            if not invs:
                return []
            ids = [i.id for i in invs]
            receipts: dict[int, set[int]] = {}
            for inv_id, rid in db.execute(select(InvoicePayment.invoice_id, InvoicePayment.receipt_id)
                                          .where(InvoicePayment.invoice_id.in_(ids),
                                                 InvoicePayment.receipt_id.isnot(None))).all():
                receipts.setdefault(inv_id, set()).add(rid)
            pids = {i.project_id for i in invs if i.project_id}
            proj_cust = dict(db.execute(select(Project.id, Project.customer_id)
                                        .where(Project.id.in_(pids))).all()) if pids else {}
            cids = {c for c in ((i.po.customer_id if i.po is not None and i.po.customer_id
                                 else proj_cust.get(i.project_id)) for i in invs) if c}
            names = dict(db.execute(select(Customer.id, func.coalesce(Customer.legal_entity_name, Customer.name))
                                    .where(Customer.id.in_(cids))).all()) if cids else {}
            labels = dict(billing_rules.INVOICE_STAGES)
            out = []
            for i in invs:
                cid = i.po.customer_id if i.po is not None and i.po.customer_id else proj_cust.get(i.project_id)
                stage = billing_rules.stage_of_invoice(i)
                out.append({
                    "id": i.id, "kind": i.kind,
                    "invoice_number": i.invoice_number, "proforma_number": i.proforma_number,
                    "customer_id": cid, "customer_name": names.get(cid),
                    "invoice_date": i.invoice_date.isoformat() if i.invoice_date else None,
                    "grand_total": float(i.grand_total or 0),
                    "received": float((i.paid_amount or 0) + (getattr(i, "tds_deducted", None) or 0)),
                    "balance": float(i.balance_amount or 0),
                    "stage": stage, "stage_label": labels.get(stage, stage),
                    "receipt_ids": sorted(receipts.get(i.id, set())),
                    "payment_block": payment_block(i) if i.kind != InvoiceKind.PROFORMA.value else None,
                })
            return out
    except Exception:  # noqa: BLE001 — a hint, never a failure of the list
        import logging
        logging.getLogger(__name__).warning("receipt search: invoice hits failed", exc_info=True)
        return []


@router.get("/{receipt_id}")
def get_receipt(receipt_id: int, db: Session = Depends(get_crm_db), user: CurrentUser = Depends(RC_READ)):
    """One receipt — the work desk's "Receipts to map" items open it straight
    in the workbench (`received-amounts?receipt=<id>`, 23 Oct 2026). Declared
    after every literal GET of this router."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return envelope(_receipt_payload(db, receipt))


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


def plan_mapping(available: Decimal, items: list[tuple[Invoice, Decimal | None]]
                 ) -> tuple[list[tuple[Invoice, Decimal]], Decimal]:
    """How much cash each invoice gets when a receipt is mapped (PURE, tested).
    Explicit amounts first (each ≤ the invoice's balance), then the open ones
    oldest-first from what is left. Raises ValueError with the reason."""
    left = _d(available)
    out: dict[int, Decimal] = {}
    explicit = [(inv, _d(a)) for inv, a in items if a is not None]
    for inv, amt in explicit:
        num = inv.invoice_number or f"#{inv.id}"
        if amt > _d(inv.balance_amount):
            raise ValueError(f"{float(amt):,.2f} on {num} is more than its balance {float(_d(inv.balance_amount)):,.2f}")
        if amt > left:
            raise ValueError(f"Only {float(left):,.2f} is left on this receipt to map")
        out[inv.id] = amt
        left = (left - amt).quantize(TWO)
    auto = [inv for inv, a in items if a is None]
    plan, left = allocate(left, auto)
    for inv, take in plan:
        out[inv.id] = take
    by_id = {inv.id: inv for inv, _ in items}
    ordered = sorted(out.items(), key=lambda kv: (by_id[kv[0]].invoice_date or date.min, kv[0]))
    return [(by_id[i], a) for i, a in ordered if a > 0], left


def _receipt_payload(db: Session, receipt: CustomerReceipt) -> dict:
    customer = db.get(Customer, receipt.customer_id)
    branch = db.get(CustomerBranch, receipt.branch_id) if receipt.branch_id else None
    return _serialize(db, receipt, getattr(customer, "name", None), getattr(branch, "branch_name", None))


def _infer_branch(db: Session, customer_id: int, invoices) -> CustomerBranch | None:
    """The branch a receipt with no branch chosen belongs to: the single branch
    of its invoices, else the customer's only branch, else None."""
    if invoices:
        from services.invoice_facets import invoice_facets
        facets = invoice_facets(db, invoices)
        ids = {(facets.get(i.id) or {}).get("branch_id") for i in invoices}
        if len(ids) == 1 and None not in ids:
            return db.get(CustomerBranch, next(iter(ids)))
    only = db.execute(select(CustomerBranch).where(CustomerBranch.customer_id == customer_id)
                      .limit(2)).scalars().all()
    return only[0] if len(only) == 1 else None


@router.post("")
def create_receipt(body: ReceiptIn, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    customer = db.get(Customer, body.customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if body.payment_mode and body.payment_mode not in PAYMENT_MODES:
        raise HTTPException(status_code=400, detail=f"Payment mode must be one of: {', '.join(PAYMENT_MODES)}")
    branch = None
    if body.branch_id is not None:
        branch = db.get(CustomerBranch, body.branch_id)
        if branch is None or branch.customer_id != body.customer_id:
            raise HTTPException(status_code=400, detail=f"That branch does not belong to {customer.name}")
    invoices: list[Invoice] = []
    wanted_ids = [it.invoice_id for it in body.items] if body.items else list(body.invoice_ids)
    if body.items and len(set(wanted_ids)) != len(wanted_ids):
        raise HTTPException(status_code=400, detail="An invoice is listed twice")
    if wanted_ids:
        invoices = db.execute(
            select(Invoice).where(Invoice.id.in_(set(wanted_ids)), customer_invoice_clause(body.customer_id))
        ).scalars().all()
        missing = set(wanted_ids) - {i.id for i in invoices}
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"Invoice(s) {sorted(missing)} do not belong to {customer.name}")
        # One payment = one branch (16 Oct 2026, user decision).
        if branch is not None:
            from services.invoice_facets import invoice_facets
            facets = invoice_facets(db, invoices)
            other = [i.invoice_number or f"#{i.id}" for i in invoices
                     if (facets.get(i.id) or {}).get("branch_id") not in (None, branch.id)]
            if other:
                raise HTTPException(status_code=400,
                                    detail=f"Not invoices of {branch.branch_name}: {', '.join(other)}")
        # 20 Nov 2026 (Finance ask): every issued tax invoice takes money here —
        # one still waiting for the customer's approval / IRN included. The
        # approval note (`payment_block(i)`) stays on the picker as information.
    # 23 Oct 2026 (user ask): every receipt says which branch paid. Not chosen →
    # the one branch of the ticked invoices, else the customer's only branch,
    # else ask. A customer with no branches on record is the one exception.
    if branch is None:
        branch = _infer_branch(db, body.customer_id, invoices)
        unbranched_invoices = False
        if invoices and branch is None:
            from services.invoice_facets import invoice_facets
            f = invoice_facets(db, invoices)
            # Paying invoices that themselves have no branch on record is allowed.
            unbranched_invoices = all((f.get(i.id) or {}).get("branch_id") is None for i in invoices)
        if branch is None and not unbranched_invoices and db.execute(select(CustomerBranch.id).where(
                CustomerBranch.customer_id == body.customer_id).limit(1)).first():
            raise HTTPException(status_code=400,
                                detail=f"Choose the {customer.name} branch that paid this amount.")
    # TDS lines must name ticked invoices; one per invoice.
    by_id = {i.id: i for i in invoices}
    tds_items = list(body.tds or [])
    seen: set[int] = set()
    for t in tds_items:
        if t.invoice_id not in by_id:
            raise HTTPException(status_code=400, detail="TDS can only be recorded on an invoice you ticked")
        if t.invoice_id in seen:
            raise HTTPException(status_code=400, detail="TDS is listed twice for one invoice")
        seen.add(t.invoice_id)
    prior = _d(body.prior_period_amount)
    if prior >= _d(body.amount):
        raise HTTPException(status_code=400,
                            detail="The earlier-year part must be less than the amount received — "
                                   "a credit only for earlier years need not be recorded here")
    receipt = CustomerReceipt(
        customer_id=body.customer_id, branch_id=getattr(branch, "id", None),
        prior_period_amount=prior,
        prior_period_note=((body.prior_period_note or "").strip() or None) if prior > 0 else None,
        received_date=body.received_date, amount=_d(body.amount),
        payment_mode=body.payment_mode or None,
        reference_number=(body.reference_number or "").strip() or None,
        notes=(body.notes or "").strip() or None,
        unallocated_amount=Decimal("0"), created_by=user.id,
        proof_url=clean_proof_url(body.proof_url),
        proof_name=(body.proof_name or "").strip()[:255] or None,
    )
    db.add(receipt)
    db.flush()
    # TDS first: it settles part of each invoice, so the cash is spread over
    # what the customer actually owes in money (balance − TDS).
    tds_total = Decimal("0")
    for t in tds_items:
        inv = by_id[t.invoice_id]
        figures = tds_svc.work_out(inv, rate=t.tds_rate, base=t.tds_base, amount=t.tds_amount)
        tds_svc.book(db, inv, figures, section=t.section, receipt_id=receipt.id, user_id=user.id)
        tds_total += _d(figures["amount"])
    receipt.tds_total = tds_total
    if body.items:
        try:
            plan, left = plan_mapping(_d(body.amount) - prior,
                                      [(by_id[it.invoice_id], it.amount) for it in body.items])
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        plan, left = allocate(_d(body.amount) - prior, invoices)
    receipt.unallocated_amount = left
    for inv, take in plan:
        db.add(InvoicePayment(
            invoice_id=inv.id, payment_date=body.received_date, amount=take,
            payment_mode=body.payment_mode or None,
            reference_number=(body.reference_number or "").strip() or None,
            notes=f"From customer receipt #{receipt.id}", receipt_id=receipt.id,
            attachment_url=receipt.proof_url, created_by=user.id,
        ))
        tax.apply_invoice_payment(inv, take)
    db.commit()
    db.refresh(receipt)
    msg = f"Received {float(receipt.amount):,.2f} recorded — {len(plan)} invoice(s) settled"
    if prior > 0:
        msg += f", {float(prior):,.2f} set aside for an earlier financial year"
    if tds_total > 0:
        msg += f", TDS {float(tds_total):,.2f} pending from client"
    if left > 0:
        msg += f", {float(left):,.2f} left unallocated"
    return envelope(data=_serialize(db, receipt, customer.name, getattr(branch, "branch_name", None)),
                    message=msg)


@router.post("/proof")
def upload_proof(file: UploadFile = File(...), user: CurrentUser = Depends(RC_WRITE)):
    """Store the proof of a credit (screenshot / PDF) before the receipt is
    saved; the returned url goes in the receipt's `proof_url`."""
    name = (file.filename or "proof").strip()
    if not name.lower().endswith(PROOF_EXTENSIONS):
        raise HTTPException(status_code=400,
                            detail="Upload an image (screenshot), PDF, Excel or Word file as the proof")
    size = getattr(file, "size", None)
    if size is not None and size > PROOF_MAX_BYTES:
        raise HTTPException(status_code=400, detail="The proof file is larger than 15 MB")
    url = save_upload(file, "receipt_proofs")
    return envelope({"url": url, "name": name[:255]}, message="Proof uploaded")


@router.put("/{receipt_id}/proof")
def set_proof(receipt_id: int, body: ProofIn, db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(RC_WRITE)):
    """Attach, replace or clear (null) the proof of a saved receipt — the
    invoice payments it made follow."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt.proof_url = clean_proof_url(body.proof_url)
    receipt.proof_name = ((body.proof_name or "").strip()[:255] or None) if receipt.proof_url else None
    for pay in list(receipt.payments or []):
        pay.attachment_url = receipt.proof_url
    db.commit()
    db.refresh(receipt)
    customer = db.get(Customer, receipt.customer_id)
    branch = db.get(CustomerBranch, receipt.branch_id) if receipt.branch_id else None
    return envelope(_serialize(db, receipt, getattr(customer, "name", None), getattr(branch, "branch_name", None)),
                    message="Proof saved" if receipt.proof_url else "Proof removed")


class AssignBranchIn(BaseModel):
    receipt_ids: list[int] = Field(min_length=1, max_length=500)
    branch_id: int


@router.post("/assign-branch")
def assign_branch(body: AssignBranchIn, db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(RC_WRITE)):
    """23 Oct 2026 (user ask): name the branch on several receipts at once —
    the "<customer> · No branch on record" groups. A receipt already mapped to
    invoices of another branch is skipped and named, never changed."""
    from services.invoice_facets import invoice_facets

    branch = db.get(CustomerBranch, body.branch_id)
    if branch is None:
        raise HTTPException(status_code=404, detail="Branch not found")
    receipts = db.execute(select(CustomerReceipt).where(
        CustomerReceipt.id.in_(set(body.receipt_ids)))).scalars().all()
    done, skipped = 0, []
    for rc in receipts:
        if rc.customer_id != branch.customer_id:
            skipped.append(f"#{rc.id} (another customer)")
            continue
        invs = db.execute(select(Invoice).where(
            Invoice.id.in_({p.invoice_id for p in rc.payments or []}))).scalars().all()
        facets = invoice_facets(db, invs) if invs else {}
        other = [i.invoice_number or f"#{i.id}" for i in invs
                 if (facets.get(i.id) or {}).get("branch_id") not in (None, branch.id)]
        if other:
            skipped.append(f"#{rc.id} (mapped to {', '.join(other)})")
            continue
        rc.branch_id = branch.id
        done += 1
    db.commit()
    msg = f"{done} receipt{'s' if done != 1 else ''} set to {branch.branch_name}"
    if skipped:
        msg += f" · skipped {len(skipped)}: {'; '.join(skipped[:5])}"
    return envelope({"updated": done, "skipped": skipped}, message=msg)


@router.put("/{receipt_id}")
def update_receipt(receipt_id: int, body: ReceiptUpdate, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    """Edit a saved receipt. The amount may not drop below what is already
    applied to invoices; the invoice payments follow date / mode / reference."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    sent = body.model_fields_set
    payments = list(receipt.payments or [])
    applied = sum((_d(p.amount) for p in payments), Decimal("0"))
    if "payment_mode" in sent and body.payment_mode and body.payment_mode not in PAYMENT_MODES:
        raise HTTPException(status_code=400, detail=f"Payment mode must be one of: {', '.join(PAYMENT_MODES)}")
    new_amt = _d(body.amount) if ("amount" in sent and body.amount is not None) else _d(receipt.amount)
    new_prior = (_d(body.prior_period_amount) if "prior_period_amount" in sent
                 else _d(getattr(receipt, "prior_period_amount", 0)))
    if new_amt < applied:
        raise HTTPException(status_code=400,
                            detail=f"{float(applied):,.2f} of this receipt is already applied to invoices — "
                                   f"unmap an invoice first, or enter at least that amount")
    if new_prior > new_amt - applied:
        raise HTTPException(status_code=400,
                            detail=f"Only {float(new_amt - applied):,.2f} is not mapped to invoices — the "
                                   f"earlier-year part cannot be more than that (unmap an invoice first)")
    if "amount" in sent or "prior_period_amount" in sent:
        receipt.amount = new_amt
        receipt.prior_period_amount = new_prior
        receipt.unallocated_amount = (new_amt - applied - new_prior).quantize(TWO)
    if "prior_period_note" in sent or "prior_period_amount" in sent:
        note = (body.prior_period_note if "prior_period_note" in sent else receipt.prior_period_note) or ""
        receipt.prior_period_note = note.strip()[:1000] or None if new_prior > 0 else None
    if "received_date" in sent and body.received_date is not None:
        receipt.received_date = body.received_date
    if "payment_mode" in sent:
        receipt.payment_mode = body.payment_mode or None
    if "reference_number" in sent:
        receipt.reference_number = (body.reference_number or "").strip() or None
    if "notes" in sent:
        receipt.notes = (body.notes or "").strip() or None
    if "branch_id" in sent and body.branch_id != receipt.branch_id:
        if body.branch_id is not None:
            branch = db.get(CustomerBranch, body.branch_id)
            if branch is None or branch.customer_id != receipt.customer_id:
                raise HTTPException(status_code=400, detail="That branch does not belong to this customer")
            if payments:
                from services.invoice_facets import invoice_facets
                invs = db.execute(select(Invoice).where(Invoice.id.in_({p.invoice_id for p in payments}))).scalars().all()
                facets = invoice_facets(db, invs)
                other = [i.invoice_number or f"#{i.id}" for i in invs
                         if (facets.get(i.id) or {}).get("branch_id") not in (None, branch.id)]
                if other:
                    raise HTTPException(status_code=400,
                                        detail=f"Mapped invoices of another branch: {', '.join(other)} — unmap them first")
        receipt.branch_id = body.branch_id
    for pay in payments:
        pay.payment_date = receipt.received_date
        pay.payment_mode = receipt.payment_mode
        pay.reference_number = receipt.reference_number
    db.commit()
    db.refresh(receipt)
    return envelope(_receipt_payload(db, receipt), message="Receipt updated")


@router.post("/{receipt_id}/allocate")
def allocate_receipt(receipt_id: int, body: AllocateIn, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(RC_WRITE)):
    """Map the receipt's unallocated amount onto invoices (and record the TDS
    the customer withheld on them). Same rules as Add received amount."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if not body.items:
        raise HTTPException(status_code=400, detail="Tick at least one invoice to map")
    ids = [it.invoice_id for it in body.items]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="An invoice is listed twice")
    moved_note = ""
    other_branches = bool(body.other_branches)
    if body.move_receipt:
        moved_note, multi = _move_receipt_to_invoices(db, receipt, ids, user)
        other_branches = other_branches or multi
    customer = db.get(Customer, receipt.customer_id)
    invoices = db.execute(
        select(Invoice).where(Invoice.id.in_(set(ids)), customer_invoice_clause(receipt.customer_id))
    ).scalars().all()
    missing = set(ids) - {i.id for i in invoices}
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"Invoice(s) {sorted(missing)} do not belong to {getattr(customer, 'name', 'this customer')}")
    proforma = [i.invoice_number or f"#{i.id}" for i in invoices
                if getattr(i.kind, "value", i.kind) == InvoiceKind.PROFORMA.value]
    if proforma:
        raise HTTPException(status_code=400, detail=f"A Proforma takes no money: {', '.join(proforma)}")
    # One receipt = one branch.
    from services.invoice_facets import invoice_facets
    facets = invoice_facets(db, invoices)
    inv_branches = {(facets.get(i.id) or {}).get("branch_id") for i in invoices}
    branch_id = receipt.branch_id
    if branch_id is None:
        if body.branch_id is not None:
            b = db.get(CustomerBranch, body.branch_id)
            if b is None or b.customer_id != receipt.customer_id:
                raise HTTPException(status_code=400, detail="That branch does not belong to this customer")
            branch_id = b.id
        elif len(inv_branches) == 1 and None not in inv_branches:
            branch_id = next(iter(inv_branches))
    if branch_id is not None:
        bname = db.get(CustomerBranch, branch_id).branch_name
        # An invoice with NO branch on record may take any branch's money
        # (24 Oct 2026 — the assign-branch rule); another branch's may not.
        other = [i.invoice_number or f"#{i.id}" for i in invoices
                 if (facets.get(i.id) or {}).get("branch_id") not in (None, branch_id)]
        if other and not other_branches:
            raise HTTPException(
                status_code=400,
                detail=f"Not invoices of {bname}: {', '.join(other)} — tick “map anyway” to put this "
                       f"receipt's money on another branch's invoice")
        if other:
            moved_note = (moved_note + " " if moved_note else "") + \
                f"Mapped across branches ({', '.join(other)}) — the receipt stays on {bname}."
    receipt.branch_id = branch_id
    by_id = {i.id: i for i in invoices}
    seen: set[int] = set()
    for t in body.tds or []:
        if t.invoice_id not in by_id:
            raise HTTPException(status_code=400, detail="TDS can only be recorded on an invoice you ticked")
        if t.invoice_id in seen:
            raise HTTPException(status_code=400, detail="TDS is listed twice for one invoice")
        seen.add(t.invoice_id)
    tds_added = Decimal("0")
    for t in body.tds or []:
        inv = by_id[t.invoice_id]
        figures = tds_svc.work_out(inv, rate=t.tds_rate, base=t.tds_base, amount=t.tds_amount)
        tds_svc.book(db, inv, figures, section=t.section, receipt_id=receipt.id, user_id=user.id)
        tds_added += _d(figures["amount"])
    try:
        plan, left = plan_mapping(_d(receipt.unallocated_amount),
                                  [(by_id[it.invoice_id], it.amount) for it in body.items])
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not plan and tds_added <= 0:
        db.rollback()
        raise HTTPException(status_code=400,
                            detail="Nothing to map — the receipt has no unallocated amount, or the ticked invoices are already paid")
    for inv, take in plan:
        db.add(InvoicePayment(
            invoice_id=inv.id, payment_date=receipt.received_date, amount=take,
            payment_mode=receipt.payment_mode, reference_number=receipt.reference_number,
            notes=f"From customer receipt #{receipt.id}", receipt_id=receipt.id,
            attachment_url=receipt.proof_url, created_by=user.id,
        ))
        tax.apply_invoice_payment(inv, take)
    receipt.unallocated_amount = left
    receipt.tds_total = _d(getattr(receipt, "tds_total", 0)) + tds_added
    db.commit()
    db.refresh(receipt)
    applied = sum((a for _, a in plan), Decimal("0"))
    msg = f"Mapped {float(applied):,.2f} to {len(plan)} invoice(s)"
    if tds_added > 0:
        msg += f", TDS {float(tds_added):,.2f} pending from client"
    msg += f" — {float(left):,.2f} still on account" if left > 0 else " — nothing left on account"
    if moved_note:
        msg = f"{moved_note} {msg}"
    return envelope(_receipt_payload(db, receipt), message=msg)


def _move_receipt_to_invoices(db: Session, receipt: CustomerReceipt, ids: list[int], user) -> tuple[str, bool]:
    """20 Nov 2026: a receipt recorded on the wrong customer / branch, paying
    nothing yet, moves to the ticked invoices' customer (one customer only) and
    their branch (the single branch; several branches of the receipt's own
    customer keep the receipt's branch and are mapped across branches).
    Returns (note for the reply, invoices span several branches)."""
    from services import customer_activity
    from services.invoice_facets import invoice_facets

    if _receipt_mapped(db, receipt):
        raise HTTPException(status_code=409,
                            detail="This receipt already pays invoices — it cannot move to another customer "
                                   "or branch. Unmap them first, or map across branches.")
    invs = db.execute(select(Invoice).where(Invoice.id.in_(set(ids)))).scalars().all()
    custs = _customers_of(db, invs)
    targets = {c for c, _ in custs.values()}
    if len(targets) != 1 or None in targets:
        raise HTTPException(status_code=400,
                            detail="Tick invoices of ONE customer — a received amount pays one customer")
    target = next(iter(targets))
    facets = invoice_facets(db, invs)
    bids = {(facets.get(i.id) or {}).get("branch_id") for i in invs} - {None}
    multi = len(bids) > 1
    if len(bids) == 1:
        new_branch = next(iter(bids))
    elif target == receipt.customer_id:
        new_branch = receipt.branch_id
    else:
        new_branch = next(iter(sorted(bids))) if bids else None
    old_cid, old_bid = receipt.customer_id, receipt.branch_id
    if (target, new_branch) == (old_cid, old_bid):
        return "", multi
    names = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_({old_cid, target}))).all())
    bnames = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name).where(
        CustomerBranch.id.in_({b for b in (old_bid, new_branch) if b}))).all()) if (old_bid or new_branch) else {}

    def where(cid, bid):
        return f"{names.get(cid, f'#{cid}')}{f' · {bnames[bid]}' if bid in bnames else ''}"

    receipt.customer_id, receipt.branch_id = target, new_branch
    text = f"Received amount #{receipt.id} moved from {where(old_cid, old_bid)} to {where(target, new_branch)} " \
           f"to map {len(invs)} invoice(s) billed there"
    for cid in {old_cid, target}:
        customer_activity.record(db, cid, "RECEIPT_MOVED", text, entity="receipt", entity_id=receipt.id,
                                 branch_id=new_branch if cid == target else old_bid,
                                 subject=f"Received amount #{receipt.id}", user=user)
    return f"Receipt moved to {where(target, new_branch)}.", multi


@router.delete("/{receipt_id}/allocations/{invoice_id}")
def unmap_invoice(receipt_id: int, invoice_id: int, db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(RC_WRITE)):
    """Take one invoice off the receipt: its cash goes back on account and the
    TDS booked with this receipt on it is removed.

    TDS already marked received (Form 16A) blocks Finance with the place to
    undo it; Admin / CEO undo it in the same step (20 Nov 2026, Admin report:
    a partly paid invoice could not be unmapped from Admin)."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    pays = [p for p in (receipt.payments or []) if p.invoice_id == invoice_id]
    tds = db.execute(select(TdsRecord).where(TdsRecord.receipt_id == receipt.id,
                                             TdsRecord.invoice_id == invoice_id)).scalars().first()
    if not pays and tds is None:
        raise HTTPException(status_code=404, detail="That invoice is not mapped to this receipt")
    inv = db.get(Invoice, invoice_id)
    tds_back = Decimal("0")
    received_back = Decimal("0")
    num = getattr(inv, "invoice_number", None) or f"#{invoice_id}"
    if tds is not None:
        tds_back = _d(tds.tds_amount)
        received_back = tds_svc.unbook(db, tds, force=user.is_admin, user=user)
    cash = Decimal("0")
    for pay in pays:
        cash += _d(pay.amount)
        if inv is not None:
            tax.apply_invoice_payment(inv, -_d(pay.amount))
        db.delete(pay)
    receipt.unallocated_amount = (_d(receipt.unallocated_amount) + cash).quantize(TWO)
    receipt.tds_total = max(_d(getattr(receipt, "tds_total", 0)) - tds_back, Decimal("0"))
    if received_back > 0:
        _log_admin_override(db, inv, user,
                            f"{num} unmapped from received amount #{receipt.id} by Admin — the TDS "
                            f"received of {float(received_back):,.2f} (Form 16A) was undone with it")
    db.commit()
    db.refresh(receipt)
    msg = f"{num} unmapped — {float(cash):,.2f} back on account"
    if tds_back > 0:
        msg += f" (TDS {float(tds_back):,.2f} removed"
        msg += f", incl. {float(received_back):,.2f} marked received)" if received_back > 0 else ")"
    return envelope(_receipt_payload(db, receipt), message=msg)


def _log_admin_override(db, invoice, user, text: str) -> None:
    """Keep an Admin step that undid received TDS on the customer's log."""
    if invoice is None:
        return
    try:
        from services import customer_activity
        from services.invoice_payments import invoice_customer_ids

        cid, _ = invoice_customer_ids(db, invoice)
        if cid is not None:
            customer_activity.record(db, cid, "ADMIN_TDS_OVERRIDE", text, entity="invoice",
                                     entity_id=invoice.id,
                                     subject=invoice.invoice_number or f"#{invoice.id}", user=user)
    except Exception:  # noqa: BLE001 — a log line never blocks the step
        pass


@router.delete("/{receipt_id}")
def delete_receipt(receipt_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    """Reverse the receipt: its payments come off the invoices, then the row goes."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    booked = db.execute(select(TdsRecord).where(TdsRecord.receipt_id == receipt.id)).scalars().all()
    received = [t for t in booked if tds_svc.is_received(t)]
    if received and not user.is_admin:
        raise HTTPException(status_code=409, detail=tds_svc.received_block(received[0]))
    for t in booked:
        inv = t.invoice
        back = tds_svc.unbook(db, t, force=user.is_admin, user=user)
        if back > 0:
            _log_admin_override(db, inv, user,
                                f"Received amount #{receipt.id} deleted by Admin — the TDS received of "
                                f"{float(back):,.2f} on {getattr(inv, 'invoice_number', '')} was undone with it")
    for pay in list(receipt.payments or []):
        inv = db.get(Invoice, pay.invoice_id)
        if inv is not None:
            tax.apply_invoice_payment(inv, -_d(pay.amount))
        db.delete(pay)
    db.delete(receipt)
    db.commit()
    msg = "Receipt removed and invoice balances restored"
    if booked:
        msg += f" (TDS on {len(booked)} invoice(s) removed too)"
    return envelope(data={"id": receipt_id}, message=msg)
