# QA Leave System Test Report

**Role:** Senior QA (leave-policy architecture E2E)  
**Executed:** 2026-07-28  
**Environment:** FastAPI + PostgreSQL `localhost:5432 / karnex_db`  
**Alembic:** `0048 (head)` — includes `0047` leave timing + `0048` PE project-policy FK  
**Frontend:** `npm run build` in `frontend/admin-dashboard` — **PASS** (8.16s)  
**Harness:** `backend/scripts/qa_leave_system_test.py` (QA-* / `qa.*@test.local` only)  
**Evidence JSON:** `qa_leave_evidence.json`  
**Overall:** **PASS**

---

## Pre-flight

| Check | Result |
|-------|--------|
| `alembic upgrade head` → `0048 (head)` | PASS |
| Branch policies for Aptiv / Magna Steyr / Uno Minda | PASS (no UI fix required) |
| Customer configs modified | **None** (read-only verify) |

### Policy SQL (as executed)

```sql
SELECT c.name, p.branch_id, t.name AS leave_type, p.leave_credit_type, p.leave_credit_balance,
       p.leave_expire, p.leave_expire_timing, p.maximum_carry_forward, p.is_active
FROM customer_leave_policies p
JOIN customers c ON c.id = p.customer_id
JOIN leave_policy_types t ON t.id = p.leave_type_id
WHERE c.name IN ('Aptiv','Magna Steyr','Uno Minda') AND p.branch_id IS NOT NULL;
```

| Customer | Branch | Leave type | Credit | Expire | Carry | Active |
|----------|--------|------------|--------|--------|-------|--------|
| Aptiv | Aptiv HQ (`branch_id=9`) | Casual Leave | Monthly **1.00** | NULL | NULL | Y |
| Magna Steyr | Magna Steyr HQ (`10`) | Casual Leave | Monthly **1.00** | **Monthly** | **0** | Y |
| Uno Minda | Uno Minda HQ (`11`) | Casual Leave | Monthly **0.50** | **Yearly** | **0** | Y |
| Uno Minda | Uno Minda HQ | Sick Leave | Monthly **0.50** | **Yearly** | **0** | Y |
| Uno Minda | Uno Minda HQ | Earned Leave | Monthly **2.00** | **Yearly** | **0** | Y |

Note: `leave_credit_timing` = `End_Of_Period` on all five (compatible with engine; timing metadata does not alter batch rollover math).

---

## Per-phase PASS/FAIL

| Phase | Item | Result |
|-------|------|--------|
| 0 | Policy config matches expected | **PASS** |
| 1 | QA projects + employees + seed FKs | **PASS** |
| 2 | Jan→Jul credits + March Casual consume | **PASS** |
| 2 | July double-run idempotency | **PASS** (43→43 events) |
| 2 | Jul balances (Aptiv 6 / Magna 1 / Uno 2.5·3.5·14) | **PASS** |
| 2 | Magna: exactly 5 cycle-expiries totaling 5.0; **no `:2026-04`** | **PASS** |
| 2b | Backdated March leave draws **current** pool | **PASS** (documented) |
| 3 | Project override → Omar `project_leave_policy_id`; Amit unchanged | **PASS** |
| 3 | Omar Jul Casual = **2.0** + monthly cycle expiry | **PASS** |
| 3 | Deactivate override → Fallback seeds branch FK | **PASS** |
| 4 | UI surfaces (source review + production build) | **PASS** |
| 4 | Dec-31 projection Aptiv 11 / Magna 0 / Uno 0/0/0 | **PASS** |
| Auto | `pytest test_project_leave_override + test_leave_cycle_expiry` | **13 passed** |

---

## Phase 1 — Setup (evidence)

Created (prefixed **QA-** / **qa.\*@test.local**):

| Customer | Project | `branch_id` | Employee | PE id |
|----------|---------|-------------|----------|-------|
| Aptiv | QA-Aptiv-Test | 9 | qa.amit@test.local | 16 |
| Magna Steyr | QA-Magna-Steyr-Test | 10 | qa.meera@test.local | 17 |
| Uno Minda | QA-Uno-Minda-Test | 11 | qa.uday@test.local | 18 |

Post-seed (before credits): balances **0**, `customer_leave_policy_id` set, `project_leave_policy_id` **NULL**, `policy_source` = **branch**.

