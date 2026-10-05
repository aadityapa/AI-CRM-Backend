# CLAUDE.md — Karnex backend (AI-Interview-Model-B-V2)

Working notes for AI assistants. **Rewritten 18 Aug 2026** from a full mechanical scan of
both repos (endpoint extraction, model/table counts, migration-chain walk, full test run,
cross-repo parity diff). Numbers below were measured, not remembered.

Companion file: `F:\AI-Interview-Model-F-V2\CLAUDE.md` (frontend).

> **Corrections to the previous edition of this file** — it said ~271 endpoints, ~52 tables,
> 38 router modules, `PipelineStatus` has 20 values, and listed route-order traps that do not
> exist. Actual: **403 routes, 85 tables, 39 modules, 17 pipeline statuses, zero route-order
> traps**. The `invoices/tax-generator` "trap" is not a real route on this side at all.

---

## 1. What this repo is

One FastAPI application that is really **two products bolted together**:

| Half | Style | Storage | Entry |
| --- | --- | --- | --- |
| **Interview platform** (legacy, came first) | flat `@app.*` routes in `main.py`, raw SQL, in-process session dicts | `auth_db.py` — dual-dialect SQLite **or** Postgres, no ORM, no Alembic | `backend/main.py` (7,865 lines / 325 KB) |
| **Karnex CRM/ERP** (added later) | `APIRouter` modules, SQLAlchemy 2.0, Alembic, RBAC dependencies | `crm_db.py` — **Postgres only**, 503 when unconfigured | `backend/routers/crm/__init__.py::register_crm_routers(app)` |

They meet at `backend/services/ai_interview_bridge.py` (CRM schedules an AI L1 interview) and at
`registration_data`, the legacy users table that CRM models FK into via `models/base.py::USERS_TABLE`
(30 FK columns across 15 model modules).

Serving the UI is also this app's job: `/admin` mounts the built React dashboard (`main.py:7830`)
and `/` mounts the vanilla candidate UI (`main.py:7856`), both from the sibling **F-V2** repo
resolved by `paths._resolve_frontend_dir()` (`FRONTEND_DIR` env → `<repo>/frontend` →
`../AI-Interview-Model-F-V2/frontend`).

> ⚠️ **8 Sep 2026 — this file was RESTORED after the working tree was rolled back.** At ~16:10 IST the
> whole `F:\AI-Interview-Model-B-V2` tree was silently replaced by a ~20 Aug snapshot (65 files gone,
> 88 older, migrations ended at 0079) while the live server still ran the full code — every
> profile/resume endpoint 500'd (`ModuleNotFoundError: services.ctc`). `backend/` was restored from
> an in-session copy taken minutes earlier (1034 tests green); the pre-restore files are in
> `_archive/backend-before-restore-2026-09-08/`. The newer edition of THIS file (re-verified 20 Aug,
> with the 26 Aug – 3 Sep notes) was lost; the essentials are summarised below. Nothing was
> committed — **commit the working tree** so this cannot happen again.

**Working tree, 8 Sep 2026:** branch `main`, head **`dd61630`**, ~90 modified + 63 untracked paths,
none committed. `git status` is dominated by CRLF churn — review with `git diff --ignore-all-space`.

**Migration head is now 0120** (was 0097 on 8 Sep; see the dated notes below) (`0080`…`0097` are untracked files under `alembic/versions/`).
Deploying REQUIRES `alembic upgrade head` before serving traffic — the ORM maps columns from every
one of them. Tests: **1034 pass**, 2 stale pins fail (`test_full_pipeline_flow::test_ai_l1_can_only_be_triggered_by_ta`,
`test_pipeline_tail::test_no_other_stage_has_a_hidden_precondition`) plus the §9 known set.
`routers/crm/__init__.py::_MODULES` now registers **42** modules (adds `activity_log`,
`customer_receipts`, `public_invoice`).

**In-flight work since 18 Aug (all uncommitted) — headlines:** TA tracking dashboard
(`GET /api/dashboards/ta-tracking`); bulk "we're hiring" mail with an editable `{{token}}` template
(`services._render_email_template`, ONE `re.sub` pass — never chained `str.replace`); every resume
creates a Sourcing profile (`slot_booking.ensure_sourcing_profile`); bulk ZIP upload + AI resume
parsing + duplicate hold (`services/resume_parse.py`, migrations 0081/0087); the RMG screening gate
(0082, `hiring.rmg_screening_gate` setting, `rmg_screening_sla` job); requirement Hold/Resume +
priority (0083); the manual L1/L2/HR round pipeline (`MANUAL_ROUNDS`, 0088–0092, `HR_Screening` +
`HR_Interviewing` statuses, Sales Head approval, budget flag/resolve, 0094); email drafts with
built-in wording + admin custom drafts (0093); customer monthly hours cap on the timesheet summary;
Admin/CEO invoice undo; "Scan to view" QR on the Tax Invoice (`services/invoice_share.py`,
`routers/crm/public_invoice.py`, no auth); offer-letter overrides + PDF/DOCX download (0095,
`services/offer_letter.py`); customer receipts (0096, `routers/crm/customer_receipts.py`); Sales
hold stage (0097); opportunity list column filters; negative round verdict → rejection status;
importer provenance keys accepted on `details` (`opportunity_form_schema._IMPORT_META`); TA/RMG
recruiting notifications deep-link to the requirement's Applied Candidates row
(`services.candidate_profiles.applied_candidates_link` → `p=requirements/{id}&tab=resumes&q=<email>`).

**Added 11 Sep 2026 — Tax Invoice overhaul (migration 0098):** every printed value
lives in Settings ▸ Invoice (`invoice.*` keys incl. new `sac_code` = 998513, `service_description`,
`signatory_line`, `footer_website_url`, `footer_text`); the server renderers (`services/tax_invoice.py`
HTML/WeasyPrint + reportlab) and the new Word export (`services/tax_invoice_docx.py`,
`GET /api/invoices/{id}/tax-invoice.docx`) all read `seller_from_settings()` / `bank_from_settings()` —
the module constants are fallback only. `company_bank_accounts` (`routers/crm/bank_accounts.py`, Admin
writes, any CRM role reads) + `customer_billing_policies.bank_account_id`: the ONE account printed is
customer pick → default → `invoice.bank_*` (`company_invoice_config.resolve_bank_details`). Service
table follows the billing unit (`UNIT_COLUMNS`: cost basis · Qty (Days/Hours) · Leave · Rate Per Day ·
Amount) from `billing_breakdown_for_invoice` (frozen `approved_figures`, else live preview); `LineItem.
amount_override` makes the engine's amount win over qty × rate. `effective_sac()` treats the old default
998314 as unset (0098 also rewrites those allocation rows). `invoice_number` is editable (`InvoiceUpdate`,
unique) and can be typed at generation (`GenerateInvoiceIn.invoice_number`; `GET /api/invoices/next-number`
is declared BEFORE `/invoices/{invoice_id}` — keep it there). Footer prints the website only, as a link.
F-V2: Settings ▸ Invoice tab (+ bank accounts panel), customer form bank picker, Edit modal on the invoice
page (list pencil → `?edit=1`), Generate dialog asks the number, "Tax Invoice (PDF)" = the on-screen
sheet captured (html2canvas `windowWidth` fix — 794px used to trigger the mobile layout), "Tax Invoice
(Word)" button.

**Added 11 Sep 2026 — invoice change requests (migration 0099, head is now 0099):** a generated invoice
is never edited in place. `routers/crm/invoice_revisions.py`: `POST /api/invoices/{id}/revisions` (Sales /
Finance / Sales Head, reason ≥10 chars, header fields + line qty/rate, one pending per invoice) →
`…/{rid}/approve` / `…/reject` (action `invoice.revision.approve`, defaults Sales + Sales_Head; Admin/CEO
always; **requester cannot approve their own**) → `_apply()` recomputes line amounts, sub-total, GST and PO
consumption and refuses over-consumed PO / below-paid totals. Admin/CEO requests auto-apply but are still
recorded. `invoice_revisions` keeps reason, diff, before/after snapshots and the decision — the history the
UI shows. Events `invoice.revision_requested/approved/rejected` notify Admin, CEO, Sales_Head (+ approvers /
Finance / the requester). The old direct `PUT /invoices/{id}` header edit is Admin/CEO-only now (403
otherwise). Pinned by `tests/test_invoice_revisions.py` (5 tests).

**Added 11 Sep 2026 — leave carry-forward + timesheet recalc (migration 0100, head is now 0100):**
`maximum_carry_forward` semantics are now uniform (customer, branch AND project policies; 0100 makes the
project column nullable): **NULL = carry the whole balance, 0 = lapse, N = carry up to N** — applied by
the Dec-31 `apply_year_end_carry` (now idempotent per year via the `pe_carry:`/`pe_expire:` sources) AND
by the Monthly/Quarterly cycle expiry, which used to zero everything. UI: one `CarryForwardField`
("At expiry — Lapses / Carries all / Carries up to N") in the customer, branch and project leave dialogs;
"" in the form = NULL on the wire. `POST /api/timesheets/{id}/recalculate` re-freezes an Approved,
uninvoiced sheet against the current policy (button "Recalculate with current policy"). The timesheet
grid's Billable Day now follows `_days_from_hours` (≥ full-day hours = 1, ≥ half = 0.5), not hours ÷ 8.
Branch/customer forms warn that Holidays/Weekoff Billable = calendar-month billing (weekends billed even
unworked; Comp-Off never applies). `leave_expire = "Carry Forward"` (shown as "Never — carries forward to next year") means the balance never
lapses: `expiry_applies()` is False, the Dec-31 job still writes the year's `Carry_Forward` ledger event, and
the project alias no longer maps it to Yearly. Pinned by `tests/test_leave_carry_and_prorate.py` (8 tests).

**11 Sep 2026 (later):** `timesheets.default_hours_worked` now pre-fills new sheets with the resolved
policy's working day (`working_hours_per_day` → `hours_required_full_day` → cap → 8), and the grid's
attendance rule takes the policy thresholds (`applyHoursAttendanceRule(row, {full, half})`); "Fill worked
days with N h" button on editable sheets. `POST /api/projects/employees/{pe}/leave/sync` now also
**backfills the monthly credit from onboarding** (`backfill_pe_leave_credit_from_onboarding`, idempotent) and
assigning an employee with a back-dated onboarding does the same at once. Branch policy page hands the
customer's default leave rows to the project wizard when the branch has none (`leave_policies_source`).

**Comp-Off lifecycle (11 Sep 2026):** weekend/holiday work (Comp Off Billable off) credits a separate
"Comp-Off" leave type on the PE's leave rows (`accrue_comp_off` → `credit_pe_leave`), capped by the
customer's `comp_off_max_limit`; it shows in the Leave tab and in Apply Leave like any other type. Its
31-Dec rule comes from the customer Comp Off section: `comp_off_max_carry_forward` NULL/0 = **lapses**,
N = carry up to N (`comp_off_year_end_cap`, used by `apply_year_end_carry` for policy-less rows).
**Comp-Off is NEVER credited by the monthly job** (bug fixed 11 Sep 2026: `credit_one_pe_leave_row` returns 0 for
policy-less / Comp-Off rows — their `leave_accrual` is the running EARNED total, and the job used to re-read it as
"N per month", turning one weekend day into a balance of 10). `POST …/leave/sync` runs
`repair_comp_off_over_credit` first: every bogus `pe_credit:` event on a Comp-Off row gets a reversing
`Adjustment` (source `pe_credit_reversal:<event id>`, idempotent) and the balance drops accordingly.

**11 Sep 2026 (evening) — filters + Emp ID (migration 0101, head is now 0101):** `candidates.created_at /
created_by_id / created_by_name` (backfilled from the earliest profile's TA owner + applied date, else Zoho
`source_created_date`); stamped by `create_candidate`, the Candidates-tab ZIP job and `ensure_sourcing_profile`
(first profile only). `GET /api/candidates` takes `created_by_id` (id or CSV), `created_from/to`;
`GET /api/candidates/creators` (declared BEFORE `/{candidate_id}`). Profiles: `GET /api/candidate-profiles/
customers` + `customer_id` filter; `ta_owner_id` is now id-or-CSV and `/ta-owners` **merges duplicate accounts by
name** (ids joined with commas); `applied_from/to` fall back to `created_at` when `applied_on` is NULL.
`GET /api/requirements/{id}/resumes` takes `applied_from/to` (resume `created_at`; profile-only rows use
`applied_on`). Employees list is ordered `date_of_joining DESC NULLS LAST, id DESC` and search matches
`employee_code`; the list shows an **Emp ID** column first. Profile Workflow section exposes `employee_ref` as
"Emp ID": at Joined, `ensure_employee_for_joined_profile` looks up an EXISTING employee by that code (after the
profile link, before the email match) and `_sync_employee_from_joined_profile` UPDATES it — designation,
department, Karnex joining date, official mailbox, Emp ID, CTC, re-activated — instead of creating a second
record (the internal-trainee-placed-with-a-customer scenario). New employees get `employee_code = employee_ref`.
Project Overview prints `effective_policy` (project → branch → customer) so inherited caps/hours no longer
show as "—".

**11 Sep 2026 (night) — timesheet fixes:** (1) `compute_billables`: a HALF-day leave on a worked day bills the
worked half (`min(hours, half-day hrs)`, ≤0.5 day) PLUS the leave half per the leave rules (`_half_leave_billable`)
— 4.5 h + half Sick Leave used to bill only the leave. Client mirror in `Timesheets.tsx::computeBillables`; the grid
offers "Apply half-day leave" on Half_Day rows (was Absent only). (2) `_billable_rollup(project, entries, policy)`
adds `billed_days` = working days + week-offs when `week_off_billable` + holidays when `holidays_billable`; the
Monthly branch of `timesheet_invoice_preview` and `per_day_charge` use it as the denominator, so an all-billable
31-day month prints Qty 31 / rate÷31 on the Tax Invoice (was 21 working days). (3) Summary exposes
`comp_off_earned_gross` and `comp_off_used` (= comp-off leave taken + `lop_covered_days`); the grid marks LOP rows
paid for by weekend work as "covered by Comp-Off" (`lopCoverByRow`, budget spent in date order) instead of a bare
LOP. Pinned by `tests/test_timesheet_half_leave_and_billed_days.py`.

