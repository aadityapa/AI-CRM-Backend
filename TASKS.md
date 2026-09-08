# KARNEX Opportunity → Hire Pipeline — TASKS

Design: `backend/docs/pipeline_design.md`. Tick only what's verified by running it.

## Scope decisions (confirmed)
- Scoring engine: **deterministic keyword scorer** (kept; 100% bug already fixed). No OpenAI in scoring.
- Async infra: **APScheduler + DB-backed job table** (no Redis/Celery).
- Job portals: **adapter interface + mock**; LinkedIn/Naukri/Indeed as documented stubs (no live creds).
- Outreach: **email-first (real SMTP), WhatsApp mocked**; templates drafted for Meta submission.
- Human-review mode: **ON by default**. Mock senders **ON**. No live portal/message without sign-off.

## Phase 0 — Audit & design
- [x] Full codebase audit of existing pipeline entities (opportunity/requirement/candidate/scoring/scheduling/notify/infra)
- [x] Written design doc: state machines, schema deltas, endpoints+roles, async boundaries (`docs/pipeline_design.md`)

## Phase 3b — Resume intake integrity (no external dep) — DONE this increment
- [x] `Resume.file_sha256` + `file_size` columns (`models/resumes.py`)
- [x] Alembic `0011_resume_file_checksum` (columns + indexes incl. `(requirement_id, file_sha256)`)
- [x] `save_upload_hashed()` + `verify_crm_file_checksum()` (`services/crm_common.py`)
- [x] Dedupe by file bytes on TA upload (`routers/crm/resumes.py`) and public apply (`routers/crm/apply.py`)
- [x] Checksum/dedupe/verify algorithm proven (standalone asserts pass; repo test `tests/test_resume_checksum.py`)
- [ ] Run `tests/test_resume_checksum.py` in the real env (blocked in this sandbox by a file-mount truncation, not a code issue)
- [ ] Wire `verify_crm_file_checksum` into the file-download path
- [ ] OCR / image-only PDF handling already rejects empty text (from ATS gate) — confirm messaging

## Phase 1 — Opportunity approval chain (gaps)
- [ ] Add `CHANGES_REQUESTED` state + `/request-changes` endpoint (reason required)
- [ ] Add explicit RMG gate on the Opportunity (currently only on the spawned Requirement) — reconcile without breaking spawn
- [ ] Generic append-only `audit_log` table + central `transition()` fn (state machine module)
- [ ] Delegation + SLA escalation for approvers
- [ ] Test: Sales calling approve endpoint → 403

## Phase 2 — TA JobPost + portal publishing
- [ ] Versioned `JobPost` entity (draft→preview→publish, re-sync on edit)
- [ ] `PortalAdapter` interface (publish/update/unpublish/fetchApplicants) + Mock adapter
- [ ] LinkedIn/Naukri/Indeed stubs + required-creds doc; idempotent, backoff, partial-success

## Phase 3c/3d — Scoring polish + threshold
- [ ] Surface sub-scores + evidence in the UI (data already emitted by `ats_scoring.py`)
- [ ] Per-job-post configurable threshold (default 50)
- [ ] Human-review queue for above-threshold candidates (default ON); ARCHIVED for below

## Phase 4 — Outreach (email + WhatsApp)
- [ ] `NotificationAdapter` interface; real SMTP email, WhatsApp mock
- [ ] Consent + opt-out; signed single-use expiring booking token (SHA-256 hash stored, constant-time compare)
- [ ] Reminders (48h/4h), retries+backoff, idempotency key; escalate to TA if all channels fail
- [ ] Draft WhatsApp templates list for Meta approval

## Phase 5 — Slot booking (concurrency)
- [ ] `interview_slots` + hold/booking with row-lock/unique constraint
- [ ] Concurrency test: two simultaneous bookings → exactly one wins
- [ ] Timezone (UTC store, dual display), reschedule/cancel, .ics, no-show sweep

## Phase 6 — L1 interview + report + RMG decision
- [ ] Resumable session; single-use dead-after-use link
- [ ] L1 report with per-competency + evidence; loud failure on report error
- [ ] RMG decision screen (Select/Reject/Request L2) → audit + notify