---

## Phase 2 — Credit cycle Jan→Jul 2026

### Method
- Credits: `run_pe_leave_credit(db, as_of=month_end, pe_id=…)` for each QA PE only (never all live PEs).
- March leave: `consume_pe_leave` + `LeaveAccrualEvent` `Consumption` with source `timesheet:qa-{pe}-202603` (same ledger path the timesheet UI writes). Full UI click of Timesheets grid was **not** driven in this run; engine path is identical to production timesheet consumption.

### Jul 2026 balances (asserted)

| Employee | Leave | Balance | Consumed |
|----------|-------|---------|----------|
| Aptiv / Amit | Casual | **6.00** | 1.00 |
| Magna / Meera | Casual | **1.00** | 1.00 |
| Uno / Uday | Casual | **2.50** | 1 |
| Uno / Uday | Sick | **3.50** | 0 |
| Uno / Uday | Earned | **14.00** | 0 |

### Magna ledger shape (critical)

```
pe_cycle_expire:17:8:2026-02  amount=-1.00
pe_cycle_expire:17:8:2026-03  amount=-1.00
pe_cycle_expire:17:8:2026-05  amount=-1.00
pe_cycle_expire:17:8:2026-06  amount=-1.00
pe_cycle_expire:17:8:2026-07  amount=-1.00
```

- Count = **5**, abs total = **5.0**
- **No** `…:2026-04` row — March credit was fully consumed; April rollover had nothing to expire
- Source suffix = rollover period (`YYYY-MM` of the month-end job that expires the **prior** remainder)

### Backdated leave (known behavior)

After July, a second “March” Casual consume for Meera reduced Casual **1.00 → 0.00** (drew July’s remaining day). **Does not resurrect expired Jan–Jun credits.** Balance restored after documenting so later Dec-31 assertions stayed clean.

---

## Phase 3 — Project override chain

On **QA-Aptiv-Test** only:

1. Active `ProjectLeavePolicy`: Casual Monthly **2.0**, expire **Monthly**, carry **0**
2. Mapped **qa.omar@test.local** (new seed)

| Person | `policy_source` | `project_leave_policy_id` | `customer_leave_policy_id` | Jul Casual |
|--------|-----------------|---------------------------|----------------------------|------------|
| Omar (new) | **project** | **11** | NULL | **2.00** |
| Amit (existing) | **branch** | NULL | 10 | unchanged chain |

Omar accrued monthly cycle-expiry events under the **project** policy (6 expire rows Jan–Jul path without a March consume).

3. Deactivated override → mapped **qa.fallback@test.local** → Casual seeds with **branch** source, customer FK **10**, project FK **NULL**.

**Invariant confirmed:** adding a project override does **not** rewrite existing PE leave rows.

---

## Phase 4 — UI surfaces (steps 2–4)

Automated browser screenshots were not captured; verification is **source + production build** plus live DB provenance fields the UI binds to.

| Surface | Evidence | Status |
|---------|----------|--------|
| Opportunity Leave & Holiday helper | `wizard/index.tsx` + `WizardChrome.tsx` → *"Estimation only — prefilled from the branch policy for costing…"* | PASS |
| Customer Billing → Leave Billing Policy | `CustomerFormModal.tsx` → chip **"Customer defaults"** + FALLBACK copy | PASS |
| Branch wizard Leave step | `BranchWizardModal.tsx` → **"Inherited customer defaults"** dashed panel + `PeriodTimingPicker` under credit/expire | PASS |
| Project Leave Billing Policy dialog | `LeaveBillingPolicyModal.tsx` → both `PeriodTimingPicker`s | PASS |
| PE Leave tab chips | `ProjectEmployeeDetail.tsx` + shared `PolicySourceChip` (violet for project) | PASS |
| My Leave chips | `MyLeave.tsx` uses same `PolicySourceChip` | PASS (needs employee portal login; QA HR rows have no portal user) |
| Frontend build | `npm run build` OK; chunk `PeriodTimingPicker-*.js` present | PASS |

### Manual click checklist (for operator)

