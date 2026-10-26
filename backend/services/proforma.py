"""Proforma → Tax invoice lifecycle (23 Sep 2026).

The flow the business asked for:

    Sales fills the timesheet and submits it
      → GM (custom role) verifies and approves it, then raises a PROFORMA
        (`POST /api/timesheets/{id}/generate-invoice`, confirming the
        customer's column format first)
      → Finance reviews the Proforma on the invoice page: corrects header
        fields, then either CONVERTS it to the original tax invoice
        (`convert_to_tax_invoice`) or RETURNS it to the GM with a reason
        (`return_to_gm`)
      → a returned Proforma is fixed by the GM and raised again through the
        same generate route (the returned document is replaced, keeping its
        PI number)
      → conversion notifies the Sales Manager, who carries on with the
        customer.

Money only moves at conversion: a Proforma never draws on a PO, takes no
payments and has no TDS (`services.finance.require_tax_invoice`).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CustomerBillingPolicy, Invoice, Project, PurchaseOrder, Timesheet
from models.finance import InvoiceKind
from services.crm_common import next_sequence_number
from services.finance import (
    apply_round_off, consume_po_for_invoice, ensure_po_covers, ensure_unique_invoice_number,
    po_draw_amount,
)
from services.invoice_format import format_summary, normalize_invoice_format
from services.notify import notify_role, notify_roles

PROFORMA_PREFIX = "PI"
TAX_PREFIX = "INV"

#: Who is told what. Each is an admin-editable event (routers/crm/email_flows.py);
#: the role here is only the default route.
EVENT_PROFORMA_READY = "invoice.proforma_ready"        # → Finance
EVENT_PROFORMA_RETURNED = "invoice.proforma_returned"  # → GM
EVENT_INVOICE_GENERATED = "invoice.generated"          # → Sales Manager + Sales Head

ROLE_GM = "GM"
ROLE_SALES_MANAGER = "Sales Manager"
ROLE_FINANCE = "Finance"

MIN_RETURN_REASON = 10


# ------------------------------------------------------------------ helpers

def po_credit_days(po: PurchaseOrder | None, default: int = 30) -> int:
    """Credit days from the PO's payment terms ("Net 30 Days" → 30)."""
    if po is not None and po.payment_terms:
        m = re.search(r"(\d+)", str(po.payment_terms))
        if m:
            return max(0, min(int(m.group(1)), 365))
    return default


def invoice_credit_days(db: Session, project_id: int | None,
                        po: PurchaseOrder | None, default: int = 30) -> tuple[int, str]:
    """(credit days, where they came from) for a new / converted invoice.

    5 Oct 2026, user ask: the due date follows the CUSTOMER BRANCH. The
    project's delivery branch (else the PO's billing branch) wins when it has
    `invoice_due_days`; then the PO's payment terms; then 30. Source is
    "branch" | "po" | "default" — the invoice editor prints it.
    """
    from models import CustomerBranch

    project = db.get(Project, project_id) if project_id else None
    branch_id = getattr(project, "branch_id", None) or getattr(po, "billing_branch_id", None)
    branch = db.get(CustomerBranch, branch_id) if branch_id else None
    if branch is not None and branch.invoice_due_days is not None:
        return max(0, min(int(branch.invoice_due_days), 365)), "branch"
    if po is not None and po.payment_terms and re.search(r"\d", str(po.payment_terms)):
        return po_credit_days(po, default), "po"
    return default, "default"


def _billing_policy_for_project(db: Session, project_id: int) -> CustomerBillingPolicy | None:
    project = db.get(Project, project_id)
    if project is None or project.customer_id is None:
        return None
    return db.execute(
        select(CustomerBillingPolicy).where(CustomerBillingPolicy.customer_id == project.customer_id)
    ).scalars().first()


def resolve_invoice_format(db: Session, project_id: int, posted: dict | None) -> dict[str, bool]:
    """The format frozen on a new Proforma.

    A posted choice wins AND is remembered on the customer's billing policy
    (the GM confirmed it, so next month's dialog is pre-filled). With nothing
    posted, the customer's saved format applies; with neither, every column.
    """
    policy = _billing_policy_for_project(db, project_id)
    if posted is not None:
        fmt = normalize_invoice_format(posted)
        if policy is not None and normalize_invoice_format(policy.invoice_format) != fmt:
            policy.invoice_format = dict(fmt)   # full reassignment — JSONB is not tracked in place
        return fmt
    return normalize_invoice_format(policy.invoice_format if policy is not None else None)


