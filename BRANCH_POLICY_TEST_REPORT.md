# Branch-wise Leave & Holiday Policy — Full-Stack Test Report
### Black-box + White-box · run against the real code with dummy data

**Result: 77 automated tests PASS** (0 failures) — run in the sandbox against your
actual `services/branch_policy.py`, `routers/crm/customers.py`, and the SQLAlchemy models.

| Layer | Kind | Tests | Result |
|---|---|---|---|
| Pure billing engine (resolver, day-classify, month runner) | **White-box** unit | 8 (`test_branch_policy_uc.py`) | ✅ |
| QA Suites A–H (day-types, boundaries, month/invoice, caps, inheritance, edge, persistence) | **White-box** unit + integration | 20 (`test_branch_qa_phase1/2/3.py`) | ✅ |
| REST API over HTTP (router → DI → service → DB → JSON) | **Black-box** API | 5 (`test_branch_blackbox_api.py`) | ✅ |
| Existing Project-Employee + opportunity regression | mixed | 44 | ✅ |
| **Total** | | **77** | ✅ |

---

## Dummy data used

- **Branch A — HARMAN - Bangalore (active):** Holidays=No, Weekoff=No, Leave=No, CompOff=Yes;
  thresholds Half=4/Full=8, CompOff Half=4/Full=7, Working=8; cycle 1–31; per-day cap ON=8,
  month caps OFF; holidays 2025=9, 2026=8; Casual-Leave policy 1.5/mo, Start_of_Month.
- **Branch B — Adani Motor (inactive):** blank policy fields; 2025 holiday-year row with
  **blank** count; one Monthly / Start_of_Month leave row with **blank** balances.
- **Employee — Ravi Kumar @ HARMAN**, 8h/day, placeholder rate Rs.500/hr, full November 2025 timesheet.

---

## WHITE-BOX — what the internals were tested against

**Resolver (`resolve_branch_project_policy`) — the single shared resolver**
- project override wins; unset fields inherit the branch; built-in default otherwise.

**Two-axis day engine (`classify_day` / `day_paid`) — PAID vs BILLABLE never share a switch**
- Suite A (A1–A9): 8h→8|Yes|0 · 10h→8(cap)|Yes|0 · CL→0|Yes|0 · no-bal→0|LOP|0 ·
  holiday-off→0|Yes|0 · weekoff-off→0|Yes|0 · holiday-worked 7h→7|Yes|+1.0 ·
  holiday-worked 5h→5|Yes|+0.5 · comp-off taken→0|Yes|−1.0.
- Suite C comp-off boundaries: 3h59→0, 4h→0.5, 6h59→0.5, 7h→1.0, 9h→1.0.

**Month runner (`run_month`) — Suite B exact numbers**
- total billable **48h** · invoice **Rs.24,000** · comp-off closing **0.5** ·
  CL used **1.5**, closing **0** · **1 LOP** day (Nov6) · non-billable-present **Nov5,6,9,10,12**.

**Caps (Suite E)** — enforced ONLY when the `Is-*` toggle is ON
- per-day ON=8: 12h→8; per-day OFF: 12h→12; month OFF→48; month ON=40→40 / Rs.20,000;
  initial-no-billing excludes Nov3–5 → 32.

**Inheritance/override (Suite F)** — one resolver everywhere
- inherit→48; project Leave-Billable→Nov5 bills 8 (branch 0); project Holidays-Billable→Nov10
  bills 8 (branch 0); project per-day cap 10→Nov4 bills 10 (branch 8).

**Edge + persistence (Suites G, H)**
- empty leave table → `[]`; Adani blank holiday count → `None` (not 0); blank balances → LOP;
  frozen year → guard raises a clear 400; 0 holidays → normal billing;
  full round-trip with zero field drift + downstream recompute (Full-Day 8→9 → `day_fraction(8h)` 1.0→0.5).

---

## BLACK-BOX — what the API was tested through (HTTP only, no internals)

Driven with FastAPI **TestClient** — real requests, assertions only on JSON responses. The only
test seams are auth + DB (dependency overrides); the router → service → DB path is production code.

| # | Request (HTTP) | Asserted response |
|---|---|---|
| 1 | `POST /api/customers/branches/{id}/holiday-years` ×2 (2025, 2026) | 200; rows created |
| 2 | `GET /api/customers/branches/{id}/policy` | identity (GSTIN/PAN), flags (CompOff=true, Leave=false), cap ON, **holiday_years counts {2025:9, 2026:8}**, empty `leave_policies=[]`, `linked_projects` list |
| 3 | `PATCH .../holiday-years/{year_id}` freeze→true then false | 200; `is_freeze` flips in the list response |
| 4 | Adani: `POST` year 2025 (0 holidays) + `GET /policy` | `holiday_count = null` (blank, not 0); blank `holidays_billable=null`; one leave row `Start_of_Month` with `leave_expire=null`, `maximum_carry_forward=null` |
| 5 | `GET`/`POST` on branch `99999` | **404** for unknown branch |
| 6 | user with no CRM role → `GET /policy` | **403** (auth gate enforced) |

---

## Not covered (and why)

- **Browser/UI end-to-end (Selenium/Playwright):** the frontend build can't run in this
  sandbox (Windows-native `node_modules`), so the `BranchPolicy.tsx` page is verified
  structurally (brace-balanced, typed against existing components) but not click-tested in a
  real browser. The black-box API tests cover the exact endpoints that page calls.
- **Live instance run:** your backend doesn't hot-reload, so the new endpoints need one
  server restart before they answer on `https://192.168.1.87:2020`. The tests above run the
  same code against an in-memory DB.

---

## How to run it yourself

```
cd backend
pip install python-multipart httpx   # black-box TestClient deps (once)
python -m pytest tests/test_branch_qa_phase1.py tests/test_branch_qa_phase2.py \
  tests/test_branch_qa_phase3.py tests/test_branch_policy_uc.py \
  tests/test_branch_blackbox_api.py -v
```

---

## END-TO-END (added) — one continuous journey through the real API stack

`tests/test_branch_e2e.py` mounts **three real routers** (customers, holidays,
customer-leave-policies) and walks the whole lifecycle as one scenario, ending in the invoice:

1. `POST /branches/{id}/holiday-years` ×2 (2025, 2026) — over HTTP
2. `POST /api/holidays` ×17 (9 in 2025, 8 in 2026) — over HTTP
3. `POST /api/customer-leave-policies` (Casual, Monthly, Start_of_Month, 1.5) — over HTTP
4. `GET /branches/{id}/policy` — read the whole config back: holiday counts **{2025:9, 2026:8}**,
   flags, the leave-policy row
5. resolve the persisted branch + compute Ravi's November → **48h / Rs.24,000 / comp-off 0.5 /
   CL closing 0 / 1 LOP** (the exact Suite-B outcome, now reached end-to-end)
6. `PATCH …/holiday-years/{id}` freeze 2025 → guarded read-only

Plus an **inheritance E2E**: same branch, a project override (`Leave Billable=Yes`) run through the
single resolver flips the invoice **48 → 68 billable** (+20). This also proves the two axes are
independent — Nov6 is an unpaid **LOP** day yet still **bills** when leave-billing is on.

## Updated totals
**79 automated tests PASS** — E2E (2) + black-box API (5) + white-box QA A–H (20) +
Project-Employee & opportunity regression (52). 0 failures.
