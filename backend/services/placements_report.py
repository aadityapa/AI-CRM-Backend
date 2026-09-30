"""Customer placements: internal (redeployed) vs external (new hire) — 25 Sep 2026.

The CEO asked how many of the people we place at customers are our own staff
moving off the bench or another account, and how many we hired for that very
placement. Nothing stored answered it: every candidate who reaches Joined is
created with Employees ▸ Profile Type = Internal (2 Sep 2026 decision), so that
field says "Internal" for both. The answer is DERIVED, at the moment of each
placement, from facts that are already recorded:

* A **placement** is one `project_employees` row, dated by its customer
  onboarding date (falling back to the billing start date). Undated rows cannot
  sit in a period and are counted separately, never guessed.
* **Internal (redeployed)** — the person was on Karnex's rolls before this
  placement: they had an EARLIER placement anywhere (moved from the bench or
  another account), or their Karnex joining date is more than `GRACE_DAYS`
  before the customer onboarding.
* **External (new hire)** — joined Karnex within `GRACE_DAYS` of (or after)
  the customer onboarding, and no earlier placement: we hired them for it.
* **Unknown** — no Karnex joining date and no earlier placement. Shown as its
  own number so HR can fix the record; it is never folded into either side.

Every row carries the `reason` the rule used, so the CEO can check a label
instead of trusting it.

Revenue split: invoices in the window are attributed through their timesheet
(employee + project → that placement → its label), the same attribution the
revenue page's "by employee" table uses. A manual invoice names nobody and is
reported as `unattributed`, not hidden. Proforma invoices are excluded — a
Proforma is not issued revenue.

Windows: the revenue page's Month / Quarter / FY zooms (same anchor-month
contract, `revenue_report.Period`), or a custom date range — weekly buckets up
to `WEEKLY_MAX_DAYS`, monthly beyond. Pure aggregation over three queries.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Customer, Employee, Invoice, Project, ProjectEmployee, Timesheet
from models.finance import InvoiceKind
from services.revenue_report import SERIES_BY_PERIOD, Month, Period

#: Joined Karnex at most this many days before the customer onboarding = hired
#: for the placement. User-confirmed default (25 Sep 2026).
GRACE_DAYS = 30
#: Custom ranges up to this long are bucketed by week, longer ones by month.
WEEKLY_MAX_DAYS = 92
#: A custom range longer than this is almost certainly a typo in a year.
CUSTOM_MAX_DAYS = 3 * 366
#: Drill-down rows returned (newest first); the count says when it was cut.
MAX_ROWS = 500

INTERNAL, EXTERNAL, UNKNOWN = "internal", "external", "unknown"
KINDS = (INTERNAL, EXTERNAL, UNKNOWN)


# --------------------------------------------------------------------------- windows

@dataclass(frozen=True)
class Bucket:
    key: str
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class Window:
    kind: str            # month | quarter | fy | custom
    key: str
    label: str
    start: date
    end: date
    bucket: str          # week | month | quarter | fy
    buckets: tuple[Bucket, ...]
    previous_start: date
    previous_end: date
    comparison_label: str


def _parse_day(raw: str | None, name: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD") from exc


def resolve_window(period_raw: str | None, month_raw: str | None,
                   from_raw: str | None, to_raw: str | None, today: date) -> Window:
    """A zoom window (anchor month + period) or a custom date range.

    Raises ValueError with a message fit for a 400.
    """
    start, end = _parse_day(from_raw, "date_from"), _parse_day(to_raw, "date_to")
    if start or end:
        if not (start and end):
            raise ValueError("A custom range needs both date_from and date_to")
        if end < start:
            raise ValueError("date_to cannot be before date_from")
        days = (end - start).days + 1
        if days > CUSTOM_MAX_DAYS:
            raise ValueError("A custom range can span at most three years")
        buckets = _week_buckets(start, end) if days <= WEEKLY_MAX_DAYS else _month_buckets(start, end)
        return Window(
            kind="custom", key=f"{start.isoformat()}_{end.isoformat()}",
            label=f"{start:%d %b %Y} – {end:%d %b %Y}", start=start, end=end,
            bucket="week" if days <= WEEKLY_MAX_DAYS else "month", buckets=tuple(buckets),
            previous_start=start - timedelta(days=days), previous_end=start - timedelta(days=1),
            comparison_label="vs the previous range",
        )
    period = Period.parse(period_raw, month_raw, today)
    n = SERIES_BY_PERIOD[period.kind]
    buckets = [Bucket(p.key, p.short_label, p.start, p.end)
               for p in (period.shift(i - (n - 1)) for i in range(n))]
    prev = period.previous
    return Window(
        kind=period.kind, key=period.key, label=period.label, start=period.start, end=period.end,
        bucket=period.kind, buckets=tuple(buckets),
        previous_start=prev.start, previous_end=prev.end, comparison_label=period.comparison_label,
    )


def _week_buckets(start: date, end: date) -> list[Bucket]:
    """Monday-based weeks, clipped to the range so no day falls outside it."""
    out: list[Bucket] = []
    cur = start
    while cur <= end:
        week_end = min(cur + timedelta(days=6 - cur.weekday()), end)
        out.append(Bucket(cur.isoformat(), f"{cur:%d %b}", cur, week_end))
        cur = week_end + timedelta(days=1)
    return out


def _month_buckets(start: date, end: date) -> list[Bucket]:
    out: list[Bucket] = []
    m = Month(start.year, start.month)
    while m.start <= end:
        out.append(Bucket(m.key, m.start.strftime("%b %y"), max(m.start, start), min(m.end, end)))
        m = m.shift(1)
    return out


# --------------------------------------------------------------------------- classification

@dataclass(frozen=True)
class Placement:
    pe_id: int
    employee_id: int
    employee: str
    employee_code: str | None
    customer_id: int | None
    customer: str | None
    project_id: int
    project: str
    placed_on: date | None
    karnex_joined: date | None


def classify(placed_on: date, karnex_joined: date | None,
             prior: Placement | None) -> tuple[str, str, int | None]:
    """(kind, reason, gap_days) for one placement. PURE — every branch is tested."""
    gap = (placed_on - karnex_joined).days if karnex_joined else None
    if prior is not None:
        where = " · ".join(p for p in (prior.project, prior.customer) if p)
        when = f" on {prior.placed_on:%d %b %Y}" if prior.placed_on else ""
        return INTERNAL, f"Redeployed — earlier placement {where}{when}", gap
    if karnex_joined is None:
        return UNKNOWN, "No Karnex joining date on the employee record", None
    if gap > GRACE_DAYS:
        return INTERNAL, (f"On Karnex rolls since {karnex_joined:%d %b %Y} "
                          f"({gap} days before onboarding)"), gap
    if gap >= 0:
        return EXTERNAL, f"New hire — joined Karnex {gap} day(s) before onboarding", gap
    return EXTERNAL, f"New hire — joined Karnex {-gap} day(s) after onboarding", gap


def _placement_date():
    return sa.func.coalesce(ProjectEmployee.onboarding_date, ProjectEmployee.billing_date)


def _placements(db: Session, *, employee_ids: set[int] | None = None,
                start: date | None = None, end: date | None = None,
                customer_id: int | None = None, project_id: int | None = None,
                undated: bool = False) -> list[Placement]:
    placed = _placement_date()
    q = (select(ProjectEmployee.id, ProjectEmployee.employee_id, Employee.first_name,
                Employee.last_name, Employee.employee_code, Project.customer_id, Customer.name,
                Project.id, Project.name, placed, Employee.date_of_joining)
         .join(Employee, Employee.id == ProjectEmployee.employee_id)
         .join(Project, Project.id == ProjectEmployee.project_id)
         .outerjoin(Customer, Customer.id == Project.customer_id))
    if employee_ids is not None:
        if not employee_ids:
            return []
        q = q.where(ProjectEmployee.employee_id.in_(employee_ids))
    if undated:
        q = q.where(placed.is_(None))
    if start is not None:
        q = q.where(placed >= start)
    if end is not None:
        q = q.where(placed <= end)
    if customer_id:
        q = q.where(Project.customer_id == customer_id)
    if project_id:
        q = q.where(ProjectEmployee.project_id == project_id)
    return [Placement(r[0], r[1], " ".join(p for p in (r[2], r[3]) if p) or f"Employee #{r[1]}",
                      r[4], r[5], r[6], r[7], r[8], r[9], r[10])
            for r in db.execute(q).all()]


def _prior_index(history: list[Placement]) -> dict[int, Placement | None]:
    """pe_id → the same person's placement just before it (anywhere), or None.

    Ordered by date then id, so two rows on the same day are still ordered
    deterministically; an undated row is never anyone's prior.
    """
    by_emp: dict[int, list[Placement]] = defaultdict(list)
    for p in history:
        if p.placed_on is not None:
            by_emp[p.employee_id].append(p)
    prior: dict[int, Placement | None] = {}
    for rows in by_emp.values():
        rows.sort(key=lambda p: (p.placed_on, p.pe_id))
        for i, p in enumerate(rows):
            prior[p.pe_id] = rows[i - 1] if i else None
    return prior


# --------------------------------------------------------------------------- revenue

def _billed_by_pair(db: Session, start: date, end: date, customer_id: int | None,
                    project_id: int | None) -> tuple[dict[tuple[int, int], float], float]:
    """((employee_id, project_id) → billed excl. GST, unattributed) in the window."""
    q = (select(Timesheet.employee_id, Invoice.project_id, sa.func.sum(Invoice.sub_total))
         .select_from(Invoice)
         .join(Project, Project.id == Invoice.project_id)
         .outerjoin(Timesheet, Timesheet.id == Invoice.timesheet_id)
         .where(Invoice.invoice_date >= start, Invoice.invoice_date <= end,
                sa.or_(Invoice.kind.is_(None), Invoice.kind != InvoiceKind.PROFORMA.value))
         .group_by(Timesheet.employee_id, Invoice.project_id))
    if customer_id:
        q = q.where(Project.customer_id == customer_id)
    if project_id:
        q = q.where(Invoice.project_id == project_id)
    pairs: dict[tuple[int, int], float] = {}
    unattributed = 0.0
    for emp_id, proj_id, total in db.execute(q).all():
        amount = float(total or 0)
        if emp_id is None:
            unattributed += amount
        else:
            pairs[(emp_id, proj_id)] = pairs.get((emp_id, proj_id), 0.0) + amount
    return pairs, round(unattributed, 2)


# --------------------------------------------------------------------------- report

def _pct(part: int | float, whole: int | float) -> float | None:
    return round(part / whole * 100, 1) if whole else None


def placements_report(db: Session, *, month_raw: str | None = None, period_raw: str | None = None,
                      date_from: str | None = None, date_to: str | None = None,
                      customer_id: int | None = None, project_id: int | None = None,
                      today: date | None = None) -> dict:
    today = today or date.today()
    win = resolve_window(period_raw, month_raw, date_from, date_to, today)
    series_start = min(win.buckets[0].start, win.previous_start)

    # 1. Placements anywhere in the series (plus the comparison window).
    placed = _placements(db, start=series_start, end=win.end,
                         customer_id=customer_id, project_id=project_id)
    # 2. Revenue in the selected window, attributed per (employee, project).
    billed, unattributed = _billed_by_pair(db, win.start, win.end, customer_id, project_id)
    # 3. Every placement of every person involved — priors live at OTHER customers too.
    people = {p.employee_id for p in placed} | {emp for emp, _ in billed}
    history = _placements(db, employee_ids=people)
    prior = _prior_index(history)

    def label(p: Placement) -> tuple[str, str, int | None]:
        if p.placed_on is None:
            return UNKNOWN, "No onboarding date on the assignment", None
        return classify(p.placed_on, p.karnex_joined, prior.get(p.pe_id))

    in_window = [p for p in placed if win.start <= p.placed_on <= win.end]
    in_previous = [p for p in placed if win.previous_start <= p.placed_on <= win.previous_end]
    labelled = [(p, *label(p)) for p in in_window]

    counts = {k: 0 for k in KINDS}
    for _, kind, _, _ in labelled:
        counts[kind] += 1
    prev_counts = {k: 0 for k in KINDS}
    for p in in_previous:
        prev_counts[label(p)[0]] += 1
    classified = counts[INTERNAL] + counts[EXTERNAL]

    series = []
    for b in win.buckets:
        row = {"key": b.key, "label": b.label, "start": b.start.isoformat(), "end": b.end.isoformat(),
               INTERNAL: 0, EXTERNAL: 0, UNKNOWN: 0}
        for p in placed:
            if b.start <= p.placed_on <= b.end:
                row[label(p)[0]] += 1
        row["internal_pct"] = _pct(row[INTERNAL], row[INTERNAL] + row[EXTERNAL])
        series.append(row)

    # Revenue: each billed (employee, project) pair → that placement's label.
    by_pair = {(p.employee_id, p.project_id): p for p in history}
    revenue = {k: 0.0 for k in KINDS}
    customers: dict[int | None, dict] = {}

    def cust_slot(cid: int | None, name: str | None) -> dict:
        if cid not in customers:
            customers[cid] = {"customer_id": cid, "customer": name or "—",
                              **{k: 0 for k in KINDS},
                              **{f"billed_{k}": 0.0 for k in KINDS}}
        return customers[cid]

    for (emp_id, proj_id), amount in billed.items():
        p = by_pair.get((emp_id, proj_id))
        kind = label(p)[0] if p else UNKNOWN
        revenue[kind] += amount
        if p is not None:
            cust_slot(p.customer_id, p.customer)[f"billed_{kind}"] += amount
    for p, kind, _, _ in labelled:
        cust_slot(p.customer_id, p.customer)[kind] += 1

    by_customer = []
    for slot in customers.values():
        slot["total"] = slot[INTERNAL] + slot[EXTERNAL] + slot[UNKNOWN]
        slot["internal_pct"] = _pct(slot[INTERNAL], slot[INTERNAL] + slot[EXTERNAL])
        for k in KINDS:
            slot[f"billed_{k}"] = round(slot[f"billed_{k}"], 2)
        by_customer.append(slot)
    by_customer.sort(key=lambda r: (-r["total"], -(r["billed_internal"] + r["billed_external"]),
                                    r["customer"] or ""))

    labelled.sort(key=lambda t: (t[0].placed_on, t[0].pe_id), reverse=True)
    rows = [{
        "pe_id": p.pe_id, "employee_id": p.employee_id, "employee": p.employee,
        "employee_code": p.employee_code, "customer_id": p.customer_id, "customer": p.customer,
        "project_id": p.project_id, "project": p.project,
        "placed_on": p.placed_on.isoformat(),
        "karnex_joined": p.karnex_joined.isoformat() if p.karnex_joined else None,
        "kind": kind, "reason": reason, "gap_days": gap,
        "billed": round(billed.get((p.employee_id, p.project_id), 0.0), 2),
    } for p, kind, reason, gap in labelled[:MAX_ROWS]]

    undated = len(_placements(db, undated=True, customer_id=customer_id, project_id=project_id))
    total_revenue = sum(revenue.values()) + unattributed
    return {
        "as_of": today.isoformat(),
        "window": {
            "kind": win.kind, "key": win.key, "label": win.label,
            "start": win.start.isoformat(), "end": win.end.isoformat(),
            "bucket": win.bucket, "comparison_label": win.comparison_label,
        },
        "headline": {
            "placements": len(in_window),
            "unique_people": len({p.employee_id for p in in_window}),
            **counts,
            "internal_pct": _pct(counts[INTERNAL], classified),
            "external_pct": _pct(counts[EXTERNAL], classified),
            "undated": undated,
            "previous": {"placements": len(in_previous), **prev_counts},
        },
        "series": series,
        "by_customer": by_customer,
        "revenue": {**{k: round(v, 2) for k, v in revenue.items()},
                    "unattributed": unattributed, "total": round(total_revenue, 2),
                    "internal_pct": _pct(revenue[INTERNAL], revenue[INTERNAL] + revenue[EXTERNAL])},
        "rows": rows,
        "rows_truncated": len(labelled) > MAX_ROWS,
        "rules": {
            "grace_days": GRACE_DAYS,
            "internal": (f"Earlier placement anywhere, or Karnex joining date more than "
                         f"{GRACE_DAYS} days before the customer onboarding"),
            "external": f"Joined Karnex within {GRACE_DAYS} days of the customer onboarding (or after it)",
            "unknown": "No Karnex joining date and no earlier placement — fix the employee record",
            "revenue": "Invoices attributed through their timesheet; manual invoices are unattributed; "
                       "Proformas are excluded",
        },
    }