## Phase 7 — Cross-cutting
- [x] Passwords bcrypt; SHA-256 for file checksums (this + prior session)
- [ ] Per-endpoint authz + IDOR tests across the new entities
- [ ] AES-256-GCM at rest for sensitive candidate PII; retention + delete
- [ ] HTTPS/HSTS (documented; app still plain HTTP)

## Phase 8/9/10 — Tests, UI, build
- [ ] Integration tests (real DB) for every branch in the spec
- [ ] Role dashboards + candidate kanban; four async states; audit trail views
- [ ] typecheck/lint/build green; no secret in client bundle (grep-proven)

---

# Customer Branch-wise Leave & Holiday Policy — Module Tasks

> Started as an autonomous, gated build. One checklist item per deliverable;
> checked off only after its GATE passes. Non-negotiable: exact field names / enums.

## PHASE 0 — Discovery & conventions lock  ✅ (awaiting go-ahead for Phase 1)

### Stack & conventions (detected)
- **Backend:** FastAPI + SQLAlchemy 2.0 + **Alembic** (numbered migrations; latest
  `0027_invoice_line_tax_invoice_fields.py` → next is **0028**). Postgres. Native
  `pg_enum` helper for enums (stores the enum *value* string).
- **Frontend:** React + TS + Vite + Tailwind. Design tokens in
  `src/design-system/tokens/tokens.css` + `tokens.ts`, `src/styles/tokens.css`,
  `tailwind.config.cjs`. Reusable UI in `src/crm/components/ui` (Field, Modal, Tabs,
  KpiCard, StatusBadge, DataTable) + `DataTable`. Utility classes like `bg-surface-1`,
  `border-subtle`, `text-brand-600`, `rounded-card`, `focusRing` (never hardcode colors).
- **Branch UI today:** branches are managed inside `src/crm/pages/Customers.tsx`
  (the standalone "Customer Branches" tab was folded into Customers earlier).

### Entity / Field map (spec → codebase)  — most already exists
| Spec section / field | Current model.field | Status |
|---|---|---|
| **1 Branch Identity** Customer FK, Branch Name, Billing Address, GSTIN, PAN, Branch Legal Name | `CustomerBranch.customer_id / branch_name / billing_address / gstin / pan / branch_legal_name` | **EXISTS** |
| **2 Holiday Billing Policy** (per branch/year table: Calendar Year, Holiday Count, IsFreeze) | Holiday drill-down = `models.leave.Holiday(branch_id, year, holiday_date, name, is_active)`; **no per-branch/year header row with IsFreeze** | **GAP → new `BranchHolidayYear(branch_id, calendar_year, is_freeze)`; Holiday Count = COUNT(Holiday for branch+year)** |
| **3 Leave & Holiday Billing Policy** billability + thresholds | `CustomerBranch.holidays_billable / weekoff_billable / leave_billable / comp_off_billable / hours_required_half_day / hours_required_full_day / hours_required_half_day_comp_off / hours_required_full_day_comp_off / working_hours_per_day` | **EXISTS** |
| **4 Billing Properties** cycle + 4 toggle/value caps | `CustomerBranch.billing_frequency / billing_cycle_start_day / billing_cycle_end_day / is_max_billable_hours_per_day+max_ / is_max_billable_hours_per_month+max_ / is_max_billable_days_per_month+max_ / is_initial_no_billing_period + initial_no_billing_qty + initial_no_billing_period` | **EXISTS** |
| **5 Billable Leave Policy** sub-table (Leave Name, Credit Type, Leave_Expire, Is Max Limit, Prorate, Credit Balance, Initial Credit Balance, Max Carry Forward, Credit Timing, Effective Date) | `models.leave.CustomerLeavePolicy` (has `branch_id`, `leave_type_id`, `leave_credit_type`, `leave_expire`, `is_max_limit`, `prorate_balance_credit`, `leave_credit_balance`, `initial_credit_balance`, `maximum_carry_forward`, `leave_credit_timing`, `effective_date`) | **EXISTS (branch-scoped)** — 2 enum mismatches, see Open Q1/Q2 |
| **6 Linked Projects + inheritance** Project repeats §3–4 fields; one resolver | `Project` has ONLY `billing_cycle_start_day/end_day, billing_frequency, max_billable_hours_day, max_billable_hours_month, max_billable_days_month, no_billing_period_days`; **missing** §3 billability/thresholds/comp-off + the `is_max_*` toggles + `working_hours_per_day` | **GAP → add nullable override fields to Project + a `resolve_branch_project_policy()` resolver (project value if set else branch default)** |

