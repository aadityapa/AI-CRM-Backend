# Project Employees — TASKS

Track Phases 1–5 for the PE bridge module. Check items only when verified.
Spec: agent prompt 2026-07-14 (see DESIGN-DECISIONS.md D1).

## Phase 1 — Data model
- [x] `ProjectEmployee` bridge fields (onboarding, billing_date, work_mode, experience, is_exit, exit_date)
- [x] UNIQUE `(project_id, employee_id)` + reactivate-on-remap (D4 — not partial unique)
- [x] `project_employee_leave_details` (migration 0023)
- [x] `project_employee_rates` with `is_current_rate` (migration 0023)
- [x] `leave_applications.project_employee_id` / `timesheets.project_employee_id`
- [x] TimesheetPeriod = computed API rollups (D3)
- [x] CustomerLeavePolicy + holidays exist (no Karnex special-case — D5)
- [x] ClientPO + `po_project_allocations` as usage ledger (D7)
- [x] `project_experience_years` column (migration 0024)

## Phase 2 — Business logic
- [x] Mapping seeds leave copy from customer policy + initial rate row
- [x] Leave credit job (service + CLI script + cron note)
- [x] Leave apply keyed by PE; approve debits PE balance; insufficient → 400 (except LOP / comp-off earned)
- [x] Billable-days formula in `project_employee_billing.compute_billable_days`
- [x] Mid-period rate split engine (`split_period_by_rate` / `invoice_amount_split`)
- [x] Invoice path uses PE rates for amount; PO drawdown; 80% warn / 100%+expiry block invoice
- [x] Timesheet entry NEVER blocked by PO (submit gate removed — D8)
- [x] Exit: stop accrual, close/flag open periods, settlement balance flag

## Phase 3 — UI
- [x] Project Employees list page + map modal + detail route
- [x] LIST: Leave Balance, Current Rate, PO status chip, Is Exit; Group-by-Employee; filters project/client/active|exited
- [x] DETAIL: Holidays tab (read-only client calendar); Timesheet formula visible; Commercial & PO drawdown bar
- [x] Token / skeleton / empty / focus-visible polish on list+detail

## Phase 4 — Tests (8 scenarios)
- [x] A — Avinash dual-project leave independence
- [x] B — Different policies same project → different PE balances
- [x] C — Accrual / upfront / mid-month proration
- [x] D — Billable 23−2−1 = 20
- [x] E — Mid-period rate split invoice amount
- [x] F — PO 80% warn / 100% block / expiry block (invoice only)
- [x] G — Duplicate active mapping → 409
- [x] H — Exit stops accrual + settlement flag
- [x] pytest green for `test_project_employee_*.py` (25 passed)

## Phase 5 — Self-review
- [x] Token audit on PE pages (semantic token classes; critical slate hardcodes reduced)
- [x] Manual screenshot checklist documented (automated skipped — D14)
- [x] Architecture docs synced + README `_Last synced_` bumped
- [x] `admin-dashboard` production build succeeds

## How to capture screenshots manually
1. Open `https://192.168.1.87:2020/admin` → login as HR/Sales_Head.
2. Navigate **Project Employees** → capture list (filters + chips).
3. Open a PE detail → capture General / Leave / Holidays / Timesheet / Commercial tabs.
4. Save under `docs/project-employee/outputs/` as `pe-list.png`, `pe-detail-*.png`.

## Leave credit cron
```bash
# Monthly on the 1st (example)
cd F:/AI-Interview-Model-B-V2/backend
python scripts/run_pe_leave_credit.py --as-of $(date +%Y-%m-%d)
```
