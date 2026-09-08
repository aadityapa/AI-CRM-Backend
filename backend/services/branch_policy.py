"""Customer Branch-wise Leave & Holiday Policy — resolution + pure billing rules.

Spec sections:
  §3 billability flags + hour thresholds
  §4 billing cycle + caps (enforced ONLY when the matching Is-* toggle is ON)
  §5 branch Billable Leave Policy sub-table (may be empty — valid)
  §6 Project INHERITS the branch policy but may OVERRIDE any field
     (resolution: project value if set, else branch default).

`resolve_branch_project_policy(project, branch)` is the ONE reusable resolver.
All the billability/threshold/cap helpers are pure (no DB, no framework) so they
are trivially unit-testable.

Field-name note: the Project model carries a few legacy cap column names that
differ from the branch (`max_billable_hours_day` vs `max_billable_hours_per_day`).
The resolver maps them explicitly so callers see one canonical shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from models import (
    BranchHolidayYear,
    CustomerBillingPolicy,
    CustomerBranch,
    CustomerLeavePolicy,
    Holiday,
    LeavePolicyType,
    Project,
)

ZERO = Decimal("0")
ONE = Decimal("1")
HALF = Decimal("0.5")

# built-in fallbacks when neither project nor branch sets a value
_DEFAULT_HALF = Decimal("4")
_DEFAULT_FULL = Decimal("8")
_DEFAULT_WORKING = Decimal("8")


def _pick(project_val, branch_val, default=None):
    """Resolution rule: project value if set (not None), else branch, else default."""
    if project_val is not None:
        return project_val
    if branch_val is not None:
        return branch_val
    return default


def _dec(v):
    return None if v is None else Decimal(str(v))


@dataclass
class ResolvedBillingPolicy:
    """Canonical, fully-resolved policy for a project (project→branch→default)."""

    # §3 billability
    holidays_billable: bool = False
    weekoff_billable: bool = False
    leave_billable: bool = False
    comp_off_billable: bool = False
    # §3 thresholds
    hours_required_half_day: Decimal = _DEFAULT_HALF
    hours_required_full_day: Decimal = _DEFAULT_FULL
    hours_required_half_day_comp_off: Decimal = _DEFAULT_HALF
    hours_required_full_day_comp_off: Decimal = _DEFAULT_FULL
    working_hours_per_day: Decimal = _DEFAULT_WORKING
    # §4 cycle
    billing_frequency: str | None = None
    billing_cycle_start_day: int | None = None
    billing_cycle_end_day: int | None = None
    # §4 caps — value + its Is-* enable toggle
    is_max_billable_hours_per_day: bool = False
    max_billable_hours_per_day: Decimal | None = None
    is_max_billable_hours_per_month: bool = False
    max_billable_hours_per_month: Decimal | None = None
    is_max_billable_days_per_month: bool = False
    max_billable_days_per_month: Decimal | None = None
    is_initial_no_billing_period: bool = False
    initial_no_billing_qty: int | None = None
    initial_no_billing_period: str | None = None

    # ------------------------------------------------------------- day fraction
    def day_fraction_from_hours(self, hours, *, comp_off: bool = False) -> Decimal:
        """Full day when hours >= full threshold, half when >= half threshold, else 0.
        Uses the comp-off thresholds when comp_off=True."""
        h = Decimal(str(hours or 0))
        full = self.hours_required_full_day_comp_off if comp_off else self.hours_required_full_day
        half = self.hours_required_half_day_comp_off if comp_off else self.hours_required_half_day
        if h >= Decimal(str(full)):
            return ONE
        if h >= Decimal(str(half)):
            return HALF
        return ZERO

    # ------------------------------------------------------------- billability
    def is_billable(self, category: str) -> bool:
        """Is a day of the given category billable under this policy?
        category ∈ {present, holiday, weekoff, leave, comp_off}."""
        c = (category or "").lower()
        if c == "present":
            return True
        if c == "holiday":
            return bool(self.holidays_billable)
        if c == "weekoff":
            return bool(self.weekoff_billable)
        if c == "leave":
            return bool(self.leave_billable)
        if c == "comp_off":
            return bool(self.comp_off_billable)
        return False

    # ------------------------------------------------------------- cap enforcement
    def cap_hours_per_day(self, hours) -> Decimal:
        h = Decimal(str(hours or 0))
        if self.is_max_billable_hours_per_day and self.max_billable_hours_per_day is not None:
            return min(h, Decimal(str(self.max_billable_hours_per_day)))
        return h

    def cap_hours_per_month(self, hours) -> Decimal:
        h = Decimal(str(hours or 0))
        if self.is_max_billable_hours_per_month and self.max_billable_hours_per_month is not None:
            return min(h, Decimal(str(self.max_billable_hours_per_month)))
        return h

    def cap_days_per_month(self, days) -> Decimal:
        d = Decimal(str(days or 0))
        if self.is_max_billable_days_per_month and self.max_billable_days_per_month is not None:
            return min(d, Decimal(str(self.max_billable_days_per_month)))
        return d


def resolve_branch_project_policy(project: Project | None,
                                  branch: CustomerBranch | None) -> ResolvedBillingPolicy:
    """The single reusable resolver: project override → branch default → built-in."""
    p = project
    b = branch

    def pb(pattr, battr, default=None, *, dec=False):
        pv = getattr(p, pattr, None) if p is not None else None
        bv = getattr(b, battr, None) if b is not None else None
        val = _pick(pv, bv, default)
        return _dec(val) if dec else val

    return ResolvedBillingPolicy(
        holidays_billable=bool(pb("holidays_billable", "holidays_billable", False)),
        weekoff_billable=bool(pb("weekoff_billable", "weekoff_billable", False)),
        leave_billable=bool(pb("leave_billable", "leave_billable", False)),
        comp_off_billable=bool(pb("comp_off_billable", "comp_off_billable", False)),
        hours_required_half_day=pb("hours_required_half_day", "hours_required_half_day", _DEFAULT_HALF, dec=True),
        hours_required_full_day=pb("hours_required_full_day", "hours_required_full_day", _DEFAULT_FULL, dec=True),
        hours_required_half_day_comp_off=pb("hours_required_half_day_comp_off",
                                            "hours_required_half_day_comp_off", _DEFAULT_HALF, dec=True),
        hours_required_full_day_comp_off=pb("hours_required_full_day_comp_off",
                                            "hours_required_full_day_comp_off", _DEFAULT_FULL, dec=True),
        working_hours_per_day=pb("working_hours_per_day", "working_hours_per_day", _DEFAULT_WORKING, dec=True),
        billing_frequency=_freq(pb("billing_frequency", "billing_frequency", None)),
        billing_cycle_start_day=pb("billing_cycle_start_day", "billing_cycle_start_day", None),
        billing_cycle_end_day=pb("billing_cycle_end_day", "billing_cycle_end_day", None),
        # caps — value columns differ in name on Project (legacy) vs branch
        is_max_billable_hours_per_day=bool(pb("is_max_billable_hours_per_day",
                                              "is_max_billable_hours_per_day", False)),
        max_billable_hours_per_day=pb("max_billable_hours_day", "max_billable_hours_per_day", None, dec=True),
        is_max_billable_hours_per_month=bool(pb("is_max_billable_hours_per_month",
                                                "is_max_billable_hours_per_month", False)),
        max_billable_hours_per_month=pb("max_billable_hours_month", "max_billable_hours_per_month", None, dec=True),
        is_max_billable_days_per_month=bool(pb("is_max_billable_days_per_month",
                                               "is_max_billable_days_per_month", False)),
        max_billable_days_per_month=pb("max_billable_days_month", "max_billable_days_per_month", None, dec=True),
        is_initial_no_billing_period=bool(pb("is_initial_no_billing_period",
                                             "is_initial_no_billing_period", False)),
        initial_no_billing_qty=_pick(
            getattr(p, "initial_no_billing_qty", None) if p is not None else None,
            getattr(b, "initial_no_billing_qty", None) if b is not None else None,
            getattr(p, "no_billing_period_days", None) if p is not None else None,  # legacy project fallback
        ),
        initial_no_billing_period=pb("initial_no_billing_period", "initial_no_billing_period", None),
    )


def _freq(v):
    return getattr(v, "value", v) if v is not None else None


# ---------------------------------------------------------------- effective (branch → customer)
# (out_key, branch_attr, customer_policy_attr, built_in_default)
# NOTE the deliberate name bridges: branch "weekoff_billable" vs customer
# "week_off_billable"; branch "hours_required_*" vs customer "min_hours_*";
# branch "working_hours_per_day" vs customer "normal_hours_per_day".
_EFFECTIVE_FIELDS: tuple[tuple[str, str, str | None, object], ...] = (
    # Paid leaves/year the customer bills even when leave is not billable
    # (the "APTIV rule", 0078) — the CTC slab adds these back to billing days.
    ("billable_leaves_per_year", "billable_leaves_per_year", "billable_leaves_per_year", None),
    ("holidays_billable", "holidays_billable", "holidays_billable", False),
    ("weekoff_billable", "weekoff_billable", "week_off_billable", False),
    ("leave_billable", "leave_billable", "leave_billable", False),
    ("comp_off_billable", "comp_off_billable", "comp_off_billable", False),
    ("min_hours_full_day", "hours_required_full_day", "min_hours_full_day", 8.0),
    ("min_hours_half_day", "hours_required_half_day", "min_hours_half_day", 4.0),
    ("working_hours_per_day", "working_hours_per_day", "normal_hours_per_day", None),
    ("billing_type", "billing_type", "billing_type", None),
    # billing_frequency has no customer-level counterpart — branch value or null.
    ("billing_frequency", "billing_frequency", None, None),
    # Contractual monthly hours cap (branch Billing Properties) — consumed by the
    # New Opportunity CTC slab so projected revenue never exceeds the cap.
    ("max_billable_hours_month", "max_billable_hours_per_month", None, None),
)

_BOOL_KEYS = frozenset(
    {"holidays_billable", "weekoff_billable", "leave_billable", "comp_off_billable"}
)


def effective_customer_branch_policy(db: Session, branch: CustomerBranch) -> dict:
    """Resolve the branch → customer-default → built-in policy for one branch.

    Used by the New Opportunity form to inherit the branch's billing policy.
    Each field: branch value when set (not NULL), else the customer's default
    billing policy row, else the built-in default (None when there is none).
    ``sources`` records where each value came from: "branch" | "customer" |
    "default" | None (no value anywhere).
    """
    cust_policy = db.execute(
        select(CustomerBillingPolicy)
        .where(CustomerBillingPolicy.customer_id == branch.customer_id)
    ).scalars().first()

    data: dict = {"branch_id": branch.id, "customer_id": branch.customer_id}
    sources: dict[str, str | None] = {}
    for key, battr, cattr, default in _EFFECTIVE_FIELDS:
        val = getattr(branch, battr, None)
        src: str | None = "branch"
        if val is None:
            val = getattr(cust_policy, cattr, None) if (cust_policy is not None and cattr) else None
            src = "customer"
        if val is None:
            val = default
            src = "default" if default is not None else None
        if isinstance(val, Decimal):
            val = float(val)
        elif key in _BOOL_KEYS and val is not None:
            val = bool(val)
        data[key] = val
        sources[key] = src

    # --- Leave & Holiday form extras (read-only aggregation, no migration) ---

    # Holidays: count of ACTIVE holidays in this branch's holiday calendar for
    # the current calendar year (no branch-level "holiday year" column exists —
    # BranchHolidayYear rows are per-year headers, so "today's year" is used).
    # Calendar membership mirrors services.project_employees: branch-specific
    # rows OR customer-wide rows (branch NULL) OR global rows (customer NULL).
    # COUNT DISTINCT DATES, not rows: the same date often exists both as a
    # global National row AND inside the branch calendar (e.g. Independence
    # Day) — the timesheet deducts by distinct dates (a date SET), so a
    # double-counted date would inflate this to 11 when the calendar has 10.
    # Blank (null) when zero — same convention as branch_holiday_years' count.
    holiday_year = date.today().year
    holidays_count = int(db.execute(
        select(func.count(func.distinct(Holiday.holiday_date))).where(
            Holiday.is_active.is_(True),
            Holiday.year == holiday_year,
            or_(
                Holiday.branch_id == branch.id,
                and_(Holiday.customer_id == branch.customer_id,
                     Holiday.branch_id.is_(None)),
                and_(Holiday.customer_id.is_(None), Holiday.branch_id.is_(None)),
            ),
        )
    ).scalar() or 0)
    data["holidays_count"] = holidays_count if holidays_count > 0 else None
    sources["holidays_count"] = "branch" if holidays_count > 0 else None

    # NOTE: no weekoff-count column exists on CustomerBranch (verified) — the
    # form keeps its own default; no key is emitted for it.

    # Leave: ACTIVE customer leave policies for this customer, branch-specific
    # OR customer-wide, in ONE select (leave-type name joined in — no N+1).
    # Dedupe per leave_type_id: the branch-specific row wins.
    policy_rows = db.execute(
        select(CustomerLeavePolicy, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == CustomerLeavePolicy.leave_type_id)
        .where(
            CustomerLeavePolicy.customer_id == branch.customer_id,
            CustomerLeavePolicy.is_active.is_(True),
            or_(CustomerLeavePolicy.branch_id == branch.id,
                CustomerLeavePolicy.branch_id.is_(None)),
        )
    ).all()
    chosen: dict[int, tuple[CustomerLeavePolicy, str]] = {}
    for pol, type_name in policy_rows:
        prev = chosen.get(pol.leave_type_id)
        if prev is None or (pol.branch_id is not None and prev[0].branch_id is None):
            chosen[pol.leave_type_id] = (pol, type_name)

    def _agg_source(pols) -> str | None:
        if not pols:
            return None
        return "branch" if any(p.branch_id is not None for p in pols) else "customer"

    def _annual_leave(p) -> Decimal:
        """Annual leave-day entitlement for ONE policy, mirroring the APPLIED
        computation in services.project_employees (so the estimate matches what
        is actually credited):
          One_Time          → initial + period   (upfront, once)
          Monthly           → period × 12 + initial   (period = per-month credit)
          Yearly/Quarterly  → period + initial    (period is already the yearly amount)
        `period` = leave_credit_balance, `initial` = initial_credit_balance.
        """
        ct = (p.leave_credit_type or "Monthly")
        period = Decimal(str(p.leave_credit_balance or 0))
        initial = Decimal(str(p.initial_credit_balance or 0))
        if ct == "One_Time":
            return initial + period
        if ct == "Monthly":
            return period * Decimal(12) + initial
        return period + initial

    picked = [p for p, _ in chosen.values()]
    data["leave_total"] = (
        float(sum(_annual_leave(p) for p in picked))
        if picked else None
    )
    sources["leave_total"] = _agg_source(picked)

    # Strict branch link: at least one active leave policy row with THIS branch_id.
    # New Opportunity Leave & Holiday estimates use this — customer-wide / global
    # calendars alone must not invent Holidays/Leave/Weekoff on the form.
    branch_linked = [p for p in picked if p.branch_id is not None]
    data["has_branch_leave_policy"] = bool(branch_linked)
    sources["has_branch_leave_policy"] = "branch" if branch_linked else None
    data["branch_leave_total"] = (
        float(sum(_annual_leave(p) for p in branch_linked))
        if branch_linked else None
    )
    sources["branch_leave_total"] = "branch" if branch_linked else None

    # Per-type breakup (18 Aug 2026): the Opportunity form shows WHICH leaves
    # make up the paid allowance — "Earned 12 · Sick 6 · Casual 6", not one
    # bare number. Same rows the totals above summed, so they always agree.
    _breakup_ids = {id(p) for p in (branch_linked if branch_linked else picked)}
    breakup = sorted(
        (
            {"name": type_name, "annual": float(_annual_leave(pol))}
            for pol, type_name in chosen.values()
            if id(pol) in _breakup_ids
        ),
        key=lambda r: r["name"],
    )
    data["leave_breakup"] = breakup or None

    # Holidays that belong to THIS branch calendar only (not customer-wide / global).
    branch_holidays_count = int(db.execute(
        select(func.count(func.distinct(Holiday.holiday_date))).where(
            Holiday.is_active.is_(True),
            Holiday.year == holiday_year,
            Holiday.branch_id == branch.id,
        )
    ).scalar() or 0)
    data["branch_holidays_count"] = (
        branch_holidays_count if branch_holidays_count > 0 else None
    )
    sources["branch_holidays_count"] = "branch" if branch_holidays_count > 0 else None

    monthly = [p for p in picked if p.leave_credit_type == "Monthly"]
    data["credit_leave_monthly"] = (
        float(sum(Decimal(str(p.leave_credit_balance or 0)) for p in monthly))
        if monthly else None
    )
    sources["credit_leave_monthly"] = _agg_source(monthly)

    # Leave Policy name: only when EXACTLY ONE active leave type applies to the
    # branch (per the same dedupe rule) — otherwise no single value maps.
    if len(chosen) == 1:
        only_pol, only_name = next(iter(chosen.values()))
        data["leave_policy_name"] = only_name
        sources["leave_policy_name"] = "branch" if only_pol.branch_id is not None else "customer"
    else:
        data["leave_policy_name"] = None
        sources["leave_policy_name"] = None

    data["sources"] = sources
    return data


# ---------------------------------------------------------------- holiday count
def branch_holiday_count(db: Session, branch_id: int, calendar_year: int) -> int:
    """Derived Holiday Count for a branch/year = COUNT of active Holiday rows.
    A BranchHolidayYear row may exist with 0 holidays (renders blank)."""
    return int(db.execute(
        select(func.count(Holiday.id)).where(
            Holiday.branch_id == branch_id,
            Holiday.year == calendar_year,
            Holiday.is_active.is_(True),
        )
    ).scalar() or 0)


def branch_holiday_years(db: Session, branch_id: int) -> list[dict]:
    """The §2 table rows: Calendar Year | Holiday Count (derived) | IsFreeze."""
    rows = db.execute(
        select(BranchHolidayYear).where(BranchHolidayYear.branch_id == branch_id)
        .order_by(BranchHolidayYear.calendar_year)
    ).scalars().all()
    out = []
    for r in rows:
        cnt = branch_holiday_count(db, branch_id, r.calendar_year)
        out.append({
            "id": r.id,
            "calendar_year": r.calendar_year,
            "holiday_count": cnt if cnt > 0 else None,   # blank when zero
            "is_freeze": bool(r.is_freeze),
        })
    return out


# ==========================================================================
# PAID/BILLABLE two-axis day evaluation + comp-off ledger + month runner.
# PAID (payroll) is driven by leave/comp-off BALANCE; BILLABLE (invoice) by the
# resolved billability flags + thresholds + caps. They never share a switch.
# ==========================================================================
from dataclasses import dataclass as _dc  # noqa: E402


@_dc
class DayEval:
    billable_hours: Decimal          # hours the CUSTOMER is charged for this day
    comp_off_delta: Decimal          # comp-off earned(+)/consumed(-) at classify time
    leave_days: Decimal              # leave-balance days this day requests (0 / 0.5 / 1)
    comp_off_days: Decimal           # comp-off-balance days this day requests (taken)
    paid_unconditional: bool         # paid regardless of balance (worked / holiday / week-off)


def classify_day(policy: ResolvedBillingPolicy, *, worked_hours=0, leave_hours=0,
                 is_holiday: bool = False, is_weekoff: bool = False,
                 comp_off_taken: bool = False) -> DayEval:
    """Pure per-day evaluation under a resolved policy.

    BILLABLE hours:
      - normal worked day: cap_per_day(worked)
      - worked on holiday/week-off: cap_per_day(worked) ONLY when Comp Off Billable
      - leave portion: leave_hours ONLY when Leave Billable
      - pure holiday-off / week-off-off: working_hours ONLY when that flag is billable
    COMP-OFF earned ONLY when working on a holiday/week-off (≥full → 1.0, ≥half → 0.5).
    """
    w = Decimal(str(worked_hours or 0))
    lh = Decimal(str(leave_hours or 0))
    wh_per_day = Decimal(str(policy.working_hours_per_day or 8))

    if comp_off_taken:
        return DayEval(ZERO, -ONE, ZERO, ONE, False)

    billable = ZERO
    comp = ZERO
    worked_on_off = (is_holiday or is_weekoff) and w > 0
    if worked_on_off:
        billable += policy.cap_hours_per_day(w) if policy.comp_off_billable else ZERO
        comp += policy.day_fraction_from_hours(w, comp_off=True)   # earned
    elif w > 0:
        billable += policy.cap_hours_per_day(w)

    leave_days = ZERO
    if lh > 0:
        leave_days = (lh / wh_per_day) if wh_per_day > 0 else ZERO
        if policy.leave_billable:
            billable += lh

    if w == 0 and lh == 0:
        if is_holiday and policy.holidays_billable:
            billable += wh_per_day
        elif is_weekoff and policy.weekoff_billable:
            billable += wh_per_day

    paid_unconditional = (w > 0) or is_holiday or is_weekoff
    return DayEval(billable, comp, leave_days, ZERO, paid_unconditional)


def day_paid(ev: DayEval, *, leave_balance, comp_off_balance) -> bool:
    """PAID axis for a single day given current balances."""
    if ev.paid_unconditional:
        return True
    if ev.leave_days > 0:
        return Decimal(str(leave_balance)) >= ev.leave_days
    if ev.comp_off_days > 0:
        return Decimal(str(comp_off_balance)) >= ev.comp_off_days
    return True


def run_month(policy: ResolvedBillingPolicy, days: list[dict], *, opening_cl="0",
              opening_comp="0", bill_rate="0", exclude_labels=frozenset()) -> dict:
    """Sequence a month of day dicts through the two axes.

    day dict keys: label, worked, leave, holiday, weekoff, comp_off_taken.
    Returns billable total (month-cap applied only when its toggle is ON), invoice,
    CL used/closing, comp-off closing, LOP day count, and the non-billable-present list.
    """
    cl = Decimal(str(opening_cl))
    comp = Decimal(str(opening_comp))
    cl_used = ZERO
    total = ZERO
    lop = 0
    non_billable_present: list = []
    per_day: list = []
    for d in days:
        ev = classify_day(
            policy,
            worked_hours=d.get("worked", 0), leave_hours=d.get("leave", 0),
            is_holiday=d.get("holiday", False), is_weekoff=d.get("weekoff", False),
            comp_off_taken=d.get("comp_off_taken", False),
        )
        bh = ZERO if d["label"] in exclude_labels else ev.billable_hours
        is_lop = False
        # leave draw
        if ev.leave_days > 0:
            if cl >= ev.leave_days:
                cl -= ev.leave_days
                cl_used += ev.leave_days
            else:
                is_lop = True
        # comp-off consume (taken)
        if ev.comp_off_days > 0:
            if comp >= ev.comp_off_days:
                comp -= ev.comp_off_days
            else:
                is_lop = True
        # comp-off earn
        if ev.comp_off_delta > 0:
            comp += ev.comp_off_delta
        total += bh
        if bh == 0:
            non_billable_present.append(d["label"])
        if is_lop:
            lop += 1
        per_day.append({"label": d["label"], "billable_hours": float(bh), "lop": is_lop})
    total_capped = policy.cap_hours_per_month(total)
    return {
        "total_billable_hours": total_capped,
        "invoice_amount": total_capped * Decimal(str(bill_rate)),
        "cl_used": cl_used,
        "cl_closing": cl,
        "comp_off_closing": comp,
        "lop_days": lop,
        "non_billable_present_days": non_billable_present,
        "per_day": per_day,
    }


def branch_year_is_frozen(db: Session, branch_id: int, calendar_year: int) -> bool:
    """True when a branch/year is frozen (IsFreeze=ON) — edits to that year's
    holidays must be blocked with a clear message."""
    row = db.execute(
        select(BranchHolidayYear).where(BranchHolidayYear.branch_id == branch_id,
                                        BranchHolidayYear.calendar_year == calendar_year)
    ).scalars().first()
    return bool(row and row.is_freeze)


def ensure_year_editable(db: Session, branch_id: int, calendar_year: int) -> None:
    """Raise a clear 400 when a branch/year is frozen (IsFreeze=ON) — used to block
    edits to that year's holidays."""
    from fastapi import HTTPException
    if branch_year_is_frozen(db, branch_id, calendar_year):
        raise HTTPException(
            status_code=400,
            detail=f"Holiday year {calendar_year} is frozen for this branch and cannot be edited. "
                   f"Unfreeze it first.",
        )
