"""Invoice summary for the customer (23 Oct 2026, user ask).

Sales / the Sales Manager send each customer, every month, a short table of the
month's invoices — "Sr · Invoice Number · Invoice Date · Invoice Amount · Tax
Amount · Total Amount · Description" with the description reading
"Contract Staffing Service <employee> - <Mon YYYY>". This builds that table,
customer by customer, for one month, so it can be copied straight into a mail.

* Only ISSUED tax invoices (a Proforma is not a bill yet; returned ones never).
* `basis="invoice"` (default) — invoices DATED in the month (what was sent);
  `basis="work"` — invoices FOR the month's work (the billed timesheet's month,
  else the invoice date).
* Customer = the project's customer, else the PO's (the Invoices list's rule).
* Invoice Amount = value before GST (`sub_total`), Tax = `tax_amount`,
  Total = `grand_total` (round-off included).
* The employee and the work month come from `invoice_facets` (one batched read).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

BASES = ("invoice", "work")


def parse_month(key: str | None, today: date | None = None) -> tuple[int, int]:
    """"2026-10" → (2026, 10); blank → this month. ValueError on junk. PURE."""
    if not key:
        t = today or date.today()
        return t.year, t.month
    try:
        y, m = (int(x) for x in str(key).split("-")[:2])
    except ValueError:
        raise ValueError("month must be YYYY-MM") from None
    if not (2000 <= y <= 2100 and 1 <= m <= 12):
        raise ValueError("month must be YYYY-MM")
    return y, m


def _month_bounds(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, end


def describe(service: str, employee: str | None, month_label: str | None) -> str:
    """"Contract Staffing Service Anamalamudi Gowtham - Sep 2026". PURE."""
    head = " ".join(p for p in (service.strip(), (employee or "").strip()) if p)
    return f"{head} - {month_label}" if month_label else head


def _num(v) -> float:
    return float(v if isinstance(v, Decimal) else (v or 0))


def invoice_summary(db: Session, *, month: str | None = None, basis: str = "invoice",
                    customer_id: int | None = None, today: date | None = None) -> dict:
    from models import Customer, Invoice, Project, PurchaseOrder, Timesheet
    from models.finance import InvoiceKind
    from services.invoice_facets import invoice_facets, month_label
    from services.org_settings import setting

    if basis not in BASES:
        raise ValueError(f"basis must be one of {', '.join(BASES)}")
    y, m = parse_month(month, today)
    start, end = _month_bounds(y, m)
    key = f"{y:04d}-{m:02d}"
    customer_of = sa.func.coalesce(Project.customer_id, PurchaseOrder.customer_id)
    stmt = (select(Invoice, customer_of.label("cid"))
            .outerjoin(Project, Project.id == Invoice.project_id)
            .outerjoin(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
            .where(Invoice.kind != InvoiceKind.PROFORMA.value))
    if basis == "invoice":
        stmt = stmt.where(Invoice.invoice_date >= start, Invoice.invoice_date < end)
    else:
        stmt = (stmt.outerjoin(Timesheet, Timesheet.id == Invoice.timesheet_id)
                .where(sa.or_(sa.and_(Timesheet.year == y, Timesheet.month == m),
                              sa.and_(Timesheet.id.is_(None), Invoice.invoice_date >= start,
                                      Invoice.invoice_date < end))))
    if customer_id:
        stmt = stmt.where(customer_of == int(customer_id))
    rows = db.execute(stmt.order_by(Invoice.invoice_date, Invoice.invoice_number)).all()
    invoices = [inv for inv, _ in rows]
    facets = invoice_facets(db, invoices)
    cids = {int(c) for _, c in rows if c}
    names = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cids))).all()) if cids else {}
    service = str(setting("invoice.service_description") or "Contract Staffing Service")

    groups: dict[int, dict] = {}
    for inv, cid in rows:
        cid = int(cid) if cid else 0
        g = groups.setdefault(cid, {"customer_id": cid or None,
                                    "customer": names.get(cid) or "No customer on record",
                                    "rows": [], "totals": {"count": 0, "invoice_amount": 0.0,
                                                           "tax_amount": 0.0, "total_amount": 0.0}})
        f = facets.get(inv.id, {})
        row = {
            "sr": len(g["rows"]) + 1, "invoice_id": inv.id, "invoice_number": inv.invoice_number,
            "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else None,
            "invoice_amount": _num(inv.sub_total), "tax_amount": _num(inv.tax_amount),
            "total_amount": _num(inv.grand_total),
            "employee": f.get("employee"), "work_month": f.get("month"),
            "work_month_label": f.get("month_label"), "period_label": f.get("period_label"),
            "branch": f.get("branch_name"),
            "payment_status": getattr(inv.payment_status, "value", inv.payment_status),
            "description": describe(service, f.get("employee"), f.get("month_label")),
        }
        g["rows"].append(row)
        t = g["totals"]
        t["count"] += 1
        for k in ("invoice_amount", "tax_amount", "total_amount"):
            t[k] = round(t[k] + row[k], 2)
    out = sorted(groups.values(), key=lambda g: (g["customer_id"] is None, g["customer"].lower()))
    grand = {"count": sum(g["totals"]["count"] for g in out),
             "invoice_amount": round(sum(g["totals"]["invoice_amount"] for g in out), 2),
             "tax_amount": round(sum(g["totals"]["tax_amount"] for g in out), 2),
             "total_amount": round(sum(g["totals"]["total_amount"] for g in out), 2)}
    return {"month": key, "month_label": month_label(key), "basis": basis,
            "service": service, "groups": out, "totals": grand}
