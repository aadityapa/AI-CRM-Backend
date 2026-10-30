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
    ("partially_paid", "Partly paid — balance due"),
    ("paid", "Paid"),
)
STAGE_LABELS = dict(STAGES)
#: Stages that are somebody's move (the rest are information).
OPEN_STAGES = {"awaiting_proforma", "returned", "proforma", "awaiting_customer", "irn_pending", "unpaid",
               "partially_paid"}

MAX_LIMIT = 100
#: Payment terms assumed when an invoice has no due date (the Revenue report's
#: and "Who owes us"'s rule — the three never disagree).
DEFAULT_TERMS_DAYS = 30
#: Days-late buckets — same keys as `finance_reports.LATE_BUCKETS`.
LATE_BUCKETS: tuple[tuple[str, str, int, int | None], ...] = (
    ("not_due", "Not yet due", -10**6, 0),
    ("late_1_30", "1–30 days late", 1, 30),
    ("late_31_60", "31–60 days late", 31, 60),
    ("late_61_90", "61–90 days late", 61, 90),
    ("late_90_plus", "Over 90 days late", 91, None),
)
LATE_KEYS = {k for k, *_ in LATE_BUCKETS} | {"late"}
#: Stages whose invoice is out with the customer — they can be late.
MONEY_STAGES = ("awaiting_customer", "irn_pending", "unpaid", "partially_paid")
#: 26 Oct 2026 — "every generated invoice" (Finance + Sales Manager / Head):
#: the stage group `invoiced` = every issued TAX invoice, paid or not.
INVOICED_STAGES = MONEY_STAGES + ("paid",)
#: Pseudo stages a filter may name besides the STAGES keys.
STAGE_GROUPS = {"open", "invoiced"}
#: Quick switches (24 Oct 2026, Finance filters).
FLAGS = ("split", "tds_pending", "no_branch", "overdue")
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


def late_bucket(days_late: int) -> str:
    """PURE. Which days-late bucket `days_late` falls in."""
    for key, _label, lo, hi in LATE_BUCKETS:
        if days_late >= lo and (hi is None or days_late <= hi):
            return key
    return LATE_BUCKETS[-1][0]


def days_late(due: date | None, invoice_date: date | None, today: date) -> int | None:
    """PURE. Days past the due date — the invoice's own, else invoice date + 30."""
    from datetime import timedelta

    if due is None and invoice_date is not None:
        due = invoice_date + timedelta(days=DEFAULT_TERMS_DAYS)
    if due is None:
        return None
    return (today - due).days


def _status(value) -> str:
    return str(getattr(value, "value", value) or "")


def stage_of(*, kind: str | None, returned: bool, customer_approved: bool, irn: bool,
             payment_status: str | None, money_in: bool = False) -> str:
    """PURE. Where one piece of billed work stands. `kind` None = no invoice yet.
    Same order as `billing_rules.invoice_stage` (24 Oct 2026): money on an
    invoice (cash or TDS) puts it in Partly paid, whatever came before."""
    if kind is None:
        return "awaiting_proforma"
    if kind == "Proforma":
        return "returned" if returned else "proforma"
    if (payment_status or "").lower() == "paid":
        return "paid"
    if money_in:
        return "partially_paid"
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
    # 24 Oct 2026 — Finance's filters.
    branch_id: int | None = None      # 0 = no branch on record
    project_id: int | None = None
    employee_id: int | None = None
    po_id: int | None = None          # 0 = no PO
    owner_id: int | None = None       # the Sales person who raised the deal
    late: str | None = None           # a LATE_BUCKETS key, or "late" (any days late)
    date_field: str = "invoice"       # invoice | due — what date_from / date_to read
    date_from: date | None = None
    date_to: date | None = None
    amount_min: float | None = None   # the row's amount (grand total, else what is left to bill)
    waiting_min: int | None = None    # days at the current stage
    flags: tuple[str, ...] = ()       # FLAGS


def parse_flags(raw: str | None) -> tuple[str, ...]:
    """PURE. "split,tds_pending" → ("split", "tds_pending"); ValueError on an unknown one."""
    out = tuple(x.strip() for x in (raw or "").split(",") if x.strip())
    bad = [x for x in out if x not in FLAGS]
    if bad:
        raise ValueError(f"unknown flag {bad[0]!r}")
    return out


