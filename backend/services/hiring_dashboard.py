"""Hiring control tower — the Sales → TA dashboard (23 Sep 2026).

One payload for the CRM Dashboard's top section: positions brought in by
Sales, opportunities created, onboardings, positions closed, what is open
and workable right now, which customers are active, and pace against the
quarter's targets. The whole thing is computed here, server-side, so every
role reads the same number and the client never derives a figure of its own.

Definitions (the ones the page prints — do not let them drift)
-------------------------------------------------------------
* **Pipeline positions** — ``requirements.no_of_positions`` summed over
  requirements CREATED in the period. A requirement is spawned when Sales
  Head approves the opportunity, so its `created_at` is the moment Sales
  "brought the positions in".
* **Opportunities** — distinct opportunities created in the period. One
  opportunity can carry several positions, so this is a separate count.
* **Onboardings** — candidate profiles that reached ``Joined``, placed on the
  day of the Joined transition (activity log), else the Karnex onboarding
  date HR typed, else the customer onboarding date, else the row's
  ``updated_at``. That ladder exists because the profile table has no
  "joined on" column and the log is the only witness of the actual move.
* **Positions closed** — split exactly as the business sheet asked:
  ``fulfilled`` = positions that Joined on requirements that reached
  Fulfilled or Closed; ``customer_closed`` = the positions that did NOT join
  on requirements the customer closed / cancelled. **Customer-closed
  positions are reported beside the fulfilled count and never added to it.**
  A requirement is placed in the period of its closing transition (activity
  log ``FULFILLED`` / ``CLOSED`` / ``CANCELLED`` / ``OPPORTUNITY_STAGE``),
  else its ``updated_at``.
* **Active positions** — open (total − joined) positions on requirements that
  are neither terminal nor rejected: everything still moving, including the
  ones waiting on approval or held.
* **Workable positions** — the subset of active positions TA can source
  today: requirement in ``WORKABLE_STATUSES`` (engineering-approved and not
  on hold). One tuple to edit when the rule changes.
* **Active customers** — customers with at least one live deal
  (``pipeline_stage`` New / Active and approved); listed by name with their
  open positions.
* **Targets** — company-level, per FY quarter: a Sales positions target and a
  TA fulfilment (onboardings) target, stored in ``app_settings`` as
  ``hiring.target.<sales|ta>.<FY>-Q<n>`` with a per-role default quarter
  target (``hiring.target.<sales|ta>.default_quarter``). A month reads a
  third of its quarter; an FY reads the sum of its four quarters — so the
  three zooms can never disagree with each other.
* **Pace** — for the CURRENT period only: actual ÷ working weeks elapsed vs
  gap ÷ working weeks left (Mon–Fri, holidays ignored on purpose — a
  proctoring-grade calendar is not what a pace gauge needs).
* **Hiring stage delays** — candidates currently waiting at Internal L1,
  Internal L2, Customer interview, Offer and Joining, with days since their
  last stage move (activity log ``STATUS_CHANGE``, else ``updated_at``).

`Period` / `Month` are reused from `revenue_report` so the anchor-month +
zoom contract is identical to the CEO Revenue page (Indian FY, Q1 = Apr–Jun).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    AppSetting, CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity,
    OpportunityApprovalStatus, PipelineStage, PipelineStatus, Requirement, RequirementActivityLog,
    RequirementStatus,
)
from services.ist import to_ist
from services.revenue_report import FY_START_MONTH, Month, Period, SERIES_BY_PERIOD

# --------------------------------------------------------------------------- rules

#: Requirements TA can work on today. Edit here when the business rule moves.
WORKABLE_STATUSES: tuple[RequirementStatus, ...] = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)
#: Terminal — the requirement is done, one way or the other.
TERMINAL_STATUSES: tuple[RequirementStatus, ...] = (
    RequirementStatus.FULFILLED,
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)
#: Refused before sourcing ever started — not open, not closed by a customer.
REJECTED_STATUSES: tuple[RequirementStatus, ...] = (
    RequirementStatus.SALES_HEAD_REJECTED,
    RequirementStatus.ENGINEERING_REJECTED,
)
#: Requirements whose positions Joined count as FULFILLED.
FULFILLED_STATUSES: tuple[RequirementStatus, ...] = (
    RequirementStatus.FULFILLED,
    RequirementStatus.CLOSED,
)
#: Requirements whose unfilled positions were closed BY THE CUSTOMER.
CUSTOMER_CLOSED_STATUSES: tuple[RequirementStatus, ...] = (
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)
#: Activity-log actions that mark a requirement's closing moment.
CLOSING_ACTIONS: tuple[str, ...] = ("FULFILLED", "CLOSED", "CANCELLED", "OPPORTUNITY_STAGE")
#: Opportunities that count a customer as "active".
LIVE_STAGES: tuple[PipelineStage, ...] = (PipelineStage.NEW, PipelineStage.ACTIVE)

#: Hiring-stage buckets for the delays panel, in pipeline order.
STAGE_BUCKETS: tuple[tuple[str, str, tuple[PipelineStatus, ...]], ...] = (
    ("internal_l1", "Internal L1",
     (PipelineStatus.TECHNICAL_SCREENING, PipelineStatus.RMG_REVIEW)),
    ("internal_l2", "Internal L2", (PipelineStatus.SALES_SCREENING,)),
    ("customer_interview", "Customer interview",
     (PipelineStatus.CUSTOMER_SCREENING, PipelineStatus.CUSTOMER_INTERVIEW,
      PipelineStatus.L1_FEEDBACK, PipelineStatus.L2_FEEDBACK)),
    ("offer", "Offer",
     (PipelineStatus.SHORTLISTED, PipelineStatus.CUSTOMER_APPROVAL,
      PipelineStatus.HR_SCREENING, PipelineStatus.HR_INTERVIEWING)),
    ("joining", "Joining", (PipelineStatus.PREBOARDING,)),
)
#: Days waiting at a stage before the row turns amber / red.
STAGE_WARN_DAYS = 3
STAGE_BAD_DAYS = 7
#: Top-N customers named on the page (the rest roll into "others").
TOP_CUSTOMERS = 10
#: Share of open positions held by one customer that raises the concentration flag.
CONCENTRATION_WARN_PCT = 50.0

#: app_settings keys. `<role>` is "sales" or "ta"; `<quarter>` is e.g. "2026-Q2".
TARGET_PREFIX = "hiring.target."
TARGET_DEFAULT_SUFFIX = "default_quarter"
TARGET_ROLES = ("sales", "ta")


# --------------------------------------------------------------------------- small helpers


def _as_date(value: datetime | date | None) -> date | None:
    """Calendar day (IST) of a stored timestamp; a date passes through."""
    if value is None:
        return None
    if isinstance(value, datetime):
        local = to_ist(value) if value.tzinfo is not None else value
        return local.date()
    return value


def _ev(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _pct(part: float, whole: float) -> float | None:
    if whole <= 0:
        return None
    return round(part / whole * 100, 1)


def _working_days(start: date, end: date) -> int:
    """Mon–Fri days in [start, end], inclusive. Zero when the range is empty."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    count = full_weeks * 5
    weekday = start.weekday()
    for i in range(rest):
        if (weekday + i) % 7 < 5:
            count += 1
    return count