### Implementation plan (Phases 1–3 deliverables)
- [x] **P1-a** New `BranchHolidayYear` model + migration 0028 (branch_id, calendar_year, is_freeze; unique(branch_id, calendar_year)).
- [x] **P1-b** Link `Holiday` drill-down to branch+year (already has both cols; add index/FK-consistency + optional `branch_holiday_year_id`).
- [x] **P1-c** Reconcile §5 `Leave_Expire` + `Leave Credit Timing` enums (per Open Q1/Q2).
- [x] **P1-d** Add Project override fields for §3 & §4 (nullable) + migration.
- [x] **P2-a** CRUD API: branch identity + §3/§4 fields (extend existing branch endpoints), holiday-year table + holiday drill-down, branch leave-policy sub-table (empty allowed).
- [x] **P2-b** Pure `resolve_branch_project_policy(project, branch)` resolver (project→branch) — unit tested.
- [x] **P2-c** Pure billability/threshold/cap functions; caps enforced only when `is_*` toggle ON — unit tested.
- [x] **P2-d** Seed/round-trip test: HARMAN-Bangalore (empty leave policy) + Adani Motor (blank counts/balances, 1 template leave row).
- [x] **P3-a** Branch **View** form: six blocks in order, two tables with per-row View/Edit, holiday-year → holiday drill-down. Linear-meets-Stripe, tokens only.
- [x] **P3-b** Branch **Edit** form parity.

### Open Questions (faithful interpretation chosen; confirm at gate)
- **Q1 — `Leave_Expire` type.** Spec lists it with value **"Days"** (a dropdown), but the
  existing column `CustomerLeavePolicy.leave_expire` is **Boolean** and is already used by
  the year-end carry/expiry job (`apply_year_end_carry` reads `policy.leave_expire`).
  *Faithful plan:* add a new string/enum column **`leave_expire`** (values: `Days`, extendable)
  as the spec field, and rename the existing boolean to `leave_expire_enabled` for the job
  (migration handles both). **Confirm** vs. keeping the boolean as-is.
- **Q2 — `Leave Credit Timing` value.** Spec value is **`Start_of_Month`**; existing rows use
  **`Start_Of_Period`**. The column is a free `String`, so it already stores any value.
  *Faithful plan:* accept/emit `Start_of_Month` in the branch module and make the credit job
  treat `Start_of_Month` as upfront-at-month-start (same as current Monthly start). **Confirm.**
- **Q3 — `IsFreeze` semantics.** Assumed: freezing a branch/year locks that year's holiday
  records (read-only) and freezes its holiday-billing for the year. **Confirm.**
- **Q4 — Blank Holiday Count (Adani 2025).** A `BranchHolidayYear` row exists with zero linked
  holidays → Holiday Count renders blank/"—" (COUNT=0), not an error. **Confirm.**
- **Q5 — "Leave Name" source.** Kept as FK to the `LeavePolicyType` master (existing), so
  "Leave Name" = the type's name, rather than a free-text field. **Confirm.**
- **Q6 — §5 table location.** Reuse `CustomerLeavePolicy` with `branch_id` (already the
  branch-scoped leave-policy table) rather than a new table. **Confirm.**
- **Q7 — Project overrides shape.** Add the missing §3/§4 fields **inline on `Project`** (nullable),
  consistent with the partial set already inline, rather than a separate `ProjectBillingPolicy`
  table. **Confirm.**

