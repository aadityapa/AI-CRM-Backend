# Timesheet Logic — Test & Verification Report

**Method:** Static trace of the actual backend code (no live DB access in this environment). Each
scenario was walked through the real functions in `backend/services/timesheets.py`,
`backend/services/project_employees.py`, and `backend/routers/crm/timesheets.py`.

**Overall verdict:** The core timesheet logic is now **largely correct** — leave-with-balance,
loss-of-pay, weekend/holiday comp-off (bill vs credit), per-leave-type billable, and idempotency all
behave as intended in code. **One real design bug** was found (the "Week Off Billable" flag is now
dead), plus a few things to confirm.

---

## Key functions verified
- `effective_billing_policy()` — resolves project → branch → customer → defaults; now includes
  `comp_off_billable`. Branch resolved via `project.branch_id` first, then `opportunity.branch_id`.
- `compute_billables()` — per-entry billable hours/days; per-type leave billable + LOP split + comp-off
  gating.
- `comp_off_earned()` / `comp_off_billed()` — mutually exclusive by `comp_off_billable`.
- `accrue_comp_off()` — credits comp-off leave on approval (delta vs `ts.comp_off_accrued`, idempotent).
- `classify_timesheet_leave_paid_vs_lop()` — splits each leave day into PAID vs Loss-of-Pay per type,
  date order.
- `consume_timesheet_leaves()` — debits paid leave + records LOP on approval.
- `recompute_entry_live()` / `persist_recomputed_entries()` — recompute from CURRENT policy on read.
- `timesheet_leave_balances_map()` — balances for the Apply-leave picker (includes Comp-Off).

---

## Scenario results

| # | Scenario | Expected | Code result | Status |
|---|----------|----------|-------------|--------|
| 1 | Apply leave, employee HAS balance | Paid; billable if the type is billable per branch Leave Billing Policy; balance consumed on approval | `classify` PAID=min(req,avail); `compute_billables` bills full hours+days; `consume` debits | ✅ PASS |
| 2 | Apply leave, NO balance | Loss of Pay: unpaid, non-billable, not billed to client | `classify` excess→LOP; `compute_billables` paid=0 → 0 billable; LOP recorded | ✅ PASS |
| 3 | Partial balance (has 1, applies 3) | 1 paid + 2 LOP | PAID=min(req,avail) per date order; remainder→LOP | ✅ PASS |
| 4 | Half-day leave | 0.5 handled | `_entry_leave_days` = 0.5; split works | ✅ PASS |
| 5 | Explicit "Loss of Pay" leave type | Always unpaid / non-billable | `is_loss_of_pay_name` → 0 billable, all LOP | ✅ PASS |
| 6 | Leave type billable vs not (per branch policy) | Per-type `is_billable` wins; else flat `leave_billable` | `leave_billable_by_type` map consulted first | ✅ PASS |
| 7 | Works weekend, **Comp Off Billable ON** | Billed (invoice ↑), NO comp-off leave | `compute_billables` bills; `comp_off_earned` → 0; `accrue_comp_off` skips | ✅ PASS |
| 8 | Works weekend, **Comp Off Billable OFF** | Not billed; comp-off leave credited on approval; usable later | `compute_billables` → 0; `comp_off_earned` credits; `accrue_comp_off` credits PE/employee | ✅ PASS |
| 9 | Works on a holiday | Same Comp-Off-billable gate as weekend | Holiday-worked branch uses `comp_off_billable` | ✅ PASS |
| 10 | Pure holiday off (0 h) | Billed 1 day iff `holidays_billable` | Returns `(full_day_hours, 1)` when ON, else 0 | ✅ PASS |
| 11 | Apply Comp-Off as leave | Comp-Off appears in picker; consumes comp-off balance (may go negative) | `timesheet_leave_balances_map` includes comp-off; `classify` comp-off = full, allow negative | ✅ PASS |
| 12 | Present day, 8 h | Billable 1 day / 8 h | PRESENT → capped hours + `_days_from_hours` | ✅ PASS |
| 13 | Hours 0 on a working day | Attendance Absent; 0 billable | Frontend sets Absent; billable 0 | ✅ PASS |
| 14 | Policy changed after entries saved | Summary/invoice reflect CURRENT policy without re-save | `recompute_entry_live` on read | ✅ PASS |
| 15 | Billable leave on an HOURLY invoice | Pays a full day (8 h), not days-only | Billable leave returns full/half **hours AND days** | ✅ PASS |
| 16 | Reject → resubmit → re-approve | No double comp-off credit / no double LOP | Delta vs `ts.comp_off_accrued`; consume delta vs prior `timesheet:{id}` ledger | ✅ PASS |