def customer_invoice_format(db: Session, project_id: int) -> dict[str, bool]:
    """What the GM's dialog is pre-filled with (the customer's saved choice)."""
    policy = _billing_policy_for_project(db, project_id)
    return normalize_invoice_format(policy.invoice_format if policy is not None else None)


def replaceable_proforma(db: Session, ts: Timesheet) -> Invoice | None:
    """A RETURNED Proforma on this sheet may be raised again — the old document
    is replaced (same PI number) rather than sitting beside the new one."""
    rows = returned_proformas(db, ts)
    return rows[0] if rows else None


QTY_UNITS = ("Hourly", "Daily")
_C = Decimal("0.01")
_Q4 = Decimal("0.0001")
_HALF = Decimal("0.5")


def round_per_day(*, unit: str | None, rate, amount, per_day, step=Decimal("1")) -> dict | None:
    """PURE. The per-day round off on a Proforma (24 Oct 2026, Finance ask).

    A Monthly (or Yearly, already ÷12) rate over a PART month bills
    rate ÷ days × days: ₹1,20,000 over 31 days is ₹3,870.9677 a day, and the
    printed per-day figure never multiplied back to the amount. Rounded, the
    invoice bills `days × per-day` with the per-day figure rounded to the rupee
    (half up) and prints exactly those two numbers.

    A FULL month (amount = the monthly rate) is never touched — it bills the
    rate exactly, with no ₹1 drift. Nor is a cycle longer than a month
    (amount > rate) or an Hourly / Daily rate (already priced per unit).
    Returns {per_day, days, amount, before} or None when nothing changes.

    `step` (24 Oct 2026, later): Decimal("1") = the GM's "round to the rupee"
    toggle; Decimal("0.01") = the DEFAULT for every part month — the per-day
    figure the invoice prints (to the paisa) × the billed days, so
    "30 days × ₹8,387.10 = ₹2,51,613.00" reads true. With the paisa step the
    billed days must be whole or half days; anything else is left alone.
    """
    if (unit or "Monthly") not in ("Monthly", "Yearly"):
        return None
    try:
        r, a, p = Decimal(str(rate or 0)), Decimal(str(amount or 0)), Decimal(str(per_day or 0))
    except Exception:  # noqa: BLE001
        return None
    if r <= 0 or a <= 0 or p <= 0 or a >= r:
        return None
    days = a / p
    # Days come in halves (half days); a tiny gap is the per-day figure's own
    # rounding, not a real fraction — snap to the half day.
    snapped = (days / _HALF).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * _HALF
    step = Decimal(str(step))
    if abs(days - snapped) < Decimal("0.05"):
        days = snapped
    elif step < Decimal("1"):
        return None             # not a whole / half day count — the default never guesses
    else:
        days = days.quantize(_C, rounding=ROUND_HALF_UP)
    if days <= 0:
        return None
    rounded = p.quantize(step, rounding=ROUND_HALF_UP)
    new_amount = (days * rounded).quantize(_C, rounding=ROUND_HALF_UP)
    if new_amount > r:
        new_amount = r          # rounding can never bill more than the whole month
    return {"per_day": rounded, "days": days, "amount": new_amount, "before": a.quantize(_C)}