1. Hard-refresh `/admin` after rebuild.
2. **Projects → QA-Aptiv-Test → Team → QA Omar** → Leave: violet **Project override** chip; Casual timing caption.
3. Same project → **QA Amit** → **Branch policy** chip (not rewritten).
4. **Customers → Aptiv → Edit → Billing Policy** → Customer defaults framing.
5. **Branch wizard** (Aptiv HQ) → Leave step → Inherited customer defaults panel + timing checkboxes.
6. **New Opportunity** (any of the 3) → Leave & Holiday Details helper text.

---

## Phase 4 optional — Dec-31 / entering 2027

Ran Aug→Dec month-ends + `as_of=2026-12-31` **for the three original QA PEs only** on the shared local DB (acceptable for this QA run).

| Customer | Entering 2027 |
|----------|---------------|
| Aptiv / Amit | Casual **11.00** (full carry) |
| Magna / Meera | Casual **0** |
| Uno / Uday | Casual **0** / Sick **0** / Earned **0** |

Uno year-end: **one** `pe_expire:…:2026` Adjustment per leave type (3 events).

---

## Automated suites

```text
python -m pytest tests/test_project_leave_override.py tests/test_leave_cycle_expiry.py -q
.............                                                            [100%]
13 passed in 1.40s
```

No engine bugs found; **no service code changes** in this QA pass. Harness added only: `backend/scripts/qa_leave_system_test.py`.

---

## Deviations / notes

1. **March leave** applied via service/ledger equivalent of timesheet consumption (not UI grid click). Behavior matches production `consume_pe_leave` + `LeaveAccrualEvent`.
2. **My Leave** portal chips not click-verified (QA employees are HR records without auth users). PE detail API fields `policy_source` / FKs verified in DB for Omar vs Amit.
3. After `--through-dec`, live QA balances are at **2027** state (Amit 11, Meera 0, Uday 0). Re-run with `--reset` (default) to replay from Jan if needed.
4. Existing seed employees (`amit.leave@karnex.test` etc.) were **not** modified; only `qa.*@test.local` / `QA-*` projects.

---

## Where to see data in CRM UI

Login with existing admin (e.g. `crm_admin@karnex.test`).

- **Projects → QA-Aptiv-Test / QA-Magna-Steyr-Test / QA-Uno-Minda-Test → Team →** Amit / Meera / Uday / Omar  
- **Employees →** search `qa.amit@test.local`, `qa.meera@test.local`, `qa.uday@test.local`, `qa.omar@test.local`  
- Leave balances / chips on PE **Leave** tab

---

## Cleanup

### Script (preferred)

```bash
cd backend
python scripts/qa_leave_system_test.py --cleanup
```

### SQL (manual — QA rows only; does not touch Aptiv/Magna/Uno policies)

```sql
BEGIN;

-- Ledger for QA employees
DELETE FROM leave_accrual_events
WHERE employee_id IN (SELECT id FROM employees WHERE email LIKE 'qa.%@test.local');

-- PE leave details
DELETE FROM project_employee_leave_details
WHERE project_employee_id IN (
  SELECT pe.id FROM project_employees pe
  JOIN employees e ON e.id = pe.employee_id
  WHERE e.email LIKE 'qa.%@test.local'
);

-- Optional: PE rates if present
DELETE FROM project_employee_rates
WHERE project_employee_id IN (
  SELECT pe.id FROM project_employees pe
  JOIN employees e ON e.id = pe.employee_id
  WHERE e.email LIKE 'qa.%@test.local'
);

DELETE FROM project_employees
WHERE employee_id IN (SELECT id FROM employees WHERE email LIKE 'qa.%@test.local');

DELETE FROM project_leave_policies
WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'QA-%');

DELETE FROM projects WHERE name LIKE 'QA-%';

DELETE FROM opportunities WHERE opp_id LIKE 'OPP-QA-%';

DELETE FROM employees WHERE email LIKE 'qa.%@test.local';

COMMIT;
```

**Restore customer config:** N/A — no customer/branch policies were changed.

---

## Bugs found / fixed

| Bug | Action |
|-----|--------|
| None in leave engine / architecture | — |
| QA harness Unicode arrows crashed on Windows cp1252 | Fixed in harness print strings |

---

## Re-run commands

```bash
cd backend
python -m alembic upgrade head
python scripts/qa_leave_system_test.py --through-dec --reset
python -m pytest tests/test_project_leave_override.py tests/test_leave_cycle_expiry.py -q

# Frontend (from AI-Interview-Model-F-V2)
cd frontend/admin-dashboard && npm run build
```
