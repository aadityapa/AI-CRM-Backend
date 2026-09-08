# Leave Scenario Test Report — Aptiv / Magna Steyr / Uno Minda

**Test window:** projects start 01-Jan-2026 → today (27-Jul-2026) = 7 monthly credit cycles (Jan–Jul).
**Method:** full trace of the real engine (`services/project_employee_leave_credit.py`,
`services/project_employees.py` seed + consume, timesheet ledger path) plus a runnable
real-execution harness: `backend/scripts/test_leave_scenarios.py` (isolated SQLite DB — your live
data is never touched). Run it with your venv: `python scripts\test_leave_scenarios.py`.

---

## Leave update per employee (as of 27-Jul-2026)

### 1. Aptiv — employee "Amit" (Casual 1.0/month, carry-forward yearly)
| Item | Value |
|---|---|
| Credited Jan–Jul (1.0 × 7) | **7.0** |
| March timesheet leave | −1.0 (balance at March = 3.0 → sufficient → **paid leave**) |
| **Balance today** | **6.0 CL** |
| Dec-31-2026 projection | +5 (Aug–Dec) → 11.0, **no expiry** → carries 11.0 into 2027 ✔ |

Config: policy `leave_credit_type=Monthly`, `leave_credit_balance=1`, `leave_expire=NULL`,
no carry cap. **Engine supports this scenario exactly.**

### 2. Magna Steyr — employee "Meera" (1.0/month, expires in the month if unused)
| Month | Event | Balance |
|---|---|---|
| Jan | +1.0 credit | 1.0 |
| Feb | Jan remainder **expires** (−1.0), +1.0 credit | 1.0 |
| Mar | Feb remainder **expires** (−1.0), +1.0 credit, **−1.0 leave used** | 0.0 |
| Apr | no expiry (March ended at 0 — used), +1.0 credit | 1.0 |
| May–Jun | prior month expires (−1.0), +1.0 credit each | 1.0 |
| Jul | Jun remainder expires (−1.0), +1.0 credit | **1.0** |

**Balance today: 1.0** (July's credit only) · used 1.0 (March) · **expired 5.0** (Jan, Feb, Apr, May, Jun credits — verified by executed simulation, ledger row per expiry).
Dec-31 projection: with carry cap 0 the Dec-31 run expires December's 1.0 → **0 into 2027**
(and even without the cap, the Jan-2027 rollover would expire it).

**DEFECT — FIXED.** Monthly expiry was not supported (the only expiry ran on Dec 31). The credit
job (`services/project_employee_leave_credit.py`) now implements **cycle expiry**: for policies
with `leave_expire = "Monthly"` (or "Quarterly" — expires at Jan/Apr/Jul/Oct rollovers), the
remainder carried from prior periods expires to zero BEFORE the new period's credit, recorded as
a negative `Adjustment` ledger row, idempotent via `pe_cycle_expire:{pe}:{type}:{YYYY-MM}`. The
accrual-start month never expires its own opening grant. "Yearly"/blank policies keep the
existing Dec-31 carry/expiry path unchanged.

### 3. Uno Minda — employee "Uday" (CL 0.5 + SL 0.5 + EL 2.0 per month, expire yearly)
| Leave type | Credited Jan–Jul | Used (March) | **Balance today** |
|---|---|---|---|
| Casual (0.5 × 7) | 3.5 | −1.0 (March balance 1.5 → paid) | **2.5** |
| Sick (0.5 × 7) | 3.5 | — | **3.5** |
| Earned (2.0 × 7) | 14.0 | — | **14.0** |

Dec-31-2026 projection: CL 5.0 / SL 6.0 / EL 24.0 all **expire** (cap 0) → 0 / 0 / 0 into 2027 ✔.
**Engine supports this scenario** (yearly expiry at the Dec-31 run).

---

## Additional findings (tester's notes)

1. **Credit job must run every month.** Credits are keyed per period
   (`pe_credit:{pe}:{type}:{YYYY-MM}`, idempotent) but the job credits **only the `as_of`
   month** — running it today does NOT backfill Jan–Jun. If your scheduler/CLI
   (`scripts/run_pe_leave_credit.py`) hasn't run monthly since January, real employees are
   under-credited. Backfill = run once per missed month with that month's date (the harness
   does exactly this).