def target_key(role: str, quarter_key: str) -> str:
    return f"{TARGET_PREFIX}{role}.{quarter_key}"


def target_default_key(role: str) -> str:
    return f"{TARGET_PREFIX}{role}.{TARGET_DEFAULT_SUFFIX}"


def quarter_of(month: Month) -> Period:
    return Period("quarter", month)


def parse_quarter_key(raw: str | None) -> Period:
    """"2026-Q2" → the FY quarter Period (Q1 = Apr–Jun of that FY; Q4 is
    Jan–Mar of the FOLLOWING calendar year). Raises ValueError."""
    text = (raw or "").strip()
    match = re.fullmatch(r"(\d{4})-Q([1-4])", text)
    if not match:
        raise ValueError("quarter must look like 2026-Q2")
    fy_year, q = int(match.group(1)), int(match.group(2))
    if not 2000 <= fy_year <= 2100:
        raise ValueError("quarter must look like 2026-Q2")
    first = Month(fy_year, FY_START_MONTH).shift((q - 1) * 3)
    return Period("quarter", first)


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class ReqRow:
    id: int
    opportunity_id: int
    customer_id: int
    customer_name: str
    positions: int
    joined: int
    status: str
    created_on: date
    closed_on: date | None
    opp_stage: str
    opp_approved: bool

    @property
    def open(self) -> int:
        return max(0, self.positions - self.joined)