---

## Issues found

### 🔴 ISSUE-1 (Medium) — "Week Off Billable" flag is now DEAD in billing
`compute_billables()` bills weekend-**worked** hours based on **`comp_off_billable`**, and never reads
`policy.week_off_billable`. The `week_off_billable` value is still resolved and exposed in
`billing_policy` (line ~986) and still has a **checkbox in the Edit Project / Branch UI**, but toggling
it has **no effect** on billing.

**Impact:** A user who enables "Week Off Billable" expecting weekend work to be billed gets nothing
unless they also enable "Comp Off Billable." Two overlapping flags, one silently inert → confusing.

**Fix options (pick the intended semantics):**
- **(A) Consolidate:** remove/hide the "Week Off Billable" toggle and rely solely on "Comp Off Billable"
  (bill vs credit). Simplest; matches current logic.
- **(B) Honor both:** in the `if not is_working:` branch, bill when
  `hours > 0 and (policy.comp_off_billable or policy.week_off_billable)`, and define precedence
  (e.g. week_off_billable = "bill the weekend as normal worked time"; comp_off_billable = "bill as
  comp-off"). Then `comp_off_earned` must exclude days already billed by `week_off_billable` too.
- Recommendation: **(A)** unless the business truly needs a separate "bill weekend directly" path.

### 🟡 ISSUE-2 (Confirm, by design) — Consumption/LOP/comp-off happen only on APPROVAL
`consume_timesheet_leaves`, `classify_...`, and `accrue_comp_off` run in `approve_timesheet` only. A
Draft/Submitted sheet shows **projected** balances (frontend) but nothing is committed until approval.
If the business expects balances to move at **Submit**, call the same functions in the submit handler
(idempotent). Otherwise document that balances update on approval.

### 🟡 ISSUE-3 (Confirm) — Comp-Off leave can overdraw (go negative)
Applying Comp-Off as leave is intentionally exempt from the insufficient-balance block (like leave
applications), so comp-off balance can go negative. Confirm this overdraft is desired; if not, cap it
(then excess would become LOP).

### 🟡 ISSUE-4 (Known, separate) — PE leave balances are a snapshot at map time
Leave types added to the branch's Billable Leave Policy **after** an employee is mapped don't appear on
that employee until a re-sync runs (`seed_leave_details_from_customer_policy` skips existing types and
isn't auto-rerun). This affects which leave types the employee can even apply. Add/keep the
"Sync from customer policy" action.

### 🟢 ISSUE-5 (Low / edge) — Cross-timesheet concurrent consumption
`classify` computes "available before this sheet" by adding back this sheet's own prior ledger rows —
correct for re-approval. But two **different** pending timesheets for the same employee/leave-type,
approved almost simultaneously, could each see the same available balance and over-consume. Sequential
approval is fine; only a concern under true concurrency. Optional: lock the balance row on approval.

---

## What to do next (priority order)
1. **ISSUE-1** — decide "Week Off Billable" semantics and either remove the dead toggle or make billing
   honor it. (Highest — it's a live, misleading control.)
2. **ISSUE-2** — confirm approval-only timing is intended; if not, also consume/classify on Submit.
3. **ISSUE-4** — ensure the "sync leave policies to existing PEs" action exists so employees can apply
   newly-added leave types.
4. **ISSUE-3 / ISSUE-5** — confirm comp-off overdraft policy; add row-locking only if concurrency is real.

## Recommended automated coverage (so this stays green)
Add `backend/tests/test_timesheet_logic.py` covering scenarios 1–16 above as unit tests against
`compute_billables`, `comp_off_earned`/`comp_off_billed`, `classify_timesheet_leave_paid_vs_lop`,
`accrue_comp_off`, and `consume_timesheet_leaves` (with a seeded in-memory DB), plus an assertion that
`week_off_billable` has a defined, tested effect (or is removed).
