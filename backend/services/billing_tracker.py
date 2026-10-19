"""The billing tracker (20 Oct 2026, user ask: "for Finance, Admin and CEO on
the Dashboard, one filter that holds approved timesheets and invoices with
customer & month — I see an invoice approved but not showing there, same for
timesheets").

The work desk's billing tabs are a to-do list: each one shows one step of the
chain, inside a time window, cut to a page. That is right for "what do I do
next", wrong for "where is Aakash's May invoice". This module answers the
second question: EVERY approved timesheet and EVERY invoice, one row per piece
of billed work, no time window, filtered on the server by customer / month /
stage / search and paged — so nothing can fall off a list.

One row is one of:

* an APPROVED timesheet (with the latest invoice document raised from it, if
  any — `invoices.timesheet_id` is unique, so there is at most one);
* an invoice raised without a timesheet (a manual invoice).

The STAGE of a row is where it stands in the chain (PURE `stage_of`):

    awaiting_proforma → proforma → awaiting_customer → irn_pending →
    unpaid → paid            (and returned = Finance sent the Proforma back)

The customer is the project's customer, else the PO's (the invoice list's
rule); the MONTH is the timesheet's month, else the invoice date — the same
rule `services/invoice_facets` gives every invoice screen, so the tracker and
the lists never disagree. A cross-month timesheet carries its period too
("21 Apr – 30 May 2026").
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

#: Stage keys in the order the chain runs; the UI's chips follow it.
STAGES = (
    ("awaiting_proforma", "Approved — Proforma to raise"),
    ("returned", "Proforma returned to GM"),
    ("proforma", "Proforma with Finance"),
    ("awaiting_customer", "Invoice issued — customer to approve"),
    ("irn_pending", "Customer approved — IRN to add"),
    ("unpaid", "Invoice open — payment due"),
    ("paid", "Paid"),
)
STAGE_LABELS = dict(STAGES)
#: Stages that are somebody's move (the rest are information).
OPEN_STAGES = {"awaiting_proforma", "returned", "proforma", "awaiting_customer", "irn_pending", "unpaid"}

MAX_LIMIT = 100
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_key(year: int | None, month: int | None) -> str | None:
    if not year or not month:
        return None
    return f"{int(year):04d}-{int(month):02d}"


def month_label(key: str | None) -> str | None:
    """PURE. "2026-05" → "May 2026"."""
    if not key or len(key) < 7:
        return None
    try:
        return f"{_MONTHS[int(key[5:7]) - 1]} {key[:4]}"
    except (ValueError, IndexError):
        return None


def period_label(year: int, month: int, start: date | None, end: date | None) -> str | None:
    """PURE. The sheet's window ONLY when it is not the calendar month —
    "21 Apr – 30 May 2026"; None for an ordinary month (the month says it)."""
    if not start or not end:
        return None
    from calendar import monthrange

    first, last = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    if start == first and end == last:
        return None
    a = f"{start.day} {_MONTHS[start.month - 1]}" + (f" {start.year}" if start.year != end.year else "")
    return f"{a} – {end.day} {_MONTHS[end.month - 1]} {end.year}"


def _status(value) -> str:
    return str(getattr(value, "value", value) or "")


def stage_of(*, kind: str | None, returned: bool, customer_approved: bool, irn: bool,
             payment_status: str | None) -> str:
    """PURE. Where one piece of billed work stands. `kind` None = no invoice yet."""
    if kind is None:
        return "awaiting_proforma"
    if kind == "Proforma":
        return "returned" if returned else "proforma"
    if (payment_status or "").lower() == "paid":
        return "paid"
    if not customer_approved:
        return "awaiting_customer"
    if not irn:
        return "irn_pending"
    return "unpaid"


@dataclass
class TrackerFilters:
    customer_id: int | None = None
    month: str | None = None          # "YYYY-MM" — the WORK month
    stage: str | None = None          # one STAGES key, "open" (somebody's move) or None
    search: str | None = None
    page: int = 1
    limit: int = 25


def _parse_month(key: str | None) -> tuple[int, int] | None:
    if not key:
        return None
    try:
        y, m = int(key[:4]), int(key[5:7])
        date(y, m, 1)
    except (ValueError, IndexError):
        raise ValueError("month must be YYYY-MM") from None
    return y, m


def _options(db: Session, projects: dict, po_cust: dict, cust_names: dict) -> tuple[dict[int, str], set[str]]:
    """Every customer and work month with billed work — three small grouped queries."""
    from models import Invoice, Timesheet, TimesheetStatus

    customers: dict[int, str] = {}
    months: set[str] = set()
    for pid, y, m in db.execute(select(Timesheet.project_id, Timesheet.year, Timesheet.month)
                                .where(Timesheet.status == TimesheetStatus.APPROVED)
                                .group_by(Timesheet.project_id, Timesheet.year, Timesheet.month)).all():
        cid = projects.get(pid, (None, None))[1]
        if cid:
            customers[cid] = cust_names.get(cid) or f"Customer {cid}"
        if month_key(y, m):
            months.add(month_key(y, m))
    for pid, po_id, d, tsid in db.execute(select(Invoice.project_id, Invoice.po_id, Invoice.invoice_date,
                                                 Invoice.timesheet_id)).all():
        cid = projects.get(pid, (None, None))[1] or po_cust.get(po_id)
        if cid:
            customers[cid] = cust_names.get(cid) or f"Customer {cid}"
        if tsid is None and d is not None:
            months.add(month_key(d.year, d.month))
    return customers, months


def billing_tracker(db: Session, f: TrackerFilters) -> dict:
    """{rows, meta: {total, page, limit, counts, options}} — see the module doc."""
    from models import Customer, Employee, Invoice, Project, PurchaseOrder, Timesheet, TimesheetStatus

    if f.stage and f.stage not in STAGE_LABELS and f.stage != "open":
        raise ValueError("unknown stage")
    ym = _parse_month(f.month)
    limit = max(1, min(int(f.limit or 25), MAX_LIMIT))
    page = max(1, int(f.page or 1))

    # Project → (name, customer id, branch); PO → customer. Small tables.
    projects = {pid: (pname, cid) for pid, pname, cid in db.execute(
        select(Project.id, Project.name, Project.customer_id)).all()}
    po_cust = dict(db.execute(select(PurchaseOrder.id, PurchaseOrder.customer_id)).all())
    cust_names = dict(db.execute(
        select(Customer.id, func.coalesce(Customer.legal_entity_name, Customer.name))).all())

    # 1 — approved timesheets (+ their one invoice document).
    ts_q = (select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.status == TimesheetStatus.APPROVED))
    if ym:
        ts_q = ts_q.where(Timesheet.year == ym[0], Timesheet.month == ym[1])
    sheets = db.execute(ts_q).all()
    # 0141 — a sheet split across POs has several invoices (one per part).
    docs: dict[int, list] = {}
    if sheets:
        for inv in db.execute(select(Invoice).where(
                Invoice.timesheet_id.in_([ts.id for ts, _ in sheets]))
                .order_by(Invoice.split_no.is_(None).desc(), Invoice.split_no, Invoice.id)).scalars():
            docs.setdefault(inv.timesheet_id, []).append(inv)

    # 2 — invoices with no timesheet (manual), month by invoice date. An invoice
    # whose sheet is no longer Approved (rare: reopened after the invoice) is
    # listed on its own as well, so no issued invoice is ever missing.
    covered = {ts.id for ts, _ in sheets}
    inv_q = select(Invoice).where(or_(Invoice.timesheet_id.is_(None),
                                      Invoice.timesheet_id.notin_(select(Timesheet.id).where(
                                          Timesheet.status == TimesheetStatus.APPROVED))))
    if ym:
        first = date(ym[0], ym[1], 1)
        nxt = date(ym[0] + (ym[1] == 12), 1 if ym[1] == 12 else ym[1] + 1, 1)
        inv_q = inv_q.where(or_(
            Invoice.timesheet_id.in_(select(Timesheet.id).where(Timesheet.year == ym[0], Timesheet.month == ym[1])),
            (Invoice.timesheet_id.is_(None)) & (Invoice.invoice_date >= first) & (Invoice.invoice_date < nxt)))
    loose = [i for i in db.execute(inv_q).scalars().all() if i.timesheet_id not in covered]
    loose_sheets = {}
    ids = [i.timesheet_id for i in loose if i.timesheet_id]
    if ids:
        loose_sheets = {ts.id: (ts, emp) for ts, emp in db.execute(
            select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.id.in_(ids))).all()}

    def name(emp) -> str | None:
        if emp is None:
            return None
        return " ".join(x for x in (emp.first_name, emp.last_name) if x) or None

    def row(ts, emp, inv, rest: float | None = None) -> dict:
        pid = (inv.project_id if inv is not None else None) or (ts.project_id if ts is not None else None)
        pname, pcid = projects.get(pid, (None, None))
        cid = pcid or (po_cust.get(inv.po_id) if inv is not None and inv.po_id else None)
        if ts is not None:
            mkey = month_key(ts.year, ts.month)
            period = period_label(ts.year, ts.month, ts.period_start_date, ts.period_end_date)
        else:
            mkey = month_key(inv.invoice_date.year, inv.invoice_date.month) if inv.invoice_date else None
            period = None
        kind = (inv.kind or "Tax") if inv is not None else None
        st = stage_of(kind=kind, returned=bool(inv is not None and inv.returned_at),
                      customer_approved=bool(inv is not None and inv.customer_approved_at),
                      irn=bool(inv is not None and inv.irn_number),
                      payment_status=_status(inv.payment_status) if inv is not None else None)
        overdue = bool(inv is not None and st in ("awaiting_customer", "irn_pending", "unpaid")
                       and inv.due_date and inv.due_date < date.today())
        part = getattr(inv, "split_no", None) if inv is not None else None
        return {
            "key": (f"ts:{ts.id}:{inv.id}" if ts is not None and inv is not None
                    else f"ts:{ts.id}:rest" if ts is not None and rest is not None
                    else f"ts:{ts.id}" if ts is not None else f"inv:{inv.id}"),
            # 0141 — "Part 2" of a sheet split across POs; the unbilled rest of a
            # split sheet is its own row ("Rest to bill").
            "part_label": (f"Part {part}" if part else "Rest to bill" if rest is not None else None),
            "rest_to_bill": rest,
            "stage": st, "stage_label": STAGE_LABELS[st], "overdue": overdue,
            "customer_id": cid, "customer": cust_names.get(cid),
            "project_id": pid, "project": pname,
            "employee_id": getattr(emp, "id", None), "employee": name(emp),
            "employee_code": getattr(emp, "employee_code", None),
            "month": mkey, "month_label": month_label(mkey), "period_label": period,
            "timesheet_id": ts.id if ts is not None else None,
            "timesheet_status": _status(ts.status) if ts is not None else None,
            "approved_at": ts.approved_at.isoformat() if ts is not None and ts.approved_at else None,
            "invoice_id": inv.id if inv is not None else None,
            "invoice_kind": kind,
            "invoice_number": inv.invoice_number if inv is not None else None,
            "proforma_number": inv.proforma_number if inv is not None else None,
            "invoice_date": inv.invoice_date.isoformat() if inv is not None and inv.invoice_date else None,
            "due_date": inv.due_date.isoformat() if inv is not None and inv.due_date else None,
            "sub_total": float(inv.sub_total or 0) if inv is not None else rest,
            "grand_total": float(inv.grand_total or 0) if inv is not None else None,
            "balance": float(inv.balance_amount or 0) if inv is not None else None,
            "payment_status": _status(inv.payment_status) if inv is not None else None,
            "customer_approved_at": inv.customer_approved_at.isoformat()
            if inv is not None and inv.customer_approved_at else None,
            "irn_recorded": bool(inv is not None and inv.irn_number),
            "returned_reason": inv.returned_reason if inv is not None and inv.returned_at else None,
        }

    from services.timesheets import billing_progress
    rows = []
    for ts, emp in sheets:
        sheet_docs = docs.get(ts.id) or []
        if not sheet_docs:
            rows.append(row(ts, emp, None))
            continue
        rows.extend(row(ts, emp, inv) for inv in sheet_docs)
        if any(i.split_no is not None for i in sheet_docs):
            prog = billing_progress(None, ts, sheet_docs)
            left = prog.get("remaining")
            if left is not None and left > 0.01 and not prog.get("returned"):
                rows.append(row(ts, emp, None, rest=float(left)))
    for inv in loose:
        ts, emp = loose_sheets.get(inv.timesheet_id, (None, None))
        rows.append(row(ts, emp, inv))

    # The filter choices list EVERY customer / month that has billed work —
    # never only those left after the current filter (a chosen month must not
    # make the other months disappear from its own dropdown).
    customers_seen, months_seen = _options(db, projects, po_cust, cust_names)
    # Customer + search narrow everything (counts included); the stage narrows the rows only.
    if f.customer_id is not None:
        rows = [r for r in rows if r["customer_id"] == f.customer_id]
    if f.search and f.search.strip():
        q = f.search.strip().lower()
        rows = [r for r in rows if q in " ".join(str(r.get(k) or "") for k in (
            "customer", "project", "employee", "employee_code", "invoice_number", "proforma_number",
            "month_label", "period_label")).lower()]

    counts = {k: 0 for k, _ in STAGES}
    amounts = {k: 0.0 for k, _ in STAGES}
    for r in rows:
        counts[r["stage"]] += 1
        amounts[r["stage"]] += float(r["grand_total"] or 0)
    counts["all"] = len(rows)
    counts["open"] = sum(counts[k] for k in OPEN_STAGES)
    overdue = sum(1 for r in rows if r["overdue"])

    if f.stage == "open":
        rows = [r for r in rows if r["stage"] in OPEN_STAGES]
    elif f.stage:
        rows = [r for r in rows if r["stage"] == f.stage]
    # Newest work first; within a month, by customer then employee.
    rows.sort(key=lambda r: ((r["month"] or ""), (r["customer"] or "").lower(),
                             (r["employee"] or "").lower()), reverse=False)
    rows.sort(key=lambda r: r["month"] or "", reverse=True)
    total = len(rows)
    out = rows[(page - 1) * limit: page * limit]
    return {"rows": out, "meta": {
        "total": total, "page": page, "limit": limit, "pages": max(1, -(-total // limit)),
        "counts": counts, "amounts": {k: round(v, 2) for k, v in amounts.items()}, "overdue": overdue,
        "stages": [{"key": k, "label": v, "open": k in OPEN_STAGES} for k, v in STAGES],
        "options": {
            "customers": sorted(({"id": k, "name": v} for k, v in customers_seen.items()),
                                key=lambda c: c["name"].lower()),
            "months": [{"key": m, "label": month_label(m)} for m in sorted(months_seen, reverse=True)],
        },
    }}
