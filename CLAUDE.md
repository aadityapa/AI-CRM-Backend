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

**Migration head is now 0103** (was 0097 on 8 Sep; see the dated notes below) (`0080`…`0097` are untracked files under `alembic/versions/`).
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
`current_question`, fullscreen/visibility/focus context and an optional JPEG `evidence` upload → `data/
integrity_evidence/<token>/`, served by `GET /interview/integrity-evidence/{token}/{name}`), and on termination
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
