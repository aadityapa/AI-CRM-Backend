"""Customer-wise financial reports: complete ledger + payment receivables.

GET /api/finance/reports/customer-ledger?customer_id=&date_from=&date_to=
    Chronological debit/credit statement for one customer:
    - DEBIT : invoices raised (grand_total, dated invoice_date)
    - CREDIT: payments received (payment_date), TDS deducted (tds_amount,
              dated at the parent invoice's invoice_date — deduction happens
              at source), credit notes (credit_date; status carried so
              non-approved notes are visible but flagged)
    Returns opening balance (activity before date_from), ordered entries with
    a running balance, and closing balance.

GET /api/finance/reports/receivables?as_of=
    Per-customer outstanding rollup as of a date. 19 Oct 2026 (CEO found the old
    "0-30 / 31-60" columns confusing — 0-30 mixed money not yet due with money
    a month late): every rupee owed is placed by HOW LATE it is against its due
    date — Not yet due · 1–30 days late · 31–60 · 61–90 · 90+ days late. The due
    date is the invoice's, else invoice date + 30 days (the Revenue report's
    rule, so the two pages agree). Each customer carries its open invoices
    (number, month billed, employee, due date, days late) for the drill-down.

Proformas are not issued invoices and are left out of both reports.

Branch-wise (2 Nov 2026, Finance ask): both take `branch_id` (0 = no branch on
record). An invoice's branch is its project's branch, else its PO's billing
branch (`invoice_facets`); payments / TDS / credit notes follow their invoice and
money still on account follows its received amount's branch. The ledger also
lists money received but not yet mapped ("On account") — it is a real credit.

Read access: Finance / Sales / Sales Head (+ Admin, CEO). 19 Oct 2026 — Sales
reads the Financial Reports tab, Finance works it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, get_crm_db, subtab_gate
from models import (
    CreditNote, Customer, Invoice, InvoicePayment, Project, PurchaseOrder, TdsRecord,
)
from models.finance import InvoiceKind
from schemas.common import envelope

router = APIRouter(prefix="/api/finance/reports", tags=["CRM: Finance Reports"])

INV_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
#: Sub-tabs (22 Oct 2026): the page's own tab first, then Reports ▸ Financial
#: Reports (where the page is embedded), then the invoices grant as before.
_FIN_REPORTS = subtab_gate("reports", "financial", INV_READ)
LEDGER_READ = subtab_gate("finance-reports", "ledger", _FIN_REPORTS)
RECEIVABLES_READ = subtab_gate("finance-reports", "receivables", _FIN_REPORTS)
NOT_PROFORMA = Invoice.kind != InvoiceKind.PROFORMA.value
#: Payment terms assumed when an invoice has no due date (revenue_report's rule).
DEFAULT_TERMS_DAYS = 30
#: The receivable columns, in order — key, label, (min, max) days late.
LATE_BUCKETS: tuple[tuple[str, str, int, int | None], ...] = (
    ("not_due", "Not yet due", -10**6, 0),
    ("late_1_30", "1–30 days late", 1, 30),
    ("late_31_60", "31–60 days late", 31, 60),
    ("late_61_90", "61–90 days late", 61, 90),
    ("late_90_plus", "Over 90 days late", 91, None),
)


def late_bucket(days_late: int) -> str:
    """Which receivable column `days_late` belongs to. PURE."""
    for key, _label, lo, hi in LATE_BUCKETS:
        if days_late >= lo and (hi is None or days_late <= hi):
            return key
    return LATE_BUCKETS[-1][0]

ZERO = Decimal("0")


def _num(v) -> float:
    return float(v or 0)


def _customer_invoices(db: Session, customer_id: int) -> list[tuple[Invoice, Project | None]]:
    """The customer's issued invoices — through the project's customer OR the PO's
    (22 Oct 2026: an invoice raised on a PO with no project, or on a project of
    another customer record, used to be missing from the ledger)."""
    from sqlalchemy import or_
    po_ids = select(PurchaseOrder.id).where(PurchaseOrder.customer_id == customer_id)
    return db.execute(
        select(Invoice, Project)
        .outerjoin(Project, Project.id == Invoice.project_id)
        .where(or_(Project.customer_id == customer_id, Invoice.po_id.in_(po_ids)), NOT_PROFORMA)
        .order_by(Invoice.invoice_date, Invoice.id)
    ).all()


NO_BRANCH = 0
NO_BRANCH_LABEL = "No branch on record"


def _invoice_branches(db: Session, invoices) -> dict[int, tuple[int | None, str | None]]:
    """invoice id → (branch id, branch name): the project's delivery branch, else the
    PO's billing branch — the `invoice_facets` rule every finance screen uses."""
    from services.invoice_facets import invoice_facets
    try:
        facets = invoice_facets(db, invoices)
    except Exception:  # noqa: BLE001 — a missing branch label never breaks a ledger
        facets = {}
    return {i.id: ((facets.get(i.id) or {}).get("branch_id"), (facets.get(i.id) or {}).get("branch_name"))
            for i in invoices}


