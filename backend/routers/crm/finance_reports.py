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

Read access: Finance / Sales / Sales Head (+ Admin, CEO). 19 Oct 2026 — Sales
reads the Financial Reports tab, Finance works it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, get_crm_db
from models import (
    CreditNote, Customer, Invoice, InvoicePayment, Project, TdsRecord,
)
from models.finance import InvoiceKind
from schemas.common import envelope

router = APIRouter(prefix="/api/finance/reports", tags=["CRM: Finance Reports"])

INV_READ = gated_read("invoices", "Finance", "Sales_Head", "Sales")
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


def _customer_invoices(db: Session, customer_id: int) -> list[tuple[Invoice, Project]]:
    return db.execute(
        select(Invoice, Project)
        .join(Project, Project.id == Invoice.project_id)
        .where(Project.customer_id == customer_id, NOT_PROFORMA)
        .order_by(Invoice.invoice_date, Invoice.id)
    ).all()


@router.get("/customer-ledger")
def customer_ledger(
    customer_id: int,
    date_from: date | None = None,
    date_to: date | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(INV_READ),
):
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")

    inv_rows = _customer_invoices(db, customer_id)
    invoice_ids = [inv.id for inv, _ in inv_rows]
    inv_by_id = {inv.id: inv for inv, _ in inv_rows}

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
            "note": prj.name,
            "debit": Decimal(inv.grand_total or 0), "credit": ZERO,
        })
    for p in payments:
        inv = inv_by_id.get(p.invoice_id)
        entries.append({
            "date": p.payment_date, "rank": 1, "type": "Payment",
            "ref": p.reference_number or (inv.invoice_number if inv else ""),
            "note": f"{p.payment_mode or 'Payment'} against {inv.invoice_number if inv else '—'}",
            "debit": ZERO, "credit": Decimal(p.amount or 0),
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
        })
    for cn in credit_notes:
        inv = inv_by_id.get(cn.invoice_id)
        entries.append({
            "date": cn.credit_date, "rank": 3, "type": "Credit Note",
            "ref": cn.credit_note_number,
            "note": f"{cn.credit_type} against {inv.invoice_number if inv else '—'}"
                    + (f" · {cn.status}" if getattr(cn, "status", None) else ""),
            "debit": ZERO, "credit": Decimal(cn.total_amount or 0),
        })

    entries = [e for e in entries if e["date"] is not None]
    entries.sort(key=lambda e: (e["date"], e["rank"]))

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
        })

    totals = {
        "debit": _num(sum((e["debit"] for e in in_range), ZERO)),
        "credit": _num(sum((e["credit"] for e in in_range), ZERO)),
    }
    return envelope(data={
        "customer_id": customer_id,
        "customer_name": customer.name,
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
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(INV_READ),
):
    as_of = as_of or date.today()

    inv_rows = db.execute(
        select(Invoice, Project, Customer)
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
        .where(Invoice.invoice_date <= as_of, NOT_PROFORMA)
    ).all()
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
            "oldest_days_late": 0, "open_invoices": [],
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
        row["open_invoices"].append({
            "id": inv.id, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "month_label": f.get("month_label"), "branch_name": f.get("branch_name"),
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
