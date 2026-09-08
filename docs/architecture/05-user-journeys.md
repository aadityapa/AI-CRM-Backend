# 5. User Journey Diagrams (per role)

Roles come from two systems:

- **Interview platform** (`registration_data.role`): `candidate`, `hr`.
- **CRM RBAC** (`models/rbac.py::RoleName`): `Admin, CEO, Sales, Sales_Head, RMG, TA, HR, Finance`.

## 5.1 Role → CRM view access matrix

```mermaid
graph LR
    Admin --> A_all["ALL views (universal)"]
    Sales --> S["Opportunities (Pipeline + Requirements) · Candidate Profiles · Projects · Project Employees · Timesheets (create/edit) · Customers* · Reports"]
    SalesHead["Sales_Head"] --> SH["Opportunities (Pipeline + Approvals) · Projects · Project Employees · Timesheets (create/edit) · Executive Dashboard · Offers · Reports"]
    RMG --> R["Opportunities → Requirements (Eng. Review) · Candidate Profiles · Resumes(view) · Timesheets (RMG reports) · RMG Dashboard · Reports"]
    TA --> T["Opportunities → Requirements · Resumes/ATS · Candidates · Profiles · Trigger AI Interview · TA Dashboard · Reports"]
    HR --> H["Employees · Project Employees · Leave Applications · Holidays · Timesheets (approve) · Offers · Reports"]
    Finance --> F["Purchase Orders · Invoices · TDS · Project Employees · Projects · Timesheets (approve) · Finance Dashboard · Reports"]
    Candidate --> C["Static interview UI only (no CRM/admin)"]

    note["* Customers create/edit = Sales, Sales_Head only.<br/>Admin passes every role_required check (allow_admin=True)."]
    classDef n fill:#fef9c3,stroke:#a16207,color:#000
    class note n
```

## 5.2 Candidate journey (AI interview)

```mermaid
journey
    title Candidate — take the AI interview
    section Access
      Open invite link (?invite=token): 4: Candidate
      Read welcome card: 4: Candidate
      Pass device test (mic/cam/net): 3: Candidate
      Verify email + access key: 3: Candidate
    section Interview
      Enter fullscreen (proctored): 3: Candidate
      Warmup - introduce yourself: 4: Candidate
      Hear question (TTS): 4: Candidate
      Answer by voice (VAD+Whisper): 3: Candidate
      Auto-advance / Send / Skip: 4: Candidate
    section Finish
      Submit / time expiry: 3: Candidate
      Redirect to thank-you page: 5: Candidate
```

## 5.3 HR (interview platform) journey

```mermaid
journey
    title HR — schedule, run, review interviews
    section Setup
      Login (HR): 4: HR
      Pick/save job template: 4: HR
      Extract skills from JD/CV: 3: HR
      ATS preview and ranking: 3: HR
    section Schedule
      Schedule interview: 4: HR
      Send invite email (SMTP): 3: HR
      Share access key: 4: HR
    section Review
      Open HR dashboard/records: 4: HR
      Unlock and read report: 4: HR
      Adjust per-question score exclusion: 3: HR
      Set decision (shortlist/reject/hold): 5: HR
```

## 5.4 Sales & Sales_Head journey (CRM front office)

```mermaid
journey
    title Sales / Sales_Head — pipeline to delivery
    section Sales
      Create customer + branches/contacts (5-step wizard): 4: Sales
      Edit branch (4-step: Info → Holiday Billing → Leave & Holiday Billing + Billable Leave → Billing Props): 4: Sales
      Create opportunity (11-step wizard, Prev+Next bottom-right): 4: Sales
      Edit opportunity from list row (same full wizard; GET+hydrate → versioned PUT): 3: Sales
      Add type-aware Candidate CTC slabs with auto revenue and budget: 4: Sales
      Advance pipeline stage: 3: Sales
      Create requirement (Draft): 4: Sales
      Submit requirement for approval: 3: Sales
    section Sales_Head
      Approve/reject requirement: 4: Sales_Head
      Close/cancel requirement: 3: Sales_Head
      Create project on win (Details → Leave & Holiday Billing Policy ← branch prefill → Billing Properties): 4: Sales_Head
      View executive dashboard: 5: Sales_Head
```

## 5.5 RMG journey (engineering review & staffing)

