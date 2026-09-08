# 4. API Request/Response Flow Diagrams

Two API surfaces share the same host and JWT:

- **Legacy interview API** — routes defined directly in `main.py` (`@app.*`), authorized
  by the JWT `role` claim via `_require_user`.
- **Karnex CRM API** — 20 routers under `/api/*`, authorized by DB-backed RBAC via
  `role_required`, returning a uniform envelope `{success, data, message, errors, meta}`.

## 4.1 API surface map

```mermaid
graph LR
    subgraph legacy["Legacy Interview API (main.py)"]
        auth["/auth/* — register, login, me, logout"]
        setup["/setup, /extract-skills"]
        loop["/next, /answer, /submit, /report"]
        media["/candidate/tts, /candidate/transcribe, /candidate/validate-speech"]
        invite["/candidate/invite/{token}[/verify|/login]"]
        hrrec["/hr/*, /hr-records, /hr-record/{id}"]
        job["/job/config[s], /job/template/*"]
        atsapi["/ats/score[/upload], /candidates/ranked"]
        proctor["/proctor/*, /interview/violation, /interview/integrity-logs"]
        health["/health/live, /health/ready, /version, /models"]
    end

    subgraph crm["CRM API (/api/*)"]
        me["/api/me · /project-leave · /leave-balances"]
        customers["/api/customers · DELETE (cascade policies/holidays) · /all-branches<br/>POST/PUT …/contacts (Sales/Sales_Head/Finance) · /api/opportunities · DELETE (cascade req/profile/AI)"]
        req["/api/requirements · attachments · /api/resumes"]
        candp["/api/candidates · DELETE (cascade profiles) · /api/candidate-profiles"]
        aiint["/api/candidate-profiles/{id}/ai-interviews"]
        proj["/api/projects · DELETE /{id} (cascade PE/TS/unpaid INV)<br/>/all-employees · /employees/{pe_id} · DELETE employees/{pe_id}<br/>rates · leave-policies · /api/timesheets · DELETE /{id}"]
        leave["/api/leave-applications · DELETE /{id}<br/>/api/holidays · /api/holiday-names · /api/customer-leave-policies"]
        finance["/api/purchase-orders · /api/purchase-orders/{id}/invoices · /api/invoices · /api/tds<br/>Tax Invoice: /api/invoice/totals|pdf|bulk|template · /api/states<br/>GET /api/invoices/{id}/tax-invoice.pdf"]
        emp["/api/employees · DELETE (prefer deactivate)"]
        mast["/api/{masters} · /api/contact-roles · /api/settings"]
        usersadm["/api/users (Admin)"]
        dash["/api/dashboard/* · /api/reports/*"]
        notif["/api/notifications · /api/crm-files/*"]
        tplreq["/api/template-requests · DELETE /{id}"]
        askai["/api/ai/assist · /api/ai/help-context<br/>(Ask AI read-only help · any_crm_role · 20/min)"]
    end

    subgraph observ["Admin/Observability (routers/admin.py)"]
        plogs["/api/prompt-logs[/stats|/filters|/export]"]
        usage["/admin/ai/usage"]
    end
```

## 4.1b Ask AI (read-only help)

```mermaid
sequenceDiagram
    autonumber
    participant UI as AskAiPanel (PlatformTopBar)
    participant API as POST /api/ai/assist
    participant Auth as any_crm_role
    participant KB as ai_help KB
    participant LLM as tracked_chat_completion
    participant Log as prompt_logger (ai_assist)

    UI->>API: {message, route/tab_key, history[]}
    API->>Auth: Bearer JWT + ≥1 CRM role
    Auth-->>API: CurrentUser
    API->>KB: ground system prompt (tab + related + rules)
    API->>LLM: OpenAI chat (max_tokens cap, no stream)
    LLM-->>Log: anonymized route + question (no PII)
    LLM-->>API: reply (+ optional NAVIGATE_TO)
    API-->>UI: envelope {reply, navigate_to, actions[], read_only}
    Note over UI: User clicks "Go to {page}" → crmNavigate only (no writes)
```

## 4.2 CRM request/response envelope flow

```mermaid
sequenceDiagram
    autonumber
    participant UI as React CRM (crm/api.ts)
    participant API as FastAPI /api/* router
    participant Dep as crm_deps (auth chain)
    participant Svc as services/*
    participant DB as PostgreSQL

    UI->>API: GET /api/customers?page=1 (Bearer JWT)
    API->>Dep: get_crm_db()
    alt Postgres not configured
        Dep-->>UI: 503 CRM unavailable
    end
    API->>Dep: get_current_user() + role_required(...)
    Dep->>DB: SELECT user by sub + roles (user_roles)
    alt inactive / missing role
        Dep-->>UI: 403 Forbidden
    end
    Dep-->>API: CurrentUser(id, roles)
    API->>Svc: paginate + serialize (to_dict)
    Svc->>DB: SELECT ... LIMIT/OFFSET
    DB-->>Svc: rows
    Svc-->>API: items + meta
    API-->>UI: 200 {success:true, data:[...], meta:{page,total,pages}}
    UI->>UI: unwrap envelope, render (401→clear session+reload)
```