def _requirement_rows(db: Session) -> list[ReqRow]:
    """Every requirement with the columns the whole page needs — three
    queries (requirements+opportunity+customer, Joined counts, closing
    timestamps), never per row."""
    rows = db.execute(
        select(
            Requirement.id, Requirement.opportunity_id, Requirement.customer_id, Customer.name,
            Requirement.no_of_positions, Requirement.status, Requirement.created_at,
            Requirement.updated_at, Opportunity.pipeline_stage, Opportunity.approval_status,
        )
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .join(Customer, Customer.id == Requirement.customer_id)
    ).all()
    if not rows:
        return []

    joined_by_opp: dict[int, int] = dict(
        db.execute(
            select(CandidateProfile.opportunity_id, sa.func.count())
            .where(CandidateProfile.pipeline_status == PipelineStatus.JOINED)
            .group_by(CandidateProfile.opportunity_id)
        ).all()
    )
    closing_ts: dict[int, datetime] = dict(
        db.execute(
            select(RequirementActivityLog.requirement_id, sa.func.max(RequirementActivityLog.timestamp))
            .where(RequirementActivityLog.action_type.in_(CLOSING_ACTIONS))
            .group_by(RequirementActivityLog.requirement_id)
        ).all()
    )

    out: list[ReqRow] = []
    for (rid, opp_id, cust_id, cust_name, positions, status, created_at, updated_at,
         stage, approval) in rows:
        status_v = _ev(status)
        terminal = status_v in {s.value for s in TERMINAL_STATUSES}
        closed_on = _as_date(closing_ts.get(rid) or updated_at) if terminal else None
        out.append(ReqRow(
            id=rid, opportunity_id=opp_id, customer_id=cust_id, customer_name=cust_name or "",
            positions=int(positions or 1), joined=int(joined_by_opp.get(opp_id, 0)),
            status=status_v, created_on=_as_date(created_at) or date.today(),
            closed_on=closed_on, opp_stage=_ev(stage),
            opp_approved=_ev(approval) == OpportunityApprovalStatus.APPROVED.value,
        ))
    return out


@dataclass(frozen=True)
class JoinRow:
    profile_id: int
    opportunity_id: int
    joined_on: date


def _joined_rows(db: Session) -> list[JoinRow]:
    """Every Joined profile with the day it joined (see the module docstring
    for the date ladder)."""
    rows = db.execute(
        select(CandidateProfile.id, CandidateProfile.opportunity_id,
               CandidateProfile.karnex_onboarding_date, CandidateProfile.customer_onboarding_date,
               CandidateProfile.updated_at)
        .where(CandidateProfile.pipeline_status == PipelineStatus.JOINED)
    ).all()
    if not rows:
        return []
    joined_ts: dict[int, datetime] = dict(
        db.execute(
            select(CandidateProfileActivityLog.profile_id,
                   sa.func.max(CandidateProfileActivityLog.timestamp))
            .where(CandidateProfileActivityLog.action_type == "STATUS_CHANGE",
                   CandidateProfileActivityLog.comment.like(f"%-> {PipelineStatus.JOINED.value}%"))
            .group_by(CandidateProfileActivityLog.profile_id)
        ).all()
    )
    out: list[JoinRow] = []
    for pid, opp_id, karnex_on, customer_on, updated_at in rows:
        when = (_as_date(joined_ts.get(pid)) or karnex_on or customer_on
                or _as_date(updated_at) or date.today())
        out.append(JoinRow(profile_id=pid, opportunity_id=opp_id, joined_on=when))
    return out