### GATE (Phase 0) — STOP
Plan written, existing patterns identified, gaps + Open Questions listed. Awaiting go-ahead
for Phase 1 (and answers to Q1–Q7, especially Q1 which changes a migration).

## PHASE 1 — Schema & migrations  ✅ (awaiting go-ahead for Phase 2)

**Delivered**
- `models.customers.BranchHolidayYear` (branch_id, calendar_year, is_freeze, created_at; unique(branch_id, calendar_year)) + `CustomerBranch.holiday_years` relationship. Holiday Count is DERIVED from `Holiday(branch_id, year)` (year row can have 0 holidays → blank).
- `models.leave.CustomerLeavePolicy.leave_expire`: Boolean → **String(24)** nullable (spec "Leave_Expire" dropdown; value `"Days"`, NULL = no expiry). `apply_year_end_carry` still reads it via `bool(...)`, so expiry logic is unchanged. Pydantic schemas + seed + test helper updated to the string form.
- `LEAVE_CREDIT_TIMINGS` now also accepts **`Start_of_Month`** (spec §5). Added `LEAVE_EXPIRE_UNITS = ("Days",)`.
- `models.projects.Project`: added §3 + §4 override columns (all nullable = inherit): holidays/weekoff/leave/comp_off billable, the four hour thresholds + comp-off thresholds, working_hours_per_day, the four `is_max_*`/`is_initial_*` toggles, initial_no_billing_qty/period.
- Alembic migration **`0028_branch_holiday_year_and_policy_overrides.py`** (create table, boolean→string with `postgresql_using`, add project columns) + downgrade.

**Schema self-review (field checklist)** — every spec field present & correctly typed/nullable:
- §1 identity — pre-existing on CustomerBranch ✓
- §2 holiday-year header — new table ✓ · Holiday drill-down = existing Holiday rows ✓ · IsFreeze ✓
- §3 billability + 5 thresholds — CustomerBranch ✓ · Project overrides added ✓
- §4 cycle + 4 toggle/value caps — CustomerBranch ✓ · Project toggles added ✓
- §5 leave-policy sub-table (branch-scoped) — CustomerLeavePolicy ✓ · Leave_Expire dropdown ✓ · Start_of_Month ✓
- §6 Project override fields — added ✓ · resolver is Phase 2

**Verification:** `create_all` builds the full new schema on SQLite; branch_holiday_years present; all 15 Project override columns present; leave_expire is String. **42 tests pass** (PE UC + invoice/my-leave + billing + scenarios) — no regression.

**GATE (Phase 1) — STOP.** Ready for Phase 2 (CRUD + resolver + pure billing functions + unit tests) on your go-ahead.

> ⚠️ Separate issue (not this module): external tooling reverted the earlier
> "remove Contact Phone / HiringManager_Contact" change — `opportunity_form_schema.py`
> has both keys again and `tests/test_opportunity_form_schema.py` is corrupted
> (truncated at line 86), which fails the whole test file's collection.

## PHASE 2 — Business logic & unit tests  ✅ gate met (P2-a CRUD router pending)

**Delivered** — `services/branch_policy.py`
- `resolve_branch_project_policy(project, branch)` — the ONE reusable resolver
  (project value if set, else branch, else built-in default). Handles the legacy
  Project cap column-name differences internally so callers get one canonical shape.
- `ResolvedBillingPolicy` with pure helpers: `is_billable(category)`,
  `day_fraction_from_hours(hours, comp_off=)`, and cap enforcers
  `cap_hours_per_day / cap_hours_per_month / cap_days_per_month` — each enforced
  ONLY when its `is_*` toggle is on.
- `branch_holiday_count(db, branch, year)` (derived) + `branch_holiday_years(db, branch)`
  (§2 table rows; count renders blank when zero).

