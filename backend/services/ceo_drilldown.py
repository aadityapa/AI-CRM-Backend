"""CEO dashboard drill-downs — one page behind every tile (27 Oct 2026).

The CEO asked: "when I click Billed, open a separate page with every invoice
behind that number and which of them are pending, paid or partly paid; the
same for Cash collected with its POs; the same for every tile of Customer,
Sales, People and Billing — think like a CEO".

Every metric here answers ONE tile and returns the same shape, so a single
React page renders all of them:

    metric · tab · title · question       what the page answers, in words
    period                                the dashboard's period view (anchor + zoom)
    kpis      [{key, label, value, format, tone, sub}]
    charts    [{key, title, subtitle, rows, series[{key, label, tone, stack}],
                x_key, layout, format}]
    segments  [{key, label, tone, count, amount}]   chips over tables[0]
    default_segment                                 the chip a tile opens on
    tables    [{key, title, subtitle, columns[{key, label, format, link}], rows}]
    related   [{metric, label}]                     the next questions
    notes     [str]                                 the definitions, in words

⚠️ Nothing here is a second definition. Every figure is computed the way the
tile it opens computes it — the Revenue report's invoice window (Tax only,
`sub_total`, the PROJECT's customer), `billing_rules.invoice_stage` for where
an invoice stands, `executive_dashboard._customer / _sales / _people` for the
other tabs — so the page always adds up to the tile that sent the CEO there.

Admin / CEO only for the Customer / Sales / People metrics; the Finance
metrics are also open to whoever may read the Finance tab (route gate).
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from models import (
    Customer, CustomerBranch, CustomerReceipt, Invoice, InvoicePayment, POStatus,
    PaymentStatus, Project, PurchaseOrder, TdsRecord,
)
from models.finance import InvoiceKind
from services import billing_rules
from services import executive_dashboard as ex
from services import hiring_dashboard as hiring
from services.invoice_facets import invoice_facets
from services.revenue_report import (
    TARGET_MONTH_KEY, TARGET_MONTH_PREFIX, Month, Period, _deployed_rows, _monthly_rate,
    _setting_number, revenue_report,
)

logger = logging.getLogger("karnex.crm.ceo_drilldown")

#: Rows per chart before the rest fold into "Others (N)".
TOP_ROWS = 12
#: Rows per table — a page that lists 5,000 invoices helps nobody; the CSV does.
MAX_TABLE_ROWS = 2000
#: A PO ending within this many days, or this used, is "renew soon".
PO_RENEW_DAYS = 60
PO_RENEW_USED_PCT = 90.0
#: A customer's billing that moved this much against the previous period.
CUSTOMER_MOVE_PCT = 10.0

LATE_BUCKETS = (
    ("not_due", "Not yet due", "success"),
    ("late_1_30", "1–30 days late", "warning"),
    ("late_31_60", "31–60 days late", "warning"),
    ("late_61_90", "61–90 days late", "danger"),
    ("late_90_plus", "90+ days late", "danger"),
)
STAGE_TONE = {
    "awaiting_customer": "info", "pending_einvoice": "violet", "unpaid": "warning",
    "partially_paid": "brand", "paid": "success", "proforma": "neutral",
}
_LIVE_STAGES = ("awaiting_customer", "pending_einvoice", "unpaid", "partially_paid", "paid")


# --------------------------------------------------------------------------- shape helpers


def kpi(key: str, label: str, value, fmt: str = "money", tone: str = "neutral", sub: str | None = None,
        go: dict | None = None) -> dict:
    """A tile. `go` says what clicking it shows (28 Oct 2026, user ask: every tile clickable):
    `{segments: [...]}` narrows the main table to those chips ([] = every row), `where: <field>`
    keeps rows whose field is non-zero, `sort: <field>` orders them (largest first), and
    `metric` (+ `scope`) opens another drill page instead. A tile without `go` shows the
    whole main table."""
    out = {"key": key, "label": label, "value": value, "format": fmt, "tone": tone, "sub": sub}
    if go:
        out["go"] = go
    return out


def col(key: str, label: str, fmt: str = "text", link: str | None = None, align: str | None = None) -> dict:
    out = {"key": key, "label": label, "format": fmt}
    if link:
        out["link"] = link
    if align or fmt in ("money", "int", "pct", "days", "num"):
        out["align"] = align or "right"
    return out


def series(key: str, label: str, tone, stack: str | None = None, off: bool = False) -> dict:
    out = {"key": key, "label": label, "tone": tone}
    if stack:
        out["stack"] = stack
    if off:
        out["off"] = True
    return out


def chart(key: str, title: str, rows: list[dict], ser: list[dict], *, subtitle: str | None = None,
          layout: str = "horizontal", fmt: str = "money", x_key: str = "label") -> dict:
    return {"key": key, "title": title, "subtitle": subtitle, "rows": rows, "series": ser,
            "layout": layout, "format": fmt, "x_key": x_key}


def table(key: str, title: str, columns: list[dict], rows: list[dict], subtitle: str | None = None) -> dict:
    total = len(rows)
    return {"key": key, "title": title, "subtitle": subtitle, "columns": columns,
            "rows": rows[:MAX_TABLE_ROWS], "total": total, "truncated": total > MAX_TABLE_ROWS}


def segments(rows: list[dict], defs, amount_key: str | None = None) -> list[dict]:
    """Chips over the main table — (key, label, tone) in order, each with its
    count and amount; a segment with nothing in it is still listed (zero is news)."""
    out = []
    for key, label, tone in defs:
        hit = [r for r in rows if r.get("segment") == key]
        out.append({"key": key, "label": label, "tone": tone, "count": len(hit),
                    "amount": round(sum(float(r.get(amount_key) or 0) for r in hit), 2) if amount_key else None})
    return out


def top_rows(rows: list[dict], sort_key: str, sum_keys: list[str], label_key: str = "label",
             n: int = TOP_ROWS) -> list[dict]:
    """The `n` biggest rows by `sort_key`; the rest folded into one "Others (N)" row."""
    ordered = sorted(rows, key=lambda r: -(r.get(sort_key) or 0))
    head, rest = ordered[:n], ordered[n:]
    if rest:
        other = {label_key: f"Others ({len(rest)})"}
        for k in sum_keys:
            other[k] = round(sum(float(r.get(k) or 0) for r in rest), 2)
        head.append(other)
    return head


def _iso(d) -> str | None:
    return d.isoformat() if isinstance(d, date) else None


def _r(v) -> float:
    return round(float(v or 0), 2)


def _pct(part, whole) -> float | None:
    return round(float(part) * 100.0 / float(whole), 1) if whole else None


def late_bucket(days: int, open_: bool = True) -> str | None:
    if not open_:
        return None
    if days <= 0:
        return "not_due"
    if days <= 30:
        return "late_1_30"
    if days <= 60:
        return "late_31_60"
    if days <= 90:
        return "late_61_90"
    return "late_90_plus"


def _scope_note(customer_id, project_id) -> list[str]:
    if customer_id or project_id:
        return ["Narrowed to the customer / project chosen on the dashboard — clear it there to see the whole company."]
    return []


# --------------------------------------------------------------------------- invoice loader


_INVOICE_COLS = (
    Invoice.id, Invoice.invoice_number, Invoice.invoice_date, Invoice.due_date, Invoice.sub_total,
    Invoice.tax_amount, Invoice.grand_total, Invoice.paid_amount, Invoice.balance_amount,
    Invoice.payment_status, Invoice.tds_deducted, Invoice.customer_approved_at, Invoice.irn_number,
    Invoice.po_id, Invoice.project_id, Invoice.timesheet_id, Invoice.split_no, Invoice.split_start,
    Invoice.split_end, Invoice.kind,
)


def _invoice_scope(customer_id: int | None, project_id: int | None) -> list:
    out = [Invoice.kind != InvoiceKind.PROFORMA.value]
    if customer_id:
        out.append(Project.customer_id == customer_id)
    if project_id:
        out.append(Invoice.project_id == project_id)
    return out


def load_invoices(db: Session, where: list, today: date, customer_id=None, project_id=None) -> list[dict]:
    """Every Tax invoice matching `where`, with where it stands (the ONE stage rule),
    what was received (cash + TDS), when it is due and how late, the PO it draws on,
    and the work month / employee / branch (`invoice_facets`)."""
    rows = db.execute(
        select(Invoice, Project.customer_id, Customer.name, Project.name, PurchaseOrder.po_number)
        .options(load_only(*_INVOICE_COLS))
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
        .outerjoin(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
        .where(*_invoice_scope(customer_id, project_id), *where)
    ).all()
    facets = invoice_facets(db, [r[0] for r in rows])
    out = []
    for inv, cid, cname, pname, po_number in rows:
        f = facets.get(inv.id, {})
        stage = billing_rules.stage_of_invoice(inv)
        paid, tds = _r(inv.paid_amount), _r(inv.tds_deducted)
        balance = _r(inv.balance_amount) if stage != "paid" else 0.0
        due = billing_rules.effective_due(inv.due_date, inv.invoice_date)
        late = billing_rules.days_late(inv.due_date, inv.invoice_date, today) if balance > 0 else 0
        out.append({
            "id": inv.id, "link": f"invoices/{inv.id}", "invoice": inv.invoice_number or f"#{inv.id}",
            "invoice_date": _iso(inv.invoice_date), "_date": inv.invoice_date,
            "work_month": f.get("month_label"), "work_period": f.get("period_label"),
            "customer_id": cid, "customer": cname, "customer_link": f"customers/{cid}" if cid else None,
            "project_id": inv.project_id, "project": pname,
            "project_link": f"projects/{inv.project_id}" if inv.project_id else None,
            "employee": f.get("employee"), "employee_id": f.get("employee_id"),
            "employee_link": f"employees/{f['employee_id']}" if f.get("employee_id") else None,
            "branch": f.get("branch_name"),
            "po_id": inv.po_id, "po_number": po_number, "po_link": f"pos/{inv.po_id}" if inv.po_id else None,
            "part": f"Part {inv.split_no}" if inv.split_no else None,
            "sub_total": _r(inv.sub_total), "tax": _r(inv.tax_amount), "grand_total": _r(inv.grand_total),
            "paid": paid, "tds": tds, "received": round(paid + tds, 2), "balance": balance,
            "due_date": _iso(due), "_due": due, "days_late": max(0, late),
            "late_bucket": late_bucket(late, balance > 0),
            "stage": stage, "stage_label": billing_rules.STAGE_LABELS.get(stage, stage),
            "segment": stage,
        })
    out.sort(key=lambda r: (r["_date"] or date.min, r["id"]), reverse=True)
    return out


def _strip(rows: list[dict]) -> list[dict]:
    """Drop the private (`_`) keys before a row goes on the wire."""
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]


INVOICE_COLUMNS = [
    col("invoice", "Invoice", link="link"), col("invoice_date", "Invoice date", "date"),
    col("work_month", "Work month"), col("customer", "Customer", link="customer_link"),
    col("project", "Project", link="project_link"), col("employee", "Employee", link="employee_link"),
    col("po_number", "PO", link="po_link"), col("sub_total", "Billed (excl. GST)", "money"),
    col("grand_total", "Invoice value", "money"), col("received", "Received", "money"),
    col("balance", "Balance due", "money"), col("due_date", "Due", "date"),
    col("days_late", "Days late", "days"), col("stage_label", "Where it stands", "badge"),
]


def _with_gst_column(columns: list[dict]) -> list[dict]:
    """The invoice columns with GST right after the value before GST — the Billed page's
    "GST on it" tile needs a column it can sort by."""
    out = []
    for c in columns:
        out.append(c)
        if c["key"] == "sub_total":
            out.append(col("tax", "GST", "money"))
    return out


def _months(period: Period) -> list[Month]:
    return ex._months_in(period)


def _by_month(rows: list[dict], period: Period, date_key: str, split_key: str, keys, value_key: str) -> list[dict]:
    """One row per month of the period, the value summed per split value."""
    out = []
    for m in _months(period):
        row = {"label": m.start.strftime("%b %y"), "key": m.key}
        for k in keys:
            row[k] = 0.0
        for r in rows:
            d = r.get(date_key)
            if d and m.start <= d <= m.end and r.get(split_key) in keys:
                row[r[split_key]] += float(r.get(value_key) or 0)
        for k in keys:
            row[k] = round(row[k], 2)
        out.append(row)
    return out


def _month_sums(rows: list[dict], period: Period, date_key: str, value_keys: list[str],
                today: date | None = None) -> list[dict]:
    """One row per month of the period with each value summed side by side (28 Oct 2026,
    user ask: the monthly chart reads like "Who we billed" — grouped columns, not a stack).
    Months that have not started yet are left off, so a running FY is not half empty."""
    out = []
    for m in _months(period):
        if today is not None and m.start > today:
            break
        row = {"label": m.start.strftime("%b %y"), "key": m.key, **{k: 0.0 for k in value_keys}}
        for r in rows:
            d = r.get(date_key)
            if d and m.start <= d <= m.end:
                for k in value_keys:
                    row[k] += float(r.get(k) or 0)
        for k in value_keys:
            row[k] = round(row[k], 2)
        out.append(row)
    return out


def _by_customer(rows: list[dict], value_keys: list[str], sort_key: str) -> list[dict]:
    acc: dict[str, dict] = {}
    for r in rows:
        name = r.get("customer") or "—"
        slot = acc.setdefault(name, {"label": name, **{k: 0.0 for k in value_keys}})
        for k in value_keys:
            slot[k] += float(r.get(k) or 0)
    for slot in acc.values():
        for k in value_keys:
            slot[k] = round(slot[k], 2)
    return top_rows(list(acc.values()), sort_key, value_keys)


# --------------------------------------------------------------------------- finance: billed


def m_billed(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    rows = load_invoices(db, [Invoice.invoice_date >= period.start, Invoice.invoice_date <= period.end],
                         today, customer_id, project_id)
    billed = sum(r["sub_total"] for r in rows)
    tax = sum(r["tax"] for r in rows)
    received = sum(r["received"] for r in rows)
    due = sum(r["balance"] for r in rows)
    overdue = sum(r["balance"] for r in rows if r["days_late"] > 0)
    paid_n = sum(1 for r in rows if r["stage"] == "paid")
    stage_defs = [(k, billing_rules.STAGE_LABELS[k], STAGE_TONE[k]) for k in _LIVE_STAGES]
    for r in rows:
        r["overdue_amount"] = r["balance"] if r["days_late"] > 0 else 0.0
    monthly = _month_sums(rows, period, "_date", ["sub_total", "received", "balance", "overdue_amount"], today)
    for m in monthly:
        m["overdue"] = m.pop("overdue_amount")
    mix = [{"label": billing_rules.STAGE_LABELS[k],
            "amount": round(sum(r["grand_total"] for r in rows if r["stage"] == k), 2),
            "balance": round(sum(r["balance"] for r in rows if r["stage"] == k), 2)} for k in _LIVE_STAGES]
    return {
        "title": "Billed", "question": f"Every invoice raised in {period.label} — and where each one stands now",
        "kpis": [
            kpi("billed", "Billed (excl. GST)", round(billed, 2), tone="brand", sub=f"{len(rows)} invoices",
                go={"segments": [], "sort": "sub_total"}),
            kpi("gst", "GST on it", round(tax, 2), sub=f"invoice value {round(billed + tax):,}",
                go={"segments": [], "where": "tax", "sort": "tax"}),
            kpi("received", "Received so far", round(received, 2), tone="success",
                sub=f"{_pct(received, billed + tax) or 0:.0f}% of the invoice value",
                go={"segments": [], "where": "received", "sort": "received"}),
            kpi("due", "Still to collect", round(due, 2), tone="warning" if due else "success",
                sub=f"{sum(1 for r in rows if r['balance'] > 0)} invoices open",
                go={"segments": [], "where": "balance", "sort": "balance"}),
            kpi("overdue", "Of which overdue", round(overdue, 2), tone="danger" if overdue else "success",
                sub=f"{sum(1 for r in rows if r['days_late'] > 0 and r['balance'] > 0)} invoices past due",
                go={"segments": [], "where": "overdue_amount", "sort": "days_late"}),
            kpi("paid", "Fully paid", paid_n, "int", "success", f"of {len(rows)} invoices",
                go={"segments": ["paid"]}),
        ],
        "charts": [
            chart("monthly", "Billed month by month", monthly,
                  [series("sub_total", "Billed", "brand"), series("received", "Received", "success"),
                   series("balance", "Still due", "warning"), series("overdue", "Overdue", "danger")],
                  subtitle="By invoice date — billed excl. GST; received and due on the invoice value"),
            chart("customers", "Who we billed", _by_customer(rows, ["sub_total", "received", "balance"], "sub_total"),
                  [series("sub_total", "Billed", "brand"), series("received", "Received", "success"),
                   series("balance", "Still due", "warning")]),
            chart("stages", "Where the money stands", mix,
                  [series("amount", "Invoice value", "brand"), series("balance", "Still due", "warning")]),
        ],
        "segments": segments(rows, stage_defs, "grand_total"),
        "default_segment": scope if scope in _LIVE_STAGES else None,
        "tables": [table("invoices", "Invoices", _with_gst_column(INVOICE_COLUMNS), _strip(rows))],
        "related": [{"metric": "collected", "label": "Cash collected"},
                    {"metric": "outstanding", "label": "Outstanding"},
                    {"metric": "margin", "label": "Gross margin"}],
        "notes": ["Billed = Tax invoices dated in the period, value before GST (Proformas are not money yet).",
                  "Where it stands follows the invoice: awaiting the customer's approval → e-invoice (IRN) → "
                  "unpaid → partly paid → paid. Received = cash + TDS the customer withheld."]
                 + _scope_note(customer_id, project_id),
    }


# --------------------------------------------------------------------------- finance: collected


def m_collected(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    pay = db.execute(
        select(InvoicePayment.id, InvoicePayment.payment_date, InvoicePayment.amount, InvoicePayment.payment_mode,
               InvoicePayment.reference_number, InvoicePayment.receipt_id,
               Invoice.id, Invoice.invoice_number, Invoice.invoice_date, Invoice.due_date, Invoice.po_id,
               Project.customer_id, Customer.name, Project.id, Project.name, PurchaseOrder.po_number)
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
        .outerjoin(PurchaseOrder, PurchaseOrder.id == Invoice.po_id)
        .where(InvoicePayment.payment_date >= period.start, InvoicePayment.payment_date <= period.end,
               *_invoice_scope(customer_id, project_id))
    ).all()
    rows = []
    for (pid, pdate, amount, mode, ref, rid, iid, inum, idate, idue, po_id, cid, cname, prj_id, prj,
         po_number) in pay:
        due = billing_rules.effective_due(idue, idate)
        late_by = (pdate - due).days if (pdate and due) else 0
        rows.append({
            "id": pid, "payment_date": _iso(pdate), "_date": pdate, "amount": _r(amount),
            "mode": mode or "—", "reference": ref,
            "invoice": inum or f"#{iid}", "link": f"invoices/{iid}", "invoice_date": _iso(idate),
            "due_date": _iso(due), "days_to_pay": (pdate - idate).days if (pdate and idate) else None,
            "late_by": max(0, late_by),
            "customer_id": cid, "customer": cname, "customer_link": f"customers/{cid}" if cid else None,
            "project": prj, "project_link": f"projects/{prj_id}" if prj_id else None,
            "po_id": po_id, "po_number": po_number, "po_link": f"pos/{po_id}" if po_id else None,
            "segment": "late" if late_by > 0 else "on_time", "_po": po_id,
        })
    rows.sort(key=lambda r: (r["_date"] or date.min, r["id"]), reverse=True)
    total = sum(r["amount"] for r in rows)
    late_amt = sum(r["amount"] for r in rows if r["segment"] == "late")
    weighted = sum(r["amount"] * (r["days_to_pay"] or 0) for r in rows if r["days_to_pay"] is not None)

    # The POs behind this cash — what the order was worth, what is left on it and
    # how much of it came in this period.
    po_ids = {r["_po"] for r in rows if r["_po"]}
    by_po: dict[int, float] = defaultdict(float)
    for r in rows:
        if r["_po"]:
            by_po[r["_po"]] += r["amount"]
    po_rows = _po_rows(db, today, [PurchaseOrder.id.in_(po_ids or {0})])
    for p in po_rows:
        p["collected_here"] = round(by_po.get(p["id"], 0.0), 2)
    po_rows.sort(key=lambda p: -p["collected_here"])
    no_po = round(sum(r["amount"] for r in rows if not r["_po"]), 2)

    # Money in the bank that is not on an invoice yet (receipts on account).
    rq = (select(CustomerReceipt.id, CustomerReceipt.received_date, CustomerReceipt.amount,
                 CustomerReceipt.unallocated_amount, CustomerReceipt.payment_mode,
                 CustomerReceipt.reference_number, Customer.id, Customer.name, CustomerBranch.branch_name)
          .join(Customer, Customer.id == CustomerReceipt.customer_id)
          .outerjoin(CustomerBranch, CustomerBranch.id == CustomerReceipt.branch_id)
          .where(CustomerReceipt.unallocated_amount > 0, CustomerReceipt.received_date <= period.end))
    if customer_id:
        rq = rq.where(CustomerReceipt.customer_id == customer_id)
    on_account = [{
        "id": rid, "received_date": _iso(d), "amount": _r(a), "on_account": _r(u), "mode": m or "—",
        "reference": ref, "customer": cn, "customer_link": f"customers/{cid}", "branch": bn,
        "link": f"received-amounts?receipt={rid}",
    } for rid, d, a, u, m, ref, cid, cn, bn in db.execute(rq.order_by(CustomerReceipt.received_date.desc())).all()] \
        if not project_id else []
    on_account_total = sum(r["on_account"] for r in on_account)

    monthly = _by_month(rows, period, "_date", "segment", ("on_time", "late"), "amount")
    modes: dict[str, float] = defaultdict(float)
    for r in rows:
        modes[r["mode"]] += r["amount"]
    return {
        "title": "Cash collected", "question": f"Every payment received in {period.label} — from whom, against which invoice and PO",
        "kpis": [
            kpi("collected", "Collected", round(total, 2), tone="success", sub=f"{len(rows)} payments"),
            kpi("customers", "Customers who paid", len({r["customer_id"] for r in rows}), "int"),
            kpi("days", "Avg days to pay", round(weighted / total, 1) if total else None, "days",
                sub="from invoice date, by amount"),
            kpi("late", "Paid after the due date", _pct(late_amt, total), "pct",
                "warning" if late_amt else "success", f"{round(late_amt):,} of the cash"),
            kpi("pos", "POs it came against", len(po_ids), "int", sub=f"{round(no_po):,} on invoices with no PO" if no_po else None),
            kpi("on_account", "Received, not yet mapped", round(on_account_total, 2), tone="warning" if on_account_total else "neutral",
                sub=f"{len(on_account)} receipts on account"),
        ],
        "charts": [
            chart("monthly", "Cash in month by month", monthly,
                  [series("on_time", "On time", "success", "c"), series("late", "Paid late", "warning", "c")]),
            chart("customers", "Who paid us", _by_customer(rows, ["amount"], "amount"),
                  [series("amount", "Collected", "success")]),
            chart("pos", "Purchase orders — collected vs what is left",
                  top_rows([{"label": p["po_number"], "collected_here": p["collected_here"], "balance": p["balance"]}
                            for p in po_rows], "collected_here", ["collected_here", "balance"]),
                  [series("collected_here", "Collected this period", "success"), series("balance", "Left on the PO", "brand")]),
            chart("modes", "How it came in", top_rows([{"label": k, "amount": round(v, 2)} for k, v in modes.items()],
                                                      "amount", ["amount"]),
                  [series("amount", "Amount", "brand")]),
        ],
        "segments": segments(rows, [("on_time", "Paid on time", "success"), ("late", "Paid late", "warning")], "amount"),
        "default_segment": scope if scope in ("on_time", "late") else None,
        "tables": [
            table("payments", "Payments", [
                col("payment_date", "Paid on", "date"), col("customer", "Customer", link="customer_link"),
                col("invoice", "Invoice", link="link"), col("invoice_date", "Invoice date", "date"),
                col("po_number", "PO", link="po_link"), col("project", "Project", link="project_link"),
                col("amount", "Amount", "money"), col("mode", "Mode"), col("reference", "Reference"),
                col("days_to_pay", "Days to pay", "days"), col("late_by", "Days late", "days"),
            ], _strip(rows)),
            table("pos", "Purchase orders behind this cash", PO_COLUMNS + [col("collected_here", "Collected this period", "money")],
                  _strip(po_rows)),
            table("on_account", "Received but not yet mapped to an invoice", [
                col("received_date", "Received", "date"), col("customer", "Customer", link="customer_link"),
                col("branch", "Branch"), col("amount", "Amount", "money"),
                col("on_account", "Not on an invoice", "money"), col("mode", "Mode"),
                col("reference", "Reference", link="link"),
            ], on_account, subtitle="Open the receipt and map it — until then the invoices still read as unpaid."),
        ],
        "related": [{"metric": "billed", "label": "Billed"}, {"metric": "outstanding", "label": "Outstanding"},
                    {"metric": "dso", "label": "DSO"}, {"metric": "po", "label": "Purchase orders"}],
        "notes": ["Collected = payments recorded against Tax invoices, by payment date (GST included, as received).",
                  "Paid late = after the invoice's due date (its own, else invoice date + 30 days)."]
                 + _scope_note(customer_id, project_id),
    }


# --------------------------------------------------------------------------- finance: outstanding / overdue


def m_outstanding(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    rows = load_invoices(db, [Invoice.payment_status != PaymentStatus.PAID, Invoice.balance_amount > 0,
                              Invoice.invoice_date <= period.end], today, customer_id, project_id)
    for r in rows:
        r["segment"] = r["late_bucket"] or "not_due"
    rows.sort(key=lambda r: (-r["days_late"], -r["balance"]))
    total = sum(r["balance"] for r in rows)
    overdue = sum(r["balance"] for r in rows if r["days_late"] > 0)
    tds = db.execute(
        select(sa.func.coalesce(sa.func.sum(TdsRecord.tds_balance), 0))
        .where(TdsRecord.invoice_id.in_([r["id"] for r in rows] or [0]))
    ).scalar() if rows else 0
    buckets = [{"label": label, "balance": round(sum(r["balance"] for r in rows if r["segment"] == key), 2),
                "invoices": sum(1 for r in rows if r["segment"] == key)} for key, label, _ in LATE_BUCKETS]
    by_cust: dict[str, dict] = {}
    for r in rows:
        slot = by_cust.setdefault(r["customer"] or "—", {"label": r["customer"] or "—", "not_due": 0.0, "overdue": 0.0})
        slot["overdue" if r["days_late"] > 0 else "not_due"] += r["balance"]
    for s in by_cust.values():
        s["total"] = round(s["not_due"] + s["overdue"], 2)
        s["not_due"], s["overdue"] = round(s["not_due"], 2), round(s["overdue"], 2)
    # When the money should land: open balances by their due month (past due → this month).
    land: dict[str, dict] = {}
    this_month = Month(today.year, today.month)
    for r in rows:
        d = r["_due"] or today
        m = this_month if d < this_month.start else Month(d.year, d.month)
        slot = land.setdefault(m.key, {"label": m.start.strftime("%b %y"), "key": m.key, "overdue": 0.0, "due": 0.0})
        slot["overdue" if r["days_late"] > 0 else "due"] += r["balance"]
    landing = [dict(v, overdue=round(v["overdue"], 2), due=round(v["due"], 2)) for _, v in sorted(land.items())][:8]
    oldest = max((r["days_late"] for r in rows), default=0)
    return {
        "title": "Outstanding" if scope != "overdue" else "Overdue",
        "question": "Every invoice still waiting for money — how late, from whom, and when it should land",
        "kpis": [
            kpi("outstanding", "Outstanding", round(total, 2), tone="warning" if total else "success",
                sub=f"{len(rows)} open invoices"),
            kpi("overdue", "Overdue", round(overdue, 2), tone="danger" if overdue else "success",
                sub=f"{_pct(overdue, total) or 0:.0f}% of what is owed"),
            kpi("not_due", "Not yet due", round(total - overdue, 2), tone="success"),
            kpi("customers", "Customers owing", len({r["customer_id"] for r in rows}), "int"),
            kpi("oldest", "Oldest overdue", oldest, "days", "danger" if oldest > 90 else "warning" if oldest else "success"),
            kpi("tds", "TDS still to come", _r(tds), sub="withheld by customers, certificate pending"),
        ],
        "charts": [
            chart("ageing", "How late", buckets, [series("balance", "Balance", "danger"), series("invoices", "Invoices", "neutral", off=True)]),
            chart("customers", "Who owes us", top_rows(list(by_cust.values()), "total", ["not_due", "overdue", "total"]),
                  [series("overdue", "Overdue", "danger", "o"), series("not_due", "Not yet due", "success", "o"),
                   series("total", "Total", "neutral", off=True)]),
            chart("landing", "When it should land", landing,
                  [series("overdue", "Already late (chase now)", "danger", "l"), series("due", "Due in this month", "brand", "l")],
                  subtitle="By due date; anything already late is counted in the current month"),
        ],
        "segments": segments(rows, LATE_BUCKETS, "balance"),
        "default_segment": None,
        "default_segments": [k for k, _, _ in LATE_BUCKETS if k != "not_due"] if scope == "overdue" else None,
        "tables": [table("invoices", "Open invoices", INVOICE_COLUMNS, _strip(rows))],
        "related": [{"metric": "collected", "label": "Cash collected"}, {"metric": "dso", "label": "DSO"},
                    {"metric": "billed", "label": "Billed"}],
        "notes": [f"Open Tax invoices dated up to {period.end:%d %b %Y}, as of today.",
                  "Days late count from the invoice's due date, else invoice date + 30 days."]
                 + _scope_note(customer_id, project_id),
    }


def m_overdue(db, period, today, customer_id=None, project_id=None, scope=None) -> dict:
    return m_outstanding(db, period, today, customer_id, project_id, scope="overdue")


# --------------------------------------------------------------------------- finance: margin


def m_margin(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    data = ex._customer(db, period, today)
    customers = [c for c in data["customers"] if not customer_id or c["customer_id"] == customer_id]
    projects, employees = [], []
    for c in customers:
        for p in c["projects"]:
            if project_id and p["project_id"] != project_id:
                continue
            projects.append({**{k: v for k, v in p.items() if k != "employees"}, "customer": c["customer"],
                             "customer_link": f"customers/{c['customer_id']}", "link": f"projects/{p['project_id']}",
                             "segment": "loss" if p["margin"] < 0 else "profit"})
            for e in p["employees"]:
                employees.append({**e, "customer": c["customer"], "project": p["project"],
                                  "link": f"employees/{e['employee_id']}", "project_link": f"projects/{p['project_id']}",
                                  "margin_pct": _pct(e["margin"], e["billed"]) if e["billed"] else None})
    rows = []
    for c in customers:
        rows.append({**{k: v for k, v in c.items() if k != "projects"}, "link": f"customers/{c['customer_id']}",
                     "segment": "no_billing" if c["billed"] <= 0 else ("loss" if c["margin"] < 0 else "profit")})
    billed = sum(r["billed"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    no_ctc = sum(1 for e in employees if not e.get("has_ctc"))
    loss = [r for r in rows if r["segment"] == "loss"]
    return {
        "title": "Gross margin", "question": "What each customer, project and person earns after their people cost",
        "kpis": [
            kpi("billed", "Billed (excl. GST)", round(billed, 2), tone="brand"),
            kpi("cost", "People cost", round(cost, 2), sub="CTC ÷ 12 × months deployed"),
            kpi("margin", "Gross margin", round(billed - cost, 2), tone="success" if billed >= cost else "danger"),
            kpi("margin_pct", "Margin %", _pct(billed - cost, billed), "pct",
                "success" if (_pct(billed - cost, billed) or 0) >= 25 else "warning"),
            kpi("loss", "Loss-making customers", len(loss), "int", "danger" if loss else "success"),
            kpi("no_ctc", "Heads without a CTC", no_ctc, "int", "warning" if no_ctc else "success",
                "their cost is missing — margin is overstated" if no_ctc else None),
        ],
        "charts": [
            chart("customers", "Billed vs cost by customer", top_rows(
                [{"label": r["customer"], "billed": r["billed"], "cost": r["cost"], "margin": r["margin"]} for r in rows],
                "billed", ["billed", "cost", "margin"]),
                [series("billed", "Billed", "brand"), series("cost", "People cost", "warning"), series("margin", "Margin", "success")]),
            chart("projects", "Margin by project", top_rows(
                [{"label": p["project"], "margin": p["margin"], "billed": p["billed"]} for p in projects],
                "billed", ["margin", "billed"]),
                [series("margin", "Margin", "success"), series("billed", "Billed", "brand", off=True)]),
            chart("monthly", "Month by month", [{"label": m["label"], "key": m["key"], "billed": m["billed"], "cost": m["cost"],
                                                 "margin": round(m["billed"] - m["cost"], 2)} for m in data["monthly"]],
                  [series("billed", "Billed", "brand"), series("cost", "People cost", "warning"), series("margin", "Margin", "success")]),
        ],
        "segments": segments(rows, [("profit", "Profitable", "success"), ("loss", "Loss-making", "danger"),
                                    ("no_billing", "Cost, no billing", "warning")], "margin"),
        "default_segment": None,
        "tables": [
            table("customers", "By customer", [
                col("customer", "Customer", link="link"), col("billed", "Billed", "money"),
                col("cost", "People cost", "money"), col("margin", "Margin", "money"),
                col("margin_pct", "Margin %", "pct"), col("heads", "Heads", "int"),
                col("change_pct", "Billed vs previous", "pct"),
            ], sorted(rows, key=lambda r: r["margin"])),
            table("projects", "By project", [
                col("project", "Project", link="link"), col("customer", "Customer", link="customer_link"),
                col("billed", "Billed", "money"), col("cost", "People cost", "money"),
                col("margin", "Margin", "money"), col("margin_pct", "Margin %", "pct"), col("heads", "Heads", "int"),
            ], sorted(projects, key=lambda r: r["margin"])),
            table("employees", "By person", [
                col("employee", "Employee", link="link"), col("customer", "Customer"),
                col("project", "Project", link="project_link"), col("monthly_rate", "Monthly rate", "money"),
                col("billed", "Billed", "money"), col("cost", "Cost", "money"), col("margin", "Margin", "money"),
                col("margin_pct", "Margin %", "pct"),
            ], sorted(employees, key=lambda r: r["margin"])),
        ],
        "related": [{"metric": "billed", "label": "Billed"}, {"metric": "deployed", "label": "Deployed heads"},
                    {"metric": "customers", "label": "Customers"}],
        "notes": ["Margin = billed (excl. GST) − people cost. People cost = annual CTC ÷ 12 × the months each "
                  "person was on the customer in the period. Overheads are not taken off.",
                  "Lists are sorted worst first, so the loss-makers are at the top."] + _scope_note(customer_id, project_id),
    }


# --------------------------------------------------------------------------- finance: target


def m_target(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    rep = revenue_report(db, period.anchor.key, today, period_raw=period.kind)
    t = rep["targets"]
    invoices = ex._invoices_in_window(db, period.start, period.end)
    default = _setting_number(db, TARGET_MONTH_KEY)
    rows, run = [], 0.0
    run_target = 0.0
    for m in _months(period):
        billed = sum(i["sub_total"] for i in invoices if m.start <= i["invoice_date"] <= m.end)
        target = _setting_number(db, TARGET_MONTH_PREFIX + m.key)
        target = target if target is not None else default
        run += billed
        run_target += target or 0
        rows.append({"label": m.start.strftime("%b %y"), "key": m.key, "month": m.label, "billed": round(billed, 2),
                     "target": round(target, 2) if target is not None else None,
                     "gap": round((target or 0) - billed, 2) if target is not None else None,
                     "attainment_pct": _pct(billed, target) if target else None,
                     "cumulative": round(run, 2), "cumulative_target": round(run_target, 2),
                     "future": m.start > today,
                     "segment": ("future" if m.start > today else "hit" if target and billed >= target
                                 else "missed" if target else "no_target")})
    billed = sum(r["billed"] for r in rows)
    target = t.get("month_target")
    return {
        "title": "Revenue target", "question": "Are we on track — month by month against the target",
        "kpis": [
            kpi("billed", "Billed", round(billed, 2), tone="brand"),
            kpi("target", f"Target for {period.short_label}", target),
            kpi("attainment", "Attainment", t.get("month_attainment_pct"), "pct",
                "success" if (t.get("month_attainment_pct") or 0) >= 100 else "warning"),
            kpi("run_rate", "Run rate", t.get("run_rate"), sub="if the pace holds to the end of the period"),
            kpi("fy_target", f"{t.get('fy_label') or 'FY'} target", t.get("fy_target")),
            kpi("fy_projection", "FY projection", t.get("fy_projection"),
                tone="success" if (t.get("fy_projection_pct") or 0) >= 100 else "warning",
                sub=f"{t.get('fy_projection_pct') or 0:.0f}% of the FY target · needs "
                    f"{round(t.get('fy_required_monthly') or 0):,} / month" if t.get("fy_target") else None),
        ],
        "charts": [
            chart("monthly", "Billed vs target", rows,
                  [series("billed", "Billed", "brand"), series("target", "Target", "neutral")]),
            chart("cumulative", "Running total vs running target", rows,
                  [series("cumulative", "Billed to date", "success"), series("cumulative_target", "Target to date", "neutral")]),
        ],
        "segments": segments(rows, [("hit", "Target met", "success"), ("missed", "Below target", "danger"),
                                    ("no_target", "No target set", "neutral"), ("future", "Still to come", "neutral")]),
        "default_segment": None,
        "tables": [table("months", "Month by month", [
            col("month", "Month"), col("billed", "Billed", "money"), col("target", "Target", "money"),
            col("gap", "Gap", "money"), col("attainment_pct", "Attainment", "pct"),
            col("cumulative", "Billed to date", "money"),
        ], rows)],
        "related": [{"metric": "billed", "label": "Billed"}, {"metric": "customers", "label": "Customers"}],
        "notes": ["Targets are company-wide and stored per month; a quarter or FY target is the sum of its months.",
                  "Set them from the dashboard's Targets button."]
                 + (["The customer / project filter does not apply here — a target is company-wide."]
                    if customer_id or project_id else []),
    }


# --------------------------------------------------------------------------- finance: DSO


def m_dso(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    as_of = today if period.contains(today) else period.end
    window = 90
    open_rows = load_invoices(db, [Invoice.payment_status != PaymentStatus.PAID, Invoice.balance_amount > 0,
                                   Invoice.invoice_date <= as_of], today, customer_id, project_id)
    recent = load_invoices(db, [Invoice.invoice_date > as_of - timedelta(days=window), Invoice.invoice_date <= as_of],
                           today, customer_id, project_id)
    paid = db.execute(
        select(InvoicePayment.amount, InvoicePayment.payment_date, Invoice.invoice_date, Project.customer_id)
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .join(Project, Project.id == Invoice.project_id)
        .where(InvoicePayment.payment_date >= period.start, InvoicePayment.payment_date <= period.end,
               *_invoice_scope(customer_id, project_id))
    ).all()
    acc: dict[int, dict] = {}

    def slot(cid, name):
        return acc.setdefault(cid, {"customer_id": cid, "customer": name, "link": f"customers/{cid}",
                                    "outstanding": 0.0, "overdue": 0.0, "billed_90": 0.0, "_w": 0.0, "_p": 0.0})
    names = {}
    for r in open_rows:
        s = slot(r["customer_id"], r["customer"])
        s["outstanding"] += r["balance"]
        s["overdue"] += r["balance"] if r["days_late"] > 0 else 0
        names[r["customer_id"]] = r["customer"]
    for r in recent:
        slot(r["customer_id"], r["customer"])["billed_90"] += r["grand_total"]
    for amount, pdate, idate, cid in paid:
        if cid in acc and pdate and idate:
            acc[cid]["_w"] += float(amount or 0) * max(0, (pdate - idate).days)
            acc[cid]["_p"] += float(amount or 0)
    rows = []
    for s in acc.values():
        dso = round(s["outstanding"] / s["billed_90"] * window, 1) if s["billed_90"] > 0 else None
        rows.append({
            "customer_id": s["customer_id"], "customer": s["customer"], "link": s["link"],
            "outstanding": round(s["outstanding"], 2), "overdue": round(s["overdue"], 2),
            "billed_90": round(s["billed_90"], 2), "dso": dso,
            "avg_days_to_pay": round(s["_w"] / s["_p"], 1) if s["_p"] else None,
            "segment": ("no_recent" if dso is None else "slow" if dso > 60 else "watch" if dso > 45 else "healthy"),
        })
    rows.sort(key=lambda r: -(r["dso"] if r["dso"] is not None else 10_000))
    out_total = sum(r["outstanding"] for r in rows)
    billed_total = sum(r["billed_90"] for r in rows)
    dso_all = round(out_total / billed_total * window, 1) if billed_total else None
    w = sum(float(a or 0) * max(0, (p - i).days) for a, p, i, _c in paid if p and i)
    p_total = sum(float(a or 0) for a, _p, _i, _c in paid)
    return {
        "title": "DSO — days sales outstanding", "question": "How many days of billing are sitting unpaid, customer by customer",
        "kpis": [
            kpi("dso", "DSO", dso_all, "days", "success" if (dso_all or 0) <= 45 else "warning" if (dso_all or 0) <= 60 else "danger",
                sub=f"outstanding ÷ last {window} days' billing × {window}"),
            kpi("days", "Avg days to pay", round(w / p_total, 1) if p_total else None, "days", sub=f"payments in {period.short_label}"),
            kpi("outstanding", "Outstanding", round(out_total, 2), tone="warning"),
            kpi("billed_90", f"Billed in the last {window} days", round(billed_total, 2), sub="incl. GST"),
            kpi("slow", "Slow payers", sum(1 for r in rows if r["segment"] == "slow"), "int", "danger", "DSO above 60 days"),
        ],
        "charts": [
            chart("dso", "DSO by customer", top_rows([{"label": r["customer"], "dso": r["dso"] or 0,
                                                       "avg_days_to_pay": r["avg_days_to_pay"] or 0} for r in rows],
                                                     "dso", ["dso"]),
                  [series("dso", "DSO (days)", "warning"), series("avg_days_to_pay", "Avg days to pay", "brand")], fmt="days"),
            chart("owed", "What they owe", top_rows([{"label": r["customer"], "overdue": r["overdue"],
                                                       "not_due": round(r["outstanding"] - r["overdue"], 2)} for r in rows],
                                                     "overdue", ["overdue", "not_due"]),
                  [series("overdue", "Overdue", "danger", "o"), series("not_due", "Not yet due", "success", "o")]),
        ],
        "segments": segments(rows, [("slow", "Slow (60+ d)", "danger"), ("watch", "Watch (45–60 d)", "warning"),
                                    ("healthy", "Healthy", "success"), ("no_recent", "No recent billing", "neutral")],
                             "outstanding"),
        "default_segment": None,
        "tables": [table("customers", "By customer", [
            col("customer", "Customer", link="link"), col("dso", "DSO", "days"),
            col("avg_days_to_pay", "Avg days to pay", "days"), col("outstanding", "Outstanding", "money"),
            col("overdue", "Overdue", "money"), col("billed_90", f"Billed last {window} d", "money"),
        ], rows)],
        "related": [{"metric": "outstanding", "label": "Outstanding"}, {"metric": "collected", "label": "Cash collected"}],
        "notes": [f"DSO = outstanding ÷ the last {window} days' billing (incl. GST) × {window}, as of "
                  f"{as_of:%d %b %Y}. Healthy ≤ 45 days, watch to 60, slow beyond."] + _scope_note(customer_id, project_id),
    }


# --------------------------------------------------------------------------- finance: purchase orders


PO_COLUMNS = [
    col("po_number", "PO", link="link"), col("customer", "Customer", link="customer_link"),
    col("branch", "Branch"), col("status", "Status", "badge"), col("start_date", "Start", "date"),
    col("end_date", "End", "date"), col("days_left", "Days left", "days"), col("total_value", "Value", "money"),
    col("consumed", "Used", "money"), col("balance", "Balance", "money"), col("used_pct", "Used %", "pct"),
]


def _po_rows(db: Session, today: date, where: list) -> list[dict]:
    rows = db.execute(
        select(PurchaseOrder.id, PurchaseOrder.po_number, PurchaseOrder.customer_id, Customer.name,
               CustomerBranch.branch_name, PurchaseOrder.status, PurchaseOrder.start_date, PurchaseOrder.end_date,
               PurchaseOrder.total_value, PurchaseOrder.consumed_value, PurchaseOrder.balance_value)
        .join(Customer, Customer.id == PurchaseOrder.customer_id)
        .outerjoin(CustomerBranch, CustomerBranch.id == PurchaseOrder.billing_branch_id)
        .where(*where)
    ).all()
    out = []
    for pid, num, cid, cname, branch, status, start, end, total, used, bal in rows:
        st = getattr(status, "value", status)
        used_pct = _pct(float(used or 0), float(total or 0))
        days_left = (end - today).days if end else None
        if st == POStatus.CANCELLED.value:
            seg = "cancelled"
        elif st == POStatus.EXHAUSTED.value:
            seg = "exhausted"
        elif end and end < today:
            seg = "expired"
        elif (days_left is not None and days_left <= PO_RENEW_DAYS) or (used_pct or 0) >= PO_RENEW_USED_PCT:
            seg = "renew"
        else:
            seg = "healthy"
        out.append({
            "id": pid, "po_number": num, "link": f"pos/{pid}", "customer_id": cid, "customer": cname,
            "customer_link": f"customers/{cid}", "branch": branch, "status": st,
            "start_date": _iso(start), "end_date": _iso(end), "days_left": days_left,
            "total_value": _r(total), "consumed": _r(used), "balance": _r(bal), "used_pct": used_pct, "segment": seg,
        })
    return out


def m_po(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    where = [PurchaseOrder.customer_id == customer_id] if customer_id else []
    rows = _po_rows(db, today, where)
    # Billed against each PO in the period, and every customer's burn today (the
    # monthly rate of everyone deployed now) → months of cover.
    billed = defaultdict(float)
    for pid, amt in db.execute(
        select(Invoice.po_id, sa.func.coalesce(sa.func.sum(Invoice.sub_total), 0))
        .where(Invoice.kind != InvoiceKind.PROFORMA.value, Invoice.invoice_date >= period.start,
               Invoice.invoice_date <= period.end, Invoice.po_id.isnot(None))
        .group_by(Invoice.po_id)
    ).all():
        billed[pid] = float(amt or 0)
    burn = defaultdict(float)
    for d in _deployed_rows(db, customer_id):
        if (not d["start"] or d["start"] <= today) and (not d["exit"] or d["exit"] >= today):
            burn[d["customer_id"]] += _monthly_rate(d["rate"], d["unit"])
    active_bal = defaultdict(float)
    for r in rows:
        if r["segment"] in ("healthy", "renew"):
            active_bal[r["customer_id"]] += r["balance"]
    for r in rows:
        r["billed_here"] = round(billed.get(r["id"], 0.0), 2)
        b = burn.get(r["customer_id"], 0.0)
        r["customer_cover"] = round(active_bal[r["customer_id"]] / b, 1) if b else None
    order = {"renew": 0, "expired": 1, "healthy": 2, "exhausted": 3, "cancelled": 4}
    rows.sort(key=lambda r: (order.get(r["segment"], 9), r["days_left"] if r["days_left"] is not None else 99999))
    live = [r for r in rows if r["segment"] in ("healthy", "renew")]
    value = sum(r["total_value"] for r in live)
    used = sum(r["consumed"] for r in live)
    expired_left = sum(r["balance"] for r in rows if r["segment"] == "expired")
    cover = [{"label": (r["customer"] or "—"), "balance": round(active_bal[cid], 2),
              "burn": round(burn.get(cid, 0.0), 2)} for cid, r in {r["customer_id"]: r for r in live}.items()]
    ending = defaultdict(float)
    for r in live:
        if r["end_date"]:
            ending[r["end_date"][:7]] += r["balance"]
    ending_rows = [{"label": date.fromisoformat(k + "-01").strftime("%b %y"), "balance": round(v, 2)}
                   for k, v in sorted(ending.items())][:12]
    return {
        "title": "Purchase orders", "question": "How much order book is left, what ends soon, and how long it lasts",
        "kpis": [
            kpi("live", "Live POs", len(live), "int", "brand", f"{len({r['customer_id'] for r in live})} customers"),
            kpi("value", "Live PO value", round(value, 2)),
            kpi("balance", "Balance left to bill", round(value - used, 2), tone="success"),
            kpi("used", "Used", _pct(used, value), "pct", "warning" if (_pct(used, value) or 0) >= 80 else "neutral"),
            kpi("renew", "Renew soon", sum(1 for r in rows if r["segment"] == "renew"), "int", "warning",
                f"ending within {PO_RENEW_DAYS} days or {PO_RENEW_USED_PCT:.0f}%+ used"),
            kpi("expired", "Expired with balance", round(expired_left, 2), tone="danger" if expired_left else "success",
                sub="never billed — ask the customer to extend"),
        ],
        "charts": [
            chart("customers", "Order book by customer", top_rows(
                [{"label": r["customer"], "consumed": r["consumed"], "balance": r["balance"]} for r in live],
                "balance", ["consumed", "balance"]),
                [series("consumed", "Used", "neutral", "p"), series("balance", "Left", "success", "p")]),
            chart("cover", "Balance vs monthly burn", top_rows(cover, "balance", ["balance", "burn"]),
                  [series("balance", "PO balance", "brand"), series("burn", "Monthly burn", "warning")],
                  subtitle="Burn = the monthly rate of everyone deployed at the customer today"),
            chart("ending", "When the balance runs out of time", ending_rows,
                  [series("balance", "Balance on POs ending that month", "warning")]),
        ],
        "segments": segments(rows, [("renew", "Renew soon", "warning"), ("expired", "Expired", "danger"),
                                    ("healthy", "Healthy", "success"), ("exhausted", "Used up", "neutral"),
                                    ("cancelled", "Cancelled", "neutral")], "balance"),
        "default_segment": scope if scope in ("renew", "expired", "healthy", "exhausted") else None,
        "tables": [table("pos", "Purchase orders", PO_COLUMNS + [
            col("billed_here", f"Billed in {period.short_label}", "money"),
            col("customer_cover", "Customer cover (months)", "num"),
        ], rows)],
        "related": [{"metric": "collected", "label": "Cash collected"}, {"metric": "billed", "label": "Billed"}],
        "notes": ["A PO is drawn by the invoice value BEFORE GST.",
                  "Customer cover = the customer's live PO balance ÷ the monthly rate of everyone deployed there today."],
    }


# --------------------------------------------------------------------------- customer tab


def m_customers(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    data = ex._customer(db, period, today)
    rows = []
    for c in data["customers"]:
        if customer_id and c["customer_id"] != customer_id:
            continue
        ch = c["change_pct"]
        seg = ("quiet" if c["billed"] <= 0 else "new" if c["previous"] <= 0
               else "growing" if ch is not None and ch >= CUSTOMER_MOVE_PCT
               else "shrinking" if ch is not None and ch <= -CUSTOMER_MOVE_PCT else "steady")
        rows.append({**{k: v for k, v in c.items() if k != "projects"}, "link": f"customers/{c['customer_id']}",
                     "segment": seg})
    s = data["summary"]
    return {
        "title": "Customers", "question": "Every account — how it is growing, paying, staffed and covered",
        "kpis": [
            kpi("billing", "Customers billing", sum(1 for r in rows if r["billed"] > 0), "int", "brand",
                f"of {len(rows)} with activity"),
            kpi("billed", "Billed", round(sum(r["billed"] for r in rows), 2), sub=f"{s.get('change_pct') or 0:+.0f}% {period.comparison_label}"),
            kpi("growing", "Growing", sum(1 for r in rows if r["segment"] in ("growing", "new")), "int", "success"),
            kpi("shrinking", "Shrinking", sum(1 for r in rows if r["segment"] == "shrinking"), "int", "warning"),
            kpi("top", "Top customer share", s.get("top1_share_pct"), "pct",
                "warning" if s.get("concentration_risk") else "neutral", s.get("top_customer")),
            kpi("thin_po", "PO cover under 2 months", sum(1 for r in rows if r["po_cover_months"] is not None
                                                          and r["po_cover_months"] < ex.PO_COVER_WARN_MONTHS), "int", "warning"),
        ],
        "charts": [
            chart("billed", f"Billed vs {period.comparison_label.replace('vs ', '')}", top_rows(
                [{"label": r["customer"], "billed": r["billed"], "previous": r["previous"]} for r in rows],
                "billed", ["billed", "previous"]),
                [series("billed", "This period", "brand"), series("previous", "Previous", "neutral")]),
            chart("owed", "Collected and owed", top_rows(
                [{"label": r["customer"], "collected": r["collected"], "outstanding": r["outstanding"],
                  "overdue": r["overdue"]} for r in rows], "outstanding", ["collected", "outstanding", "overdue"]),
                [series("collected", "Collected", "success"), series("outstanding", "Outstanding", "warning"),
                 series("overdue", "Overdue", "danger")]),
            chart("people", "People on each account", top_rows(
                [{"label": r["customer"], "internal": r["internal"], "external": r["external"],
                  "open": r["open_positions"], "heads": r["heads"]} for r in rows], "heads", ["internal", "external", "open", "heads"]),
                [series("internal", "Internal", "brand", "h"), series("external", "External", "violet", "h"),
                 series("open", "Open positions", "warning")], fmt="int"),
        ],
        "segments": segments(rows, [("growing", "Growing", "success"), ("new", "New", "brand"),
                                    ("steady", "Steady", "neutral"), ("shrinking", "Shrinking", "warning"),
                                    ("quiet", "No billing", "danger")], "billed"),
        "default_segment": scope if scope in ("growing", "shrinking", "quiet", "new", "steady") else None,
        "tables": [table("customers", "Accounts", [
            col("customer", "Customer", link="link"), col("billed", "Billed", "money"),
            col("change_pct", "Change", "pct"), col("share_pct", "Share", "pct"),
            col("collected", "Collected", "money"), col("outstanding", "Outstanding", "money"),
            col("overdue", "Overdue", "money"), col("margin_pct", "Margin %", "pct"),
            col("heads_today", "Deployed today", "int"), col("open_positions", "Open positions", "int"),
            col("po_balance", "PO balance", "money"), col("po_cover_months", "PO cover (mo)", "num"),
        ], rows)],
        "related": [{"metric": "margin", "label": "Gross margin"}, {"metric": "deployed", "label": "Deployed heads"},
                    {"metric": "po", "label": "Purchase orders"}],
        "notes": [f"Growing / shrinking = billed {CUSTOMER_MOVE_PCT:.0f}% or more above / below the previous period."],
    }


def m_deployed(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    data = ex._customer(db, period, today)
    rows = []
    for c in data["customers"]:
        if customer_id and c["customer_id"] != customer_id:
            continue
        for p in c["projects"]:
            if project_id and p["project_id"] != project_id:
                continue
            for e in p["employees"]:
                live = e["deployed_today"] and not e["exited"]
                rows.append({**e, "customer": c["customer"], "customer_link": f"customers/{c['customer_id']}",
                             "project": p["project"], "project_link": f"projects/{p['project_id']}",
                             "link": f"employees/{e['employee_id']}", "pe_link": f"project-employees/{e['pe_id']}",
                             "kind_label": e["kind"].title() if e.get("kind") else "Unknown",
                             "segment": "today" if live else "left"})
    by_cust: dict[str, dict] = {}
    for r in rows:
        slot = by_cust.setdefault(r["customer"], {"label": r["customer"], "internal": 0, "external": 0, "unknown": 0, "total": 0})
        slot[r["kind"] if r["kind"] in ("internal", "external") else "unknown"] += 1
        slot["total"] += 1
    trend = [{"label": b["label"], "key": b["key"], "total": b["total"]} for b in data["deployed_trend"]["month"] if not b["future"]]
    billed = sum(r["billed"] for r in rows)
    today_n = sum(1 for r in rows if r["segment"] == "today")
    return {
        "title": "Deployed heads", "question": "Who worked on which account in the period — and what each one earns",
        "kpis": [
            kpi("heads", "Heads in the period", len(rows), "int", "brand"),
            kpi("today", "Deployed today", today_n, "int", "success"),
            kpi("internal", "Internal", sum(1 for r in rows if r["kind"] == "internal"), "int"),
            kpi("external", "External", sum(1 for r in rows if r["kind"] == "external"), "int"),
            kpi("per_head", "Billed per head", round(billed / len(rows), 2) if rows else None),
            kpi("burn", "Monthly burn today", round(sum(r["monthly_rate"] for r in rows if r["segment"] == "today"), 2),
                tone="brand", sub="monthly rate of everyone deployed now"),
        ],
        "charts": [
            chart("customers", "Heads by customer", top_rows(list(by_cust.values()), "total",
                                                            ["internal", "external", "unknown", "total"]),
                  [series("internal", "Internal", "brand", "h"), series("external", "External", "violet", "h"),
                   series("unknown", "Unknown", "neutral", "h")], fmt="int"),
            chart("trend", "Deployed at month end", trend, [series("total", "Heads", "success")], fmt="int"),
        ],
        "segments": segments(rows, [("today", "Deployed today", "success"), ("left", "Rolled off", "neutral")], "billed"),
        "default_segment": scope if scope in ("today", "left") else None,
        "tables": [table("heads", "People", [
            col("employee", "Employee", link="link"), col("customer", "Customer", link="customer_link"),
            col("project", "Project", link="project_link"), col("kind_label", "Internal / external", "badge"),
            col("onboarding", "Onboarded", "date"), col("exit", "Exit", "date"),
            col("monthly_rate", "Monthly rate", "money"), col("billed", "Billed", "money"),
            col("cost", "Cost", "money"), col("margin", "Margin", "money"),
        ], sorted(rows, key=lambda r: -r["billed"]))],
        "related": [{"metric": "margin", "label": "Gross margin"}, {"metric": "headcount", "label": "Headcount"}],
        "notes": ["A person on two customers is listed once per assignment."],
    }


# --------------------------------------------------------------------------- sales tab


def m_positions(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    data = ex._sales(db, period, today)
    rows = []
    for p in data["positions"]:
        if customer_id and p["customer_id"] != customer_id:
            continue
        if scope == "brought_in" and not p["created_in_period"]:
            continue
        seg = ("closed" if not p["live"] else "filled" if p["open"] == 0
               else "stale" if p["age_days"] >= ex.STALE_POSITION_DAYS else "open")
        rows.append({k: v for k, v in p.items() if k != "candidates"} | {
            "link": f"opportunities/{p['opportunity_id']}", "customer_link": f"customers/{p['customer_id']}",
            "label_title": f"{p['opp_id'] or ''} {p['title']}".strip(),
            "joined_names": ", ".join(c["name"] for c in p["candidates"]) or None, "segment": seg})
    by_cust: dict[str, dict] = {}
    for r in rows:
        slot = by_cust.setdefault(r["customer"], {"label": r["customer"], "joined": 0, "open": 0, "positions": 0})
        slot["joined"] += r["joined"]
        slot["open"] += r["open"] if r["live"] else 0
        slot["positions"] += r["positions"]
    owners: dict[str, dict] = {}
    for r in rows:
        slot = owners.setdefault(r["owner"] or "Unassigned", {"label": r["owner"] or "Unassigned", "positions": 0, "joined": 0})
        slot["positions"] += r["positions"]
        slot["joined"] += r["joined"]
    default = {"open": "open", "stale": "stale", "filled": "filled"}.get(scope or "")
    total = sum(r["positions"] for r in rows if r["live"])
    joined = sum(r["joined"] for r in rows if r["live"])
    return {
        "title": "Positions brought in" if scope == "brought_in" else "Positions",
        "question": "Every position — how many we filled, what is still open and how long it has waited",
        "kpis": [
            kpi("brought_in", "Brought in this period", sum(r["positions"] for r in rows if r["created_in_period"]), "int", "brand"),
            kpi("open", "Open now", sum(r["open"] for r in rows if r["live"]), "int", "warning"),
            kpi("fill", "Fill rate (live)", _pct(joined, total), "pct", "success" if (_pct(joined, total) or 0) >= 60 else "warning"),
            kpi("stale", f"Open {ex.STALE_POSITION_DAYS}+ days", sum(1 for r in rows if r["segment"] == "stale"), "int", "danger"),
            kpi("joined_here", "Joined in the period", sum(r["joined_in_period"] for r in rows), "int", "success"),
            kpi("internal", "Internal / external", f"{sum(r['internal'] for r in rows)} / {sum(r['external'] for r in rows)}", "text"),
        ],
        "charts": [
            chart("customers", "Positions by customer", top_rows(list(by_cust.values()), "positions", ["joined", "open", "positions"]),
                  [series("joined", "Filled", "success", "p"), series("open", "Open", "warning", "p")], fmt="int"),
            chart("monthly", "Brought in vs onboarded", [{"label": m["label"], "key": m["key"], "positions_in": m["positions_in"],
                                                          "onboardings": m["onboardings"]} for m in data["monthly"]],
                  [series("positions_in", "Positions in", "brand"), series("onboardings", "Onboarded", "success")], fmt="int"),
            chart("owners", "By Sales owner", top_rows(list(owners.values()), "positions", ["positions", "joined"]),
                  [series("positions", "Positions", "brand"), series("joined", "Joined", "success")], fmt="int"),
        ],
        "segments": segments(rows, [("open", "Open", "warning"), ("stale", f"Stale ({ex.STALE_POSITION_DAYS}+ d)", "danger"),
                                    ("filled", "Filled", "success"), ("closed", "Closed", "neutral")]),
        "default_segment": default,
        "tables": [table("positions", "Positions", [
            col("label_title", "Position", link="link"), col("customer", "Customer", link="customer_link"),
            col("owner", "Sales owner"), col("status", "Status", "badge"), col("positions", "Positions", "int"),
            col("joined", "Joined", "int"), col("open", "Open", "int"), col("fill_pct", "Fill %", "pct"),
            col("age_days", "Age (days)", "days"), col("created_on", "Opened", "date"),
            col("joined_names", "Who joined"),
        ], rows)],
        "related": [{"metric": "onboarded", "label": "Onboarded"}, {"metric": "stages", "label": "Waiting in stages"}],
        "notes": ["Positions = `no_of_positions` of each requirement; open = positions − joined."],
    }


def m_onboarded(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    joins = [j for j in hiring._joined_rows(db) if period.contains(j.joined_on)]
    kinds = ex._joined_kinds(db, joins)
    from models import Candidate, CandidateProfile, Opportunity
    info = {pid: (name, oid, code, title, cid, cname) for pid, name, oid, code, title, cid, cname in db.execute(
        select(CandidateProfile.id,
               sa.func.trim(sa.func.coalesce(Candidate.first_name, "") + " " + sa.func.coalesce(Candidate.last_name, "")),
               Opportunity.id, Opportunity.opp_id, Opportunity.title, Customer.id, Customer.name)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .where(CandidateProfile.id.in_([j.profile_id for j in joins] or [0]))
    ).all()}
    rows = []
    for j in joins:
        name, oid, code, title, cid, cname = info.get(j.profile_id, (None, None, None, None, None, None))
        if customer_id and cid != customer_id:
            continue
        kind, reason, _e = kinds.get(j.profile_id, ("unknown", "", ""))
        rows.append({"profile_id": j.profile_id, "candidate": name or f"Candidate #{j.profile_id}",
                     "link": f"profiles/{j.profile_id}", "joined_on": _iso(j.joined_on), "_date": j.joined_on,
                     "position": f"{code or ''} {title or ''}".strip(), "position_link": f"opportunities/{oid}" if oid else None,
                     "customer": cname, "customer_link": f"customers/{cid}" if cid else None,
                     "kind_label": kind.title(), "reason": reason, "segment": kind})
    rows.sort(key=lambda r: r["_date"], reverse=True)
    monthly = _by_month(rows, period, "_date", "segment", ("internal", "external", "unknown"), "_one")
    for m in monthly:
        for k in ("internal", "external", "unknown"):
            m[k] = sum(1 for r in rows if r["segment"] == k and r["_date"].strftime("%b %y") == m["label"])
    by_cust: dict[str, dict] = {}
    for r in rows:
        slot = by_cust.setdefault(r["customer"] or "—", {"label": r["customer"] or "—", "internal": 0, "external": 0, "unknown": 0, "total": 0})
        slot[r["segment"]] += 1
        slot["total"] += 1
    n_int = sum(1 for r in rows if r["segment"] == "internal")
    n_ext = sum(1 for r in rows if r["segment"] == "external")
    return {
        "title": "Onboarded", "question": f"Everyone who joined a customer in {period.label} — internal or external",
        "kpis": [
            kpi("joined", "Onboarded", len(rows), "int", "success"),
            kpi("internal", "Internal", n_int, "int", "brand"),
            kpi("external", "External", n_ext, "int", "violet"),
            kpi("internal_pct", "Internal share", _pct(n_int, n_int + n_ext), "pct"),
            kpi("unknown", "Not linked to an employee", sum(1 for r in rows if r["segment"] == "unknown"), "int", "warning"),
        ],
        "charts": [
            chart("monthly", "Onboarded month by month", monthly,
                  [series("internal", "Internal", "brand", "k"), series("external", "External", "violet", "k"),
                   series("unknown", "Unknown", "neutral", "k")], fmt="int"),
            chart("customers", "Where they joined", top_rows(list(by_cust.values()), "total", ["internal", "external", "unknown", "total"]),
                  [series("internal", "Internal", "brand", "k"), series("external", "External", "violet", "k"),
                   series("unknown", "Unknown", "neutral", "k")], fmt="int"),
        ],
        "segments": segments(rows, [("internal", "Internal", "brand"), ("external", "External", "violet"),
                                    ("unknown", "Unknown", "neutral")]),
        "default_segment": None,
        "tables": [table("joiners", "Onboarded", [
            col("candidate", "Candidate", link="link"), col("joined_on", "Joined", "date"),
            col("position", "Position", link="position_link"), col("customer", "Customer", link="customer_link"),
            col("kind_label", "Internal / external", "badge"), col("reason", "Why"),
        ], _strip(rows))],
        "related": [{"metric": "positions", "label": "Positions"}],
        "notes": ["Internal = an earlier placement anywhere, or joined Karnex more than 30 days before the onboarding."],
    }


def m_stages(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    tower = hiring.hiring_dashboard(db, period.anchor.key, period.kind, today)
    sd = tower["stage_delays"]
    rows = [{**r, "link": "profiles", "segment": r["state"]} for r in sd["rows"]]
    return {
        "title": "Waiting in stages", "question": "Where candidates are stuck between hiring steps, and for how long",
        "kpis": [
            kpi("waiting", "Waiting", sd["total_waiting"], "int", "brand"),
            kpi("over_warn", f"Over {sd['warn_days']} days", sum(r["over_warn"] for r in rows), "int", "warning"),
            kpi("over_bad", f"Over {sd['bad_days']} days", sum(r["over_bad"] for r in rows), "int", "danger"),
        ],
        "charts": [chart("stages", "Candidates waiting per stage", [
            {"label": r["label"], "ok": r["count"] - r["over_warn"], "warn": r["over_warn"] - r["over_bad"], "bad": r["over_bad"]}
            for r in rows], [series("ok", "On time", "success", "w"), series("warn", f"{sd['warn_days']}+ days", "warning", "w"),
                             series("bad", f"{sd['bad_days']}+ days", "danger", "w")], fmt="int"),
                   chart("avg", "Average wait (days)", [{"label": r["label"], "avg_days": r["avg_days"] or 0,
                                                         "max_days": r["max_days"]} for r in rows],
                         [series("avg_days", "Average", "brand"), series("max_days", "Longest", "danger")], fmt="days")],
        "segments": segments(rows, [("bad", "Stuck", "danger"), ("warn", "Slow", "warning"), ("ok", "Moving", "success")]),
        "default_segment": None,
        "tables": [table("stages", "By stage", [
            col("label", "Stage", link="link"), col("count", "Waiting", "int"), col("avg_days", "Avg days", "days"),
            col("max_days", "Longest", "days"), col("over_warn", f"{sd['warn_days']}+ days", "int"),
            col("over_bad", f"{sd['bad_days']}+ days", "int"),
        ], rows)],
        "related": [{"metric": "positions", "label": "Positions"}, {"metric": "onboarded", "label": "Onboarded"}],
        "notes": ["Days since the candidate's last stage change."],
    }


# --------------------------------------------------------------------------- people tab


def m_headcount(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    from models import Employee
    from services.project_closure import DEPLOYED, deployment_by_employee
    emps = db.execute(select(Employee.id, Employee.first_name, Employee.last_name, Employee.employee_code,
                             Employee.role_title, Employee.date_of_joining, Employee.current_ctc, Employee.is_active,
                             Employee.is_resigned, Employee.date_of_resignation, Employee.last_working_day)).all()
    # 29 Oct 2026 — a month bar on the dashboard opens this page at THAT month, so the
    # list is "as of the period's end" (today for a period still running) and adds up
    # to the bar: deployed on a past day = an assignment that had started and not ended
    # (the `_deployed_rows` start / exit rule the People tab's month-end figures use).
    as_of = min(today, period.end)
    past = as_of < today
    current = []
    for eid, fn, ln, code, role, doj, ctc, active, resigned, resigned_on, lwd in emps:
        exit_on = lwd or (resigned_on if resigned else None)
        if (doj and doj > as_of) or (exit_on and exit_on < as_of) or (not exit_on and not active and not past):
            continue
        current.append({"employee_id": eid, "employee": " ".join(p for p in (fn, ln) if p) or f"#{eid}",
                        "employee_code": code, "role": role or "No designation", "doj": _iso(doj),
                        "tenure_years": round((as_of - doj).days / 365.25, 1) if doj else None,
                        "cost_month": round(float(ctc) / 12, 2) if ctc is not None else None,
                        "on_notice": "Yes" if resigned and not past else None, "exit_on": _iso(exit_on),
                        "link": f"employees/{eid}"})
    if past:
        live: dict[int, dict] = {}
        for r in _deployed_rows(db):
            if (r["start"] and r["start"] > as_of) or (r["exit"] and r["exit"] < as_of):
                continue
            slot = live.setdefault(r["employee_id"], {"status": DEPLOYED, "projects": []})
            slot["projects"].append({"project_id": r["project_id"], "project": r["project"], "customer": r["customer"]})
    else:
        live = deployment_by_employee(db, {e["employee_id"] for e in current}, today)
    for e in current:
        d = live.get(e["employee_id"], {})
        e["segment"] = "deployed" if d.get("status") == DEPLOYED else "bench"
        e["status_label"] = "Deployed" if e["segment"] == "deployed" else "Bench"
        e["projects"] = ", ".join(p.get("project") or p.get("name") or "" for p in d.get("projects", []) if isinstance(p, dict)) or None
    people = ex._people(db, period, today)
    roles = [{"label": r["role"], "deployed": r["deployed"], "bench": r["bench"]} for r in people["by_role"]]
    bench_cost = sum(e["cost_month"] or 0 for e in current if e["segment"] == "bench")
    default = scope if scope in ("deployed", "bench") else None
    when = f"on {as_of:%d %b %Y}" if past else "today"
    title = {"deployed": f"Deployed {when}", "bench": "On the bench"}.get(scope or "", "Headcount")
    return {
        "title": title, "question": f"Everyone on the rolls {when} — who is billing and who is waiting for work",
        "kpis": [
            kpi("headcount", "Headcount", len(current), "int", "brand"),
            kpi("deployed", "Deployed", sum(1 for e in current if e["segment"] == "deployed"), "int", "success"),
            kpi("bench", "On the bench", sum(1 for e in current if e["segment"] == "bench"), "int", "warning"),
            kpi("utilisation", "Utilisation", people["headline"]["utilisation_pct"], "pct"),
            kpi("bench_cost", "Bench cost / month", round(bench_cost, 2), tone="danger" if bench_cost else "success"),
            kpi("notice", "On notice", sum(1 for e in current if e["on_notice"]), "int", "warning"),
        ],
        "charts": [
            chart("monthly", "Deployed vs bench at month end", [{"label": m["label"], "key": m["key"], "deployed": m["deployed"] or 0,
                                                                 "bench": m["bench"] or 0} for m in people["monthly"] if not m["future"]],
                  [series("deployed", "Deployed", "success", "h"), series("bench", "Bench", "warning", "h")], fmt="int"),
            chart("roles", "By designation", top_rows(roles, "deployed", ["deployed", "bench"]),
                  [series("deployed", "Deployed", "success", "h"), series("bench", "Bench", "warning", "h")], fmt="int"),
            chart("tenure", "Tenure", people["tenure"], [series("deployed", "Deployed", "success", "t"),
                                                         series("bench", "Bench", "warning", "t")], fmt="int"),
        ],
        "segments": segments(current, [("deployed", "Deployed", "success"), ("bench", "Bench", "warning")], "cost_month"),
        "default_segment": default,
        "tables": [table("people", "People", [
            col("employee", "Employee", link="link"), col("employee_code", "Emp ID"), col("role", "Designation"),
            col("status_label", "Status", "badge"), col("projects", "Working on"), col("doj", "Joined", "date"),
            col("tenure_years", "Tenure (yrs)", "num"), col("cost_month", "Cost / month", "money"),
            col("exit_on", "Last day", "date"),
        ], sorted(current, key=lambda e: (e["segment"] != "bench", -(e["cost_month"] or 0))))],
        "related": [{"metric": "joiners", "label": "Joiners"}, {"metric": "exits", "label": "Exits"},
                    {"metric": "rolloffs", "label": "Roll-offs"}],
        "notes": ["Headcount = active employees not past their last working day. Deployed = on a live assignment today."],
    }


def _emp_table_rows(rows: list[dict], seg: str) -> list[dict]:
    return [{**r, "link": f"employees/{r['employee_id']}", "segment": seg,
             "projects": ", ".join(p.get("project") or p.get("name") or "" for p in r.get("projects") or [] if isinstance(p, dict)) or None}
            for r in rows]


def m_joiners(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    p = ex._people(db, period, today)
    rows = _emp_table_rows(p["joiners"], "joined")
    return {
        "title": "Joiners", "question": f"Who joined Karnex in {period.label}",
        "kpis": [kpi("joiners", "Joined", len(rows), "int", "success"),
                 kpi("net", "Net change", p["headline"]["net_change"], "int"),
                 kpi("cost", "Added cost / month", round(sum(r["cost_month"] or 0 for r in rows), 2))],
        "charts": [chart("monthly", "Joiners vs exits", [{"label": m["label"], "key": m["key"], "joiners": m["joiners"], "exits": m["exits"]}
                                                         for m in p["monthly"]],
                         [series("joiners", "Joined", "success"), series("exits", "Left", "danger")], fmt="int")],
        "segments": [], "default_segment": None,
        "tables": [table("joiners", "Joiners", [
            col("employee", "Employee", link="link"), col("employee_code", "Emp ID"), col("role", "Designation"),
            col("doj", "Joined", "date"), col("projects", "Working on"), col("cost_month", "Cost / month", "money"),
        ], rows)],
        "related": [{"metric": "exits", "label": "Exits"}, {"metric": "headcount", "label": "Headcount"}],
        "notes": ["Dated by the employee's joining date."],
    }


def m_exits(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    p = ex._people(db, period, today)
    rows = _emp_table_rows(p["exits"], "left") + _emp_table_rows(p["on_notice"], "notice")
    h = p["headline"]
    return {
        "title": "Exits & attrition", "question": "Who left, who is serving notice, and what attrition looks like",
        "kpis": [kpi("exits", "Left", h["exits"], "int", "danger"),
                 kpi("notice", "Serving notice", h["on_notice"], "int", "warning"),
                 kpi("attrition", "Attrition", h["attrition_pct"], "pct",
                     "danger" if (h["attrition_pct"] or 0) >= ex.ATTRITION_WARN_PCT else "neutral",
                     "exits ÷ average headcount in the period"),
                 kpi("tenure", "Avg tenure today", h["avg_tenure_years"], "num", sub="years")],
        "charts": [chart("monthly", "Exits month by month", [{"label": m["label"], "key": m["key"], "exits": m["exits"], "joiners": m["joiners"]}
                                                             for m in p["monthly"]],
                         [series("exits", "Left", "danger"), series("joiners", "Joined", "success", off=True)], fmt="int")],
        "segments": segments(rows, [("left", "Left", "danger"), ("notice", "Serving notice", "warning")]),
        "default_segment": "notice" if scope == "notice" else None,
        "tables": [table("people", "People", [
            col("employee", "Employee", link="link"), col("employee_code", "Emp ID"), col("role", "Designation"),
            col("doj", "Joined", "date"), col("exit_on", "Last day", "date"), col("projects", "Working on"),
            col("cost_month", "Cost / month", "money"),
        ], rows)],
        "related": [{"metric": "headcount", "label": "Headcount"}, {"metric": "rolloffs", "label": "Roll-offs"}],
        "notes": ["Left = last working day (else resignation date) inside the period and already past."],
    }


def m_rolloffs(db, period: Period, today: date, customer_id=None, project_id=None, scope=None) -> dict:
    p = ex._people(db, period, today)
    rows = [{**r, "link": f"employees/{r['employee_id']}", "pe_link": f"project-employees/{r['project_employee_id']}",
             "project_link": f"projects/{r['project_id']}",
             "ends_label": "Project closes" if r.get("ends_by") == "project_close" else "PO expires",
             "segment": "soon" if (r.get("days_left") is not None and r["days_left"] <= 30) else "later"}
            for r in p["rolloffs"]]
    rows.sort(key=lambda r: r.get("days_left") if r.get("days_left") is not None else 9999)
    return {
        "title": "Roll-offs — next 90 days", "question": "Whose cover ends soon, and how much monthly billing stops with it",
        "kpis": [kpi("rolloffs", "Rolling off", len(rows), "int", "warning"),
                 kpi("soon", "Within 30 days", sum(1 for r in rows if r["segment"] == "soon"), "int", "danger"),
                 kpi("rate", "Monthly billing at risk", round(sum(m["monthly_rate"] for m in p["rolloffs_by_month"]), 2), tone="warning")],
        "charts": [chart("months", "Roll-offs by month", p["rolloffs_by_month"],
                         [series("heads", "Heads", "warning"), series("monthly_rate", "Monthly rate at risk", "danger", off=True)], fmt="int")],
        "segments": segments(rows, [("soon", "Within 30 days", "danger"), ("later", "31–90 days", "warning")]),
        "default_segment": None,
        "tables": [table("rolloffs", "Roll-offs", [
            col("employee_name", "Employee", link="link"), col("customer_name", "Customer"),
            col("project_name", "Project", link="project_link"), col("role_title", "Role"),
            col("po_end_date", "Cover ends", "date"), col("days_left", "Days left", "days"), col("ends_label", "Why"),
        ], rows)],
        "related": [{"metric": "headcount", "label": "Headcount", "scope": "bench"}, {"metric": "po", "label": "Purchase orders"}],
        "notes": ["Cover ends at the earlier of the project's last working day and the latest PO end date."],
    }


# --------------------------------------------------------------------------- registry


METRICS = {
    # finance
    "billed": ("finance", m_billed), "collected": ("finance", m_collected),
    "outstanding": ("finance", m_outstanding), "overdue": ("finance", m_overdue),
    "margin": ("finance", m_margin), "target": ("finance", m_target), "dso": ("finance", m_dso),
    "po": ("finance", m_po),
    # customer
    "customers": ("customer", m_customers), "deployed": ("customer", m_deployed),
    # sales
    "positions": ("sales", m_positions), "onboarded": ("sales", m_onboarded), "stages": ("sales", m_stages),
    # people
    "headcount": ("people", m_headcount), "joiners": ("people", m_joiners), "exits": ("people", m_exits),
    "rolloffs": ("people", m_rolloffs),
}
FINANCE_METRICS = frozenset(k for k, (tab, _f) in METRICS.items() if tab == "finance")


def drilldown(db: Session, metric: str, month_raw: str | None = None, period_raw: str | None = None,
              today: date | None = None, customer_id: int | None = None, project_id: int | None = None,
              scope: str | None = None) -> dict:
    """One drill-down page. Raises ValueError on an unknown metric / bad period."""
    metric = (metric or "").strip().lower()
    if metric not in METRICS:
        raise ValueError(f"Unknown metric {metric!r}; expected one of {', '.join(sorted(METRICS))}")
    today = today or date.today()
    period = Period.parse(period_raw or ex.DEFAULT_PERIOD, month_raw, today)
    tab, fn = METRICS[metric]
    body = fn(db, period, today, customer_id, project_id, (scope or "").strip().lower() or None)
    return {"metric": metric, "tab": tab, "as_of": today.isoformat(), "period": ex._period_view(period, today),
            "filters": {"customer_id": customer_id, "project_id": project_id, "scope": scope},
            "segments": [], "default_segment": None, "related": [], "notes": [], **body}