def stage_since(stage: str, *, approved_at, created_at, returned_at, issued_at, invoice_date,
                customer_approved_at, irn_recorded_at):
    """PURE. When the row entered its current stage — "waiting N days" counts from here.
    Paid has no clock."""
    pick = {
        "awaiting_proforma": approved_at,
        "returned": returned_at,
        "proforma": created_at,
        "awaiting_customer": issued_at or invoice_date,
        "irn_pending": customer_approved_at,
        "unpaid": irn_recorded_at or customer_approved_at or issued_at or invoice_date,
        "partially_paid": irn_recorded_at or issued_at or invoice_date,
    }.get(stage)
    return pick


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
    from services.billing_rules import invoice_customer_id, invoice_window_clause, work_month_clause

    customers: dict[int, str] = {}
    months: set[str] = set()
    for pid, y, m in db.execute(select(Timesheet.project_id, Timesheet.year, Timesheet.month)
                                .where(Timesheet.status == TimesheetStatus.APPROVED,
                                       work_month_clause(Timesheet.year, Timesheet.month))
                                .group_by(Timesheet.project_id, Timesheet.year, Timesheet.month)).all():
        cid = projects.get(pid, (None, None))[1]
        if cid:
            customers[cid] = cust_names.get(cid) or f"Customer {cid}"
        if month_key(y, m):
            months.add(month_key(y, m))
    for pid, po_id, d, tsid in db.execute(select(Invoice.project_id, Invoice.po_id, Invoice.invoice_date,
                                                 Invoice.timesheet_id).where(invoice_window_clause())).all():
        # 27 Oct 2026: the PO's customer first — the ONE rule (`billing_rules.invoice_customer_id`).
        cid = invoice_customer_id(projects.get(pid, (None, None))[1], po_cust.get(po_id))
        if cid:
            customers[cid] = cust_names.get(cid) or f"Customer {cid}"
        if tsid is None and d is not None:
            months.add(month_key(d.year, d.month))
    return customers, months


