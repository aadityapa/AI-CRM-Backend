# Project Employees — Design Decisions

Living log of decisions made while completing the Project Employee (PE) module.
Spec of record: user prompt in the work session of 2026-07-14 (no `PROJECT-EMPLOYEE-SPEC.md`
was found in either backend or frontend workspaces — treated that prompt as SoT).

---

## D1 — Spec location

| | |
|---|---|
| **Decision** | Treat the agent prompt as the specification of record. |
| **Reasoning** | Workspace search found no `PROJECT-EMPLOYEE-SPEC.md`. Phase 1 code + prompt requirements are sufficient to complete Phases 2–5. |
| **Date** | 2026-07-14 |

## D2 — Design tokens

| | |
|---|---|
| **Decision** | Use canonical `src/design-system/tokens/tokens.css` (imported by `src/styles/tokens.css`) + CRM UI primitives. Prefer semantic classes (`text-primary`, `border-subtle`, `rounded-control`, `shadow-focus-ring`, `bg-surface-1`). |
| **Reasoning** | Design-system path exists and is the source of truth; legacy `tokens.css` is the compatibility alias layer. Avoid new hex/spacing literals on PE pages. |
| **Date** | 2026-07-14 |

## D3 — TimesheetPeriod storage

| | |
|---|---|
| **Decision** | Do **not** add a mutable `TimesheetPeriod` table. Expose **computed API rollups** from `timesheets` + `timesheet_entries` (+ leave/holiday flags on entries). |
| **Reasoning** | Prompt allows views/API rollups. Duplicate editable aggregates would drift from source timesheets. `timesheet_rollups_for_pe()` already serves this; billable formula is enforced via `compute_billable_days` when regenerating entry billables. |
| **Date** | 2026-07-14 |

## D4 — Active mapping uniqueness

| | |
|---|---|
| **Decision** | Keep full `UNIQUE(project_id, employee_id)` and **reactivate** soft-exited rows on remap. Do **not** switch to a Postgres partial unique on active-only. |
| **Reasoning** | Partial unique would allow many historical rows per pair and complicate leave/rate history. Existing API already returns 409 on active duplicate and reuses the same PE id on remount — preserves leave ledger continuity. |
| **Date** | 2026-07-14 |

## D5 — CustomerLeavePolicy / Karnex internal

| | |
|---|---|
| **Decision** | Karnex internal / bench leave is just another `CustomerLeavePolicy` (or employee balances when no PE). No special-case branch for “company policy”. |
| **Reasoning** | Prompt: no special-casing. PE leave is always seeded from the **project customer’s** active policies (branch preferred). |
| **Date** | 2026-07-14 |

## D6 — Leave seed = copy, not live ref

| | |
|---|---|
| **Decision** | On map, copy balances into `project_employee_leave_details` and store `customer_leave_policy_id` as audit provenance only. Later policy edits do **not** mutate existing PE balances. |
| **Reasoning** | Prompt § Phase 2.1. Accrual job reads seed metadata (`leave_accrual`, policy max_carry / expire when still linked) but never resets opening/initial from a live policy fetch. |
| **Date** | 2026-07-14 |

## D7 — PO usage ledger

| | |
|---|---|
| **Decision** | Extend existing `po_project_allocations.consumed_amount` + `purchase_orders.consumed_value/balance_value` as the usage ledger. Surface `po_status` (`ok` / `warn_80` / `blocked`) on PE list/detail from allocation utilization. |
| **Reasoning** | Invoice create already drawdowns these columns. A second ledger would desync. Period amounts remain visible via invoices filtered by project. |
| **Date** | 2026-07-14 |

## D8 — PO gates: invoice yes, timesheet never

| | |
|---|---|
| **Decision** | Block **invoice** generate/create when PO expired or balance exhausted. **Never** block timesheet create/submit/edit for PO reasons. UI warns at ≥80% utilization. |
| **Reasoning** | Prompt § Phase 2.6. Prior code gated timesheet submit on PO expiry (`check_balance=False`); removed that call so delivery can continue while commercial fix is pending. |
| **Date** | 2026-07-14 |

## D9 — Leave accrual job

| | |
|---|---|
| **Decision** | Implement `services/project_employee_leave_credit.py` + CLI `backend/scripts/run_pe_leave_credit.py` (callable from cron). Document schedule in TASKS.md. |
| **Reasoning** | Avoid new infra dependency; matches existing “script + service” patterns in the repo. |
| **Date** | 2026-07-14 |

## D10 — Mid-period rate / invoice

| | |
|---|---|
| **Decision** | Pure engine in `project_employee_billing.py`; invoice path loads PE rates and splits via `split_period_by_rate` / `invoice_amount_split` when generating from timesheets. |
| **Reasoning** | Unit-testable core already existed; wiring must stay server-side so UI cannot invent totals. |
| **Date** | 2026-07-14 |

## D11 — Comp-off insufficient balance

| | |
|---|---|
| **Decision** | Skip balance gate for Loss of Pay and `comp_off_type == "Earned"`; for Comp-Off **Consumed**, still require PE balance. |
| **Reasoning** | Prompt: “reject if insufficient (except comp-off)” — interpreted as earning/credit path and LOP, not unlimited unpaid leave via consume. |
| **Date** | 2026-07-14 |

## D12 — Exit flow

| | |
|---|---|
| **Decision** | Setting `is_exit=true` (or `is_active=false`) stops future accrual (credit job skips), closes open timesheet periods to Draft→flagged note via `exit_project_employee()`, and sets `settlement_leave_balance` snapshot on leave detail response. |
| **Reasoning** | Prompt § Phase 2.7. Soft exit keeps historical rates/leave intact for audit and remount. |
| **Date** | 2026-07-14 |

## D13 — Tracking file location

| | |
|---|---|
| **Decision** | `docs/project-employee/DESIGN-DECISIONS.md`, `docs/project-employee/TASKS.md`, `docs/project-employee/outputs/`. |
| **Reasoning** | Prefer docs location over polluting root `TASKS.md` (already used by Opportunity→Hire pipeline). |
| **Date** | 2026-07-14 |

## D14 — Automated screenshots

| | |
|---|---|
| **Decision** | Skip Playwright/Puppeteer capture in this environment; document manual checklist under TASKS Phase 5. |
| **Reasoning** | No guaranteed headed browser + admin session against `https://192.168.1.87:2020` in the agent sandbox. |
| **Date** | 2026-07-14 |

## D15 — `project_experience_years`

| | |
|---|---|
| **Decision** | Add nullable `project_experience_years` on `project_employees` (distinct from total `experience_years`). Migration `0024`. |
| **Reasoning** | Spec lists both experience and project_experience on the bridge. |
| **Date** | 2026-07-14 |

## D16 — PE Leave / Holidays detail payload

| | |
|---|---|
| **Decision** | Extend `GET /api/projects/employees/{pe_id}` (same endpoint) with `leave_eligibility[]`, `leave_summary` (balance / consumed / accrued_this_year), `credit_history[]` (PE-scoped `leave_accrual_events`), `leave_applications[]`, and `holiday_calendar` metadata. Write `pe_seed:{pe_id}:{leave_type_id}` accrual events on policy seed. |
| **Reasoning** | Avoid new routes; Leave tab needs eligibility + ledger + apps in one load. Holidays stay read-only from existing `holidays_for_pe`. |
| **Date** | 2026-07-14 |