def _opportunity_rows(db: Session) -> list[tuple[int, date]]:
    """(opportunity id, created on) for the opportunities-created trend."""
    rows = db.execute(select(Opportunity.id, Opportunity.created_at)).all()
    return [(oid, _as_date(created) or date.today()) for oid, created in rows]


# --------------------------------------------------------------------------- targets


def _setting_number(db: Session, key: str) -> float | None:
    row = db.get(AppSetting, key)
    if row is None or row.value in (None, ""):
        return None
    try:
        return float(str(row.value).replace(",", ""))
    except ValueError:
        return None


def quarter_target(db: Session, role: str, quarter: Period) -> tuple[float | None, str]:
    """(target, source) for one FY quarter: the quarter's own row, else the
    role's default quarter target, else nothing."""
    own = _setting_number(db, target_key(role, quarter.key))
    if own is not None:
        return own, "quarter"
    default = _setting_number(db, target_default_key(role))
    if default is not None:
        return default, "default"
    return None, "none"


def period_target(db: Session, role: str, period: Period) -> tuple[float | None, str]:
    """A month is a third of its quarter; an FY is the sum of its quarters."""
    if period.kind == "quarter":
        return quarter_target(db, role, period)
    if period.kind == "month":
        value, source = quarter_target(db, role, quarter_of(period.anchor))
        return (round(value / 3, 2) if value is not None else None), source
    total = 0.0
    sources: set[str] = set()
    known = 0
    for q in range(4):
        quarter = Period("quarter", period.first_month.shift(q * 3))
        value, source = quarter_target(db, role, quarter)
        if value is not None:
            total += value
            known += 1
        sources.add(source)
    if known == 0:
        return None, "none"
    return round(total, 2), ("quarter" if "quarter" in sources else "default")


def targets_view(db: Session, period: Period) -> dict:
    """What the Targets modal edits and the gauges read."""
    quarter = quarter_of(period.anchor)
    out: dict = {"quarter_key": quarter.key, "quarter_label": quarter.label}
    for role in TARGET_ROLES:
        own = _setting_number(db, target_key(role, quarter.key))
        default = _setting_number(db, target_default_key(role))
        value, source = period_target(db, role, period)
        out[role] = {"quarter": own, "default_quarter": default,
                     "period": value, "source": source}
    return out


def set_targets(db: Session, *, quarter_key: str | None, sales_quarter: float | None = None,
                ta_quarter: float | None = None, sales_default: float | None = None,
                ta_default: float | None = None, clear: set[str] | None = None) -> None:
    """Write the quarter / default targets. `clear` names rows to delete:
    "sales_quarter", "ta_quarter", "sales_default", "ta_default". Caller commits."""
    clear = clear or set()

    def _put(key: str, value: float | None, description: str) -> None:
        row = db.get(AppSetting, key)
        if value is None:
            if row is not None:
                db.delete(row)
            return
        text = f"{value:.2f}"
        if row is None:
            db.add(AppSetting(key=key, value=text, description=description))
        else:
            row.value = text

    if quarter_key:
        if sales_quarter is not None or "sales_quarter" in clear:
            _put(target_key("sales", quarter_key),
                 None if "sales_quarter" in clear else sales_quarter,
                 f"Sales positions target for {quarter_key}")
        if ta_quarter is not None or "ta_quarter" in clear:
            _put(target_key("ta", quarter_key),
                 None if "ta_quarter" in clear else ta_quarter,
                 f"TA fulfilment (onboardings) target for {quarter_key}")
    if sales_default is not None or "sales_default" in clear:
        _put(target_default_key("sales"), None if "sales_default" in clear else sales_default,
             "Default Sales positions target per quarter")
    if ta_default is not None or "ta_default" in clear:
        _put(target_default_key("ta"), None if "ta_default" in clear else ta_default,
             "Default TA fulfilment target per quarter")