```mermaid
journey
    title RMG — engineering review & candidate quality
    section Requirements
      Review approved requirements: 4: RMG
      Add RMG JD text and/or file: 3: RMG
      Engineering approve (JD required): 4: RMG
    section Candidates
      Review candidate profiles: 4: RMG
      Record skill evaluations: 3: RMG
      Trigger AI L1 interview: 4: RMG
      View RMG dashboard: 5: RMG
    section Timesheets
      Review Timesheet Due report: 5: RMG
      Edit daily grid (Week Off hours editable; Apply leave + Comp-Off): 4: RMG
      Excess leave / Absent / Half_Day → LOP reporting; Summary + Invoice LOP column: 4: RMG
      Weekend work: Comp Off Billable → bill or credit: 4: RMG
      Submit/resubmit drafts: 4: RMG
      Approve/reject submitted sheets: 4: RMG
      Generate invoice from approved sheet: 3: RMG
```

## 5.6 TA journey (sourcing & AI interviews)

```mermaid
journey
    title TA — sourcing pipeline
    section Sourcing
      Add job postings to requirement: 4: TA
      Upload resumes: 4: TA
      Run ATS scan against RMG JD: 3: TA
      Shortlist / reject resumes: 4: TA
    section AI Interview
      Schedule AI interview from resume: 4: TA
      Receive completion notification: 5: TA
      Open candidate report deep-link: 4: TA
```

## 5.7 HR (CRM) & Finance journeys (back office)

```mermaid
journey
    title HR (CRM) & Finance — delivery & billing
    section HR (CRM)
      Create/manage employees: 4: HR
      Map employees to projects (PE bridge): 4: HR
      Seed PE leave from Customer Leave Policy: 4: HR
      Maintain holidays calendar (customer holidays require branch): 4: HR
      Edit branch holiday-year dates (Sales/HR) — same rows as Holidays tab: 4: Sales
      Freeze branch holiday year: 3: Sales
      Approve PE-scoped leave applications: 4: HR
      Approve/reject timesheets: 4: HR
      RMG timesheet reports (due · submission · approvals): 4: RMG
    section Finance
      Create purchase orders (Open/Regular, start/end dates, CGST/IGST slab): 4: Finance
      Edit purchase orders (list pencil / detail Edit → PUT): 4: Finance
      Quick-add customer contact from PO Header (Finance write on POST/PUT contacts): 4: Finance
      Allocate PO to project during create (customer+branch-filtered): 3: Finance
      Allocate PO to projects (detail): 3: Finance
      Dashboard warning when PO end_date within 45 days / expired: 4: Finance
      Generate invoices using PE effective rates: 4: Finance
      Block invoice/submit when PO expired: 4: Finance
      Invoice detail GST + editable buyer State Code override: 5: Finance
      New Tax Invoice generator (form/Excel/PDF): 5: Finance
      Download Tax Invoice PDF from invoice detail: 5: Finance
      View Tax Invoice (A4 print): 4: Finance
      Record payments + TDS: 4: Finance
      View finance dashboard: 5: Finance
```

## 5.8 Admin / CEO journey (governance)

```mermaid
journey
    title Admin/CEO — who can access the application
    section Users
      Create account (email + password + CRM roles): 5: Admin/CEO
      Assign/replace CRM roles + tab access: 5: Admin/CEO
      Activate/deactivate or delete login access: 5: Admin/CEO
      Full CRM deletes (customers, projects, invoices, …): 5: Admin/CEO
    section Self-service (any user)
      Login with email or username: 5: User
      Update own profile + password: 5: User
    section Configuration
      Manage master data + access templates: 4: Admin/CEO
      Access all CRM views + AI usage logs: 5: Admin/CEO
```

## 5.9 CRM lifecycle swimlane (cross-role, end to end)