## 4.3 Legacy interview auth flow (role-claim)

```mermaid
sequenceDiagram
    autonumber
    participant UI as Static JS (core.js apiFetch)
    participant API as FastAPI @app.* (main.py)
    participant Auth as _require_user
    participant DB as auth_db (raw SQL)

    UI->>API: POST /answer (Bearer JWT, x-device-id)
    API->>Auth: _require_user(request, {"hr","candidate"})
    Auth->>Auth: _decode_token_from_header (HS256, exp)
    alt invalid/expired
        Auth-->>UI: 401 Unauthorized
    else role not allowed
        Auth-->>UI: 403 Forbidden
    end
    Auth-->>API: (payload, None)
    API->>API: _enforce_invite_device_binding (single device)
    API->>DB: append answer, persist interview_progress
    API-->>UI: 200 next-question payload
```

## 4.4 File upload flow (multipart, e.g. resume / CV / documents)

```mermaid
flowchart LR
    up["POST multipart<br/>(e.g. /api/requirements/{id}/resumes)"] --> role{"role_required('TA')"}
    role -->|denied| f403["403"]
    role -->|ok| save["crm_common.save_upload()<br/>uuid filename under data/crm_uploads/"]
    save --> url["stored URL: /api/crm-files/&lt;rel&gt;"]
    url --> row["insert DB row (resumes/documents)"]
    row --> resp["200 envelope with file URL"]
    serve["GET /api/crm-files/{rel_path}"] --> guard["resolve_crm_file()<br/>reject path traversal"]
    guard --> file["stream file (any_crm_role)"]
```

## 4.5 Per-user tab access (Admin/CEO)

Admin/CEO manage application login accounts on the Users page (`#/crm/users`):
create with email + password + CRM roles (`POST /api/users`), replace roles,
activate/deactivate, and delete. `GET /api/users` lists legacy `role='hr'`
accounts only (candidate interview logins are excluded). Deactivated users are
rejected at `POST /auth/login` as well as on CRM `/api/me`. Hard delete
(`DELETE /api/users/{id}`) detaches/reassigns FK refs to the acting Admin/CEO
when possible.

Admin/CEO also assign an explicit allow-list of UI tab keys per user via
`POST /api/users/{id}/tab-access`. Stored in `user_profiles.tab_access` (JSON text).
`NULL` → role-based sidebar defaults; non-null → only listed keys appear in the shell.

Users update their own profile (name, phone, password, avatar) via
`GET/PATCH /api/me/profile` and `POST /api/me/change-password` — Admin does not
edit another user’s password from the Users page.

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Admin/CEO (Users page)
    participant API as POST /api/users/{id}/tab-access
    participant DB as user_profiles.tab_access
    participant Shell as App.tsx + CrmApp.tsx

    Admin->>API: {tabs: ["crm:customers","iv:ats",...]} (Bearer JWT)
    API->>DB: UPSERT tab_access JSON
    DB-->>API: saved
    API-->>Admin: 200 {success, data:{tab_access}}
    Note over Shell: On next /api/me, user receives tab_access
    Shell->>Shell: tabVisible(null) → role defaults<br/>tabVisible([...]) → Admin allow-list