**11 Sep 2026 (late) — migration 0102, head is now 0102:** `customer_billing_policies.comp_off_covers_lop`
(default FALSE). The "Harman rule" (weekend work automatically makes up the month's LOP) is now an **opt-in per
customer** — `BillingPolicy.comp_off_covers_lop`, customer-level only (branch/project don't override); with it off,
`lop_cover` is 0, the LOP stays on the sheet and the manager applies Comp-Off / any leave on that row. Customer form:
Comp Off section ▸ "Loss of Pay in the same month". `test_weekend_work_covers_lop` sets the flag; new
`test_weekend_work_does_not_cover_lop_by_default`. PE Leave tab "Leave history — credits & debits":
`pe_credit_history` now also returns `timesheet:<id>` events of this PE's sheets (consumption + comp-off credit) and
`pe_cycle_expire:`; comp-off reversal notes carry `PE#<id>`. Invoice tagline removed (`invoice.seller_tagline` key
deleted; `get_seller_details()["tagline"]` is always "").

**14 Sep 2026 — Full data backup (Settings ▸ Backup, Admin/CEO only):** `services/data_backup.py` +
`routers/crm/backup.py` (`/api/admin/backup/{datasets|status|history|download/{name}}`, `POST /api/admin/backup`
`{datasets:[…]|["all"]}`; all `role_required()`). `DATASETS` is the registry "tab → tables" (10 datasets; CRM
tables dumped from `Base.metadata`, legacy interview tables + `registration_data` via the SQLAlchemy inspector
with quoted identifiers). One ZIP: `karnex-backup.xlsx` (write-only workbook, sheet per table) + `<dataset>/csv|json/
<table>` + `files/<table>/<id>_<name>/<col>__<file>` for every `/api/crm-files/…` reference (row gets
`_files_folder`) + README. Redaction by column-name SUBSTRING (`is_redacted_column`: password/secret/token/
api_key/access_key/device_id/salt) and `app_settings` credential values; `login_data`/`password_reset_tokens`/
`openai_response_cache` never dumped. Background thread, ONE build at a time (409), archives in
`data/backups/` (gitignored), last `CRM_BACKUP_KEEP`=3 kept, `.part`/`.xlsx.tmp` cleaned on failure. CSV
neutralises formula injection. **`tests/test_data_backup.py::test_no_orm_table_is_forgotten_by_the_registry`
fails when a new table is not assigned to a dataset — add it there.** F-V2: `pages/settings/BackupTab.tsx`
(dataset checklist, setTimeout-chain polling, blob download via `authFetch`). Optional `date_from`/`date_to` (inclusive) window: `date_column_for(table)` picks the first of `_DATE_COLUMNS` (created_at, entry_date, invoice_date, applied_on, …); tables without one (masters, policies, settings) are always dumped whole. UI presets: All data · This FY · Last FY · This year · Last 12 months.

**14 Sep 2026 — Help & Support bot + tickets (migration 0103, head is now 0103):** `models/support.py`
(`support_tickets`, `support_ticket_messages`), `services/support.py`, `routers/crm/support.py` (`/api/support/chat`,
`/tickets[/meta|/summary|/{id}|/{id}/messages|/{id}/rating]`, `PATCH /tickets/{id}` staff-only). Gate is bare
`get_current_user` ON PURPOSE (legacy HR-only logins need support too); ownership enforced in `_load` (404, never
leaks); staff = `CurrentUser.is_admin`. Bot = Ask AI's `build_messages` with the SUPPORT persona swapped in
(`SUPPORT_SYSTEM_PROMPT`), no tools, `temperature 0.3`, `call_type="support_bot"`; the decision is the LAST line
`ESCALATE: yes|no` (`split_escalation_marker`, after `_parse_reply` strips `NAVIGATE_TO`), offline provider →
KB answer + escalate; `/chat` is rate-limited **per login** (`rate_limit.limit(spec, key_func=)` now accepts a key).
Tickets: `ticket_no = T-<id>` after flush (no race); Open → In_Progress on staff reply; user reply on
Resolved reopens, on Closed is refused (400); status/priority/assignee changes are system messages; assignee must be
an Admin/CEO login (`_staff_name`, name resolved server-side); `add_message` bumps `updated_at` (queue order);
rating once, owner only, once Resolved/Closed. Notifications: `support.ticket_raised` (Admin+CEO, dedupe per
ticket), `support.ticket_replied` (dedupe per MESSAGE — the outbox persists dedupe keys), `support.ticket_status`,
`support.ticket_assigned`; all in `email_flows.EVENTS`. F-V2: `components/support/SupportWidget.tsx` mounted in
`App.tsx` (every page; chat · raise ticket · my tickets · thread; Esc only when focus is inside; focus returns to
the trigger), `crm/pages/SupportTickets.tsx` — the list lives as **Settings ▸ Support Tickets** (`CrmSettingsPage` reads `?tab=`;
no sidebar entry); routes `support-tickets` (list, Admin/CEO) and `support-tickets/:id` (detail; also renders for the
owner from their bell link) stay for deep links. 13 tests in `tests/test_support_tickets.py`.
`tests/test_adaptive_question_engine.py::test_followup_fallback_adapts_to_answer_strength` is FLAKY (random
follow-up pick) — unrelated.

**14 Sep 2026 — customer-wise Projects hub:** every hub tab (Projects · Project Employees · Timesheets · Invoices ·
Customer Received Amount) now has the Purchase Orders layout — one expandable section per customer with a count and
summary — via `crm/components/CustomerGroupedList.tsx` (`CustomerGroupedList`, `ViewToggle`, `useGroupView`; the
"By customer / Flat list" choice is remembered in `localStorage["crm.hub.view"]`, default By customer). Grouped mode
fetches the whole filter set (`fetchAllMaster`), flat mode keeps the server-paged `DataTable`. Backend adds
`customer_id` / `customer_name` to `GET /api/timesheets` rows (via the project) and `GET /api/invoices` rows (via the
PO's customer, else the project's; legal entity name), both batched per page.

**14 Sep 2026 — interview times are IST end to end (`services/ist.py`):** one `IST` (Asia/Kolkata, FIXED
+05:30 fallback — `interview_rounds._IST` and `slots._DISPLAY_TZ` used to fall back to `timezone.utc` on a box
without tzdata, a silent 5h30 shift), `now_ist_stamp()`, `to_ist()`, `ist_naive()`, `read_as_ist()`. The report was
"TA scheduled the MANUAL L1 at 11 AM, candidate's mail said 3 PM". Closed: (1) manual L1/L2/HR rounds
(`schedule_l2_face_to_face`) mailed the raw datetime-local string (`2026-09-15T11:00`, no zone — mail clients guess);
every message now says `15 Sep 2026, 11:00 AM IST` (`_fmt_slot_ist`; `raw_when` keeps the typed text).
(2) `build_ics_invite` wrote a FLOATING `DTSTART` via `strftime` on the UTC-aware `event_dt` → the calendar card
showed 05:30; it now emits `DTSTART;TZID=Asia/Kolkata:` + a VTIMEZONE block. (3) Interview-round `raw_when`
(`services/interview_rounds.py`) printed the UTC clock of the posted ISO instant — now `ist_naive()` first.
(4) `POST /api/resumes/{id}/schedule-ai-interview` took NO time and the bridge stamped `datetime.now()` (server
clock at the click); it accepts `{scheduled_at: "YYYY-MM-DD HH:MM"}` (IST; 400 otherwise), the Applied Candidates
dialog has a picker, and the bridge's no-time default is `now_ist_stamp()`. `ScheduleAiInterviewModal` →
`/candidate-profiles/{id}/ai-interviews` was already a verbatim string passthrough. Pinned by
`tests/test_interview_time_ist.py` (12). Deploy: the app no longer depends on the host `TZ`; keep the DB session in
UTC; `pip install tzdata` on Windows hosts.

**14 Sep 2026 — role-based Dashboard ("every role gets its own desk"):** `services/dashboard_desk.py` +
three routes in `routers/crm/dashboards.py`: `GET /api/dashboard/today` (any CRM role; the role's 4 KPI tiles
`{key,label,value,detail,state ok|warn|bad,path,format int|money|percent}`, merged + de-duplicated for multi-role
users, capped at `MAX_TILES`=8; Admin/CEO get the company tiles), `GET /api/dashboard/upcoming?days=7` (rounds,
AI L1 slots, accepted-offer joinings, PE roll-offs, PO expiries, invoice due dates — each source only when the
caller can open its tab), `GET /api/dashboard/team` (`role_required("Sales_Head")` = Sales Head/Admin/CEO: `stuck`
points with the owning area, worst first, + per-person `ta` (from `ta_tracking`) and `sales` rows). All three use
the same `allowed(tab, *roles)` precedence as `my_work` (template decides alone when present). SLA constants live at
the top of the module (`SOURCING_SLA_DAYS` 5, `REVIEW_SLA_HOURS` 48, `CUSTOMER_WAIT_DAYS` 3, `STALLED_DAYS` 14,
`HIRING_STUCK_DAYS` 30). `my_work` gained `rmg_screening_pending`, `customer_feedback_to_chase`,
`timesheets_to_invoice`, `preboarding_open`. F-V2: `crm/pages/dashboard/DeskWidgets.tsx` (`TodayStrip`,
`UpcomingPanel`, `QuickActions` (role-filtered links), `TeamPanel`); `CrmDashboard.tsx` composes greeting + role chip
→ Today strip → My work | Coming up + Quick actions → Team (heads) → the pre-existing role sections. Admin/CEO with
no operational role get the company stuck-points where My work would be. Tests: `tests/test_dashboard_desk.py` (6).

**15 Sep 2026 — previous-year carry forward on a PE leave row:** `POST /api/projects/employees/{pe}/leave/{leave_id}/
carry-forward` `{from_year, days, note?}` (HR / Sales Head via `gated_write("project-employees")`; `from_year` must be a
past year). `services/project_employees.set_pe_carry_forward` books ONE `Carry_Forward` ledger event per (PE, type,
year), source `pe_carry:{pe}:{type}:{year}:manual` — re-posting the same year books only the DELTA (never stacks),
zero removes it; `opening_balance` and `leave_balance` move by the delta, and the event appears in the PE history
(`pe_carry:{pe}:%`) and in "Accrued this year". The Dec-31 job's idempotency key is the exact `pe_carry:{pe}:{type}:{yr}`,
so the two never collide. F-V2 PE Leave tab: "Carry fwd" column (opening balance, Add/Edit link) + "Carry forward"
header button → `PeCarryForwardModal`. Pinned by `test_leave_carry_and_prorate.py::test_manual_carry_forward_*`.

**15 Sep 2026 — Interview Integrity rebuilt (`services/interview_integrity.py`):** ONE event taxonomy
(`EVENT_TYPES`: label · family · penalty · strike?) — the strike set now includes what the candidate page
really sends on a tab change (`visibility_hidden`, `window_blur`), `fullscreen_exit`, `alt_tab`, `windows_key`,
`multiple_faces`, plus the new `clipboard` and `devtools`; `key_escape`/`key_f11`/`no_face`/`context_menu` are
informational. `main._count_integrity_violations` / `_INTEGRITY_VIOLATION_TYPES` are aliases of the module, so
`POST /interview/violation` is now the **server-side authority** for the 3-strike termination (it also accepts
`current_question`, fullscreen/visibility/focus context; the JPEG `evidence` upload it took until 23 Sep 2026 is
gone — the session recording replaced it), and on termination
notifies the scheduling TA (`interview.integrity_alert`, bell + email, dedupe per token). `_merge_proctor_events_
into_schedule` is idempotent (it used to re-append the whole proctor event list on every call).
`GET /interview/integrity-logs` returns **every** schedule (`list_interview_integrity_logs(db, None)`; CRM-scheduled
interviews are owned by `karnex-crm` and were invisible before) with per-family counts, `integrity_score` (100 −
weighted penalties, terminated capped at 20), `needs_review` (≤ 70, terminated, or `shared_with` — same device id /
IP across different candidate emails), CRM context (`profile_id`, requirement, customer, scheduler, AI score) and a
`summary`; `terminated` is the subset. `GET …/integrity-logs/export` (CSV, formula-neutralised) is declared BEFORE
`GET …/integrity-logs/{invite_token}` (full timeline with question index + evidence URLs) — keep it there
(`test_interview_integrity.py::test_export_route_is_declared_before_the_token_route`). F-V2: `pages/IntegrityLogs.tsx`
rebuilt (score ring, KPIs incl. Needs review / Avg integrity, chips All · Needs review · Live · Invited · Completed ·
Terminated, search, customer + date filters, family chips, timeline with evidence lightbox, CSV export, link to the
profile's AI Interview tab); candidate runtime logs `clipboard` / `context_menu` / `devtools` (F12, Ctrl+Shift+I/J/C,
Ctrl+U) and `no_face` (3 empty scans, ≤ 1 per 30 s), and attaches a 320-px JPEG to camera events
(`face_detection.captureEvidence`); `index.html` cache-bust `app.js?v=22`. Not done: answer-timing ("reading")
anomaly — needs the VAD timing join on `/answer`.

**15 Sep 2026 — AI interview link path, end-to-end fixes (`tests/test_interview_link_flow.py`, 11):**
(1) `services/invite_links.py` is the ONE invite-URL builder: Settings `email.public_base_url` → `PUBLIC_BASE_URL`
→ the scheduling request's origin (X-Forwarded-*, localhost swapped for the LAN IP) → last seen base. Every CRM path
(`ai_interview_bridge.schedule_l1_interview(..., request=)`, `ai_interviews._invite_url`, `resumes`, `slots`) uses
it; the bridge REFUSES to schedule (`scheduled=False`) when no base resolves instead of mailing `/?invite=…`.
`main._remember_invite_base` records the base from `/candidate/invite/{token}`. (2) `/candidate/invite/{token}/verify`
counts FAILED attempts only, resets on success, tells the candidate how many are left, and lets the already-bound
device (`x-device-id == active_device_id`, status verified/active) straight through with an empty POST
(`already_verified`) — a refresh mid-interview no longer burns an attempt. (3) `_public_schedule_view` is the only
schedule shape the candidate page sees (adds `job_title`, `timing_mode`, `time_limit_sec`, `num_q`; the
`scheduled_wait` response used to return the raw row WITH the access key). (4) CRM PUT on an AI interview keeps the
`__KARNEX_CFG__:` block (it split on a marker that never matched → config unparseable → candidate interviewed against
`jobs[0]`); a missing template now errors instead of falling back when the invite named one. (5) `_persist_fast_final_
report` no longer syncs the keyword-fallback verdict to the CRM — only `_upgrade_interview_report_background` /
`_finalize_interview_snapshot` do (fallback synced, flagged `fallback_ai_failed`, only if the upgrade throws);
`_score_percent` clamps 0..100. (6) `_should_recover_progress` leaves `report_status="generating"` rows younger than
10 min alone (it raced the background upgrade). (7) `/submit` with no session → 404 (was 200+error); `/answer`
closes a timed interview server-side at limit+90 s; `/next` already returns `time_remaining_sec` and the client now
re-anchors its clock to it. (8) Multi-worker without `REDIS_URL` REFUSES to start (`ALLOW_MULTI_WORKER_UNSAFE=1`
overrides). F-V2 runtime: `scene.js` (three.js from jsDelivr) is a dynamic import with a fallback — a blocked CDN
no longer blanks the page; `maybeAutoClearCache` keeps `karnexInviteDevice*` + auth on a version change during an
invite; `_newDeviceId()` works on plain-HTTP origins; a failed `/submit` shows a Retry card instead of redirecting to
Thank-You (`_showFinalizeRetry`), and the finalize loop retries one timeout with 30 s; `index.html` `app.js?v=23`.
Recruiter message now says "queued" when the outbox took the mail (SMTP happens later; check the Emails tab).

**15 Sep 2026 — scheduled time on the invite panel + 12-hour clock everywhere:** the Applied Candidates
invite card printed the AI link's `created_at` (the moment the TA clicked — "12:52 PM" for a 4 PM slot). Both
resume enrichers (`services/resumes.py` and the profile-only branch in `routers/crm/resumes.py`) now read
`scheduled_at_local` from the legacy schedule row (`get_schedules_by_tokens`) and emit it as an IST ISO instant via
`services.ist.local_stamp_to_iso` ("2026-09-15T16:00:00+05:30"); `link.created_at` is the fallback only. Candidate
emails use `services.ist.human_when` — "Tuesday, 15 September 2026 at 4:00 PM IST" (`interview_invite_email.
format_when`, `ai_interviews._when_text`). F-V2: `lib/datetime.ts` (`fmtDateTime12`, `fmtTime12`, `fmtDateShort` —
en-IN, 12-hour, no seconds) replaces every bare `Date.toLocaleString()` (locale-dependent, 24 h on en-GB boxes) in 15
files; the candidate runtime's "Scheduled:" lines use `formatHrDateTimeDisplay`; `index.html` `app.js?v=24`.

**15 Sep 2026 — RMG shortlisting desk (Applied Candidates):** (1) `POST /api/resumes/{id}/schedule-ai-interview`
no longer requires `ats_status == Shortlisted` — only an ATS-Rejected row is refused (the RMG screening gate
`rmg_screening_blocks_l1` still applies), so RMG sees **AI L1 and Go manual side by side** the moment the screening
is Shortlisted (F-V2 button gates: resume rows `ats != Rejected && (ats Shortlisted || rmg Shortlisted)`, profile-only
rows for TA **or** RMG). (2) "View resume" `FileLink` inside the RMG screening block — the CV opens inline without
leaving the tab. (3) Excel-style column chooser: `TABLE_REGISTRY["requirement_resumes"]` in
`routers/crm/table_preferences.py` (11 keys, nothing sortable — the list is client-sorted); F-V2 `ResumesTab` uses
`useTableLayout("requirement_resumes", RESUME_COLUMN_KEYS)` + `TableCustomizerButton`, Actions always pinned last;
`TableCustomizer` hides its Sort section when `sortable` is empty. (4) `PATCH /api/requirements/{id}/jd-skills`
`{rmg_jd_text?, description?, skills?}` — `gated_write("requirements","RMG","Sales","Sales_Head")` (Admin/CEO
implicit), **no creator restriction**, any non-terminal status (400 on Fulfilled/Closed/Cancelled), reuses
`_validated_skills`, activity `JD_SKILLS`; the full `PUT` stays creator-only + Draft/Rejected-only. F-V2: Details tab
shows an amber "JD / skills missing" banner + "Edit JD & skills" (`JdSkillsModal`) for those roles. Pinned by
`tests/test_requirement_jd_skills.py` (7).

**16 Sep 2026 — employee import "Last Working Day":** `services/employee_excel.COLUMNS` gained a trailing
`Last Working Day` column (template · export · import; sets `last_working_day` + `is_resigned`). Older downloaded
templates now fail the header check ("column count") — download a fresh one. The legacy HR sheet was converted with
managers resolved to emails, rows ordered managers-first (the importer resolves a manager against rows already
saved), Permanent→Full_Time / Contingent→Contract, `Designation||Role`→Designation + Role Title, self-reporting
cleared; `employees-import-ready.xlsx` at the repo root (not committed).

**16 Sep 2026 — AI interview voice + answer capture (`tests/test_candidate_speech_endpoints.py`, 11):** a live
interview ran with NO voice and every answer saved as "skip". Root: `/candidate/tts` and `/candidate/transcribe`
answered every provider failure with **HTTP 200 + `{"error"}`**, and the candidate page swallowed it — a dead/throttled
OpenAI key looked exactly like a silent candidate. Now: both return real statuses with a `code` (`400 no_text/no_audio/
too_short`, `503 tts_unavailable/stt_unavailable`, `502 tts_failed/tts_empty/stt_failed`; silence is `200 {"text":"",
"code":"no_speech"}`) via `main._speech_error`, and log to `karnex.interview.speech` (prewarm failures now WARN, not
DEBUG). **Indian-English voice:** `services/tts_prewarm.speech_request_kwargs()` is the ONE builder of the OpenAI
speech call (live stream + prewarm) and adds `instructions=DEFAULT_TTS_INSTRUCTIONS` for `gpt-4o-mini-tts` (never for
`tts-1*`); override with `OPENAI_TTS_INSTRUCTIONS` (`none` disables); the prewarm cache key includes the instructions.
`/answer`'s speech-evidence 409 guard now matches `silent_no_response` (`_NO_RESPONSE_TRIGGERS`) — the client never sent
"no_response", so it was dead code. F-V2 runtime: `js/question_voice.js` (server stream → server blob on a CLONED
response — the old blob fallback read an already-consumed body → browser `speechSynthesis` with an `en-IN` voice; resolves
on END; every fallback goes to `#candidateVoiceNotice` + console), `js/speech_transcribe.js` (one `/candidate/transcribe`
client; `TranscribeUnavailableError` + `transcriptionRecentlyDown()` distinguish "service down" from "silence"),
`interview_auto_advance.js` starts the browser `SpeechRecognition` (en-IN) when Silero (jsDelivr) fails to load,
`candidate.js` refuses an AUTOMATIC skip while ≥12 KB of audio is recorded and transcription is down ("tap Send to
retry"), reads the capture snapshot BEFORE `stopAutoAdvanceTurn()` resets it, wraps the pre-POST part of
`submitCandidateAnswer` so a throw can no longer wedge `_answerSubmitInFlight`, and a non-`speech_blocked` 409 no longer
dies with "body stream already read". `index.html` `app.js?v=26`. Deploy: restart the backend (the OpenAI client is
`lru_cache`d — a fixed key needs a restart); watch the log for `TTS provider error` / `transcription provider error`;
optional `OPENAI_TTS_VOICE` (default `nova`).

**16 Sep 2026 — invite link policy (`tests/test_invite_link_policy.py`, 7):** a link is inert BEFORE its slot, usable
ANY time after it, and works exactly ONCE — completion/termination closes it, not the clock. `main._invite_valid_hours()`
reads `INVITE_LINK_VALID_HOURS` (blank/0 = never expire, the default; the old hard-coded 24 h is gone) and drives both
`_invite_access_state` and the stale sweep in `_cleanup_expired_integrity_rows`. `/candidate/invite/{t}/verify` now checks
completed/terminated BEFORE the credentials (a wrong key on a closed link no longer burns an attempt) and refuses
`scheduled_wait` with **425** `{status:"scheduled_wait", seconds_until_start, starts_at_ist}`. `/login` returns
`resume: {current, total}` when the session already has answered turns (`_resume_info`). F-V2: `#screenInviteNotYet`
(countdown, re-runs the lookup at zero) on lookup `access.reason == "scheduled_wait"` and on the 425; the startup
line says "Resuming your interview from question N of M". Device check: guided mic test (say "one", say "two", clap —
`MIC_STEPS` in `device_test.js`, chips `[data-mic-step]`, pass = voice OR clap heard), 5-second triangle-wave chime
instead of the 660 Hz beep; Rules + Device Check screens are full-viewport glass panels (override layer "Pre-interview
screens v2" in `index.html`, JS hooks unchanged); `#recordingBadge` (`js/recording_badge.js`) blinks "REC · This
interview is being recorded" on the candidate feed from STEP-8 until `submitInterview`. `index.html` `app.js?v=27`.

**16 Sep 2026 — "interview closed on Skip" (`tests/test_skip_answer_flow.py`, +3):** in `timing_mode == "time"` the
question pool grows lazily via `_expand_time_mode_pool`, and `/answer` only called it for ANSWERED turns — skipping the
last generated question made `next_question_payload` return "Interview completed" with time still on the clock. Now it
runs on every non-finalizing turn, and `/next` tops the pool up before deciding "completed". `/answer` also takes an
optional `turn` form field (the question index the client is answering — `_parse_client_turn`): a stale turn
(`client_turn < current`, i.e. a double-click after the first press already advanced) is answered idempotently and
audited as `ignored_stale_turn` instead of skipping the NEXT question; the old `len(answers) > turn_index` check never
caught this because both counters move together. F-V2 `candidate.js`: `_pressInFlight` locks BOTH buttons on entry
(before the Skip pre-work that can wait seconds for a transcript; internal `_retryAfterTranscription` continuations
pass through; every early return calls `_releasePress()`), sends `turn`, and on a `speech_blocked` 409 resumes
listening with `autoSkip: false` for that turn (`beginAutoAdvanceTurn({autoSkip})` → `_autoSkipOffForTurn`) — the old
handler re-armed the silence timer and looped skip→409→skip. Timer: `state.interviewClockSynced` — the countdown shows
`--.--` until `/next` reports `time_remaining_sec`, so a resume never flashes the full limit before jumping. Note the
clock is wall-clock from `interview_started_epoch` (time away is deducted by design). `index.html` `app.js?v=28`.

**18 Sep 2026 — CEO Revenue report (`tests/test_revenue_report.py`, 8):** `services/revenue_report.py` is a pure
aggregation over ONE 13-month invoice/payment window pulled once (dialect-agnostic; no DB-side date maths).
`revenue_report(db, "YYYY-MM", today)` → `{month, label, as_of, headline, series[12], by_customer, ageing, pipeline,
efficiency, leakage}`. Definitions the CEO page prints and that must not drift: **billed** = `invoices.sub_total`
(excl. GST) by `invoice_date`; **collected** = `invoice_payments.amount` by `payment_date`; **outstanding** = open
(`payment_status != PAID`) invoice `balance_amount` dated up to month end; **ageing** = days past `due_date`, or
`invoice_date + DEFAULT_TERMS_DAYS` (30) when none; `concentration_risk` when one customer ≥ `CONCENTRATION_WARN_PCT`
(50 %); `po_cover_months` = active PO balance ÷ trailing-`RUNWAY_AVG_MONTHS` (3) average billed; leakage reads
`approved_figures.line_items[].loss_of_pay_days / lop_covered_days / no_billing_days_excluded` of the month's approved
sheets. `GET /api/reports/revenue?month=YYYY-MM` (`routers/crm/reports.py`) is **`role_required()` = Admin/CEO only — no
Access Template can widen it** (pinned by `test_route_is_admin_only`); 400 on a bad month. The Admin/CEO desk
(`dashboard_desk.today_tiles`) now leads with `co_revenue_month` (same billed definition, month-to-date) linking to
`reports?tab=revenue`. F-V2: `crm/pages/reports/RevenueReport.tsx` (month picker ‹ › capped at the current month, KPI
strip with MoM/YoY chips, recharts `ComposedChart` bar billed + line collected, customer table with share bars and the
concentration banner, ageing buckets + "Who owes us most", Coming up / Efficiency / Leakage panels); `CrmReports.tsx`
adds the `Revenue` tab only when `useHasRole()` (Admin/CEO), reads `?tab=`, and a deep link to a hidden tab falls back to
Opportunities (`HOSTED_TABS` skip the shared DataTable fetch/CSV).

**18 Sep 2026 — `/admin` deep links (`tests/test_admin_deep_links.py`, 3):** production answered
`{"detail":"Not Found"}` for every CRM "View report" / "Full report" button — the links were `/admin?view=…` while the
dashboard is a StaticFiles mount at `/admin/` (Starlette's slash redirect covers a bare `/admin` only, and the LAN box
happened to redirect; the proxy in front of karnexgroup.com did not). Every generated link (13 modules) now carries the
slash (`/admin/?view=…` — pinned by a source scan of `routers/` + `services/`), and `GET /admin` is an explicit 307 to
`/admin/?<same query>` declared BEFORE the mount so links already in sent emails / bell notifications keep working.

**18 Sep 2026 — Revenue report v2 (CEO asks; `tests/test_revenue_report.py` now 18):** same payload, six more
sections, still Admin/CEO only. `targets` — `app_settings` keys `revenue.target_month_default`, `revenue.target_fy`,
`revenue.target.YYYY-MM` (override); attainment, `run_rate` (billed ÷ days elapsed × days in month, current month
only), Indian FY (`FY_START_MONTH`=4, `fy_for()`), `fy_projection` = billed-to-date + trailing-3-month average × months
left, `fy_required_monthly`. `PUT /api/reports/revenue/targets` `{month?, month_target?, fy_target?, clear_*}` writes
those rows (Admin/CEO). `margin` — people cost = `employees.current_ctc` (**annual rupees**) ÷ 12 × the share of the
month each active `project_employees` row overlaps (`billing_date`/`onboarding_date` → `exit_date`; exited with no date
= never counted); `heads_without_ctc` is reported, never treated as free. `forecast` — next `FORECAST_MONTHS`=3 from
the deployed team's `billing_rate` (`_monthly_rate`: Yearly ÷ 12, Daily × 21, Hourly × 21 × 8), prorated for exits,
`po_shortfall` vs active PO balance. `collections` — DSO = outstanding ÷ trailing-90-day billing (incl. GST) × 90,
amount-weighted `avg_days_to_pay` of the month's receipts. `dimensions` — by `Opportunity.opp_type`, by
`CustomerBranch.branch_name`, by Sales owner (`Opportunity.created_by` → `registration_data.full_name` via raw SQL in
a savepoint; the test stub has no name column). `alerts` — behind month target (bad < 80 %), FY projection short, top-3
customer down ≥ `CUSTOMER_DROP_WARN_PCT` (30) MoM, concentration, 90+ overdue, PO cover < `PO_COVER_WARN_MONTHS` (2),
loss-making accounts; `alert_level()` drives the dashboard tile state (`revenue_summary_for_tile`).
`services/revenue_export.py::build_revenue_workbook` → `GET /api/reports/revenue/export.xlsx` (nine sheets, openpyxl).
Scheduler job `revenue_month_close` (`scheduler.revenue_month_close` setting, days 1–7 of a month, dedupe
`revenue_close:<YYYY-MM>`) mails Admin/CEO the closed month via `notify_roles(event="reports.revenue_month_close")` —
listed in `routers/crm/email_flows.py` so the route/wording are admin-editable. F-V2 page: alerts strip first, Targets
modal (three PUTs: month override · default · FY), target + FY panels with attainment bars, margin table, forecast
table, DSO tiles inside Ageing, "Billing by dimension", "Excel pack" button (blob via `authFetch`).

**21 Sep 2026 — position (headcount) change requests, migration 0104, head is now 0104
(`tests/test_requirement_positions.py`, 19):** `requirements.no_of_positions` is the sourcing target
(`check_and_mark_fulfilled` measures Joined profiles against it), so it no longer moves silently.
`requirement_position_requests` (from/to, reason ≥10, status, status_before/after, joined_at_decision,
requester + decider) + `routers/crm/requirement_positions.py`: Sales / Sales Head request
(`gated_write_action("requirement.positions.request", "requirements", "Sales", "Sales_Head")`), **RMG
approves — RMG + Admin/CEO ONLY** (`gated_write_action("requirement.positions.approve", "requirements",
"RMG")`, user decision), requester can't approve their own (Admin/CEO may; their own requests auto-apply
and are still logged), one Pending per requirement (Postgres partial index + a checked read), Closed /
Cancelled refused. **The joined floor is checked TWICE** — at request and again at approval, because
candidates join while a request waits; a refused approval writes nothing. Status re-derivation on approve
(`_apply`): an INCREASE on a **Fulfilled** requirement reopens it to `In_Progress` (Fulfilled is derived
state, so it is ours to undo — Closed/Cancelled are not, hence the block) and logs `REOPENED`; anything
else re-runs `check_and_mark_fulfilled`, so a DECREASE to the joined count fulfils on the spot;
pre-sourcing statuses are untouched. Events `requirement.positions_requested` (RMG + Sales Head) /
`_approved` (TA + Sales Head + requester — TA sources against the new target) / `_rejected`, all in
`email_flows.EVENTS`; both actions in `services/action_permissions.ACTIONS`; the new table is assigned to
the opportunities dataset in `data_backup.DATASETS`. ⚠️ `GET /api/requirements/position-requests/pending`
(RMG's cross-requirement queue) is declared **BEFORE** `/{requirement_id}/position-requests` — both are
two segments, so the literal must win; pinned by a router-order test AND a TestClient test.
**Route COUNTING is not a valid probe of registration**: this stack now resolves FastAPI 0.141 /
Starlette 1.6, where `include_router` mounts a sub-app instead of flattening onto `app.routes` — assert
with a real request (a 503 "CRM requires PostgreSQL" means the route resolved), never with `len(app.routes)`.
`services/requirements.py` gained the shared headcount helpers: `joined_counts_for_opportunities` (ONE
grouped query), `joined_count`, `positions_summary`, `positions_by_opportunity` (three queries for a whole
page — never per row). `GET /api/opportunities` rows now carry `positions_total / positions_joined /
positions_open / positions_change_pending / requirement_id`; an opportunity with no requirement yet is
absent from the map and the UI prints "—". Deliberately NOT sortable: it is derived per page, so a
server sort would order one page and read like a lie. **`GET /api/opportunities/{id}` carries the same
keys plus `requirement_status`** — a Sales login has NO route to the requirement page (the Requirements
sub-tab was removed Aug 2026), so the panel is mounted on the OPPORTUNITY detail page and needs them
there; pinned by `test_the_opportunity_detail_carries_the_headcount`. F-V2: `PositionsCell` in the
Opportunities list ("open / total" + a "Change" badge), and `crm/components/PositionsPanel.tsx` — ONE
shared panel mounted on **both** the requirement page and the opportunity page (open/filled/total, amber
pending banner with Approve/Reject for RMG/Admin and Withdraw for the requester, history) with
`PositionChangeModal` / `PositionDecisionModal`, plus `PendingPositionChangesPanel` (list-page only)
above RMG's Engineering Review Queue. ⚠️ **Positions do NOT use `ensure_visible`** —
`requirement_positions._visible_requirement` scopes them the way the OPPORTUNITY is scoped, because that
is the page they are reached from: `GET /api/opportunities` is not creator-scoped, so every Sales user
opens every deal, while requirement visibility scopes Sales to `created_by` — a Sales user looking at a
colleague's opportunity got "Requirement not found" (reported 21 Sep 2026). Now Sales / Sales_Head / RMG /
Admin all pass and TA stays limited to `TA_VISIBLE_STATUSES`. **The word "requirement" never appears in
user-facing copy from this router** — Sales has no Requirements page, so every message names the
opportunity; `requirement_label` is imported here as `opportunity_label` (it returns the parent opp_id,
never REQ-xxxx) and `test_no_user_facing_message_says_requirement` scans every `detail=` string, judging
the rendered text with `{…}` interpolations stripped.
⚠️ **An approved change also writes back to the OPPORTUNITY** (`_sync_opportunity_positions`, reported
21 Sep 2026): `details.tm_positions_count` ("Positions (Count)" in Time & Material Details) is the SAME
fact — it is what seeds `no_of_positions` at spawn — so the opportunity page was showing 1 and 2 for one
question. It now follows the approved count (full dict reassignment; SQLAlchemy does not track in-place
JSONB edits), and because **RFI Value = annual revenue × period/12 × positions is strictly LINEAR in the
count**, a set `rfi_value` is SCALED by `to/from` (a 1 → 2 exactly doubles the deal). Scaling, not
recomputing, preserves a figure Sales adjusted by hand; a blank RFI stays blank, and both moves are
appended to the `POSITIONS_CHANGED` activity comment so a commercial number never changes silently.
Admin/CEO direct changes sync identically; a rejection touches nothing.

**21 Sep 2026 — Revenue report v3: three zooms, two filters, people-level P&L
(`tests/test_revenue_report.py` now 27):** `services/revenue_report.py` gained a `Period`
(`month | quarter | fy`) beside the existing `Month`. **The `month` parameter is the ANCHOR at every
zoom** — `?month=2026-09&period=quarter` is the quarter holding Sep 2026 — so every `?month=` link
already in circulation (the `revenue_month_close` email, the dashboard tile) keeps resolving and the UI
needs one date control. Quarters follow the **Indian FY** (`FY_START_MONTH`=4: Q1 Apr–Jun … Q4 Jan–Mar,
so Jan–Mar belongs to the FY that started the PREVIOUS April). `SERIES_BY_PERIOD` makes the trend
adaptive (12 months · 8 quarters · 5 FYs) and `Period.previous` / `.year_ago` give like-for-like
MoM/QoQ/YoY. Every section now takes the Period: `_in(period, rows)` replaced the old
`_month_key(...) == month.key` filter, `_overlap_fraction` takes anything with `.start`/`.end`, and a
period costs `CTC / 12 × period.months × frac` — **the `/12` must be multiplied by the months or a
quarter's margin is 3× too flattering**. Rates that only mean something per month
(`pipeline.avg_monthly_billed`, the FY projection's trailing average, `efficiency.revenue_per_head_per_month`)
divide by `period.months`. **Targets stay stored PER MONTH**: a quarter/FY target is the sum of its
months (`months_with_target` says how many resolved), so the three zooms can never disagree and the
Targets modal never needs to know the zoom — it names the ANCHOR month, not the period.
`_forecast` is always the next 3 MONTHS whatever the zoom (an operational horizon, not a reporting
granularity). **Filters `customer_id` / `project_id` go into SQL** (`_invoice_filters`, threaded through
`_invoices_in_window` / `_payments_in_window` / `_open_invoices` / `_pipeline` / `_leakage` /
`_collections` / `_deployed_rows`), not a post-filter — `_open_invoices` and `_leakage` read rows the
window query never sees, so a Python filter would have left ageing and leakage unfiltered. Payload adds
`filters.options` (only customers/projects that actually BILLED in the window, projects carrying
`customer_id` so the UI can chain the dropdowns). New sections: **`by_project`** (billed · share · change ·
heads · cost · margin) and **`by_employee`** — revenue per person via `Invoice.timesheet_id →
timesheets.employee_id`, which is why `_invoices_in_window` now outerjoins Timesheet + Employee. A manual
invoice names nobody, so it is reported as `unlinked_billed` + `coverage_pct` and NOT hidden; deployed
heads that billed nothing are `idle_heads`/`idle_cost` (the bench list, alert at `IDLE_HEADS_WARN`=3),
and loss-making projects raise their own alert. `GET /api/reports/revenue` and
`…/revenue/export.xlsx` both take `period` / `customer_id` / `project_id` (still `role_required()` =
Admin/CEO only), so the workbook is always the slice on screen; the export gains **By project** and
**By employee** sheets and its filename is `period_key` (`2026-Q2`, `FY2026`). F-V2: `PeriodSwitcher`
segmented control, `FilterBar` (project list scoped to the chosen customer; picking a customer drops a
project belonging to someone else), ‹ › step by `meta.months`, the two new tables, and every "this
month" string is now period-aware.

**21 Sep 2026 — filter dropdowns list EVERYTHING + cash-flow forecast
(`tests/test_revenue_report.py` now 31):** (1) `_filter_options` used to return only customers and
projects that had BILLED in the window; on a quiet month the CEO opened "All customers" and saw two
names out of a book of dozens and read it as a bug (reported 21 Sep 2026). It now returns **every**
customer and project with a `billed` flag and an `active` flag, so nothing is hidden and the UI groups
them ("Billed in this period" / "No billing"). This is also the right answer on the merits — "why did
this account bill nothing?" is precisely what a revenue page should be able to show, so a quiet account
must stay selectable. (2) **`_cashflow`** — DSO says how slowly we collect; this says WHEN the money
lands. Open invoice balances are placed on the calendar by `_effective_due` (due_date, else
`invoice_date + DEFAULT_TERMS_DAYS` — the SAME rule the ageing buckets use, so the two can never
disagree) across the next `CASHFLOW_MONTHS`=3 calendar months, next to that month's people cost
(`CTC/12 × overlap`). Every invoice carries two dates: `expected` on agreed terms and
`expected_at_pace` shifted by **`_slip_days`** — the amount-weighted days-past-due of the last year's
receipts, clamped to `0..MAX_SLIP_DAYS`(120). ⚠️ **Weighted by AMOUNT on purpose**: ₹1 L paid 40 days
late against ₹8.9 L paid on time is 4 days of slip, not 40 — pinned by
`test_cashflow_uses_the_pace_we_actually_get_paid_at`. Clamped at 0 because paying early is a favour,
not a plan. ⚠️ **`_cashflow` is always as of TODAY**, even when the page shows a past period — a
forecast of a closed month is not a forecast; the UI labels it. An overdue invoice still lands in the
current month's bucket rather than dropping out of the forecast. Also reports `overdue` (chase first),
`unbilled_ready` (approved timesheets with no invoice — the cheapest lever), `beyond_horizon`,
`top_expected` and `heads_without_ctc` (the cost is understated, never silently). New alerts:
`cash_shortfall` (**bad** — cash is the one number that ends a company, so it outranks a soft month)
and `slow_payers`. Export gains **Cash flow** + **Expected inflows** sheets and four Summary rows.
The section honours `customer_id`/`project_id` like every other.

**21 Sep 2026 — Revenue page redesign (F-V2 only, no server change):** the CEO reported the page
"glitching"; two faults were real CSS bugs — `inputCls` ends in `w-full` so an appended `w-40` never
applied, and an alpha modifier on a `var()` token (`bg-surface-2/60`) compiles to no rule at all. Both
traps and the new header/alerts/table layout are written up in `F-V2 CLAUDE.md`.

**22 Sep 2026 — opportunity stage now cascades to the requirement + notifies
(`tests/test_opportunity_stage_cascade.py`, 18):** reported 21 Sep — Sales closed
"Senior non-AUTOSAR engineer" as Closed_Won ("Ganesh T Selected") and **TA kept sourcing it**.
`stage_transition` moved the opportunity, hid the candidate profiles, logged and committed — and
**never touched the child Requirement**, which stayed `In_Progress`, i.e. inside the set TA works
from. ⚠️ Compounding it: **TA's "Opportunities" nav does not render the opportunity list at all** —
`OpportunitiesWorkspace` gives TA no Pipeline/Applicants tab (`PIPELINE_TAB_ROLES` /
`APPLICANTS_TAB_ROLES` are Admin/Sales/Sales_Head) and falls through to `RequirementsListPage`, so
what TA calls an opportunity IS its requirement. Hiding the profiles could never have fixed this.
`services/opportunities.py` gains the declarative map + a PURE decision function
(`requirement_status_for_stage(stage, current, held_from)` — no DB, every branch testable) and
`cascade_stage_to_requirements()`. **Deliberately differentiated, not one blanket "Closed"**:
Closed_Won / Closed_Partial → `Closed` (here Won means the role was FILLED, so sourcing stops just
like a loss — the difference is why, not whether), Closed_Lost / Rejected / Archived → `Cancelled`,
On_Hold / Sales_Hold → `On_Hold` **reusing `held_from_status`** (RMG's manual hold, 25 Aug 2026) so
Reactivate lands on the EXACT prior status, Active → restore. Guard rails, each pinned:
`CASCADE_PROTECTED_STATUSES` (pre-sourcing + `Fulfilled` + already-terminal) is never rewritten — a
deal that never reached sourcing must not fabricate a "Closed" requirement, and `Fulfilled` is a fact
that already happened; re-holding an already-held requirement is a **no-op** (it would overwrite
`held_from_status` with "On_Hold" and strand the resume path); Reactivate only resurrects a HELD
requirement, never a Cancelled one. The loop handles EVERY requirement on the deal, not just the
first. Notifications: three events — `opportunity.stage_closed` / `_held` / `_reactivated`, all in
`email_flows.EVENTS` so wording and routing stay admin-editable — to **TA + RMG + Sales_Head + Admin
+ CEO** (user decision), actor excluded, `dedupe_prefix=f"opp_stage:{id}:{stage}"` so a double-click
cannot double-mail while a later move to a different stage still sends.
⚠️ **`TA_VISIBLE_STATUSES` was split** (`services/requirements.py`): `TA_LIVE_STATUSES` is the default
queue (Open_For_Sourcing · Posted_On_Portals · In_Progress · On_Hold · Fulfilled) and
`TA_ARCHIVE_STATUSES` (Closed · Cancelled) is reachable ONLY when the request names one —
`apply_visibility(stmt, user, requested_status)` takes the status for exactly this. `TA_VISIBLE_STATUSES`
is now the union and is what `ensure_visible` uses, so the bell link in the new mail opens a closed
requirement instead of 404-ing. One query either way: the alternative (client asks per live status)
would fan out five requests just to exclude two. F-V2: `TA_FILTER_STATUSES` gains Closed + Cancelled.

**22 Sep 2026 (later) — ONE status across roles + TA stage tabs
(`tests/test_opportunity_stage_cascade.py` now 23):** two follow-ups to the cascade above.
(1) **Sales set "Close Lost" and TA's screen said "Cancelled"** — two vocabularies for one fact,
because TA's page shows the REQUIREMENT and the cascade maps Lost → Cancelled.
`services/requirements.py` gains `display_status_for(req_status, opp_stage)` (PURE, no DB) +
`STAGE_OVERRIDES_REQUIREMENT_BADGE`: when the DEAL is settled or parked (the two holds, the three
closes, Rejected, Archived) every role badges the **Sales wording**; while it is live (New/Active)
the sourcing status still wins, because "Active" would not tell TA whether they can source yet —
Open_For_Sourcing vs Pending_Engineering_Review is their whole day. `serialize_requirement` now emits
`opportunity_stage` + `display_status` alongside the untouched internal `status`
(`selectinload(Requirement.opportunity)` was already on the list query, so no N+1).
(2) ⚠️ **The previous build's TA scoping was wrong and is reverted.** Narrowing TA inside
`apply_visibility` made "All statuses" quietly not mean all — the user reported exactly that.
`apply_visibility` is back to the full `TA_VISIBLE_STATUSES`; **scoping now lives in the TAB**, where
the user can see and change it. `TA_LIVE_STATUSES` / `TA_ARCHIVE_STATUSES` remain as documentation of
the split. `GET /api/requirements` gains `opportunity_stage` — **CSV**, because the "Closed" tab is
three stages and must stay ONE request (unlike `status`, which is single and forces the client's
`Promise.all` fan-out). Validated against `_STAGE_VALUES` per CSV member, not just the first.
F-V2: `TA_STAGE_TABS` mirrors the Sales Opportunities strip (Active · Customer Hold · Sales Hold ·
Closed · Rejected · Archived · All), `taMode` is now identified by WHICH strip the user got rather
than by its absence, and the status dropdown is relabelled "All sourcing statuses" — it narrows
within a tab instead of pretending to be the whole scope.

**22 Sep 2026 — HR's Workflow section reaches the Employees record, migration 0105, head is now
0105 (`tests/test_joined_employee_sync.py`, 13):** asked whether the Joined → Employee sync already
happened. It DID — `ensure_employee_for_joined_profile` finds the existing employee by the **Emp ID**
HR typed and `_sync_employee_from_joined_profile` UPDATES it (never a second row) — but only for
fields that had a column, so Offer Letter Reference, Customer Onboarding Date, Relocation and the
Resignation Certificate stayed stranded on the candidate profile. 0105 adds those four to `employees`
plus **`created_at`/`updated_at`** — the table predated `TimestampMixin` and had neither, which is why
the list could only ever sort by `date_of_joining`. `Employee` now carries `TimestampMixin`.
The sync is **declarative**: `_PROFILE_TO_EMPLOYEE` is a (profile field → employee column) tuple, so
adding a field is one line — the old wall of `if x: emp.y = x` is exactly why four fields were never
copied. ⚠️ **Only fields HR actually filled are written**: this runs against a LIVE employee who may
hold better data than the profile, so a blank means "not captured", never "erase it".
`_KEEP_FALSE` carves out the fields where `False` is a real answer (`relocation_applicable`,
`total_experience_years`) — a truthiness check would silently drop an explicit "no".
⚠️ **`karnex_onboarding_date` → `date_of_joining`, NEVER `customer_onboarding_date`**: DOJ drives
payroll and leave accrual, and taking the customer's value would rewrite four years of service for an
internal employee placed today (pinned). `work_location` is resolved from the OPPORTUNITY's
`tm_work_location` (via `_opportunity_location`), not from the profile — the "Customer Location" HR
sees is read-only and has no profile column. Emp ID and official email keep their UNIQUE clash guards;
the old address moves to `personal_email`. ⚠️ **The docstring said `profile_type EXTERNAL` and
contradicted the code** (`INTERNAL` since the 2 Sep 2026 decision) — corrected.
`GET /api/employees` gains `sort_by` through an `_EMPLOYEE_SORTS` map (`date_of_joining` **stays the
default** so the list does not move under anyone; + `recently_updated`, `name`), 400 on an unknown key.
`serialize_employee` exposes the four new fields + `updated_at`.
**Project history enriched**: `serialize_project_history` takes a `context` dict from the new
`project_history_context(db, rows, employee_id)` — customer, opportunity id/title, `positions_total`
(what the deal was sourcing for), billing rate/unit, onboarding/exit, `is_current`. **THREE batched
queries for the whole list** (projects · opportunities+requirement · project-employees), never per row
— a long-serving employee's history page would otherwise be an N+1; pinned by a source scan. Null-safe
for a project with no opportunity (`projects.opportunity_id` is NULLABLE since 0069).
F-V2: the Project History tab now shows Project+customer, Opportunity (linked), Position held,
Headcount, Rate and an "Ongoing" badge; the Employees list gains the sort control.

**22 Sep 2026 — AI interview: why it died after the warm-up, plus session recording
(`tests/test_session_recording.py` 15, `tests/test_interview_not_attempted.py` 8):**
reported after the September deploy — "after *Please introduce yourself.* the interview closes with
an error". The SERVER path is clean (reproduced a real warm-up turn: answer saved, index advanced,
next question served). Three client-side causes, all shipped 15–16 Sep:
⚠️ **(1) `interview_security.js` terminated at `TERMINATE_AT = 3` while the server terminates only
past `MAX_WARNINGS` (the 4th strike, `main.py::interview_violation`).** The browser was ending
interviews the server allowed, on the strike that should have been the THIRD WARNING — the candidate
saw "Warning 2 of 3" then a red "Interview Ended". Combined with the 15 Sep widening of
`STRIKE_TYPES` (`window_blur`, `visibility_hidden`, `fullscreen_exit` all became strikes), three
ordinary focus events killed an interview a minute in. `TERMINATE_AT` is now `MAX_WARNINGS + 1` and
is only the OFFLINE fallback — the server's `auto_terminated` was already honoured and is the
authority. `showWarning` now renders a real third ("Final warning") instead of stopping at 2.
⚠️ **(2) The clock is wall-clock from `interview_started_epoch` and `mark_interview_started` is
idempotent by design**, so a candidate who opens the link, sees the warm-up and returns later is past
the limit on reconnect: `/next` returns "Interview completed" and the client submits. That is the
designed behaviour; the HARM was the report. `services/interview_outcome.py` (pure, no I/O) now
separates **"did badly" from "never happened"**: `scored_answer_count` ignores warm-up indices,
blanks and skips, and `apply_not_attempted` rewrites `recommendation` → **"Not Attempted"** /
`overall_fitment` → "Not Assessed" + `verdict_suppressed_reason="no_scored_answers"` when it is 0.
⚠️ **Skipping every question is still an ATTEMPT** and keeps its Reject — refusing to answer is real
signal; only "we never asked" is exempt. Applied in `hr/service.build_report_record`, the ONE funnel
every report passes through (fast path · AI upgrade · both recovery paths), so it needs no other hook.
The numeric scores are deliberately left at 0 — they honestly describe an empty transcript.
⚠️ **(3) `core.js::handleJson` throws on ANY `{"error"}` body, even on HTTP 200**, and several
handlers return exactly that (`/next` "No active session", `/answer` "Interview already completed",
the device-binding 403). The client prints `Error: …` and stops dead. Also fixed: the
premature-completion guard at `candidate.js:1353` was **dead code** (`idx < total` can never be true —
the completed payload sets `index === total`); it now branches on `completion_reason ===
"questions_exhausted"` and retries ONCE (`_premCompletionRetried`), which works because `/next` tops
the rolling pool up before deciding.

**22 Sep 2026 — whole-session recording + S3 (no migration; legacy `interview_schedule` columns):**
`services/media_storage.py` is the ONE driver interface — `S3Storage` (boto3, presigned GET so bytes
never pass through the app server and no bucket is public) and `LocalStorage` (dev / `start_app.bat`
/ the fallback). Chosen once at first use from `MEDIA_STORAGE_BACKEND` (`s3|local|auto`) +
`MEDIA_S3_BUCKET`/`AWS_S3_BUCKET`, `MEDIA_S3_PREFIX` (default `karnex/interviews`), `MEDIA_S3_REGION`,
`MEDIA_S3_STORAGE_CLASS`, `MEDIA_URL_TTL_S`. ⚠️ **A bad bucket degrades to local, never raises** — an
ops mistake must not end a live interview; `storage_health()` says which driver is in force.
`services/interview_recording.py`: the candidate's browser records with `MediaRecorder` and POSTs a
~15 s chunk to `POST /interview/recording/chunk`; each chunk is its own object under `parts/`, and
`finalize_from_parts` joins them in SEQUENCE order (zero-padded `%06d`, so chunk 10 cannot sort
between 1 and 2) into `recordings/<token>/session.webm`. ⚠️ **Chunked because the interviews worth
watching are the ones that ended badly** — a single upload at the end would lose every crash,
termination and closed laptop. Finalize is idempotent and runs from THREE places: the client's
`/recording/complete`, both submit paths (`_finalize_session_recording`), and on demand when the
Integrity detail opens — so a crashed interview still yields a playable file. `discard_parts` only
runs once a final object exists. Every failure path answers 200/`{"status": …}`: recording is
evidence, the interview is the product. `interview_schedule` gains `recording_key / recording_bytes /
recording_mime / recording_status` (both dialects, `_ensure_schedule_security_columns_*`) — **key
only, never the bytes**; a ~22 MB blob would bloat every backup and the Settings ▸ Backup ZIP.
Routes: `…/chunk`, `…/complete`, `GET /interview/recording/{token}` (HR), `GET
/interview/recording-config` (⚠️ deliberately NOT `/interview/recording/config` — a separate path
cannot be shadowed by the parametric sibling whatever the declaration order), `GET
/interview/media/{key:path}` (local driver only). **Size: 320x240 @ 6 fps, VP8 52 kbps + mono Opus
12 kbps ≈ 22 MB per 45-minute interview** (~4.4 GB/month at 200 interviews ≈ $0.10 in S3); pinned by
`test_the_defaults_stay_small_enough_to_be_affordable`, and every knob is an env var so it tunes
without a frontend build. ~~Integrity snapshots moved to the same storage~~ — **snapshots were REMOVED
on 23 Sep 2026** (see the note below); the recording is the evidence. `boto3>=1.34.0` added to
`requirements.txt`; it is imported lazily so an existing deployment without it keeps working on the
local driver.

**23 Sep 2026 — LIVE interview view + camera snapshots removed (`tests/test_session_recording.py` now 20):**
"we have started recording — can we watch live, or only after?" It was after-only; now both. The parts the
candidate's browser already uploads every ~15 s ARE the stream: `interview_recording.live_manifest(token,
after_seq)` lists the chunks a viewer has not seen (`{live, finalized, parts:[{seq,bytes}], next_after,
chunk_seconds, session_status}`) and `part_bytes(token, seq)` reads one. Routes `GET /interview/recording/
{token}/live?after=N` and `GET …/{token}/part/{seq}` (`_integrity_auth`, i.e. Integrity Logs roles) — three
segments, so the two-segment `/{invite_token}` can never shadow them. ⚠️ **Parts are served THROUGH the app for
both drivers, never presigned**: the viewer appends them with `fetch()` + MediaSource, and a cross-origin fetch
from S3 would need bucket CORS for every dashboard origin; ~120 KB per 15 s per viewer is nothing, and the
FINAL file still streams straight from S3. ⚠️ **A live session is never finalized on demand**:
`_recording_detail(token, session_status)` / `interview_recording_playback` return `{available: False, live:
True}` while `is_live_session(status)` (`LIVE_SESSION_STATUSES` = pending · verified · active · scheduled) —
finalize is followed by `discard_parts`, which would delete the HEADER chunk from under the uploading browser
and make the final rebuild start mid-stream (unplayable). Pinned by `test_a_live_session_is_never_finalized_
on_demand`. The chunk route stamps `recording_status="recording"` on seq 0 (so the list shows "Watch live"
off the schedule row, never off storage); `_finalize_session_recording` overwrites it with ready/missing.
**Camera snapshots are gone**: `_store_integrity_evidence`, the `evidence` upload on `POST /interview/violation`,
`GET /interview/integrity-evidence/…`, `has_evidence` / `evidence_url`, the `snapshot_*` config keys and the
`.gitignore` entry — a still frame answered nothing the recording does not (the event's timestamp is the seek
position). Snapshots already in storage under `snapshots/` are orphaned; delete the prefix when convenient.
F-V2: `IntegrityLogs.tsx` `LiveRecordingPlayer` (MSE `video/webm; codecs="vp8,opus"` sequential append; Blob
rebuild fallback that keeps the playhead; "Jump to live"; polls every `chunk_seconds/2`, hands over to the
ordinary player 3 s after the manifest says the session ended), red "Watch live" chip on the row, list auto-
refresh every 30 s only while a session is live; the lightbox / `EvidenceImage` / `face_detection.captureEvidence`
/ `interview_security` `evidence` field removed; `index.html` `app.js?v=32`.

**22 Sep 2026 — camera is MANDATORY (`device_test.js`, F-V2):** one switch, `CAMERA_REQUIRED = true`.
It was optional so camera-less candidates could sit the interview; that is no longer acceptable — an
AI interview with no video cannot be proctored or recorded. `WEBCAM_PASS_STATES` is now `{"ok"}`,
`_markWebcamSkipped` became `_markWebcamUnavailable` (blocking error + retry, not a silent pass), the
"Skip — no camera" button is hidden and `_skipWebcam` refuses, and the tile says the interview is
recorded. Set the constant to false and the previous optional behaviour returns intact. Everything
else about the gate — mic script, chime, network check, `_verifiedMicStream` handoff — is unchanged.

**23 Sep 2026 — warm-up time limit (`tests/test_warmup_time_limit.py`, 9):** `utils/warmup.warmup_time_limit_sec()`
reads `INTERVIEW_WARMUP_TIME_LIMIT_SEC` (default **60**, clamped 15..600, `0` = off) and `next_question_payload` emits it as
`warmup_time_limit_sec` on the warm-up payload ONLY. The client counts it down on the question and moves on at zero (F-V2
`CLAUDE.md`). No server-side enforcement on purpose: the warm-up is unscored, so the clock is a courtesy to the interview's
overall time limit, which `/answer` already enforces.

**23 Sep 2026 — AI verdict inline on the Interviews tab (`tests/test_ai_interview_summary.py`, 12):**
`services/ai_interview_summary.summarize_interview_record(record)` is PURE and boils an `interview_records` payload down to
one stable shape (overall/technical/communication/problem-solving on 0–100, recommendation, fitment, summary, ≤4
strengths / improvements, ≤8 skill scores, question counts, terminated / not-attempted flags). ⚠️ Precedence mirrors
`CandidateReportPage.tsx` — `score_reasons.<dim>` → `scoring_summary` → flat field — so the card and the report page
never disagree on a number; 0–10 inputs are ×10, `communication_required: false` yields None. `GET
/api/candidate-profiles/{id}/ai-interviews/{link_id}/summary` (same `gated_read("profiles")` as the list; ONE legacy read;
falls back to the link's own `overall_score_percent` when the record lags the background upgrade). Declared BEFORE the bare
`/{link_id}` PUT/DELETE (pinned). F-V2: `crm/components/AiInterviewOverview.tsx` mounted inside the AI card in
`Profiles.tsx`'s Interviews tab. **"Full report" was never broken** — `/admin/?view=candidateReport&cid=<email>&iid=<record>`
resolves through `App.readInitialView` → `/hr/candidates/{email}/interviews/{id}` — but it needs the Interview Platform's
Reports tab (`iv:candidates`: TA/HR/RMG/Admin); a Sales/Finance login was bounced silently to the landing page. The card now
hides the link for those roles ("Full report: ask RMG or TA") and, more to the point, they no longer need it.

**23 Sep 2026 — every stage tab 500'd (`tests/test_opportunity_stage_cascade.py` now 25,
`tests/test_no_shadowed_imports.py`):** `GET /api/requirements?opportunity_stage=New,Active` raised
`UnboundLocalError: Opportunity` for EVERY role. `list_requirements` used the module-level `Opportunity` in the
stage branch, but the older search branch further down did `from models import Opportunity` — Python makes that
name a LOCAL for the whole function, so the earlier read blew up. pyflakes does not report this shape and the
22 Sep tests only exercised `apply_visibility`, never the handler. Fixed (inner import removed; stage + search now
join the opportunity ONCE), pinned by calling the handler the way FastAPI does. ⚠️ **The same shape sat in
`resumes.upload_resume`** — the duplicate-upload branch called `find_or_create_candidate_from_resume` before the
try-block below re-imported it (inside a swallowed `try`, so the re-uploaded resume silently got no candidate);
fixed. `tests/test_no_shadowed_imports.py` now walks the AST of every production module and fails on
"name read before its inner import" — **never re-import a module-level name inside a function body.**

**23 Sep 2026 — custom roles + admin password reset, migration 0106, head is now 0106
(`tests/test_custom_roles.py`, 19):** "Admin/CEO can create a new role from Access Control, pick its tab
permissions, and reset anyone's password." The eight built-in roles are a native Postgres enum (`role_name`) baked
into every gate, so a new job title is NOT added there. A **custom role is data**: `models/custom_roles.py`
(`custom_roles`: name UNIQUE ≤40, description, is_active, `tab_access`/`field_access` — the SAME grant map as an
Access Template; `user_custom_roles` membership, CASCADE on role delete). It takes effect through the two places
every CRM request already passes and **no gate changed**: ⚠️ `crm_deps.get_current_user` unions the user's ACTIVE
custom-role NAMES into `CurrentUser.roles` (`custom_role_names`, never raises — a pre-0106 DB must not lock logins
out), so a user whose only role is "GM" is not "No CRM role assigned"; and `access_templates.effective_access`
resolves the grants from the custom roles when no explicit template is assigned (`custom_role_grants`: several roles
MERGE with the higher rung winning per tab; `source="custom_role"`; an explicit template still outranks them; the
per-user override still layers on top). Consequences to remember: a GM with `invoices: edit` passes
`gated_read/gated_write("invoices", "Finance")` and fails `gated_create` (edit < create); **`role_required("Finance")`
endpoints stay closed** to custom roles, exactly as for templates (pinned). Built-in names (and Superadmin) are
RESERVED; names are unique case-insensitively; a role must grant ≥1 tab; unknown tab keys are dropped like templates
do. `routers/crm/custom_roles.py` → `/api/roles` (GET built-in + custom with member counts · POST · PUT/DELETE
`/{id}` (409 while members remain) · GET/PUT `/{id}/members` replace-list, unknown ids refused), all `role_required()`
= Admin/CEO. `services/users_admin.reset_password` + `POST /api/users/{id}/reset-password {new_password?}`: blank
→ `generate_temporary_password()` (policy-passing, RETURNED ONCE, never stored in clear or logged); explicit value
goes through `password_hashing.validate_password`; **an admin cannot reset their own account here** (400 — that is
Change password). Writes via `auth_db.update_user_password` (the same setter the self-service reset uses).
`GET /api/users` rows carry `custom_roles` (ONE batched query). Tables added to `data_backup.DATASETS["users"]`.
F-V2: Access Control gains a **Roles** tab (`crm/pages/RolesAdmin.tsx`), `crm/components/TabPermissionMatrix.tsx`
(the module-grouped View/Edit/Create picker, extracted so roles and templates share one vocabulary; registry from
`/api/access-templates/registry`), `crm/components/ResetPasswordModal.tsx` (Generate → shown once with Copy · Set),
mounted on the role's Members modal AND on every Users row ("Reset Password", disabled for yourself); custom roles
show as green chips beside built-in ones. A GM login lands on the CRM (`hasCrmAccess` = any role) and the sidebar /
page gates follow `me.access.visible_tabs` as for any templated user. Deploy: `alembic upgrade head`.

**23 Sep 2026 (later) — ONE source of access per user (`tests/test_custom_roles.py` now 22):** reported —
a user in the custom role "Sales Manager" still showed "Default — Sales" in the Users-tab dropdown, and that template
silently outranked the role. A template and a custom role are now **exclusive**: `custom_roles.set_members` clears
`UserProfile.access_template_id` for every user it ADDS, `access_templates.assign_template` calls
`remove_user_from_all_roles` when a template is chosen, and the dropdown has ONE endpoint —
`POST /api/users/{id}/access-source {kind: default|template|role, id}` (`users_admin.set_access_source`): `role` also
drops any other custom role (one role from this control; the Roles tab can still add several), `default` clears both.
F-V2: the column is now **Access** with `<optgroup>`s "Custom roles" / "Access templates" (values `role:ID` /
`template:ID`); the row updates from the response so the chips and the select can never disagree.

**23 Sep 2026 (later still) — Edit Roles dialog lists custom roles (`tests/test_custom_roles.py` now 25):**
reported with a screenshot — the Users-tab "Edit Roles" dialog offered only the eight built-in roles; Sales Manager
and GM were nowhere to pick. `RolesIn.custom_roles: list[int] | None` — a list REPLACES the user's custom-role set
in the same save as `roles`, `None` leaves it alone (older callers unchanged). `custom_roles.set_user_roles(db, uid,
ids)` is the transpose of `set_members` (refuses unknown AND inactive ids; choosing any role clears the template; no
commit) and `user_role_ids(db, uid)`. ⚠️ **Root cause of Balasaheb's "Default — Sales" found here**:
`users_admin._auto_assign_role_template` runs on EVERY Edit Roles save and attaches the ACTIVE template tagged with
the role — it knew nothing about custom roles, so it re-attached the Sales template over "Sales Manager" each time
and the template then outranked the role. It now returns early when the user holds any custom role (a custom role IS
the explicit configuration), pinned by `test_role_defaults_never_reattach_a_template_over_a_custom_role`. F-V2:
`RoleSelector` takes `customRoles / selectedCustom / onCustomChange` and renders a green "Custom roles" group after
the built-in ones (inactive roles shown only when already held); `EditRolesModal` fetches `/api/roles` once, maps the
row's role NAMES to ids, and sends `custom_roles` only when that fetch succeeded — a failed fetch must not strip
roles the user already has. Create/Invite still offer built-in roles only.

**23 Sep 2026 — full suite on the dev box (Python 3.14, Windows): 1332 pass, the 10 failures were all
test-side and are fixed:** `test_candidate_speech_endpoints` (6) used `asyncio.get_event_loop().run_until_complete`,
which no longer creates a loop outside a running one on 3.12+ (RuntimeError on 3.14) → `asyncio.run`. Two stale pins
rewritten to the current contract: `test_ai_l1_can_only_be_triggered_by_ta` → `…_by_recruiting_roles_only`
(`TRIGGER_ROLES == {TA, RMG}` since the 15 Sep RMG desk) and `test_no_other_stage_has_a_hidden_precondition`
(`ENTRY_REQUIREMENTS` = Customer_Approval + Joined since 2 Sep). `test_requirement_positions::test_the_routes_are_
actually_reachable_on_the_app` now accepts **401 or 503** — with a CRM URL in the environment the bearer check answers
before `get_crm_db`; both prove the route resolved. The flaky `test_followup_fallback_adapts_to_answer_strength` pins
`random.shuffle` (the "parallel execution" prompt sits in both pools). §9's known set is therefore down to the five
pre-existing `test_boundary_question_finalize` / `test_password_security` / `test_timesheet_entry_grid` items.

**23 Sep 2026 — Proforma → Tax invoice flow, migration 0107, head is now 0107
(`tests/test_proforma_flow.py`, 14; `test_midmonth_timesheet` + `test_rmg_timesheet_reports` re-pinned):**
the business asked that the person who fills a timesheet no longer approves and invoices it. **Sales fills + submits
→ GM (custom role) verifies, approves and raises a PROFORMA → Finance reviews / corrects and either GENERATES the
original invoice or RETURNS it with a reason → the GM reissues → the Sales Manager is told when the original
exists.** `services/proforma.py` is the lifecycle module. Data (0107): `invoices.kind` (String — `Proforma` | `Tax`,
`InvoiceKind`; existing rows are `Tax`), `proforma_number` (the PI number survives conversion — it is the customer's
reference), `invoice_format` JSONB, `returned_reason/at/by`; `customer_billing_policies.invoice_format`.
⚠️ **Money only moves at conversion**: `services.finance.consume_po_for_invoice` is the ONE PO-drawdown (balance
re-checked at that moment — other invoices may have drawn since), `require_tax_invoice` refuses payments / TDS /
credit notes / change requests on a Proforma, and both delete paths (`crm_delete`) skip the PO reversal for one.
`POST /api/timesheets/{id}/generate-invoice` (action `timesheet.generate_invoice`, default **GM**) now creates the
Proforma (`PI-YYYY-NNN`, no `invoice_number` body field any more; `invoice_format` instead) and REPLACES a RETURNED
Proforma under the same number (`replaceable_proforma`; `can_generate_invoice` / `invoice_ref` in `services/timesheets`
are the one shape every timesheet payload prints). `po-options` returns `invoice_format` (the customer's saved
choice, pre-filling the GM's dialog) + `returned_proforma`. Finance: `POST /api/invoices/{id}/convert
{invoice_number?, invoice_date?}` and `…/return {reason ≥10}` (action `invoice.convert_proforma`, default Finance);
`PUT /invoices/{id}` on a Proforma is Finance-direct (dates, buyer state, `invoice_format`; the PI number is not
editable — the tax number is typed at conversion); `GET /invoices?kind=Proforma|Tax`. Action defaults moved:
`timesheet.approve` / `.reject` → **GM** (Sales deliberately off), `timesheet.submitted` route → GM + CEO.
Events: `invoice.proforma_ready` (Finance), `invoice.proforma_returned` (GM), `invoice.generated` → **Sales Manager**.
⚠️ **Custom roles in notifications**: `roles.name` is a Postgres enum, so comparing "GM" against it RAISES (and
poisons the transaction) — `notify._user_ids_in_role` and `recipients.role_recipients` route a non-built-in name to
the `user_custom_roles` tables instead (`custom_roles.user_ids_in_custom_role`, savepointed); `email_flows` pickers
and validation use `custom_roles.all_role_names(db)` so GM / Sales Manager are routable. **Client-specific format**:
`services/invoice_format.py` (pure) — the three OPTIONAL columns `sac` · `leave` · `per_day`; `tax_invoice.
service_columns(inv)` / `service_cell(...)` are the ONE service-table definition that the HTML, reportlab, Word and
(mirrored) on-screen renderers all build from, so a hidden column is hidden everywhere; `PROFORMA_COLOR` (#c2410c)
titles the PROFORMA INVOICE in orange on every output; files are `Proforma_…`. **Timesheet upload takes any allowed
file** (Sales request): an `.xlsx` is parsed as before; anything else (`bulk-import?year=&month=`) is ATTACHED to that
month's Draft sheet as `import_source` (`_attach_only_import`) — a signed PDF cannot be read into a day grid reliably,
so it is kept as evidence and the grid is filled in the editor. Deploy: `alembic upgrade head`; in Access Control ▸
Roles give **GM** `timesheets: edit` + `invoices: create` (+ projects/customers view) and **Sales Manager** at least
`invoices: view`, then add the people; the existing "Default — …" templates stay as they are.
⚠️ **A saved row in Users ▸ Action Permissions outranks the new code defaults** — the dev box had
`timesheet.approve` saved as RMG + Sales, so RMG could still approve after this change. Reset `timesheet.approve`,
`timesheet.reject` and `timesheet.generate_invoice` to default (or set them to GM) there. Corollary for tests:
`action_permissions.roles_for_action` reads the LIVE Postgres through `crm_db.get_session_factory()`, never the
test's SQLite — any test asserting a gate's default must monkeypatch it (see `test_midmonth_timesheet`).

**23 Sep 2026 — Hiring control tower (`tests/test_hiring_dashboard.py`, 15):** the business sheet asked for
pipeline positions · opportunities · onboardings · positions closed (fulfilled vs closed-by-customer) · active and
workable positions · active customers · target vs actual · hiring-stage delays, on the Dashboard, with gauges.
`services/hiring_dashboard.py` is a PURE aggregation (three batched queries for requirements + Joined counts +
closing timestamps, two for Joined profiles, one for opportunities, two for the delays panel — never per row) over
the `Period` / `Month` classes REUSED from `revenue_report` (same anchor-month + zoom contract, Indian FY quarters).
`GET /api/dashboard/hiring?month=&period=` (`routers/crm/dashboards.py`; default zoom **quarter** because targets
are quarterly) is `gated_read("dashboard")` — every CRM role and every custom role reads the same numbers.
Definitions that must not drift (the docstring is the contract): **pipeline positions** = `no_of_positions` of
requirements CREATED in the period (spawn = Sales Head approval); **onboardings** = Joined profiles dated by the
`STATUS_CHANGE … -> Joined` activity-log timestamp → `karnex_onboarding_date` → `customer_onboarding_date` →
`updated_at` (there is no joined-on column; HR touching the row weeks later must not move the joining);
**positions closed** = `fulfilled` (Joined positions on Fulfilled/Closed requirements) reported BESIDE
`customer_closed` (unfilled positions on Closed/Cancelled) — **never added together**, per the sheet; a requirement
is placed in the period of its closing log row (`FULFILLED` / `CLOSED` / `CANCELLED` / `OPPORTUNITY_STAGE`), else
`updated_at`; **active positions** = open (total − joined) on non-terminal, non-rejected requirements;
**workable positions** = the subset in `WORKABLE_STATUSES` (Open_For_Sourcing · Posted_On_Portals · In_Progress —
engineering-approved and not on hold; ONE tuple to edit when the rule changes); **active customers** = customers
with an approved New/Active opportunity, listed by name with open positions and a concentration flag at ≥ 50 %.
**Targets** are company-level per FY quarter in `app_settings`: `hiring.target.<sales|ta>.<YYYY-Qn>` with a
`…default_quarter` fallback; a month reads a THIRD of its quarter and the FY the SUM of its four quarters, so the
zooms can never disagree. `PUT /api/dashboard/hiring/targets` `{quarter, sales_quarter?, ta_quarter?, sales_default?,
ta_default?, clear:[…]}` is `role_required("Sales_Head")` (Sales Head / Admin / CEO — a management number, not
template-widenable); `parse_quarter_key("2026-Q4")` → Jan–Mar 2027. **Pace** is working-day based (Mon–Fri,
holidays ignored on purpose): `current_per_week` = actual ÷ elapsed working weeks, `required_per_week` = gap ÷
working weeks left (current period only), `acceleration` = required ÷ current, state ok ≤ 1.0 · warn ≤ 1.5 · bad.
**Stage delays** bucket live profiles (Internal L1 = Technical_Screening + RMG_Review · Internal L2 =
Sales_Screening · Customer interview = the four customer stages · Offer = Shortlisted / Customer_Approval /
HR_* · Joining = Preboarding) by days since their last `STATUS_CHANGE` (amber ≥ 3, red ≥ 7). F-V2:
`crm/pages/dashboard/HiringControlTower.tsx` mounted at the top of `CrmDashboard.tsx` (see F-V2 `CLAUDE.md`).

**25 Sep 2026 — approval buttons are their own grant, migration 0108, head is now 0108
(`tests/test_approval_permissions.py`, 25; `test_access_template_authority` re-pinned):** reported with a
screenshot — Sanjana (Sales) filled and submitted a timesheet and was offered Approve / Reject on it. Root cause:
`gated_write_action` let ANY templated user through on a tab Edit grant — the grant Sales needs to FILL the sheet —
and the UI (`useCanAct("timesheets","edit",…)`) mirrored it; the stale saved Action Permissions row (RMG + Sales)
did the rest. The same hole sat under every approval behind `gated_write(tab, Role)` (opportunity / requirement
Sales Head + engineering approval, the Sales Head terms decision, budget resolve). Fix, one rule end to end:
`action_permissions.ACTIONS` entries are now `Action(label, description, roles, kind, group)` NamedTuples;
`kind == APPROVAL` for 15 actions (`APPROVAL_ACTIONS`: timesheet approve/reject/generate_invoice,
invoice.convert_proforma, invoice.revision.approve, credit_note.approve, opportunity.approve,
requirement.sales_head_approve / engineering_approve / positions.approve, profile.rmg_screening /
sales_head_decision / budget_resolve, leave.approve / reject). ⚠️ **`action_permissions.user_may(db, user, action)`
is THE decision**: Admin/CEO always; a templated / custom-role user → that template's / role's **`action_access`**
list alone (a tab grant NEVER implies an approval); otherwise the action's role list (saved row, else default).
`gated_write_action` branches on the kind: an approval needs the tab at **view** (to reach the record) + `user_may`;
a MANAGE action keeps the old "template tab Edit is authoritative" rule, untouched. A call site that names no roles
takes the defaults from the registry (`recalculate` used to say RMG + Sales and `po-options` Finance + RMG for the
same actions — both now registry = GM). `/api/me` gains **`approvals`** (every approval the user may do, from
`allowed_approvals` = the same function), and the `meta.can_approve` of invoice revisions / position requests now
comes from `user_may` too. Data (0108): `access_templates.action_access` + `custom_roles.action_access` (JSON list).
**NULL = never configured**: a template falls back to the role list; a custom role resolves to the approvals whose
default names it (a "GM" role approves what GM approves, no backfill). 0108 backfills every role-TAGGED template
from a frozen snapshot of the defaults ("Default — Finance" converts Proformas, "Default — Sales" gets NO
timesheet approval; pinned against the registry by `test_the_0108_snapshot_matches_the_registry`) and **deletes the
saved Action Permissions rows for the three timesheet actions** (the stale RMG + Sales row). A new template starts
from its role tag's defaults; unknown keys are dropped (`clean_action_list`). `GET /api/access-templates/registry`
adds `approvals` (the editor catalogue) and **`role_tags`** (built-in operational roles + ACTIVE custom roles — the
Role tag dropdown did not offer GM / Sales Manager); the template role tag can now be CLEARED (update used to skip
None). `POST /api/users` and `/api/users/invite` take `custom_roles: [ids]` (validated BEFORE the legacy row is
written — `custom_roles.validate_role_ids`, shared with `set_user_roles`), so a GM / Sales Manager is created in one
step; a custom role alone satisfies "at least one role". `test_every_approval_endpoint_is_gated_by_its_approval_action`
source-scans each approval route; `test_every_gated_action_is_registered` fails when a `gated_write_action` key is
missing from ACTIONS (it would silently be treated as MANAGE). Deploy: `alembic upgrade head`, restart; open Access
Control ▸ Access Templates / Roles and check each template's **Approvals** section.

**25 Sep 2026 — projects get a last working day; closing moves the team to the bench, migration 0109, head is now
0109 (`tests/test_project_closure.py`, 14):** reported — an ended project had nowhere to record when it ended and its
people stayed "deployed" forever. `projects.end_date` (the LAST WORKING DAY) + `closure_capped_exits` (JSON) +
`closed_reason / closed_at / closed_by`. `services/project_closure.py` is the one module that knows how a project ends:
`schedule_project_close` (reason ≥ 10; refuses — by name — anyone onboarding after the date; caps every open
assignment's `exit_date` at the end date **straight away**, so timesheet windows, the revenue forecast and the roll-off
radar see the end coming; a day already gone closes at once) → `apply_project_close` (each open assignment through the
SAME `exit_project_employee` a single exit uses — accrual stopped, open sheets flagged for settlement, leave marked —
Project History rows closed, status Completed, `closed_at` stamped; idempotent) ← daily job `project_closures`
(`scheduler.project_closures`, picks `end_date < today AND closed_at IS NULL`, each project in a savepoint).
⚠️ **The end date is the last day WORKED — people are on the bench from the NEXT day** (pinned). ⚠️ A close only ever
CAPS an exit, never extends one; `closure_capped_exits` remembers every date it overwrote so `cancel_project_close`
(scheduled only — 409 once the team has left) restores each one exactly (pinned: a personal later plan survives a
cancelled close). Routes (`routers/crm/projects.py`): `GET /{id}/close-preview?end_date=` (read), `POST /{id}/close`
`{end_date, reason}` and `DELETE /{id}/close` behind `gated_write_action("project.close","projects")` — a new MANAGE
action, defaults Sales_Head · RMG · HR. `_guard_status_change`: editing status to Completed while people are still on
the project → 400 "Use Close project"; Completed → Active/On_Hold reopens and forgets the closure but does NOT put people
back. `_guard_assignment_against_closure`: no assigning on a closed project (409), no onboarding after the end date
(400). Events `project.close_scheduled` / `project.closed` (RMG · HR · Sales_Head · Admin, in `email_flows.EVENTS`,
best-effort in a savepoint — a mail failure never undoes a close). **Bench**: `deployment_by_employee` (ONE query) +
`live_assignment_filter` — deployed = active, not exited, exit date today or later; `GET /api/employees` rows carry
`deployment_status` (Deployed · Bench · null for an inactive person) + `current_projects`, and `?deployment=bench|
deployed` filters in SQL. `dashboards.bench_rolloffs` now ends cover at the EARLIER of the project's last working day
and the latest PO end (`ends_by`), so a closing project always shows on the radar. `project_out` carries `end_date` +
`closure`. Deploy: `alembic upgrade head`, restart.

**25 Sep 2026 — internal vs external placements on the Revenue page (`tests/test_placements_report.py`, 11):**
`Employees ▸ Profile Type` cannot answer this — every joined candidate is created Internal (2 Sep decision). So
`services/placements_report.py` DERIVES it per placement (= one `project_employees` row, dated onboarding → billing
date): **internal** = an earlier placement anywhere (priors are looked up across ALL customers even under a customer
filter — pinned) or a Karnex joining date more than `GRACE_DAYS`=30 before the customer onboarding; **external** =
joined within 30 days of (or after) it with no earlier placement; **unknown** = no joining date and no prior — shown,
never folded in. Undated assignments are counted as `undated`, never guessed. Every row carries its `reason`. Windows:
the page's zoom via `revenue_report.Period` (same anchor-month contract; 12/8/5 buckets) or `date_from/date_to` (weekly
≤ `WEEKLY_MAX_DAYS`=92, monthly beyond, max 3 years) with a same-length previous window for the comparison. Revenue
split: invoices in the window attributed through `timesheet → (employee, project) → placement → label`; manual
invoices = `unattributed`; **Proformas excluded**. ⚠️ **The main revenue report does NOT exclude Proformas yet**
(`_invoices_in_window` / `_open_invoices` have no `kind` filter since 0107) — billed, outstanding and ageing count an
unissued Proforma; decide and fix separately. `GET /api/reports/revenue/placements` (`role_required()` = Admin/CEO,
same as the page); the Excel pack gains **Placements** + **Placement list** sheets (`_placement_sheets`). Suite on
25 Sep: 1,425 pass; `test_public_invoice_qr::test_public_endpoints_need_no_login_and_hide_internal_finance` fails
identically on the untouched tree (empty bank IFSC in the fixture environment) — pre-existing.

**25 Sep 2026 (later) — a template never locks a user out (`tests/test_custom_roles.py` +3):** reported with a
screenshot — Laddagiri got "No CRM role is assigned" after a template was assigned. ⚠️ **A template decides WHICH tabs;
a ROLE (built-in or active custom) is what lets anyone into the CRM** (`_gate` step 2, `CrmApp`'s `!me.roles.length`).
One-source-of-access (`assign_template` → `remove_user_from_all_roles`) removed the user's ONLY role when it was a
custom one. `users_admin.ensure_role_for_template(db, uid, template)` now runs inside `assign_template` BEFORE custom
roles are dropped (so both the Users "Access" dropdown and Access Templates ▸ Assign are covered): a user with a
built-in role → unchanged; none, template tagged with a built-in role → that role is ADDED (`role_added` in the
reply — "Default — Sales" means "for Sales people"); none, only custom roles, no built-in tag → **400** with the fix
spelled out; no role at all + untagged → proceeds (nothing to lose; the Users row flags it). `set_access_source("default")`
refuses for a user with no built-in role, and `replace_roles` refuses to clear EVERY role ("deactivate instead").
`set_access_source` now returns `roles` too. F-V2: Users-tab "No role — cannot open the CRM" chip, the CRM's no-role
screen says whether a template is present and where to fix it, the Assign toast names an added role.
To fix an account already stuck: Access Control ▸ Users ▸ Edit Roles → tick a built-in role (or GM / Sales Manager).

**25 Sep 2026 — rate saved against the wrong unit (`tests/test_rate_unit_fix.py`, 5; no migration):** reported —
Kaluvoi Reddy, Aug 2026, 168 billable hours invoiced at ₹1,414.77. The engine was right for what was saved: the PE was
mapped at ₹1,414.77 with the unit left on the form's DEFAULT "Monthly", so Qty 1 × ₹1,414.77 / Month. Two real defects
made that invisible and unfixable: every mapping form defaulted to Monthly, and **once saved the unit had no editor
anywhere** (every later screen edited the amount only; the old PE PUT, given `billing_unit`, upserted a NEW rate row dated
today and left the history half in each unit). Fixes: `services/project_employees.set_pe_billing_unit` relabels EVERY rate
row, never mints one; `routers/crm/projects._apply_commercial_changes` is the ONE path both PE PUT routes take for
`billing_rate` / `billing_unit`; new `PUT /api/projects/employees/{pe}/billing-unit` behind `write_rates`
(= `gated_write_action("project_employee.rates", …)`, now shared by the three rate routes too). A unit change re-freezes
the assignment's APPROVED sheets that have no live invoice (`services/timesheets.refreeze_uninvoiced_sheets`, logged
`TS_RECALCULATED`); issued invoices are never touched. `freeze_invoice_figures(db, ts, entries, **extra)` is now the ONE
0075 snapshot builder (approve · Recalculate · unit fix). Guard: `project_employee_billing.rate_unit_warning(unit, rate,
per_hour)` (pure; per-hour < `MIN_PLAUSIBLE_HOURLY`=₹100 on a non-hourly unit, or > `MAX_PLAUSIBLE_HOURLY`=₹25,000) →
`line.rate_unit_warning` on the invoice preview (+ `project_employee_id`); `_apply_frozen_figures` carries the live verdict
onto older snapshots. It warns, never blocks. To fix the reported sheet: PE ▸ Commercial Details ▸ Edit rates ▸ "Rates are
priced per" = Per Hour (or Edit Rate ▸ Priced per in the Raise Proforma dialog) → the Aug sheet re-freezes at
168 × ₹1,414.77 = ₹2,37,681.36.

**25 Sep 2026 — timesheet import reads real customer layouts, Excel AND PDF
(`tests/test_timesheet_import_formats.py`, 13; no migration):** reported — a customer sheet failed every day with
"unknown code 'FRIDAY'": the matrix reader took the row straight under the dates as the codes, and this layout has a
WEEKDAY row in between. `services/timesheet_import.py` (pure) is now the one reader: every source becomes a GRID
(`xlsx_grids`, `pdf_grids`) and `read_day_marks(grids, year, month)` tries, in order, **matrix** (`find_matrix`: a
date row, then the best-SCORING code row within `MAX_ROWS_BELOW_DATES`=6 — weekday names never count; aligned by
column, or by sequence for a text line), **token stream** (`find_token_stream`: the same matrix with its layout lost —
pypdf writes a table one cell per line) and **day list** (`find_day_list`: one date per row + code/hours). Date rows may
be real dates or bare day numbers (a strictly consecutive run, completed by the upload's `year`/`month` or the file's
own "Year / Month" header — `header_period`, which replaced the router's copy). Codes + aliases live in `_CODES`
(P/WFH, WO/OFF, H/PH, L/PL/SL/CL/EL, A/LOP, HD/HALF); an hours-only row imports as Present with those hours. Router:
our template (header Date | Hours Worked | Status) keeps its full parser; any other .xlsx and **every PDF** go through
the reader; a PDF with nothing readable (a scan) — and any other file type — is attached to the chosen month as before
(`_attach_only_import(..., note=)`). pdfplumber is used WHEN INSTALLED (better ruled tables) but is NOT a requirement;
pypdf (already one) + the token-stream reader cover text PDFs — pinned by `test_a_pdf_reads_without_pdfplumber`.

**25 Sep 2026 — timesheet submit / reject notifications reach the right people
(`tests/test_timesheet_notifications.py`, 2):** reported — (1) Sales submitted and the GM heard nothing: the notice was
addressed by ROLE NAME ("GM", "CEO" — or whatever a saved Email Flows row said), while the GM's approval came from a
TEMPLATE's Approvals (assigning a template removes custom roles — one source of access). New
`action_permissions.user_ids_who_may(db, action)` returns every active login that `user_may` lets act, through a role
default, a saved role list or a template's / custom role's Approvals (Admin/CEO excluded — they may do everything; the
route decides whether they hear; savepointed, never raises). `notify.notify_roles(..., user_ids=)` adds such people on
top of the route's roles (bell + email, de-duplicated; a DISABLED event stays silent). Submit passes
`user_ids_who_may(db, "timesheet.approve")` and now names who submitted. (2) Reject told only the EMPLOYEE; it now
also tells the SUBMITTER (`_submitter_user_id`: latest `TS_SUBMITTED` log row, else `TS_CREATED`) "X rejected Y's
timesheet for <month>. Reason: …", skipped when they are the rejecter or the employee. Both rejection dedupe keys now
include `submitted_at`, so a resubmitted sheet rejected again with a same-length reason is no longer swallowed.
F-V2: "Fill from file (Excel / PDF)" on an editable timesheet page (same `bulk-import` endpoint, this sheet's
project/employee/year/month).

**25 Sep 2026 — hours worked on a HOLIDAY are kept (`test_timesheet_entry_grid` re-pinned):** reported — some
employees work on holidays and the grid would not take hours. `resolve_entry_fields` forced a calendar holiday's
hours to 0 in two places; it now keeps them (`_holiday_hours`, clamped 0–24) while the day stays `Holiday`. Nothing
else changed: `compute_billables` / `accrue_comp_off` already bill worked holiday hours when Comp Off Billable is on
and credit Comp-Off leave otherwise, exactly like week-off work. F-V2: the Hours input is editable on holiday rows
and the soft hour-cap now applies to every day type.

**25 Sep 2026 — a PO is drawn by the value BEFORE GST, migration 0110, head is now 0110
(`tests/test_po_draw_base_value.py`, 5; `test_invoice_revisions` + `test_proforma_flow` re-pinned):** Finance
reported PO utilisation including tax. `po_commercial_summary` already said a PO's `total_value` is the TAXABLE BASE,
yet every Tax invoice drew its `grand_total` — a ₹10 L order read exhausted after ~₹8.47 L of work. `services/finance`
now has the ONE rule: `po_draw_amount(sub_total)` (the base), `ensure_po_covers(po, amount)` (400 "cannot cover …
(the invoice value before GST)"), `move_po_drawdown(db, po, project_id, ±amount)` (PO AND project allocation
together — they were updated side by side in five places), `release_invoice_from_po(db, invoice)` (delete / Admin
undo; a Proforma gives nothing back). Every caller goes through them: manual `POST /api/invoices` (now ends in
`consume_po_for_invoice`), Proforma raise check + conversion, the recurring-billing job, invoice change requests
(delta and PO move on sub-totals), both delete paths. `test_no_po_movement_uses_the_grand_total_any_more` source-scans
for the old shape. 0110 RESTATES existing data from the invoices themselves: `consumed_value` = Σ sub_total of the
PO's Tax invoices, `balance_value` = total − consumed (≥ 0), Active ⇄ Exhausted (Cancelled untouched), allocation
`consumed_amount` = Σ per (PO, project); downgrade restates on `grand_total`. The PO activity log line now reads
"… for X (value before GST; invoice total incl. GST Y)". Deploy: `alembic upgrade head`, restart.

**25 Sep 2026 — Screening Desk, internal fast-track to Sales, ATS on Candidate Profiles, migration 0111, head is now
0111 (`tests/test_screening_desk.py`, 17; `test_approval_permissions` re-pinned):** RMG / GM screened by opening each
opportunity → requirement → Applied Candidates → View resume → ATS → Shortlist. **New `services/screening_desk.py` +
`routers/crm/screening_desk.py`** (`_MODULES` += `screening_desk`): `GET /api/screening-desk` is ONE queue across every
live opportunity — profiles in `DESK_STAGES` (Sourcing · Technical_Screening · RMG_Review), not hidden, whose
opportunity's LATEST requirement is in `TA_LIVE_STATUSES`; filters `screening` (pending default · shortlisted ·
rejected · all), customer / opportunity / requirement / TA / applied window / `ats_band` (high ≥70 · medium ≥50 · low ·
unscored) / `internal` / search, `sort` newest · oldest · ats; meta carries per-tab `counts` (computed WITHOUT the
screening filter), `positions` (group headers over the whole result, pending first) and filter `options`.
`POST /api/screening-desk/score` (≤ `MAX_SCORE_BATCH`=5) is the auto-ATS: materialises the resume row for
profile-only applicants (`ensure_resume_for_profile`, MOVED to `services/resumes.py`, still importable from the
router) and runs `run_ats_scan` in a savepoint per profile. ⚠️ It never runs `auto_pipeline_after_scan` — opening a
page must not auto-shortlist or email a slot invite. ATS for a (candidate, opportunity) is the latest resume on ANY of
the opportunity's requirements, the SAME rule as `enrich_profiles_list` and the new `_LATEST_ATS_SCORE`, so one
candidate can never show two scores. **Internal candidate** = an ACTIVE employee whose official / personal email is the
candidate's, whose `employee_code` HR typed as the profile's `employee_ref`, or who was created from this profile —
derived (`internal_clause` for SQL, `internal_matches` batched per page, with today's Bench/Deployed from
`project_closure.deployment_by_employee`); `profile_type` cannot answer it. **Fast-track:** `POST
/api/candidate-profiles/{id}/fast-track-to-sales` `{note ≥ 10}` → `fast_track_internal`: internal only (400), not
resigned (400), from `FAST_TRACK_FROM` only (409); moves straight to Sales_Screening (the one jump the transition map
does not model), stamps screening Shortlisted, fills `employee_ref` from the employee, logs FAST_TRACKED +
STATUS_CHANGE, and calls the new public `candidate_profiles.record_stage_arrival` (workflow date + stage-owner notify,
exactly what `perform_transition` does) plus a TA notification (event `profile.fast_tracked`, in `email_flows.EVENTS`).
`GET /api/candidate-profiles/{id}` adds `internal_employee` + `fast_track_block`. **Approvals:** `profile.rmg_screening`
defaults to **RMG + GM** (was RMG) and new APPROVAL `profile.fast_track_internal` (RMG, GM); `rmg_roles` now takes its
roles from the registry. **0111** appends both keys to SAVED Approvals lists of templates tagged / custom roles named
RMG or GM (NULL lists already follow the defaults); `_APPROVAL_DEFAULTS` there holds only what 0111 changed, and the
snapshot test now layers 0108 then 0111 — ⚠️ a future approval action needs its own migration snapshot or that test
fails. **Candidate Profiles list:** `ats_score` sort key + `ats_min`/`ats_max` filters (list AND export; the export gains
an ATS Score column). `table_preferences` gains `announce` (`{"ats_score": "ai_interview"}`): an announced column a
saved layout never saw is inserted VISIBLE after its anchor; every other new column still arrives hidden.

**25 Sep 2026 — ONE candidate status on every screen (`tests/test_candidate_status.py`, 47; no migration):** the business
sent its status sheet ("Manual L1 – Scheduled", "Customer L2 – Failed", "HR Discussion" …) because `pipeline_status` says
which STAGE a candidate is in, never what happened IN it. `services/candidate_status.py` DERIVES that status from facts
already stored — stage + the latest `interview_events` row per round (`L1_Interview`/`L2_F2F` = Manual L1/L2,
`Customer_Interview` (stage "L2" = legacy L2) / `Customer_L2` = Customer L1/L2; Cancelled / No Show rows are ignored) +
`L1_REQUESTED`/`L2_REQUESTED` activity rows ("Yet to Schedule") + the latest AI link (`effective_result`) + RMG screening.
Naming convention: **`<who runs the round> – <result>`**, result ∈ Yet to Schedule · Scheduled · Passed · Failed; a round
held without a verdict reads Scheduled (hint says "waiting for the verdict"). Stage words: Shortlisted = **Candidate
Selected**, HR_Screening = **HR Discussion**, HR_Interviewing = **HR Round**, Preboarding = **Pre-Onboarding**
(`STAGE_LABELS`, also used by the stage-arrival bell titles). ⚠️ User rule: a fresh applicant reads **Sourcing** even after
RMG's shortlist — the status moves only when an L1 is requested / booked (screening Rejected → "RMG Rejected"). An AI
interview outranks nothing manual but is never read as "Manual L1 – Yet to Schedule". `derive_status(StatusFacts)` is PURE;
`load_facts` is 3 queries per 1,000 profiles; `statuses_for` never raises (stage-only fallback). `STATUS_DEFS` is the
catalogue (key · label · tone neutral/info/warn/ok/bad · group · hint · **stages it can come from**) — ⚠️ the `stages` set
drives the list filter's SQL pre-narrow and is brute-force pinned against `derive_status`
(`test_every_derived_status_declares_its_stage`): a new branch that returns a status from an undeclared stage fails there.
Payloads: `candidate_status` on `enrich_profiles_list` (list · applicants · export), `profile_detail`, the candidate
page's `profiles`, `reports/candidate-profiles` (CSV gets the words); `profile_status` on Applied Candidates rows and
Screening Desk rows (`attach_to_rows`, keyed by `profile_id`). `GET /api/candidate-profiles/status-options` (catalogue,
declared before `/{profile_id}`, pinned) and `?status_key=` (CSV) on the list AND export: `_narrow_by_status` runs AFTER
every filter + visibility, BEFORE ordering — `profile_ids_with_status` selects id/stage/screening/withdrawn with the
same joins (`with_only_columns`), derives, and adds `id IN (…)`, so page / count / sort stay exact. Export "Status" /
"Status Group" columns are the derived words (the raw "Withdrew From" column folded into "Self Withdrawn (…)").
**Stage column removed** from `TABLE_REGISTRY["candidate_profiles"]` (saved layouts drop it via `_clean`). Wording:
"HR Screening" → "HR Discussion" in messages / email-flow descriptions; requirement "engineering review" → "RMG review" in
notifications, the desk tile, the approval label and the ATS hint.

**26 Sep 2026 — the application is "Karnex Orbit" (`tests/test_app_name.py`, 15):** `config.APP_NAME = "Karnex Orbit"`
is the ONE product name (`APP_TITLE` is the same value under the name `main.py` imports — the FastAPI title, `/version`
and `/healthz` `service`). Every user-facing string reads it: the password-reset and interview-invite mails
(`email_smtp`, subject + "— Karnex Orbit" sign-off), the outbox From display name (`email_outbox._DEFAULT_FROM_LABEL`
→ "Pavan Sanap (Karnex Orbit)"; `candidate_comms` inline sends likewise), the `.ics` PRODID, the Ask AI and Support-bot
system prompts, the invoice PDF footer, the Settings ▸ Backup README header, the `CrmNotConfiguredError` text and the
AI-link schedule headline (`ai_interview_bridge`). ⚠️ **"Karnex" alone is the COMPANY** — the tax invoice wordmark,
offer letters, `org.company_short_name`, "— Karnex Recruitment Team" are deliberately unchanged. The test scans the
string literals of every user-facing module for the old names (Karnex CRM · AI HR Suite · KARNEX AI HR · AI Interview
Demo · AI Assessment Center · AI Hiring OS); comments / docstrings / file names keep the old vocabulary and that is fine.
F-V2: `lib/brand.ts` mirrors the constant (see that repo's notes).

**26 Sep 2026 — Screening Desk asks the interview ROUTE after a shortlist (`tests/test_screening_desk.py` now 26):**
reported — RMG / GM shortlisted from the desk and had no way to say whether the candidate takes the **AI L1** or a
**manual L1**; both paths existed only on Applied Candidates and the profile page. No new endpoint: every desk row now
carries `interview_route` (`screening_desk.interview_route`, PURE) — `chosen` `ai | manual | null`, `open` (Shortlisted,
stage in `ROUTE_STAGES` = Sourcing · Technical_Screening, nothing chosen), plus the AI link status/result/score and the
manual L1 requested/scheduled/result — built by `_attach_interview_route` from the SAME two batched helpers the Applied
Candidates rows read (`candidate_profiles.latest_ai_interviews`, `resumes.manual_round_state`), so the two screens can
never disagree. Any AI link at all (Pending included) = the AI route; an `L1_REQUESTED` log row or an `L1_Interview`
event = manual. The choice itself reuses the existing routes: `POST …/ai-interviews` (AI L1) and
`POST …/skip-ai-l1 {request_manual_l1: true}` (manual). F-V2: `crm/components/InterviewRouteChoice.tsx` (the shared
`GoManualModal` now backs the profile banner and the Applied Candidates button too) — see that repo's notes.

**26 Sep 2026 — new-applicant notice reaches everyone who may SCREEN; a grant opens the requirement list
(`tests/test_screening_notifications.py` 3, `tests/test_opportunity_stage_cascade.py` now 28):** reported — a TA
applied a candidate and neither RMG nor the GM got the bell or the email, and the GM's Opportunities tab was blank.
(1) `notify_rmg_new_applicant` addressed the role NAME "RMG" alone; the GM is a CUSTOM role and an RMG whose access
comes from a template's Approvals is not in a role called RMG. `candidate_profiles.screening_notify_user_ids(db)` =
`user_ids_who_may(db, "profile.rmg_screening")` — the SAME `user_may` that gates the Shortlist button — is now passed as
`user_ids=` by all three screening notices (single apply · bulk-ZIP summary in `resumes.py` · the SLA reminder in
`scheduler.py`; `notify_role` forwards `user_ids` to `notify_roles`), so recipients can never drift from who can act.
Both events (`profile.rmg_screening_requested`, `profile.rmg_screening_sla`, constants on `candidate_profiles`) are now
listed in `email_flows.EVENTS` (defaults RMG + GM) — they fired outside the list before, so Admin could neither see nor
re-route them. Pinned by a source scan of the three call sites. ⚠️ The notice still fires only while
`hiring.rmg_screening_gate` is on (default true): with the gate off nothing is stamped Pending and the desk queue is
empty, so a "screening needed" mail would point at nothing. (2) `services/requirements.sees_all_requirements(db, user)`:
RMG / Sales Head / Admin by role — OR a user with no `SCOPED_ROLES` (Sales, TA) whose Access Template / custom role
grants one of `REQUIREMENT_TABS` (`requirements`, `opportunities`); `apply_visibility(..., db=)` and
`ensure_visible(..., db=)` take the session (the five call sites in `routers/crm/requirements.py` pass it; `db=None`
keeps the pure role rule for existing callers/tests). A GM used to get 403 "Your role cannot view requirements" from
`list_requirements` (bare `get_current_user` — visibility was the only gate). A templated **Sales** user stays scoped to
their own deals — the grant admits, the scoping rule is theirs. F-V2: `OpportunitiesWorkspace` follows the grants for
templated / custom-role users (see that repo's notes). Deploy: in Access Control ▸ Roles give **GM** `opportunities:
view`, `requirements: edit` (Applied Candidates is `gated_write("requirements")`) and `profiles: edit`; restart.

**28 Sep 2026 — interview-wise AI spend (`tests/test_ai_interview_costs.py`, 17; no Alembic migration — the legacy
prompt-log table grows by ALTER):** the CEO asked what each AI interview costs (candidate · when · how much) with
daily / weekly / monthly / quarterly / yearly analytics, Admin/CEO only. Three layers. **(1) `services/ai_pricing.py`**
is the ONE price list — chat per 1M tokens in/out, TTS text in + spoken audio OUT per minute, STT audio IN per minute
(OpenAI's published $/min for the mini audio models); longest model prefix wins; `OPENAI_PRICING_JSON` overrides a
rate without a deploy (the old `OPENAI_PRICING_USD_PER_1K` still honoured); `usd_inr_rate()` reads Settings
`ai.usd_inr_rate` (default 84). **(2) `prompt_logger`**: ⚠️ **every call is priced AT LOG TIME** into the new
`ai_prompt_logs.cost_usd` (+ `audio_seconds`) — `_ADDED_COLUMNS` are ALTERed on both dialects by
`init_prompt_log_table`, and `backfill_prompt_log_costs` prices the pre-existing NULL rows ONCE from their tokens at
today's rates (a priced row is never re-priced, so a price change only ever affects new calls). **Attribution is a
ContextVar** (`interview_context(...)` / `set_interview_context(...)` / `current_interview_context()`): the Candidate
column on AI Logs read "-" for every row because the fourteen `tracked_chat_completion` sites deep in `ai.py` never
knew the session. Now the handlers that DO know it enter the context and `log_openai_call` fills its blank
interview / candidate / template fields from it — `main._interview_log_context(session)` at `/next`, `/answer`,
`_apply_turn_evaluation` (the per-turn thread) and `_evaluate_and_store_report` (sync submit · background upgrade ·
recovery, all funnel there), the HR-page strengths/weaknesses regeneration by record id, and
`_interview_log_context_from_request` (bearer → session) for `/candidate/tts` + `/candidate/transcribe`. ⚠️ A bare
`threading.Thread` does NOT inherit a ContextVar: `tts_prewarm.prewarm_tts` captures `current_interview_context()`
and re-enters it (pinned by a source scan). **Audio is logged now too** — `log_audio_call(kind="tts"|"transcribe")`:
the live TTS stream (`call_type` `tts_stream`, seconds estimated from the text at `TTS_CHARS_PER_SECOND`=15),
prewarmed clips (`tts_prewarm`, logged inside `ai.synthesize_speech_bytes`), and transcription (`transcribe`, seconds
from the client's new `duration_ms` form field — the browser is the only party that knows the clip's length — else
`STT_BYTES_PER_SECOND`); a cache hit costs nothing and is not logged; failures are logged with `status=failed` and
cost 0. `/admin/ai/usage` and `get_token_usage_stats` now report SUM(cost_usd) instead of the old "average of the two
token rates". **(3) `services/ai_interview_costs.py`** (`interview_cost_report(db_target, crm_db, …)`): ONE grouped
query over `ai_prompt_logs` per (interview, kind), then `interview_progress` (invite token, status, questions
answered, timestamps) → `interview_schedule` (scheduled time, started/completed, who scheduled) → CRM
`ai_interview_links` → profile (TA owner) → opportunity → customer, four batched queries for a whole page. Definitions
that must not drift: an interview's DATE is the day of its FIRST AI call; trend buckets at `day | week (ISO) | month |
quarter (Indian FY, Q1 = Apr–Jun) | fy` and **empty buckets are zeros, never gaps**; `other_spend` (ATS, resume parsing,
Ask AI, support bot — no interview id) is reported BESIDE the interviews and `total_spend` = both, never folded in;
filters (search · customer · TA · template · status) narrow the KPIs, trend and table together; rupees are printed,
never stored. `routers/crm/ai_costs.py` (`_MODULES` += `ai_costs`): `GET /api/ai-costs/interviews` (whole page +
one page of rows, `meta.total`) and `…/interviews/export.csv` (formula-neutralised), both `role_required()` =
**Admin/CEO only, not template-widenable** (pinned), reading the prompt-log store through `ai._db_target()` — the
same target the logger writes to. F-V2: AI Logs ▸ **Interview Costs** tab (see that repo's notes). Deploy: restart
(the ALTER + backfill run at startup); optional Settings row `ai.usd_inr_rate`. ⚠️ Interviews run BEFORE this deploy
have their chat calls priced by the backfill but no attribution (they were logged with an empty `interview_id`), so
they appear under "Other AI spend", not as interviews — only interviews from now on are itemised.
⚠️ **Same day, production 500 on the first open (`tests/test_ai_interview_costs.py` now 18):** the per-interview
query said `LIKE 'tts_%'`, and **psycopg2 reads a bare `%` in the statement as a parameter marker** — the SQLite
tests (`?` placeholders) never saw it. `_kind_case` now uses `SUBSTR(call_type, 1, n) = 'tts_'` (no wildcard, both
drivers), and `test_no_sql_in_the_module_carries_a_bare_percent_for_psycopg2` scans every SQL string in the module
for a `%` that is not `%s`. **Any raw SQL that reaches the legacy store must escape a literal `%` as `%%` on Postgres
or avoid it** — `prompt_logger` and `auth_db` follow the same rule. The two best-effort lookups (`interview_progress`,
`interview_schedule`) now `rollback()` on failure so a bad statement cannot poison the connection for the next query.

**28 Sep 2026 (later) — "AI Logs" is now "AI Costs": the raw-log tabs and their API are gone, and the figures are
exact (`tests/test_ai_interview_costs.py` now 21):** the user asked for the Logs + Analytics tabs to go, the tab
renamed, and the data made accurate. **Removed:** `routers/admin.py` (the whole `/api/prompt-logs/*` + `/admin/ai/usage`
API — nothing else called it) and its `main.py` include; `prompt_logger.query_prompt_logs / get_prompt_log_by_id /
get_token_usage_stats / get_distinct_values / prompt_logger_status / cleanup_old_db_logs`. The prompt-log TABLE stays —
it is the ledger the cost report reads. **Accuracy, three changes:** (1) chat calls price **cached prompt tokens** at the
cached rate (`usage.prompt_tokens_details.cached_tokens` → `Price.cached_input`); (2) transcription prices the
**audio tokens** the response reports (`usage.input_token_details.audio_tokens`, the gpt-4o-*-transcribe models) at
`audio_in_per_1m`, the per-minute figure is the fallback only — and the call is logged INSIDE `ai.transcribe_speech_bytes`
(the only frame that sees `usage`), under the request's `interview_context`, with the browser's `duration_s`; (3) TTS
is priced on the **measured** length of the MP3 — `utils/mp3_duration.py::mp3_duration_seconds` walks the frame headers
(MPEG-1/2/2.5 Layer III, ID3v2 skipped; pure) — the live stream logs at the END of `_relay` from the bytes actually sent
(an aborted stream bills what was produced), the prewarm clip in `ai.synthesize_speech_bytes`. `log_audio_call` never
replaces a measured length with the text estimate. ⚠️ Pricing changes only affect NEW rows (priced at log time).
**Retention that keeps the money:** the old DB cleanup DELETED rows past `PROMPT_LOG_RETENTION_DAYS`, which would have
erased cost history; `prompt_logger.prune_prompt_log_text` NULLs only the heavy text (prompts, payloads, response) and
keeps tokens / seconds / cost / attribution forever; scheduler job `prompt_log_retention` (`scheduler.prompt_log_retention`,
Settings ▸ Operations) runs it + `cleanup_old_file_logs` + `response_cache.purge_expired`. F-V2: the platform view key
stays `promptLogs` (saved tab grants and bookmarks keep working) but the nav label is **AI Costs** and the page is
`pages/InterviewCosts.tsx::AiCostsPage` alone — `pages/PromptLogs.tsx` and `api/promptLogs.ts` deleted.

**28 Sep 2026 — CEO dashboard: four FY tabs (`tests/test_executive_dashboard.py`, 11; `test_revenue_report` re-pinned):**
the CEO asked for Finance · Customer · Sales (+ a fourth, chosen: People) at the top of the Dashboard, by financial year,
graphical. `services/executive_dashboard.py` is ONE composition module — nothing is recomputed with a second rule:
**Finance** = `revenue_report(...)` sections (headline, targets, margin, collections, ageing, cashflow, pipeline,
efficiency, alerts, by_type, top customers) + `monthly` (billed · collected · people cost month-by-month INSIDE the
period, built from `_invoices_in_window` / `_payments_in_window` / `_deployed_rows` because the report's own trend is
12 months / 8 quarters / 5 FYs and cannot show how THIS year unfolded); **Customer** = customer → project → employee
tree (billed by invoice date · previous period · share · collected · outstanding / overdue (`_open_balances`, the ageing
due rule) · people cost `CTC/12 × months × overlap` · margin · heads / live today (`deployment_by_employee`) · PO balance ·
open positions (`hiring._customers`) · per person rate / unit / monthly rate / billed via timesheet / cost / onboarding /
exit / **internal vs external** from `placements_report.classify` over the person's whole placement history
(`_kind_index`); a manual invoice stays on the project as `unlinked_billed`, never dropped); **Sales** =
`hiring_dashboard(...)` (kpis · pace · targets · series · funnel · customers · stage_delays — pinned equal) +
`positions` (every live position, plus on the table's "All" those opened / closed in the period: filled / open /
fill %, age, owner via `_user_names`, and each JOINED candidate with `kind` — `_joined_kinds` links the profile to its
Employee (`candidate_profile_id`, else `employee_ref` = `employee_code`) and runs the SAME `classify` with the joining
day as the placement date and the person's last assignment BEFORE it as the prior; no employee record → `unknown`,
never guessed), `onboarding_mix`, `monthly` (internal / external stacked + positions in), `by_owner`; **People** =
headcount (active and not past `last_working_day`), deployed / bench today, utilisation, bench cost / month, joiners
(`date_of_joining`), REALISED exits (`last_working_day` else `date_of_resignation`, ≤ today — a planned last day is
"on notice" and shows in its month on the strip), attrition = exits ÷ average headcount, tenure, revenue per deployed
head, `bench_rolloffs(90)`. `GET /api/dashboard/ceo?tab=finance|customer|sales|people&month=&period=`
(`routers/crm/dashboards.py`) is **`role_required()` = Admin/CEO only, not template-widenable** (pinned) — the Customer
tab prints every customer's revenue beside every employee's cost. `month` is the ANCHOR at every zoom, default
**`period=fy`**. ⚠️ **Decision closed the same day: the Revenue report no longer counts Proformas** —
`revenue_report._invoice_filters` always adds `Invoice.kind != Proforma`, so billed / outstanding / ageing / cash flow
(and everything composed from them) are over ISSUED Tax invoices only (`test_revenue_report_no_longer_counts_proformas`).
F-V2: `crm/pages/dashboard/CeoDashboard.tsx`, mounted for Admin/CEO INSTEAD of the hiring tower (its Sales tab is the
tower); the pace dials are now the coloured `Speedometer` (see that repo's notes).

**28 Sep 2026 (later) — onboarding trend at three zooms (`tests/test_executive_dashboard.py` now 12):** the Sales
tab payload gains `onboarding_trend: {month, quarter, fy}` (`_onboarding_trend`) — internal / external / unknown
onboardings per bucket: `month` = the months of the selected period, `quarter` / `fy` = the last 8 quarters / 5 FYs
ending at the anchor (`SERIES_BY_PERIOD`, `Period(kind, period.anchor).shift(-i)`), oldest first, `future` flagged. One
payload, so the UI flips Monthly · Quarterly · Yearly with no reload. Same `classify` kinds as `onboarding_mix`.

**28 Sep 2026 — the Screening Desk is the whole RMG / GM workbench (`tests/test_screening_desk.py` now 32; no
migration):** "RMG & GM should never open Opportunities or Candidate Profiles for their own work — L1 / L2 scheduling and
feedback from the desk too." Inventory of what RMG does per candidate (all of it already had an endpoint): ATS · resume ·
screening Shortlist/Reject · fast-track · route (AI L1 / manual L1) · book the L1 / L2 (`l2-face-to-face`) or ask TA
(`l2-request`) · record round feedback (`PUT interview-rounds/{id}`) · extra L3/L4 rounds · skill evaluation · Submit to
Sales / Reject (`status-transition`) · history. **Server (`services/screening_desk.py`):** every row now carries `ai_l1`
(from `latest_ai_interviews`), `rounds` (`ladder_state` over `manual_round_state` — the SAME helpers the Applied Candidates
tab reads), **`next_step`** (PURE: `{key, label, owner you|TA|candidate|AI|done, tone}` — the one-line "whose move" every
row prints), **`decision`** (PURE `decision_state`: Submit/Reject only at RMG_Review, blocked until a requested/booked L1
or L2 carries a result — the requirement page's rule) and `tab`. Fifth tab **`review`** = stage RMG_Review;
`shortlisted` is now shortlisted-and-NOT-in-review (`screening_tab_for`, counts grouped by (status, stage)). Position
headers carry `review`, **`jd_missing`** (no `rmg_jd_text` and no skills — the "Could not score" case), `description`,
`rmg_jd_text`, `skills` (`_skills_by_requirement`, one query). `meta.approvals` = `approvals_queue(db, user)`: the
Pending_Engineering_Review requirements, items ONLY when `user_may(requirement.engineering_approve)` (the approve gate's
own decision). `meta.round_results` = the verdict scale.
⚠️ **GM acts as RMG on the technical ladder — `action_permissions.screens_as_rmg(db, user)`.** GM is a custom role
(`roles == {"GM"}`), so every `"RMG" in user.roles` check said no while the desk's own gate (the APPROVAL
`profile.rmg_screening`) said yes: the RMG_Review → Sales/RMG_Rejected transition (`STAGE_AUTHORITY`), the L1–L4 rounds
(`ROUND_WRITE_ROLES`), the L2 request owner check and `_derive_l2_interviewer`. ONE rule now: whoever may take the
screening decision (built-in RMG, Admin/CEO, or a template / custom role holding the approval via `user_may`) acts as RMG
there. Threaded as an optional `db` (`user_may_transition_from(…, db)`, `allowed_next_statuses_for_user(…, db=)`,
`rounds_writable_by(user, db)`, `ensure_may_write_round(user, kind, db)`) — callers without a session keep the pure role
rule, so `role_required("RMG")` endpoints (requirement approvals, position requests) are NOT widened. Pinned by
`test_a_gm_with_the_screening_approval_acts_as_rmg_on_the_technical_ladder`. F-V2: `ScreeningDesk.tsx` +
`InterviewLadder.tsx` / `SkillEvalGrid.tsx` / `RmgApprovalsStrip.tsx` / `JdSkillsModal` approve mode (see that repo's
notes); the profile page's RMG banners and technical-round feedback now key off `useCanApprove("profile.rmg_screening")`.
Suite: 1,587 pass in six chunks.

**28 Sep 2026 (late) — every CEO tab reads like Finance (`tests/test_executive_dashboard.py` now 15):** the Customer,
Sales and People tabs gained the data the Finance layout needs — alerts → tiles → "How the year unfolded" → three dials →
four bar charts → table. **Customer**: `monthly[]` (billed · collected · cost per month + `customers: {name: billed}` for the
top `DONUT_SLICES` billers, everyone else folded into "Others (N)"; `monthly_series` names the series), per customer
`monthly_burn` (Σ monthly rate of heads deployed TODAY) and `po_cover_months` (active PO balance ÷ burn; None with no burn),
`alerts` (concentration · overdue per customer · PO cover < `PO_COVER_WARN_MONTHS`=2 · down ≥ `CUSTOMER_DROP_WARN_PCT`=30 vs
previous · loss-making). **Sales**: `_onboarding_trend` buckets carry `positions_in` at all three zooms (the tower's
"pipeline positions" rule — it now takes `reqs`), `by_customer` (live positions rolled up: open · joined · internal ·
external · stale), `alerts` (pace `bad` · stale ≥ `STALE_POSITION_DAYS`=30 · open-position concentration · stage delays past
`bad_days` · onboardings with no employee record). **People**: `monthly[]` adds `deployed` / `bench` / `cost` / `bench_cost`
at month end — deployed on a date is read from the SAME `_deployed_rows` (start / exit) the Revenue report costs, because
`deployment_by_employee` only knows today; `headline.people_cost_month` + `bench_cost_pct` (the third dial), `by_role`
(deployed · bench · on notice · cost per designation), `tenure` (`TENURE_BUCKETS` <1 · 1–2 · 2–4 · 4+ yrs, deployed vs
bench), `rolloffs_by_month` (heads + monthly rate that stops billing, from `bench_rolloffs`), `alerts` (bench cost ≥
`BENCH_COST_WARN_PCT`=10 of people cost · roll-offs ≤ 30 d · attrition ≥ `ATTRITION_WARN_PCT`=20 · on notice · heads without
CTC). Every alert is the Revenue report's `{key, level, title, detail}` shape (`_alert`) so ONE chip strip renders all four
tabs. F-V2: `CeoDashboard.tsx` (see that repo's notes).

**28 Sep 2026 (later still) — deployed heads on customer sites over time (`tests/test_executive_dashboard.py` now 17):**
the CEO asked "how many candidates are deployed at each customer location, monthly / quarterly / by FY". The Customer tab
payload gains `deployed_trend: {month, quarter, fy}` (`_deployed_trend`) — heads deployed at the END of each bucket (as of
TODAY for a bucket still running; `future` for one not started), `customers: {name: heads}` and `locations: {loc: heads}`
+ `total` (a person on two customers counts once per customer, once in the total). The `DONUT_SLICES` customers with the
highest PEAK across every bucket are their own series, the rest "Others" — peak, not today's count, so an account that
was big last year stays visible at the quarter / FY zooms, which look back 8 / 5 buckets and therefore read EVERY
assignment (`_deployed_rows`, start / exit — the same rows the People tab and the Revenue report cost). **Location** has
no column on the assignment: `_project_locations` = the project's delivery branch (`CustomerBranch.city`, else
`branch_name`) → the opportunity's `tm_work_location` → "Unspecified" (`UNSPECIFIED_LOCATION`). `deployed_series`
names the customer and location series. F-V2: the "Deployed on customer sites" panel (By customer / By location ×
Monthly / Quarterly / Yearly) under the customer-wise unfolded chart.

**28 Sep 2026 (night) — monthly · quarterly · yearly revenue targets, per FY (`tests/test_revenue_report.py` now 33):**
the CEO asked to set a month, a quarter and a financial-year target from the Dashboard. **Storage stays per month**:
`PUT /api/reports/revenue/targets` gains `quarter_target` (+ `clear_quarter_target`) — with `month`, the FY quarter holding
it gets three `revenue.target.<YYYY-MM>` overrides of a third each (the LAST month absorbs the rounding paise; pinned);
without `month` it is a 400. `fy_target` with `month` now writes **`revenue.target_fy.<start year>`**
(`TARGET_FY_PREFIX`, e.g. `…2026` = FY 2026-27) — one target per financial year; the old single `revenue.target_fy`
stays as the DEFAULT for years without one (`_targets` reads the per-FY key first). `targets` payload adds the anchor's
own rungs so one dialog can edit all three whatever the zoom: `anchor_month` / `anchor_month_target` /
`anchor_month_target_is_override`, `quarter_key` / `quarter_label` / `quarter_target` / `quarter_months_with_target`,
`fy_key` / `fy_target_is_default`. F-V2: `crm/components/RevenueTargetsModal.tsx` (ONE dialog for the Revenue page and
the CEO dashboard; only the rungs that CHANGED are written, quarter first so an explicit month wins), `FySelect` (see
that repo's notes).

**28 Sep 2026 — Suggested Candidates score against the RMG JD, name what fits and what lacks, and carry the
whole history (`tests/test_candidate_match.py` now 7; no migration):** "on what basis does the application suggest
these candidates?" — it was skills + band + history, and the 35 % rows in the report were candidates with NO skill
overlap at all (20 band + 10 same customer + 3 CV + 2 phone). `services/candidate_match.py` rewritten around a
**basis** (`suggestion_basis(db, opp)`, returned as `meta.basis` by `GET /api/opportunities/{id}/suggested-candidates`):
the opportunity's LATEST requirement — its `RequirementSkill`s (they WIN over the opportunity's list per skill id;
the opportunity fills the rest) and its **RMG JD** (`rmg_jd_text`, else `description`) through the ATS's own
`extract_jd_keywords` — plus the CTC-slab band. **Corpus per candidate, no file opened, no model call**: recorded
skills · technical domain · roles · job titles + employers · education · the `matched` / `jd_keywords_matched`
terms in `resumes.ats_score_breakdown` (what earlier ATS scans PROVED), so an untyped skill list still matches.
Pools **renormalised over what is configured** (like the ATS): mandatory 30 · optional 15 · JD fit 15 · experience 20
(half within a year) · history 15 (late stage 6 · this customer 6 · any 3) · contact 5; a candidate with no skill
rows and no ATS terms scores the skill pools from their latest resume's `ats_score` (`skills_from_ats`, named in
the gaps); **a rejection at THIS customer within `RECENT_REJECTION_DAYS`=365 costs `RECENT_REJECTION_PENALTY`=15**
after normalisation and is the first gap ("Rejected at this customer N month(s) ago (title) — −15 pts"); older or
elsewhere rejections are history only. The pool is built in TIERS (skill match → this customer → late stage →
ATS-rated ≥ `ATS_POOL_MIN`=50 → band) and truncated in that order, so the cap never drops a skill match. Every row
carries `strengths[]` / `gaps[]` (`{area, detail}`: Skills · JD fit · Experience · History · Contact),
`jd_terms_matched/missing`, `missing_optional_skills`, `penalty`, and **`history[]`** — EVERY previous application
(opportunity, customer + `this_customer`, stage label, outcome joined/rejected/withdrawn/engaged/in_progress,
withdrawn-from, RMG screening, AI L1 effective result + score via `latest_ai_interviews`, ATS score for that
opportunity, applied date) — five batched queries for the whole pool. `reasons` and `last_application` stay for
older callers; the email endpoint's allow-list is unchanged. F-V2: `SuggestedCandidatesTab` (see that repo's notes).

**28 Sep 2026 (late night) — HR's desk = the People tab (`tests/test_executive_dashboard.py` now 18):**
`GET /api/dashboard/people?month=&period=` (`routers/crm/dashboards.py`) returns `executive_dashboard(db, "people", …)`
alone — headcount · deployed vs bench · joiners / exits · attrition · tenure · roll-offs — behind
`gated_read("employees", "HR")` (HR by role, or a template / custom role granting the Employees tab); the other three
tabs stay Admin/CEO only on `/ceo`. Nothing in the payload HR cannot already read on the employee record. F-V2:
`HrPeopleSection` on the HR dashboard (see that repo's notes).

**28 Sep 2026 (night) — Sourcing → Technical Screening → Technical Interview (`tests/test_candidate_status.py` now 50,
`tests/test_applied_profile_only_rows.py` +1; no migration):** user rule — the front of the flow reads by WHO HOLDS the
candidate. `services/candidate_status._internal` (derived status only; the stored stage, transitions and permissions are
untouched): **Sourcing** = with TA, not in RMG / GM's queue (screening NULL — gate off / legacy); **Technical Screening**
(`technical_screening`, group `screening`) = screening **Pending**, i.e. on the Screening Desk — with the gate on (default)
a TA upload lands here AT ONCE (user decision: keep it automatic, no "Send to RMG" step); **Technical Interview**
(`technical_interview`, group `internal`) = RMG / GM **Shortlisted**, until an L1 is asked for / booked — then the round's
own status wins as before. Both new statuses apply only in `_PRE_REVIEW` (Sourcing · Technical_Screening); RMG_Review with
nothing booked still reads "Manual L1 – Yet to Schedule". ⚠️ This SUPERSEDES the 25 Sep rule "a fresh applicant reads
Sourcing even after RMG's shortlist". `GROUPS` is now sourcing · **screening** (Technical Screening + RMG Rejected) ·
internal (Technical interview & internal rounds) · customer · selection · closed. **Filters:** `keys_for_groups()` +
`status_filter_keys(status_key, status_group)` (both given → they intersect); `?status_group=` (CSV) on
`GET /api/candidate-profiles`, its export, AND `GET /api/requirements/{id}/resumes` — the resumes list resolves the
matching profiles of the opportunity ONCE (`profile_ids_with_status`) and narrows both resume rows and profile-only rows
(`_profile_only_applied_rows(..., profile_ids=)`), so page / count stay exact. Every payload that carries
`candidate_status` / `profile_status` (TA lists, Applied Candidates, Screening Desk, profile, reports, exports) shows the
new words with no other change.

**28 Sep 2026 (night) — "Customer #60" on the position page (`tests/test_requirement_detail_names.py`, 2):** reported
with a screenshot — a TA opened a position and the header read "Customer #60". The requirement LIST already attached
`customer_name` / `location_name` (so TA never had to call /api/customers), but the DETAIL page fetched the name from
`GET /api/customers/{id}`, which is `gated_read("customers")` — TA / RMG / GM without the Customers tab got a 403 and the
id. `routers/crm/requirements._one` (the ONE builder of every single-requirement response) now calls `_attach_names`, so
the payload is self-sufficient like the list rows. ⚠️ Rule: a page must never look up a NAME through a tab-gated
endpoint — use the payload or `GET /api/customers/names` (`any_crm_role`). F-V2: the same fix on PO detail and Holidays.

**28 Sep 2026 (night, later) — Stage + Status on Applied Candidates, every applicant listed, TA budget decision
(`tests/test_candidate_status.py` 51, `tests/test_applied_profile_only_rows.py` 6, `tests/test_ta_budget_decision.py` 5,
`tests/test_screening_desk.py` re-pinned; no migration):** user asks for the TA's Applied Candidates tab. **(1) Stage +
round** — `candidate_status.derive_status` now also stamps `stage_key/stage_label` (the PHASE, `STAGES`: Sourcing ·
Technical Screening · Technical Interview · Sales Screening · Customer Screening · Customer Interviewing · Candidate
Selected · HR Screening · Onboarding · Closed; `stage_for(facts)` PURE — the three front phases come from the facts,
Self Withdrawn from `withdrawn_from`, a closed row stays in the phase it closed in) and `round_key/round_label/round_state`
(`round_for`: "Technical L1 Interview" / "Technical L1 Interview (AI)" / "Technical L2 Interview" / "Customer L1|L2
Interview" / "HR Round" + Yet to Schedule · Scheduled · Passed · Failed · Under Review; non-round statuses map through
`_STATUS_ROUND`). `as_dict()` carries them as `stage: {key,label}` + `round: {key,label,state}` on EVERY payload; the
round key names the row field holding the date (F-V2 `ROUND_WHEN_FIELDS`). Export gains a **Stage** column.
**(2) Filter** — `?phase=` (CSV of `STAGES` keys) on `GET /api/candidate-profiles` (+ export) and
`GET /api/requirements/{id}/resumes` via `profile_ids_in_phase` (SQL pre-narrow by `_pipelines_for_phase`, then derive;
live phases exclude `REJECTED_BUCKET`, "closed" = `REJECTED_BUCKET`). It REPLACES tonight's `status_group`
(`keys_for_groups` / `status_filter_keys` removed) and the resumes list's old `stage` param (the only caller was the
pill row). **(3) Every applicant in Applied Candidates** — `_profile_only_applied_rows` no longer requires RMG
Shortlisted: every non-hidden profile on the opportunity without a resume row on this requirement. **(4) TA budget
decision** — rows carry `expected_ctc` (profile, else candidate) · `budget_ctc_max` (requirement) · `over_budget`
(`candidate_profiles.budget_fit`, PURE, both figures needed). `POST /api/candidate-profiles/{id}/ta-decision
{decision: hold|release|reject|not_fit, note}` (`gated_write("profiles","TA")`) → `ta_budget_decision`: only at
Sourcing / Technical_Screening (409 later); **hold** = `budget_status = "TA_Hold"` (`candidate_status.TA_HOLD`), status
"On Hold – Over Budget", stage Sourcing, OFF the Screening Desk (`screening_desk._base` excludes it); **release** clears
it; **reject / not_fit** (reason ≥ 5) go through `perform_transition` → Rejected, then an activity row
`TA_REJECTED_BUDGET` / `TA_NOT_FIT` (`TA_CLOSE_ACTIONS`, read by `load_facts` into `StatusFacts.ta_closed`) makes the
status read **"Rejected – Over Budget"** / **"Not Fit"**. ⚠️ `perform_transition` clears a lingering `TA_Hold` on ANY
move, and the HR "Not Recommend" → Concern flag treats `TA_Hold` as unset, so the TA hold can never collide with the
Pre-Onboarding budget flag that shares the column. `TABLE_REGISTRY["requirement_resumes"]` gains `profile_stage`
(announced visible after `rmg_screening_status`). Suite: 1,607 pass.

**28 Sep 2026 (late night) — TA → RMG / GM → TA hand-offs made explicit, feedback-due reminders
(`tests/test_ta_decision.py` 12 — replaces `test_ta_budget_decision.py`; `tests/test_interview_followups.py` 4;
`test_candidate_status` / `test_screening_desk` / `test_screening_notifications` re-pinned; no migration):** user flow,
⚠️ **it REVERSES tonight's "keep the upload automatic" decision**. (1) **An upload waits at Sourcing, with TA.**
`slot_booking.ensure_sourcing_profile` no longer stamps screening Pending (its `notify_rmg` param is gone), the bulk ZIP
no longer sends the "awaiting screening" summary, and `POST /api/candidate-profiles` stamps nothing for a non-screener
(a screener — `screens_as_rmg` — still self-shortlists). `candidate_profiles.ta_decision(db, profile, decision, note,
user)` (was `ta_budget_decision`) — `TA_DECISIONS` = **screen** (→ `send_for_screening`: Pending + `SENT_FOR_SCREENING`
activity + hold lifted; refused once Pending / Shortlisted / Rejected — `_screening_refusal`) · **hold** · **release** ·
**reject** (reason ≥ 5 → Rejected + `TA_REJECTED` → status "Rejected by TA") · **withdraw** (reason ≥ 5 → Self_Withdrawn
through `perform_transition`, any live stage). `POST …/{id}/ta-decision` pattern updated; **`POST
/api/candidate-profiles/send-for-screening {profile_ids}`** (TA, declared before the parametric POSTs) sends a batch with
ONE summary (`_notify_screening_batch`, link → Screening Desk). The TA_Hold status is now plain **"On Hold"**; "Rejected
– Over Budget" / "Not Fit" are gone (`TA_CLOSE_ACTIONS = {"TA_REJECTED": "ta"}`). The Screening Desk never shows a
never-sent pre-review row (`_base`: screening NOT NULL or RMG_Review). `rmg_screening_blocks_l1` now also blocks a
not-yet-sent candidate at Sourcing / Technical_Screening. The SLA reminder measures from the latest
`SENT_FOR_SCREENING` row (else the apply date). (2) **RMG / GM choose the route, TA schedules.** New `POST
…/{id}/request-ai-l1 {note?}` (`rmg_roles`; 409 unless Shortlisted, pre-review, no AI link yet) logs `AI_L1_REQUESTED`
and tells TA (`profile.ai_l1_requested`); status "AI L1 – Yet to Schedule"; `manual_round_state` carries
`ai_l1_requested`, desk `ladder_state` → `ai_requested`. The manual route is still `skip-ai-l1`. (3) **The L2 follows
the L1 verdict**: `l2-request` with round L2 → 400 "Record the L1 feedback first" until `l1_verdict_recorded` (a
resulted `L1_Interview` event, or a completed AI link). (4) **Feedback due** — `services/interview_followups.py`: a
round is due once `scheduled_at + duration` (default 60 min) has passed with no result, not Cancelled / No Show /
Rescheduled, within `LOOKBACK_DAYS`=45, candidacy live. Owners: L1–L4 → screeners (`screening_notify_user_ids`), HR
round → HR, customer rounds → Sales + Sales_Head, TA → their own candidates, Admin/CEO → all (`areas_for`).
`GET /api/dashboard/feedback-due` (any CRM role) = `feedback_due_for(db, user)`. Scheduler job
`interview_feedback_due` (`scheduler.interview_feedback_due`, default on) is the first **every-pass** job
(`EVERY_PASS_JOBS` skips the run-hour and once-a-day checks); it reminds each round at most once per
`REMIND_EVERY_HOURS`=24, remembered as a `FEEDBACK_REMINDER` activity row ("round #<id>") — ⚠️ `notify_*` writes a
bell row on EVERY call; only the email is deduped by the outbox, so a job that runs every pass needs its own record.
(5) **AI L1 outcome reaches every screener**: `ai_interview.passed_review` now passes `user_ids=_screeners(db)` and a
new `ai_interview.failed_review` fires on a fail. `routers/crm/candidate_profiles._notify_ta` + `_candidate_name` are
the ONE "tell the TA owner (else every TA)" path (skip-ai-l1, rmg-screening, l2-request, request-ai-l1 — was four
copies). `email_flows.EVENTS` += `profile.rmg_screening_decided`, `profile.ai_l1_requested`, `candidate.l1_requested`,
`candidate.l2_requested`, `profile.ai_l1_skipped`, `ai_interview.failed_review`, `interview.feedback_due` (they fired
outside the list). `TABLE_REGISTRY["requirement_resumes"]` drops `ats_status` (the column is gone). Removed the unused
`TemplateRequestStatus` import in `ai_interview_bridge`. Suite: 1,623 pass, 2 skipped (run from a writable copy).

**28 Sep 2026 (latest) — TA row follows the DERIVED stage, auto-ATS on every add, rounds tally, "Customer
Shortlisted" (`tests/test_ta_decision.py` now 18; `test_candidate_status` / `test_candidate_match` re-pinned; no
migration):** screenshot report — a TA row read Stage "Technical Interview" / Status "Technical L1 Interview · Scheduled"
with no time, yet offered Technical Screening / Hold / Run ATS Scan. (1) **Time:** that round was booked with a
free-text time that never parsed (`scheduled_at` NULL, only `raw_when`); `resumes.manual_round_state` now falls back to
`raw_when` for every `<round>_when`. (2) **TA calls are a SOURCING-phase thing**: `candidate_profiles.ta_decision` and
`send_for_screening` check the DERIVED stage (`_ta_stage` → `candidate_statuses_for`, `candidate_status.SOURCING_STAGE`)
instead of the stored pipeline value — a legacy row stored at Sourcing with an L1 booked reads Technical Interview and gets
409 "… this one is at Technical Interview"; Self Withdraw stays open at any live stage. (3) **ATS is automatic on every
add**: upload and bulk ZIP already scanned; `POST /api/candidate-profiles` (Candidates page / Apply to Opportunity) now
calls the new `services/resumes.auto_score_profile` (latest requirement · candidate CV → `ensure_resume_for_profile` →
`run_ats_scan`, savepoint, never fails the apply, NO auto-pipeline). The per-row "Run ATS Scan" is gone from the UI; the
endpoints stay (the Applicants tab and scan-all use them). (4) **Rounds tally**: `manual_round_state` adds
`rounds_booked` / `rounds_done` — every `interview_events` row not in `interview_rounds.NOT_HELD_STATUSES` (Cancelled ·
No Show · Rescheduled…, now the ONE tuple; `interview_followups` imports it) plus ONE for an AI L1 (any link; done when
completed). (5) **Stage chips = the Stage column**: `candidate_status.phase_counts(db, stmt)` (same `stage_for` + the
`REJECTED_BUCKET` "closed" rule as `profile_ids_in_phase`, so a chip's count is what clicking it lists) →
`GET /api/requirements/{id}/resumes` `meta.phase_counts` (the opportunity's non-hidden profiles; "Applied by" narrows
them, search / dates do not). (6) **"Candidate Selected" → "Customer Shortlisted"** everywhere it is a label: STAGES
`selection` (key unchanged — URLs keep working), the `candidate_selected` status, `STAGE_LABELS[Shortlisted]` (bell
titles). Suite: 1,627 pass, 2 skipped.

**28 Sep 2026 (last) — the work desk: every role's daily tasks as Dashboard tabs (`tests/test_work_desk.py`, 4;
no migration):** user ask — "give tabs like the CEO dashboard, each role's daily task, separate". New
`services/work_desk.py::desk(db, user)` behind **`GET /api/dashboard/desk`** (`any_crm_role`; it REPLACES
`/api/dashboard/feedback-due`, which is gone). It computes no rule of its own — each tab reshapes an existing answer into
ONE item shape `{key, title, subtitle, chip, tone ok|warn|bad|info, when, path (CRM path), action, profile_id}`:
**feedback** (`interview_followups.feedback_due_for`) · **schedule** (TA: requested-not-booked AI L1 / Technical L1 /
Technical L2 / HR round via `manual_round_state` + `latest_ai_interviews`, Customer L1 at Customer_Interview and
Customer L2 at L1_Feedback when not booked — the same rules as TA's Applied Candidates buttons) · **sourcing** (TA: own
candidates whose DERIVED stage is Sourcing — never sent / on hold) · **screening** (screeners: RMG screening Pending) ·
**route** (screeners: Shortlisted, pre-review, no AI link / AI request / manual L1) · **upcoming**
(`dashboard_desk.upcoming(…, 7)`) · **queues** (`dashboards.my_work`, count = Σ items). Tabs per login: TA / Admin get
schedule + sourcing (TA scoped to `ta_owner_id == user`, Admin everyone); `screens_as_rmg` gets screening + route; feedback
only when `areas_for` has an area or the user is TA; upcoming + queues for everyone. Each tab builds in its own savepoint
and a failure is logged and LEFT OUT — one broken tab never blanks the desk (pinned). `MAX_ITEMS`=50 per tab; the count is
the real total. `_profiles()` is the one batched query (profile · candidate · opportunity · latest requirement id, ≤ 500),
`_applied_path()` builds `requirements/{id}?tab=resumes&q=<email|name>`. `my_work` gains **`proformas_to_convert`**
(Finance: `kind = Proforma`, not returned → `invoices?tab=Proforma`; F-V2 `InvoicesPage` now reads `?tab=`).
`interview_rounds.NOT_HELD_STATUSES` docstring note unchanged. Suite: 1,631 pass, 2 skipped.

**28 Sep 2026 (final) — RMG / GM pending work in one list + "interview done" results (`tests/test_rmg_tasks.py`, 7;
`test_screening_desk` re-pinned: 4 desk-gated routes; no migration):** user ask — every RMG / GM pending task on the
Screening Desk AND the Dashboard, each opening the exact place; RMG told when any interview is done and it stays
highlighted until looked at. **`services/rmg_tasks.py::screener_tasks(db, user)`** is THE list (`CATEGORIES`, reading
order): **feedback** (`interview_followups.feedback_due_for`) · **results** (below) · **screening** · **route** ·
**booking** (l1/l2/ai `_book` — TA books, RMG may) · **decide** (RMG_Review, ladder judged) · **ai_failed** — these five
straight from `screening_desk.next_step` over the desk's own `_base` rows (`_STEP_CATEGORY`, ≤ `MAX_DESK_ROWS`) — then
**approvals** (`approvals_queue`), **jd** (live requirement, no `rmg_jd_text`, no skills → `requirements/{id}?tab=details`),
**headcount** (Pending `RequirementPositionRequest`, only when `user_may("requirement.positions.approve")`),
**templates** (`TemplateRequest` Pending_RMG, when the Template Requests tab / RMG allows). Item shape = the work desk's;
desk items link `screening-desk?task=<cat>&focus=<profile id>` (`desk_path`). Each category in its own savepoint.
`task_profile_ids(db, user, task)` → `GET /api/screening-desk?task=` (sets `DeskFilters.profile_ids`, screening
forced to "all"; unknown task → 400). `GET /api/screening-desk/tasks` and `POST …/results-reviewed {profile_id, keys?}`
sit behind `desk_gate`. **Results to review**: a finished AI L1 (`completed_at`) or a round verdict (`RESULT_RECORDED`
activity row, comment `round:<event id> …`) within `REVIEW_WINDOW_DAYS`=21 on a live profile, until a
`RESULT_REVIEWED` row names its key (`ai:<link>` / `round:<event>`) — activity log, so no migration and an audit trail.
`record_round_result(db, profile, event, user, previous)` runs on BOTH round create and update (a new or changed
verdict, kinds `REVIEWED_KINDS` — HR's round excluded): a screener's own verdict is marked reviewed at once; anyone
else's (Sales on a customer round, a panel interviewer) notifies every screener (`interview.result_recorded`, new in
`email_flows.EVENTS`, RMG + GM, deduped per verdict). AI L1 pass / fail notices now link to
`screening-desk&task=results&focus=<id>` (`ai_interview_bridge._review_link`). Desk rows carry `new_results`.
`work_desk`: a screener's tabs are `screener_tasks` categories (feedback · results · screening always; others while
non-empty) — the Dashboard and the desk cannot disagree; the old `_screening_tab` / `_route_tab` are gone.

**28 Sep 2026 (last) — Screening Desk filters (`tests/test_screening_desk.py` +5):** `DeskFilters` gains `location`
(Candidate.city ilike), `exp_fit` in / out / unknown (SQL against the desk's own Requirement band, NULL bounds =
open), and three DERIVED filters — `next_owner` (`next_step.owner`: you · TA · candidate · AI · done), `route` (ai ·
manual · none, from `interview_route`) and `new_results` (`rmg_tasks.unreviewed_results`). When a derived filter is on,
`desk_queue` reads ≤ `MAX_DERIVED_ROWS`=2000 ordered ids, keeps them through `_derived_keep` (the SAME batched helpers
the rows print) and pages in Python, so `total` stays exact. Bad values → 400. Search also matches the customer name,
`req_number`, city and technical domain. Sorts gain `ats_low` and `experience`. `_options` adds `positions`
(`{id, label "title · REQ-…", customer_id, opportunity_id}`) for the new Position filter; `requirement_id` was
already accepted. All new params on `GET /api/screening-desk`. Note: the tab counts ignore the derived filters.

**28 Sep 2026 (night, last) — Dashboard tiles only, TA hand-off fixes, the round-time drift, Submit-to-Sales note,
Sales next-step buttons (`tests/test_work_desk.py` re-pinned, `tests/test_handover_note.py` 3, `test_ta_decision` +1,
`test_applied_profile_only_rows` +1; no migration):** (1) **Every desk tab carries `link`** (`work_desk.tab_link`): RMG /
GM categories → `screening-desk?task=<key>`, everything else → `my-tasks?tab=<key>` (`TASKS_PAGE`). The Dashboard shows the
tiles only; the work happens on the page the tile opens. (2) ⚠️ **TA notices reach BOTH TAs**: `routers/crm/candidate_profiles.
_ta_recipients` = the TA owner (who applied) + the TA who pressed Technical Screening (latest `SENT_FOR_SCREENING` log row),
de-duplicated, never the actor — reported: Gargee sent Mohammed's candidate, RMG chose the manual L1, only Mohammed heard.
`_notify_ta` (shortlist / reject, AI or manual route, L2 request) uses it. (3) **Applied Candidates is ordered by LAST
ACTIVITY**: `resumes._last_activity_by_candidate` (ONE grouped query over the opportunity's profile logs) feeds
`_paginate_merged`, whose key is now max(upload / apply time, last activity) — a candidate RMG just acted on tops the list.
(4) ⚠️ **The round-time drift (the 5:24 am Technical L1)**: the API returns aware UTC instants, and two editors
(`InterviewLadder.EditRoundModal`, the profile's round form) put `scheduled_at.slice(0,16)` — the UTC clock — into a
datetime-local field; saving it back (read as IST by `read_as_ist`) moved the round **5h30 earlier on every edit**. F-V2
`lib/datetime.isoToIstInput` fixes the display; the profile form now sends the typed wall clock, not `toISOString()`.
Rounds already shifted must be re-timed by hand (nothing records which were edited). `manual_round_state`'s
`<round>_scheduled` / `_when` now skip NOT-HELD rounds (a cancelled L1 no longer reads "scheduled" and the rebook button
returns) — the same rule as the tally. (5) **`services/handover_note.py`** — `compose_handover_note(HandoverFacts)` (PURE)
+ `handover_note(db, profile)` (3 queries): the Submit-to-Sales recommendation built ONLY from recorded facts — every
technical round that happened (verdict · interviewer · feedback clipped to `FEEDBACK_CHARS`=220), the completed AI L1
(verdict + score), skill ratings (at / above the required level = strengths, below = "To probe"; unrated skills never
guessed), experience · notice · expected CTC. `GET /api/candidate-profiles/{id}/handover-note` (`rmg_roles`). All three
Submit-to-Sales dialogs pre-fill it (the fixed "L1 & L2 Done - RMG Review Completed…" string is gone).

**29 Sep 2026 — hand-overs, GM positions, customer slots → TA, customer feedback reminders, the HR tail
(`test_rmg_tasks` +1, `test_requirement_positions` +1, `test_ta_decision` +1, `test_interview_followups` +1,
`test_joined_employee_sync` +1 / re-pinned; `test_screening_desk` + `test_screening_notifications` re-pinned; no
migration):** (1) **"Submit to Sales" is empty right after a submission** — the to-do list only holds candidates still in
RMG_Review. `rmg_tasks.recent_handovers(db, days=HANDOVER_DAYS=30)` reads the `RMG_Review -> Sales_Screening`
STATUS_CHANGE rows (`_HANDOVER_PREFIX`) + `FAST_TRACKED`, newest first, with the candidate's current `candidate_status`,
who submitted (`revenue_report._user_names`) and the recommendation; `GET /api/screening-desk/handed-over` (`desk_gate`).
(2) **GM got "Your role cannot view positions"**: `requirement_positions._visible_requirement` now also admits
`screens_as_rmg(db, user)` or `requirements.sees_all_requirements(db, user)`. (3) ⚠️ **Customer slots are a PROPOSAL,
never a booking** (user flow): `_schedule_customer_round_with_move` is GONE — it booked the first slot and mailed the
candidate before anyone asked if they could make it. `routers/crm/candidate_profiles._propose_customer_slots` logs
`CUSTOMER_SLOTS_PROPOSED` (constant in `services/candidate_profiles`, the slots as `customer_slots_text` lines) and tells
the TAs (`candidate.customer_slots_proposed`, in `email_flows.EVENTS`); TA books the round from Applied Candidates, which
invites the candidate and moves the status. `_CUSTOMER_STAGE_ROUND_KIND[L1_Feedback]` is now `Customer_L2` (the next round
to line up). The work desk's schedule tab prints "customer offered <first slot> (+N more)" (`work_desk._customer_slots`).
(4) **`candidate_profiles.ta_user_ids(db, profile)`** — owner + the TA who sent for screening — is now a SERVICE (the
router's `_ta_recipients` delegates). Customer-round feedback reminders go to `interview_followups.CUSTOMER_ROUND_ROLES`
(Sales · Sales_Head · **Sales Manager**) + those TAs; `areas_for` gives a Sales Manager the customer rounds;
`ROUND_WRITE_ROLES` customer kinds add "Sales Manager" so they can record the verdict. (5) HR's out-of-budget flag notifies
Sales_Head + **Sales Manager** (`notify_roles`) + the submitter; `budget-resolve` checks `user_may("profile.budget_resolve")`
instead of a hard-coded Sales / Sales_Head role list. `ensure_employee_for_joined_profile` also finds an internal
candidate by `Employee.personal_email` (same first name), the key the Screening Desk already uses. **`GET /api/employees`
now defaults to `recently_updated`** (`DEFAULT_EMPLOYEE_SORT`, user ask "latest first"). Suite: 1,653 pass, 2 skipped.

**29 Sep 2026 (later) — TA books the customer round FROM the slots Sales passed on (`test_ta_decision` +1; no
migration):** the slots lived only as text in the `CUSTOMER_SLOTS_PROPOSED` activity row, so TA had to re-type them.
`services/candidate_profiles` now owns the ONE shape: `fmt_slot_ist` (the router's `_fmt_slot_ist` is an alias),
`customer_slots_text`, `customer_slots_comment(kind, sched, note)` (what is logged) and its PURE inverse
`parse_customer_slots(comment)` → `{kind, slots[{scheduled_at "YYYY-MM-DDTHH:MM" IST | None, label, meeting_link}],
interviewer, duration_minutes, note}` — the round-trip is pinned (panel names may hold commas; an unparseable time keeps
its label with `scheduled_at` None). `latest_customer_slots(db, ids)` (one query, + `proposed_at` / `proposed_by_id`)
feeds `manual_round_state` (`customer_slots` on every Applied Candidates row and the profile detail),
`GET …/interview-rounds/options` (`customer_slots` — TA's Schedule form lists them as picks) and the work desk's
"customer offered …" subtitle (`work_desk._offered`, only when the offer is for THAT round — an L1 offer never
labels the L2). Feedback after the round is unchanged server-side (`interview_followups`: Sales · Sales Head ·
Sales Manager + the TAs, daily, and the My Tasks feedback tab). Suite: 1,654 pass, 2 skipped.

**29 Sep 2026 (evening) — TA's three "to schedule" tabs, customer link mandatory, the Sales Manager has the whole of
Sales, migration 0112, head is now 0112 (`test_work_desk` +1 / re-pinned, `test_custom_roles` +2):** (1) **`work_desk`**:
the single "To schedule" tab is now THREE — `schedule_customer` (Customer L1 / L2) · `schedule_internal` (AI L1 ·
Technical L1 / L2) · `schedule_hr` (`SCHEDULE_TABS`, `SCHEDULE_ROUNDS` label → (tab, round kind), PURE
`rounds_to_schedule`). Every item carries `round_kind` (what the UI books in place; `AI_L1` for the AI link),
`customer_slots` (only for THAT round) and `candidate_email` (placeholders never). ⚠️ **TA scope is
`ta_works_clause`**: the TA owner OR the TA who pressed Technical Screening (SQL twin of `ta_user_ids`) — reported: a
TA did not see the customer interview of a colleague's candidate she had sent. (2) **A customer round SCHEDULED without
the customer's meeting link is refused** — `interview_rounds.require_customer_meeting_link(values)` on round CREATE
(400; `CUSTOMER_ROUND_KINDS`); a round recorded with a verdict needs none. (3) ⚠️ **`services/role_implications.py`**:
`ROLE_IMPLIES = {"sales manager": ("Sales", "Sales_Head")}` — the ONE place a custom role carries built-in roles.
Read by `crm_deps.get_current_user` (`with_implied` → every role check, `role_required("Sales_Head")`, `/api/me.roles`),
`action_permissions.default_actions_for_role` (a never-configured / newly created Sales Manager role approves what Sales +
Sales Head approve), `user_ids_who_may`, `notify._user_ids_in_role` and `recipients.role_recipients`
(`custom_roles_implying` — a Sales Head / Sales notice reaches the Sales Manager). **0112** updates the saved data: every
custom role NAMED Sales Manager and template TAGGED Sales Manager gets the tab / field grants of the templates tagged
Sales_Head / Sales merged in (higher rung wins, nothing lowered), and a SAVED Approvals list gains the five Sales / Sales
Head approvals (`_SALES_APPROVALS`, a snapshot pinned against the registry). Downgrade is a no-op. Deploy:
`alembic upgrade head`, restart. Suite: 1,657 pass, 2 skipped.

**29 Sep 2026 — upload form: Current location + Note (`tests/test_upload_form_location_note.py`, 3; no migration):**
`POST /api/requirements/{id}/resumes` takes two more optional Form fields, `current_location` (≤ 120) and `note`
(≤ 1000). Both are kept on `resume.application_details` (the Applied Candidates row prints them). `current_location`
fills the candidate's **`city` only when it is empty** (`slot_booking.find_or_create_candidate_from_resume`), so a
known candidate's city is never overwritten. The note is also logged on the profile's Activity Log as `TA_NOTE`
("TA note on upload: …") by `slot_booking.record_upload_note(db, profile, note, user_id)`. A blank note or no profile
writes nothing. It runs inside the upload's best-effort candidate-bootstrap block, right after
`ensure_sourcing_profile`, whose return value is now used. `education` / `technical_domain` / `skills` are still
accepted: the redesigned F-V2 form no longer shows them but forwards what the parser read.

**29 Sep 2026 — Finance's work desk = the billing chain (`tests/test_work_desk.py` now 8; no migration):** user ask —
three Finance tabs before Upcoming: approved timesheets, the GM's Proformas, and the original invoices.
`work_desk._finance_tabs(db)` runs four batched queries:
- **`fin_timesheets`** "Approved timesheets": Approved sheets from the last `FIN_TIMESHEET_DAYS`=90 (by
  `approved_at`, else `updated_at`) whose latest invoice row is none (chip "Waiting for GM's Proforma") or a RETURNED
  Proforma (chip "Returned to GM", amber). The 90-day window exists because a sheet invoiced by hand, with no
  `timesheet_id`, would otherwise sit on the list forever.
- **`fin_proformas`** "Proformas to convert": `kind = Proforma` and not returned. Title is the PI number plus the
  amount; the item turns amber after `PROFORMA_WAIT_DAYS`=3 and red after 6; action "Generate tax invoice" →
  `invoices/{id}`.
- **`fin_invoices`** "Tax invoices issued": Tax invoices dated in the last `FIN_ISSUED_DAYS`=30, newest first, with
  the payment status as the chip and "from PI-…" when a Proforma preceded it.
Tabs carry **`stage`** (Coming up · Your move · Done) and **`info`** (`TAB_STAGE`): Proformas are the only tab that
counts as waiting on Finance. The tabs are given to the Finance role, and to a non-admin template or custom role that
may `invoice.convert_proforma`. Admin/CEO keep the company view. `work_desk.rupees()` prints ₹ with Indian grouping
(via `tax_invoice.format_inr`).

**29 Sep 2026 (night) — Customer L2 hand-off to TA, Sales opens any requirement, migration 0113, head is now 0113
(`test_customer_round_autostatus` re-pinned +2, `test_transition_note_and_offer` re-pinned, `test_ta_decision` re-pinned,
`test_candidate_status` +1, `test_work_desk` +1, `test_opportunity_stage_cascade` +1):** screenshot report — Sales moved a
candidate to "Customer L2 Interview" (stored `L2_Feedback`) with slots and the note "move to L2"; TA got nothing to do and
the row read "Customer L2 – Scheduled · Time not set". Cause: ⚠️ `_record_customer_round_from_transition` wrote a round on
ARRIVAL at L1/L2 Feedback (kind Customer_L2, status Completed, no time / link / verdict) — it counted as held, so
`cust_l2_scheduled` hid TA's button and the desk item, and "Feedback due" showed on a round that never happened. Now:
(1) arriving at L1/L2 Feedback writes NO round (the note stays in the STATUS_CHANGE activity row); closing the ladder
(Shortlisted / Customer Approval / customer rejections) still records the verdict on the latest customer round.
(2) `comment_required_for` no longer requires a note on the move to L1/L2 Feedback (user decision: "is Feedback really
required? if not remove") — only rejections, backward moves and the ladder's closing verdict. (3) `candidate_status._customer`:
at L2_Feedback with no Customer L2 round → **"Customer L2 – Yet to Schedule"** (`customer_l2_pending`; `_round_defs` for
customer_l2 now has `pending`). (4) `work_desk.CUSTOMER_L2_STAGES` = (L1_Feedback, L2_Feedback): the L2 is to schedule at
either (desk item + `_profiles` stage list). (5) `routers/crm/candidate_profiles._propose_customer_slots` now runs on EVERY
move into a customer-round stage: with slots as before; without slots TA is still told ("agree the time with Sales … My
Tasks ▸ Customer interviews", Sales' note appended) unless that round is already booked (a held/scheduled row of that kind,
`NOT_HELD_STATUSES` excluded). **0113** deletes the placeholders already written — kind Customer_Interview / Customer_L2,
status Completed, no scheduled_at / raw_when / link / result / interviewer / Zoho ids, user_role Customer — after copying
their text to the activity log as `CUSTOMER_NOTE`. Downgrade no-op. (6) ⚠️ **`requirements.ensure_visible` admits every Sales
user** (report: Sanjana's link to requirement 82 → 404 "Requirement not found" because a colleague raised the deal). Every
Sales user opens every opportunity, so they open its requirement (same rule as `requirement_positions._visible_requirement`);
the Sales LIST (`apply_visibility`) stays own-deals and writes keep their creator / approval checks. Deploy:
`alembic upgrade head`, restart.

**29 Sep 2026 (late) — Sales' work desk, "Terms Sent Back", budget statuses, approvers hear the approval
(`tests/test_work_desk.py` now 11: +3; no migration):** user ask — "after Feedback due, tabs with all the work Sales
has to do, so Sales never opens the candidate profile". **`work_desk._sales_tabs(db, user, is_admin=)`** (builder right
after `feedback`, for Sales / Sales_Head / the Sales Manager (implied) / Admin / CEO): one tab per stage Sales owns —
**`sales_submit`** (Sales_Screening) · **`sales_response`** (Customer_Screening; amber past `CUSTOMER_WAIT_DAYS`=3, red
past 6) · **`sales_decide`** (a customer stage whose DERIVED status is a verdict — `_SALES_DECIDE`: customer L1 / L2
passed · failed · review; a round still to happen is not a decision) · **`sales_terms`** (Shortlisted; chip "Sent back by
Sales Head" when `terms_sent_back`) · **`sales_approval`** (Customer_Approval, for whoever `user_may`
`profile.sales_head_decision`) or **`sales_waiting`** (the same rows, `info`, for everyone else) · **`sales_budget`**
(`budget_status = Out_of_Budget`, for `user_may(profile.budget_resolve)`). Every item carries what the move needs —
`current_status` + `allowed` (the SAME `allowed_next_statuses_for_user` the profile's Next-step bar uses, minus
`_DESK_EXCLUDED_MOVES` = Customer_Approval (that is the terms dialog) + Self_Withdrawn), the pending `offer`
(`_pending_offers`), `expected_ctc` / `current_ctc`, `approved_ctc_budget` + `ctc_slab_band` (`approved_ctc_budgets`),
`opportunity_label`, `status_label`, `days_waiting` (from the latest `STATUS_CHANGE`, `_since_status_change`) and, for
budget, `hr_note`. Five batched queries for the desk. **Scope** (`sales_scope`): a plain Sales user sees the deals they
raised (`Opportunity.created_by`) plus any candidacy they have an activity row on; Sales Head / Sales Manager / Admin see
all. `TAB_STAGE` captions: Your move · With the customer · Your approval · Waiting (info). **Statuses**
(`candidate_status`): `terms_sent_back` "Terms Sent Back" (Shortlisted, WARN) — `sales_head_decision` now logs
**`OFFER_SENT_BACK`** (`TERMS_SENT_BACK`) on send-back and `load_facts` reads it against `SUBMITTED_FOR_APPROVAL`
(`TERMS_SUBMITTED`) — the later one wins, so a resubmission clears it (`StatusFacts.terms_sent_back`); the
Pre-Onboarding budget hold now has words: `budget_concern` "Pre-Onboarding – Budget Concern", `budget_flagged` "Out of
Budget – With Sales" (BAD), `budget_replied` "Budget Reply – With HR" (`BUDGET_*` constants mirrored from
`candidate_profiles`, pinned equal by a test; stages Preboarding + HR_Interviewing). **Notifications**:
`_notify_stage_owner` now also passes `user_ids=` for stages decided by an approval (`_ARRIVAL_APPROVAL`:
Customer_Approval → `user_ids_who_may("profile.sales_head_decision")`, actor excluded), so a Sales Manager / GM whose
approval comes from a template or custom role hears "Pending Sales Head Approval". Wording: the approval moves the
candidate to **HR Discussion** (the docstring said Preboarding). Suite: 1,669 pass, 2 skipped.

**29 Sep 2026 (night) — the same mail twice (`tests/test_duplicate_notifications.py`, 4;
`test_screening_notifications` re-pinned; no migration):** reported — one person got "AI interview completed: X" and
"AI L1 passed — review X" TWICE each for one interview. (1) `ai_interview_bridge.sync_completed_interview` runs on every
report save (quick fallback · AI upgrade · finalize) and its `already_synced` guard needs an IDENTICAL score, so the
upgraded score re-announced everything. Now `announce = first completion OR the verdict changed`; a new score with the
same verdict updates the link quietly (no activity row, no mail). Dedupe keys `ai_done:<link>:<result>` /
`ai_review:<link>:<result>`. The "completed" notice goes to the candidate's OWN TAs (`_candidate_tas` →
`ta_user_ids`) minus the screeners (they get the review mail) — the whole TA role only when the profile has none.
(2) **A safety net under EVERY notice**: `email_outbox.is_recent_repeat` — `queue_email` drops the same subject to the
same address within `REPEAT_WINDOW_MINUTES`=30, except `NEVER_COLLAPSE_EVENTS` (reset / invite / candidate links /
support replies / direct messages — resending those is the point); `notify._add_bell` drops the same title + link to
the same user within `BELL_REPEAT_MINUTES`=30 (notify_user · notify_roles · notify_employee). Both savepointed.
⚠️ A saved Email Flows row for `ai_interview.completed` still REPLACES the role list (admin choice).

**29 Sep 2026 (night) — no automatic candidate mail on a scan, TA closes tell RMG / GM, HR's desk, placements for HR
(`test_screening_desk` +1 / re-pinned, `test_ta_decision` +1, `test_work_desk` now 12, `test_executive_dashboard` now 19;
no migration):** (1) ⚠️ **`slot_booking.auto_pipeline_after_scan` is GONE** — reported: a candidate added from the
Candidates tab and applied to an opportunity got the "pick your interview slot" mail. The manual scan routes (per-row
`ats-scan`, `ats-scan-profile`, `scan-all` = "Score N pending") ran it, auto-shortlisting at `ats_auto_threshold` and mailing
the slot invite. Every candidate mail is now a TA action (Slot invite · Schedule AI L1). `ResumeScanResult` loses
`auto_shortlisted` / `slot_invite_sent`; `seed_crm` drops the `ats_auto_threshold` / `ats_auto_invite` rows (existing rows
are inert). Pinned: `test_no_scan_route_mails_the_candidate` (source scan) + the module no longer has the function.
(2) **TA Reject / Self Withdraw tell every screener**: `candidate_profiles.notify_screeners_of_ta_close(db, profile, how,
note, user)` (savepoint; RMG + GM by role PLUS `screening_notify_user_ids`, actor excluded, dedupe
`ta_closed:<id>:<how>`), event `profile.ta_closed` (`TA_CLOSED_EVENT`, in `email_flows.EVENTS`, RMG + GM). (3) **HR's work
desk** (`work_desk._hr_tabs`, for the HR role): `hr_discussion` (HR_Screening; `hr_requested` → action "Request HR round"
or "Open") · `hr_interviews` (info — `HR_Interview` events from an hour ago onward, not held, no verdict; `meeting_link`,
`interviewer`) · `hr_onboarding` (Preboarding; the chip follows `budget_status`: Out_of_Budget "with Sales", Resolved
"Sales replied — your call", Concern, else "Complete onboarding") · `hr_joining` (info — profiles by
`customer_onboarding_date` and employees by future `date_of_joining`, next `HR_JOINING_DAYS`=30) · `hr_joined` (info —
active employees who joined in the last `HR_JOINED_DAYS`=30) · `hr_exits` (info — `last_working_day` in the next
`HR_EXIT_DAYS`=60). Upcoming / queues stay as before. (4) **People tab carries `placements`**
(`executive_dashboard._people_placements` → `placements_report` for the same anchor + zoom; window · headline · series ·
by_customer · rows · rules with every revenue / billed field STRIPPED — HR reads it through `GET /api/dashboard/people`;
`None` on failure, logged). Suite: 1,673 pass, 2 skipped.

**29 Sep 2026 (night, later) — Submit-to-Sales checklist + TA location reminder (`tests/test_sales_readiness.py`, 5;
no migration):** (1) `services/handover_note.sales_readiness(db, profile)` — what Sales needs (Current / Expected CTC ·
notice · experience · current + preferred location · phone · real email · CV · a technical verdict · skill ratings
(optional)), each `{key,label,value,ok,required,field,input,hint}`; returned as `checks` by `GET …/handover-note`.
`PATCH /api/candidate-profiles/{id}/sales-details` (`rmg_roles`) writes ONLY `SALES_DETAIL_FIELDS` (CTCs arrive in LAC,
stored in rupees; experience on the profile; notice / city / preferred / phone on the candidate), logs `SALES_DETAILS`,
returns the refreshed checks. A gap never blocks the submit. Also fixed: the note printed "expects 2500000 L" —
`to_lac()` (≥ 1,000 = rupees). (2) `slot_booking.remind_missing_location(db, profile, user_id)` (+ pure
`missing_locations`): bell + email to the TA who added a profile whose Candidate Location / Preferred Location is blank
(event `profile.location_missing`, in `email_flows.EVENTS`, dedupe per profile); called by `POST /api/candidate-profiles`
(the reply message names what is missing) and the single resume upload — NOT the bulk ZIP (one mail per CV would flood).

**29 Sep 2026 (latest) — the Sales ladder, "Submit to Sales Head", Sales' billing tabs (`test_work_desk` now 13,
`test_custom_roles` re-pinned; no migration):** (1) ⚠️ **The Sales Manager is a RUNG, not a copy of the Sales Head**
(user rule: "Sales limited, Sales Manager more than Sales, Sales Head all of Sales"; reported: Balasaheb, a Sales
Manager, showed Sales · Sales Manager · Sales Head chips). `role_implications.ROLE_IMPLIES["sales manager"]` is now
`("Sales",)` only — every Sales check passes, no `role_required("Sales_Head")` gate does. What lifts them above Sales:
`TEAM_ROLES` / `sees_team(roles)` (whole-team deals — `requirements.sees_all_requirements`, `work_desk` Sales scope),
`APPROVAL_DEFAULTS_FROM` (`approval_default_roles` → `action_permissions.default_actions_for_role`: a never-configured
role approves what Sales + Sales Head approve; saved Approvals from 0112 unchanged) and `HEARS` (`custom_roles_implying`
→ Sales Head notices still reach them). `CurrentUser.held_roles` = roles actually held (before implication); `/api/me`
adds **`display_roles`** (the chips) and **`sees_team`**. (2) `sales_terms` is now **"Submit to Sales Head"** with
item `section`s — `TERMS_SENT_BACK` · `TERMS_TO_SUBMIT` · `TERMS_WAITING` (the Customer_Approval rows, action Open);
the count is what Sales still has to send; the separate `sales_waiting` tab is gone (the approver keeps
`sales_approval`). (3) `_sales_billing_tabs(db, user, everyone=)` — `sales_timesheets` (Draft ≤ this month · Rejected ·
Submitted; red when the month ended > `SHEET_LATE_DAYS`=5 ago; count = Draft + Rejected), `sales_invoices_pending`
(Approved sheets with no issued invoice / a returned Proforma, + Proformas with Finance), `sales_invoices` (Tax
invoices, last `SALES_INVOICES_DAYS`=90, Overdue chip) and `sales_collections` (Tax, unpaid, past due — Sales owns
the customer) — every item's `section` is the CUSTOMER. Scope: a plain Sales user sees projects of the deals they
raised + sheets they acted on (`TimesheetActivityLog`); Sales Manager / Sales Head / Admin see all.

**29 Sep 2026 (late) — AI report links, AI Costs made complete, HR desk v2, Candidate Profiles directory, error sweep
(`tests/test_ai_interview_costs.py` now 29, `tests/test_work_desk.py` HR test re-pinned, `tests/test_profile_round_columns.py` 3,
`test_screening_desk` re-pinned; no migration):** (1) **"Not Found" on the AI verdict card** was the CLIENT calling
`/candidate-profiles/…/summary` without `/api` (F-V2 fix). **`services/report_links.ai_report_link(email, record)`** is now
the ONE builder of `/admin/?view=candidateReport&cid=…&iid=…` (six hand-formatted copies, raw email — a `+` read as a space)
used by candidate_profiles · interview_calendar · resumes · ai_interviews · routers resumes · slots. (2) ⚠️ **AI Costs read
"0 interviews / ₹2"** because chat calls before 28 Sep carried no `interview_id` and speech was never logged — the ledger,
not the maths. **`services/ai_cost_repair.py`**: `attribute_orphan_calls` gives each unattributed interview-kind call
(`INTERVIEW_CALL_PREFIXES`) the ONE `interview_progress` session whose window (created − 5 min → finalized/last activity
+ 45 min) holds it — ambiguous overlaps are left alone; `estimate_missing_audio` writes one `tts_estimated` + one
`transcribe_estimated` row (`status = "estimated"`, ids `est-<kind>-<interview>`, idempotent) per FINISHED interview created
before `AUDIO_LOGGED_SINCE` (2026-09-28) that has no audio rows — question characters ÷ `TTS_CHARS_PER_SECOND`, answer words ÷
`SPEECH_WORDS_PER_SECOND` (2.5), priced per minute. Runs at startup (background thread in `main.py`) and inside the daily
`prompt_log_retention` job. The report carries `estimated_usd` per interview + `summary.estimated_usd /
estimated_interviews`; `OTHER_FAMILIES` names report-analysis and unmatched interview calls. Two calls that bypassed the
ledger now go through `tracked_chat_completion`: the legacy CV parse (`resume_parse_cv`, `ai.py`) and the ATS semantic
review (`ats_semantic_review`, `services/resumes.py`). (3) **HR desk v2** (`work_desk`): an HR-only login no longer gets
Upcoming / My queues (`hr_only`); new `_hr_people_tabs` — `hr_leave` (Pending leave applications, chip by start date) ·
`hr_records` (active employees missing any of `HR_REQUIRED_FIELDS`: CTC · Emp ID · Joining date · Designation · Reporting
manager · Date of birth) · `hr_bench` (`deployment_by_employee` = Bench) · `hr_celebrations` (birthdays + work anniversaries
in `HR_CELEBRATION_DAYS`=14, `_next_occurrence` handles 29 Feb). (4) **Candidate Profiles directory**: every list row carries
`rounds` {tech_l1 · tech_l2 · tech_l3 (L3/L4) · cust_l1 · cust_l2 · hr → latest HELD round: when · result · status ·
interviewer · mode · feedback (≤ `ROUND_FEEDBACK_CHARS` 220) · upcoming} and `next_interview` — `round_ladder` (PURE) over
ONE `_interview_rows` query shared with `latest_interviews` (whose per-row count was O(n²)); a customer round stored as
`Customer_Interview` with stage "L2" is the customer's second round (`round_column_key`). `GET /api/candidate-profiles?
with_phase_counts=true` adds `meta.phase_counts` — every stage incl. Closed, computed on the filtered query BEFORE the
bucket and the phase (`pre_bucket`), so each chip counts what clicking it lists. `TABLE_REGISTRY["candidate_profiles"]` gains
`phase` (the derived stage — NOT the removed Zoho "stage" key), `next_interview`, `round_*`, announced visible (chained
after `customer` / `pipeline_status` / `ats_score`); `round_tech_l3` stays opt-in. (5) **Error sweep**: server log — the
summary 404s above and 137 `asyncio … _ProactorBasePipeTransport._call_connection_lost()` ERRORs in one afternoon
(Windows, a browser dropping a keep-alive socket) — `logging_setup.ProactorDisconnectFilter` drops exactly those, and
**`JsonFormatter` now writes the traceback (`exc`)** — every `logger.exception` / `exc_info=True` used to reach the log
without it. `services/candidates.py` returned `created_at` twice in one dict (first copy removed). Frontend ↔ backend route
contract cross-checked (every `crm*()` call against the app's 640 routes): the summary path was the only broken one.
⚠️ Found in flight, NOT mine: `services/opportunities.CASCADE_PROTECTED_STATUSES` was narrowed to Fulfilled/Closed/Cancelled
by a concurrent edit at 17:42 IST and `test_opportunity_stage_cascade` (2 tests) fails against it until its tests follow.
Suite otherwise: 1,687 pass, 2 skipped.

**29 Sep 2026 (last) — a deal's stage reaches every login; TA applies only to sourcing deals, migration 0114, head is
now 0114 (`tests/test_opportunity_stage_cascade.py` now 32; `test_screening_desk` re-pinned):** user report — "if Sales
closes or holds an opportunity it does not reflect in every login", and TA's Apply to Opportunity listed C-2026-00099 /
00098, which neither the Sales Head nor RMG had approved. (1) ⚠️ **The cascade now moves PRE-SOURCING requirements too**
(`services/opportunities.py`): `CASCADE_PROTECTED_STATUSES` is only Fulfilled · Closed · Cancelled; new
`PRE_SOURCING_STATUSES` joins `_HOLDABLE_BY_CASCADE`. A deal closed while its requirement waited on an approval used to
sit in the RMG Review Queue / Screening Desk approvals strip for ever — now closing settles it (Closed / Cancelled per
`STAGE_CLOSES_REQUIREMENT`) and a hold pauses it with `held_from_status`, so Reactivate hands it back to the exact
approval step (the RMG resume endpoint already restores any `held_from_status`). This SUPERSEDES the 22 Sep rule "a deal
that never reached sourcing must not fabricate a Closed requirement". (2) `services/requirements.py`: `ta_visible_clause()`
/ `ta_may_see(req)` replace the bare `status IN TA_VISIBLE_STATUSES` in `apply_visibility` / `ensure_visible` — a hold
whose `held_from_status` is a pre-sourcing value stays hidden from TA (it never reached them). New `SOURCING_STATUSES`
(Open_For_Sourcing · Posted_On_Portals · In_Progress), `sourcing_opportunity_clause()` (stage New / Active AND a
requirement in sourcing) and `recruiter_only(user)` (TA holding none of Sales · Sales_Head · RMG, not admin).
`GET /api/opportunities?sourcing=true` narrows to open deals (the Apply picker sends it for a recruiter-only TA);
`POST /api/candidate-profiles` refuses a recruiter-only TA on any other deal (400, names why). (3) `GET /api/requirements`
`status` is now a CSV (one paginated request — TA's Active tab asks for the three sourcing statuses). (4) **0114**
repairs existing data with the same rules: requirements of closed / rejected / archived deals → Closed / Cancelled (unless
already settled), of Customer / Sales Hold deals → On_Hold with `held_from_status`. Downgrade no-op. Also confirmed with
the 29 Sep ladder: a Sales Manager (roles Sales + Sales Manager) no longer auto-approves their own opportunity
(`privileged = has_any("Sales_Head", "Admin")`), cannot archive, cannot set hiring targets (`role_required("Sales_Head")`)
and does not get the Sales Head dashboard section. Deploy: `alembic upgrade head`, restart.

**29 Sep 2026 (night, last) — filters on every My Tasks tab (`tests/test_work_desk.py` now 15, `test_rmg_tasks`
re-pinned; no migration):** user ask — "Sales: timesheets customer-wise, month-wise, search; invoices pending and
generated need filters; Finance the same; apply what fits to every role". The server gives every desk item its FACETS:
`customer`, `month` ("YYYY-MM") and, on the billing tabs, `project` — `work_desk._facets(customer, project, year,
month, day=)`: a timesheet item's month is its PERIOD, an invoice's is its `invoice_date`, never the day it was touched
(Sales billing + Finance builders). `fill_facets(db, tabs)` (in `desk()`, savepointed) fills the rest: a candidate item
takes its opportunity's customer (ONE query for the whole desk), any item's month falls back to `when[:7]`. ⚠️
Truncation moved from `_tab` / `_screener_tabs` to the END of `desk(db, user, max_items=MAX_ITEMS)` (count still taken
first); `GET /api/dashboard/desk?full=true` passes `FULL_MAX_ITEMS`=500 — the My Tasks page asks for it so its filters
never work on the first 50 alone; the Dashboard tiles keep 50. The RMG / GM categories stay capped at 50 by
`rmg_tasks` (their full list is the Screening Desk, which has its own filters). Pinned:
`test_every_item_carries_filter_facets_and_my_tasks_gets_the_whole_list`.

**29 Sep 2026 (night, after filters) — Technical-screening hand-off date + half day in Present Days, migration 0115,
head is now 0115 (`test_ta_decision` +1 assertion, `test_midmonth_timesheet` +1):** (1) Screenshot report — a candidate
already with Sales and the customer read "Technical screening — Not yet" in the profile's Hand-offs. Since the 28 Sep flow
the profile stays at the Sourcing STAGE while RMG / GM screen it, so `_STAGE_DATE_STAMPS` (stage moves only) never fired.
`candidate_profiles.stamp_technical_submission(profile)` (fills a blank only) now runs on every hand-over: `send_for_screening`,
the RMG / GM screening decision (`POST …/rmg-screening`), a screener's own add (`POST /api/candidate-profiles`) and the
internal fast-track (`screening_desk.fast_track_internal`). **0115** backfills blanks from the earliest `SENT_FOR_SCREENING`
row, else the earliest of the screening decision (`RMG_SCREENING` / `FAST_TRACKED` row, `rmg_screening_at`) and
`sales_submission_date`, else — for a profile with any screening status — the apply date. Downgrade no-op. (2) User rule: a
HALF DAY counts 0.5 in Total Present Days, the same as Total Billable Days (21 full + one 4-hour half day = 21.5, was 21).
`timesheet_summary` `present_days` = full Present rows + 0.5 × `half_days` (now a float); `half_days` still counts the rows,
so payroll's "Present" column is days and "Half Days" is how many. Deploy: `alembic upgrade head`, restart.

**29 Sep 2026 (night) — Candidate Profiles: latest change first (`tests/test_candidate_profiles_list_enrichment.py` +1;
no migration):** user ask — "latest change first on top". `routers/crm/candidate_profiles._LAST_ACTIVITY` = the newest
`candidate_profile_activity_log.timestamp` of the profile (every stage move, screening call, round verdict and note writes
one), else `updated_at`, else `created_at` — a correlated scalar, sort key **`last_activity`** in `_SORTABLE` and in
`TABLE_REGISTRY["candidate_profiles"]["sortable"]`. F-V2 makes it the directory's default order.

**29 Sep 2026 (night, last) — Sales desks trimmed (`tests/test_work_desk.py` re-pinned; no migration):** a Sales-family login (Sales · Sales Manager · Sales Head, with no TA / screener / Finance / HR role) no longer gets the generic **Upcoming** / **My queues** tabs (`sales_only` beside `hr_only` in `work_desk.desk`); a real Sales Head (`"Sales_Head" in held_roles`, not Admin/CEO) no longer gets **"Submit to Sales Head"** (`sales_terms`) — they keep **`sales_approval`**; Sales and the Sales Manager keep it.

**29 Sep 2026 (night, GM desk) — the GM's billing chain on the board, off-desk task items, more desk filters
(`tests/test_rmg_tasks.py` +1, `tests/test_screening_desk.py` +2; no migration):** (1) **`work_desk.billing_chain(db,
audience="finance"|"gm")`** is now the ONE timesheet → Proforma → tax-invoice query set (`submitted` · `awaiting` ·
`proformas` · `issued`, five batched queries, facets on every item); `_finance_tabs` reads it with Finance's words and
the GM gets his own (`_CHAIN_WORDS`: "Approved — raise the Proforma" / "Returned by Finance — reissue" → Raise / Reissue
Proforma → `timesheets/{id}`; "With Finance" → `invoices/{id}`). `rmg_tasks.CATEGORIES` gains four page categories —
**`ts_approve`** (Submitted sheets, oldest first, "Review & approve"), **`proforma_raise`**, **`proforma_finance`**,
**`invoices_issued`** — present only when `user_may` the approval in `BILLING_CATEGORIES` (`timesheet.approve` /
`timesheet.generate_invoice`; `_billing`, savepointed); `INFO_CATEGORIES` (the last two) never count in `total`. Every
category now carries `info` + `always`; `work_desk._screener_tabs` keeps `always` tabs at zero and adds `info` + a
`stage` caption (`_SCREENER_STAGE`), so the GM's Dashboard shows the same tiles, each linking
`screening-desk?task=<key>`. (2) **"Results to review 1" opened an empty queue**: a desk category may hold candidates who
are PAST the desk (with Sales / the customer) — `desk_ids` never included them, so `?task=` filtered to nothing. The
server is unchanged (their items already link to the profile); F-V2 lists them under the board with their links and a
"Mark reviewed" for results. (3) **`screening_desk.DeskFilters`** gains SQL filters `budget` over / within / unknown
(expected CTC — profile, else candidate — vs `Requirement.budget_ctc_max`), `priority` High / Medium / Low,
`waiting_min` (days since applying, against `date.today()`), `exp_min` / `exp_max`; and DERIVED ones read per row in
`_derived_keep` — `notice` 15 / 30 / 60 / 90 / unknown (PURE `notice_days` / `notice_bucket` parse "Immediate", "30
days", "2 months", "3 weeks"), `ai_result` passed / failed / pending / none (`ai_result_bucket` over
`latest_ai_interviews`), `l1_result` hire / no_hire / awaiting / none (`l1_result_bucket` over `manual_round_state`). Bad
values and exp_min > exp_max → 400. All on `GET /api/screening-desk`. Suite: 1,699 pass, 2 skipped.

**29 Sep 2026 (night, very last) — a Dashboard per Sales rung (`tests/test_work_desk.py` now 15; no migration):**
`work_desk.sales_rung(user)` → `sales` | `manager` (`sees_team`) | `head` (`"Sales_Head" in held_roles`) | None (Admin/CEO,
unchanged). Every rung loses `sales_invoices_pending` + `sales_invoices` (the builder still makes them; Admin keeps them).
`SALES_RUNG_TABS` / `_sales_rung_tabs`: **Sales** + `renewals` (Active POs ending ≤ `RENEWAL_PO_DAYS`=45 and
`bench_rolloffs(RENEWAL_ROLLOFF_DAYS=90)`, scoped to customers of deals they raised, section = customer) · `joining_soon`
(their candidates with `customer_onboarding_date` in the next `SALES_JOINING_DAYS`=30, via `sales_scope`); **Sales Manager**
the same for the whole team + `team_stuck` (with Sales > `TEAM_SALES_WAIT_DAYS`=2 d / with the customer >
`CUSTOMER_WAIT_DAYS`=3 d since the last STATUS_CHANGE) · `team_timesheets` (Draft / Rejected for months already ended) ·
`team_collections` (Tax, unpaid, past due) — all sectioned by the SALESPERSON (`Opportunity.created_by`); the Manager's
own `sales_timesheets` / `sales_collections` are now own-deals only (`billing_everyone`). **Sales Head** drops
`sales_submit` / `sales_response` / `sales_collections` (and `sales_terms`, above) and gets `head_pace` (the hiring
tower's quarter pace — `hiring_dashboard(...)["pace"]`, never the Admin-only revenue figures) · `head_stuck`
(`team_overview` stuck points + salespeople with stalled deals) · `head_lost` (deals Closed_Lost / Rejected and candidates
Customer_Rejected this month) · `head_collections` (top `HEAD_TOP_COLLECTIONS`=10 by balance, count = all overdue) ·
`head_po_renewals`. ⚠️ New keys must NOT start with `sales_` — F-V2 `isSalesTab` treats that prefix as an action tab.

**30 Sep 2026 — Edit applicant can set the current location (`tests/test_upload_form_location_note.py` now 4; no
migration):** `routers/crm/resumes.ResumeUpdateIn.current_location` (≤ 120) + `_DETAIL_KEYS` — the redesigned F-V2
Edit applicant dialog edits the same `application_details.current_location` the upload form writes (a profile-only
row saves it to the candidate's `city`). Everything else this round is F-V2 (the dialog kit and the wizard chrome —
see that repo's notes). Suite: 1,701 pass, 2 skipped.

**30 Sep 2026 — the Admin / CEO desk is only their own decisions (`tests/test_work_desk.py` now 16; no
migration):** screenshot report — the CEO's "My work today" carried every role's tabs (579 tasks: TA scheduling,
RMG screening, Finance's chain, HR …). `work_desk.desk` now short-circuits for Admin / CEO to **`CEO_TABS`** =
`opp_approvals` (new `_opp_approvals_tab`: opportunities at `Pending_Sales_Head_Approval`, oldest first, "Waiting N
days", customer facet, → `opportunities/{id}` "Review & approve") + `sales_approval` (candidate terms at
Customer_Approval, taken from `_sales_tabs` so the Approve / Send back / Reject moves stay in place). Nothing else —
no feedback / schedule / sourcing / screener / finance / HR / upcoming / queues; every one of those is some role's
day and lives on that role's Dashboard. Every other login is unchanged.

**30 Sep 2026 — RMG / GM "Direct to Sales" from the Screening Desk (`tests/test_screening_desk.py` now 33; no
migration):** user ask — when TA adds a candidate who closely matches the position, RMG / GM can send them straight
to Sales for the customer round, reason mandatory. `services/screening_desk.direct_to_sales(db, profile, note, user)`
(reason ≥ `MIN_FAST_TRACK_NOTE`=10 → 400; from `DIRECT_TO_SALES_FROM` = the desk stages only → 409, PURE
`direct_to_sales_block`) shares the ONE move with the internal fast-track — `_send_straight_to_sales(…, who, tag,
why)`: screening stamped Shortlisted, `stamp_technical_submission`, → Sales_Screening, `FAST_TRACKED` +
`STATUS_CHANGE … [direct to Sales] <reason>` rows, `record_stage_arrival` (Sales notified), `_tell_ta(why=)`.
`POST /api/candidate-profiles/{id}/direct-to-sales {note}` behind `rmg_roles` (the `profile.rmg_screening` approval —
no new action). Desk rows and `GET /api/candidate-profiles/{id}` carry `direct_to_sales_block`. The internal
fast-track (`profile.fast_track_internal`) is unchanged.

**30 Sep 2026 — HR's Offered CTC at Pre-Onboarding, migration 0116, head is now 0116 (`tests/test_hr_offer.py`, 6):**
user rule — "when the candidate is at Pre-Onboarding, HR must add the Offered CTC after the HR discussion / round; that
tab is visible to HR only." Four columns on `candidate_profiles` — `hr_offered_ctc` (annual rupees) · `hr_offered_at` ·
`hr_offered_by` · `hr_offer_note` — read and written ONLY through **`services/hr_offer.py`**: `may_see(user)` (HR by role,
or Admin/CEO — ⚠️ a template grant never widens it), `edit_block(profile)` / `may_edit` (`EDIT_STAGES` = Preboarding only;
`VISIBLE_STAGES` = HR_Screening · HR_Interviewing · Preboarding · Joined is what the tab shows at), `payload(db, profile,
names)` (the figure + who/when + the figures it is decided against: current / expected CTC, the Sales Head-approved terms
= `approved_offer` — the latest Pending/Accepted `offer_history` row — its offer + joining dates, the offer letter
reference), `set_offer(...)` (409 outside the window, 400 for ≤ 0 or > `MAX_CTC` ₹100 Cr — a Lac typed as rupees; stamps
and logs `HR_OFFERED_CTC` with the previous figure) and **`employee_ctc(profile, offer)`** = HR's offered CTC when set,
else the approved offer's `ctc` — the ONE rule both `ensure_employee_for_joined_profile` and
`_sync_employee_from_joined_profile` use for the Employees record's `current_ctc` (pinned: the offered figure wins over
the approved terms; without one the terms still apply). `GET /api/candidate-profiles/{id}` carries **`hr_offer` only
when `may_see(user)`** — every other login never receives the key, so the tab cannot render for them (pinned by a
source scan). `PUT /api/candidate-profiles/{id}/hr-offer {offered_ctc (rupees), note?}` is
**`role_required("HR")`** (Admin/CEO implicit; pinned). `ProfileUpdate` has no such field, so the general PUT cannot set it.
Deploy: `alembic upgrade head`, restart.

**30 Sep 2026 — Applied Candidates: status chips, Archive, waiting days, who took the round
(`tests/test_applied_profile_only_rows.py` +2; no migration):** user ask after the owner-stage mock-up — filter by
STATUS (the Stage column is hidden), show how long each candidate has waited, move rejected candidates to an Archive
tab, and name the interviewer with a link to the interview pop-up. `GET /api/requirements/{id}/resumes` gains
**`status_key`** (CSV of `STATUS_DEFS` keys → `profile_ids_with_status` on the opportunity's profiles, applied to
resume rows AND profile-only rows) and **`bucket=live|archive`** (default live): `candidate_status.applied_buckets`
splits the opportunity's profiles by `REJECTED_BUCKET` — the SAME rule as the Closed phase — so a rejected /
withdrawn candidacy leaves the live list; ⚠️ a legacy resume with NO profile stays on the live list (it was never
closed; the live filter is an exclusion of archived candidates, not an inclusion). `meta.status_counts`
(`candidate_status.status_counts`: `{live: {key: n}, archive: {…}, live_total, archive_total}`) replaces
`meta.phase_counts` on this endpoint (`phase_counts` stays for the Candidate Profiles directory); `?phase=` still
works. Every row carries **`waiting_days` / `waiting_since`** (`resumes.waiting_since`, PURE: days since the
candidacy's last activity — the clock that already orders the list — else since the apply / upload time, never
negative) and `manual_round_state` adds **`<round>_interviewer`** (`InterviewEvent.interviewer`) beside each
round's `_when`. `TABLE_REGISTRY["requirement_resumes"]` drops `profile_stage` (saved layouts drop it via `_clean`).

**30 Sep 2026 (later) — every status as a chip + the Stage column back; the PO picker knows the employee; Sales'
invoice tabs; the JD as a file (`test_midmonth_timesheet` +1, `test_work_desk` re-pinned, `test_requirement_jd_skills`
+1; no migration):** (1) Applied Candidates chips list EVERY status of the bucket (Sourcing … HR Round · Pre-Onboarding ·
Joined; the closes on Archive) and the Stage column returned (`TABLE_REGISTRY["requirement_resumes"]` has `profile_stage`
again, announced after `applied_by`) — user decision reversing the morning's hide. (2) **`GET /api/timesheets/{id}/po-options`**:
"this employee's PO" now = TAGGED to them OR **billed for one of their timesheets before** (`billed_before` = invoice count,
`tagged_to_employee`; `employee_match` is the union and still drives `suggested_po_id`) — most customers never tag a PO to
a person, so the picker's employee scope was empty (user report: the whole customer book listed). ONE grouped query over
`Invoice.timesheet_id → Timesheet.employee_id`. (3) **`work_desk._sales_billing_tabs(…, invoices_everyone=)`**: three
invoice tabs for EVERY Sales rung — `sales_invoices_pending` (approved sheets with no Proforma / returned), new
**`sales_proformas`** (with Finance) and `sales_invoices` (issued Tax) — and the Sales Manager / Sales Head see every
customer's invoices (`invoices_everyone = billing_everyone or sales_all`; the timesheet / collections scope is unchanged).
Every billing item carries the **`employee`** facet (the sheet's employee; an invoice through its timesheet — one query;
a manual invoice names nobody), so My Tasks groups by customer OR employee and filters by employee. (4) **`POST
/api/requirements/{id}/attachments` with `kind=rmg_jd` reads the file's text** (`_jd_text_from_file` →
`services.resumes.extract_resume_text`, PDF / DOCX / TXT, clipped to `JD_TEXT_MAX_CHARS`=20,000, best-effort): a blank
`rmg_jd_text` is filled from it (activity `JD_SKILLS`), a written one is left alone, and `extracted_text` /
`jd_text_filled` come back so the dialog shows it. The Edit JD & skills dialog uploads through this route.

**30 Sep 2026 (last) — ATS scored when TA sends for Technical Screening + re-scored when the JD / skills change;
Archive is MANUAL (`test_ta_decision` +3, `test_applied_profile_only_rows` re-pinned; no migration):** screenshot report —
the Screening Desk read "Could not score … Nothing to score against" and a rejected candidate moved to Archive on its own.
(1) `send_for_screening` runs `resumes.auto_score_profile` inline for ≤ `SCREENING_SCORE_INLINE`=3 candidates; the batch
route hands larger sends to `score_profiles_in_background` after commit. `auto_score_profile` now scores an UPLOADED resume
on the requirement even when the candidate record has no CV (it only built one from `cv_url` before). (2) The real gap was
the POSITION: no skills + no RMG JD → `ats_scoring` refuses, the row stays Pending_Scan, and nothing ever re-tried.
`resumes.rescore_requirement(db, req, uid, limit=RESCORE_MAX=200)` re-scores Pending_Scan / Scored rows
(`RESCORABLE_STATUSES` — never a Shortlisted / Rejected resume, never a closed candidacy) + live profile-only applicants
with a CV, each savepointed; `has_ats_criteria(db, req)`; `rescore_requirement_in_background` (own session, daemon thread,
skips quietly without a CRM DB). Triggered by `routers/crm/requirements._rescore_after_jd_change` after `PATCH …/jd-skills`,
a `rmg_jd` attachment upload (Word / PDF — the text already fills a blank JD) and `engineering-approve`; the replies carry
`rescoring`. (3) **Archive**: `candidate_status.ARCHIVED_ACTION` / `RESTORED_ACTION` (`APPLIED_ARCHIVED` /
`APPLIED_RESTORED` activity rows, latest wins, `archived_profile_ids`) — `applied_buckets` / `status_counts` archive a
candidacy only when it is closed (`REJECTED_BUCKET`) AND flagged; a rejected candidate stays live until then, and a reopened
one is live again. `candidate_profiles.set_applied_archive` (409 on a live candidacy, idempotent) behind
`POST /api/candidate-profiles/{id}/archive {archived}` (`rmg_roles` = RMG / GM). Applied Candidates rows carry `archivable` /
`archived`. This SUPERSEDES the morning's "a rejected candidacy leaves the live list" rule.

**30 Sep 2026 (latest) — TA may write the JD too; po-options names the project and customer
(`test_requirement_jd_skills` +1; no migration):** screenshot report — a TA opened "Edit JD & skills" and the
PDF / Word upload answered 403: `POST /api/requirements/{id}/attachments` (and its DELETE) said
`gated_write("requirements", "RMG")` while the PATCH admitted RMG · Sales · Sales Head. ONE tuple now,
`routers/crm/requirements.JD_EDIT_ROLES = (RMG, Sales, Sales_Head, TA)`, read by the PATCH, the upload and the
delete (a GM comes through the requirements Edit grant as before; Admin/CEO implicit) — pinned by a source scan so the
three can never drift again. `GET /api/timesheets/{id}/po-options` adds `project_name` / `customer_name` for the
redesigned Raise Proforma dialog's header. F-V2: the requirement page had kept a STALE private copy of the dialog
(no drop zone) — deleted; see that repo's notes.

**30 Sep 2026 (evening) — Feedback-due facets on the desk board; TA's "Pending activities" tab
(`test_rmg_tasks` +1, `test_work_desk` re-pinned; no migration):** (1) `rmg_tasks._feedback_items` rows carry
`section` (the position "OPP · title"), `round_kind`, `interviewer` and `overdue_hours` — the Screening Desk's
Feedback-due panel groups by position or by day and filters by round (F-V2). (2) `work_desk._ta_tabs(db, user,
owner_id)` builds the three schedule tabs + sourcing ONCE (each half savepointed, so one failing leaves the other) and
puts a **`ta_pending` "Pending activities"** tab in front of them — every schedule + sourcing item copied with
`activity` (the list it came from), `section` (the position — My Tasks groups by it; the "· customer offered …" suffix
stripped) and its `round_kind`; keys `pend:<original>`. Tab order for a TA: feedback · ta_pending · schedule_customer ·
schedule_internal · schedule_hr · sourcing · upcoming · queues. The desk's F-V2 filter bar gains Activity + Round.

**1 Oct 2026 — Applied Candidates chips are STAGES again (`test_applied_profile_only_rows` re-pinned; no
migration):** user decision reversing the 30 Sep status chips ("under Opportunities, every login: show stages, not
status"). `candidate_status.status_counts` now also returns **`phases: {live: {stage key: n}, archive: {…}}`** — the
`phase_counts` rule (`stage_for`, a closed candidacy under "closed") per Archive bucket, from the SAME facts load — so
`GET /api/requirements/{id}/resumes` `meta.status_counts` serves both the stage chips (`?phase=`, F-V2) and anything
still reading the status counts. `status_key` keeps working.

**1 Oct 2026 — a withdrawn candidate can re-apply (`test_ta_decision` +1; no migration):** user ask — "after Self
Withdrawn I need a button to apply to this opportunity again". `TA_DECISIONS` gains **`reapply`** →
`candidate_profiles.reapply_candidacy(db, profile, note, user)`: ONLY a `Self_Withdrawn` candidacy reopens (409 for a
rejection — that was somebody else's decision); it lands back at **Sourcing with TA** — `withdrawn_from_status` and the
RMG screening (`rmg_screening_status/note/by/at`) cleared so TA sends them for Technical Screening afresh, a TA hold
dropped, an Archive flag lifted (`RESTORED_ACTION` row) — logged as `STATUS_CHANGE Self_Withdrawn -> Sourcing: Re-applied`
+ **`REAPPLIED`**. Interview rounds / AI links stay (they happened). Same route, `POST …/ta-decision {decision: "reapply",
note?}` (`gated_write("profiles","TA")`), note optional.

**1 Oct 2026 — HR Interviewing + Joined phases, RMG / GM read Applied Candidates through the screener gate, TA
assignments per position, availability on the rows, migration 0117, head is now 0117
(`tests/test_requirement_ta_assignments.py` 4, `test_candidate_status` re-pinned):** four screenshot asks. (1) **Phases**:
`candidate_status.STAGES` gains `hr_interviewing` (HR_Interviewing) and `joined` (Joined) — `_STAGE_BY_PIPELINE` maps them,
so the chips, `?phase=`, `phase_counts` / `status_counts.phases` and the Stage column all split "HR Screening" from "HR
Interviewing" and "Onboarding" from "Joined". (2) ⚠️ **`crm_deps.screener_or(gate)`** — reported: a login holding RMG + the
GM custom role got "You do not have access to the 'requirements' tab" on Applied Candidates and Positions (`GET
/api/requirements/{id}/resumes` 403). Their access comes from the custom role, so the template decides ALONE and the built-in
RMG role never gets a say. The wrapper runs the gate and, on a 403 only, admits whoever `action_permissions.screens_as_rmg`
says screens as RMG. Applied to the seven RMG-including gates in `routers/crm/resumes.py` (resumes list, review, reparse,
the three scan routes, schedule-ai-interview), `requirement_positions.POS_READ` and the priority PATCH; pinned by a source
scan. (3) **TA assignments**: `requirement_ta_assignments` (0117; one row per (requirement, TA login), CASCADE),
`services/requirement_assignments.py` — `ta_options` (active TA logins), `assignments_by_requirement` (ONE query per
page), `assigned_requirement_ids`, `set_assignments` (replace-list; only active TA ids, else ValueError → 400; logs
`TA_ASSIGNED`; newly assigned TAs get bell + email, event `requirement.ta_assigned` in `email_flows.EVENTS`, deduped per
(requirement, TA); removed ones hear nothing). `routers/crm/requirement_assignments.py` (`_MODULES` += it): `GET
/api/requirements/{id}/ta-assignments` (`POS_READ`; `meta.can_assign`, `options` only for writers) · `PUT …` `{user_ids,
note?}` (`screener_or(gated_write("requirements","RMG","Sales_Head"))`), both scoped by `_visible_requirement`. ⚠️ An
assignment is NOT a visibility rule — every TA still sees every sourcing position; `GET /api/requirements?assigned_to_me=true`
is the opt-in narrowing. Payloads: `assigned_tas` on requirement list rows and `_one`; `positions_by_opportunity` (the
opportunity list + detail) adds `requirement_priority` + `assigned_tas`. Table added to `data_backup.DATASETS["opportunities"]`.
(4) **Availability**: Applied Candidates rows carry `notice_period` (the application's typed value, else
`Candidate.notice_period` — `routers/crm/resumes.availability_by_candidate` / `_with_availability`, one query),
`resignation_status`, `last_working_day`; `TABLE_REGISTRY["requirement_resumes"]` gains `availability` (announced after
Status). Deploy: `alembic upgrade head`, restart.

**1 Oct 2026 — the closing note, re-apply after a rejection, no email to employees (`test_ta_decision` re-pinned; no
migration):** (1) **`candidate_status.closing_notes(db, profile_ids)`** — who closed each candidacy, when and WHY: the
LATEST `STATUS_CHANGE` row ("<from> -> <to>: <reason>", the shape `perform_transition` always writes) whose target is in
`REJECTED_BUCKET`; a later move to a live status drops it. `GET /api/requirements/{id}/resumes` prints it as `closed_note`
`{status, reason, by, by_id, at}` on closed rows only (ONE query for the page). Every rejection already requires the reason
(`comment_required_for`, TA reject ≥ 5, RMG screening note), so nothing new is asked — it is now SHOWN. (2)
**`reapply_candidacy` reopens ANY closed candidacy** (user: "if we want to apply again after a rejection"): a withdrawal
needs no reason, re-applying over a rejection needs one (≥ `MIN_COMMENT_LENGTH`, 400 otherwise); a live candidacy stays 409.
Other opportunities: the Candidates page's Apply to Opportunity (the candidate master keeps the whole history). (3) **Checked:
every Upload Resume and Bulk ZIP creates / enriches the Candidate master** (`find_or_create_candidate_from_resume` by email,
else name; placeholder `resume-<id>@noemail.karnex.local` when the CV has none) plus a Sourcing profile — nothing to change.
(4) ⚠️ **Employees get no email** — reported with a screenshot: a project employee deployed at Harman received "Timesheet due
for September 2026" from noreply@karnex.in. `notify.employee_emails_enabled()` reads Settings `notify.employee_emails`
(`EMPLOYEE_EMAILS`, default **false**) and `notify_employee` skips the mail unless it is on — timesheet-due reminders, leave
decisions, approvals, all of them; bells to a linked login still fire; candidate / login / role mail is a different path.
Settings ▸ Operations ▸ "Email employees on the HR master".

**1 Oct 2026 (evening) — the Opportunities list carries the position row's facts (no migration):**
`services/requirements.positions_by_opportunity` (the headcount map on `GET /api/opportunities` + the detail) now joins the
Opportunity once and adds `requirement_experience_min/max`, `requirement_budget_ctc_min/max`,
`requirement_target_closure_date`, `requirement_work_mode`, `requirement_location_name` (one `Location` query for the
page) and `requirement_display_status` (`display_status_for(req status, deal stage)` — the ONE wording TA's list badges),
so the pipeline list prints the same row as TA's position list for every role (F-V2 `CLAUDE.md`). Still three queries +
one for a whole page, never per row.

**1 Oct 2026 (night) — a deal on hold parks its candidates (`tests/test_candidate_status.py` +2; no migration):**
screenshot report — C-2026-00086 was on Customer Hold yet Debjani Das still read "Submitted to Customer" on Candidate
Profiles. Closing a deal hides its profiles (`_set_profiles_hidden`); a HOLD never did, and nothing on the row said the
deal was parked. Deliberately NOT hidden — a hold is reversible and the candidacy must resume exactly where it was —
instead the derived status says it: `candidate_status.StatusFacts.opportunity_stage` (ONE extra batched query in
`load_facts`: profile → `Opportunity.pipeline_stage`), `DEAL_HOLD_STATUS_KEY` (`On_Hold` → **"Customer Hold"**,
`Sales_Hold` → **"Sales Hold"**, tone WARN, new group **`parked` "Opportunity on hold"**, declared over every live
stage so the `status_key` filter finds them), applied in `_derive` BEFORE every other rule unless the stored stage is
in `_SETTLED` (Joined + the rejections / withdrawal keep their own word). The stored stage, `stage_for` (the phase chips /
Stage column), TA's buttons and permissions are untouched; the Status cell prints "Opportunity · Customer Hold". The
hint spells out that the candidate may be applied to other opportunities meanwhile — `POST /api/candidate-profiles`'s
duplicate check is per (candidate, opportunity), so that already works. F-V2 needs no change: the badge reads tone / group
from the catalogue. Deploy: restart.

**1 Oct 2026 (night, last) — TAs assigned at RMG approval (`tests/test_requirement_ta_assignments.py` now 5; no
migration):** user ask — "RMG / GM assign the TA at the time of RMG approval, and from the Opportunities list without
going inside". `EngineeringApproveIn.ta_user_ids: list[int] | None` — `POST /api/requirements/{id}/engineering-approve`
calls `requirement_assignments.set_assignments` after the approval (None = team untouched; a list REPLACES it; a non-TA
id → 400 and nothing is approved); the assigned TAs get the "assigned to you" bell + email on top of the role-wide
"open for sourcing" notice, and the reply message counts them. The list-row button is UI only (the existing
`GET / PUT …/ta-assignments`). F-V2: `AssignTasButton`, `TaPicker` (see that repo's notes).

**1 Oct 2026 (night) — JD & skills card on the position AND opportunity pages (F-V2 only; no server change):** the
client now mirrors `routers/crm/requirements.JD_EDIT_ROLES` through the requirements Edit grant, so a GM / custom role
that passes `gated_write("requirements", *JD_EDIT_ROLES)` also sees the Add / Edit JD & skills button. Everything else
(`PATCH …/jd-skills`, the `rmg_jd` attachment upload that reads the file's text, the re-score) is unchanged — see F-V2
`CLAUDE.md`.

**1 Oct 2026 (night) — one template request per opportunity; the upload form's locations reach the candidate,
migration 0118, head is now 0118 (`test_template_request_fulfill_link` +1 / re-pinned, `test_upload_form_location_note`
+3):** two screenshot reports. (1) TA pressed "Request template" several times → RMG got duplicates.
`routers/crm/template_requests.open_request_for(db, req)` = the live (not Cancelled) request of the requirement's
OPPORTUNITY (else the requirement); `POST /api/template-requests` answers **409** naming it ("TR-…, waiting for RMG /
template ready"); a cancelled request does not block a new ask. F-V2 replaces the button with "Template requested · TR-…".
(2) TA typed Preferred location on Upload Resume and the profile said "Candidate Preferred Location — missing": the form
kept it on `resumes.application_details` only, while the profile, the Submit-to-Sales checklist and the missing-location
reminder read `candidates.preferred_locations`. `slot_booking.copy_locations_to_candidate(candidate, details,
overwrite=, keys=)` (`LOCATION_FIELDS`: current_location → `city`, preferred_location → `preferred_locations`) — an upload
fills BLANKS only (`find_or_create_candidate_from_resume`), the Edit applicant PUT (`update_resume`) overwrites the ones TA
changed. **0118** backfills blank candidate city / preferred locations from each candidate's latest resume that carries
one (Postgres `DISTINCT ON`; downgrade no-op). Deploy: `alembic upgrade head`, restart.

**1 Oct 2026 (night) — bulk upload emails every candidate about the opening; the CV's email is checked
(`tests/test_opening_interest.py`, 22; no migration):** user ask — "when TA bulk-uploads, every candidate gets a
professional 'we have this opening, are you interested?' mail; they reply; TA confirms the details and moves them to
Technical Screening; make sure the email from the resume is right". (1) **Email extraction** (`services/resume_parse.py`):
`clean_email(raw)` (PURE — lower-case, drops `mailto:` / wrapping punctuation, un-glues a phone number a PDF ran into the
address, fixes unambiguous provider typos `_DOMAIN_TYPOS` + `.con`→`.com`, refuses file names (`image001.png@…`),
`_EXAMPLE_DOMAINS`, our placeholders, malformed addresses); `find_resume_emails(text)` (rejoins "john @ gmail . com" /
"[at] [dot]"); `pick_resume_email(text, name)` — the CANDIDATE's address among several: local part carries their name,
not a role mailbox (`_ROLE_LOCALS` hr@ careers@ noreply@ …), first in reading order; ⚠️ `reconcile_email(model, text, name)`
— **the model may only CHOOSE among addresses literally in the CV** (or there with line breaks removed); an invented
address is dropped for the best literal one (the old merge let the model's answer win). Applied in `_regex_parse`,
`_ai_parse`, the OCR path (clean only — no text) and on every cache hit (entries cached before the cleaner).
(2) **`services/opening_interest.py`**: `opening_facts(db, req)` (role · experience band · location (requirement
location, else the opportunity's `tm_work_location`) · work mode · ≤ 6 skills, mandatory first) — ⚠️ **the customer is
never named** (first-touch mail about a client's opening); `opening_message(...)` subject "Job opportunity: <role> — are
you interested?", a facts table and five asks (interest Yes/No · current + expected CTC · notice / LWD · current +
preferred location · a time for a call), signed by the TA (`sender_details`, savepointed); admin-editable draft, event
**`candidate.opening_interest`** (`email_flows.EVENTS` kind candidate, `builtin_candidate_draft`, `CANDIDATE_MAIL_EVENTS`
label, `NEVER_COLLAPSE_EVENTS`); `send_opening_mail(db, profile, candidate, req, user, to_email=, resend=)` — address =
THIS CV's (`resume.email`) else the record's, through `clean_email` (placeholders never mailed); one per candidacy
(`OPENING_MAIL_SENT` activity row + outbox `dedupe_key opening_interest:<profile>`; `resend` skips both); **Reply-To =
the TA** (company mailbox when the login has none) so the candidate's answer lands with a person; never raises — returns
sent · no_email · already_sent · not_sent. `opening_states(db, ids)` = the latest of `OPENING_MAIL_SENT` /
`CANDIDATE_INTERESTED` / `CANDIDATE_NOT_INTERESTED` (one query) → `opening_mail` on every Applied Candidates row.
`send_candidate_email` gained `dedupe_key=`. (3) **Bulk ZIP** (`POST …/resumes/bulk-zip`): Form `send_opening_email`
(default true; the dialog's checkbox); `_run_bulk_zip_job(..., user_email, send_opening_email)` builds the facts + signature
ONCE and mails each APPLIED row in its own savepoint (a mail problem never costs the CV); held duplicates are not mailed
(TA resolves them, then uses the row button). Applied rows carry `email` + `opening_mail`; the job result adds
`opening_mail {enabled, sent, no_email}` and the message counts them. (4) **TA's answer**: `TA_DECISIONS` += `interested`
(logs `CANDIDATE_INTERESTED`, then `send_for_screening` — same refusals) and `not_interested` (logs
`CANDIDATE_NOT_INTERESTED`, then Self_Withdrawn with `NOT_INTERESTED_NOTE` when no note; screeners are NOT told — it never
reached them); both at the derived Sourcing stage only. **`POST /api/candidate-profiles/{id}/opening-email {resend}`**
(`gated_write("profiles","TA")`) — single uploads, an applied held duplicate, or after TA corrected a wrong address
(409 closed / already sent, 400 no usable email, 503 when the outbox declines). Deploy: restart.

**2 Oct 2026 — RMG / GM add the missing skills, RMG JD and customer JD from the JD & skills card
(`test_requirement_jd_skills` re-pinned +1; no migration):** (1) `routers/crm/requirements.JD_EDIT_GATE =
screener_or(gated_write("requirements", *JD_EDIT_ROLES))` is now the ONE dependency of `PATCH …/jd-skills`, `POST
…/attachments` and `DELETE /attachments/{id}` — a GM (custom role) or a template-limited RMG who screens as RMG
(`screens_as_rmg`) is admitted on the 403 path, exactly like the resumes routes. (2) `POST /api/requirements/{id}/
attachments` with **`kind=customer_jd`** (`_add_customer_jd`) stores the file as the OPPORTUNITY's attachment
(`opportunity_attachments`, kind customer_jd, folder `opportunity_attachments`) — where the opportunity form puts it and
where `serialize_requirement` reads `customer_jd_attachments` — logs on both activity logs, never touches `rmg_jd_text`
and starts no re-score. The opportunity's own attachment routes (Sales / Sales Head) are unchanged. Deploy: restart.

**5 Oct 2026 — "Pending Approval" includes positions waiting for RMG; seller GSTIN / PAN corrected, migration 0119,
head is now 0119 (`tests/test_pending_approval_tab.py`, 2):** two screenshot reports. (1) The Screening Desk listed two
positions waiting for RMG approval; the Opportunities list showed them under **Active** and **Pending Approval** was empty —
that tab only asked for the OPPORTUNITY's own `Pending_Sales_Head_Approval`. `services/requirements.
awaiting_approval_clause()` = the deal awaits the Sales Head, OR it is Approved + New/Active with a requirement in
`AWAITING_APPROVAL_STATUSES` (Pending_Sales_Head_Approval · Pending_Engineering_Review). `GET /api/opportunities` gains
`awaiting_approval` (true = the clause, false = its negation); F-V2 sends true on Pending Approval and false on every stage
tab, so each deal still lives in exactly ONE tab (a held deal stays in its hold tab). (2) The Tax Invoice printed
"GSTIN AAHCK4749A / PAN 27AAHCK4749A1ZL" — the two Settings ▸ Invoice rows were saved into each other's boxes (the
renderers were right). **0119** sets `invoice.seller_gstin` = 27AAHCK4749A1ZL and `invoice.seller_pan` = AAHCK4749A where
the rows exist; the `org_settings` defaults and the `tax_invoice.SELLER_*` fallbacks now carry the same (they held a
different, wrong pair). `org_settings.normalize_value` (upper-case) + `validation_error` (GSTIN 15-char pattern, PAN
10-char) run in BOTH save routes (`PUT /api/org-settings`, `PUT /api/settings/{key}`), so a swapped pair is a 400 now; the
generic route also invalidates the settings cache. Deploy: `alembic upgrade head`, restart.

**5 Oct 2026 — Word export of the Tax Invoice fixed and redesigned (`tests/test_tax_invoice.py` +1; no
migration):** reported — "Download Word" answered **400**. ⚠️ lxml refuses control characters (a vertical tab pasted
into a buyer address from Word / Excel) with a `ValueError`, and `main._handle_value_error` turns ANY ValueError into a
bare 400. `services/tax_invoice_docx._clean` now strips them from every string, and the route wraps the build: a
failure is logged with its traceback and answered 500 with the reason (never the global 400). Layout rebuilt: every
table is FIXED layout with an explicit `w:tblGrid` (`_fix_widths` — cell widths alone are a hint Word / LibreOffice
auto-fit over, which is why the description column collapsed and the nested GST tables overflowed), invoice meta and
bank details are borderless label/value tables (colons line up), cells vertically centred with even padding, a zebra
service table with a repeating header row, GST summary + totals side by side ending in a navy **GRAND TOTAL** row,
amount / tax in words side by side, centred footer link. Check a change by rendering: `soffice --headless --convert-to
pdf` on the device.

**5 Oct 2026 (later) — branch-wise invoice due days, invoice Round Off, held deals park candidates in Archive,
migration 0120, head is now 0120 (`tests/test_round_off_and_due_days.py` 5, `test_applied_profile_only_rows` re-pinned
+1):** three screenshot asks. (1) **Due date per customer branch**: `customer_branches.invoice_due_days` (NULL = follow
the PO, 0 = due on receipt, ≤ 365; in `_BranchBillingFields`, `BranchBillingPolicyIn`, `BRANCH_BILLING_POLICY_FIELDS`,
`serialize_branch`). `services/proforma.invoice_credit_days(db, project_id, po)` → `(days, "branch"|"po"|"default")`:
the project's branch, else the PO's billing branch, wins when set; then the PO's payment terms; then 30. Used by the
Proforma raise (`generate-invoice`, replaces `po_credit_days` there) and `convert_to_tax_invoice`; `serialize_invoice`
(detail) carries `credit_days` / `credit_days_source` so the editor re-derives the due date. (2) **Round Off**:
`invoices.round_off` Numeric(6,2), **NULL = not rounded** (0.00 = rounded, already whole). `finance.apply_round_off(inv,
enabled)` is THE rule — grand total = sub-total + GST to the nearest rupee (`tax_invoice.round_to_rupee`, half up), the
difference stored as its own line, balance / payment status refreshed; ⚠️ GST and the sub-total never move and the PO is
still drawn by the sub-total. `apply_invoice_gst_totals` re-applies it, so a recompute keeps the choice. Chosen by
`GenerateInvoiceIn.round_off` (raise), `ConvertProformaIn.round_off` (convert; None = keep the Proforma's) and
`InvoiceUpdate.round_off` on a PROFORMA only — on a Tax invoice it is a 400 ("chosen before the original invoice is
generated"). Every renderer (HTML, reportlab, Word) prints a "Round Off" row before GRAND TOTAL
(`format_round_off`); `serialize_invoice` excludes it from `stored_grand` and returns `gst.round_off`. (3) **Archive**:
`candidate_status.archive_clause()` (SQL) = latest manual `APPLIED_ARCHIVED` row, OR the deal is On_Hold / Sales_Hold
(`HOLD_STAGES`) and the candidacy is not settled (Joined / rejections stay put); `archive_reasons(db, ids)` →
"manual" (wins) | "hold". `applied_buckets` / `status_counts` use it, so Applied Candidates' Archive tab and the
Candidate Profiles list (`bucket=archive`, `_apply_bucket`; active / rejected exclude archived; `phase_counts.archive`;
rows carry `archived`) agree. A held deal's candidates leave every login's live list and come back the moment Sales
reactivates it — no rows written either way. ⚠️ `set_applied_archive` now archives ANY stage by hand (SUPERSEDES the
30 Sep "closed only" rule); restoring a hold-parked candidacy is 409. Route gate `archive_gate =
screener_or(gated_write("profiles", "RMG", "Sales", "Sales_Head"))`. Applied Candidates rows add `archive_reason`;
`archivable` = has a profile and not archived. Deploy: `alembic upgrade head`, restart. Suite: 1,762 pass, 2 skipped.

**5 Oct 2026 (night) — customer approval of a tax invoice + the e-invoice IRN, migration 0121, head is now 0121
(`tests/test_invoice_customer_approval.py` 6, `test_proforma_flow` +1 / re-pinned, `test_work_desk` re-pinned):** user
flow — Finance generates the original invoice → the Sales Manager / Sales Head sends it to the customer and, when the
customer accepts it UNCHANGED, confirms that to Finance (a change goes through an invoice change request, never here) →
Finance records the IRN + Acknowledgement No.; the IRN is Finance / Admin / CEO only. **0121** adds to `invoices`:
`customer_approved_at` (indexed) / `_by` / `customer_approval_note`, `irn_number` / `ack_number` / `ack_date` /
`irn_recorded_at` / `_by`. **`services/invoice_customer_approval.py`** is the one module: `may_confirm` (`CONFIRM_ROLES`
Sales_Head + the "Sales Manager" custom role, + Admin/CEO — BY ROLE, case-insensitive; a template grant never widens it),
`may_see_irn` (Finance + Admin/CEO), `confirm_customer_approval` (Tax only — a Proforma is 400; once only — 409; logs
`INVOICE_CUSTOMER_APPROVED` on the source timesheet; notifies Finance, event **`invoice.customer_approved`** in
`email_flows.EVENTS`, savepointed), `withdraw_customer_approval` (only before the IRN is in), `record_irn` (409 until the
approval; `irn_error` PURE — IRN = 64 hex chars, Ack No. = 10–20 digits, ack date not in the future; re-saving corrects,
logged), `payload(invoice, user, names)` → `customer_approval` for every reader and **`einvoice` ONLY for Finance /
Admin / CEO** (every other login never receives the key). Routes (`routers/crm/finance.py`, all behind `INV_READ`, the
role check in the service): `POST` / `DELETE /api/invoices/{id}/customer-approval {note?}`, `PUT
/api/invoices/{id}/einvoice {irn, ack_number, ack_date?}`; `GET /api/invoices/{id}` carries both blocks
(`_with_approval`); `GET /api/invoices?customer_approved=true|false` (Tax only; rows carry `customer_approved_at`, and
`irn_recorded` / `ack_number` for Finance / Admin / CEO only — the IRN itself stays on the invoice page). The
`invoice.generated` notice now goes to the Sales Manager AND the Sales Head (`notify_roles`) and tells them to confirm.
Work desk: Finance's **`fin_customer_approved` "Customer approved invoices"** after Tax invoices issued (IRN pending, oldest
approval first — the count — then IRN recorded in the last `FIN_IRN_DONE_DAYS`=30; sections "IRN to add" / "IRN
recorded"); Sales Manager / Sales Head get **`inv_confirm` "Confirm with customer"** (Tax invoices of the last
`CONFIRM_LOOKBACK_DAYS`=90 not yet confirmed; amber past `CONFIRM_WAIT_DAYS`=7). Admin / CEO keep `CEO_TABS` (they read
the IRN on the invoice page and the Invoices list). Not printed on the invoice. Deploy: `alembic upgrade head`, restart.

**6 Oct 2026 — ATS closer to the ATS Scoring page, one candidate line on Applied Candidates, billing filters on My
Tasks (`tests/test_ats_facts_and_applicant_line.py` 6, `test_requirement_jd_skills` re-pinned; no migration):** three
screenshot reports. (1) **A CV the ATS Scoring page rated 75 read 45.84 on Applied Candidates.** Three causes in
`services/resumes.run_ats_scan`: it ignored what TA typed (a Naukri CV says "3y 2m", which `EXPERIENCE_RE` cannot read →
0 experience points; the position's city counted only when the CV named it), it never read the CUSTOMER's JD (only the
RMG JD), and the AI reviewer carried 40 %. Now: `candidate_facts(db, resume)` = the application's experience (else the
candidate's) + every location given (current / preferred, application + candidate record) → `score_resume_against_
requirement(..., candidate_facts=)` (typed experience wins over the CV's, `experience_source` application | resume;
location matches on the CV OR a given location) and into the AI prompt (band, experience, locations; prompt reworded
from "strict" to a recruiter's read); `ats_jd_text(db, req)` → (text, "rmg" | "customer") — the RMG JD (text + files),
else the opportunity's `customer_jd` files (`_jd_files`; `has_ats_criteria` counts them); blend `KEYWORD_SHARE` 0.4 /
`AI_SHARE` 0.6 (was 0.6 / 0.4). `ATS_SCORE_VERSION` = 2 is stamped on every breakdown; `ats_outdated(resume)` (Scored and
older) is on every serialized resume, and `scan-all` re-scores Pending AND outdated rows (F-V2 "Re-score N (ATS
updated)"). A customer JD added from the JD & skills card re-scores the position when it has no RMG JD. (2) **One
candidate line**: rows read `application_details` only, so a Candidates-tab application (its resume row is built by
`ensure_resume_for_profile` with no details) and an upload looked different. `availability_by_candidate` now also returns
`facts` (experience · current / expected CTC in lakhs via `_lakhs` · city · preferred locations) and
`_with_applicant_facts(row, avail)` fills every gap of a COPY (what the application typed wins) — applied with
`_with_availability` to resume AND profile-only rows in ONE pass over the page. Edit applicant now also writes notice /
current + expected CTC / experience to the candidate record (`slot_booking.copy_application_facts_to_candidate`, only
the keys TA changed, a blank never erases) and an edited expected CTC to the profile (the budget check). (3) **Finance's
billing items** (`work_desk.billing_chain`) carry `section` = customer, `employee` (Proformas / invoices through their
timesheet, `employees_of`, one query) and `amount`; `_facets(..., employee=, amount=)`; Sales' invoice items carry
`amount` too. Suite: 1,775 pass, 2 skipped.

**CLAUDE.md itself:** both repos' files are tracked in git (`git checkout -- CLAUDE.md` restores the committed
edition); the September notes above exist only in the working tree — **commit them**.

**Savepoint discipline:** every best-effort `try/except` around DB work wraps it in
`db.begin_nested()` — on Postgres a swallowed statement failure otherwise poisons the transaction and
every later statement 500s (`InFailedSqlTransaction`). Copy this pattern for any new best-effort block.

---

## 2. Orientation map

```
backend/
  main.py            app assembly + ALL legacy auth + ~70 interview/HR endpoints  ← 7,865 L, start here for interview work
  ai.py              model-facing engine: generation, evaluation, TTS, transcription, scoring guards (3,520 L)
  auth_db.py         legacy raw-SQL store, dual-dialect (3,386 L)
  session.py         ★ 20 lines. `sessions` dict + `_session_locks`. The docstring's Redis store does NOT exist.
  crm_db.py          SQLAlchemy engine/session for the CRM (Postgres only)
  crm_deps.py        ★ the RBAC core — every CRM route's Depends() lives here
  candidate/         next_question_payload — the /next contract (273 L)
  services/interview/question_service.py   ★ the LIVE question prompt (a user message, no system message)
  models/            22 modules, SQLAlchemy 2.0, **85 tables**
  schemas/           18 modules, Pydantic; schemas/common.py::envelope() is the response contract
  routers/crm/       39 modules, **403 routes**
  routers/admin.py   prompt logs + AI usage (legacy `hr` JWT role, not CRM RBAC)
  routers/question_bank.py   13 endpoints, own `_require_hr`
  services/          50 modules — timesheets, leave credit, finance, tax, notify, scheduler, outbox
  ai_help/           Ask AI knowledge base (Python TypedDicts, not YAML) + 10 SELECT-only tools
  alembic/versions/  95 files, single root 0001, single head **0097** (0080+ untracked); CRM tables only
  tests/             102 files, **815 tests**
  scripts/           DB-touching operational + QA CLIs
  tools/             Zoho/NEXUS CSV/NDJSON importers (dry-run by default)
services/            strangler-proxy stubs (see §11) — inert
k8s/, Dockerfile, docker-compose*.yml, render.yaml
```

Ten largest Python files: `main.py` 7865 · `ai.py` 3520 · `auth_db.py` 3386 ·
`services/timesheets.py` 2433 · `services/tax_invoice.py` 1542 · `services/project_employees.py` 1314 ·
`routers/crm/timesheets.py` 1312 · `routers/crm/projects.py` 1194 · `services/finance.py` 1164 ·
`services/candidate_profiles.py` 1159.

---

## 3. Auth and RBAC — read this before touching any endpoint

**Two parallel auth implementations. Do not mix them.**

- **Legacy interview endpoints**: `main._require_user(request, allowed_roles)` (`main.py:1711`,
  8 lines) returns a `(payload, error_response)` **tuple**, called imperatively inside the handler.
  Nothing enforces that a handler checks the error half.
- **CRM endpoints**: `crm_deps.get_current_user` as a real `Depends()`.

Both HS256, both funnel to `auth_secret.auth_secret()` (`auth_secret.py:55`) which reads
**`AUTH_SECRET`** and refuses empty / known-weak / `< 32`-byte values in every environment.

### Legacy token shapes (only two)

`_issue_access_token` (`main.py:579`) emits `sub, role, full_name, email, iat, exp`
(TTL `AUTH_TOKEN_TTL_MIN`, default 480 min).

1. **hr / candidate** — from `/auth/login`, `/auth/refresh`.
2. **invite-session** — adds `{"invite_token": <token>}` (`main.py:7404`), `role="candidate"`,
   `sub="invite-{token[:10]}"`. That one claim drives `_session_key_from_payload`,
   `_enforce_invite_device_binding`, and the 403 in `/auth/refresh`.

⚠️ `auth_db.register_user` accepts **only `{"hr","candidate"}`**, so the `"manager"` / `"admin"`
role names in the `_require_user` sets at `main.py:2721, 5531, 5604, 5936, 6000, 6122, 6130, 6147, 6797`
are **unreachable dead names** — those endpoints are HR-only in practice.

`POST /auth/refresh` (`main.py:6875`, 30/min): re-issues for a still-valid bearer; expired/missing → 401;
invite-session tokens → 403. ⚠️ It rebuilds purely from the old claims and **never re-reads the user**,
so a deactivated or demoted account keeps refreshing.

### CRM roles and the gate ladder

`models/rbac.py::RoleName` = `CEO, Admin, Sales, Sales_Head, RMG, TA, HR, Finance` (8 values).
`CurrentUser.is_admin` is true for **Admin or CEO**; Admin/CEO bypass every gate.

| Dependency (`crm_deps.py`) | Line | Meaning |
| --- | --- | --- |
| `get_crm_db` | 50 | session; **503** when Postgres is unconfigured |
| `get_current_user` | 83 | 2 raw SQL queries. ⚠️ **Does not require any CRM role** — an empty role set passes |
| `any_crm_role` | 127 | 403 when `roles` is empty |
| `role_required(*roles)` | 109 | role check; always adds `{Admin, CEO}`. No args = admin-only (aliased `admin_only`) |
| `require_access(tab, mode=…)` | 366 | Access-Template check **with no role fallback** — passes anyone untemplated |
| `gated_read` / `gated_write` / `gated_create` | 305 / 310 / 315 | **the normal choice** — `_gate` at view / edit / create |
| `gated_write_action(action, tab, *defaults)` | 324 | role list resolved **per request** from `action_permissions` (admin-editable, 60 s cache) |
| `page_params` | 211 | `page≥1`, `limit` clamped 1..100, `sort_dir` coerced |

**`_gate` precedence (`crm_deps.py:280-300`) — templates are AUTHORITATIVE:**

1. Admin/CEO → allowed, always (`:282`).
2. No roles at all → 403 (`:284`).
3. User **has** a template/override → the template **alone** decides; roles are never consulted
   (`:287-289`). This can grant beyond the role and restrict below it.
4. Unrestricted **and** the endpoint names no roles → any CRM role passes (`:292`).
5. Otherwise role check against `set(roles) | {Admin, CEO}` (`:294-300`).

Modes are a ladder: `view < edit < create` (`access_registry.mode_satisfies`; `mode_satisfies(None, x)`
is always `False`). Pinned by `tests/test_access_template_authority.py` (14 tests).

⚠️ **`require_access` is materially more permissive than `_gate`** — no role fallback, passes anyone
unrestricted. It is used on 15 timesheet endpoints *paired with* `get_current_user` plus in-body
ownership filtering. Do not reach for it just because `timesheets.py` does.

### Access Templates

`services/access_registry.py` — **21 grantable tabs** (`TABS`, `:34-56`):
`dashboard, customers, rate-cards, opportunities, candidates, template-requests, profiles, projects,
project-employees, branch-policy, my-leave, leave-applications, holidays, timesheets, pos, invoices,
tds, employees, reports, users, settings`.
`FIELDS_BY_TAB` (`:62-192`) covers **20 of 21** (no `dashboard` — it has no form). Field counts:
rate-cards 6 · customers 12 · opportunities 16 · candidates 13 · profiles 13 · template-requests 6 ·
projects 15 · project-employees 11 · branch-policy 6 · my-leave 2 · leave-applications 8 · holidays 7 ·
timesheets 10 · pos 14 · invoices 13 · tds 3 · employees 18 · reports 4 · users 7 · settings 4.
`FIELD_MODES = ("view","edit")` — creation is record-level only.

`services/access_templates.py::effective_access` (`:175-228`): Admin/CEO → `{full: True}`; otherwise
the assigned template is the base **only if `t.is_active`** — ⚠️ an assigned-but-inactive template
leaves zero visible tabs (the live lockout trap; `scripts/diagnose_access.py <user> --tab <tab>` explains
any user's effective access). Per-user `UserProfile.tab_access` keys resolve to `"create"`;
`field_access` keys to `"edit"`. `visible_tabs is None` ⟺ unrestricted.
`_strip_removed_keys` (`:47-68`) silently drops registry-unknown tab/field keys so old templates stay
saveable — a **deliberate** behaviour change that `tests/test_access_templates.py::test_api_validation_rejects_unknown`
still fails against (see §9).

**Field-level enforcement** — `reject_view_only_fields` has exactly **4 call sites**:
`candidates.py:187`, `candidate_profiles.py:401`, `employees.py:150`, `opportunities.py:430`.
A field grant wins over the tab mode in both directions.
⚠️ **Gap:** projects, pos, invoices, timesheets, holidays, rate-cards, branch-policy and
template-requests have field catalogues but **no enforcement call** — a view-only field grant there
is UI theatre only.

⚠️ **`access_templates.can_edit_tab` (`:237`) is broken relative to the ladder** — exact string compare
`== "edit"`, so a `"create"` grant returns `False`. No production callers today; it is a booby trap.

⚠️ **`enforce_roles()` / `main._enforce_crm_roles()` fails open by design** (`crm_deps.py:162-196`)
on 20 legacy call sites: no bearer, no CRM DB, `roles is None`, or zero CRM roles → the check no-ops.
Only a user who *has* roles and matches none gets a 403.

---

## 4. Conventions you must follow

1. **Every CRM response goes through `schemas/common.py::envelope()`** → `{success, data, message, errors, meta?}`.
   Verified: of 359 decorator-declared handlers, exactly **14 skip it and all 14 are correct**
   (binary/HTML/CSV responses, or delegation to a helper that envelopes internally). The frontend's
   `crm/api.ts` throws when `success === false` **even on a 200**.
2. **Route declaration order is load-bearing.** A full 403-route collision scan found **zero traps
   today**. The load-bearing literal-before-parametric pairs to preserve:
   `access_templates.py:28 /registry` · `customers.py:118 /policy-matrix` and `:183 /all-branches` ·
   `opportunities.py:365 /next-id` · `candidates.py:76 /check-duplicates` ·
   `projects.py:246 /all-employees` · `timesheets.py:1247 /due` — all before their `/{id}` sibling.
   Near-misses that are safe by *shape*, not ordering, and would break on edit:
   `projects.py:350 GET /employees/{pe_id}` vs `:665 GET /{project_id}/history`;
   `projects.py:89 PUT /leave-policies/{policy_id}`.
   There is **no test** asserting this invariant — worth adding.
3. **New CRM router → add it to `_MODULES` in `routers/crm/__init__.py`** (`:19-27`, currently 39 names,
   matching the 39 modules on disk — nothing unregistered). Registration is per-module via importlib so
   one bad import costs only its own routes; it has silently 404'd all CRM endpoints twice.
   Guarded by `tests/test_crm_router_registry.py`. Two extra routers attach post-loop:
   `holidays.names_router` and `employees.subform_router`.
4. **New API prefix → three places:** `routers/crm/__init__.py`, the frontend's
   `vite.config.ts::API_PROXY_PREFIXES`, and `F-V2/scripts/vercel-build.mjs::apiPrefixes`.
   ⚠️ **`/apply` and `/book` are currently missing from BOTH frontend lists** — see §10.
5. **Rejection reasons are ≥10 chars** (`schemas/common.py::RejectIn.validated_reason`) across
   opportunities, requirements, timesheets, leave. It raises `ValueError`, **not** `HTTPException` —
   all 4 call sites wrap it; a fifth that forgets gets a 500. (Profile transitions use a *different*
   minimum: `candidate_profiles.MIN_COMMENT_LENGTH = 5`.)
6. **Deletes are usually 409-with-a-count, not cascade** (`services/crm_delete.py`). Destruction is
   explicit (`?force=true`, often Admin/CEO only). Pinned by `test_crm_list_deletes.py` (21 tests).
7. **Soft-delete for calendar-ish data**: holidays and leave policies deactivate, never `DELETE`.
   `masters.py` exposes **no DELETE at all** for its 11 resources.
8. **Money and hours are computed server-side.** Client-supplied `balance`, `amount`, `days` are
   recomputed or ignored. `employee_leave_balances.balance` is always `accrued + carry_forward − consumed`.
   ⚠️ One exception: `InvoiceCreate.sub_total` **overrides** the line sum — see §9.
9. `models/` uses `pg_enum()` (`base.py:33`) — native Postgres enums storing enum **values**
   (`values_callable`, `validate_strings=True`). Tests shim `JSONB/ARRAY/UUID/INET` onto SQLite via `@compiles`.
10. `masters.py` deliberately omits `from __future__ import annotations` — FastAPI needs runtime
    annotations for its closure-scoped models.
11. **No `TODO` / `FIXME` / `HACK` / `XXX` comments exist anywhere** in `routers/crm/`, `models/`,
    `schemas/`, `services/`, `crm_deps.py`, `crm_db.py`, or the legacy half. Debt here is structural,
    not annotated — which is why this file is long.

---

## 5. Domain state machines (verbatim from code)

**Opportunity** — `OppType ∈ T&M | Work_Package | Fixed_Price | Retainer`.
`PipelineStage ∈ New, Active, On_Hold, Closed_Won, Closed_Lost, Closed_Partial, Rejected, Archived`.
`STAGE_TRANSITIONS` (`services/opportunities.py:30-39`): `New → {Active, On_Hold, Rejected}`;
`Active → {On_Hold, Closed_Won, Closed_Lost, Closed_Partial, Rejected}`;
`On_Hold → {Active, Closed_Lost, Closed_Partial, Rejected}`; every `Closed_*` and `Rejected → {Archived}`;
`Archived → []` (terminal). `Archived` is reachable only from a closed/rejected state; the
"Sales_Head/Admin only" rule is the endpoint gate (`opportunities.py:684`), not the map.
Approval: Sales-created → `Pending_Sales_Head_Approval`; Sales_Head/Admin-created → auto-approved.
Approval **spawns exactly one Requirement** (idempotent) pre-stamped `Pending_Engineering_Review`.
`PUT` **merges** partial `details` JSONB and never nulls unsent keys; optimistic concurrency via
`version` → 409 (`opportunities.py:442`, `:509`) — **the only versioned entity in the CRM**.

**Requirement** — `RequirementStatus` (11): `Draft, Pending_Sales_Head_Approval, Sales_Head_Rejected,
Pending_Engineering_Review, Engineering_Rejected, Open_For_Sourcing, Posted_On_Portals, In_Progress,
Fulfilled, Closed, Cancelled`. **There is no declarative transition map** — `_require_status(req, allowed, action)`
(`routers/crm/requirements.py:60`) enforces it per endpoint. Effective graph:
submit (from `Draft|Sales_Head_Rejected|Engineering_Rejected`) → `Pending_Sales_Head_Approval` →
approve → `Pending_Engineering_Review` → engineering-approve → `Open_For_Sourcing` → first job posting
auto-advances → `Posted_On_Portals` → first public apply auto-advances → `In_Progress` →
joined ≥ positions auto → `Fulfilled`; `/close`, `/cancel` from any non-terminal.
Engineering-approve **hard-requires a JD** (`:344-355`: `rmg_jd_text` or an `rmg_jd` attachment, else 400).
Visibility (`services/requirements.py`): `TA_VISIBLE_STATUSES = (Open_For_Sourcing, Posted_On_Portals,
In_Progress, Fulfilled)`; `SEE_ALL_ROLES = (Admin, Sales_Head, RMG)`; Sales sees only its own
`created_by`; `ensure_visible` raises **404, not 403**, so existence never leaks.
`requirement_label()` swaps REQ-xxxx for the parent opportunity's `opp_id` in bell notifications and emails.

**Candidate profile** — `PipelineStatus` has **17** values (not 20):
`Sourcing, Technical_Screening, RMG_Review, Sales_Screening, Customer_Screening, Customer_Interview,
L1_Feedback, L2_Feedback, Shortlisted, Customer_Approval, Preboarding, Joined, Sales_Rejected,
RMG_Rejected, Customer_Rejected, Self_Withdrawn, Rejected`.
`TRANSITION_MAP` is composed (`services/candidate_profiles.py:29-130`) from `_FORWARD` +
`_BACKWARD` (`Customer_Screening→Sales_Screening`, `Customer_Interview→Customer_Screening`,
`L1_Feedback→Customer_Interview`, `L2_Feedback→L1_Feedback`) + `_STAGE_REJECTIONS` +
`_ALWAYS = [Self_Withdrawn, Rejected]` (suppressed for `Customer_Screening` via `_NO_GENERIC`).
`STAGE_AUTHORITY` decides who may move **out of** a stage: TA owns Sourcing/Technical_Screening,
RMG owns RMG_Review, Sales owns the Screening pair, Sales+Sales_Head own the interview stages and
Shortlisted, **`Customer_Approval` is Sales_Head ONLY** (separation of duties), Preboarding is HR+Sales_Head.
Entering `Customer_Approval` requires an existing offer (`ENTRY_REQUIREMENTS`).
`perform_transition` (`:587-652`) order: comment length → value validity → terminal → allowed-next →
`user_may_transition_from` (403) → entry requirement → apply → stamp dates → activity log →
auto-record customer round → notify → on `Joined`, cascade `check_and_mark_fulfilled`
(double-wrapped in bare `except: pass` — a join can never fail on a fulfilment error, but a broken
`check_and_mark_fulfilled` is now invisible).

**`PROFILE_VISIBILITY` is `{}` since 18 Aug 2026** (user decision) — every CRM role sees every
pipeline stage. It previously scoped Sales/Sales_Head to Sales_Screening-onward, which hid a candidate
TA had just applied. `_SALES_VISIBLE` stays as the documented set Sales *owns* (filter chips).
Pinned by `test_pipeline_handoff.py::test_every_role_sees_the_whole_pipeline`.

**Timesheet** — `Draft | Submitted | Approved | Rejected` (UI labels: "Pending for Submission" /
"Pending for Approval"). `EDITABLE_STATUSES = (Draft, Rejected)` gates period-update, entry upsert and
submit. Approve requires exactly `Submitted`; reject accepts `(Submitted, Approved)` — the correction
path — but 409s once an invoice exists. Approve/reject is `gated_write_action("timesheet.approve"/"…reject",
"timesheets", "RMG", "Sales")` — **HR is deliberately excluded from deciding** even though HR is on the
notification list. `KARNEX_CRM_USER_GUIDE.md` says otherwise; **the code is right**.
Ledger effects apply as **deltas at both submit and approve**; reject/delete run
`reverse_timesheet_ledger_effects`.

Billing precedence (bill XOR credit): `week_off_billable`/`holidays_billable` > `comp_off_billable` >
comp-off credit. Any billing path means zero comp-off earned that day. Unworked week-off/holiday days
bill a full `min_hours_full_day` when their flag is on (calendar-month model). Comp-off balance/limit
fields apply only when `comp_off_billable` is **off** (credit mode).
`_is_comp_off_work_day` treats `day_type` as **authoritative** (a Working Saturday bills, never credits)
and honours `policy.week_off_days` for legacy rows with no `day_type`.
Policy inheritance everywhere: **project override → branch → customer default → built-in default**,
resolved field-by-field by `services/branch_policy.py::resolve_branch_project_policy`.

**LOP reduces Monthly bills** (13 Aug 2026, `timesheet_invoice_preview`): each LOP day deducts
`rate × lop / working_days_in_window`, and the line's `qty` becomes `amount / rate` so Qty × Rate always
reconciles. Hourly/Daily and the non-leave-billable Monthly ratio already zeroed LOP days.
Pinned by `test_midmonth_timesheet.py::test_lop_reduces_a_monthly_invoice`.

**WEEKEND WORK COVERS LOP** (the "Harman rule", `timesheet_summary`): in comp-off **credit** mode a
worked week-off/holiday day first makes up an LOP day — summary/preview report NET LOP
(`lop_covered_days` exposed), the Monthly invoice bills the full month, and the covering fraction earns
NO comp-off. Billed modes untouched. Pinned by `test_midmonth_timesheet.py::test_weekend_work_covers_lop`.

**Leave** — accrual is a **job, not a trigger** (`services/project_employee_leave_credit.py`).
`run_pe_leave_credit()` credits only the `as_of` month and never backfills; `apply_year_end_carry()`
acts only on 31 December. The scheduler runs it daily (`JOBS["pe_leave_credit"]`) and repairs itself:
`missing_credit_periods()` finds closed months with no `pe_credit:` ledger row and replays them at
month end. Replay is safe because credits are keyed `pe_credit:{pe}:{type}:{YYYY-MM}`. The
`leave.credit_repaired` email fires only when a repair actually **moved balances**.
Deliberate repairs beyond `scheduler.pe_leave_credit_lookback` (12 months) go through
`scripts/run_pe_leave_credit.py --from YYYY-MM [--to] [--all-periods] [--dry-run]`.
`LeaveApplication.status` is a plain `String(16)`, **not** a `pg_enum` — values policed only by
`schemas/leave.py::LEAVE_APP_STATUSES = ("Pending","Approved","Rejected","Cancelled")`.
Approve/reject are `role_required("HR")`; create/update/cancel/delete are bare `get_current_user`
plus self-scoping.

**Invoice/PO** — invoice cannot exceed PO balance (400). `GET /{id}/po-options` lists candidates; with
multiple live POs and no `po_id` the generate call 400s rather than silently picking. `due_date` parsed
from PO `payment_terms` ("Net 30 Days" → 30). `POST /purchase-orders/{id}/renew` raises the next PO in a
series (migration 0068). Three deliberate non-behaviours: the old PO is **not** touched, unspent balance
does **not** carry over, allocations are **not** copied. `po_number` is required — it is the customer's
reference, so generating one would invent a document.
`finance.py:674` uses `@router.api_route(methods=["PUT","PATCH"])` — a grep for `@router.put` misses
invoice update entirely.

---

## 6. Interview engine essentials

- Sessions are a **plain in-process dict** (`session.py`, 20 lines), as is proctor state.
  **`UVICORN_WORKERS` must stay 1.** The Redis-backed store in the docstring **is not implemented**
  anywhere; `REDIS_URL` is read only by `rate_limit.py:49` and the multi-worker warning at `main.py:2408`.
- Session keys (`main.py:751`): `inv:{invite_token}` · `hr:{sub}` · `"demo-session"` fallback.
- Two entry points: `POST /setup` (HR direct) and `POST /candidate/invite/{token}/login`
  (device-bound via the `x-device-id` header).
- `GET /next` → `candidate/service.py::next_question_payload`. **The server owns the clock and the
  count**; the warm-up question ("Please introduce yourself.") is index 0 and is invisible to the
  progress UI and to scoring.
- `POST /answer` is idempotent per turn index (`main.py:3585`); skips can be promoted to answers when
  VAD evidence shows the candidate spoke (**409 `speech_blocked`** otherwise). It is the **only**
  handler that takes `session_lock`.
- Transcription is **not** Whisper-the-model: `OPENAI_TRANSCRIBE_MODEL`, default `gpt-4o-mini-transcribe`.
  TTS: `gpt-4o-mini-tts` / voice `nova`, with an in-process LRU prewarm cache (24 entries / 900 s).
- Scoring runs **deterministic guards in `ai.py` before any model call** — `preflight_per_question_evaluation`
  (`ai.py:544`) can return a full zero/capped row and skip the model entirely; `apply_quality_caps_to_per_question_row`
  (`:630`) is the post-model ceiling. Then three separate rubrics:
  `evaluate_per_question_interview_batch` (`:1161`), `evaluate_with_model_skill_based` (`:2913`),
  `evaluate_communication_skills` (`:3425`).
- **`merge_per_question_eval_into_report` (`ai.py:1423`) overwrites `overall_score`** with the mean of
  evaluable per-question scores, and also rewrites `technical_score`, `problem_solving_score`, clamps
  each `skill_scores[].score`, and re-derives `recommendation`/`overall_fitment`. The skill model's
  number survives only as `skill_model_overall_score`. Anything that reads a score must know this runs last.
- `POST /submit`: candidates get a fast fallback report (`report_status="ready_pending_ai"`) upgraded by
  a BackgroundTask; HR gets the synchronous path. A startup recovery worker (`main.py:2341`) finalizes
  stale sessions as `recovered`.
- **`ai.py` reads no model env var at all** — every `model=` default is the literal `"gpt-4o-mini"`.
  Overrides live in callers (`INTERVIEW_OPENAI_MODEL`, `OPENAI_TRANSCRIBE_MODEL`, `OPENAI_TTS_MODEL`,
  `OPENAI_TTS_VOICE`).
- **Dead code, grep-confirmed:** `backend/prompts/interview/*` (104 L, only `tests/test_prompt_builder.py`
  references them) · `validators/interview/validate_question_objects` (defined, never called anywhere) ·
  `ai.py:2270 generate_questions_with_model`, `:2148 evaluate_turns_batch_with_model`,
  `:2489 generate_one_question_per_skill` (0 callers, ~330 L).
- The live question prompt is `services/interview/question_service._single_template_prompt` — a **user
  message with no system message** (`:130`). ⚠️ `generate_mode_aware_questions` declares eight arguments
  (`interview_mode, skills, experience, role, tech_stack, resume_summary, jd_text, cv_text`) that the
  prompt never uses; `INTERVIEW_MODE_AWARE_GENERATION` therefore does not make generation mode-aware.

**Two ATS engines, cleanly split, zero shared code:**

| | Engine A (legacy) | Engine B (CRM) |
| --- | --- | --- |
| File | `backend/ats.py` (586 L) | `backend/services/ats_scoring.py` |
| Style | weighted `AtsWeights` + embedding cosine + an LLM variant | pure deterministic, no DB, no OpenAI |
| State | JSON files (`data/ats_cache.json`, `data/job_configs.json`) | none |
| Used by | `/ats/score`, `/ats/score/upload`, `/candidates/ranked`, `/job/config*` | CRM requirement resume pipeline, CRM dashboards |

**Ask AI** (`ai_help/`) is a separate read-only CRM feature. KB is `ai_help/entries.py` (18 `HelpEntry`
TypedDicts) + `business_rules.py`; `all_entries()` is `lru_cache`d so **KB edits need a process restart**.
`ai_help/tools.py` holds 10 **SELECT-only** query tools; `assist.py::_run_tool_rounds` allows up to
`_MAX_TOOL_ROUNDS = 3` lookups then drops the tool list. Rules that must survive any edit:
permission filtering happens in `routers/crm/ai_assist.py` where the user is known (and `run_tool`
re-checks at execution); `FINANCE_ROLES` is duplicated there and in `crm_data.py` **on purpose**;
tools **never raise** (errors come back as `{"error": …}`); rows clamped `MAX_ROWS = 25`, payloads
`_MAX_RESULT_CHARS = 4000`. This is also the **only rate-limited CRM endpoint** (20/min).

---

## 7. Migrations

- **Single root `0001`, single head `0097`** (0080–0097 uncommitted; the 18 Aug walk below covered 0001–0079). Chain walked mechanically: length 77,
  **0 unreachable revisions, 0 dangling `down_revision` references.** Linear, no branches, no merges.
- **Revisions `0026` and `0027` do not exist** — `0028.down_revision == "0025"`. Alembic is happy;
  revision ids are opaque strings. **Do not "fix" it.**
- `alembic/env.py` excludes the legacy tables and the `registration_data` stub.

| Rev | Summary |
| --- | --- |
| 0069 | `projects.opportunity_id` → NULLABLE. Projects need no sales opportunity; all branch/serializer paths are null-safe. |
| 0070 | Pre-ladder template grants `"edit"` → `"create"` (the old editor's max mode was "Insert / Edit"; the ladder would have silently demoted them). |
| 0071 | `notification_routes.subject_template/body_template` — per-event email wording is admin data now, applied by `email_outbox._apply_event_template` with plain `replace`, **never `str.format`** (a typo'd token renders literally, never drops mail). |
| 0072 | `week_off_days` CSV (0=Mon..6=Sun) on `customer_billing_policies` / `customer_branches` / `projects`; `parse_week_off_days` is strict — **any bad token invalidates that level entirely** so it falls through the chain. |
| 0073 | `timesheets.period_start_date/period_end_date` — per-sheet window override (`PATCH /{id}/period`); NULL = derived from PE onboarding/exit. `upsert_entries` validates against `sheet_period_bounds`, not `pe_period_bounds`. |
| 0074 | `invoice_lines.qty` `Numeric(10,2)` → `(12,4)`. A Monthly qty is the billed *fraction* of the month; at 2dp qty×rate drifted up to 1 %. The engine now **redefines** `amount = qty(4dp) × rate`, and both preview and generate pass `lines=[]` to `karnex_gst_tax_and_grand` so GST is computed on the exact sub-total. |
| 0075 | `timesheets.approved_figures` JSONB — invoice figures **frozen at approval** (after `consume_timesheet_leaves`, so the paid-vs-LOP split is final). `_apply_frozen_figures()` swaps them into invoice-preview and generate-invoice, exposing `figures_drifted` / `live_sub_total` / `frozen_at`. Reject clears the snapshot. Legacy pre-0075 approvals have NULL → live figures. |
| 0076 | `customer_rate_cards` — per-customer experience-band pricing with five NULLABLE rate columns (blank = "not quoted", never zero). API `/api/rate-cards`, gated Sales/Sales_Head via the `rate-cards` tab; bands may not overlap. |
| 0077 | `customer_rate_cards.branch_id` — rate cards are BRANCH-wise (NULL = customer-wide fallback; uniqueness + overlap scoped per branch). |
| 0078 | `billable_leaves_per_year` on `customer_billing_policies` AND `customer_branches` (branch wins) — the "APTIV rule": paid leaves the customer bills even when leave is not billable. |
| 0079 | `customer_rate_cards.effective_from` — VERSIONED slab ladders. A new ladder supersedes the old from its date; overlap + uniqueness scoped per (branch, version). The Opportunity form prices from the version current TODAY (NULL = since forever). |

Also settings-driven now (`services/org_settings.KEYS` → DB row → env → default): TDS rate
(`finance.tds_rate_percent`), the whole invoice seller/bank block (`invoice.*`, consumed by
`company_invoice_config`), and `uitext.*` copy overrides served by `GET /api/ui-text`.

---

## 8. Running it

```bash
# Windows, the normal path (runs alembic upgrade head, then uvicorn on :2020)
start_app.bat                      # --http | --https | --no-browser
rebuild_all.bat                    # builds the F-V2 dashboard first, then start_app

# Docker
docker compose up -d --build       # monolith + postgres on :2020
docker compose --profile cache up -d   # + redis (used only by rate limiting)

# Migrations
cd backend && python -m alembic upgrade head && python -m alembic current   # head = 0097

# Tests — run from backend/, no live DB needed
cd backend && python -m pytest -q
pip install python-multipart httpx  # one-time, needed by the TestClient suites
```

There is **no `pytest.ini` / `pyproject.toml` / `conftest.py`** anywhere. Tests must run from
`backend/` or imports fail; CI works around this with `python -m pytest backend/tests` from the root.

⚠️ **The tests cannot run against a read-only checkout.** `main.py:2469` calls `init_auth_db()` at
import time, so a mounted/read-only tree fails collection on 5 files with
`sqlite3.OperationalError: attempt to write a readonly database`. Copy `backend/` somewhere writable first.

---

## 9. Test health (measured 18 Aug 2026)

```
815 collected · 807 passed · 8 failed · 0 errors · ~23 s
```

| Failure | Status | Cause |
| --- | --- | --- |
| `test_boundary_question_finalize::test_boundary_metadata_on_timer_auto_save` | known | asserts note "Boundary Question Evaluated"; code writes "Auto-submitted on timeout" |
| `test_password_security::test_login_transparently_upgrades_legacy_hash` | known | fixture's legacy schema predates `is_active` |
| `test_password_security::test_register_then_login_uses_modern_hash` | known | same missing column |
| `test_timesheet_entry_grid::test_classify_calendar_day_defaults` | known | `Decimal('8') != Decimal('8.50')` |
| `test_timesheet_entry_grid::test_create_timesheet_generate_days_and_save_draft` | known | `8.0 != 8.5`, same threshold drift |
| `test_access_templates::test_api_validation_rejects_unknown` | **NEW** | expects 400, gets 200 — `_strip_removed_keys` now silently drops unknown tab keys **by design**. Stale test, not a regression. Rewrite it to assert the new contract. |
| `test_resume_checksum::test_filename_is_not_trusted` | **NEW** | `safe_upload_extension` now **raises 400** for a disallowed extension instead of sanitising. Production is stricter/safer; test not updated. |
| `test_timesheet_logic::test_issue4_seed_leave_skips_existing_types` | **NEW** | `AttributeError: 'SimpleNamespace' has no attribute 'branch_id'` — `resolve_effective_leave_policies` reads `pol.branch_id` since the branch-policy work; the stub was never updated. |

**`test_rmg_timesheet_reports` is on the documented known-failure list but now passes (7/7).**
`CLEANUP_REPORT.md` is stale on this point.
`backend/scripts/test_leave_scenarios.py` collects 0 tests (a live-DB operational script pytest picks up
by name).

All three new failures are **stale tests trailing deliberate production changes**. Rewrite, don't delete —
each pins a contract someone believed was still enforced.

---

## 10. Bugs, gaps and security findings

Ordered roughly by severity. Everything here is evidenced at a file:line.

### Interview half

| # | Finding |
| --- | --- |
| **A1** | 🟠 **`POST /candidate/invite/{token}/login` (`main.py:7286`) trusts the `verified` session flag rather than re-checking credentials.** It takes no body and compares no email or key; those are checked *only* in `/verify` (`:7208`, exact email match + `hmac.compare_digest` on the key). The one thing standing between an invite URL and a running interview is `main.py:7307-7309`: **if** the schedule has a stored `access_key` and `session_status` is not yet `verified`/`active`, login 403s "Please verify your identity first." So the hole is not universal — but it is wide open for any schedule created **without** an access key (A3), and the flag it trusts can be re-set by anyone via A2. Treat A1+A2+A3 as one fix: make login re-establish identity itself (or consume a short-lived, single-use verify token) instead of reading a mutable status column. |
| **A2** | Device takeover: the "already active elsewhere" check (`:7255`) runs only when `session_status == "active"`, but `/verify` itself sets `"verified"` — so a second caller can re-verify mid-interview and overwrite `active_device_id`, 403-ing the original candidate. |
| **A3** | `main.py:7238` — a schedule created without an access key falls through to email-only verification. |
| **A4** | `login_attempts` is a **lifetime counter with no reset** (`:7216`, incremented on *every* `/verify` including successes). After 10 total attempts the invite link is permanently locked; no reset path exists in the codebase. |
| **A5** | `POST /report` returns the **globally latest submitted session** (`_latest_submitted_session`, `:4121`) regardless of ownership. |
| **A8** | Device binding is enforced on **2 of 11** candidate endpoints (`/answer`, `/submit`). Not on `/next`, `/candidate/transcribe`, `/candidate/tts`, `/candidate/validate-speech`, `/session-status`, `/interview/violation`, or any `/proctor/*`. |
| **A9** | `/proctor/violation` and `/proctor/end-session` have **no ownership check**, and `end-session` writes `reports[candidateId]` from a **client-supplied form field** (`:6512`) — one candidate can overwrite another's proctor report. |
| **A10** | Auth failures return **HTTP 200** with `{"error": …}` (`/auth/login` `:6889`, `/auth/register` `:6839`). |
| **A11** | `/auth/refresh` never re-reads the user — deactivation and role changes don't take effect until the current token expires. |
| **B1** | 🔴 **`GET /api/prompt-logs/export` is unreachable** — declared at `routers/admin.py:215`, *after* `GET /api/prompt-logs/{log_id}` at `:194`. Every request binds `log_id="export"` → 404. The frontend calls it (`F-V2 src/api/promptLogs.ts:117`); **the export button is broken**. One-line fix: move it above. |
| **B2** | `_should_recover_progress` (`main.py:2272`) returns **`True`** for terminal statuses (`completed`, `terminated`, `abandoned`, …); the only escape is `report_status == "ready"`. Any finished interview whose report never reached `ready` is re-finalised **every recovery interval, forever**. |
| **B3** | `INTERVIEW_SAFE_MODE` does not disable all OpenAI calls. It is read at `main.py:1423, 5839, 6059` and `question_service.py:228` only. `_evaluate_and_store_report` calls the skill and communication rubrics unconditionally; `/candidate/tts` and `/candidate/transcribe` never check it. |
| **C1–C3** | `/submit` (`:4023`), `/next` (`:3140`) and `/setup` (`:2982`) mutate `sessions[sk]` with **no `session_lock`**, concurrently with a locked `/answer`. `/submit` pops the session outside the lock. |
| **C4** | `_proctor_sessions` counters are read-modify-write with no guard (`:6455`), unlike the three cache locks elsewhere in the file. |
| **C6** | Unbounded per-process state: `_proctor_sessions` (never popped), `_session_locks` (never pruned), `_auth_rate_hits` keys, `_INVITE_BOOTSTRAP_LOCKS`, `_INVITE_PREWARM_STATE`. |
| **D** | Two security-headers middlewares are both registered (`main.py:2531` and `:2678`) with conflicting CSP. Starlette's ordering means **the strict one at 2541 wins and 2690 is dead**. Consequences: (a) `SECURITY_HEADERS_ENABLED=false` does not disable headers — it silently *downgrades* the CSP and drops `camera`/`microphone` from Permissions-Policy; (b) HSTS is emitted on plain HTTP regardless. Delete `:2678-2691`. |
| **E** | CORS with `CORS_ALLOW_ORIGINS` unset falls back to a regex matching **any** IPv4 origin (`main.py:2500`). In production it logs a warning and proceeds. |
| **F** | Exception swallowing: 77 `except Exception` in `main.py`, 30 in `auth_db.py`, 11 in `ai.py`; bodies ending in bare `pass`: 17 / 10 / 7. Notable: `question_service.py:141` and `:255` fall through to accepting **unvalidated** model output. |

### CRM half

| # | Finding |
| --- | --- |
| **8.1** | 🟠 **Seven read endpoints are gated only on `get_current_user`, which requires no CRM role, and do no in-body scoping**: `credit_notes.py:107` + `:124` (**every credit note, amounts and invoice links, no filter**), `holidays.py:61/:96/:206`, `leave_policies.py:83/:130` (full customer commercial leave policy). Give them `any_crm_role` at minimum, `gated_read(...)` ideally. |
| **8.6** | 🟠 No rate limiting on the public endpoints. `POST /api/apply/{token}` (`apply.py:218`) is an **unauthenticated multipart upload** that writes to disk, creates `Resume` + `Candidate` rows and can auto-advance a requirement. `POST /api/book/{token}/confirm` (`slots.py:342`) schedules a real AI L1 interview (OpenAI cost) and sends email/WhatsApp. The apply token (`apply.py:59`) is **deterministic and non-expiring** — no timestamp, no revocation short of rotating `AUTH_SECRET`. |
| **8.2** | Optimistic concurrency exists on **exactly one entity** (Opportunity). Projects, customers, branch policies, timesheets, requirements, profiles, POs and invoices are last-write-wins. With the new hub tabs putting several roles on the same customer record, this is the most likely source of a future "my edit disappeared" report. |
| **8.3** | `create_invoice` (`finance.py:583-599`) computes each line's amount server-side, then lets a client-supplied `body.sub_total` **override the line sum**. GST is computed from `sub_total`, so tax is internally consistent, but the persisted invoice can disagree with the sum of its own lines and the PDF renders both. The timesheet-driven path does not have this hole. |
| **8.4** | N+1 on list endpoints: `customers.py:92` → `serialize_customer` does a `has_po` query **per customer** (up to 100/page — a one-line `IN` fix); also `opportunities.py:186`, `leave_applications.py:298`, `leave_policies.py:102`, `customers.py:806/:1005`, `projects.py:825`, `ai_interviews.py:193`. `services/candidate_profiles.py::enrich_profiles_list` shows the batched pattern to copy. Separately, **every gated request costs 3–4 uncached auth queries** (`get_current_user` ×2 + `effective_access` ×1–2). |
| **8.5** | `access_templates.can_edit_tab` ignores the ladder (§3). Fix or delete. |
| **8.7** | `files.py:23 GET /api/crm-files/{rel_path:path}` is `any_crm_role` — any CRM role can fetch any uploaded artefact (CVs, contracts, payment proofs). Mitigated (traversal-proof, `uuid4` filenames, `nosniff`, inline allow-list) but it is capability-URL security: a URL once seen stays valid. `me_profile.py:173` is the better-hardened template. |
| **8.8** | Deliberate swallows that should at least log: `timesheets.py:578` (a systematically failing preview silently disables the 0075 freeze for every approval, with no log line), `candidate_profiles.py:647/:649`, `apply.py:298`. Unexplained ones worth a look: `tax_invoice.py:168/:1450`, `project_employees.py:1224`, `resumes.py:136`, `email_flows.py:357`. |
| **8.9** | `masters.py` lets RMG/Sales/TA quick-add `skills` and six roles quick-add `contact-roles`, but `update_item` is **always** `admin_only` — those roles can create master values they cannot then fix. |
| **8.10** | No SQL injection. Six f-string `sa.text()` sites all interpolate a module constant or a hardcoded literal tuple; every user value is a bind param. `sort_by` resolves through `_SORTABLE` dicts. |

### Cross-repo (see also F-V2 §Parity)

| # | Finding |
| --- | --- |
| **P0** | 🔴 **`/apply` and `/book` are missing from BOTH `vite.config.ts::API_PROXY_PREFIXES` and `scripts/vercel-build.mjs::apiPrefixes`.** `Requirements.tsx:2848` builds the recruiter-visible booking link as `${window.location.origin}/book/${token}`; on Vercel there is no rewrite, so the candidate gets the SPA shell or a 404. The TA apply link is safe only while it is generated from the backend's own `request.base_url`. Add both prefixes to both files. |
| **P1** | 🔴 **The branch hours cap exists only on the client.** `ctcSlab.ts:85-91` applies `max_billable_hours_month × 12`; `opportunity_ctc.py:53-54` has no equivalent, and `normalize_tm_billing_details` + `_replace_ctc_slab` **overwrite** whatever the form sent. Measured with cap 180 on the 227-day base: the form shows 2,160 h / ₹2.16 M annual revenue; the server stores 1,816 h / ₹1.816 M — a silent **19 % understatement**. The cap is not a schema field, so it is stripped from the payload and the server could not honour it even if it wanted to. Decide which engine is right and make the other match. |
| **P2** | `opportunity_ctc.py:162` uses `int(target − exp_min) − 1` (truncates); `ctcSlab.ts:175` uses `Math.round(...) − 1`. For exp_min 5 → target 7.5 the backend derives 1 cycle (₹90,909) and the frontend 2 (₹82,645). Same class of bug for legacy non-digit `appraisal_cycle` strings (`"2.6"` → 1 vs 3). |
| **P3** | Timesheet day figures: backend returns `ONE` day for an unworked billable week-off/holiday; `Timesheets.tsx:222` uses `hours / 8`. With `min_hours_full_day = 9` the grid shows 1.13 days where the invoice counts 1.0. Attendance derivation also differs — the backend uses policy thresholds, `crm/lib/timesheetAttendance.ts:10-14` **hardcodes 8 h → Present, 4 h → Half_Day**. On a 9-hour customer an 8-hour day is Present (LOP 0) in the grid and Half_Day (LOP 0.5) on the server. Hour-cap ordering differs too (backend caps hours but derives the day fraction from *uncapped* hours). |
| **P4** | Opportunity form schema drift: `project_scope` is allowed server-side for Work_Package/Fixed_Price/Retainer but **has no UI field at all** — those types submit no type-specific data. `project_duration_months` likewise has no field, so Fixed_Price annualisation can never receive a duration. `sales_stage` is computed by the form but has no schema field, so `stripHiddenFields` drops it and **it is never persisted**. `test_opportunity_form_schema.py` only introspects the *server's own* key sets — it cannot catch any of this. |
| **P5** | Cross-domain status-label collision: `ui.tsx:48` maps `Shortlisted → "Customer Shortlisted"`, but `Shortlisted` is also `AtsStatus.SHORTLISTED` — an internally-shortlisted resume is mislabelled. Same flat-keyspace problem in `statusHelp.ts` for `Rejected`, `Draft`, `Cancelled`, `Approved`, `Pending`, `Active`. |

---

## 11. Known state and structural debt

- **`services/` (repo root) is scaffolding.** 142 lines total; `karnex_proxy/app_factory.py` builds a
  catch-all httpx proxy to the monolith. Zero domain logic, no DB, no auth. CI builds the images;
  production runs the monolith only. RabbitMQ is provisioned in the overlay and used by nothing.
  `k8s/` has manifests for 2 of 4 services and no kustomization. Treat all of it as aspirational.
- **Port mismatch**: base compose publishes the monolith on 2020; the microservices overlay and nginx
  upstreams assume `ai-interview:8010`.
- `main.py` is 325 KB and mixes app assembly, auth, HR endpoints, proctoring, ATS and static-build
  orchestration. The natural seams already exist as directories (`hr/`, `candidate/`, `ats.py`).
- **Six duplications** (see also `FEATURE_INVENTORY.md`): two ATS engines · two customer/opportunity
  stores (`/masters/*` aliases still read by the old template UI) · two role systems · two schema
  mechanisms · three timesheet billing implementations (`services/timesheets.py::compute_billables`,
  `Timesheets.tsx::computeBillables`, `services/project_employee_billing.py::compute_billable_days`) ·
  two GST paths (`services/tax.py` pure vs `services/finance.py` DB-aware). Also three copies of
  `_db_target()` (`ai.py:29`, `ats.py:23`, `question_service.py:21`) and two `_is_production_env()`.
- `POST /api/admin/nexus/seed` and `/api/admin/nexus/leave-credit` in `users_admin.py` are labelled
  TEMPORARY test support and are live behind Admin/CEO.
- Not implemented despite being in the runbook: AES-256-GCM at rest for candidate PII, data-retention /
  candidate-delete, TLS termination (HSTS middleware exists and activates under TLS).
- `_to_delete/` is a gitignored quarantine bin — safe to delete. `newfiles/` is a redundant source copy
  of already-applied email-outbox files.
- Rate limits are **off by default outside production** (`rate_limit.py:23`), and `limit()` binds the
  limiter at *decoration* time. `setup_rate_limit(app)` is currently called at `main.py:2356`, right
  after `app = FastAPI(...)` and before every decorator, so the ordering hazard is satisfied — but it
  now logs `ERROR` if it ever no-ops while enabled. There is also an independent hand-rolled limiter
  (`_allow_auth_rate_limit`, `main.py:2627`) for `/auth/login` and `/auth/register` that is always on.

---

## 12. Readiness for new work

**Safe to extend**

- Adding a CRM router (importlib registry isolates failures; `_MODULES` is guarded by a test).
- Adding an Access-Template tab or field — `TABS` / `FIELDS_BY_TAB` are pure data and everything
  derives from them; removal degrades silently rather than 400-ing every save.
- Adding a master resource — one `_register(...)` call in `masters.py` yields four correctly-gated,
  enveloped, paginated endpoints.
- New gated endpoints via the `gated_read/write/create` ladder; `gated_write_action` gives
  runtime-editable role lists for free.
- Notifications and email — `notify_*` + `email_outbox` are well-factored (same-transaction queueing,
  dedupe keys, admin-editable routing and wording, `FOR UPDATE SKIP LOCKED` drain).
- Migrations — linear, single head, unbroken.
- Deterministic services: `opportunity_ctc`, `tax`, `branch_policy` helpers, `project_employee_billing`,
  `candidate_match`, `ats_scoring`, and the whole `ai.py` guard family (`:125-737`) — pure, no I/O,
  individually testable. `utils/*` and `candidate/service.py` likewise.

**Fragile — write the test first**

- `services/timesheets.py` (2,433 L) + `routers/crm/timesheets.py` (1,312 L). Billing precedence,
  comp-off, LOP coverage, the calendar-month model, the 0075 freeze and **three** period-resolution
  functions (`pe_period_bounds` / `sheet_period_bounds` / `period_bounds`) all interact.
- `services/project_employees.py` (1,314 L) — leave seeding, rate history and exit settlement in one
  module, feeding both the timesheet and invoice engines.
- `routers/crm/customers.py` (1,086 L, 37 endpoints) — four distinct nouns and two `gated_*` tab keys
  in one file; the most route-order surface area in the repo.
- `main.py` — no module boundary to lean on; route order, middleware order and `_require_user`'s tuple
  contract are all load-bearing and none is type-enforced.
- The session dict — an untyped `dict` with ~25 keys written from 8 handlers and 2 background threads,
  with inconsistent lock discipline.
- `merge_per_question_eval_into_report` — silently rewrites five report fields including `overall_score`.
- `auth_db.py` — every function written twice (Postgres + SQLite branches); easy to update one and not
  the other.

**Recommended order of work**

1. **A1 + A2 + A3 together** — make invite login re-establish identity itself (a short-lived, single-use
   verify token consumed at login) instead of trusting the mutable `session_status`, make the access key
   mandatory, and stop `/verify` from re-binding a device on a session that is already under way.
2. **P0** — add `/apply` and `/book` to both frontend prefix lists.
3. **P1** — resolve the CTC hours-cap divergence (money on screen ≠ money in the DB).
4. **8.1** — gate the seven bare-`get_current_user` reads.
5. **8.6** — rate-limit the two public POSTs; add an expiry claim to the apply token.
6. **B1** — move `/api/prompt-logs/export` above `/{log_id}` (one line, restores a broken button).
7. **B2** — exclude terminal statuses from `_should_recover_progress`.
8. **D** — delete the dead security-headers middleware at `main.py:2678`.
9. **C1–C3** — put `/next`, `/submit`, `/setup` under `session_lock`, or hide the session behind a typed
   façade that acquires it.
10. Rewrite the three stale tests (§9); add a route-order invariant test; add a `log.exception` to
    `timesheets.py:578` and `candidate_profiles.py:647`.
11. Delete the confirmed-dead code in §6 (~430 L) and the two tests that exist only to keep it alive.
12. Convert `_require_user` to a real `Depends()`; drop the dead `"manager"`/`"admin"` role names.

---

## 13. Docs worth reading before big changes

| File | Why | Trust |
| --- | --- | --- |
| `KARNEX_CRM_README.md` | bootstrap + env | **stale** on table/migration counts |
| `KARNEX_CRM_USER_GUIDE.md` | role walkthrough | **diverges from code** on HR timesheet approval, CEO, and Customer_Approval authority — trust the code |
| `FEATURE_INVENTORY.md` | endpoint/table inventory + the duplications | counts stale (see header of this file) |
| `TIMESHEET_LOGIC_TEST_REPORT.md` | ISSUE-1..5 | ISSUE-1 closed 12 Aug 2026; rest partly open |
| `LEAVE_SCENARIO_TEST_REPORT.md` | the monthly-credit-job operational risk | good |
| `QA_LEAVE_SYSTEM_TEST_REPORT.md` | live-DB leave verification + teardown SQL | good |
| `TEST-CASES.md` / `TEST-RESULTS.md` | the Project-Employee UC-01..12 acceptance spec | good |
| `ACCESS_TEMPLATES_HANDOFF.md` | what still needs `Depends(require_access(...))` | see §3 gap list |
| `PRODUCTION_RUNBOOK.md` | deploy steps + hardening list | migration numbers stale |
| `NEXUS_VS_KARNEX_GAP_ANALYSIS.md` | the 8 prioritised product gaps vs the Zoho app | good |
| `CLEANUP_REPORT.md` | known test failures | **stale** — `test_rmg_timesheet_reports` now passes; three new failures unlisted |