**Gate tests — `tests/test_branch_policy_uc.py` (8 passed):**
- (a) inheritance: project override wins, unset fields inherit branch ✓
- (b) each billability toggle (holiday/weekoff/leave/comp-off) ✓
- (c) caps enforced ON, ignored OFF even with a value set ✓
- (d) empty leave-policy branch reads cleanly ✓
- round-trip HARMAN-Bangalore (empty leave policy, 2025→9 / 2026→8 holidays) ✓
- round-trip Adani Motor (2025 year row, blank count; 1 Monthly/Start_of_Month row, blank balances) ✓
- dummy employee end-to-end: resolved policy → present billable, leave not, comp-off billable, per-day hours capped at 8 → 3 billable days ✓

**Regression:** full backend PE + branch suites **50 passed**.

**Remaining for P2-a (CRUD router):** BranchHolidayYear create/list/freeze endpoints,
Holiday drill-down under a year, and surfacing the branch §3/§4 fields + the leave-policy
sub-table in the branch serializer. Service layer is done; the FastAPI router is thin
plumbing but won't go live until the backend process restarts (it does not hot-reload).

**GATE (Phase 2) — STOP.** Unit tests (a)–(d) pass. Ready for Phase 3 (UI) on go-ahead.

## PHASE 3 — UI (View + Edit) + P2-a CRUD router  ✅ (built; needs backend restart + FE rebuild to go live)

**Backend (appended to `routers/crm/customers.py`, parse-verified):**
- `GET /api/customers/branches/{id}/policy` — one-call aggregation: identity + §3/§4
  (serialize_branch) + §2 holiday years (derived count) + §5 leave-policy rows + §6 linked projects.
- `GET/POST /api/customers/branches/{id}/holiday-years` and `PATCH .../{year_id}` (freeze toggle).
- §2 holiday drill-down reuses existing `GET /api/holidays?branch_id=&year=`; §5 reuses
  `GET/POST /api/customer-leave-policies?branch_id=`; §1/§3/§4 reuse existing branch GET/PUT.

**Frontend (new `src/crm/pages/BranchPolicy.tsx`, route `branch-policy/:id`):**
- Six blocks in spec order: 1 Identity · 2 Holiday Billing Policy (year table with Add/Freeze +
  click-to-drill-down holidays) · 3 Leave & Holiday Billing (checkboxes + thresholds) ·
  4 Billing Properties (cycle + 4 toggle/value caps) · 5 Billable Leave Policy sub-table
  (empty-state valid; Add template row) · 6 Linked Projects.
- View by default; "Edit policy" toggles inputs for blocks 3 & 4 and PUTs the branch.
- Tokens/components only (Field, DataTable, StatusBadge, KpiCard, btn*, inputCls, focusRing).
- Structurally verified (brace-balanced, exports present). Reach via
  `?view=crm&p=branch-policy/{branchId}`.

**Caveats (same as prior FE/BE work):** backend does not hot-reload, so the new endpoints
need one server restart; the FE needs a rebuild; if Cursor is co-editing `routes.ts` it may
revert the route (the new page file itself persists). A link from the branch list can be
added in one line once Cursor is paused.

**GATE (Phase 3) — module complete pending the restart/rebuild.**

---

# QA — Branch-wise Leave & Holiday Policy (two-axis: PAID vs BILLABLE)

New pure functions in `services/branch_policy.py`: `classify_day`, `day_paid`,
`run_month`, `branch_year_is_frozen`. PAID axis = leave/comp-off balance; BILLABLE
axis = resolved flags + thresholds + caps. They never share a switch.

- [x] **Suite A** — day-type logic (A1–A9) — `tests/test_branch_qa_phase1.py` ✅
- [x] **Suite C** — comp-off boundaries (3h59/4/6h59/7/9) ✅
- [x] **Suite B** — full month → 48 billable / Rs.24,000 / comp-off 0.5 / CL 0 / 1 LOP
- [x] **Suite E** — caps E1–E5
- [x] **Suite F** — project inheritance/override F1–F4
- [x] **Suite G** — edge/empty G1–G5
- [x] **Suite H** — round-trip persistence

**GATE 1 (A + C): PASS** — 2/2. STOP for go-ahead to Phase 2.

