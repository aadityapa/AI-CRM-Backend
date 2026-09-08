"""Deep business rules for Ask AI — short, factual, concept (not code).

Attached to relevant tabs via HelpEntry.rule_ids. Keep entries concise;
the LLM receives them as grounding context only.
"""
from __future__ import annotations

BUSINESS_RULES: dict[str, str] = {
    "leave_seeding": (
        "Leave seeding / snapshot: When a Project Employee (PE) is mapped to a project, "
        "leave balance rows are seeded from the Customer's Leave Policy (branch-scoped "
        "policies win over customer-wide). Types that already exist on the PE are skipped. "
        "Editing a policy later does not rewrite already-seeded PE rows — only new seeds "
        "and future credit-job runs observe the new policy."
    ),
    "effective_date": (
        "Effective Date (leave policy): Accrual start is max(policy.effective_date, "
        "PE.onboarding_date) over whichever side is set. effective_date only pushes "
        "accrual start later — never earlier than onboarding. If accrual start is still "
        "in the future: upfront (One_Time / Yearly Start_Of_Period) opens at "
        "initial_credit_balance and defers the period grant to the credit job; "
        "Monthly/Quarterly Start behaves like End_Of_Period until that date. "
        "Changing effective_date later never rewrites already-seeded balances."
    ),
    "leave_billing_policy": (
        "Leave Billing Policy (per leave type on Customer / Branch / Project): Controls "
        "whether days of that leave type count as billable on timesheets "
        "(leave_billable_by_type). Project overrides win when set, then branch, then "
        "customer. Separately from leave quotas — this only affects billing math, not "
        "whether the employee may take leave."
    ),
    "comp_off": (
        "Comp-Off billable vs credit: If Comp Off Billable is ON for the effective "
        "billing policy, overtime/comp-off hours on the timesheet are billed and do "
        "NOT earn leave credit. If Comp Off Billable is OFF, approving a timesheet "
        "can grant Comp-Off leave credit (PE-scoped when the employee is project-mapped) "
        "instead of billing those hours. Comp-Off balances may go negative; paid leave "
        "types are clamped at ≥ 0."
    ),
    "loss_of_pay": (
        "Loss of Pay (LOP): A special leave type used when unpaid absence is recorded. "
        "LOP is excluded from paid leave pool totals. Marking LOP on a timesheet day "
        "reduces billable days unless leave is configured as billable for that type. "
        "Employees do not draw from casual/sick/earned balances for LOP."
    ),
    "holiday_scoping": (
        "Holiday scoping by branch: Holidays belong to a customer branch and calendar "
        "year. Timesheets and PE calendars use holidays for the project's billing "
        "branch (project.branch_id, else opportunity.branch_id). A holiday on another "
        "branch does not apply. Branch policy can mark holidays_billable so holiday "
        "days still count toward billable days."
    ),
    "billable_days": (
        "Timesheet billable-day math: Billable days ≈ working days − leave − holidays, "
        "but leave/holidays are subtracted only when that category is NOT billable under "
        "the effective policy (project → branch → customer → defaults). Display often "
        "shows billable_day = hours ÷ 8 (or project max_billable_hours_day). Week-off "
        "and Comp-Off have their own billable flags. Result never goes negative."
    ),
    "rate_effective_date": (
        "Commercial rate Effective Date: PE/project rates are effective-dated. Invoice "
        "and amount math use the rate whose effective_from is the latest on or before "
        "the work date. A mid-period rate change splits the period into sub-periods."
    ),
}


def rules_text(rule_ids: list[str] | None) -> str:
    if not rule_ids:
        return ""
    parts = [BUSINESS_RULES[rid] for rid in rule_ids if rid in BUSINESS_RULES]
    return "\n\n".join(parts)
