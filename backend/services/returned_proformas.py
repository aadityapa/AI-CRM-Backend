"""Proformas Finance RETURNED — the Dashboard board (26 Oct 2026, user ask:
"when Finance rejects a Proforma, Sales Manager / Head and RMG / GM see on the
Dashboard which month was returned and why, then check the timesheet, update
and submit").

A returned Proforma stays on its timesheet until the GM raises the sheet again
(the generate route deletes it and reuses its PI number), so "open returns" is
simply every Proforma with `returned_at` set. Each row carries the sheet's
state, so the board says whose move it is:

    to_correct  — Approved, untouched since the return: check the timesheet;
                  reopen it to correct (Sales / GM) or the GM reissues as is
    correcting  — reopened (Draft / Rejected): fill and resubmit
    resubmitted — Submitted: the GM approves
    reissue     — approved again after the return: the GM raises the Proforma
    manual      — a Proforma with no timesheet (raised by hand)

`POST /api/timesheets/{id}/reopen-for-correction` is the "go and fix it" door
(`routers/crm/timesheets.py`); this module only reads.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

#: Who sees the board (by role). Plus whoever may approve / raise from a
#: timesheet or screens as RMG — the GM is a custom role.
VIEW_ROLES = ("Finance", "Sales", "Sales_Head", "RMG")

STEPS = (
    ("to_correct", "Check the timesheet — reopen to correct, or reissue as is", "Sales / GM"),
    ("correcting", "Timesheet reopened — correct it and submit", "Sales"),
    ("resubmitted", "Corrected and submitted — GM to approve", "GM"),
    ("reissue", "Approved again — GM to raise the Proforma", "GM"),
    ("manual", "Raised by hand — no timesheet to correct", "GM / Finance"),
)
STEP_LABELS = {k: (label, who) for k, label, who in STEPS}


def _aware(dt):
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def next_step(ts_status: str | None, approved_at, returned_at, *, has_live_part: bool = False) -> str:
    """PURE. Whose move a returned Proforma is waiting on (see the module doc).

    `has_live_part`: another part of the same sheet is still live (a split
    sheet). The sheet cannot be reopened then (the server refuses while an
    invoice is live), so the returned part goes back to the GM to raise again
    — "reissue", never "to_correct" (27 Oct 2026 filter audit)."""
    if ts_status is None:
        return "manual"
    st = (ts_status or "").lower()
    if st in ("draft", "rejected"):
        return "correcting"
    if st == "submitted":
        return "resubmitted"
    if has_live_part:
        return "reissue"
    a, r = _aware(approved_at), _aware(returned_at)
    if a is not None and r is not None and a > r:
        return "reissue"
    return "to_correct"


def may_see(db: Session, user) -> bool:
    if user.is_admin or user.has_any(*VIEW_ROLES):
        return True
    from services.action_permissions import screens_as_rmg, user_may
    try:
        return (user_may(db, user, "timesheet.approve") or user_may(db, user, "timesheet.generate_invoice")
                or screens_as_rmg(db, user))
    except Exception:  # noqa: BLE001 — a permission read must never 500 the Dashboard
        return False


def may_reopen(db: Session, user) -> bool:
    """Who may send an approved sheet back for correction after Finance returned
    its Proforma: the timesheet's approvers (GM), the people who fill it (Sales
    family), RMG / whoever screens as RMG, Admin / CEO."""
    if user.is_admin or user.has_any("Sales", "Sales_Head", "RMG"):
        return True
    from services.action_permissions import screens_as_rmg, user_may
    try:
        return user_may(db, user, "timesheet.reject") or screens_as_rmg(db, user)
    except Exception:  # noqa: BLE001
        return False


def _status(v) -> str:
    return str(getattr(v, "value", v) or "")


def board(db: Session, user, *, customer_id: int | None = None, month: str | None = None,
          search: str | None = None, step: str | None = None) -> dict:
    """{rows, meta: {counts, total, options, can_reopen, can_reissue}}."""
    from models import Customer, Employee, Invoice, Project, PurchaseOrder, Timesheet
    from models.finance import InvoiceKind
    from services.action_permissions import user_may
    from services.billing_rules import invoice_customer_id, invoice_window_clause
    from services.billing_tracker import _parse_month, month_key, month_label, period_label
    from services.revenue_report import _user_names

    if step and step not in STEP_LABELS:
        raise ValueError("unknown step")
    ym = _parse_month(month)

    invs = db.execute(
        select(Invoice).where(Invoice.kind == InvoiceKind.PROFORMA.value, Invoice.returned_at.isnot(None),
                              invoice_window_clause())
        .order_by(Invoice.returned_at.desc(), Invoice.id.desc())).scalars().all()
    ts_ids = {i.timesheet_id for i in invs if i.timesheet_id}
    sheets = {t.id: (t, e) for t, e in db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.id.in_(ts_ids))).all()} if ts_ids else {}
    pids = {i.project_id for i in invs if i.project_id} | {t.project_id for t, _ in sheets.values()}
    projects = {pid: (n, cid) for pid, n, cid in db.execute(
        select(Project.id, Project.name, Project.customer_id).where(Project.id.in_(pids))).all()} if pids else {}
    po_ids = {i.po_id for i in invs if i.po_id}
    po_rows = {pid: (cid, num) for pid, cid, num in db.execute(
        select(PurchaseOrder.id, PurchaseOrder.customer_id, PurchaseOrder.po_number)
        .where(PurchaseOrder.id.in_(po_ids))).all()} if po_ids else {}
    # Sheets with ANOTHER live (not returned) invoice — a split sheet's other part.
    live_sheets = {t for (t,) in db.execute(
        select(Invoice.timesheet_id).where(Invoice.timesheet_id.in_(ts_ids), Invoice.returned_at.is_(None))
        .distinct()).all()} if ts_ids else set()
    cust_names = dict(db.execute(select(Customer.id, func.coalesce(Customer.legal_entity_name, Customer.name))).all())
    names = _user_names(db, {i.returned_by for i in invs if i.returned_by})
    today = date.today()

    rows: list[dict] = []
    for inv in invs:
        ts, emp = sheets.get(inv.timesheet_id, (None, None))
        pid = inv.project_id or (ts.project_id if ts is not None else None)
        pname, pcid = projects.get(pid, (None, None))
        po_cid, po_num = po_rows.get(inv.po_id, (None, None))
        # ONE customer per invoice — the PO's, else the project's (the Invoices
        # list / billing tracker / work desk rule, billing_rules.invoice_customer_id).
        cid = invoice_customer_id(pcid, po_cid)
        if ts is not None:
            mkey = month_key(ts.year, ts.month)
            if getattr(inv, "split_start", None) and getattr(inv, "split_end", None):
                period = period_label(ts.year, ts.month, inv.split_start, inv.split_end)
            else:
                period = period_label(ts.year, ts.month, ts.period_start_date, ts.period_end_date)
        else:
            mkey = month_key(inv.invoice_date.year, inv.invoice_date.month) if inv.invoice_date else None
            period = None
        key = next_step(_status(ts.status) if ts is not None else None,
                        ts.approved_at if ts is not None else None, inv.returned_at,
                        has_live_part=inv.timesheet_id in live_sheets)
        label, who = STEP_LABELS[key]
        ret = _aware(inv.returned_at)
        rows.append({
            "invoice_id": inv.id,
            "proforma_number": inv.proforma_number or inv.invoice_number,
            "amount": float(inv.grand_total or 0),
            "sub_total": float(inv.sub_total or 0),
            "part": inv.split_no,
            "po_number": po_num,
            "returned_reason": inv.returned_reason,
            "returned_at": ret.isoformat() if ret else None,
            "returned_by": names.get(inv.returned_by) if inv.returned_by else None,
            "days_since": (today - ret.date()).days if ret else None,
            "customer_id": cid, "customer": cust_names.get(cid),
            "project_id": pid, "project": pname,
            "employee_id": getattr(emp, "id", None),
            "employee": " ".join(x for x in (getattr(emp, "first_name", None), getattr(emp, "last_name", None)) if x) or None,
            "employee_code": getattr(emp, "employee_code", None),
            "month": mkey, "month_label": month_label(mkey), "period_label": period,
            "timesheet_id": ts.id if ts is not None else None,
            "timesheet_status": _status(ts.status) if ts is not None else None,
            "step": key, "step_label": label, "step_who": who,
        })

    options = {
        "customers": sorted(({"id": c, "name": cust_names.get(c) or f"Customer {c}"}
                             for c in {r["customer_id"] for r in rows if r["customer_id"]}),
                            key=lambda x: x["name"].lower()),
        "months": [{"key": m, "label": month_label(m)}
                   for m in sorted({r["month"] for r in rows if r["month"]}, reverse=True)],
    }
    if customer_id is not None:
        rows = [r for r in rows if r["customer_id"] == customer_id]
    if ym:
        rows = [r for r in rows if r["month"] == month_key(*ym)]
    if search and search.strip():
        q = search.strip().lower()
        rows = [r for r in rows if q in " ".join(str(r.get(k) or "") for k in (
            "customer", "project", "employee", "employee_code", "proforma_number", "month_label",
            "returned_reason", "returned_by", "po_number")).lower()]
    counts = {k: 0 for k, *_ in STEPS}
    for r in rows:
        counts[r["step"]] += 1
    counts["all"] = len(rows)
    if step:
        rows = [r for r in rows if r["step"] == step]
    can_reissue = bool(user.is_admin or _safe(lambda: user_may(db, user, "timesheet.generate_invoice")))
    return {"rows": rows, "meta": {
        "total": len(rows), "counts": counts,
        "steps": [{"key": k, "label": label, "who": who} for k, label, who in STEPS],
        "options": options,
        "can_reopen": may_reopen(db, user),
        "can_reissue": can_reissue,
    }}


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False
