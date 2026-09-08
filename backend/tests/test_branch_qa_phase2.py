"""Branch-wise Leave & Holiday Policy — QA Phase 2 (integration).

SUITE B — full Nov-2025 month → timesheet → invoice (exact totals)
SUITE E — billing caps (per-day ON/OFF, per-month ON/OFF, initial no-billing)
SUITE F — project inheritance / override via the single shared resolver

Run:  python -m pytest tests/test_branch_qa_phase2.py -q
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from services.branch_policy import (
    resolve_branch_project_policy, classify_day, run_month,
)

D = Decimal


def _branch(**over):
    base = dict(
        holidays_billable=False, weekoff_billable=False, leave_billable=False, comp_off_billable=True,
        hours_required_half_day=D("4"), hours_required_full_day=D("8"),
        hours_required_half_day_comp_off=D("4"), hours_required_full_day_comp_off=D("7"),
        working_hours_per_day=D("8"),
        billing_cycle_start_day=1, billing_cycle_end_day=31,
        is_max_billable_hours_per_day=True, max_billable_hours_per_day=D("8"),
        is_max_billable_hours_per_month=False, max_billable_hours_per_month=None,
        is_max_billable_days_per_month=False, max_billable_days_per_month=None,
        is_initial_no_billing_period=False, initial_no_billing_qty=None, initial_no_billing_period=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _policy(**over):
    return resolve_branch_project_policy(None, _branch(**over))


# Ravi's exact November 2025 (per the spec)
DAYS = [
    {"label": "Nov3", "worked": 8},
    {"label": "Nov4", "worked": 10},
    {"label": "Nov5", "leave": 8},
    {"label": "Nov6", "leave": 8},
    {"label": "Nov7", "worked": 4, "leave": 4},
    {"label": "Nov8", "worked": 7, "weekoff": True},
    {"label": "Nov9", "weekoff": True},
    {"label": "Nov10", "holiday": True},
    {"label": "Nov11", "worked": 5, "holiday": True},
    {"label": "Nov12", "comp_off_taken": True},
    {"label": "Nov13", "worked": 8},
    {"label": "Nov14", "worked": 8},
]


# ===================================================================== SUITE B
def test_suite_B_full_month():
    p = _policy()
    r = run_month(p, DAYS, opening_cl="1.5", opening_comp="0", bill_rate="500")
    assert r["total_billable_hours"] == D("48"), r["total_billable_hours"]
    assert r["invoice_amount"] == D("24000"), r["invoice_amount"]
    assert r["comp_off_closing"] == D("0.5"), r["comp_off_closing"]
    assert r["cl_used"] == D("1.5") and r["cl_closing"] == D("0")
    assert r["lop_days"] == 1
    assert r["non_billable_present_days"] == ["Nov5", "Nov6", "Nov9", "Nov10", "Nov12"]


# ===================================================================== SUITE E
def test_suite_E_caps():
    # E1 per-day cap ON=8, worked 12 -> 8
    assert classify_day(_policy(), worked_hours=12).billable_hours == D("8")
    # E2 per-day cap OFF, worked 12 -> 12
    assert classify_day(_policy(is_max_billable_hours_per_day=False), worked_hours=12).billable_hours == D("12")
    # E3 month cap 0/OFF -> Suite B stays 48
    assert run_month(_policy(), DAYS, opening_cl="1.5", bill_rate="500")["total_billable_hours"] == D("48")
    # E4 month cap ON=40 -> 48 clamps to 40, invoice 20000
    r4 = run_month(_policy(is_max_billable_hours_per_month=True, max_billable_hours_per_month=D("40")),
                   DAYS, opening_cl="1.5", bill_rate="500")
    assert r4["total_billable_hours"] == D("40") and r4["invoice_amount"] == D("20000")
    # E5 initial no-billing period covering Nov3-5 -> those excluded (48 - 8 - 8 - 0 = 32)
    r5 = run_month(_policy(), DAYS, opening_cl="1.5", bill_rate="500",
                   exclude_labels=frozenset({"Nov3", "Nov4", "Nov5"}))
    assert r5["total_billable_hours"] == D("32"), r5["total_billable_hours"]


# ===================================================================== SUITE F
def test_suite_F_inheritance_and_override():
    branch = _branch()
    # F1 no override -> resolves to branch defaults -> month total 48
    p1 = resolve_branch_project_policy(SimpleNamespace(), branch)
    assert run_month(p1, DAYS, opening_cl="1.5", bill_rate="500")["total_billable_hours"] == D("48")

    branch_policy = resolve_branch_project_policy(None, branch)

    # F2 Leave Billable=Yes on ProjectX -> Nov5 CL bills 8h on ProjectX, 0 on branch
    px2 = resolve_branch_project_policy(SimpleNamespace(leave_billable=True), branch)
    assert classify_day(px2, leave_hours=8).billable_hours == D("8")
    assert classify_day(branch_policy, leave_hours=8).billable_hours == D("0")

    # F3 Holidays Billable=Yes on ProjectX -> Nov10 holiday bills 8h on ProjectX, 0 on branch
    px3 = resolve_branch_project_policy(SimpleNamespace(holidays_billable=True), branch)
    assert classify_day(px3, is_holiday=True).billable_hours == D("8")
    assert classify_day(branch_policy, is_holiday=True).billable_hours == D("0")

    # F4 Max Billable Hrs/Day=10 on ProjectX -> Nov4 bills 10 on ProjectX vs 8 on branch
    px4 = resolve_branch_project_policy(
        SimpleNamespace(max_billable_hours_day=D("10"), is_max_billable_hours_per_day=True), branch)
    assert classify_day(px4, worked_hours=10).billable_hours == D("10")
    assert classify_day(branch_policy, worked_hours=10).billable_hours == D("8")
