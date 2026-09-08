"""Branch-wise Leave & Holiday Policy — QA Phase 1 (unit).

SUITE A — day-type logic (billable hrs | paid | comp-off delta)
SUITE C — comp-off hour boundaries

Uses the real resolver to build the HARMAN-Bangalore policy, then the pure
classify_day / day_paid functions. GATE 1.

Run:  python -m pytest tests/test_branch_qa_phase1.py -q
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from services.branch_policy import resolve_branch_project_policy, classify_day, day_paid

D = Decimal


def harman_policy():
    """HARMAN - Bangalore config, resolved through the real resolver (no project override)."""
    branch = SimpleNamespace(
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"),
        billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
        is_max_billable_hours_per_month=False, is_max_billable_days_per_month=False,
    )
    return resolve_branch_project_policy(None, branch)


def _eval(policy, *, leave_balance="9", comp_off_balance="9", **kw):
    ev = classify_day(policy, **kw)
    paid = day_paid(ev, leave_balance=leave_balance, comp_off_balance=comp_off_balance)
    return ev.billable_hours, paid, ev.comp_off_delta


# ===================================================================== SUITE A
def test_suite_A_day_types():
    p = harman_policy()
    # A1 worked 8h -> 8 | Yes | 0
    assert _eval(p, worked_hours=8) == (D("8"), True, D("0"))
    # A2 worked 10h -> 8 (capped) | Yes | 0
    assert _eval(p, worked_hours=10) == (D("8"), True, D("0"))
    # A3 CL, balance>=1 -> 0 | Yes | 0
    assert _eval(p, leave_hours=8, leave_balance="1.5") == (D("0"), True, D("0"))
    # A4 leave, balance 0 -> 0 | No (LOP) | 0
    assert _eval(p, leave_hours=8, leave_balance="0") == (D("0"), False, D("0"))
    # A5 holiday off -> 0 | Yes | 0
    assert _eval(p, is_holiday=True) == (D("0"), True, D("0"))
    # A6 week-off off -> 0 | Yes | 0
    assert _eval(p, is_weekoff=True) == (D("0"), True, D("0"))
    # A7 holiday worked 7h -> 7 | Yes | +1.0
    assert _eval(p, worked_hours=7, is_holiday=True) == (D("7"), True, D("1.0"))
    # A8 holiday worked 5h -> 5 | Yes | +0.5
    assert _eval(p, worked_hours=5, is_holiday=True) == (D("5"), True, D("0.5"))
    # A9 comp-off taken -> 0 | Yes (credit-1) | -1.0
    assert _eval(p, comp_off_taken=True, comp_off_balance="1") == (D("0"), True, D("-1.0"))


# ===================================================================== SUITE C
def test_suite_C_comp_off_boundaries():
    p = harman_policy()
    f = lambda h: p.day_fraction_from_hours(D(h), comp_off=True)
    assert f("3.99") == D("0")
    assert f("4") == D("0.5")
    assert f("6.99") == D("0.5")
    assert f("7") == D("1.0")
    assert f("9") == D("1.0")