# --------------------------------------------------------------------------- sections


def _in(period: Period, d: date | None) -> bool:
    return d is not None and period.contains(d)


def _pace(actual: float, target: float | None, period: Period, today: date) -> dict:
    """The gauge: current pace vs the pace the rest of the period needs.

    Only a CURRENT period has a "rest"; a closed one reports its final pace
    and no requirement, and a future one has no pace at all.
    """
    is_current = period.contains(today)
    is_past = period.end < today
    last_day = min(today, period.end) if (is_current or is_past) else period.start
    wd_elapsed = _working_days(period.start, last_day) if (is_current or is_past) else 0
    wd_total = _working_days(period.start, period.end)
    wd_left = max(0, wd_total - wd_elapsed)
    weeks_elapsed = wd_elapsed / 5 if wd_elapsed else 0.0
    weeks_left = wd_left / 5
    gap = None if target is None else max(0.0, target - actual)
    current_per_week = round(actual / weeks_elapsed, 2) if weeks_elapsed > 0 else 0.0
    required_per_week = (round(gap / weeks_left, 2)
                         if (is_current and gap is not None and weeks_left > 0) else None)
    acceleration = (round(required_per_week / current_per_week, 2)
                    if required_per_week is not None and current_per_week > 0 else None)
    if target is None:
        state = "none"
    elif gap == 0:
        state = "ok"
    elif not is_current:
        state = "bad" if is_past else "none"
    elif acceleration is None:
        state = "bad"
    elif acceleration <= 1.0:
        state = "ok"
    elif acceleration <= 1.5:
        state = "warn"
    else:
        state = "bad"
    return {
        "actual": actual, "target": target, "gap": gap, "attainment_pct": _pct(actual, target or 0),
        "is_current": is_current,
        "working_days_total": wd_total, "working_days_elapsed": wd_elapsed,
        "working_days_left": wd_left,
        "current_per_week": current_per_week, "required_per_week": required_per_week,
        "acceleration": acceleration, "state": state,
    }


def _series(period: Period, reqs: list[ReqRow], joins: list[JoinRow],
            opps: list[tuple[int, date]]) -> list[dict]:
    """The trend at the page's zoom: last 12 months / 8 quarters / 5 FYs, oldest first."""
    n = SERIES_BY_PERIOD[period.kind]
    buckets = [period.shift(-i) for i in range(n - 1, -1, -1)]
    out = []
    for b in buckets:
        out.append({
            "key": b.key, "label": b.short_label,
            "positions_in": sum(r.positions for r in reqs if _in(b, r.created_on)),
            "opportunities": sum(1 for _, on in opps if _in(b, on)),
            "onboardings": sum(1 for j in joins if _in(b, j.joined_on)),
            "fulfilled": sum(min(r.joined, r.positions) for r in reqs
                             if r.status in {s.value for s in FULFILLED_STATUSES} and _in(b, r.closed_on)),
            "customer_closed": sum(r.open for r in reqs
                                   if r.status in {s.value for s in CUSTOMER_CLOSED_STATUSES}
                                   and _in(b, r.closed_on)),
        })
    return out


