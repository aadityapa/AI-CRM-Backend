"""Billing rules shared by every list, queue and dashboard (23 Oct 2026).

Two user reports drove this module:

* "Awaiting customer approval shows 101 invoices on the Invoices list but 25 on
  the Sales Manager's dashboard" — the dashboard tile had its own 90-day window.
  Every screen that answers "which invoices still wait for the customer?" now
  uses `awaiting_customer_clause()`.
* "We only need data from FY 2026-27 — March 2026 onwards" — the first WORK
  month the application works with is an Admin setting
  (`billing.data_start_month`, default 2026-03). Older rows stay in the
  database and in the financial reports; the working lists start there.
* "The project closed on 30 Apr 2026, why was a May 2026 timesheet created?" —
  `last_working_day(project, pe)` / `month_after_end(...)` are the ONE answer to
  "is this month still owed a timesheet?".

Everything here is pure or a SQL expression — no I/O except the cached setting.
"""
from __future__ import annotations

import re
from datetime import date

import sqlalchemy as sa

DATA_START_KEY = "billing.data_start_month"
DEFAULT_DATA_START = (2026, 3)
_MONTH_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def parse_month(value: str | None) -> tuple[int, int] | None:
    """'2026-03' -> (2026, 3); anything else -> None."""
    m = _MONTH_RE.match((value or "").strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return (y, mo) if 1 <= mo <= 12 and 1900 < y < 2200 else None


def data_start() -> tuple[int, int]:
    """(year, month) of the first work month the application lists. Never raises."""
    try:
        from services.org_settings import setting

        return parse_month(setting(DATA_START_KEY)) or DEFAULT_DATA_START
    except Exception:  # noqa: BLE001 — a settings hiccup must not hide data wrongly
        return DEFAULT_DATA_START


def data_start_date() -> date:
    y, m = data_start()
    return date(y, m, 1)


def data_start_label() -> str:
    y, m = data_start()
    return f"{_MONTHS[m - 1]} {y}"


def data_start_meta() -> dict:
    """What every list's `meta.data_start` carries, so the page can say it."""
    y, m = data_start()
    return {"key": f"{y:04d}-{m:02d}", "label": data_start_label()}


def month_in_window(year: int, month: int, start: tuple[int, int] | None = None) -> bool:
    return (int(year), int(month)) >= (start or data_start())


def work_month_clause(year_col, month_col, start: tuple[int, int] | None = None):
    """SQL: (year, month) on or after the data start. Never NULL for real rows."""
    y, m = start or data_start()
    return sa.or_(year_col > y, sa.and_(year_col == y, month_col >= m))


def invoice_window_clause(start: tuple[int, int] | None = None):
    """SQL on `Invoice`: its WORK month (the timesheet's month, else the invoice
    date — the `invoice_facets` rule) is on or after the data start."""
    from models.finance import Invoice
    from models.timesheets import Timesheet

    y, m = start or data_start()
    in_window = sa.select(Timesheet.id).where(
        work_month_clause(Timesheet.year, Timesheet.month, (y, m)))
    return sa.or_(
        sa.and_(Invoice.timesheet_id.isnot(None), Invoice.timesheet_id.in_(in_window)),
        sa.and_(Invoice.timesheet_id.is_(None), Invoice.invoice_date >= date(y, m, 1)),
    )


def awaiting_customer_clause(start: tuple[int, int] | None = None):
    """The ONE rule for "issued, waiting for the customer's approval": a Tax
    invoice in the `awaiting_customer` stage (no approval, no money on it yet),
    inside the data window. Used by the Invoices list and the
    Confirm-with-customer tile. 24 Oct 2026: an older invoice that already has
    money on it is in Partially paid / Paid, never here as well."""
    return sa.and_(invoice_stage_case() == "awaiting_customer", invoice_window_clause(start))


# ---------------------------------------------------------------- invoice stage

#: Where a tax invoice stands — ONE stage each, in the order the money moves
#: (24 Oct 2026, user report: an invoice still waiting for the customer's
#: approval was also listed under "Unpaid"). Proforma is its own stage.
INVOICE_STAGES = (
    ("proforma", "Proforma"),
    ("awaiting_customer", "Awaiting customer"),
    ("pending_einvoice", "Pending e-invoice"),
    ("unpaid", "Unpaid"),
    ("partially_paid", "Partially paid"),
    ("paid", "Paid"),
)
INVOICE_STAGE_KEYS = tuple(k for k, _ in INVOICE_STAGES)


def invoice_stage(*, kind, payment_status, paid_amount, tds_deducted,
                  customer_approved: bool, irn: bool) -> str:
    """PURE twin of `invoice_stage_case()`.

    Paid wins; then any money on it (cash or TDS) = partially paid — an older
    invoice paid before the approval / IRN rule stays with its money; then the
    approval → IRN steps; then unpaid (IRN in, nothing received)."""
    k = getattr(kind, "value", kind)
    if k == "Proforma":
        return "proforma"
    if str(getattr(payment_status, "value", payment_status) or "") == "Paid":
        return "paid"
    if float(paid_amount or 0) > 0 or float(tds_deducted or 0) > 0:
        return "partially_paid"
    if not customer_approved:
        return "awaiting_customer"
    if not irn:
        return "pending_einvoice"
    return "unpaid"


def stage_of_invoice(inv) -> str:
    return invoice_stage(kind=inv.kind, payment_status=inv.payment_status,
                         paid_amount=inv.paid_amount, tds_deducted=getattr(inv, "tds_deducted", 0),
                         customer_approved=bool(inv.customer_approved_at), irn=bool(inv.irn_number))


def invoice_stage_case():
    """SQL CASE giving each invoice its stage key — the Invoices tabs, their
    counts and the Finance desk all read this, so they can never disagree."""
    from models.finance import Invoice, InvoiceKind, PaymentStatus

    money = sa.or_(sa.func.coalesce(Invoice.paid_amount, 0) > 0,
                   sa.func.coalesce(Invoice.tds_deducted, 0) > 0)
    return sa.case(
        (Invoice.kind == InvoiceKind.PROFORMA.value, "proforma"),
        (Invoice.payment_status == PaymentStatus.PAID, "paid"),
        (money, "partially_paid"),
        (Invoice.customer_approved_at.is_(None), "awaiting_customer"),
        (Invoice.irn_number.is_(None), "pending_einvoice"),
        else_="unpaid",
    )


# ---------------------------------------------------------------- invoice status

#: The Status column of the Invoices list, one key per invoice (26 Oct 2026,
#: user ask: "a filter on the Status column in every tab"). It refines the
#: stage: a Proforma is with Finance or returned to the GM; an open tax invoice
#: is overdue, due within `DUE_SOON_DAYS` or not yet due (its due date, else
#: invoice date + `DEFAULT_TERMS_DAYS`); a paid one is settled or still has TDS
#: pending from the client.
DEFAULT_TERMS_DAYS = 30
DUE_SOON_DAYS = 7
INVOICE_STATUSES = (
    ("proforma_with_finance", "With Finance"),
    ("proforma_returned", "Returned to GM"),
    ("overdue", "Overdue"),
    ("due_soon", "Due in 7 days"),
    ("not_due", "Not yet due"),
    ("paid_tds_pending", "Paid · TDS pending"),
    ("paid", "Paid · settled"),
)
INVOICE_STATUS_KEYS = tuple(k for k, _ in INVOICE_STATUSES)
#: Which statuses each stage can hold — the filter offers only these per tab.
STATUSES_BY_STAGE = {
    "proforma": ("proforma_with_finance", "proforma_returned"),
    "awaiting_customer": ("overdue", "due_soon", "not_due"),
    "pending_einvoice": ("overdue", "due_soon", "not_due"),
    "unpaid": ("overdue", "due_soon", "not_due"),
    "partially_paid": ("overdue", "due_soon", "not_due"),
    "paid": ("paid_tds_pending", "paid"),
}


def _due_state(due, invoice_date, today: date) -> str:
    from datetime import timedelta
    if due is None and invoice_date is not None:
        due = invoice_date + timedelta(days=DEFAULT_TERMS_DAYS)
    if due is None:
        return "not_due"
    if due < today:
        return "overdue"
    if due <= today + timedelta(days=DUE_SOON_DAYS):
        return "due_soon"
    return "not_due"


def invoice_status(*, stage: str, returned: bool, tds_pending: bool, due, invoice_date,
                   today: date | None = None) -> str:
    """PURE twin of `invoice_status_case()`."""
    today = today or date.today()
    if stage == "proforma":
        return "proforma_returned" if returned else "proforma_with_finance"
    if stage == "paid":
        return "paid_tds_pending" if tds_pending else "paid"
    return _due_state(due, invoice_date, today)


#: Words for each stage — the ONE label every list prints for an invoice
#: (27 Oct 2026: My Tasks printed `payment_status`, so "awaiting customer",
#: "pending e-invoice" and "unpaid" all read "Unpaid" and filtered together).
STAGE_LABELS = dict(INVOICE_STAGES)
STATUS_LABELS = dict(INVOICE_STATUSES)
#: Stages whose money Finance / Sales may chase — payments are refused (409)
#: before the customer's approval and the IRN, so only these are collections.
COLLECTABLE_STAGES = ("unpaid", "partially_paid")


def effective_due(due, invoice_date):
    """PURE. The due date every screen uses: the invoice's own, else invoice
    date + `DEFAULT_TERMS_DAYS` (the status / ageing / days-late rule)."""
    from datetime import timedelta
    if due is not None:
        return due
    if invoice_date is not None:
        return invoice_date + timedelta(days=DEFAULT_TERMS_DAYS)
    return None


def days_late(due, invoice_date, today: date | None = None) -> int:
    """PURE. Days past the effective due date (0 when not yet due / unknown)."""
    d = effective_due(due, invoice_date)
    today = today or date.today()
    return max(0, (today - d).days) if d else 0


def overdue_clause(today: date | None = None):
    """SQL on `Invoice`: the effective due date is before today (no date
    arithmetic in SQL)."""
    from datetime import timedelta

    from models.finance import Invoice
    today = today or date.today()
    return sa.or_(sa.and_(Invoice.due_date.isnot(None), Invoice.due_date < today),
                  sa.and_(Invoice.due_date.is_(None),
                          Invoice.invoice_date < today - timedelta(days=DEFAULT_TERMS_DAYS)))


def due_range_clause(date_from: date | None, date_to: date | None):
    """SQL: the EFFECTIVE due date is inside [from, to] — an invoice with no
    due date is placed at invoice date + the default terms, never dropped."""
    from datetime import timedelta

    from models.finance import Invoice
    terms = timedelta(days=DEFAULT_TERMS_DAYS)
    own, implied = [Invoice.due_date.isnot(None)], [Invoice.due_date.is_(None)]
    if date_from:
        own.append(Invoice.due_date >= date_from)
        implied.append(Invoice.invoice_date >= date_from - terms)
    if date_to:
        own.append(Invoice.due_date <= date_to)
        implied.append(Invoice.invoice_date <= date_to - terms)
    return sa.or_(sa.and_(*own), sa.and_(*implied))


def invoice_customer_id(project_customer_id, po_customer_id):
    """PURE. ONE customer per invoice on every screen (27 Oct 2026: the tracker
    and My Tasks used the project's customer, the Invoices list the PO's): the
    PO's customer — the customer's own order document — else the project's."""
    return po_customer_id or project_customer_id


def invoice_customer_clause(customer_id: int):
    """SQL twin of `invoice_customer_id`: the invoice's PO belongs to the
    customer, or it has no PO customer and its project does — so an invoice
    lists under exactly ONE customer."""
    from models.finance import Invoice, PurchaseOrder
    from models.projects import Project

    po_of_cust = sa.select(PurchaseOrder.id).where(PurchaseOrder.customer_id == customer_id)
    po_with_cust = sa.select(PurchaseOrder.id).where(PurchaseOrder.customer_id.isnot(None))
    proj_of_cust = sa.select(Project.id).where(Project.customer_id == customer_id)
    return sa.or_(
        Invoice.po_id.in_(po_of_cust),
        sa.and_(sa.or_(Invoice.po_id.is_(None), Invoice.po_id.notin_(po_with_cust)),
                Invoice.project_id.in_(proj_of_cust)),
    )


def invoice_chip(stage: str, status: str | None = None, days: int = 0) -> str:
    """PURE. The chip printed for an issued invoice: the stage, refined by its
    status ("Unpaid · overdue 12 d"). `status_of_chip` on the client reads the
    part before " · ", so the filter groups by stage."""
    label = STAGE_LABELS.get(stage, stage)
    if stage == "paid":
        return label if status != "paid_tds_pending" else f"{label} · TDS pending"
    if status == "overdue":
        return f"{label} · {days} d overdue" if days else f"{label} · overdue"
    if status == "due_soon":
        return f"{label} · due soon"
    return label


def invoice_status_case(today: date | None = None):
    """SQL CASE of `invoice_status` — dialect-agnostic (no date arithmetic in
    SQL: the cut-off dates are worked out here)."""
    from datetime import timedelta

    from models.finance import Invoice, InvoiceKind, PaymentStatus, TdsRecord, TdsStatus

    today = today or date.today()
    soon = today + timedelta(days=DUE_SOON_DAYS)
    # No due date on record → invoice date + the default terms.
    late_issue = today - timedelta(days=DEFAULT_TERMS_DAYS)
    soon_issue = soon - timedelta(days=DEFAULT_TERMS_DAYS)
    overdue = sa.or_(sa.and_(Invoice.due_date.isnot(None), Invoice.due_date < today),
                     sa.and_(Invoice.due_date.is_(None), Invoice.invoice_date < late_issue))
    due_soon = sa.or_(sa.and_(Invoice.due_date.isnot(None), Invoice.due_date <= soon),
                      sa.and_(Invoice.due_date.is_(None), Invoice.invoice_date <= soon_issue))
    tds_pending = sa.exists(sa.select(TdsRecord.id).where(
        TdsRecord.invoice_id == Invoice.id, TdsRecord.tds_status != TdsStatus.PAID))
    return sa.case(
        (sa.and_(Invoice.kind == InvoiceKind.PROFORMA.value, Invoice.returned_at.isnot(None)), "proforma_returned"),
        (Invoice.kind == InvoiceKind.PROFORMA.value, "proforma_with_finance"),
        (sa.and_(Invoice.payment_status == PaymentStatus.PAID, tds_pending), "paid_tds_pending"),
        (Invoice.payment_status == PaymentStatus.PAID, "paid"),
        (overdue, "overdue"),
        (due_soon, "due_soon"),
        else_="not_due",
    )


# ---------------------------------------------------------------- end of work

def last_working_day(project=None, pe=None) -> date | None:
    """The last day this assignment is owed a timesheet: the earlier of the
    person's exit date and the project's last working day. None = open."""
    days = []
    if pe is not None and getattr(pe, "exit_date", None):
        days.append(pe.exit_date)
    if project is not None and getattr(project, "end_date", None):
        days.append(project.end_date)
    return min(days) if days else None


def month_after_end(year: int, month: int, project=None, pe=None) -> bool:
    """True when the whole month starts after the last working day."""
    last = last_working_day(project, pe)
    return last is not None and date(int(year), int(month), 1) > last


def end_refusal(year: int, month: int, project=None, pe=None) -> str | None:
    """Why no timesheet may be created for this month, or None."""
    last = last_working_day(project, pe)
    if last is None or date(int(year), int(month), 1) <= last:
        return None
    who = "the project" if (project is not None and getattr(project, "end_date", None) == last) \
        else "the employee on this project"
    return (f"No timesheet for {_MONTHS[int(month) - 1]} {year}: {who} ended on "
            f"{last.strftime('%d %b %Y')}. The last timesheet is for "
            f"{_MONTHS[last.month - 1]} {last.year}.")


def owed_months(pe, project, today: date, start: date | None = None) -> list[tuple[int, int]]:
    """(year, month) pairs this assignment owes a timesheet for: from its start
    (`start`, else billing / onboarding date) — never before the data start —
    through the earlier of today and its last working day. Oldest first."""
    first = start or getattr(pe, "billing_date", None) or getattr(pe, "onboarding_date", None)
    if first is None:
        return []
    first = max(first, data_start_date())
    last = last_working_day(project, pe)
    end = min(today, last) if last else today
    out: list[tuple[int, int]] = []
    y, m = first.year, first.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def drop_sheets_after_end(db, project, pe=None) -> int:
    """Delete this project's (or one assignment's) Draft / Rejected sheets with
    no invoice for a month that starts after the last working day — the same
    rule migration 0147 applied to existing data. Returns how many. Best-effort:
    each sheet in its own savepoint; caller commits."""
    from models import ProjectEmployee, Timesheet, TimesheetStatus
    from models.finance import Invoice
    from models.timesheets import TimesheetActivityLog, TimesheetAttachment, TimesheetEntry

    q = sa.select(Timesheet).where(
        Timesheet.project_id == project.id,
        Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.REJECTED]),
        ~sa.select(Invoice.id).where(Invoice.timesheet_id == Timesheet.id).exists())
    if pe is not None:
        q = q.where(Timesheet.employee_id == pe.employee_id)
    removed = 0
    for ts in db.execute(q).scalars().all():
        assignment = pe or db.execute(sa.select(ProjectEmployee).where(
            ProjectEmployee.project_id == ts.project_id,
            ProjectEmployee.employee_id == ts.employee_id)).scalars().first()
        if not month_after_end(ts.year, ts.month, project, assignment):
            continue
        try:
            with db.begin_nested():
                for model in (TimesheetEntry, TimesheetActivityLog, TimesheetAttachment):
                    db.execute(sa.delete(model).where(model.timesheet_id == ts.id))
                db.delete(ts)
                db.flush()
            removed += 1
        except Exception:  # noqa: BLE001 — something else points at it: keep it
            continue
    return removed


def start_refusal(year: int, month: int) -> str | None:
    """Why a timesheet before the data start may not be created, or None."""
    if month_in_window(year, month):
        return None
    return (f"Timesheets start from {data_start_label()} (Settings ▸ Organisation ▸ "
            f"Billing ▸ Data starts from). {_MONTHS[int(month) - 1]} {year} is before that.")
