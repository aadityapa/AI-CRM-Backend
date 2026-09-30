"""CEO revenue report (18 Sep 2026) — one month, seen the way a CEO reads it.

Answers, in order: how much did we BILL this month and how much CASH came in;
is that better or worse than last month / last year; who is the money coming
from and how concentrated is it; what is stuck (receivables ageing); what is
already earned but not yet billed and how long the POs will carry us; and how
productive the deployed team is.

Definitions (keep the UI and this file in sync):
* billed          — sum of `invoices.sub_total` (excl. GST) by invoice_date.
* collected       — sum of `invoice_payments.amount` by payment_date (cash in),
                    whichever invoice it settled.
* outstanding     — open `balance_amount` of every invoice dated on or before
                    the month end (today's balances; historical months are
                    therefore "as of today", not as they stood then).
* ageing          — open balances bucketed by days past due TODAY; an invoice
                    with no due_date falls due DEFAULT_TERMS_DAYS after issue.
* pipeline        — Approved timesheets with no invoice (their frozen
                    `approved_figures.totals.sub_total`), and active-PO balance
                    divided by the trailing 3-month average bill = months of cover.
* leakage         — LOP days / no-billing days on the month's approved sheets.

v2 (18 Sep 2026, CEO asks — all Admin/CEO only, all in this one payload):
* targets         — month + financial-year (Apr–Mar) targets from app_settings
                    (`revenue.target_month_default`, `revenue.target_fy`,
                    per-month `revenue.target.YYYY-MM`), attainment, run-rate
                    projection for the current month and the FY.
* margin          — billed minus the monthly cost of the people deployed on
                    that customer's projects: `employees.current_ctc` is the
                    ANNUAL CTC in rupees, so cost = CTC / 12 for every PE whose
                    assignment overlaps the month. Heads without a CTC are
                    counted and reported, never silently treated as free.
* forecast        — next FORECAST_MONTHS months from the active project
                    employees' billing rates (monthly equivalent of the unit),
                    prorated for onboarding / exit dates inside the month.
* collections     — DSO (outstanding ÷ trailing-90-day billing × 90) and the
                    amount-weighted days-to-pay of the cash received this month.
* dimensions      — this month's billing split by opportunity type, customer
                    branch and Sales owner (the opportunity's creator).
* alerts          — the CEO's exception list: behind target, top customer
                    dropped > CUSTOMER_DROP_WARN_PCT MoM, 90+ day overdue,
                    PO cover under PO_COVER_WARN_MONTHS, concentration.

v3 (21 Sep 2026, CEO asks): the same payload at three zooms and two filters.
* period          — `month` | `quarter` | `fy`. The `month` parameter is the
                    ANCHOR in every mode, so every `?month=` link already in
                    circulation keeps working and the UI needs one date control.
                    Quarters follow the Indian FY (Q1 Apr–Jun … Q4 Jan–Mar).
                    The trend adapts: 12 months, 8 quarters or 5 FYs.
* filters         — `customer_id` / `project_id` narrow the WHOLE page (they go
                    into SQL, not into a post-filter), and the payload carries
                    the option lists for the two dropdowns.
* by_project      — billing, people cost and margin per project.
* by_employee     — billing per person via `Invoice.timesheet_id →
                    timesheets.employee_id`; invoices with no timesheet are
                    reported as `unlinked_billed` with a coverage %, never
                    hidden. Deployed heads with no billing are the bench list.
* targets are stored PER MONTH; a quarter/FY target is the sum of its months,
  so the three zooms can never disagree with one another.

Everything is computed in Python over one 12-month window of invoices and
payments: the volumes are small, and it keeps the report identical on the
Postgres the app runs on and the SQLite the tests use.
"""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import sqlalchemy as sa

from models import (
    BillingUnit, Customer, CustomerBranch, Employee, Invoice, InvoicePayment, Opportunity, POStatus,
    PaymentStatus, Project, ProjectEmployee, PurchaseOrder, Timesheet, TimesheetStatus,
)
from models.base import USERS_TABLE
from models.finance import InvoiceKind

#: Months of history behind the selected month in the trend series.
SERIES_MONTHS = 12
#: Months averaged for the PO runway ("at this burn, cover lasts N months").
RUNWAY_AVG_MONTHS = 3
#: Payment terms assumed when an invoice carries no due date.
DEFAULT_TERMS_DAYS = 30
#: Ageing buckets: (label, min_days_past_due, max_days_past_due or None).
AGEING_BUCKETS: tuple[tuple[str, int, int | None], ...] = (
    ("Not yet due", -10**9, 0),
    ("1–30 days", 1, 30),
    ("31–60 days", 31, 60),
    ("61–90 days", 61, 90),
    ("90+ days", 91, None),
)
#: Share of the month's billing from one customer at which we flag risk.
CONCENTRATION_WARN_PCT = 50.0
TOP_CUSTOMERS = 8
TOP_OVERDUE = 6
#: Months projected forward from the deployed team's billing rates.
FORECAST_MONTHS = 3
#: Working days / hours assumed when a PE bills per day / per hour.
FORECAST_DAYS_PER_MONTH = 21
FORECAST_HOURS_PER_DAY = 8
#: Financial year starts in April (India).
FY_START_MONTH = 4
#: MoM fall in the top customer's billing that raises an alert.
CUSTOMER_DROP_WARN_PCT = 30.0
#: PO cover (months) below which the runway alert fires.
PO_COVER_WARN_MONTHS = 2.0
#: Trailing window for DSO.
DSO_WINDOW_DAYS = 90
#: Row caps for the v3 project / employee tables.
TOP_PROJECTS = 10
TOP_EMPLOYEES = 15
#: Deployed heads that billed nothing before the bench alert fires.
IDLE_HEADS_WARN = 3
#: Calendar months of cash-flow forecast (the CEO's "can we cover payroll" horizon).
CASHFLOW_MONTHS = 3
#: Biggest expected inflows listed by name.
TOP_EXPECTED = 6
#: Historical lateness is clamped here — one ancient unpaid invoice must not
#: push every forecast date a year out.
MAX_SLIP_DAYS = 120
#: app_settings keys (Settings ▸ Revenue targets; Admin/CEO via the report page).
TARGET_MONTH_KEY = "revenue.target_month_default"
TARGET_FY_KEY = "revenue.target_fy"        # the default FY target
TARGET_FY_PREFIX = "revenue.target_fy."   # + FY start year (2026 = FY 2026-27) — overrides the default
TARGET_MONTH_PREFIX = "revenue.target."   # + YYYY-MM


