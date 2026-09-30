"""Project-Employee billing & leave engine (NEXUS / Karnex).

Pure, side-effect-free calculation rules for the Project Employee module — the
bridge that owns leave, holiday, timesheet, commercial rate, and invoice basis
*per project mapping* (so "Avinash on Samsung" and "Avinash on Microsoft" compute
independently). No DB or framework imports → fully unit-testable.

Implements the Section-4/5 rules agreed in the design:
  - billable days = working - leave - holidays (honouring per-branch billable flags)
  - append-only leave LEDGER → balance is derived, never a mutable running total
  - monthly accrual + proration + carry-forward cap + expiry
  - effective-dated commercial rates → invoice by the rate in effect on the work date
  - PO drawdown ledger with FIFO-by-expiry allocation and expiry/exhaustion blocking
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

TWOPLACES = Decimal("0.01")


def _money(x) -> Decimal:
    return Decimal(str(x)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def _days(x) -> Decimal:
    return Decimal(str(x))


# --------------------------------------------------------------- billable days
def compute_billable_days(
    total_working_days,
    leave_days,
    holiday_days,
    *,
    leave_billable: bool = False,
    holidays_billable: bool = False,
) -> Decimal:
    """Total Billable Days = Working - Leave - Holidays, minus each only when that
    category is NOT billable under the client/branch policy. Never negative."""
    billable = _days(total_working_days)
    if not leave_billable:
        billable -= _days(leave_days)
    if not holidays_billable:
        billable -= _days(holiday_days)
    return billable if billable > 0 else Decimal("0")


# ------------------------------------------------------------ effective rates
@dataclass(frozen=True)
class RateRow:
    effective_from: date
    rate: Decimal

    @staticmethod
    def of(effective_from: date, rate) -> "RateRow":
        return RateRow(effective_from, _money(rate))


def rate_for_date(rate_rows: list[RateRow], on_date: date) -> Decimal | None:
    """The rate in effect on `on_date`: the row with the greatest effective_from
    that is <= on_date. None if the date precedes every rate row."""
    eligible = [r for r in rate_rows if r.effective_from <= on_date]
    if not eligible:
        return None
    return max(eligible, key=lambda r: r.effective_from).rate


@dataclass(frozen=True)
class RateSubPeriod:
    start: date
    end: date  # inclusive
    rate: Decimal


def split_period_by_rate(start: date, end: date, rate_rows: list[RateRow]) -> list[RateSubPeriod]:
    """Split [start, end] into contiguous sub-periods, each with a single rate.
    A mid-period `effective_from` starts a new sub-period. Days before the first
    applicable rate carry rate 0 (flag upstream)."""
    if end < start:
        return []
    boundaries = sorted({start} | {r.effective_from for r in rate_rows if start < r.effective_from <= end})
    subs: list[RateSubPeriod] = []
    for i, b in enumerate(boundaries):
        seg_end = (boundaries[i + 1] - _one_day()) if i + 1 < len(boundaries) else end
        subs.append(RateSubPeriod(b, seg_end, rate_for_date(rate_rows, b) or Decimal("0")))
    return subs


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def invoice_amount_uniform(billable_days, rate) -> Decimal:
    """Simple case: one rate for the whole period."""
    return _money(_days(billable_days) * _money(rate))


def invoice_amount_split(subperiod_billable: list[tuple[Decimal, Decimal]]) -> Decimal:
    """subperiod_billable: list of (billable_days, rate). Sum days*rate."""
    total = Decimal("0")
    for days_, rate in subperiod_billable:
        total += _days(days_) * _money(rate)
    return _money(total)


# ------------------------------------------------------- rate/unit sanity
#: An hour of contract staffing below / above these is almost certainly a rate
#: typed against the WRONG unit — ₹1,414.77 saved "per Month" bills ₹8.42 an
#: hour (reported 25 Sep 2026: a 168-hour month invoiced at ₹1,414.77). Wide on
#: purpose: the check names a likely typo, it never blocks an invoice.
MIN_PLAUSIBLE_HOURLY = Decimal("100")
MAX_PLAUSIBLE_HOURLY = Decimal("25000")

_UNIT_WORD = {"Hourly": "hour", "Daily": "day", "Monthly": "month", "Yearly": "year"}


def rate_unit_warning(unit: str | None, rate, per_hour) -> str | None:
    """Plain-language warning when a rate and its unit look mismatched, else None.

    `per_hour` is what ONE hour of this employee costs under the saved unit
    (the invoice preview already derives it). Pure — callers attach the result
    to what they show before money is committed."""
    if unit is None or rate is None or per_hour is None:
        return None
    per_hour = Decimal(str(per_hour))
    rate = _money(rate)
    if rate <= 0 or per_hour <= 0:
        return None
    word = _UNIT_WORD.get(unit, str(unit).lower())
    if per_hour < MIN_PLAUSIBLE_HOURLY and unit != "Hourly":
        return (f"₹{rate:,} per {word} works out to only ₹{_money(per_hour):,} an hour — "
                f"if ₹{rate:,} is really the hourly rate, change the billing unit to "
                f"Per Hour in the employee's Commercial Details.")
    if per_hour > MAX_PLAUSIBLE_HOURLY:
        return (f"₹{rate:,} per {word} works out to ₹{_money(per_hour):,} an hour — "
                f"check that the billing unit is right in the employee's Commercial Details.")
    return None


# ------------------------------------------------------------- leave ledger
CREDIT_TYPES = {"credit", "carry_forward", "adjust_add"}
DEBIT_TYPES = {"consume", "expire", "adjust_sub"}


@dataclass(frozen=True)
class LeaveLedgerEntry:
    entry_type: str          # credit | consume | carry_forward | expire | adjust_add | adjust_sub
    amount: Decimal
    effective_date: date
    note: str = ""

    @staticmethod
    def of(entry_type: str, amount, effective_date: date, note: str = "") -> "LeaveLedgerEntry":
        if entry_type not in CREDIT_TYPES | DEBIT_TYPES:
            raise ValueError(f"Unknown leave ledger entry_type: {entry_type!r}")
        return LeaveLedgerEntry(entry_type, _days(amount), effective_date, note)


def leave_balance(entries: list[LeaveLedgerEntry], *, as_of: date | None = None) -> Decimal:
    """Balance = Σ credits − Σ debits, up to `as_of` (inclusive) if given."""
    bal = Decimal("0")
    for e in entries:
        if as_of is not None and e.effective_date > as_of:
            continue
        if e.entry_type in CREDIT_TYPES:
            bal += e.amount
        else:
            bal -= e.amount
    return bal


# ---------------------------------------------------------- accrual & rollover
def monthly_accrual(annual_quota, periods: int = 12) -> Decimal:
    """Per-period accrual amount for a monthly-credited policy."""
    if periods <= 0:
        raise ValueError("periods must be > 0")
    return (_days(annual_quota) / Decimal(periods)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def prorate_credit(full_amount, days_present: int, days_in_period: int) -> Decimal:
    """Prorate a period's credit for mid-period join/exit (Prorate Balance Credit)."""
    if days_in_period <= 0:
        return Decimal("0")
    ratio = Decimal(max(0, days_present)) / Decimal(days_in_period)
    return (_days(full_amount) * ratio).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def carry_forward(remaining, max_carry_forward) -> tuple[Decimal, Decimal]:
    """Return (carried, expired) at policy-year rollover given a carry cap."""
    remaining = _days(remaining)
    cap = _days(max_carry_forward)
    if remaining <= 0:
        return Decimal("0"), Decimal("0")
    carried = remaining if remaining <= cap else cap
    return carried, remaining - carried


