"""Authoritative Candidate CTC Slab calculations for every opportunity type."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

MONEY_PLACES = Decimal("0.01")
ZERO = Decimal("0")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _zero_when_blank(value: Any) -> Decimal:
    return _decimal(value) or ZERO


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def _is_true(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip().lower() == "true"


def calculate_billing_bases(details: dict | None) -> tuple[Decimal, Decimal | None]:
    """Return (actual billing days, hours).

    BILLABLE means the customer pays for that day, so it stays IN the base
    (user-confirmed 18 Aug 2026): Holidays+Weekoff billable, 24 leave, 12 paid
    → 365−24+12 = 353; nothing billable → 365−10−104−24 = 227 (+12 paid =
    239); everything billable → 365. Customer-paid leaves add back capped at
    what leave deducted. Blank values count as zero; hours stay blank until
    Hours Per Day is supplied. Mirror of ctcSlab.ts::calculateBillingBases —
    change BOTH or the form and the server disagree.
    """
    d = details or {}
    leave_deducted = ZERO if _is_true(d.get("leave_billable")) else _zero_when_blank(d.get("leave"))
    deductions = (
        (ZERO if _is_true(d.get("weekoff_billable")) else _zero_when_blank(d.get("weekoff")))
        + (ZERO if _is_true(d.get("holidays_billable")) else _zero_when_blank(d.get("holidays")))
        + leave_deducted
    )
    paid_add_back = min(_zero_when_blank(d.get("paid_leaves")), leave_deducted)
    days = _money(max(ZERO, Decimal("365") - deductions + paid_add_back))
    hours_per_day = _decimal(d.get("hours_per_day"))
    hours = _money(days * max(ZERO, hours_per_day)) if hours_per_day is not None else None
    return days, hours


def normalize_tm_billing_details(details: dict | None) -> dict:
    """Apply T&M billable-flag / hours defaults and authoritative billing bases.

    Holidays / weekoff / leave are NOT invented here — blank means 0 in
    ``calculate_billing_bases`` (via ``_zero_when_blank``). Callers must send
    real estimates from the form (branch-linked leave policy prefill or user input).
    """
    out = dict(details or {})
    defaults = {
        "hours_per_day": 8,
        "holidays_billable": False,
        "weekoff_billable": False,
        "leave_billable": False,
    }
    for key, value in defaults.items():
        if key not in out:
            out[key] = value
    days, hours = calculate_billing_bases(out)
    out["actual_billing_days"] = float(days)
    out["actual_billing_hours"] = float(hours) if hours is not None else None
    return out


def derive_ctc_row(
    row: dict,
    *,
    opportunity_type: str,
    details: dict | None = None,
) -> dict:
    """Recalculate the complete CTC chain for every opportunity/billing type.

    Client-supplied derived fields are always overwritten. A missing rate,
    billing type, or required base produces blank (None) outputs.
    """
    out = dict(row)
    d = details or {}
    days, hours = calculate_billing_bases(d)

    # Exp Max is derived: ONE YEAR above Exp Min (NEXUS parity, 4 Sep 2026,
    # user decision). Every slab row is one year of the candidate's career
    # (7→8, 8→9, 9→10); the previous midpoint-to-target rule showed 7→9,
    # 8→9, 9→10 for a target of 10, which is not how the reference reads.
    exp_min = _decimal(out.get("exp_min"))
    target_exp = _decimal(out.get("target_exp"))
    out["exp_max"] = float(exp_min + Decimal("1")) if exp_min is not None else None

    rate = _decimal(out.get("rate"))
    opp_type = str(opportunity_type or "").strip()
    billing_type = str(d.get("billing_type") or "").strip()
    annual: Decimal | None = None
    if rate is not None:
        if opp_type == "T&M":
            if billing_type == "Per Hour":
                annual = rate * hours if hours is not None else None
            elif billing_type == "Per Day":
                annual = rate * days
            elif billing_type == "Per Month":
                annual = rate * Decimal("12")
            elif billing_type == "Per Year":
                annual = rate
        elif opp_type == "Work_Package":
            annual = rate
        elif opp_type == "Fixed_Price":
            duration = _decimal(d.get("project_duration_months"))
            # Blank means the total is already annual; zero cannot be annualized.
            annual = (
                rate
                if duration is None
                else rate / (duration / Decimal("12"))
                if duration > ZERO
                else None
            )
        elif opp_type == "Retainer":
            annual = rate * Decimal("12")

    if annual is None:
        out.update({
            "revenue_monthly": None,
            "revenue_annual": None,
            "engineering_budget": None,
            "approved_ctc_lac": None,
        })
        return out

    management_cost_pct = _zero_when_blank(out.get("management_cost_pct"))
    hike_pct = _zero_when_blank(out.get("hike_pct"))
    annual = _money(annual)
    monthly = _money(annual / Decimal("12"))
    engineering_budget = _money(
        annual * (Decimal("1") - management_cost_pct / Decimal("100"))
    )
    # APPRAISAL CYCLES (NEXUS parity, 14 Aug 2026): the number of hikes that
    # must fit inside the fixed budget before the candidate leaves the band —
    # derived as Target − Exp Min − 1 (the joining year needs no hike).
    #   Approved CTC = Budget / (1 + hike%)^cycles
    # so an offer at Approved grows by hike% each appraisal and still fits the
    # budget in the final year. 5→7 = 1 cycle (÷1.1); 6→7 = 0 (full budget);
    # 7→10 = 2 (÷1.21). Legacy rows without exp/target keep the old single
    # division (cycles = 1) so stored opportunities re-derive unchanged.
    if exp_min is not None and target_exp is not None:
        cycles = max(0, int(target_exp - exp_min) - 1)
        out["appraisal_cycle"] = str(cycles)
    else:
        raw_cycle = str(out.get("appraisal_cycle") or "").strip()
        cycles = int(raw_cycle) if raw_cycle.isdigit() else 1
    denominator = (Decimal("1") + hike_pct / Decimal("100")) ** cycles
    approved_ctc = _money(engineering_budget / denominator) if denominator > ZERO else None

    out.update({
        "revenue_monthly": float(monthly),
        "revenue_annual": float(annual),
        "engineering_budget": float(engineering_budget),
        "approved_ctc_lac": float(approved_ctc) if approved_ctc is not None else None,
    })
    return out