@dataclass(frozen=True)
class Month:
    year: int
    month: int

    @classmethod
    def parse(cls, raw: str | None, today: date) -> "Month":
        """"YYYY-MM" → Month; blank → the current month. Raises ValueError."""
        text = (raw or "").strip()
        if not text:
            return cls(today.year, today.month)
        try:
            y, m = text.split("-", 1)
            year, month = int(y), int(m)
        except ValueError as exc:
            raise ValueError("month must be YYYY-MM") from exc
        if not 1 <= month <= 12 or not 2000 <= year <= 2100:
            raise ValueError("month must be YYYY-MM")
        return cls(year, month)

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year, self.month, monthrange(self.year, self.month)[1])

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return self.start.strftime("%B %Y")

    def shift(self, months: int) -> "Month":
        idx = self.year * 12 + (self.month - 1) + months
        return Month(idx // 12, idx % 12 + 1)


#: How far back each zoom level looks in the trend series.
SERIES_BY_PERIOD = {"month": 12, "quarter": 8, "fy": 5}
PERIOD_TYPES = ("month", "quarter", "fy")


@dataclass(frozen=True)
class Period:
    """A month, an FY quarter or a full financial year — one anchor, three zooms.

    The CEO asked for Month / Quarterly / Yearly on one page (21 Sep 2026). The
    ANCHOR is always a month (`?month=YYYY-MM`, the parameter that already
    existed), and `kind` decides how far it widens. That keeps every existing
    link working — a bare `?month=` is still the month view — and means the
    quarter and FY views need no second date format.

    Quarters follow the **Indian financial year** (Apr–Mar), not the calendar:
    Q1 Apr–Jun · Q2 Jul–Sep · Q3 Oct–Dec · Q4 Jan–Mar. A Jan–Mar quarter
    therefore belongs to the FY that started the PREVIOUS April, which is why
    `fy_start_year` looks backwards for months before April.
    """

    kind: str          # month | quarter | fy
    anchor: Month      # any month inside the period

    # ---------------------------------------------------------------- parsing

    @classmethod
    def parse(cls, kind_raw: str | None, month_raw: str | None, today: date) -> "Period":
        kind = (kind_raw or "month").strip().lower()
        if kind not in PERIOD_TYPES:
            raise ValueError(f"period must be one of {', '.join(PERIOD_TYPES)}")
        return cls(kind, Month.parse(month_raw, today))

    # ------------------------------------------------------------- boundaries

    @property
    def fy_start_year(self) -> int:
        return self.anchor.year if self.anchor.month >= FY_START_MONTH else self.anchor.year - 1

    @property
    def first_month(self) -> Month:
        if self.kind == "month":
            return self.anchor
        if self.kind == "fy":
            return Month(self.fy_start_year, FY_START_MONTH)
        # Quarter: step back to the start of this FY quarter.
        offset = (self.anchor.year * 12 + self.anchor.month - 1) - (self.fy_start_year * 12 + FY_START_MONTH - 1)
        return Month(self.fy_start_year, FY_START_MONTH).shift((offset // 3) * 3)

    @property
    def months(self) -> int:
        return {"month": 1, "quarter": 3, "fy": 12}[self.kind]

    @property
    def last_month(self) -> Month:
        return self.first_month.shift(self.months - 1)

    @property
    def start(self) -> date:
        return self.first_month.start

    @property
    def end(self) -> date:
        return self.last_month.end

    @property
    def month_keys(self) -> list[str]:
        return [self.first_month.shift(i).key for i in range(self.months)]

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    # ------------------------------------------------------------ identity

    @property
    def quarter_index(self) -> int:
        """1-4 within the financial year."""
        offset = (self.first_month.year * 12 + self.first_month.month - 1) \
            - (self.fy_start_year * 12 + FY_START_MONTH - 1)
        return offset // 3 + 1

    @property
    def fy_label(self) -> str:
        y = self.fy_start_year
        return f"FY {y}-{(y + 1) % 100:02d}"

    @property
    def key(self) -> str:
        if self.kind == "month":
            return self.anchor.key
        if self.kind == "quarter":
            return f"{self.fy_start_year}-Q{self.quarter_index}"
        return f"FY{self.fy_start_year}"

    @property
    def label(self) -> str:
        if self.kind == "month":
            return self.anchor.label
        if self.kind == "quarter":
            return (f"Q{self.quarter_index} {self.fy_label} "
                    f"({self.first_month.start:%b}–{self.last_month.start:%b %Y})")
        return f"{self.fy_label} (Apr {self.fy_start_year} – Mar {self.fy_start_year + 1})"

    @property
    def short_label(self) -> str:
        if self.kind == "month":
            return self.anchor.start.strftime("%b %y")
        if self.kind == "quarter":
            return f"Q{self.quarter_index} {str(self.fy_start_year)[2:]}"
        return self.fy_label.replace("FY ", "FY")

    # ------------------------------------------------------------- navigation

    def shift(self, n: int) -> "Period":
        """n periods of the SAME kind, forward or back."""
        return Period(self.kind, self.first_month.shift(n * self.months))

    @property
    def previous(self) -> "Period":
        return self.shift(-1)

    @property
    def year_ago(self) -> "Period":
        """Same slot one year earlier — the like-for-like comparison."""
        if self.kind == "fy":
            return self.shift(-1)
        return Period(self.kind, self.first_month.shift(-12))

    @property
    def comparison_label(self) -> str:
        return {"month": "vs last month", "quarter": "vs last quarter", "fy": "vs last FY"}[self.kind]

def _num(value) -> float:
    return float(value or 0)


def _pct_change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


# --------------------------------------------------------------------------- data
#
# Every query below takes the same optional (customer_id, project_id) filter so
# that "Zoom into one account" narrows the WHOLE page, not just one panel. The
# filter is applied in SQL rather than in Python because `_open_invoices` and
# `_leakage` read rows the window query never sees.


def _invoice_filters(customer_id: int | None, project_id: int | None) -> list:
    """The predicates every invoice read in this module shares.

    A PROFORMA is a request for money, not money (23 Sep 2026 flow): it draws
    no PO, takes no payment and may be returned. Counting it as billed /
    outstanding overstated revenue and ageing until 28 Sep 2026 — every
    figure here is over ISSUED Tax invoices only.
    """
    out = [Invoice.kind != InvoiceKind.PROFORMA.value]
    if customer_id:
        out.append(Project.customer_id == customer_id)
    if project_id:
        out.append(Invoice.project_id == project_id)
    return out


def _invoices_in_window(db: Session, start: date, end: date,
                        customer_id: int | None = None, project_id: int | None = None) -> list[dict]:
    """Every invoice in the window with the dimensions the report splits by.

    The employee columns come through `Invoice.timesheet_id` — a timesheet-driven
    invoice names exactly one project employee, which is what makes employee-wise
    revenue possible at all. A manually raised invoice has no timesheet, so it
    lands in the "not linked to a timesheet" bucket rather than being dropped.
    """
    rows = db.execute(
        select(
            Invoice.id, Invoice.invoice_number, Invoice.invoice_date, Invoice.due_date,
            Invoice.sub_total, Invoice.tax_amount, Invoice.grand_total,
            Invoice.balance_amount, Invoice.payment_status,
            Project.customer_id, Customer.name,
            Project.id, Project.branch_id, Opportunity.opp_type, Opportunity.created_by,
            CustomerBranch.branch_name, Project.name,
            Invoice.timesheet_id, Timesheet.employee_id,
            Employee.first_name, Employee.last_name, Employee.employee_code, Employee.current_ctc,
        )
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
        .outerjoin(Opportunity, Opportunity.id == Project.opportunity_id)
        .outerjoin(CustomerBranch, CustomerBranch.id == Project.branch_id)
        .outerjoin(Timesheet, Timesheet.id == Invoice.timesheet_id)
        .outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Invoice.invoice_date >= start, Invoice.invoice_date <= end,
               *_invoice_filters(customer_id, project_id))
    ).all()
    return [
        {
            "id": r[0], "number": r[1], "invoice_date": r[2], "due_date": r[3],
            "sub_total": _num(r[4]), "tax": _num(r[5]), "grand_total": _num(r[6]),
            "balance": _num(r[7]), "status": getattr(r[8], "value", r[8]),
            "customer_id": r[9], "customer": r[10],
            "project_id": r[11], "branch_id": r[12],
            "opp_type": getattr(r[13], "value", r[13]), "owner_id": r[14],
            "branch": r[15], "project": r[16],
            "timesheet_id": r[17], "employee_id": r[18],
            "employee": " ".join(p for p in (r[19], r[20]) if p) or None,
            "employee_code": r[21],
            "employee_ctc": _num(r[22]) if r[22] is not None else None,
        }
        for r in rows
    ]


def _payments_in_window(db: Session, start: date, end: date,
                        customer_id: int | None = None, project_id: int | None = None) -> list[dict]:
    rows = db.execute(
        select(InvoicePayment.payment_date, InvoicePayment.amount, Project.customer_id,
               Invoice.project_id)
        .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
        .join(Project, Project.id == Invoice.project_id)
        .where(InvoicePayment.payment_date >= start, InvoicePayment.payment_date <= end,
               *_invoice_filters(customer_id, project_id))
    ).all()
    return [{"date": r[0], "amount": _num(r[1]), "customer_id": r[2], "project_id": r[3]}
            for r in rows]


def _open_invoices(db: Session, up_to: date,
                   customer_id: int | None = None, project_id: int | None = None) -> list[dict]:
    rows = db.execute(
        select(Invoice.invoice_number, Invoice.invoice_date, Invoice.due_date, Invoice.balance_amount,
               Customer.name)
        .join(Project, Project.id == Invoice.project_id)
        .join(Customer, Customer.id == Project.customer_id)
        .where(Invoice.payment_status != PaymentStatus.PAID, Invoice.invoice_date <= up_to,
               Invoice.balance_amount > 0, *_invoice_filters(customer_id, project_id))
    ).all()
    return [{"number": r[0], "invoice_date": r[1], "due_date": r[2], "balance": _num(r[3]),
             "customer": r[4]} for r in rows]


def _filter_options(db: Session, start: date, end: date) -> dict:
    """EVERY customer and project, flagged with whether it billed in the window.

    This used to list only the ones that had billed, which read as a bug: the
    CEO opened "All customers" on a quiet month and saw two names out of a book
    of dozens (reported 21 Sep 2026). It is also the wrong question — "why did
    this account bill nothing?" is exactly what a revenue page should answer, so
    an account with no invoices must still be selectable. The `billed` flag lets
    the UI group them ("Billed in this period" / "No billing") without hiding
    anything, and `active` lets it mark dormant records rather than dropping
    them (a filter that silently omits a row is worse than a longer list).
    """
    billed_customers = {
        r[0] for r in db.execute(
            select(Project.customer_id)
            .join(Invoice, Invoice.project_id == Project.id)
            .where(Invoice.invoice_date >= start, Invoice.invoice_date <= end)
            .distinct()
        ).all() if r[0]
    }
    billed_projects = {
        r[0] for r in db.execute(
            select(Invoice.project_id)
            .where(Invoice.invoice_date >= start, Invoice.invoice_date <= end)
            .distinct()
        ).all() if r[0]
    }
    customers = db.execute(select(Customer.id, Customer.name, Customer.status)).all()
    projects = db.execute(
        select(Project.id, Project.name, Project.customer_id, Project.status)).all()

    def _live(value) -> bool:
        return str(getattr(value, "value", value) or "").lower() != "inactive"

    return {
        "customers": sorted(
            ({"id": c[0], "name": c[1] or f"#{c[0]}",
              "billed": c[0] in billed_customers, "active": _live(c[2])}
             for c in customers),
            key=lambda r: (r["name"] or "").lower(),
        ),
        "projects": sorted(
            ({"id": p[0], "name": p[1] or f"#{p[0]}", "customer_id": p[2],
              "billed": p[0] in billed_projects, "active": _live(p[3])}
             for p in projects),
            key=lambda r: (r["name"] or "").lower(),
        ),
    }


# --------------------------------------------------------------------------- sections


def _in(period: Period, rows: list[dict], key: str = "invoice_date") -> list[dict]:
    """The subset of `rows` whose date falls inside the selected period."""
    return [r for r in rows if r[key] and period.contains(r[key])]


def _sum(rows: list[dict], key: str) -> float:
    return sum(r[key] for r in rows)


def _headline(period: Period, invoices: list[dict], payments: list[dict],
              open_rows: list[dict]) -> dict:
    this = _in(period, invoices)
    prev, yoy = period.previous, period.year_ago
    billed = _sum(this, "sub_total")
    billed_prev = _sum(_in(prev, invoices), "sub_total")
    billed_yoy = _sum(_in(yoy, invoices), "sub_total")
    collected = _sum(_in(period, payments, "date"), "amount")
    collected_prev = _sum(_in(prev, payments, "date"), "amount")
    return {
        "billed": round(billed, 2),
        "gst": round(_sum(this, "tax"), 2),
        "billed_incl_gst": round(_sum(this, "grand_total"), 2),
        "invoices": len(this),
        "collected": round(collected, 2),
        "collection_rate_pct": round(collected / billed * 100, 1) if billed > 0 else None,
        "outstanding": round(_sum(open_rows, "balance"), 2),
        "open_invoices": len(open_rows),
        "billed_prev": round(billed_prev, 2),
        "billed_mom_pct": _pct_change(billed, billed_prev),
        "billed_yoy": round(billed_yoy, 2),
        "billed_yoy_pct": _pct_change(billed, billed_yoy),
        "collected_prev": round(collected_prev, 2),
        "collected_mom_pct": _pct_change(collected, collected_prev),
        # Per-period averages: a quarter's ₹9 M means little until it reads as
        # ₹3 M a month next to the month view.
        "months_in_period": period.months,
        "billed_per_month": round(billed / period.months, 2),
        "comparison_label": period.comparison_label,
    }


def _series(period: Period, invoices: list[dict], payments: list[dict]) -> list[dict]:
    """The trend at the SELECTED zoom: 12 months, 8 quarters or 5 financial years."""
    points = SERIES_BY_PERIOD[period.kind]
    out = []
    for offset in range(-(points - 1), 1):
        p = period.shift(offset)
        rows = _in(p, invoices)
        out.append({"month": p.key, "label": p.short_label,
                    "billed": round(_sum(rows, "sub_total"), 2),
                    "collected": round(_sum(_in(p, payments, "date"), "amount"), 2),
                    "invoices": len(rows)})
    return out


def _by_customer(period: Period, invoices: list[dict], payments: list[dict]) -> dict:
    this = _in(period, invoices)
    prev_by: dict[int, float] = defaultdict(float)
    for i in _in(period.previous, invoices):
        prev_by[i["customer_id"]] += i["sub_total"]
    total = _sum(this, "sub_total")
    billed: dict[int, float] = defaultdict(float)
    names: dict[int, str] = {}
    count: dict[int, int] = defaultdict(int)
    for i in this:
        billed[i["customer_id"]] += i["sub_total"]
        count[i["customer_id"]] += 1
        names[i["customer_id"]] = i["customer"]
    paid: dict[int, float] = defaultdict(float)
    for p in _in(period, payments, "date"):
        paid[p["customer_id"]] += p["amount"]
    rows = sorted(
        ({"customer_id": cid, "customer": names[cid], "billed": round(amt, 2), "invoices": count[cid],
          "collected": round(paid.get(cid, 0.0), 2),
          "previous": round(prev_by.get(cid, 0.0), 2),
          "change_pct": _pct_change(amt, prev_by.get(cid, 0.0)),
          "share_pct": round(amt / total * 100, 1) if total > 0 else 0.0}
         for cid, amt in billed.items()),
        key=lambda r: r["billed"], reverse=True,
    )
    shares = [r["share_pct"] for r in rows]
    top1 = shares[0] if shares else 0.0
    return {
        "rows": rows[:TOP_CUSTOMERS],
        "customers": len(rows),
        "top1_share_pct": top1,
        "top3_share_pct": round(sum(shares[:3]), 1),
        "concentration_risk": top1 >= CONCENTRATION_WARN_PCT,
        "top_customer": rows[0]["customer"] if rows else None,
    }


def _ageing(open_rows: list[dict], today: date) -> dict:
    buckets = [{"label": label, "amount": 0.0, "count": 0} for label, _lo, _hi in AGEING_BUCKETS]
    by_customer: dict[str, float] = defaultdict(float)
    for r in open_rows:
        due = r["due_date"] or (r["invoice_date"] + timedelta(days=DEFAULT_TERMS_DAYS))
        past = (today - due).days
        for idx, (_label, lo, hi) in enumerate(AGEING_BUCKETS):
            if past >= lo and (hi is None or past <= hi):
                buckets[idx]["amount"] += r["balance"]
                buckets[idx]["count"] += 1
                break
        if past > 0:
            by_customer[r["customer"]] += r["balance"]
    for b in buckets:
        b["amount"] = round(b["amount"], 2)
    overdue = round(sum(b["amount"] for b in buckets[1:]), 2)
    top = sorted(({"customer": c, "overdue": round(a, 2)} for c, a in by_customer.items()),
                 key=lambda r: r["overdue"], reverse=True)[:TOP_OVERDUE]
    return {"buckets": buckets, "overdue_total": overdue,
            "overdue_90_plus": buckets[-1]["amount"], "top_overdue_customers": top}


def _pipeline(db: Session, series: list[dict], period: Period,
              customer_id: int | None = None, project_id: int | None = None) -> dict:
    invoiced = select(Invoice.id).where(Invoice.timesheet_id == Timesheet.id).exists()
    q = (select(Timesheet.id, Timesheet.approved_figures)
         .where(Timesheet.status == TimesheetStatus.APPROVED, ~invoiced))
    po_q = (select(func.coalesce(func.sum(PurchaseOrder.balance_value), 0))
            .where(PurchaseOrder.status == POStatus.ACTIVE))
    if customer_id or project_id:
        q = q.join(Project, Project.id == Timesheet.project_id)
        if customer_id:
            q = q.where(Project.customer_id == customer_id)
            po_q = po_q.where(PurchaseOrder.customer_id == customer_id)
        if project_id:
            q = q.where(Timesheet.project_id == project_id)
    sheets = db.execute(q).all()
    amount = 0.0
    for _id, figures in sheets:
        totals = (figures or {}).get("totals") if isinstance(figures, dict) else None
        amount += _num((totals or {}).get("sub_total"))
    po_balance = _num(db.execute(po_q).scalar())
    # The runway is always expressed in MONTHS, whatever the zoom, so the number
    # means the same thing on all three views.
    recent = [s["billed"] for s in series[-RUNWAY_AVG_MONTHS:]]
    avg = (sum(recent) / len(recent) / period.months) if recent else 0.0
    return {
        "approved_uninvoiced": {"count": len(sheets), "amount": round(amount, 2)},
        "active_po_balance": round(po_balance, 2),
        "avg_monthly_billed": round(avg, 2),
        "po_cover_months": round(po_balance / avg, 1) if avg > 0 else None,
    }


def _efficiency(db: Session, billed: float, months: int) -> dict:
    deployed = int(db.execute(
        select(func.count(ProjectEmployee.id))
        .where(ProjectEmployee.is_active.is_(True), ProjectEmployee.is_exit.is_(False))).scalar() or 0)
    return {"deployed_heads": deployed,
            "revenue_per_head": round(billed / deployed, 2) if deployed else None,
            "revenue_per_head_per_month": round(billed / deployed / months, 2) if deployed else None}


def _leakage(db: Session, period: Period, customer_id: int | None = None,
             project_id: int | None = None) -> dict:
    pairs = [(period.first_month.shift(i).year, period.first_month.shift(i).month)
             for i in range(period.months)]
    q = (select(Timesheet.approved_figures)
         .where(Timesheet.status == TimesheetStatus.APPROVED,
                sa.or_(*[sa.and_(Timesheet.year == y, Timesheet.month == m) for y, m in pairs])))
    if customer_id or project_id:
        q = q.join(Project, Project.id == Timesheet.project_id)
        if customer_id:
            q = q.where(Project.customer_id == customer_id)
        if project_id:
            q = q.where(Timesheet.project_id == project_id)
    rows = db.execute(q).all()
    lop = covered = no_billing = 0.0
    for (figures,) in rows:
        for line in ((figures or {}).get("line_items") or []) if isinstance(figures, dict) else []:
            lop += _num(line.get("loss_of_pay_days"))
            covered += _num(line.get("lop_covered_days"))
            no_billing += _num(line.get("no_billing_days_excluded"))
    return {"approved_sheets": len(rows), "lop_days": round(lop, 2),
            "lop_covered_by_weekend_work_days": round(covered, 2),
            "no_billing_period_days": round(no_billing, 2)}


# --------------------------------------------------------------------------- v2 sections


def _setting_number(db: Session, key: str) -> float | None:
    """A numeric app_settings value, or None when unset / not a number."""
    row = db.execute(sa.text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
    if not row or row[0] in (None, ""):
        return None
    try:
        return float(str(row[0]).replace(",", ""))
    except ValueError:
        return None


def fy_for(month: Month) -> tuple[Month, Month, str]:
    """(first month, last month, label) of the Indian financial year holding `month`."""
    start_year = month.year if month.month >= FY_START_MONTH else month.year - 1
    first = Month(start_year, FY_START_MONTH)
    last = first.shift(11)
    return first, last, f"FY {start_year}-{(start_year + 1) % 100:02d}"


def _targets(db: Session, period: Period, invoices: list[dict], series: list[dict],
             today: date) -> dict:
    """Targets are stored PER MONTH; a quarter or FY target is the sum of its months.

    Keeping one storage granularity means the Targets modal never has to know
    which zoom the CEO was on, and a quarter can never silently disagree with
    the three months it contains.
    """
    default_month_target = _setting_number(db, TARGET_MONTH_KEY)
    overrides = {k: _setting_number(db, TARGET_MONTH_PREFIX + k) for k in period.month_keys}
    resolved = [v if v is not None else default_month_target for v in overrides.values()]
    known = [v for v in resolved if v is not None]
    period_target = round(sum(known), 2) if known else None
    fy_first, fy_last, fy_label = fy_for(period.anchor)
    fy_override = _setting_number(db, TARGET_FY_PREFIX + str(fy_first.year))
    fy_target = fy_override if fy_override is not None else _setting_number(db, TARGET_FY_KEY)

    # The ANCHOR's own month and quarter targets, whatever the zoom — the Targets
    # dialog edits all three rungs (month · quarter · FY) from one place.
    anchor_override = _setting_number(db, TARGET_MONTH_PREFIX + period.anchor.key)
    anchor_month_target = anchor_override if anchor_override is not None else default_month_target
    quarter = Period("quarter", period.anchor)
    q_resolved = [v if v is not None else default_month_target
                  for v in (_setting_number(db, TARGET_MONTH_PREFIX + k) for k in quarter.month_keys)]
    q_known = [v for v in q_resolved if v is not None]
    quarter_target = round(sum(q_known), 2) if q_known else None

    billed = _sum(_in(period, invoices), "sub_total")
    is_current = period.contains(today)
    total_days = (period.end - period.start).days + 1
    days_elapsed = (today - period.start).days + 1 if is_current else total_days
    run_rate = (round(billed / days_elapsed * total_days, 2)
                if is_current and days_elapsed > 0 else round(billed, 2))

    fy_keys = [fy_first.shift(i).key for i in range(12)]
    # "Elapsed" stops at TODAY's month, not the period's last month — at the FY
    # zoom the period ends next March, and counting those months as elapsed
    # left zero months remaining, no "needed / month" and a projection equal
    # to billed-to-date (found on the CEO dashboard, 28 Sep 2026).
    cutoff = min(period.last_month.key, Month(today.year, today.month).key)
    elapsed_keys = [k for k in fy_keys if k <= cutoff]
    fy_billed = sum(i["sub_total"] for i in invoices if _month_key(i["invoice_date"]) in elapsed_keys)
    # The trailing average used for the FY projection must be per MONTH, whatever
    # the series points measure.
    recent = [s["billed"] for s in series[-RUNWAY_AVG_MONTHS:]]
    avg_recent = (sum(recent) / len(recent) / period.months) if recent else 0.0
    months_remaining = 12 - len(elapsed_keys)
    fy_projection = round(fy_billed + avg_recent * months_remaining, 2)

    def _pct(value: float, target: float | None) -> float | None:
        return round(value / target * 100, 1) if target else None

    return {
        # Names kept from v2 (`month_*`) so existing callers and the dashboard
        # tile keep working; they now mean "the selected period".
        "month_target": period_target,
        "month_target_is_default": bool(known) and all(v is None for v in overrides.values()),
        "month_attainment_pct": _pct(billed, period_target),
        "month_gap": round(period_target - billed, 2) if period_target is not None else None,
        "run_rate": run_rate,
        "run_rate_attainment_pct": _pct(run_rate, period_target),
        "is_current_month": is_current,
        "period_label": period.label,
        "months_with_target": len(known),
        "fy_label": fy_label,
        "fy_start": fy_first.key,
        "fy_end": fy_last.key,
        "fy_target": fy_target,
        "fy_target_is_default": fy_override is None and fy_target is not None,
        "fy_key": str(fy_first.year),
        "anchor_month": period.anchor.key,
        "anchor_month_target": anchor_month_target,
        "anchor_month_target_is_override": anchor_override is not None,
        "quarter_key": quarter.key,
        "quarter_label": quarter.short_label,
        "quarter_target": quarter_target,
        "quarter_months_with_target": len(q_known),
        "fy_billed_to_date": round(fy_billed, 2),
        "fy_attainment_pct": _pct(fy_billed, fy_target),
        "fy_months_elapsed": len(elapsed_keys),
        "fy_months_remaining": months_remaining,
        "fy_projection": fy_projection,
        "fy_projection_pct": _pct(fy_projection, fy_target),
        "fy_required_monthly": (round((fy_target - fy_billed) / months_remaining, 2)
                                if fy_target is not None and months_remaining > 0 else None),
    }


def _monthly_rate(rate: float, unit) -> float:
    """Monthly billing equivalent of a PE's rate — mirrors timesheet_invoice_preview's
    YEARLY = rate / 12 rule; Daily/Hourly use the forecast working-month constants."""
    unit_value = getattr(unit, "value", unit)
    if unit_value == BillingUnit.YEARLY.value:
        return rate / 12
    if unit_value == BillingUnit.DAILY.value:
        return rate * FORECAST_DAYS_PER_MONTH
    if unit_value == BillingUnit.HOURLY.value:
        return rate * FORECAST_DAYS_PER_MONTH * FORECAST_HOURS_PER_DAY
    return rate


def _overlap_fraction(start: date | None, end: date | None, window) -> float:
    """Share of `window` (by days) an assignment running start..end covers.

    `window` is anything with `.start` / `.end` — a Month or a Period — so the
    same proration serves the month view and the FY view.
    """
    first, last = window.start, window.end
    lo = max(first, start) if start else first
    hi = min(last, end) if end else last
    if hi < lo:
        return 0.0
    return ((hi - lo).days + 1) / ((last - first).days + 1)


def _deployed_rows(db: Session, customer_id: int | None = None,
                   project_id: int | None = None) -> list[dict]:
    q = (select(ProjectEmployee.id, ProjectEmployee.billing_rate, ProjectEmployee.billing_unit,
                ProjectEmployee.onboarding_date, ProjectEmployee.billing_date,
                ProjectEmployee.exit_date, ProjectEmployee.is_exit, ProjectEmployee.is_active,
                Employee.current_ctc, Employee.first_name, Employee.last_name,
                Project.customer_id, Customer.name,
                Project.id, Project.name, ProjectEmployee.employee_id, Employee.employee_code)
         .join(Employee, Employee.id == ProjectEmployee.employee_id)
         .join(Project, Project.id == ProjectEmployee.project_id)
         .join(Customer, Customer.id == Project.customer_id)
         .where(ProjectEmployee.is_active.is_(True)))
    if customer_id:
        q = q.where(Project.customer_id == customer_id)
    if project_id:
        q = q.where(ProjectEmployee.project_id == project_id)
    rows = db.execute(q).all()
    return [{
        "pe_id": r[0], "rate": _num(r[1]), "unit": r[2],
        # Exited with no date recorded = gone for good (never counted again).
        "start": r[4] or r[3], "exit": r[5] or (date(1900, 1, 1) if r[6] else None), "is_exit": bool(r[6]),
        "ctc": _num(r[8]) if r[8] is not None else None,
        "name": " ".join(p for p in (r[9], r[10]) if p),
        "customer_id": r[11], "customer": r[12],
        "project_id": r[13], "project": r[14],
        "employee_id": r[15], "employee_code": r[16],
    } for r in rows]


def _margin(period: Period, invoices: list[dict], deployed: list[dict]) -> dict:
    """Billed minus people cost, by customer.

    `current_ctc` is the ANNUAL figure, so a period costs CTC / 12 × months,
    prorated by the share of the period each assignment actually covered.
    """
    this = _in(period, invoices)
    billed_by: dict[int, float] = defaultdict(float)
    names: dict[int, str] = {}
    for i in this:
        billed_by[i["customer_id"]] += i["sub_total"]
        names[i["customer_id"]] = i["customer"]
    cost_by: dict[int, float] = defaultdict(float)
    heads_by: dict[int, int] = defaultdict(int)
    missing_ctc = 0
    for d in deployed:
        frac = _overlap_fraction(d["start"], d["exit"], period)
        if frac <= 0:
            continue
        names.setdefault(d["customer_id"], d["customer"])
        heads_by[d["customer_id"]] += 1
        if d["ctc"] is None or d["ctc"] <= 0:
            missing_ctc += 1
            continue
        cost_by[d["customer_id"]] += d["ctc"] / 12 * period.months * frac
    rows = []
    for cid in set(billed_by) | set(cost_by):
        b, c = billed_by.get(cid, 0.0), cost_by.get(cid, 0.0)
        rows.append({"customer_id": cid, "customer": names.get(cid, f"#{cid}"),
                     "billed": round(b, 2), "cost": round(c, 2), "margin": round(b - c, 2),
                     "margin_pct": round((b - c) / b * 100, 1) if b > 0 else None,
                     "heads": heads_by.get(cid, 0)})
    rows.sort(key=lambda r: r["margin"])
    billed = sum(billed_by.values())
    cost = sum(cost_by.values())
    return {
        "billed": round(billed, 2),
        "cost": round(cost, 2),
        "gross_margin": round(billed - cost, 2),
        "gross_margin_pct": round((billed - cost) / billed * 100, 1) if billed > 0 else None,
        "heads_without_ctc": missing_ctc,
        "loss_making": [r for r in rows if r["margin"] < 0 and r["billed"] > 0][:TOP_CUSTOMERS],
        "by_customer": sorted(rows, key=lambda r: r["billed"], reverse=True)[:TOP_CUSTOMERS],
    }


# --------------------------------------------------------------------------- v3 sections


def _by_project(period: Period, invoices: list[dict], deployed: list[dict]) -> dict:
    """Project-wise billing, cost and margin — the delivery view of the same money."""
    this = _in(period, invoices)
    total = _sum(this, "sub_total")
    prev_by: dict[int, float] = defaultdict(float)
    for i in _in(period.previous, invoices):
        prev_by[i["project_id"]] += i["sub_total"]
    billed: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    meta: dict[int, dict] = {}
    for i in this:
        pid = i["project_id"]
        billed[pid] += i["sub_total"]
        count[pid] += 1
        meta[pid] = {"project": i["project"] or f"#{pid}", "customer": i["customer"],
                     "customer_id": i["customer_id"]}
    cost: dict[int, float] = defaultdict(float)
    heads: dict[int, int] = defaultdict(int)
    missing = 0
    for d in deployed:
        frac = _overlap_fraction(d["start"], d["exit"], period)
        if frac <= 0:
            continue
        pid = d["project_id"]
        meta.setdefault(pid, {"project": d["project"] or f"#{pid}", "customer": d["customer"],
                              "customer_id": d["customer_id"]})
        heads[pid] += 1
        if d["ctc"] is None or d["ctc"] <= 0:
            missing += 1
            continue
        cost[pid] += d["ctc"] / 12 * period.months * frac
    rows = []
    for pid in set(billed) | set(cost):
        b, c = billed.get(pid, 0.0), cost.get(pid, 0.0)
        info = meta.get(pid, {})
        rows.append({
            "project_id": pid, "project": info.get("project", f"#{pid}"),
            "customer": info.get("customer"), "customer_id": info.get("customer_id"),
            "billed": round(b, 2), "invoices": count.get(pid, 0),
            "previous": round(prev_by.get(pid, 0.0), 2),
            "change_pct": _pct_change(b, prev_by.get(pid, 0.0)),
            "cost": round(c, 2), "margin": round(b - c, 2),
            "margin_pct": round((b - c) / b * 100, 1) if b > 0 else None,
            "heads": heads.get(pid, 0),
            "share_pct": round(b / total * 100, 1) if total > 0 else 0.0,
        })
    rows.sort(key=lambda r: r["billed"], reverse=True)
    return {
        "rows": rows[:TOP_PROJECTS],
        "projects": len(rows),
        "billed": round(total, 2),
        "heads_without_ctc": missing,
        "loss_making": sorted([r for r in rows if r["margin"] < 0 and r["billed"] > 0],
                              key=lambda r: r["margin"])[:TOP_PROJECTS],
    }


def _by_employee(period: Period, invoices: list[dict], deployed: list[dict]) -> dict:
    """Employee-wise revenue, via `Invoice.timesheet_id → timesheets.employee_id`.

    Only timesheet-driven invoices name a person. Manual invoices are reported
    as `unlinked_billed` with a `coverage_pct` rather than being hidden, because
    a per-head table that quietly covers 60 % of revenue reads as if it covered
    all of it.
    """
    this = _in(period, invoices)
    total = _sum(this, "sub_total")
    billed: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    meta: dict[int, dict] = {}
    unlinked = 0.0
    unlinked_count = 0
    for i in this:
        eid = i["employee_id"]
        if not eid:
            unlinked += i["sub_total"]
            unlinked_count += 1
            continue
        billed[eid] += i["sub_total"]
        count[eid] += 1
        meta.setdefault(eid, {"name": i["employee"] or f"#{eid}", "code": i["employee_code"],
                              "customer": i["customer"], "project": i["project"],
                              "ctc": i["employee_ctc"]})
    # Cost and the customer/project a head sits on come from the PE assignment,
    # which is authoritative; the invoice join only supplies the revenue.
    by_emp: dict[int, dict] = {}
    for d in deployed:
        frac = _overlap_fraction(d["start"], d["exit"], period)
        if frac <= 0:
            continue
        eid = d["employee_id"]
        slot = by_emp.setdefault(eid, {"cost": 0.0, "frac": 0.0, "ctc": d["ctc"],
                                       "name": d["name"], "code": d["employee_code"],
                                       "customer": d["customer"], "project": d["project"]})
        slot["frac"] += frac
        if d["ctc"] and d["ctc"] > 0:
            slot["cost"] += d["ctc"] / 12 * period.months * frac
    missing = sum(1 for v in by_emp.values() if not v["ctc"])
    rows = []
    for eid in set(billed) | set(by_emp):
        info = by_emp.get(eid) or {}
        fallback = meta.get(eid, {})
        b = billed.get(eid, 0.0)
        c = round(info.get("cost", 0.0), 2)
        rows.append({
            "employee_id": eid,
            "employee": info.get("name") or fallback.get("name") or f"#{eid}",
            "employee_code": info.get("code") or fallback.get("code"),
            "customer": info.get("customer") or fallback.get("customer"),
            "project": info.get("project") or fallback.get("project"),
            "billed": round(b, 2), "invoices": count.get(eid, 0),
            "cost": c, "margin": round(b - c, 2),
            "margin_pct": round((b - c) / b * 100, 1) if b > 0 else None,
            "billed_per_month": round(b / period.months, 2),
            "deployed": eid in by_emp,
        })
    rows.sort(key=lambda r: r["billed"], reverse=True)
    linked = round(total - unlinked, 2)
    billing_heads = sum(1 for r in rows if r["billed"] > 0)
    idle = [r for r in rows if r["deployed"] and r["billed"] <= 0]
    return {
        "rows": rows[:TOP_EMPLOYEES],
        "employees": len(rows),
        "billing_heads": billing_heads,
        "linked_billed": linked,
        "unlinked_billed": round(unlinked, 2),
        "unlinked_invoices": unlinked_count,
        "coverage_pct": round(linked / total * 100, 1) if total > 0 else None,
        "avg_revenue_per_head": round(linked / billing_heads, 2) if billing_heads else None,
        # Deployed but nothing billed in the period — the CEO's "who is on the
        # bench (or whose timesheet never turned into an invoice)" list.
        "idle_heads": [{"employee": r["employee"], "customer": r["customer"],
                        "project": r["project"], "cost": r["cost"]} for r in idle][:TOP_EMPLOYEES],
        "idle_count": len(idle),
        "idle_cost": round(sum(r["cost"] for r in idle), 2),
        "heads_without_ctc": missing,
    }


def _effective_due(invoice_date: date, due_date: date | None) -> date:
    """An invoice with no due date falls due DEFAULT_TERMS_DAYS after issue —
    the same rule the ageing buckets use, so the two can never disagree."""
    return due_date or (invoice_date + timedelta(days=DEFAULT_TERMS_DAYS))


def _slip_days(db: Session, today: date, customer_id: int | None = None,
               project_id: int | None = None) -> float:
    """How late this company ACTUALLY gets paid, in days past the due date.

    Amount-weighted over the last year's receipts, because one small invoice
    paid very late should not move the forecast as much as a large one paid on
    time. Clamped at 0 (paying early does not pull the forecast forward — that
    would be planning on a favour) and at MAX_SLIP_DAYS.
    """
    q = (select(InvoicePayment.amount, InvoicePayment.payment_date,
                Invoice.invoice_date, Invoice.due_date)
         .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
         .where(InvoicePayment.payment_date >= today - timedelta(days=365),
                InvoicePayment.payment_date <= today))
    if customer_id or project_id:
        q = q.join(Project, Project.id == Invoice.project_id).where(
            *_invoice_filters(customer_id, project_id))
    rows = db.execute(q).all()
    weighted = total = 0.0
    for amount, paid_on, issued, due in rows:
        if not paid_on or not issued:
            continue
        amt = _num(amount)
        if amt <= 0:
            continue
        late = (paid_on - _effective_due(issued, due)).days
        weighted += amt * max(0, late)
        total += amt
    if total <= 0:
        return 0.0
    return round(min(MAX_SLIP_DAYS, weighted / total), 1)


def _cashflow(db: Session, deployed: list[dict], today: date,
              customer_id: int | None = None, project_id: int | None = None) -> dict:
    """When the money actually lands, against the people cost until it does.

    DSO already says how SLOWLY we collect. This says WHEN — open invoices
    placed on the calendar by due date, next to the payroll we carry in the
    same months, so the answer to "can we cover the next quarter" is one line
    rather than an inference.

    Deliberately always **as of today**, even when the page is showing a past
    period: a forecast of a month that has closed is not a forecast. The UI
    labels it so.

    Two dates per invoice: `expected` on the agreed terms, and
    `expected_at_pace` on this company's measured lateness (`slip_days`).
    Planning on the first alone is how a business runs out of cash while its
    ageing report still looks fine.
    """
    open_rows = _open_invoices(db, today + timedelta(days=365 * 5), customer_id, project_id)
    slip = _slip_days(db, today, customer_id, project_id)
    months = [Month(today.year, today.month).shift(i) for i in range(CASHFLOW_MONTHS)]
    horizon_end = months[-1].end

    buckets = {m.key: {"expected": 0.0, "at_pace": 0.0, "invoices": 0} for m in months}
    overdue_amt = 0.0
    overdue_n = 0
    beyond_amt = 0.0
    beyond_n = 0
    inflows: list[dict] = []
    for r in open_rows:
        due = _effective_due(r["invoice_date"], r["due_date"])
        paced = due + timedelta(days=int(slip))
        if due < today:
            overdue_amt += r["balance"]
            overdue_n += 1
        inflows.append({"customer": r["customer"], "number": r["number"],
                        "amount": round(r["balance"], 2),
                        "due": due.isoformat(), "expected": paced.isoformat(),
                        "overdue_days": max(0, (today - due).days)})
        # An overdue invoice is still cash we expect, so it lands in the month
        # its PACED date falls in rather than vanishing from the forecast.
        for key, when in (("expected", due), ("at_pace", paced)):
            slot = _month_key(max(when, today))
            if slot in buckets:
                buckets[slot][key] += r["balance"]
                if key == "expected":
                    buckets[slot]["invoices"] += 1
        if due > horizon_end:
            beyond_amt += r["balance"]
            beyond_n += 1

    rows = []
    cost_total = expected_total = paced_total = 0.0
    for m in months:
        cost = sum(d["ctc"] / 12 * _overlap_fraction(d["start"], d["exit"], m)
                   for d in deployed if d["ctc"])
        b = buckets[m.key]
        expected_total += b["expected"]
        paced_total += b["at_pace"]
        cost_total += cost
        rows.append({
            "month": m.key, "label": m.start.strftime("%b %y"),
            "expected": round(b["expected"], 2),
            "expected_at_pace": round(b["at_pace"], 2),
            "invoices": b["invoices"],
            "people_cost": round(cost, 2),
            "net": round(b["expected"] - cost, 2),
            "net_at_pace": round(b["at_pace"] - cost, 2),
        })

    # Approved timesheets with no invoice: cash we could pull forward simply by
    # raising the bill, which is the cheapest lever on this page.
    invoiced = select(Invoice.id).where(Invoice.timesheet_id == Timesheet.id).exists()
    tq = (select(Timesheet.id, Timesheet.approved_figures)
          .where(Timesheet.status == TimesheetStatus.APPROVED, ~invoiced))
    if customer_id or project_id:
        tq = tq.join(Project, Project.id == Timesheet.project_id)
        if customer_id:
            tq = tq.where(Project.customer_id == customer_id)
        if project_id:
            tq = tq.where(Timesheet.project_id == project_id)
    sheets = db.execute(tq).all()
    unbilled = 0.0
    for _id, figures in sheets:
        totals = (figures or {}).get("totals") if isinstance(figures, dict) else None
        unbilled += _num((totals or {}).get("sub_total"))

    heads_costed = sum(1 for d in deployed
                       if d["ctc"] and _overlap_fraction(d["start"], d["exit"], months[0]) > 0)
    missing = sum(1 for d in deployed
                  if not d["ctc"] and _overlap_fraction(d["start"], d["exit"], months[0]) > 0)
    inflows.sort(key=lambda r: r["amount"], reverse=True)
    return {
        "as_of": today.isoformat(),
        "months": rows,
        "horizon_months": CASHFLOW_MONTHS,
        "slip_days": slip,
        "overdue": {"amount": round(overdue_amt, 2), "count": overdue_n},
        "beyond_horizon": {"amount": round(beyond_amt, 2), "count": beyond_n},
        "expected_total": round(expected_total, 2),
        "expected_total_at_pace": round(paced_total, 2),
        "people_cost_total": round(cost_total, 2),
        "net_total": round(expected_total - cost_total, 2),
        "net_total_at_pace": round(paced_total - cost_total, 2),
        "covers_cost": paced_total >= cost_total,
        "unbilled_ready": {"amount": round(unbilled, 2), "count": len(sheets)},
        "top_expected": inflows[:TOP_EXPECTED],
        "heads_costed": heads_costed,
        "heads_without_ctc": missing,
        "assumptions": (
            f"Open invoice balances placed on their due date (or issue + {DEFAULT_TERMS_DAYS} days "
            f"when none is set). \"At our pace\" adds the amount-weighted {slip:g} days this company "
            "actually runs late over the last year. People cost is annual CTC ÷ 12 for everyone "
            "deployed in that month; it is NOT total operating cost."
        ),
    }


def _forecast(period: Period, deployed: list[dict], po_balance: float) -> dict:
    """Always the next FORECAST_MONTHS MONTHS, whatever the zoom — a forecast is
    an operational horizon, not a reporting granularity."""
    months = []
    cumulative = 0.0
    base = period.last_month
    for offset in range(1, FORECAST_MONTHS + 1):
        m = base.shift(offset)
        amount = 0.0
        heads = 0
        roll_offs = []
        for d in deployed:
            frac = _overlap_fraction(d["start"], d["exit"], m)
            if frac <= 0:
                continue
            heads += 1
            amount += _monthly_rate(d["rate"], d["unit"]) * frac
            if d["exit"] and m.start <= d["exit"] <= m.end:
                roll_offs.append({"name": d["name"], "customer": d["customer"], "exit": d["exit"].isoformat()})
        cumulative += amount
        months.append({"month": m.key, "label": m.start.strftime("%b %y"), "amount": round(amount, 2),
                       "heads": heads, "roll_offs": roll_offs[:TOP_OVERDUE],
                       "roll_off_count": len(roll_offs),
                       "po_covered": po_balance >= cumulative})
    return {
        "months": months,
        "total": round(cumulative, 2),
        "po_shortfall": round(max(0.0, cumulative - po_balance), 2),
        "assumptions": f"Active project employees × monthly rate (Daily × {FORECAST_DAYS_PER_MONTH}, "
                       f"Hourly × {FORECAST_DAYS_PER_MONTH}×{FORECAST_HOURS_PER_DAY}, Yearly ÷ 12), "
                       "prorated for onboarding and exit dates; new hires not included.",
    }


def _collections(db: Session, period: Period, invoices: list[dict], open_rows: list[dict],
                 today: date, customer_id: int | None = None, project_id: int | None = None) -> dict:
    as_of = today if period.contains(today) else period.end
    window_start = as_of - timedelta(days=DSO_WINDOW_DAYS)
    billed_window = sum(i["sub_total"] + i["tax"] for i in invoices
                        if window_start < i["invoice_date"] <= as_of)
    outstanding = _sum(open_rows, "balance")
    dso = round(outstanding / billed_window * DSO_WINDOW_DAYS, 1) if billed_window > 0 else None
    q = (select(InvoicePayment.amount, InvoicePayment.payment_date, Invoice.invoice_date)
         .join(Invoice, Invoice.id == InvoicePayment.invoice_id)
         .where(InvoicePayment.payment_date >= period.start,
                InvoicePayment.payment_date <= period.end))
    if customer_id or project_id:
        q = q.join(Project, Project.id == Invoice.project_id).where(
            *_invoice_filters(customer_id, project_id))
    paid = db.execute(q).all()
    weighted = sum(_num(a) * max(0, (pd - idt).days) for a, pd, idt in paid if pd and idt)
    total = sum(_num(a) for a, _pd, _idt in paid)
    return {
        "dso_days": dso,
        "dso_window_days": DSO_WINDOW_DAYS,
        "avg_days_to_pay": round(weighted / total, 1) if total > 0 else None,
        "receipts": len(paid),
        "outstanding": round(outstanding, 2),
    }


def _user_names(db: Session, ids: set[int]) -> dict[int, str]:
    """Display names for legacy user ids; the table is not ORM-mapped and the
    test stub has no name column, hence raw SQL inside a savepoint."""
    ids = {int(i) for i in ids if i}
    if not ids:
        return {}
    try:
        with db.begin_nested():
            rows = db.execute(
                sa.text(f"SELECT id, full_name FROM {USERS_TABLE} WHERE id IN :ids").bindparams(
                    sa.bindparam("ids", expanding=True)),
                {"ids": sorted(ids)},
            ).all()
        return {int(r[0]): (r[1] or f"User #{r[0]}") for r in rows}
    except Exception:  # noqa: BLE001 — names are cosmetic
        return {}


def _dimensions(db: Session, period: Period, invoices: list[dict]) -> dict:
    this = _in(period, invoices)
    total = _sum(this, "sub_total")
    owner_names = _user_names(db, {i["owner_id"] for i in this if i["owner_id"]})

    def _split(label_of) -> list[dict]:
        acc: dict[str, float] = defaultdict(float)
        cnt: dict[str, int] = defaultdict(int)
        for i in this:
            label = label_of(i)
            acc[label] += i["sub_total"]
            cnt[label] += 1
        return sorted(({"label": k, "billed": round(v, 2), "invoices": cnt[k],
                        "share_pct": round(v / total * 100, 1) if total > 0 else 0.0}
                       for k, v in acc.items()), key=lambda r: r["billed"], reverse=True)

    return {
        "by_type": _split(lambda i: (i["opp_type"] or "Unlinked").replace("_", " ")),
        "by_branch": _split(lambda i: i["branch"] or f"{i['customer']} (no branch)"),
        "by_owner": _split(lambda i: owner_names.get(i["owner_id"] or 0,
                                                     "Unassigned" if not i["owner_id"] else f"User #{i['owner_id']}")),
    }


def _alerts(period: Period, headline: dict, targets: dict, by_customer: dict, ageing: dict,
            pipeline: dict, margin: dict, invoices: list[dict],
            by_project: dict | None = None, by_employee: dict | None = None,
            cashflow: dict | None = None) -> list[dict]:
    out: list[dict] = []

    def add(key, level, title, detail):
        out.append({"key": key, "level": level, "title": title, "detail": detail})

    if targets["month_target"]:
        pct = targets["run_rate_attainment_pct"] if targets["is_current_month"] else targets["month_attainment_pct"]
        if pct is not None and pct < 100:
            add("target_month", "bad" if pct < 80 else "warn",
                f"{period.label} tracking at {pct:.0f}% of target",
                f"Billed ₹{headline['billed']:,.0f} against a target of ₹{targets['month_target']:,.0f}"
                + (f" (run-rate ₹{targets['run_rate']:,.0f})" if targets["is_current_month"] else "") + ".")
    if targets["fy_target"] and targets["fy_projection_pct"] is not None and targets["fy_projection_pct"] < 100:
        add("target_fy", "warn", f"{targets['fy_label']} projected at {targets['fy_projection_pct']:.0f}% of target",
            f"Projection ₹{targets['fy_projection']:,.0f} vs target ₹{targets['fy_target']:,.0f}; "
            f"need ₹{(targets['fy_required_monthly'] or 0):,.0f}/month for the remaining "
            f"{targets['fy_months_remaining']} month(s).")

    drop_label = period.comparison_label
    for row in by_customer["rows"][:3]:
        prev = row.get("previous") or 0.0
        if prev > 0:
            drop = (prev - row["billed"]) / prev * 100
            if drop >= CUSTOMER_DROP_WARN_PCT:
                add(f"customer_drop:{row['customer_id']}", "warn",
                    f"{row['customer']} billing down {drop:.0f}% {drop_label}",
                    f"₹{prev:,.0f} → ₹{row['billed']:,.0f}.")
    if by_customer["concentration_risk"]:
        add("concentration", "warn", f"{by_customer['top_customer']} is {by_customer['top1_share_pct']:.0f}% of billing",
            "One account carries more than half the period's revenue.")
    if ageing["overdue_90_plus"] > 0:
        add("overdue_90", "bad", f"₹{ageing['overdue_90_plus']:,.0f} overdue beyond 90 days",
            "Escalate collection — " + ", ".join(c["customer"] for c in ageing["top_overdue_customers"][:3]) + ".")
    cover = pipeline["po_cover_months"]
    if cover is not None and cover < PO_COVER_WARN_MONTHS:
        add("po_cover", "bad" if cover < 1 else "warn", f"PO cover is {cover} month(s) at the current billing pace",
            f"Active PO balance ₹{pipeline['active_po_balance']:,.0f}; start renewals now.")
    if margin["loss_making"]:
        names = ", ".join(r["customer"] for r in margin["loss_making"][:3])
        add("loss_making", "warn", f"{len(margin['loss_making'])} loss-making account(s) this period", names + ".")
    if by_project and by_project["loss_making"]:
        names = ", ".join(r["project"] for r in by_project["loss_making"][:3])
        add("loss_making_projects", "warn",
            f"{len(by_project['loss_making'])} project(s) billing below their people cost", names + ".")
    if by_employee and by_employee["idle_count"] >= IDLE_HEADS_WARN:
        add("idle_heads", "warn",
            f"{by_employee['idle_count']} deployed head(s) billed nothing this period",
            f"Carrying ₹{by_employee['idle_cost']:,.0f} of cost — check the bench and unbilled timesheets.")
    # Cash is the one number that ends a company, so it outranks a soft month.
    if cashflow and not cashflow["covers_cost"]:
        short = cashflow["people_cost_total"] - cashflow["expected_total_at_pace"]
        add("cash_shortfall", "bad",
            f"Expected collections fall ₹{short:,.0f} short of people cost over "
            f"{cashflow['horizon_months']} months",
            f"₹{cashflow['expected_total_at_pace']:,.0f} expected at our actual pace "
            f"({cashflow['slip_days']:g} days late on average) against ₹"
            f"{cashflow['people_cost_total']:,.0f} of CTC."
            + (f" ₹{cashflow['unbilled_ready']['amount']:,.0f} is approved but not yet invoiced."
               if cashflow["unbilled_ready"]["amount"] > 0 else ""))
    if cashflow and cashflow["overdue"]["amount"] > 0 and cashflow["slip_days"] > DEFAULT_TERMS_DAYS:
        add("slow_payers", "warn",
            f"Customers pay {cashflow['slip_days']:g} days past due on average",
            f"That is beyond the {DEFAULT_TERMS_DAYS}-day default terms — the forecast assumes it continues.")
    order = {"bad": 0, "warn": 1}
    out.sort(key=lambda a: order.get(a["level"], 2))
    return out


def alert_level(alerts: list[dict]) -> str:
    """Dashboard tile state for a list of alerts: bad > warn > ok."""
    levels = {a["level"] for a in alerts}
    return "bad" if "bad" in levels else "warn" if "warn" in levels else "ok"


# --------------------------------------------------------------------------- entry


def revenue_report(db: Session, month_raw: str | None = None, today: date | None = None,
                   period_raw: str | None = None, customer_id: int | None = None,
                   project_id: int | None = None) -> dict:
    """The whole report for one period, optionally narrowed to a customer/project.

    `month_raw` is the ANCHOR month in every mode — `?month=2026-09&period=quarter`
    is "the quarter containing September 2026". That keeps every `?month=` link
    already in circulation (the month-close email, the dashboard tile) working
    unchanged, and means the UI only ever has one date control.
    """
    today = today or date.today()
    period = Period.parse(period_raw, month_raw, today)
    points = SERIES_BY_PERIOD[period.kind]
    # One extra period back so the trend's first point, the MoM/QoQ comparison
    # and the YoY comparison all have their data.
    window_start = min(period.shift(-points).start, period.year_ago.previous.start)
    invoices = _invoices_in_window(db, window_start, period.end, customer_id, project_id)
    payments = _payments_in_window(db, window_start, period.end, customer_id, project_id)
    open_rows = _open_invoices(db, period.end, customer_id, project_id)

    headline = _headline(period, invoices, payments, open_rows)
    series = _series(period, invoices, payments)
    by_customer = _by_customer(period, invoices, payments)
    ageing = _ageing(open_rows, today)
    pipeline = _pipeline(db, series, period, customer_id, project_id)
    targets = _targets(db, period, invoices, series, today)
    deployed = _deployed_rows(db, customer_id, project_id)
    margin = _margin(period, invoices, deployed)
    by_project = _by_project(period, invoices, deployed)
    by_employee = _by_employee(period, invoices, deployed)
    cashflow = _cashflow(db, deployed, today, customer_id, project_id)
    return {
        # `month` stays the anchor month so the UI's month control round-trips.
        "month": period.anchor.key,
        "period": period.kind,
        "period_key": period.key,
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "months_in_period": period.months,
        "label": period.label,
        "short_label": period.short_label,
        "comparison_label": period.comparison_label,
        "as_of": today.isoformat(),
        "filters": {
            "customer_id": customer_id,
            "project_id": project_id,
            "options": _filter_options(db, window_start, period.end),
        },
        "headline": headline,
        "series": series,
        "by_customer": by_customer,
        "by_project": by_project,
        "by_employee": by_employee,
        "ageing": ageing,
        "pipeline": pipeline,
        "efficiency": _efficiency(db, headline["billed"], period.months),
        "leakage": _leakage(db, period, customer_id, project_id),
        "targets": targets,
        "margin": margin,
        "forecast": _forecast(period, deployed, pipeline["active_po_balance"]),
        "collections": _collections(db, period, invoices, open_rows, today, customer_id, project_id),
        "cashflow": cashflow,
        "dimensions": _dimensions(db, period, invoices),
        "alerts": _alerts(period, headline, targets, by_customer, ageing, pipeline, margin,
                          invoices, by_project, by_employee, cashflow),
    }


def revenue_summary_for_tile(db: Session, today: date) -> dict:
    """The dashboard tile's slice: month-to-date billed + the alert state."""
    report = revenue_report(db, None, today=today)
    return {
        "billed": report["headline"]["billed"],
        "invoices": report["headline"]["invoices"],
        "alerts": report["alerts"],
        "state": alert_level(report["alerts"]),
        "target_pct": report["targets"]["run_rate_attainment_pct"],
    }
