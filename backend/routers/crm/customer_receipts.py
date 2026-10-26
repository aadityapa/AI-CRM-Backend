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
    #: Proof of the credit (19 Oct 2026) — the url `POST /proof` returned.
    proof_url: str | None = Field(default=None, max_length=512)
    proof_name: str | None = Field(default=None, max_length=255)


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


ReceiptIn.model_rebuild()


class ReceiptUpdate(BaseModel):
    """Edit a saved receipt (22 Oct 2026). Only the fields sent change."""
    received_date: date | None = None
    amount: Decimal | None = Field(default=None, gt=0)
    payment_mode: str | None = Field(default=None, max_length=64)
    reference_number: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=4000)
    #: The paying branch; null clears it (only while nothing mapped belongs elsewhere).
    branch_id: int | None = None


class AllocateItemIn(BaseModel):
    invoice_id: int
    #: Cash to put on this invoice; None = as much as is left, oldest first.
    amount: Decimal | None = Field(default=None, gt=0)


class AllocateIn(BaseModel):
    """Map a receipt's unallocated amount onto invoices (22 Oct 2026)."""
    items: list[AllocateItemIn] = Field(default_factory=list, max_length=100)
    tds: list[ReceiptTdsIn] = Field(default_factory=list, max_length=100)
    #: For a receipt with no branch yet: the branch these invoices belong to.
    branch_id: int | None = None


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
        "allocated_amount": float(sum((_d(p.amount) for p in payments), Decimal("0"))),
        "tds_total": float(getattr(r, "tds_total", 0) or 0),
        "tds_pending": float(sum((_d(t.tds_balance) for t in booked), Decimal("0"))),
        "proof_url": getattr(r, "proof_url", None),
        "proof_name": getattr(r, "proof_name", None),
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
        stmt = stmt.where(or_(CustomerReceipt.reference_number.ilike(like),
                              CustomerReceipt.notes.ilike(like), Customer.name.ilike(like),
                              CustomerBranch.branch_name.ilike(like)))
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
    return envelope(data=data, message="Customer receipts", meta=meta)


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
    if body.invoice_ids:
        invoices = db.execute(
            select(Invoice).where(Invoice.id.in_(set(body.invoice_ids)), customer_invoice_clause(body.customer_id))
        ).scalars().all()
        missing = set(body.invoice_ids) - {i.id for i in invoices}
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
        # Money is recorded against the e-invoice (8 Oct 2026): customer approval
        # + IRN first — the same rule as Record Payment on the invoice.
        waiting = [i.invoice_number or f"#{i.id}" for i in invoices if payment_block(i)]
        if waiting:
            raise HTTPException(status_code=409,
                                detail=f"Not open for payment yet (customer approval / IRN pending): "
                                       f"{', '.join(waiting)}")
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
    receipt = CustomerReceipt(
        customer_id=body.customer_id, branch_id=getattr(branch, "id", None),
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
    plan, left = allocate(body.amount, invoices)
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
    if "amount" in sent and body.amount is not None:
        new_amt = _d(body.amount)
        if new_amt < applied:
            raise HTTPException(status_code=400,
                                detail=f"{float(applied):,.2f} of this receipt is already applied to invoices — "
                                       f"unmap an invoice first, or enter at least that amount")
        receipt.amount = new_amt
        receipt.unallocated_amount = (new_amt - applied).quantize(TWO)
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
    waiting = [i.invoice_number or f"#{i.id}" for i in invoices if payment_block(i)]
    if waiting:
        raise HTTPException(status_code=409,
                            detail=f"Not open for payment yet (customer approval / IRN pending): {', '.join(waiting)}")
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
        if other:
            raise HTTPException(status_code=400, detail=f"Not invoices of {bname}: {', '.join(other)}")
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
    return envelope(_receipt_payload(db, receipt), message=msg)


@router.delete("/{receipt_id}/allocations/{invoice_id}")
def unmap_invoice(receipt_id: int, invoice_id: int, db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(RC_WRITE)):
    """Take one invoice off the receipt: its cash goes back on account and the
    TDS booked with this receipt on it is removed (409 once received)."""
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
    if tds is not None:
        tds_back = _d(tds.tds_amount)
        tds_svc.unbook(db, tds)
    cash = Decimal("0")
    for pay in pays:
        cash += _d(pay.amount)
        if inv is not None:
            tax.apply_invoice_payment(inv, -_d(pay.amount))
        db.delete(pay)
    receipt.unallocated_amount = (_d(receipt.unallocated_amount) + cash).quantize(TWO)
    receipt.tds_total = max(_d(getattr(receipt, "tds_total", 0)) - tds_back, Decimal("0"))
    db.commit()
    db.refresh(receipt)
    num = getattr(inv, "invoice_number", None) or f"#{invoice_id}"
    msg = f"{num} unmapped — {float(cash):,.2f} back on account"
    if tds_back > 0:
        msg += f" (TDS {float(tds_back):,.2f} removed)"
    return envelope(_receipt_payload(db, receipt), message=msg)


@router.delete("/{receipt_id}")
def delete_receipt(receipt_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(RC_WRITE)):
    """Reverse the receipt: its payments come off the invoices, then the row goes."""
    receipt = db.get(CustomerReceipt, receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    booked = db.execute(select(TdsRecord).where(TdsRecord.receipt_id == receipt.id)).scalars().all()
    received = [t.invoice.invoice_number if t.invoice else f"#{t.invoice_id}" for t in booked
                if _d(t.tds_paid) > 0 or list(t.payments or [])]
    if received:
        raise HTTPException(status_code=409,
                            detail=f"TDS on {', '.join(received)} was already marked received — undo that first")
    for t in booked:
        tds_svc.unbook(db, t)
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