def plan_splits(splits: list[dict], *, remaining: Decimal, remaining_qty: Decimal | None,
                rate: Decimal | None, unit: str | None, rate_split: bool = False) -> list[dict]:
    """PURE — turn the GM's per-PO entries into parts {po_id, qty, amount} (0141).

    * A quantity is accepted on an Hourly / Daily rate with one rate in force;
      otherwise every part is an amount (a Monthly rate's quantity is a fraction
      of a month, and a mid-month rate change has no single rate).
    * amount = qty × rate (paise), or qty = amount ÷ rate (4 dp).
    * The parts may leave something for later, never exceed what is left.
    * When the parts finish the sheet, the LAST part takes the exact remainder,
      so the parts always add back to the figure frozen at approval.
    Raises ValueError with the sentence the GM sees."""
    if not splits:
        raise ValueError("Add at least one PO to the split")
    seen: set[int] = set()
    qty_ok = (unit in QTY_UNITS) and not rate_split and rate is not None and rate > 0
    parts: list[dict] = []
    for s in splits:
        po_id = int(s["po_id"])
        if po_id in seen:
            raise ValueError("The same PO is listed twice — put its whole share in one line")
        seen.add(po_id)
        q, a = s.get("quantity"), s.get("amount")
        if q is not None and a is None:
            if not qty_ok:
                raise ValueError("Enter the amount for each PO — this rate is not billed per hour or day")
            qty = Decimal(str(q)).quantize(_Q4)
            amount = (qty * rate).quantize(_C, rounding=ROUND_HALF_UP)
        elif a is not None:
            amount = Decimal(str(a)).quantize(_C, rounding=ROUND_HALF_UP)
            qty = (amount / rate).quantize(_Q4, rounding=ROUND_HALF_UP) if rate else None
        else:
            raise ValueError("Each PO needs the hours / days or the amount it covers")
        if amount <= 0:
            raise ValueError("Each part must bill more than ₹0")
        parts.append({"po_id": po_id, "qty": qty, "amount": amount})
    total = sum((p["amount"] for p in parts), Decimal("0"))
    if total > remaining + _C:
        raise ValueError(f"The parts add up to ₹{total:,.2f} but only ₹{remaining:,.2f} is left to bill "
                         "on this timesheet")
    qty_sum = sum((p["qty"] or Decimal("0") for p in parts), Decimal("0"))
    finishes = (abs(remaining - total) <= Decimal("1.00")
                or (remaining_qty is not None and all(p["qty"] is not None for p in parts)
                    and abs(remaining_qty - qty_sum) <= _Q4))
    if finishes:
        last = parts[-1]
        others = parts[:-1]
        last["amount"] = remaining - sum((p["amount"] for p in others), Decimal("0"))
        if remaining_qty is not None and all(p["qty"] is not None for p in others):
            last["qty"] = (remaining_qty - sum((p["qty"] for p in others), Decimal("0"))).quantize(_Q4)
        if last["amount"] <= 0:
            raise ValueError("The last part would bill nothing — remove it")
    return parts


def _fmt_d(d) -> str:
    return d.strftime("%d-%b-%Y")


def plan_date_splits(splits: list[dict], *, days: list[tuple], window: tuple,
                     total: Decimal, taken: list[tuple] | None = None) -> list[dict]:
    """PURE — split a sheet BY DATES (0142): 1–29 Mar on PO A, 30–31 Mar on PO B.

    `days` = [(date, weight)] for every day of the sheet — the billable hours
    on an Hourly rate, else the billable days (so a Monthly rate is shared out
    by the same billed days its invoice already counts, and an unworked weekend
    the customer does not pay for moves no money). `total` = the whole sheet's
    sub-total (the figure frozen at approval). `taken` = date ranges already
    raised on live parts.

    Each part's amount = total × (weight in its dates ÷ weight of the whole
    sheet). The caller passes the parts to `plan_splits` as amounts, so the part
    that finishes the sheet still takes the exact remainder.
    Returns [{po_id, start, end, amount, weight, days}] sorted by start date.
    Raises ValueError with the sentence the GM sees."""
    if not splits:
        raise ValueError("Add at least one PO to the split")
    w_start, w_end = window
    weights = {d: Decimal(str(w or 0)) for d, w in days}
    whole = sum(weights.values(), Decimal("0"))
    calendar = whole <= 0          # nothing billable recorded — share by calendar days
    if calendar:
        weights = {d: Decimal("1") for d in weights}
        whole = Decimal(len(weights)) or Decimal("1")
    parts: list[dict] = []
    for s in splits:
        start, end = s.get("start_date"), s.get("end_date")
        if start is None or end is None:
            raise ValueError("Each PO needs the dates it covers — a From and a To date")
        if end < start:
            raise ValueError(f"{_fmt_d(end)} is before {_fmt_d(start)} — fix the dates")
        if start < w_start or end > w_end:
            raise ValueError(f"The dates must lie inside this timesheet's period "
                             f"{_fmt_d(w_start)} to {_fmt_d(w_end)}")
        parts.append({"po_id": int(s["po_id"]), "start": start, "end": end})
    parts.sort(key=lambda p: p["start"])
    ranges = [(p["start"], p["end"]) for p in parts] + list(taken or [])
    ranges.sort()
    for (a1, b1), (a2, b2) in zip(ranges, ranges[1:]):
        if a2 <= b1:
            raise ValueError(f"Two parts both bill {_fmt_d(a2)} — a day can be on one invoice only")
    seen: set[int] = set()
    for p in parts:
        if p["po_id"] in seen:
            raise ValueError("The same PO is listed twice — give it one date range")
        seen.add(p["po_id"])
        w = sum((v for d, v in weights.items() if p["start"] <= d <= p["end"]), Decimal("0"))
        if w <= 0:
            raise ValueError(f"Nothing billable between {_fmt_d(p['start'])} and {_fmt_d(p['end'])}")
        p["weight"] = w
        p["days"] = sum(1 for d in weights if p["start"] <= d <= p["end"])
        p["amount"] = (total * w / whole).quantize(_C, rounding=ROUND_HALF_UP)
    return parts