def branch_matches(entry_branch: int | None, branch_id: int | None) -> bool:
    """PURE. `branch_id` None = every branch; 0 = only entries with no branch."""
    if branch_id is None:
        return True
    if branch_id == NO_BRANCH:
        return entry_branch is None
    return entry_branch == branch_id


def ledger_branch_summary(entries: list[dict], names: dict[int, str]) -> list[dict]:
    """PURE. One row per branch over `entries` (debit · credit · balance · invoices),
    the customer's branches first by name, "No branch on record" last."""
    acc: dict[int, dict] = {}
    for e in entries:
        key = e.get("branch_id") or NO_BRANCH
        row = acc.setdefault(key, {"branch_id": key, "debit": ZERO, "credit": ZERO, "invoices": 0})
        row["debit"] += e["debit"]
        row["credit"] += e["credit"]
        if e["type"] == "Invoice":
            row["invoices"] += 1
    for bid in names:
        acc.setdefault(bid, {"branch_id": bid, "debit": ZERO, "credit": ZERO, "invoices": 0})
    out = []
    for bid, row in acc.items():
        out.append({
            "branch_id": bid, "branch_name": names.get(bid) or (NO_BRANCH_LABEL if bid == NO_BRANCH else f"Branch #{bid}"),
            "invoices": row["invoices"], "debit": _num(row["debit"]), "credit": _num(row["credit"]),
            "balance": _num(row["debit"] - row["credit"]),
        })
    out.sort(key=lambda r: (r["branch_id"] == NO_BRANCH, (r["branch_name"] or "").lower()))
    return out


