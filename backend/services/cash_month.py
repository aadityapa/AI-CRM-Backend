"""Three Finance-tab additions (5 Nov 2026, user ask).

1. **Unmapped money** — received amounts still on account (not mapped to any
   invoice). They are NOT counted as Collected (that follows the invoice's work
   month), so the Dashboard shows them on their own: total, receipts, oldest,
   and the customers holding most of it, with a link to Received Amount.
2. **Cash received month** — the same money placed by the day it ARRIVED:
   mapped payments by `payment_date`, plus money still on account by
   `received_date`. Nothing is counted twice: a payment taken from a received
   amount carries that receipt's date and only the receipt's UNALLOCATED part
   is added.
3. **Mapping changed after the month closed** — a past month's Collected moves
   when Finance maps money later. For each month, the latest day a payment
   counted in it was recorded AFTER the month ended (`mapping_changed_on`).

Pure helpers + small queries; never raises into the caller (the Finance tab
wraps every block in `_safe`)."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Customer, CustomerReceipt, Invoice, InvoicePayment, Project, PurchaseOrder

TOP_CUSTOMERS = 5


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _receipt_scope(q, customer_id, branch_id, project_explicit: bool):
    """On-account money names a customer and (maybe) a branch, never a project."""
    if project_explicit:
        return None
    if customer_id:
        q = q.where(CustomerReceipt.customer_id == customer_id)
    if branch_id is not None:
        q = q.where(CustomerReceipt.branch_id.is_(None) if branch_id == 0
                    else CustomerReceipt.branch_id == branch_id)
    return q


def on_account_summary(db: Session, today: date, customer_id=None, branch_id=None,
                       project_explicit: bool = False) -> dict:
    """Every received amount with money still on account, as of today (a backlog,
    not a period figure)."""
    empty = {"amount": 0.0, "receipts": 0, "oldest": None, "oldest_days": None, "customers": []}
    q = (select(CustomerReceipt.customer_id, Customer.name, CustomerReceipt.unallocated_amount,
                CustomerReceipt.received_date)
         .join(Customer, Customer.id == CustomerReceipt.customer_id)
         .where(CustomerReceipt.unallocated_amount > 0))
    q = _receipt_scope(q, customer_id, branch_id, project_explicit)
    if q is None:
        return dict(empty, out_of_scope=True)
    rows = db.execute(q).all()
    if not rows:
        return empty
    by_cust: dict = defaultdict(lambda: {"amount": 0.0, "receipts": 0})
    oldest = None
    total = 0.0
    for cid, name, amt, got in rows:
        a = _num(amt)
        total += a
        slot = by_cust[(cid, name)]
        slot["amount"] += a
        slot["receipts"] += 1
        if got and (oldest is None or got < oldest):
            oldest = got
    customers = sorted(
        ({"customer_id": k[0], "customer": k[1] or f"Customer #{k[0]}",
          "amount": round(v["amount"], 2), "receipts": v["receipts"]} for k, v in by_cust.items()),
        key=lambda r: -r["amount"])
    return {
        "amount": round(total, 2),
        "receipts": len(rows),
        "oldest": oldest.isoformat() if oldest else None,
        "oldest_days": (today - oldest).days if oldest else None,
        "customers": customers[:TOP_CUSTOMERS],
        "customers_total": len(customers),
    }


def cash_payments(db: Session, start: date, end: date, invoice_filters: list) -> list[dict]:
    """Mapped payments by the day the money arrived (`payment_date`)."""
    rows = db.execute(
        select(InvoicePayment.payment_date, InvoicePayment.amount)
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .join(Project, Project.id == Invoice.project_id)
        .outerjoin(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
        .where(InvoicePayment.payment_date >= start, InvoicePayment.payment_date <= end,
               *invoice_filters)
    ).all()
    return [{"date": r[0], "amount": _num(r[1])} for r in rows]


def cash_on_account(db: Session, start: date, end: date, customer_id=None, branch_id=None,
                    project_explicit: bool = False) -> list[dict]:
    """Money still on account, by the day it arrived."""
    q = (select(CustomerReceipt.received_date, CustomerReceipt.unallocated_amount)
         .where(CustomerReceipt.unallocated_amount > 0,
                CustomerReceipt.received_date >= start, CustomerReceipt.received_date <= end))
    q = _receipt_scope(q, customer_id, branch_id, project_explicit)
    if q is None:
        return []
    return [{"date": r[0], "amount": _num(r[1])} for r in db.execute(q).all()]


def cash_by_month(months, payments: list[dict], on_account: list[dict]) -> dict[str, dict]:
    """PURE — `{month key: {cash_in, cash_mapped, cash_on_account}}`."""
    out = {}
    for m in months:
        mapped = sum(p["amount"] for p in payments if p["date"] and m.start <= p["date"] <= m.end)
        held = sum(r["amount"] for r in on_account if r["date"] and m.start <= r["date"] <= m.end)
        out[m.key] = {"cash_in": round(mapped + held, 2), "cash_mapped": round(mapped, 2),
                      "cash_on_account": round(held, 2)}
    return out


def mapping_changes(months, payments: list[dict]) -> dict[str, dict]:
    """PURE — for each month, payments counted in it (by the invoice's work month)
    that were recorded AFTER the month ended: the latest such day and the amount.
    `payments` are `_payments_in_window` rows (`date` = the month they count in,
    `mapped_at` = when recorded)."""
    out: dict[str, dict] = {}
    for m in months:
        late = [p for p in payments
                if p.get("date") and m.start <= p["date"] <= m.end
                and _day(p.get("mapped_at")) and _day(p["mapped_at"]) > m.end]
        if not late:
            continue
        last = max(_day(p["mapped_at"]) for p in late)
        out[m.key] = {"mapping_changed_on": last.isoformat(),
                      "mapped_later": round(sum(p["amount"] for p in late), 2)}
    return out