def returned_proformas(db: Session, ts: Timesheet) -> list[Invoice]:
    """Every RETURNED Proforma on the sheet (0141: a split sheet can have one per
    part). Raising the sheet again replaces them, reusing their PI numbers."""
    from services.timesheets import invoices_for
    return [i for i in invoices_for(db, ts) if i.is_proforma and i.returned_at is not None]


def _timesheet_line(ts: Timesheet) -> str:
    return f"timesheet {ts.year}-{ts.month:02d} (project #{ts.project_id}, employee #{ts.employee_id})"


# ------------------------------------------------------------ notifications

def notify_proforma_ready(db: Session, invoice: Invoice, ts: Timesheet, actor) -> None:
    notify_role(
        db, ROLE_FINANCE,
        f"Proforma invoice {invoice.invoice_number} ready for review",
        f"The GM raised proforma {invoice.invoice_number} for {_timesheet_line(ts)}: "
        f"₹{float(invoice.grand_total or 0):,.2f} incl. GST. Review it and generate the original "
        f"invoice, or return it with a note.",
        f"/invoices/{invoice.id}", exclude_user_id=getattr(actor, "id", None),
        event=EVENT_PROFORMA_READY, actor=actor,
        dedupe_prefix=f"{EVENT_PROFORMA_READY}:{invoice.id}:{invoice.returned_at or ''}",
        related_type="invoice", related_id=invoice.id,
        rows=[("Proforma", invoice.invoice_number), ("Amount", f"₹{float(invoice.grand_total or 0):,.2f}"),
              ("Format", format_summary(invoice.invoice_format))],
    )


def _notify_returned(db: Session, invoice: Invoice, actor, reason: str) -> None:
    """26 Oct 2026: not only the GM — the Sales Manager / Sales Head, RMG, the
    people who may raise Proformas (a template-granted GM) and whoever filled
    the sheet hear which month came back and why; the Dashboard's Returned
    Proformas board keeps it in view until it is raised again."""
    ts = db.get(Timesheet, invoice.timesheet_id) if invoice.timesheet_id else None
    rows = [("Proforma", invoice.invoice_number), ("Reason", reason)]
    month = ""
    extra: set[int] = set()
    if ts is not None:
        from models import Employee
        from services.billing_tracker import month_key, month_label
        month =month_label(month_key(ts.year, ts.month)) or ""
        emp = db.get(Employee, ts.employee_id) if ts.employee_id else None
        name = " ".join(x for x in (getattr(emp, "first_name", None), getattr(emp, "last_name", None)) if x)
        rows = [("Proforma", invoice.invoice_number), ("Month", month or "—"),
                ("Employee", name or "—"), ("Reason", reason)]
        submitter = None
        try:
            from models import TimesheetActivityLog
            submitter = db.execute(
                select(TimesheetActivityLog.user_id)
                .where(TimesheetActivityLog.timesheet_id == ts.id,
                       TimesheetActivityLog.action_type.in_(("TS_SUBMITTED", "TS_CREATED")))
                .order_by(TimesheetActivityLog.id.desc()).limit(1)).scalar()
        except Exception:  # noqa: BLE001 — a notice never undoes the return
            submitter = None
        if submitter:
            extra.add(int(submitter))
    try:
        from services.action_permissions import user_ids_who_may
        extra |= set(user_ids_who_may(db, "timesheet.generate_invoice"))
    except Exception:  # noqa: BLE001
        pass
    extra.discard(getattr(actor, "id", None))
    what = f" for {month}" if month else ""
    notify_roles(
        db, [ROLE_GM, ROLE_SALES_MANAGER, "Sales_Head", "RMG"],
        f"Proforma {invoice.invoice_number}{what} returned by Finance",
        f"Finance sent proforma {invoice.invoice_number}{what} back: {reason}. "
        "Check the timesheet — reopen it to correct and submit again, or the GM reissues it.",
        f"/timesheets/{ts.id}" if ts is not None else f"/invoices/{invoice.id}",
        exclude_user_id=getattr(actor, "id", None),
        event=EVENT_PROFORMA_RETURNED, actor=actor,
        dedupe_prefix=f"{EVENT_PROFORMA_RETURNED}:{invoice.id}:{invoice.returned_at.isoformat() if invoice.returned_at else ''}",
        related_type="invoice", related_id=invoice.id,
        rows=rows, user_ids=extra or None,
    )