@router.get("/customer-ledger")
def customer_ledger(
    customer_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    branch_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(LEDGER_READ),
):
    """2 Nov 2026 — branch-wise: every line names the customer branch it belongs to
    (an invoice's branch = project branch, else the PO's billing branch; a payment,
    TDS or credit note follows its invoice; money on account follows its received
    amount), `branch_id` narrows the whole statement to one branch (opening and
    closing balances included), and `branches` sums every branch side by side."""
    from models import CustomerBranch, CustomerReceipt
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    branch_names = dict(db.execute(
        select(CustomerBranch.id, CustomerBranch.branch_name).where(CustomerBranch.customer_id == customer_id)
    ).all())
    if branch_id is not None and branch_id < 0:
        raise HTTPException(status_code=400, detail="branch_id must be 0 (no branch) or a branch id")
    if branch_id and branch_id not in branch_names:
        raise HTTPException(status_code=400, detail="That branch does not belong to this customer")

    inv_rows = _customer_invoices(db, customer_id)
    invoice_ids = [inv.id for inv, _ in inv_rows]
    inv_by_id = {inv.id: inv for inv, _ in inv_rows}
    inv_branch = _invoice_branches(db, [inv for inv, _ in inv_rows])
    for bid, bname in inv_branch.values():
        if bid and bid not in branch_names:
            branch_names[bid] = bname or f"Branch #{bid}"

    payments = db.execute(
        select(InvoicePayment).where(InvoicePayment.invoice_id.in_(invoice_ids or [-1]))
    ).scalars().all()
    tds_rows = db.execute(
        select(TdsRecord).where(TdsRecord.invoice_id.in_(invoice_ids or [-1]))
    ).scalars().all()
    credit_notes = db.execute(
        select(CreditNote).where(CreditNote.invoice_id.in_(invoice_ids or [-1]))
    ).scalars().all()

    # Build raw entries: (date, sort_rank, type, ref, note, debit, credit)
    entries: list[dict] = []
    for inv, prj in inv_rows:
        entries.append({
            "date": inv.invoice_date, "rank": 0, "type": "Invoice",
            "ref": inv.invoice_number,
            "note": prj.name if prj is not None else "",
            "debit": Decimal(inv.grand_total or 0), "credit": ZERO,
            "invoice_id": inv.id, "invoice_number": inv.invoice_number,
        })
    for p in payments:
        inv = inv_by_id.get(p.invoice_id)
        entries.append({
            "date": p.payment_date, "rank": 1, "type": "Payment",
            "ref": p.reference_number or (inv.invoice_number if inv else ""),
            "note": f"{p.payment_mode or 'Payment'} against {inv.invoice_number if inv else '—'}",
            "debit": ZERO, "credit": Decimal(p.amount or 0),
            "invoice_id": p.invoice_id, "invoice_number": inv.invoice_number if inv else None,
            "receipt_id": getattr(p, "receipt_id", None),
        })
    for t in tds_rows:
        inv = inv_by_id.get(t.invoice_id)
        amt = Decimal(t.tds_amount or 0)
        if amt <= 0:
            continue
        entries.append({
            # TDS is deducted at source — dated with the parent invoice.
            "date": inv.invoice_date if inv else None, "rank": 2, "type": "TDS",
            "ref": inv.invoice_number if inv else "",
            "note": "TDS deducted at source",
            "debit": ZERO, "credit": amt,
            "invoice_id": t.invoice_id, "invoice_number": inv.invoice_number if inv else None,
        })
    for cn in credit_notes:
        inv = inv_by_id.get(cn.invoice_id)
        entries.append({
            "date": cn.credit_date, "rank": 3, "type": "Credit Note",
            "ref": cn.credit_note_number,
            "note": f"{cn.credit_type} against {inv.invoice_number if inv else '—'}"
                    + (f" · {cn.status}" if getattr(cn, "status", None) else ""),
            "debit": ZERO, "credit": Decimal(cn.total_amount or 0),
            "invoice_id": cn.invoice_id, "invoice_number": inv.invoice_number if inv else None,
            "credit_note_id": cn.id,
        })
    for e in entries:
        e["branch_id"] = (inv_branch.get(e.get("invoice_id")) or (None, None))[0]
    # Money received but not mapped to any invoice yet is still a credit the customer
    # has given us — it belongs on the statement, in the month it arrived, under the
    # branch that paid. Once mapped it becomes Payment lines and leaves this list.
    for r in db.execute(
        select(CustomerReceipt).where(CustomerReceipt.customer_id == customer_id,
                                      CustomerReceipt.unallocated_amount > 0)
    ).scalars().all():
        entries.append({
            "date": r.received_date, "rank": 4, "type": "On account",
            "ref": r.reference_number or f"Received amount #{r.id}",
            "note": f"{r.payment_mode or 'Received'} · not mapped to an invoice yet",
            "debit": ZERO, "credit": Decimal(r.unallocated_amount or 0),
            "invoice_id": None, "invoice_number": None, "receipt_id": r.id,
            "branch_id": r.branch_id,
        })

    entries = [e for e in entries if e["date"] is not None]
    entries.sort(key=lambda e: (e["date"], e["rank"]))
    branches = ledger_branch_summary(
        [e for e in entries if date_to is None or e["date"] <= date_to], branch_names)
    entries = [e for e in entries if branch_matches(e.get("branch_id"), branch_id)]

    def _split(pred):
        return [e for e in entries if pred(e)]

    before = _split(lambda e: date_from is not None and e["date"] < date_from)
    in_range = _split(lambda e: (date_from is None or e["date"] >= date_from)
                      and (date_to is None or e["date"] <= date_to))

    opening = sum((e["debit"] - e["credit"] for e in before), ZERO)
    running = opening
    out_entries = []
    for e in in_range:
        running += e["debit"] - e["credit"]
        out_entries.append({
            "date": e["date"].isoformat(),
            "type": e["type"],
            "ref": e["ref"],
            "note": e["note"],
            "debit": _num(e["debit"]) or None,
            "credit": _num(e["credit"]) or None,
            "balance": _num(running),
            # Links (22 Oct 2026): every line opens its invoice; a payment that
            # came through a received amount names it too.
            "invoice_id": e.get("invoice_id"),
            "invoice_number": e.get("invoice_number"),
            "receipt_id": e.get("receipt_id"),
            "credit_note_id": e.get("credit_note_id"),
            "branch_id": e.get("branch_id"),
            "branch_name": branch_names.get(e.get("branch_id")) if e.get("branch_id") else None,
        })

    totals = {
        "debit": _num(sum((e["debit"] for e in in_range), ZERO)),
        "credit": _num(sum((e["credit"] for e in in_range), ZERO)),
    }
    return envelope(data={
        "customer_id": customer_id,
        "customer_name": customer.name,
        "branch_id": branch_id,
        "branch_name": (NO_BRANCH_LABEL if branch_id == NO_BRANCH else branch_names.get(branch_id))
        if branch_id is not None else None,
        "branches": branches,
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
        "opening_balance": _num(opening),
        "closing_balance": _num(running),
        "totals": totals,
        "entries": out_entries,
    }, message="Customer ledger")