def billing_tracker(db: Session, f: TrackerFilters, *, export: bool = False) -> dict:
    """{rows, meta: {total, page, limit, counts, options}} — see the module doc.
    `export` returns EVERY filtered row (the CSV), not one page."""
    from models import Customer, Employee, Invoice, Project, PurchaseOrder, Timesheet, TimesheetStatus

    if f.stage and f.stage not in STAGE_LABELS and f.stage not in STAGE_GROUPS:
        raise ValueError("unknown stage")
    ym = _parse_month(f.month)
    limit = max(1, min(int(f.limit or 25), MAX_LIMIT))
    page = max(1, int(f.page or 1))

    if f.late and f.late not in LATE_KEYS:
        raise ValueError("unknown late bucket")
    if f.date_field not in ("invoice", "due"):
        raise ValueError("date_field must be invoice or due")
    if f.date_from and f.date_to and f.date_to < f.date_from:
        raise ValueError("date_to is before date_from")
    today = date.today()

    from models import CustomerBranch, Opportunity, TdsRecord

    # Project → (name, customer id); PO → customer. Small tables.
    projects = {}
    proj_branch: dict[int, int | None] = {}
    proj_opp: dict[int, int | None] = {}
    for pid, pname, cid, bid, oid in db.execute(
            select(Project.id, Project.name, Project.customer_id, Project.branch_id, Project.opportunity_id)).all():
        projects[pid] = (pname, cid)
        proj_branch[pid] = bid
        proj_opp[pid] = oid
    po_cust: dict[int, int] = {}
    po_branch: dict[int, int | None] = {}
    po_number: dict[int, str] = {}
    for po_id, cid, bid, num in db.execute(select(PurchaseOrder.id, PurchaseOrder.customer_id,
                                                  PurchaseOrder.billing_branch_id, PurchaseOrder.po_number)).all():
        po_cust[po_id], po_branch[po_id], po_number[po_id] = cid, bid, num
    cust_names = dict(db.execute(
        select(Customer.id, func.coalesce(Customer.legal_entity_name, Customer.name))).all())
    branch_names = dict(db.execute(select(CustomerBranch.id, CustomerBranch.branch_name)).all())
    opp_owner = dict(db.execute(select(Opportunity.id, Opportunity.created_by).where(
        Opportunity.id.in_({o for o in proj_opp.values() if o}))).all()) if any(proj_opp.values()) else {}
    from services.revenue_report import _user_names
    owner_names = _user_names(db, {u for u in opp_owner.values() if u})

    # 1 — approved timesheets (+ their one invoice document).
    # 23 Oct 2026: from the data start work month (Settings ▸ Billing), like every list.
    from services.billing_rules import invoice_window_clause, work_month_clause

    ts_q = (select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.status == TimesheetStatus.APPROVED,
                   work_month_clause(Timesheet.year, Timesheet.month)))
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
    inv_q = select(Invoice).where(invoice_window_clause(), or_(
        Invoice.timesheet_id.is_(None),
        Invoice.timesheet_id.notin_(select(Timesheet.id).where(Timesheet.status == TimesheetStatus.APPROVED))))
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

    inv_ids = [i.id for docs_ in docs.values() for i in docs_] + [i.id for i in loose]
    tds_open: dict[int, float] = {}
    if inv_ids:
        for iid, bal, st_ in db.execute(select(TdsRecord.invoice_id, TdsRecord.tds_balance, TdsRecord.tds_status)
                                        .where(TdsRecord.invoice_id.in_(inv_ids))).all():
            if _status(st_).lower() != "paid" and float(bal or 0) > 0:
                tds_open[iid] = float(bal or 0)

    def name(emp) -> str | None:
        if emp is None:
            return None
        return " ".join(x for x in (emp.first_name, emp.last_name) if x) or None

    def row(ts, emp, inv, rest: float | None = None) -> dict:
        pid = (inv.project_id if inv is not None else None) or (ts.project_id if ts is not None else None)
        pname, pcid = projects.get(pid, (None, None))
        # 27 Oct 2026: ONE customer per invoice on every screen — the PO's, else
        # the project's (the Invoices list's rule; the tracker used to put the
        # project's first, so the same invoice changed customer between screens).
        from services.billing_rules import invoice_customer_id
        cid = invoice_customer_id(pcid, po_cust.get(inv.po_id) if inv is not None and inv.po_id else None)
        if ts is not None:
            mkey = month_key(ts.year, ts.month)
            if inv is not None and getattr(inv, "split_start", None) and getattr(inv, "split_end", None):
                # 0142 — a part split BY DATES names its own days.
                period = period_label(ts.year, ts.month, inv.split_start, inv.split_end)
            else:
                period = period_label(ts.year, ts.month, ts.period_start_date, ts.period_end_date)
        else:
            mkey = month_key(inv.invoice_date.year, inv.invoice_date.month) if inv.invoice_date else None
            period = None
        kind = (inv.kind or "Tax") if inv is not None else None
        st = stage_of(kind=kind, returned=bool(inv is not None and inv.returned_at),
                      customer_approved=bool(inv is not None and inv.customer_approved_at),
                      irn=bool(inv is not None and inv.irn_number),
                      payment_status=_status(inv.payment_status) if inv is not None else None,
                      money_in=bool(inv is not None and (float(inv.paid_amount or 0) > 0
                                                         or float(getattr(inv, "tds_deducted", 0) or 0) > 0)))
        part = getattr(inv, "split_no", None) if inv is not None else None
        po_id = inv.po_id if inv is not None else None
        bid = proj_branch.get(pid) or (po_branch.get(po_id) if po_id else None)
        owner = opp_owner.get(proj_opp.get(pid)) if pid else None
        late_days = (days_late(inv.due_date, inv.invoice_date, today)
                     if inv is not None and st in MONEY_STAGES and float(inv.balance_amount or 0) > 0 else None)
        # 27 Oct 2026: "overdue" = days late > 0 — the SAME rule as the days-late
        # select and the Invoices "Overdue" status (due date, else invoice date
        # + 30). It used to need a stored due date, so the two disagreed.
        overdue = late_days is not None and late_days > 0
        from services.billing_rules import effective_due
        eff_due = effective_due(inv.due_date, inv.invoice_date) if inv is not None else None
        since = stage_since(st, approved_at=ts.approved_at if ts is not None else None,
                            created_at=getattr(inv, "created_at", None) or (inv.invoice_date if inv is not None else None),
                            returned_at=inv.returned_at if inv is not None else None,
                            issued_at=inv.issued_at if inv is not None else None,
                            invoice_date=inv.invoice_date if inv is not None else None,
                            customer_approved_at=inv.customer_approved_at if inv is not None else None,
                            irn_recorded_at=inv.irn_recorded_at if inv is not None else None)
        since_day = since.date() if hasattr(since, "date") and callable(since.date) else since
        waiting = (today - since_day).days if isinstance(since_day, date) else None
        amount = float(inv.grand_total or 0) if inv is not None else float(rest or 0) if rest is not None else None
        return {
            "branch_id": bid, "branch": branch_names.get(bid) if bid else None,
            "po_id": po_id, "po_number": po_number.get(po_id) if po_id else None,
            "owner_id": owner, "owner": owner_names.get(owner) if owner else None,
            "days_late": late_days,
            "late_bucket": late_bucket(late_days) if late_days is not None else None,
            "waiting_days": max(0, waiting) if waiting is not None and st != "paid" else None,
            "amount": amount,
            "tds_pending": tds_open.get(inv.id) if inv is not None else None,
            "split": bool(part) or rest is not None,
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
            # The due date the date filter / days late use (own, else invoice date + 30).
            "effective_due": eff_due.isoformat() if eff_due else None,
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
    # Active customers only in the dropdown (22 Oct 2026, user rule); the rows of
    # an inactive customer still show under "All customers".
    from services.customers import active_customer_ids
    active_ids = active_customer_ids(db)
    if active_ids:
        customers_seen = {cid: n for cid, n in customers_seen.items() if cid in active_ids}
    # Customer + search + every filter narrow everything (counts included); the
    # stage narrows the rows only.
    if f.customer_id is not None:
        rows = [r for r in rows if r["customer_id"] == f.customer_id]
    if f.search and f.search.strip():
        q = f.search.strip().lower()
        rows = [r for r in rows if q in " ".join(str(r.get(k) or "") for k in (
            "customer", "project", "employee", "employee_code", "invoice_number", "proforma_number",
            "month_label", "period_label", "branch", "po_number", "owner")).lower()]
    # The pickers list what the customer (and month) holds — before the other
    # filters, so choosing a project never empties its own dropdown.
    options_extra = _row_options(rows)
    rows = apply_filters(rows, f)

    counts = {k: 0 for k, _ in STAGES}
    amounts = {k: 0.0 for k, _ in STAGES}
    for r in rows:
        counts[r["stage"]] += 1
        amounts[r["stage"]] += float(r["grand_total"] or 0)
    counts["all"] = len(rows)
    counts["open"] = sum(counts[k] for k in OPEN_STAGES)
    counts["invoiced"] = sum(counts[k] for k in INVOICED_STAGES)
    amounts["invoiced"] = sum(amounts[k] for k in INVOICED_STAGES)
    # 27 Oct 2026: the header pill's count and amount come from the SAME rows
    # (every stage, after the filters) — it printed "1 overdue · ₹354" with the
    # ₹ taken from three rows of one stage.
    overdue = sum(1 for r in rows if r["overdue"])
    late_total = round(sum(float(r["balance"] or 0) for r in rows if r["overdue"]), 2)
    summary = tracker_summary(rows)

    if f.stage == "open":
        rows = [r for r in rows if r["stage"] in OPEN_STAGES]
    elif f.stage == "invoiced":
        rows = [r for r in rows if r["stage"] in INVOICED_STAGES]
        # A register reads newest invoice first.
        rows.sort(key=lambda r: (r["invoice_date"] or "", r["invoice_number"] or ""), reverse=True)
    elif f.stage in GROUP_STAGES:
        rows = [r for r in rows if r["stage"] in GROUP_STAGES[f.stage]]
    elif f.stage:
        rows = [r for r in rows if r["stage"] == f.stage]
    if f.stage != "invoiced":
        # Newest work first; within a month, by customer then employee.
        rows.sort(key=lambda r: ((r["month"] or ""), (r["customer"] or "").lower(),
                                 (r["employee"] or "").lower()), reverse=False)
        rows.sort(key=lambda r: r["month"] or "", reverse=True)
    total = len(rows)
    out = rows if export else rows[(page - 1) * limit: page * limit]
    return {"rows": out, "meta": {
        "total": total, "page": page, "limit": limit, "pages": max(1, -(-total // limit)),
        "counts": counts, "amounts": {k: round(v, 2) for k, v in amounts.items()}, "overdue": overdue,
        "stages": [{"key": k, "label": v, "open": k in OPEN_STAGES} for k, v in STAGES],
        "options": {
            "customers": sorted(({"id": k, "name": v} for k, v in customers_seen.items()),
                                key=lambda c: c["name"].lower()),
            "months": [{"key": m, "label": month_label(m)} for m in sorted(months_seen, reverse=True)],
            **options_extra,
            "late": [{"key": k, "label": v} for k, v, *_ in LATE_BUCKETS],
            "flags": list(FLAGS),
        },
        "late_total": late_total,
        "summary": summary,
    }}


#: Stage groups the Billing Tracker tab charts (27 Oct 2026, CEO dashboard redesign).
SUMMARY_GROUPS = (
    ("to_raise", "Proforma to raise / with Finance", ("awaiting_proforma", "returned", "proforma")),
    ("with_customer", "With the customer (approval · IRN)", ("awaiting_customer", "irn_pending")),
    ("due", "Payment due", ("unpaid", "partially_paid")),
    ("paid", "Paid", ("paid",)),
)
#: 29 Oct 2026 — a bar of "How the work moved" is a stage GROUP, so the table can be
#: narrowed to exactly what the bar counted (`?stage=to_raise|with_customer|due`).
GROUP_STAGES: dict[str, frozenset[str]] = {k: frozenset(sts) for k, _label, sts in SUMMARY_GROUPS if k != "paid"}
STAGE_GROUPS.update(GROUP_STAGES)


def tracker_summary(rows: list[dict]) -> dict:
    """Tiles + chart data for the Billing Tracker tab, over the SAME filtered rows the
    table and chips count (every stage, before the stage chip) — PURE.

    `by_month` counts pieces of work per stage group (a sheet still to be invoiced
    has no amount yet, so counts are the one unit every stage shares); the money
    charts use what an invoice says — balance for what is owed, total for what is paid."""
    group_of = {st: key for key, _label, sts in SUMMARY_GROUPS for st in sts}
    by_month: dict[str, dict] = {}
    for r in rows:
        key = r.get("month") or ""
        slot = by_month.setdefault(key, {"key": key, "label": _short_month(key), **{g: 0 for g, *_ in SUMMARY_GROUPS}})
        g = group_of.get(r.get("stage"))
        if g:
            slot[g] += 1
    months = [by_month[k] for k in sorted(by_month) if k][-12:]

    def money(r, key):
        return float(r.get(key) or 0)
    late = [{"key": k, "label": label,
             "balance": round(sum(money(r, "balance") for r in rows if r.get("late_bucket") == k), 2),
             "count": sum(1 for r in rows if r.get("late_bucket") == k)} for k, label, *_ in LATE_BUCKETS]
    cust: dict[str, dict] = {}
    for r in rows:
        name = r.get("customer") or "—"
        slot = cust.setdefault(name, {"label": name, "customer_id": r.get("customer_id"),
                                      "due": 0.0, "paid": 0.0, "with_customer": 0.0})
        g = group_of.get(r.get("stage"))
        if g == "due":
            slot["due"] += money(r, "balance")
        elif g == "paid":
            slot["paid"] += money(r, "grand_total")
        elif g == "with_customer":
            slot["with_customer"] += money(r, "grand_total")
    customers = sorted(cust.values(), key=lambda c: -(c["due"] + c["with_customer"] + c["paid"]))[:10]
    for c in customers:
        for k in ("due", "paid", "with_customer"):
            c[k] = round(c[k], 2)

    def grp(g):
        return [r for r in rows if group_of.get(r.get("stage")) == g]
    to_raise, with_cust, due, paid = grp("to_raise"), grp("with_customer"), grp("due"), grp("paid")
    waits = [r["waiting_days"] for r in rows if r.get("waiting_days") is not None and r.get("stage") != "paid"]
    return {
        "tiles": {
            "to_raise": len(to_raise),
            "to_raise_returned": sum(1 for r in to_raise if r.get("stage") == "returned"),
            "with_customer": len(with_cust),
            "with_customer_amount": round(sum(money(r, "grand_total") for r in with_cust), 2),
            "due_amount": round(sum(money(r, "balance") for r in due), 2),
            "due_count": len(due),
            "overdue_amount": round(sum(money(r, "balance") for r in rows if r.get("overdue")), 2),
            "overdue_count": sum(1 for r in rows if r.get("overdue")),
            "paid_amount": round(sum(money(r, "grand_total") for r in paid), 2),
            "paid_count": len(paid),
            "tds_pending": round(sum(money(r, "tds_pending") for r in rows), 2),
            "avg_wait_days": round(sum(waits) / len(waits), 1) if waits else None,
        },
        "groups": [{"key": k, "label": label} for k, label, _ in SUMMARY_GROUPS],
        "by_month": months,
        "late": late,
        "customers": customers,
    }


def _short_month(key: str) -> str:
    try:
        y, m = key.split("-")
        return f"{_MONTHS[int(m) - 1]} {y[2:]}"
    except (ValueError, IndexError):
        return key or "—"


#: The CSV's columns (26 Oct 2026 — "take it from there whenever they want").
CSV_COLUMNS = (
    ("month_label", "Work month"), ("period_label", "Period"), ("customer", "Customer"),
    ("branch", "Branch"), ("project", "Project"), ("employee", "Employee"), ("employee_code", "Emp ID"),
    ("owner", "Sales owner"), ("invoice_kind", "Document"), ("invoice_number", "Invoice / PI no."),
    ("proforma_number", "PI no."), ("part_label", "Part"), ("po_number", "PO"),
    ("invoice_date", "Invoice date"), ("due_date", "Due date"), ("sub_total", "Value before GST"),
    ("grand_total", "Invoice total"), ("balance", "Balance"), ("tds_pending", "TDS pending"),
    ("stage_label", "Where it stands"), ("days_late", "Days late"), ("payment_status", "Payment status"),
    ("returned_reason", "Finance's return reason"),
)


def to_csv(rows: list[dict]) -> str:
    """PURE. The tracker rows as CSV, formula injection neutralised."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _k, label in CSV_COLUMNS])
    for r in rows:
        out = []
        for key, _label in CSV_COLUMNS:
            v = r.get(key)
            v = "" if v is None else v
            if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
                v = "'" + v
            out.append(v)
        w.writerow(out)
    return buf.getvalue()


def _row_options(rows: list[dict]) -> dict:
    """PURE. The branch / project / employee / PO / Sales owner choices the rows hold."""
    def pick(id_key: str, name_key: str) -> list[dict]:
        seen: dict = {}
        for r in rows:
            if r.get(id_key) is not None:
                seen.setdefault(r[id_key], r.get(name_key) or f"#{r[id_key]}")
        return sorted(({"id": k, "name": v} for k, v in seen.items()), key=lambda x: str(x["name"]).lower())

    projects = pick("project_id", "project")
    cust_of = {r["project_id"]: r["customer_id"] for r in rows if r.get("project_id")}
    for p in projects:
        p["customer_id"] = cust_of.get(p["id"])
    return {
        "branches": pick("branch_id", "branch"),
        "no_branch": sum(1 for r in rows if r.get("branch_id") is None),
        "projects": projects,
        "employees": pick("employee_id", "employee"),
        "pos": pick("po_id", "po_number"),
        "owners": pick("owner_id", "owner"),
    }


def apply_filters(rows: list[dict], f: TrackerFilters) -> list[dict]:
    """PURE. Finance's filters (24 Oct 2026) over the tracker rows."""
    def keep(r: dict) -> bool:
        if f.branch_id is not None and (r["branch_id"] or 0) != f.branch_id:
            return False
        if f.project_id is not None and r["project_id"] != f.project_id:
            return False
        if f.employee_id is not None and r["employee_id"] != f.employee_id:
            return False
        if f.po_id is not None and (r["po_id"] or 0) != f.po_id:
            return False
        if f.owner_id is not None and r["owner_id"] != f.owner_id:
            return False
        if f.late:
            if r["days_late"] is None:
                return False
            if f.late == "late" and r["days_late"] <= 0:
                return False
            if f.late != "late" and r["late_bucket"] != f.late:
                return False
        if f.date_from or f.date_to:
            # A due-date range reads the EFFECTIVE due date (own, else invoice
            # date + 30) — the overdue rule — so an invoice with no stored due
            # date is never dropped from it.
            raw = r["invoice_date"] if f.date_field == "invoice" else r.get("effective_due")
            if not raw:
                return False
            d = date.fromisoformat(raw)
            if (f.date_from and d < f.date_from) or (f.date_to and d > f.date_to):
                return False
        if f.amount_min is not None and (r["amount"] or 0) < f.amount_min:
            return False
        if f.waiting_min is not None and (r["waiting_days"] is None or r["waiting_days"] < f.waiting_min):
            return False
        for flag in f.flags:
            if flag == "split" and not r["split"]:
                return False
            if flag == "tds_pending" and not r["tds_pending"]:
                return False
            if flag == "no_branch" and r["branch_id"] is not None:
                return False
            if flag == "overdue" and not r["overdue"]:
                return False
        return True

    return [r for r in rows if keep(r)]