def _funnel(reqs: list[ReqRow]) -> dict:
    """The current snapshot of where every position stands."""
    terminal = {s.value for s in TERMINAL_STATUSES}
    rejected = {s.value for s in REJECTED_STATUSES}
    workable = {s.value for s in WORKABLE_STATUSES}
    active = [r for r in reqs if r.status not in terminal and r.status not in rejected]
    fulfilled = sum(min(r.joined, r.positions) for r in reqs
                    if r.status in {s.value for s in FULFILLED_STATUSES})
    customer_closed = sum(r.open for r in reqs
                          if r.status in {s.value for s in CUSTOMER_CLOSED_STATUSES})
    return {
        "pipeline": sum(r.positions for r in active),
        "active_open": sum(r.open for r in active),
        "workable": sum(r.open for r in active if r.status in workable),
        "awaiting_approval": sum(r.open for r in active
                                 if r.status not in workable
                                 and r.status != RequirementStatus.ON_HOLD.value),
        "on_hold": sum(r.open for r in active if r.status == RequirementStatus.ON_HOLD.value),
        "in_progress_joined": sum(r.joined for r in active),
        "fulfilled": fulfilled,
        "customer_closed": customer_closed,
    }


def _customers(reqs: list[ReqRow]) -> dict:
    """Customers with a live, approved deal — names first, then the numbers."""
    terminal = {s.value for s in TERMINAL_STATUSES}
    rejected = {s.value for s in REJECTED_STATUSES}
    live_stages = {s.value for s in LIVE_STAGES}
    by_customer: dict[int, dict] = {}
    for r in reqs:
        if r.opp_stage not in live_stages or not r.opp_approved:
            continue
        row = by_customer.setdefault(r.customer_id, {
            "id": r.customer_id, "name": r.customer_name, "opportunities": set(),
            "open_positions": 0, "total_positions": 0, "joined": 0,
        })
        row["opportunities"].add(r.opportunity_id)
        if r.status not in terminal and r.status not in rejected:
            row["open_positions"] += r.open
            row["total_positions"] += r.positions
            row["joined"] += r.joined
    rows = sorted(by_customer.values(),
                  key=lambda c: (-c["open_positions"], -c["total_positions"], c["name"].lower()))
    total_open = sum(c["open_positions"] for c in rows)
    listed = []
    for c in rows[:TOP_CUSTOMERS]:
        listed.append({**c, "opportunities": len(c["opportunities"]),
                       "share_pct": _pct(c["open_positions"], total_open)})
    others = rows[TOP_CUSTOMERS:]
    largest = rows[0] if rows else None
    largest_share = _pct(largest["open_positions"], total_open) if largest else None
    return {
        "count": len(rows),
        "total_open_positions": total_open,
        "rows": listed,
        "others": {"count": len(others),
                   "open_positions": sum(c["open_positions"] for c in others)},
        "largest": ({"name": largest["name"], "open_positions": largest["open_positions"],
                     "share_pct": largest_share} if largest else None),
        "concentration_risk": bool(largest_share is not None and largest_share >= CONCENTRATION_WARN_PCT),
    }


def _stage_delays(db: Session, today: date) -> dict:
    """Candidates waiting at each hiring stage and for how long."""
    wanted = {status for _, _, statuses in STAGE_BUCKETS for status in statuses}
    rows = db.execute(
        select(CandidateProfile.id, CandidateProfile.pipeline_status, CandidateProfile.updated_at)
        .where(CandidateProfile.pipeline_status.in_(list(wanted)),
               CandidateProfile.is_hidden.is_(False))
    ).all()
    last_move: dict[int, datetime] = {}
    if rows:
        last_move = dict(
            db.execute(
                select(CandidateProfileActivityLog.profile_id,
                       sa.func.max(CandidateProfileActivityLog.timestamp))
                .where(CandidateProfileActivityLog.action_type == "STATUS_CHANGE",
                       CandidateProfileActivityLog.profile_id.in_([r[0] for r in rows]))
                .group_by(CandidateProfileActivityLog.profile_id)
            ).all()
        )
    bucket_of = {status.value: key for key, _, statuses in STAGE_BUCKETS for status in statuses}
    waits: dict[str, list[int]] = defaultdict(list)
    for pid, status, updated_at in rows:
        since = _as_date(last_move.get(pid)) or _as_date(updated_at) or today
        waits[bucket_of[_ev(status)]].append(max(0, (today - since).days))
    out_rows = []
    for key, label, _ in STAGE_BUCKETS:
        days = waits.get(key, [])
        over_warn = sum(1 for d in days if d >= STAGE_WARN_DAYS)
        over_bad = sum(1 for d in days if d >= STAGE_BAD_DAYS)
        avg = round(sum(days) / len(days), 1) if days else 0.0
        state = "bad" if over_bad else ("warn" if over_warn else "ok")
        out_rows.append({"key": key, "label": label, "count": len(days),
                         "avg_days": avg, "max_days": max(days) if days else 0,
                         "over_warn": over_warn, "over_bad": over_bad, "state": state})
    return {"rows": out_rows, "warn_days": STAGE_WARN_DAYS, "bad_days": STAGE_BAD_DAYS,
            "total_waiting": sum(len(v) for v in waits.values())}