@router.get("/receivables")
def receivables(
    as_of: date | None = None,
    customer_id: int | None = None,
    branch_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RECEIVABLES_READ),
):
    """2 Nov 2026 — branch-wise: `customer_id` / `branch_id` narrow the page, every
    open invoice names its branch, and each customer carries `branches` (what each
    branch owes, how much of it is late) for the drill-down."""
    as_of = as_of or date.today()

    q = (select(Invoice, Project, Customer)
         .join(Project, Project.id == Invoice.project_id)
         .join(Customer, Customer.id == Project.customer_id)
         .where(Invoice.invoice_date <= as_of, NOT_PROFORMA))
    if customer_id:
        q = q.where(Customer.id == customer_id)
    inv_rows = db.execute(q).all()
    if branch_id is not None:
        br = _invoice_branches(db, [inv for inv, _p, _c in inv_rows])
        inv_rows = [r for r in inv_rows if branch_matches((br.get(r[0].id) or (None, None))[0], branch_id)]
    invoice_ids = [inv.id for inv, _, _ in inv_rows]

    pay_by_inv: dict[int, Decimal] = {}
    for p in db.execute(
        select(InvoicePayment).where(
            InvoicePayment.invoice_id.in_(invoice_ids or [-1]),
            InvoicePayment.payment_date <= as_of,
        )
    ).scalars().all():
        pay_by_inv[p.invoice_id] = pay_by_inv.get(p.invoice_id, ZERO) + Decimal(p.amount or 0)

    tds_by_inv: dict[int, Decimal] = {}
    for t in db.execute(
        select(TdsRecord).where(TdsRecord.invoice_id.in_(invoice_ids or [-1]))
    ).scalars().all():
        tds_by_inv[t.invoice_id] = tds_by_inv.get(t.invoice_id, ZERO) + Decimal(t.tds_amount or 0)

    cn_by_inv: dict[int, Decimal] = {}
    for cn in db.execute(
        select(CreditNote).where(
            CreditNote.invoice_id.in_(invoice_ids or [-1]),
            CreditNote.credit_date <= as_of,
        )
    ).scalars().all():
        cn_by_inv[cn.invoice_id] = cn_by_inv.get(cn.invoice_id, ZERO) + Decimal(cn.total_amount or 0)

    buckets = tuple(b[0] for b in LATE_BUCKETS)
    from services.invoice_facets import invoice_facets
    open_invoices = [inv for inv, _p, _c in inv_rows]
    try:
        facets = invoice_facets(db, open_invoices)
    except Exception:  # noqa: BLE001 — the drill-down labels are a nicety
        facets = {}
    rows: dict[int, dict] = {}
    for inv, prj, cust in inv_rows:
        row = rows.setdefault(cust.id, {
            "customer_id": cust.id, "customer_name": cust.name,
            "invoiced": ZERO, "received": ZERO, "tds": ZERO, "credit_notes": ZERO,
            "outstanding": ZERO, "overdue": ZERO, "invoice_count": 0, "overdue_count": 0,
            "oldest_days_late": 0, "open_invoices": [], "branches": {},
            **{b: ZERO for b in buckets},
        })
        total = Decimal(inv.grand_total or 0)
        received = pay_by_inv.get(inv.id, ZERO)
        tds = tds_by_inv.get(inv.id, ZERO)
        cns = cn_by_inv.get(inv.id, ZERO)
        outstanding = total - received - tds - cns
        row["invoiced"] += total
        row["received"] += received
        row["tds"] += tds
        row["credit_notes"] += cns
        row["invoice_count"] += 1
        if outstanding <= 0:
            continue
        row["outstanding"] += outstanding
        due = inv.due_date or (inv.invoice_date + timedelta(days=DEFAULT_TERMS_DAYS))
        days_late = (as_of - due).days
        key = late_bucket(days_late)
        row[key] += outstanding
        if days_late > 0:
            row["overdue_count"] += 1
            row["overdue"] += outstanding
            row["oldest_days_late"] = max(row["oldest_days_late"], days_late)
        f = facets.get(inv.id) or {}
        bkey = f.get("branch_id") or NO_BRANCH
        b = row["branches"].setdefault(bkey, {
            "branch_id": bkey, "branch_name": f.get("branch_name") or NO_BRANCH_LABEL,
            "outstanding": ZERO, "overdue": ZERO, "invoices": 0})
        b["outstanding"] += outstanding
        b["invoices"] += 1
        if days_late > 0:
            b["overdue"] += outstanding
        row["open_invoices"].append({
            "id": inv.id, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "month_label": f.get("month_label"), "branch_name": f.get("branch_name"),
            "branch_id": f.get("branch_id"),
            "employee": f.get("employee"), "project": prj.name,
            "due_date": due.isoformat(), "days_late": max(days_late, 0),
            "bucket": key, "outstanding": _num(outstanding),
        })

    out = []
    grand = {k: ZERO for k in ("invoiced", "received", "tds", "credit_notes", "outstanding",
                               "overdue", *buckets)}
    for row in sorted(rows.values(), key=lambda r: -r["outstanding"]):
        for k in grand:
            grand[k] += row[k]
        row["open_invoices"].sort(key=lambda i: -i["days_late"])
        row["branches"] = sorted(
            ({**b, "outstanding": _num(b["outstanding"]), "overdue": _num(b["overdue"])}
             for b in row["branches"].values()),
            key=lambda b: -b["outstanding"])
        out.append({
            **{k: (v if not isinstance(v, Decimal) else _num(v)) for k, v in row.items()},
        })

    return envelope(data={
        "as_of": as_of.isoformat(),
        "customers": out,
        "totals": {k: _num(v) for k, v in grand.items()},
        "buckets": [{"key": k, "label": label} for k, label, _lo, _hi in LATE_BUCKETS],
        "terms_days": DEFAULT_TERMS_DAYS,
    }, message="Payment receivables")
