"""What an invoice is FOR, without opening it (16 Oct 2026, user ask).

Finance and Sales read invoices by customer BRANCH, by EMPLOYEE and by the
MONTH the work was done — Customer Received Amount, My Tasks filters, the
bulk customer-approval list. One batched resolver so every screen agrees:

* branch  — the project's delivery branch, else the PO's billing branch (the
  same rule `proforma.invoice_credit_days` uses for the due date);
* month   — the billed timesheet's period (year, month), else the invoice date;
* employee — the billed timesheet's employee (a manual invoice names nobody).

Four queries for any number of invoices; never raises into a list.
"""
from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger("karnex.crm.invoice_facets")

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_key(year: int | None, month: int | None) -> str | None:
    if not year or not month:
        return None
    return f"{int(year):04d}-{int(month):02d}"


def month_label(key: str | None) -> str | None:
    """PURE. "2026-09" → "Sep 2026"."""
    if not key or len(key) < 7:
        return None
    try:
        return f"{_MONTHS[int(key[5:7]) - 1]} {key[:4]}"
    except (ValueError, IndexError):
        return None


def invoice_facets(db: Session, invoices) -> dict[int, dict]:
    """{invoice id: {branch_id, branch_name, month, month_label, period_label, timesheet_id,
    employee_id, employee}} — `period_label` only when the sheet's window is not the
    calendar month ("21 Apr – 30 May 2026", 20 Oct 2026)."""
    from models import CustomerBranch, Employee, Project, PurchaseOrder, Timesheet

    invs = [i for i in (invoices or []) if getattr(i, "id", None)]
    if not invs:
        return {}
    out: dict[int, dict] = {}
    try:
        with db.begin_nested():
            proj_branch = dict(db.execute(
                select(Project.id, Project.branch_id).where(
                    Project.id.in_({i.project_id for i in invs if i.project_id}))
            ).all()) if any(i.project_id for i in invs) else {}
            po_ids = {i.po_id for i in invs if getattr(i, "po_id", None)}
            po_branch = dict(db.execute(
                select(PurchaseOrder.id, PurchaseOrder.billing_branch_id).where(PurchaseOrder.id.in_(po_ids))
            ).all()) if po_ids else {}
            ts_ids = {i.timesheet_id for i in invs if getattr(i, "timesheet_id", None)}
            ts = {r[0]: r for r in db.execute(
                select(Timesheet.id, Timesheet.year, Timesheet.month, Employee.id,
                       Employee.first_name, Employee.last_name,
                       Timesheet.period_start_date, Timesheet.period_end_date)
                .join(Employee, Employee.id == Timesheet.employee_id, isouter=True)
                .where(Timesheet.id.in_(ts_ids))
            ).all()} if ts_ids else {}
            branch_of = {i.id: (proj_branch.get(i.project_id) or po_branch.get(getattr(i, "po_id", None)))
                         for i in invs}
            bids = {b for b in branch_of.values() if b}
            names = dict(db.execute(
                select(CustomerBranch.id, CustomerBranch.branch_name).where(CustomerBranch.id.in_(bids))
            ).all()) if bids else {}
    except Exception:  # noqa: BLE001 — facets are display / filter aids only
        logger.warning("invoice facets failed", exc_info=True)
        return {}
    for i in invs:
        t = ts.get(getattr(i, "timesheet_id", None))
        key = month_key(t[1], t[2]) if t else None
        if key is None and isinstance(getattr(i, "invoice_date", None), date):
            key = month_key(i.invoice_date.year, i.invoice_date.month)
        emp = " ".join(p for p in ((t[4], t[5]) if t else ()) if p) or None
        bid = branch_of.get(i.id)
        period = None
        if t and t[6] and t[7]:
            from services.billing_tracker import period_label
            period = period_label(t[1], t[2], t[6], t[7])
        out[i.id] = {"branch_id": bid, "branch_name": names.get(bid) if bid else None,
                     "month": key, "month_label": month_label(key), "period_label": period,
                     "timesheet_id": t[0] if t else None,
                     "employee_id": t[3] if t else None, "employee": emp}
    return out