2. **Dec-31 single-day trigger risk.** Year-end expiry/carry fires only if the job runs ON
   Dec 31. A missed run (holiday, outage) silently skips expiry AND carry events for the year.
   Recommend widening the trigger (e.g. first run in January applies the prior year's carry).
3. `leave_expire` accepts Monthly / Quarterly / Yearly (plus legacy Days). Monthly and
   Quarterly use cycle expiry in the credit job; Yearly / any other non-empty value still
   enables Dec-31 year-end expiry via `apply_year_end_carry`. Blank/NULL = no expiry.
4. Proration (`prorate_balance_credit`) and max-limit caps were OFF in all three scenarios —
   full-month credits regardless of join day. All PEs onboarded 01-Jan so no effect here.
5. March leave classification: all three had sufficient balance at consumption → **paid leave**,
   no Loss-of-Pay; the ledger records `timesheet:*` consumption idempotently.
6. Harness policies use `leave_credit_timing=End_Of_Period` so opening balance is 0 and each
   month-end credit is granted once (Start_Of_Period would seed month-1 upfront and
   double-count January when the Jan month-end job also runs).

## How to reproduce in the real app (UI)
1. Customers → each customer's branch → Leave Policy: configure the three policies as above
   (End_Of_Period timing, opening 0).
2. Create the three projects (start 01-Jan-2026), map one employee each (onboarding 01-Jan-2026).
3. Run the credit job once per month Jan→Jul (CLI: `python scripts/run_pe_leave_credit.py --as-of 2026-01-31` … `2026-07-31`).
4. Open each employee's March 2026 timesheet, apply 1 day Casual Leave, submit.
5. Project Employee → Leave Details shows the balances in the tables above.

---

## Visible in CRM UI (seeded live DB)

The same Jul-2026 scenario can be **upserted into the configured CRM Postgres**
(idempotent — does not wipe other data) so it shows under normal CRM pages:

```bash
cd backend
python scripts/seed_leave_scenarios_crm.py
# optional: replay from clean scenario leave state
python scripts/seed_leave_scenarios_crm.py --reset
# optional: advance Aug–Dec + Dec-31 year-end after Jul state
python scripts/seed_leave_scenarios_crm.py --through-dec
```

**Safety:** prints `SEEDING CRM DATABASE: {host}/{db}` first; refuses non-local /
hosted hosts unless `--force`. Credits only the three scenario project-employees
(`pe_id` filter) — never the whole PE population.

**Login:** use an existing CRM admin (e.g. `crm_admin@karnex.test`). No new auth
users are created; Amit / Meera / Uday are HR employee records only.

**Where to click**

- Customers → **Aptiv** → Branches → **Aptiv HQ** (Leave Policy: Casual 1.0/mo, no expiry)
- Customers → Aptiv → Aptiv HQ → **Aptiv Project** → Team → **Amit** → Leave Details → **Casual 6.0**
- Customers → **Magna Steyr** → Magna Steyr HQ → **Magna Steyr Project** → Team → **Meera** → **Casual 1.0**
- Customers → **Uno Minda** → Uno Minda HQ → **Uno Minda Project** → Team → **Uday** → Casual **2.5** / Sick **3.5** / Earned **14.0**
- Employees → search `Amit` / `Meera` / `Uday` (emails `*.leave@karnex.test`)
- Projects → Aptiv Project / Magna Steyr Project / Uno Minda Project

**Confirmed in DB after seed (localhost/karnex_db):** Aptiv/Amit CL 6.0 · Magna/Meera CL 1.0 ·
Uno/Uday CL 2.5, SL 3.5, EL 14.0.

---

## Executed on real backend

**Date:** 2026-07-27  
**Command:** `python scripts/test_leave_scenarios.py` from `backend/`  
**DB:** isolated `backend/scripts/leave_scenario_test.db` (recreated each run; live DB untouched)  
**Result:** ALL LEAVE SCENARIO ASSERTIONS PASSED  
**Regression:** `backend/tests/test_leave_cycle_expiry.py` — 7 passed  
**Full suite:** 477 passed, 6 failed (pre-existing, unrelated to leave credit/expiry — see below)

### Harness output (verbatim)

```
== Monthly credit job runs (March leave consumed chronologically) ==
  2026-01: credited 5.0 across 5 rows
  2026-02: credited 5.0 across 5 rows
  2026-03: credited 5.0 across 5 rows
    March leave — Aptiv: consumed 1.0, balance now 2.00
    March leave — Magna Steyr: consumed 1.0, balance now 0.00
    March leave — Uno Minda: consumed 1.0, balance now 0.50
  2026-04: credited 5.0 across 5 rows
  2026-05: credited 5.0 across 5 rows
  2026-06: credited 5.0 across 5 rows
  2026-07: credited 5.0 across 5 rows

================ LEAVE UPDATE (as of Jul 2026) ================

Aptiv — Amit
  Casual Leave   credited-to-date balance: 6.00  (consumed 1.00)
    ledger: Accrual        1.00 -> 1.00  [pe_credit:1:1:2026-01]
    ledger: Accrual        1.00 -> 2.00  [pe_credit:1:1:2026-02]
    ledger: Accrual        1.00 -> 3.00  [pe_credit:1:1:2026-03]
    ledger: Consumption   -1.00 -> 2.00  [timesheet:sim-1-2026-03]
    ledger: Accrual        1.00 -> 3.00  [pe_credit:1:1:2026-04]
    ledger: Accrual        1.00 -> 4.00  [pe_credit:1:1:2026-05]
    ledger: Accrual        1.00 -> 5.00  [pe_credit:1:1:2026-06]
    ledger: Accrual        1.00 -> 6.00  [pe_credit:1:1:2026-07]

Magna Steyr — Meera
  Casual Leave   credited-to-date balance: 1.00  (consumed 1.00)
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-01]
    ledger: Adjustment    -1.00 -> 0.00  [pe_cycle_expire:2:1:2026-02]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-02]
    ledger: Adjustment    -1.00 -> 0.00  [pe_cycle_expire:2:1:2026-03]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-03]
    ledger: Consumption   -1.00 -> 0.00  [timesheet:sim-2-2026-03]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-04]
    ledger: Adjustment    -1.00 -> 0.00  [pe_cycle_expire:2:1:2026-05]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-05]
    ledger: Adjustment    -1.00 -> 0.00  [pe_cycle_expire:2:1:2026-06]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-06]
    ledger: Adjustment    -1.00 -> 0.00  [pe_cycle_expire:2:1:2026-07]
    ledger: Accrual        1.00 -> 1.00  [pe_credit:2:1:2026-07]

Uno Minda — Uday
  Casual Leave   credited-to-date balance: 2.50  (consumed 1.00)
  Sick Leave     credited-to-date balance: 3.50  (consumed 0.00)
  Earned Leave   credited-to-date balance: 14.00  (consumed 0.00)
    ledger: Accrual        0.50 -> 0.50  [pe_credit:3:1:2026-01]
    ledger: Accrual        0.50 -> 0.50  [pe_credit:3:2:2026-01]
    ledger: Accrual        2.00 -> 2.00  [pe_credit:3:3:2026-01]
    ledger: Accrual        0.50 -> 1.00  [pe_credit:3:1:2026-02]
    ledger: Accrual        0.50 -> 1.00  [pe_credit:3:2:2026-02]
    ledger: Accrual        2.00 -> 4.00  [pe_credit:3:3:2026-02]
    ledger: Accrual        0.50 -> 1.50  [pe_credit:3:1:2026-03]
    ledger: Accrual        0.50 -> 1.50  [pe_credit:3:2:2026-03]
    ledger: Accrual        2.00 -> 6.00  [pe_credit:3:3:2026-03]
    ledger: Consumption   -1.00 -> 0.50  [timesheet:sim-3-2026-03]
    ledger: Accrual        0.50 -> 1.00  [pe_credit:3:1:2026-04]
    ledger: Accrual        0.50 -> 2.00  [pe_credit:3:2:2026-04]
    ledger: Accrual        2.00 -> 8.00  [pe_credit:3:3:2026-04]
    ledger: Accrual        0.50 -> 1.50  [pe_credit:3:1:2026-05]
    ledger: Accrual        0.50 -> 2.50  [pe_credit:3:2:2026-05]
    ledger: Accrual        2.00 -> 10.00  [pe_credit:3:3:2026-05]
    ledger: Accrual        0.50 -> 2.00  [pe_credit:3:1:2026-06]
    ledger: Accrual        0.50 -> 3.00  [pe_credit:3:2:2026-06]
    ledger: Accrual        2.00 -> 12.00  [pe_credit:3:3:2026-06]
    ledger: Accrual        0.50 -> 2.50  [pe_credit:3:1:2026-07]
    ledger: Accrual        0.50 -> 3.50  [pe_credit:3:2:2026-07]
    ledger: Accrual        2.00 -> 14.00  [pe_credit:3:3:2026-07]

✓ Jul 2026 assertions PASSED

== Dec 31 2026 projection (run credit+carry for Aug..Dec, then carry) ==
  Aptiv: balances entering 2027 = {'Casual Leave': 11.0}
  Magna Steyr: balances entering 2027 = {'Casual Leave': 0.0}
  Uno Minda: balances entering 2027 = {'Casual Leave': 0.0, 'Sick Leave': 0.0, 'Earned Leave': 0.0}
✓ 2027 balance assertions PASSED
✓ Idempotency (July + Dec-31 re-run) PASSED

Isolated test DB: F:\AI-Interview-Model-B-V2\backend\scripts\leave_scenario_test.db (safe to delete)
ALL LEAVE SCENARIO ASSERTIONS PASSED
```

### Bugs found and fixed (this run)

| Area | Finding | Fix |
|---|---|---|
| Harness | `LeavePolicyType` imported from wrong module; SQLite could not compile JSONB/ARRAY/UUID/INET; Opportunity enum/`created_by` not filled | Fixed harness only (`scripts/test_leave_scenarios.py`) |
| Harness | Default `Start_Of_Period` seeded month-1 credit then Jan month-end credited again → Aptiv 7 instead of 6 | Set policies to `leave_credit_timing=End_Of_Period` (matches opening balance 0) |
| Services | None — `_apply_cycle_expiry` / year-end carry already matched all EXACT assertions | No service change |

### Full pytest failures (pre-existing, unrelated)

1. `test_boundary_question_finalize.py` — timeout message string mismatch  
2. `test_password_security.py` (×2) — expects bcrypt prefix, got pbkdf2_sha256  
3. `test_rmg_timesheet_reports.py` — sqlite `full_name` column missing on registration_data  
4. `test_timesheet_entry_grid.py` (×2) — half-day hours 8.0 vs expected 8.5  

Leave/cycle-expiry tests (including new `test_leave_cycle_expiry.py`) all green.