## Open Questions
1. **Comp Off Billable interpretation (CONFIRMED by user):** "Comp Off Billable=Yes"
   = hours WORKED on a holiday/week-off are billable; the comp-off DAY taken later is
   NOT billed. Comp-off is earned ONLY on worked holidays/week-offs (never a normal day).
2. **Split day (CONFIRMED):** a partial-work + partial-leave day (e.g. Nov7 4h work + 4h CL)
   bills only the worked hours; the leave portion draws leave balance (0.5 day here).

No fields renamed or dropped; all billing flows through the single
`resolve_branch_project_policy` resolver.

**GATE 2 (B + E + F): PASS** — 3/3 suites (`tests/test_branch_qa_phase2.py`).
Suite B exact: 48 billable · Rs.24,000 · comp-off 0.5 · CL used 1.5 / closing 0 · 1 LOP · non-billable-present Nov5,6,9,10,12.

## Incident log (session)
- Null-byte padding corrupted 8 backend files mid-session (external editor/formatter).
  Stripped padding; all now match git HEAD (no permanent content loss). My uncommitted
  branch holiday-year endpoints in `routers/crm/customers.py` were truncated away and
  need re-appending.
- `services/opportunity_form_schema.py` remains broken (IndentationError) from the earlier
  Cursor revert of the opportunity-form change — separate from this module.

**GATE 3 (G + H): PASS** — 7/7 (`tests/test_branch_qa_phase3.py`).

## PHASE 4 — Handoff

**Result: A–H all PASS (20 assertions/cases across 3 files, run together = 20 passed).**

| Suite | What | Result |
|---|---|---|
| A | day-type logic A1–A9 | PASS |
| C | comp-off boundaries | PASS |
| B | full month → invoice | **PASS — 48 / Rs.24,000 / comp-off 0.5 / CL used 1.5 closing 0 / 1 LOP** |
| E | caps E1–E5 (ON/OFF, month cap, initial no-billing) | PASS |
| F | project inheritance/override F1–F4 (one resolver) | PASS |
| G | edge/empty G1–G5 | PASS |
| H | round-trip persistence + downstream recompute | PASS |

**Suite B key numbers (as asserted, not adjusted):** total billable **48h**, invoice
**Rs.24,000** @Rs.500/h, comp-off closing **0.5**, CL used **1.5** / closing **0**,
**1 LOP** day (Nov6), non-billable-but-present **Nov5,6,9,10,12**.

**Open Questions**
1. **Comp Off Billable interpretation (CONFIRMED):** "Comp Off Billable=Yes" = hours WORKED
   on a holiday/week-off are billable; the comp-off DAY taken later is NOT billed. Comp-off
   is earned ONLY on worked holidays/week-offs (never a normal working day).
2. **Split day (CONFIRMED):** partial-work + partial-leave bills only the worked hours; the
   leave portion draws leave balance (0.5 day for a 4h-of-8h split).

**Integrity confirmations**
- No spec field renamed or dropped; the two-axis model (PAID = leave/comp-off balance;
  BILLABLE = flags + thresholds + caps) never shares a switch.
- All billing flows through the single `resolve_branch_project_policy` resolver
  (branch default ← project override), used identically in Suites B/E/F and the app.
- Caps enforced ONLY when their `Is-*` toggle is ON (0-with-OFF = not enforced), proven in E.

**Test files:** `tests/test_branch_qa_phase1.py` (A,C), `..._phase2.py` (B,E,F),
`..._phase3.py` (G,H). Pure engine + persistence in `services/branch_policy.py`.

---

# Access Templates (department/role-wise tab + field permissions) — Plan

Admin/CEO builds reusable **named templates** (Sales, RMG, TA, HR, …) that grant, per CRM
tab and per field, a mode of **View** or **Insert/Edit**, then assigns a template to a user.
Decisions (confirmed): **live link** (edit template → all its users update), **View vs Insert/Edit**
modes, **named templates optionally tagged to a Department/role**.

## PHASE 0 — Discovery ✅ (awaiting go-ahead)