# ------------------------------------------------------------------- PO drawdown
@dataclass(frozen=True)
class PoLine:
    po_id: int
    balance: Decimal
    expiry: date | None

    @staticmethod
    def of(po_id: int, balance, expiry: date | None) -> "PoLine":
        return PoLine(po_id, _money(balance), expiry)


def po_is_blocked(po: PoLine, on_date: date) -> bool:
    """A PO blocks NEW drawdowns when expired or exhausted."""
    if po.balance <= 0:
        return True
    if po.expiry is not None and on_date > po.expiry:
        return True
    return False


@dataclass(frozen=True)
class Allocation:
    po_id: int
    amount: Decimal


@dataclass(frozen=True)
class DrawdownResult:
    allocations: list[Allocation]
    shortfall: Decimal  # unfunded amount if POs can't cover it (0 when fully funded)


def po_utilization(allocated, consumed) -> tuple[Decimal, str]:
    """Return (pct_0_to_100+, status) where status is ok | warn_80 | blocked.

    blocked when exhausted (consumed >= allocated and allocated > 0, or allocated <= 0
    with any consumption). warn_80 when pct >= 80 and < 100.
    """
    alloc = _money(allocated)
    used = _money(consumed)
    if alloc <= 0:
        return Decimal("100.00"), "blocked"
    pct = (used / alloc * Decimal("100")).quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    if used >= alloc:
        return pct, "blocked"
    if pct >= Decimal("80"):
        return pct, "warn_80"
    return pct, "ok"


def po_status_label(status: str, *, expired: bool = False) -> str:
    if expired:
        return "blocked"
    return status if status in {"ok", "warn_80", "blocked"} else "ok"


def allocate_drawdown(amount, pos: list[PoLine], on_date: date) -> DrawdownResult:
    """Allocate `amount` across live POs, FIFO by earliest expiry (undated last).
    Skips blocked POs. Returns per-PO allocations + any shortfall."""
    need = _money(amount)
    live = [p for p in pos if not po_is_blocked(p, on_date)]
    # earliest expiry first; POs without an expiry drain last.
    live.sort(key=lambda p: (p.expiry is None, p.expiry or date.max))
    allocations: list[Allocation] = []
    for po in live:
        if need <= 0:
            break
        take = po.balance if po.balance <= need else need
        if take > 0:
            allocations.append(Allocation(po.po_id, _money(take)))
            need -= take
    return DrawdownResult(allocations, _money(need) if need > 0 else Decimal("0"))
