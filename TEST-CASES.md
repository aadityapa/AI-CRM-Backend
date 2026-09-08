# Karnex CRM — Project Employee Module: Test Use Cases

> Automated coverage lives in `backend/tests/test_uc_project_employee.py`.
> Results are recorded in `TEST-RESULTS.md`. DONE = 12/12 PASS.

## Seed Data (created before running any test)

**Employees:** Avinash (EMP-001), Ranjeet (EMP-002)

**Clients & Policies**

| Policy | Client | Credit Type | Rule |
|---|---|---|---|
| POL-KARNEX | Karnex (internal) | Accrual | 1.0 day/month, carry-forward max 10, no expiry |
| POL-SAMSUNG | Samsung | Accrual | 1.5 days/month, carry-forward max 5, expires Dec 31 |
| POL-MSFT | Microsoft | Upfront | 18 days credited on mapping, prorated by join month |

**Holiday Calendars**
- Samsung: Jul 17, Aug 15 (2 holidays in Jul–Aug window)
- Microsoft: Jul 4, Jul 24, Aug 15 (3 holidays)
- Karnex: Aug 15 only

**Projects:** Project X (client: Samsung), Project Y (client: Microsoft)

**POs**
- PO-100: Samsung, value ₹10,00,000, expiry Dec 31, 2026 — funds Project X
- PO-200: Microsoft, value ₹2,00,000, expiry Sep 30, 2026 — funds Project Y

---

## UC-01: Two employees, same project, different policies
Avinash → Project X with POL-SAMSUNG (PE-001); Ranjeet → Project X with POL-KARNEX
(negotiated exception, PE-002). Run July monthly credit.
**Expected:** two separate PE records; PE-001 balance = 1.5, PE-002 balance = 1.0;
changing PE-001 has zero effect on PE-002.

## UC-02: Policy seeds on mapping (copy, not live link)
Map Avinash → Project X; admin edits POL-SAMSUNG accrual 1.5 → 2.0; check PE-001, then
run next month's credit.
**Expected:** PE-001 balance unchanged at edit time; new 2.0 applies only from the next
credit cycle.

## UC-03 (CRITICAL): Multi-project employee — full independence
Avinash → Project X (PE-001, POL-SAMSUNG, Samsung holidays, ₹8,000/day) AND
Project Y (PE-003, POL-MSFT, Microsoft holidays, ₹10,000/day). Run July credit; file &
approve 2-day leave (Jul 20–21) on Project X; generate July timesheets (23 working days).
**Expected:** PE-003 balance 18 untouched; leave form requires project selection before
showing balance; PE-001 July = 23 − 2 − 1 (Jul 17) = **20 billable**; PE-003 July =
23 − 0 − 2 (Jul 4, Jul 24) = **21 billable**; invoice PE-001 = 20 × 8,000 = ₹1,60,000;
PE-003 = 21 × 10,000 = ₹2,10,000.

## UC-04: Accrual vs upfront vs proration
New employee → Samsung on Jul 20 (prorate); another → Microsoft on Jul 20 (upfront,
prorated by remaining months).
**Expected:** Samsung July credit ≈ 1.5 × (12/31) ≈ 0.58; Microsoft = 18 × (6/12) = 9.

## UC-05: Holiday auto-linkage
Generate August timesheets for PE-001 (Samsung) and PE-003 (Microsoft).
**Expected:** PE-001 Aug holidays = 1 (Aug 15); PE-003 Aug holidays = 1 (Aug 15); Jul
dates excluded; no manual holiday field editable on the timesheet.

## UC-06: Mid-period rate change (split invoice)
PE-001 ₹8,000 eff Jul 1; add ₹9,000 eff Jul 16; July billable 20 (12 before Jul 16, 8 on/after).
**Expected:** old row is_current_rate → false, one current row; invoice =
(12 × 8,000) + (8 × 9,000) = ₹1,68,000.

## UC-07: PO drawdown across multiple projects + limits
Link PO-100 to Project X and a second Samsung project; invoice until 80% then 100%; attempt
one more invoice, then a timesheet entry.
**Expected:** per-invoice ProjectPOUsage; PO remaining decrements across both projects;
≥80% warning chip; 100%/expiry blocks invoice with clear error; timesheet entry still allowed.

## UC-08: Duplicate mapping guard
Map Avinash → Project X again while PE-001 active.
**Expected:** rejected (validation error); after PE-001 exit, remapping allowed (new record).

## UC-09: Exit flow
Set is_exit on PE-001, exit date Jul 31; run August credit.
**Expected:** no August accrual on PE-001; open periods closed at exit; remaining balance
flagged for settlement; PE-003 unaffected.

## UC-10: Insufficient balance
File 5-day leave on PE-002 (balance 1.0), not comp-off.
**Expected:** rejected at submission with balance shown; comp-off type bypasses the check.

## UC-11: Carry-forward cap & expiry (year rollover)
PE-001 balance = 8 on Dec 31 (POL-SAMSUNG: max carry 5, expires Dec 31); run rollover.
**Expected:** Jan 1 opening = 5; 3 days lapsed and logged in credit history.

## UC-12: Group-by-employee list view
Avinash on 2 projects; toggle Group by Employee.
**Expected:** flat view shows Avinash twice; grouped view collapses to one row expandable
to both mappings, each with its own balance/rate/PO chip.