**What exists today**
- Per-user access on `UserProfile`: `tab_access` (encoded list of visible tab keys) +
  `field_access` (encoded `{tab: [fields]}`). Services `get/set_tab_access`, `get/set_field_access`.
  `GET/PUT /api/users/{id}/tab-access`. `/api/me` returns `tab_access` + `field_access`
  (null = full access). `Department` master exists. Latest migration **0028** → next **0029**.
- Gap: no reusable **template** entity, and no **View/Insert** mode (today access is binary).

**Data model (Phase 1)**
- New `access_templates`: `id, name (unique), description, department_id FK→departments (nullable),
  role (nullable), is_active, tab_access JSON, field_access JSON, created_at, updated_at`.
  - `tab_access` JSON = `{ "<tab_key>": "view" | "edit" }` (tab absent = no access).
  - `field_access` JSON = `{ "<tab_key>": { "<field_key>": "view" | "edit" } }`.
- `user_profiles.access_template_id` FK→access_templates (nullable) — the **live link**.
- A **registry** of grantable tabs + fields per tab (derived from CRM nav + each page's fields)
  so the template editor shows real checkboxes, and the server can validate keys.

**Resolution (Phase 2)** — one reusable `effective_access(db, user_id)`:
- start from the linked template (live), then apply any per-user override (override wins
  per-tab/field); Admin/CEO always resolve to full access.
- Output both the **mode map** (`{tab: mode}`, `{tab:{field:mode}}`) and a legacy visible-tab
  **list** so today's `/api/me` consumers keep working.

**Enforcement (Phase 4)** — a server-side write-guard dependency: a user with `view` (or no
access) on a tab/field is rejected on insert/update; read endpoints filtered to visible tabs.
UI gates from `/api/me` (hide tabs, disable fields) but the server is the source of truth.

## Phases & gates
- [x] **P1** Schema + migration 0029 (`access_templates`, `user_profiles.access_template_id`) + tab/field registry.
- [x] **P2** CRUD API for templates (Admin/CEO), assign-to-user, `effective_access` resolver, `/api/me` returns modes. Unit tests.
- [x] **P3** Users-tab UI: Templates manager (create/edit: pick tabs + fields + View/Insert) and assign to a user (Admin/CEO only).
- [x] **P4** Server-side enforcement on write endpoints + black-box/E2E tests.

## Open Questions (faithful defaults chosen; confirm at gate)
1. **Tab/field registry source.** Tabs = the CRM nav keys (customers, opportunities, projects,
   project-employees, timesheets, invoices, …). Fields = a curated per-tab list from each page's
   form. *Confirm* whether you want ALL fields grantable or only the sensitive ones.
2. **Override precedence.** If a user has BOTH a template and a per-user override → the override
   wins per tab/field (proposed). *Confirm.*
3. **Admin/CEO bypass.** Admin and CEO always get full access regardless of template (proposed). *Confirm.*
4. **"View" tab semantics.** A tab granted `view` = the page opens read-only (no insert/save);
   `edit` = can insert/update. Fields inherit their own mode within an editable tab. *Confirm.*

**GATE (Phase 0) — STOP.** Plan + model + open questions written. Ready for Phase 1 on go-ahead.

### PHASE 1 — Schema + registry ✅ (awaiting go-ahead for Phase 2)
- `models/access_templates.py :: AccessTemplate` (name unique, description, department_id FK,
  role tag, is_active, `tab_access` JSON `{tab:mode}`, `field_access` JSON `{tab:{field:mode}}`, timestamps).
- `user_profiles.access_template_id` FK (live link) added.
- `services/access_registry.py` — 21 grantable tabs + curated fields per tab, MODES=(view,edit),
  `registry()` for the editor, `validate_access()` (rejects unknown tab/field/mode).
- Alembic **0029_access_templates** (create table + add FK column) + downgrade.
- **Verified:** create_all builds the schema; registry validates good input and rejects unknown
  tab + bad mode; **79 tests still pass** (no regression).
- **GATE (P1): PASS — STOP.** Ready for Phase 2 (CRUD API + assign + `effective_access` resolver + `/me` modes + tests).

### PHASE 2 — API + resolver ✅ (awaiting go-ahead for Phase 3)
- `services/access_templates.py`: CRUD (`create/update/delete/list/get`, name-unique, registry-validated),
  `assign_template(user, template|None)` (live link; delete unlinks users), and the reusable
  **`effective_access(db, user_id, roles)`** resolver — Admin/CEO→full; else template (live) with the
  per-user legacy override winning per tab/field (granted as `edit`); `visible_tabs=None` = role defaults.
- `routers/crm/access_templates.py` (Admin/CEO only): `GET/POST /api/access-templates`,
  `GET/PUT/DELETE /{id}`, `GET /registry`, `POST /assign`. Registered in `register_crm_routers`.
- `/api/me` now returns a mode-aware **`access`** object `{full, template_id, tabs:{tab:mode},
  fields:{tab:{field:mode}}, visible_tabs, source}` (legacy `tab_access`/`field_access` kept).
- Migration base already 0029 (Phase 1). **Tests: 9/9** (`tests/test_access_templates.py`) —
  resolver (admin/template/override/role-default/inactive) + black-box CRUD/assign/registry/validation/403/`/me`.
- **Regression: 88 tests pass.** **GATE (P2): PASS — STOP.** Ready for Phase 3 (Users-tab UI).

### PHASE 3 — Users-tab UI ✅ (awaiting go-ahead for Phase 4)
- New page `src/crm/pages/AccessTemplates.tsx` (route `access-templates`, Admin-only nav "Access Templates"):
  - template list + editor; name/role/department/active/description.
  - per-tab + per-field **mode selector** (No access / View / Insert-Edit) driven by `GET /registry`.
  - Create/Update (POST/PUT), Delete, and **Assign to user** (user dropdown → `POST /assign`).
  - Tokens/components only (Field, inputCls, btn*, StatusBadge, EmptyState, useToast). Brace-verified, exports present.
- Route + nav wired (routes.ts + CrmApp NAV, roles ["Admin"]).
- **Caveat:** needs FE rebuild + one backend restart to go live; Cursor may revert routes.ts/CrmApp.
- **GATE (P3): PASS — STOP.** Ready for Phase 4 (server-side write enforcement + black-box/E2E tests).

### PHASE 4 — Server-side enforcement ✅ (wired to major CRM routers)
- Reusable guard `crm_deps.require_access(tab, *, mode="view"|"edit", field=None)` plus
  `gated_read(tab, *roles)` / `gated_write(tab, *roles)` — combines role RBAC + template access.
- **Wired on:** `customers` (+ `branch-policy` holiday-years), `opportunities`, `projects` /
  `project-employees`, `finance` (`pos`, `invoices`, `tds`), `employees`, `timesheets`.
- **Tests:** 5/5 (`tests/test_access_enforcement.py`) incl. real `/api/customers` wire-in.

### PHASE 5 — Frontend gating + Users table assign ✅
- `src/crm/useAccess.ts` — `crmTabVisibleFromMe`, `useCanEditTab`, `useCrmAccess` from `me.access`.
- `CrmApp` nav filters on `access.tabs`; `Customers` view-only banner + write gating.
- `UsersAdmin` per-row Access Template dropdown → `POST /api/access-templates/assign`.
- **Legacy modal kept:** per-user tab/field override (`Edit Tab Access`) wins over template per
  tab/field; template is the default baseline; both coexist (documented here).

### Registry coverage ✅
- `FIELDS_BY_TAB` expanded: requirements, candidates, profiles, template-requests,
  leave-applications, holidays, my-leave, reports, settings, tds.

## Access Templates — DONE (P1–P5)
- P1 schema + registry · P2 CRUD API + `effective_access` + `/me` modes ·
  P3 template editor UI · P4 enforcement guard · P5 wired endpoints + FE gating + Users assign.
- **Total: 14 tests** (9 P2 + 5 P4). Migration **0029** at head.
- **Deploy:** alembic 0029 applied; restart backend + FE dev for live UI.