# --------------------------------------------------------------------------- entry point


def hiring_dashboard(db: Session, month_raw: str | None = None, period_raw: str | None = None,
                     today: date | None = None) -> dict:
    """The whole payload. `month` is the ANCHOR at every zoom, like the Revenue page."""
    today = today or date.today()
    period = Period.parse(period_raw, month_raw, today)

    reqs = _requirement_rows(db)
    joins = _joined_rows(db)
    opps = _opportunity_rows(db)

    fulfilled_statuses = {s.value for s in FULFILLED_STATUSES}
    customer_closed_statuses = {s.value for s in CUSTOMER_CLOSED_STATUSES}

    pipeline_positions = sum(r.positions for r in reqs if _in(period, r.created_on))
    opportunities = sum(1 for _, on in opps if _in(period, on))
    onboardings = sum(1 for j in joins if _in(period, j.joined_on))
    fulfilled = sum(min(r.joined, r.positions) for r in reqs
                    if r.status in fulfilled_statuses and _in(period, r.closed_on))
    customer_closed = sum(r.open for r in reqs
                          if r.status in customer_closed_statuses and _in(period, r.closed_on))

    funnel = _funnel(reqs)
    customers = _customers(reqs)
    targets = targets_view(db, period)
    sales_pace = _pace(pipeline_positions, targets["sales"]["period"], period, today)
    ta_pace = _pace(onboardings, targets["ta"]["period"], period, today)

    previous = period.previous
    prev_positions = sum(r.positions for r in reqs if _in(previous, r.created_on))
    prev_onboardings = sum(1 for j in joins if _in(previous, j.joined_on))
    prev_opps = sum(1 for _, on in opps if _in(previous, on))

    return {
        "as_of": today.isoformat(),
        "period": {
            "kind": period.kind, "key": period.key, "label": period.label,
            "short_label": period.short_label, "anchor_month": period.anchor.key,
            "start": period.start.isoformat(), "end": period.end.isoformat(),
            "months": period.months, "is_current": period.contains(today),
            "comparison_label": period.comparison_label,
            "days_left": max(0, (period.end - today).days) if period.contains(today) else 0,
        },
        "kpis": {
            "pipeline_positions": pipeline_positions,
            "pipeline_positions_prev": prev_positions,
            "opportunities": opportunities,
            "opportunities_prev": prev_opps,
            "onboardings": onboardings,
            "onboardings_prev": prev_onboardings,
            "positions_fulfilled": fulfilled,
            "positions_customer_closed": customer_closed,
            "active_positions": funnel["active_open"],
            "workable_positions": funnel["workable"],
            "active_customers": customers["count"],
        },
        "pace": {"sales": sales_pace, "fulfilment": ta_pace},
        "targets": targets,
        "series": _series(period, reqs, joins, opps),
        "funnel": funnel,
        "customers": customers,
        "stage_delays": _stage_delays(db, today),
        "rules": {
            "workable_statuses": [s.value for s in WORKABLE_STATUSES],
            "customer_closed_excluded_from_closed_count": True,
        },
    }