```mermaid
flowchart TB
    subgraph SalesLane["Sales / Sales_Head"]
        c1["Create Customer<br/>(5-step wizard)"] --> o1["Create Opportunity<br/>(wizard: one section per step)"] --> r1["Create + submit Requirement"]
        r1 --> a1["Sales_Head approves"]
    end
    subgraph RMGLane["RMG"]
        a1 --> e1["Engineering review: RMG JD required → Open_For_Sourcing"]
    end
    subgraph TALane["TA"]
        e1 --> p1["Post jobs + upload resumes"] --> s1["ATS scan vs RMG JD + skills"] --> ai1["Schedule AI L1 interview"]
    end
    subgraph Bridge["AI Interview bridge"]
        ai1 --> int1["Candidate takes interview"] --> sync1["Result synced to profile<br/>(Passed/Failed, skill evals)"]
    end
    subgraph SalesLane2["Sales / Sales_Head / HR"]
        sync1 --> off1["Advance profile → Offer"] --> join1["Joined"]
    end
    subgraph DeliveryLane["Sales_Head / HR"]
        join1 --> prj1["Create Project + staff Employees"] --> ts1["Timesheets"]
    end
    subgraph FinanceLane["Finance / HR"]
        ts1 --> ap1["Approve timesheets"] --> inv1["PO → Invoice → Payment/TDS"]
    end
```

## 5.10 Customer → Branch → Project → Team drill-down

Sales / Sales_Head (and any role with Customers/Projects access) navigate delivery
context via row clicks and a shared `CrmBreadcrumb` trail. Existing detail routes
are reused — no new pages.

```mermaid
flowchart LR
    list["Customers list"] --> cust["customers/:id<br/>CustomerDetailPage"]
    cust -->|"Branches tab · row click"| branch["branch-policy/:id<br/>BranchPolicyPage<br/>Projects section"]
    branch -->|"project row → crmNavigate"| proj["projects/:id<br/>ProjectDetailPage<br/>Overview · Team · Timesheet · PO & Invoices"]
    proj -->|"Team tab · row click<br/>pe_id"| pe["project-employees/:id<br/>ProjectEmployeeDetailPage"]
    crumbs["CrmBreadcrumb<br/>Customers / Customer / Branch / Project / Employee"]
    cust -.-> crumbs
    branch -.-> crumbs
    proj -.-> crumbs
    pe -.-> crumbs
```

Breadcrumb crumbs (last = current, not a link):

| Page | Trail |
|------|-------|
| Customer detail | `Customers / {customer}` |
| Branch policy | `Customers / {customer} / {branch}` |
| Project detail | `Customers / {customer} / {branch} / {project}` |
| Project employee | `Customers / {customer} / {branch} / {project} / {employee}` |

Row-click actions (Edit/Delete) use `stopPropagation` so they do not navigate.
List Delete opens **ConfirmModal** (“Do you want to delete this …?”); on confirm the
row is removed from list state immediately (`afterListDelete`) then soft-refreshed —
no need to open the entity and navigate back. After a dependency **409**, ConfirmModal
disables Delete (Close to dismiss); Employees offer **Deactivate** (`PUT is_active=false`)
when hard-delete is blocked.
Serializer additions for crumbs: project detail `customer_id`/`customer_name`/`branch_id`/`branch_name`
(via `project.branch_id` then **same-customer** opportunity branch; foreign opp branches suppressed
with `branch_unlinked` / “Branch not linked to this customer”); PE detail top-level
`branch_id`/`branch_name`; branch policy `customer_name` + `linked_projects`; team rows `pe_id`.
`projects.branch_id` (0045) is the explicit Branch→Project link; legacy rows fall back to
same-customer `opportunity.branch_id` only. `ensure_project_branch_id` never overwrites a
non-null `project.branch_id` (PUT opportunity change also keeps an existing branch when
the new opp branch is foreign). Timesheet detail header uses `pe_effective_branch` /
`_project_branch` — never raw `opp.branch_id` — so a stray cross-customer opportunity branch
(e.g. Harman under BMW) cannot appear in the timesheet Branch field. Disagreement between
`project.branch_id` and `opportunity.branch_id` is logged.

### PE Holidays + leave sync
- Holidays tab / timesheet holiday dates resolve branch via
  `pe_effective_branch` / `effective_customer_branch_for_project`
  (`project.branch_id` → same-customer opportunity branch → calendar/leave fallbacks).
  Wrong cross-customer opportunity branches are ignored.
- `POST /api/projects/employees/{pe_id}/leave/sync` (Admin/HR) back-fills missing leave types from
  customer/branch policy without mutating existing balances (idempotent).
- Leave accrual start: `accrual_start = max(CustomerLeavePolicy.effective_date, PE.onboarding_date)`
  (missing side = no bound). Monthly credit job credits 0 for periods ending before that date;
  One_Time / Yearly-start with a future start opens at 0 and is granted by the credit job on/after
  the start month. Editing `effective_date` later does not rewrite already-seeded balances.