def _notify_generated(db: Session, invoice: Invoice, actor) -> None:
    # 5 Oct 2026: the Sales Manager OR the Sales Head sends it to the customer
    # and confirms the customer's approval back to Finance (IRN next).
    notify_roles(
        db, [ROLE_SALES_MANAGER, "Sales_Head"],
        f"Invoice {invoice.invoice_number} generated — send it to the customer",
        f"Finance generated the original invoice {invoice.invoice_number} "
        f"(from proforma {invoice.proforma_number}) for ₹{float(invoice.grand_total or 0):,.2f} incl. GST. "
        f"Due {invoice.due_date.isoformat() if invoice.due_date else '—'}. Send it to the customer; once "
        "they accept it unchanged, press \"Confirm customer approval\" on the invoice so Finance adds the IRN.",
        f"/invoices/{invoice.id}", exclude_user_id=getattr(actor, "id", None),
        event=EVENT_INVOICE_GENERATED, actor=actor,
        dedupe_prefix=f"{EVENT_INVOICE_GENERATED}:{invoice.id}",
        related_type="invoice", related_id=invoice.id,
        rows=[("Invoice", invoice.invoice_number), ("Proforma", invoice.proforma_number or "—"),
              ("Amount", f"₹{float(invoice.grand_total or 0):,.2f}")],
    )


# --------------------------------------------------------------- transitions

def _require_proforma(invoice: Invoice, action: str) -> None:
    if not invoice.is_proforma:
        raise HTTPException(status_code=400,
                            detail=f"{invoice.invoice_number} is already a tax invoice — cannot {action}")


def return_to_gm(db: Session, invoice: Invoice, actor, reason: str) -> Invoice:
    """Finance sends the Proforma back. The reason is mandatory (it is what the
    GM reads) and the document stays on the timesheet until the GM reissues it."""
    _require_proforma(invoice, "return it")
    reason = " ".join((reason or "").split())
    if len(reason) < MIN_RETURN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say why it is returned (at least {MIN_RETURN_REASON} characters)")
    invoice.returned_reason = reason
    invoice.returned_at = datetime.now(timezone.utc)
    invoice.returned_by = getattr(actor, "id", None)
    db.flush()
    _notify_returned(db, invoice, actor, reason)
    return invoice


def convert_to_tax_invoice(db: Session, invoice: Invoice, actor, *,
                           invoice_number: str | None = None,
                           invoice_date: date | None = None,
                           round_off: bool | None = None) -> Invoice:
    """Finance turns the reviewed Proforma into the original tax invoice.

    In place, not a copy: the id, lines, GST and PO link stay, so every link
    already sent (bell, email, share QR) keeps opening the same document. The
    PI number moves to `proforma_number`; the tax number is what Finance typed
    or the next INV-YYYY-NNN. The PO is drawn down NOW, re-checking balance,
    because other invoices may have consumed it since the Proforma was raised.
    """
    _require_proforma(invoice, "convert it")
    if invoice.returned_at is not None:
        raise HTTPException(status_code=400,
                            detail=f"{invoice.invoice_number} was returned to the GM — wait for it to be reissued")
    number = (invoice_number or "").strip() or next_sequence_number(db, Invoice, Invoice.invoice_number, TAX_PREFIX)
    if number != invoice.invoice_number:
        ensure_unique_invoice_number(db, number)
    po = db.get(PurchaseOrder, invoice.po_id) if invoice.po_id else None
    # Every check BEFORE the first write: a refused conversion must leave the
    # Proforma exactly as it was, even inside a session nobody rolls back.
    ensure_po_covers(po, po_draw_amount(invoice.sub_total))   # base value, not incl. GST

    invoice.proforma_number = invoice.proforma_number or invoice.invoice_number
    invoice.invoice_number = number
    invoice.kind = InvoiceKind.TAX.value
    if invoice_date is not None:
        invoice.invoice_date = invoice_date
    invoice.due_date = (invoice.invoice_date or date.today()) + timedelta(
        days=invoice_credit_days(db, invoice.project_id, po)[0])
    if round_off is not None:
        apply_round_off(invoice, round_off)
    # 0143 — the TAX invoice is issued now: it carries the customer's address
    # of today (a Proforma raised before a move must not print the old one),
    # unless Finance already corrected the Proforma's address by hand.
    from services.invoice_parties import freeze as freeze_parties, snapshot_of
    _snap = snapshot_of(invoice)
    if not (_snap and _snap.get("corrected")):
        freeze_parties(db, invoice, source="converted", force=True)
    db.flush()
    consume_po_for_invoice(db, invoice, po, getattr(actor, "id", None))
    _notify_generated(db, invoice, actor)
    return invoice
