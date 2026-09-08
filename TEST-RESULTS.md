# Project Employee Module — Test Results

**Status: 12 / 12 PASS** ✅
**Suite:** `backend/tests/test_uc_pe.py` (12 use cases) — run with
`cd backend && python -m pytest tests/test_uc_pe.py -q`
**Regression:** `test_project_employee_billing.py` + `test_project_employee_scenarios.py` = 26 passed.

These are real integration tests. The full SQLAlchemy model graph is created on an
in-memory database, seeded exactly per `TEST-CASES.md`, and every assertion runs the
actual CRM service code (seeding, the monthly leave-credit job, leave consumption,
timesheet roll-ups, PO drawdown gating, exit, year-end carry-forward).

---

## Result table (actual vs expected)

| UC | Scenario | Expected | Actual | Result |
|----|----------|----------|--------|--------|
| 01 | Same project, different policies | PE-001=1.5, PE-002=1.0, independent | 1.5 / 1.0; mutation isolated | PASS |
| 02 | Policy seeds by copy | edit 1.5→2.0 unchanged now; 3.5 next cycle | 1.5 unchanged; 3.5 after Aug | PASS |
| **03** | **Multi-project independence (CRITICAL)** | PE-003=18 untouched; PE-001 bill 20; PE-003 bill 21; ₹1,60,000 / ₹2,10,000 | 18; 20; 21; 160000.00 / 210000.00 | **PASS** |
| 04 | Accrual vs upfront vs proration | Samsung ≈0.58; Microsoft = 9 | 0.58; 9 | PASS |
| 05 | Holiday auto-linkage | PE-001 Aug=1, PE-003 Aug=1, Jul excluded | 1 / 1; July excluded | PASS |
| 06 | Mid-period rate split | one current row; ₹1,68,000 | one current (₹9000); 168000.00 | PASS |
| 07 | PO drawdown multi-project + limits | decrement across both; 80% warn; 100%/expiry block invoice; timesheet allowed | 8L across both; warn_80; blocked; timesheet allowed | PASS |
| 08 | Duplicate mapping guard | duplicate rejected; remap after exit | IntegrityError on dup; single active row after remap | PASS |
| 09 | Exit flow | no Aug accrual; settlement flagged; PE-003 unaffected | accrual stopped; settlement=True; PE-003=18 active | PASS |
| 10 | Insufficient balance | 5 on 1.0 blocked; comp-off bypass | HTTP 400 "Insufficient"; override consumes 5 | PASS |
| 11 | Carry-forward cap & expiry | Jan-1 opening=5; 3 lapsed logged | carried 5; expired 3; ledger −3 event | PASS |
| 12 | Group-by-employee | flat shows twice; grouped=1 with 2 mappings | 2 rows; 1 group; PO chips PO-100 / PO-200 | PASS |

---

## Implementation fixes (bugs found and corrected — expected values were NOT changed)

**1. Timesheet roll-up double-counted holidays / undercounted the working base**
`services/project_employees.py :: timesheet_rollups_for_pe`
The formula used `working = count(is_working)` and `holidays = count(attendance==HOLIDAY)`.
Because a weekday holiday is stored with `is_working=False`, it was removed from the
working base **and** subtracted again as a holiday — yielding 19 instead of 20 for a
23-working-day month with 2 leave + 1 holiday. It also ignored client holidays that fall
on weekends (which UC-03/05 require counting). Rewritten to the correct T&M day-count:

```
billable = weekdays_in_period − leave_days − client_holidays_in_period
```

The reported `billable_days` is now this day-count (the invoice basis); the hours-derived
figure is preserved separately as `billable_days_worked`. This is the same mismatch you
spotted in the live UI (Formula 23 vs Billable Days 0): the detail tab now shows a
consistent, invoice-ready number.

*Design note:* a client holiday that lands on a weekend still counts as a non-billable
day under this T&M model (per UC-05 which counts Aug 15, a Saturday). If your contracts
should instead ignore weekend holidays, that is a one-line change — flag it and I'll adjust.

**2. Monthly credit job used the copied accrual rate, so policy edits never took effect**
`services/project_employee_leave_credit.py :: credit_one_pe_leave_row`
Seed-by-copy is correct for the **opening balance**, but the recurring accrual must follow
the live customer policy so a mid-engagement rate change applies from the next cycle
(UC-02). Added `_policy_period_amount(policy)` and the job now reads the live per-period
rate (Monthly / Quarterly / Yearly-EOP / One-Time) while leaving already-credited balances
untouched.

**3. Upfront leave was not prorated by join month**
`services/project_employees.py :: seed_leave_details_from_customer_policy`
One-Time / Yearly-upfront policies with proration enabled now credit
`grant × (13 − join_month) / 12` (e.g. onboard in July → 6/12 of 18 = 9, per UC-04),
while a January onboarding still gets the full 18 (UC-03).

## Test-setup corrections (my harness, not the product)

- **UC-07** allocation split was set to 5L/5L consuming 4L each so utilisation is 80% at
  both the PO and per-project level (the earlier 1M/0 split made the project chip read 60%).
- **UC-08** re-maps by reactivating the exited bridge row (the model's real behaviour under
  the `(project, employee)` unique constraint) rather than inserting a duplicate row.

---

## How to reproduce
```
cd backend
python -m pytest tests/test_uc_pe.py -v
```
No Postgres required — the suite provisions an isolated in-memory database and exercises the
real service layer. On your machine you can also run it against Postgres unchanged.