```

## 4.5b Access Templates (department/role-wise modes)

Reusable templates (`access_templates`) grant per-tab and per-field **view** or **edit**
modes. Users link via `user_profiles.access_template_id` (live — edits propagate).
`services.access_templates.effective_access()` merges template + per-user override (override wins).
Admin/CEO always get `full: true`.

**Enforcement:** `crm_deps.gated_read(tab, *roles)` / `gated_write(tab, *roles)` on major CRM
routers (customers, opportunities, projects, finance, employees, timesheets, …).

```mermaid
sequenceDiagram
    autonumber
    participant Admin as Admin/CEO
    participant TAPI as /api/access-templates/*
    participant Me as GET /api/me
    participant CRM as gated CRM endpoints

    Admin->>TAPI: POST template {tab_access, field_access}
    Admin->>TAPI: POST /assign {user_id, template_id}
    Me-->>Admin: access {tabs, fields, visible_tabs, source}
    CRM->>CRM: effective_access → 403 if view-only on write
```

## 4.6 Response caching & rate limiting (cross-cutting)

```mermaid
flowchart TB
    subgraph frontend["Frontend cache (api/client.ts)"]
        fc["apiGet: in-memory TTL cache + inflight dedupe<br/>for /hr/dashboard, /job/configs, /hr/schedules, logs"]
        inv["apiPut/Patch/Delete → invalidate cache"]
    end
    subgraph backend["Backend guards"]
        rl["rate_limit.py — per-IP windows<br/>/auth/login 10/min · /answer 60/min<br/>/candidate/tts 40/min · /transcribe 30/min"]
        rc["response_cache.py — optional OpenAI response cache<br/>(OPENAI_RESPONSE_CACHE_ENABLED)"]
    end
    fc --> backend
    inv --> fc
```

## 4.7 Project Employee ownership (delivery bridge)

```mermaid
flowchart LR
    map["POST /api/projects/{id}/employees"] --> seed["Seed leave_details from<br/>CustomerLeavePolicy copy<br/>accrual_start=max(effective,onboard)<br/>defer upfront if start future<br/>+ pe_seed LeaveAccrualEvent<br/>+ ensure current rate row"]
    list["GET /api/projects/all-employees<br/>?project_id&customer_id&status&group_by=employee"] --> enrich["Flat rows OR nested mappings[] · leave_balance_total · po_status"]
    detail["GET /api/projects/employees/{pe_id}"] --> nested["leave_details · leave_eligibility · leave_summary<br/>credit_history · leave_applications<br/>rates · timesheet_rollups · holidays · holiday_calendar · po<br/>+ customer_id/name · branch_id/name · project_name"]
    syncLeave["POST .../employees/{pe_id}/leave/sync"] --> seedGap["Idempotent seed missing leave types<br/>(Admin/HR; no balance reset)"]
    rates["POST/PUT .../rates"] --> sync["Sync pe.billing_rate<br/>when is_current_rate"]
    leaveApp["POST /api/leave-applications<br/>project required when active PE"] --> peBal["Debit PE leave_balance on approve"]
    credit["scripts/run_pe_leave_credit.py"] --> peBal2["Credit PE leave_details<br/>accrual_start=max(effective_date,onboarding)<br/>skip is_exit · months before start → 0"]
    exitEp["POST .../employees/{pe_id}/exit"] --> stop["Stop accrual · flag open TS<br/>settlement_pending + leave balances"]
    tsLock["POST /api/timesheets/{id}/entries"] --> holLock["Client calendar holidays locked<br/>day_type · view_flag · entry_project_id<br/>billable_day display = hours÷8<br/>leave_billable_by_type from project CustomerLeavePolicy"]
    tsCreate["POST /api/timesheets generate_days=true"] --> autoDay["Auto-classify Working/Week_Off/Holiday<br/>default hours = resolved max_billable_hours_day · else 8.50"]
    tsDetail["GET /api/timesheets/{id}"] --> leaveMap["billing_policy incl. comp_off_billable<br/>+ leave_billable_by_type + leave_balances_by_type<br/>+ max_billable_hours_day (project→branch)"]
    tsApprove["POST /api/timesheets/{id}/approve"] --> compOff["idempotent accrue+consume<br/>(deltas vs submit)"]
    tsSubmit["POST /api/timesheets/{id}/submit"] --> submitLedger["consume_timesheet_leaves<br/>+ accrue_comp_off<br/>Never blocks on PO"]
    tsReject["POST /api/timesheets/{id}/reject"] --> reverseLedger["reverse_timesheet_ledger_effects<br/>re-credit leave + reverse Comp-Off"]
    tsDel["DELETE /api/timesheets/{id}"] --> delRev["reverse ledger then hard-delete<br/>Draft/Submitted/Approved/Rejected<br/>409 if invoice-linked"]
    peSync["POST /api/projects/employees/{pe_id}/leave/sync"] --> seedLeave["seed_leave_details_from_customer_policy<br/>adds missing types only"]
    tsRpt["GET /api/timesheets/reports/{due|for-submission|approvals}"] --> rmg["RMG/HR/Finance gated_write"]
    tsAtt["POST/GET /api/timesheets/{id}/attachments<br/>DELETE /api/timesheets/attachments/{id}"] --> multi["Multi-file + legacy file_attachment_url"]
    inv["POST .../generate-invoice"] --> rateEng["timesheet_invoice_preview<br/>RateRow + split_period_by_rate<br/>+ comp_off_billable_qty line"]
    inv --> gstEng["karnex_gst_tax_and_grand<br/>tax_invoice.compute_totals<br/>persist tax + grand"]
    inv --> poGate["assert_po_allows_new_drawdown<br/>expiry / balance 100%"]
    invGet["GET /api/invoices/{id}"] --> gstSer["serialize_invoice gst block<br/>resolve_buyer_state_code → compute_karnex_gst"]
    invPatch["PATCH|PUT /api/invoices/{id}<br/>buyer_state_code"] --> gstSave["recompute GST · persist tax + grand + balance"]
    invPdf["POST .../generate-pdf"] --> gstPdf["same resolve + compute_karnex_gst tax rows"]
    taxPdf["GET .../tax-invoice.pdf"] --> mapTi["map_crm_invoice_to_tax_invoice<br/>resolve_buyer_state_code first"]
```

## 4.7b Project policy (Create + Edit Project wizard)

```mermaid
flowchart LR
    create["POST /api/projects<br/>write_projects · optional branch_id<br/>else opportunity.branch_id<br/>unset policy → seed from branch"] --> cols["projects.* overrides<br/>+ branch_id FK<br/>+ recurring_billing<br/>billing_frequency Weekly/Monthly/Quarterly/Yearly"]
    put["PUT /api/projects/{id}<br/>write_projects · optional branch_id"] --> cols
    list["GET /api/projects/{id}/leave-policies"] --> plp["project_leave_policies"]
    post["POST /api/projects/{id}/leave-policies<br/>leave_credit_type + leave_expire required<br/>leave_credit_timing + leave_expire_timing"] --> plp
    putDel["PUT/DELETE /api/projects/leave-policies/{id}<br/>+ nested …/{project_id}/leave-policies/{id}"] --> plp
    plp --> lpt["leave_policy_types FK"]
    ui["EditProjectWizard / BranchWizardModal<br/>LeaveBillingPolicyModal + shared PeriodTimingPicker<br/>Start_Of_Period|End_Of_Period under credit+expire"] --> create
    ui --> put
    ui --> post
    ui --> putDel
    peUi["PE Leave tab caption<br/>leave_detail_out cycle metadata"] --> peOut["leave_credit_type/timing<br/>leave_expire/timing"]
    plp -.-> peOut
    branchPol["POST/PUT /api/customers/branches/{id}/leave-policies<br/>same timing fields on customer_leave_policies"] --> clp["customer_leave_policies"]
    clp -.-> peOut
    ts["timesheets.effective_billing_policy<br/>ensure_project_branch_id: never overwrite<br/>backfill same-customer opp → PE/fallback<br/>_project_branch: project.branch_id → opp.branch_id"] --> resolve["project → branch → customer → defaults<br/>hours_required_* · week/leave/holiday/comp_off_billable"]
    ts --> caps["max_billable_hours_day via resolve_branch_project_policy<br/>Project.max_billable_hours_day ← branch max_billable_hours_per_day"]
    caps --> genHrs["generate_days + UI Present default Hours Worked<br/>fallback DEFAULT_WORKING_HOURS 8.50"]
    ts --> leaveTypes["leave_billable_by_type_map<br/>resolve_customer_leave_policies(project)<br/>is_billable · leave_credit_balance"]
    leaveTypes --> compute["compute_billables LEAVE branch<br/>per-type flag else policy.leave_billable"]
    ts --> weekend["Weekend/holiday worked hours<br/>week_off/holidays_billable &gt; comp_off_billable &gt; credit<br/>(bill XOR leave credit)"]
```

Create wizard steps: Project Details (name/opportunity/customer) → Leave & Holiday Billing Policy (combined holiday thresholds + leave-billing rows; create mode prefills from opportunity branch policy and persists `branch_id`) → Billing Properties. Edit keeps the same 2 policy sections (loads project’s own policy).
## 4.7 Customer Holiday Calendar (branch-year + names master)

```mermaid
flowchart LR
    names["GET/POST /api/holiday-names<br/>HR write"] --> master["holiday_names table"]
    global["GET/POST/PUT/DELETE /api/holidays<br/>HR write · Customer type REQUIRES branch_id<br/>auto-upserts branch_holiday_years + holiday_calendar_id"]
    branch["GET/PUT /api/customers/branches/{id}/policy<br/>linked_projects: Project.branch_id OR Opportunity.branch_id<br/>GET/POST/PUT/DELETE …/leave-policies<br/>GET/POST/PUT/DELETE …/holiday-years/…/holidays<br/>read_branch_policy · write_branch_policy"]
    freeze["PATCH .../holiday-years/{year_id}<br/>is_freeze blocks branch date edits"]
    master --> hol["holidays · holiday_name_id · observance Mandatory|Optional<br/>branch-scoped rows share holiday_calendar_id → branch_holiday_years"]
    branch --> hol
    global --> hol
    freeze --> bhy["branch_holiday_years"]
    hol --> ts["holidays_for_project_period<br/>Mandatory only → timesheet Holiday/0h"]
```

Customer holidays are owned by a **branch**. The Holidays tab and the branch
Holiday Billing Policy panel read/write the same `holidays` rows; creating or
updating via `/api/holidays` with a `branch_id` upserts the year header so both
UIs stay in sync.
