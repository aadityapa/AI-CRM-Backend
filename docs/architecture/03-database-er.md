# 3. Database ER Diagrams

The database holds **two coexisting schemas** in one PostgreSQL instance:

- **CRM tables** — SQLAlchemy ORM (`models/*`), managed by Alembic.
- **Legacy interview tables** — raw SQL (`auth_db.py`, `prompt_logger.py`), never touched by Alembic.

They connect only through the shared users table `registration_data` and the
`ai_interview_links` bridge (join key `invite_token`). Because ER diagrams get large,
they are split by domain. `registration_data` (users) is the central hub.

## 3.1 Domain overview (table clusters)

```mermaid
graph TB
    subgraph rbac["RBAC / Users"]
        u["registration_data (users)"]
        roles["roles · user_roles"]
        notif["notifications"]
    end
    subgraph masters["Masters"]
        m["departments · designations · skills · locations<br/>currencies · document_types · contact_roles · leave_policy_types · app_settings"]
    end
    subgraph sales["Sales domain"]
        cust["customers · branches · contacts<br/>billing_policies · documents"]
        opp["opportunities · opportunity_skills · activity_log"]
    end
    subgraph recruit["Recruitment domain"]
        req["requirements · skills · job_postings"]
        res["resumes"]
        cand["candidates · education · experience · skills"]
        prof["candidate_profiles · skill_evaluations · offer_history"]
    end
    subgraph delivery["Delivery domain"]
        proj["projects · employees · comm_matrix"]
        emp["employees · leave_balances · project_history"]
        ts["timesheets · timesheet_entries"]
    end
    subgraph finance["Finance domain"]
        fin["purchase_orders · allocations · invoices<br/>payments · tds_records"]
    end
    subgraph legacy["Legacy interview (raw SQL)"]
        li["interview_schedule · interview_records<br/>interview_progress · job_templates<br/>hr_candidate_decisions · ai_prompt_logs"]
    end
    bridge["ai_interview_links"]

    sales --> recruit
    recruit --> bridge
    bridge -.->|invite_token| legacy
    u -.-> bridge
    recruit --> delivery
    delivery --> finance
    sales --> finance
    masters --> sales
    masters --> recruit
    masters --> delivery
    u --> rbac
    u -.->|logical| legacy
```

## 3.2 RBAC & Users

```mermaid
erDiagram
    registration_data ||--o{ user_roles : "has"
    roles ||--o{ user_roles : "assigned via"
    registration_data ||--o{ notifications : "receives"
    roles ||--o{ registration_data : "primary role_id"
    employees ||--o| registration_data : "employee_id (1:1)"

    registration_data {
        int id PK
        text username UK
        text email UK
        text role "legacy: hr|candidate"
        int role_id FK "added by migration 0001"
        boolean is_active
        int employee_id FK
    }
    roles {
        int id PK
        enum name UK "Admin|Sales|Sales_Head|RMG|TA|HR|Finance"
    }
    user_roles {
        int id PK
        int user_id FK
        int role_id FK
    }
    notifications {
        int id PK
        int user_id FK
        string title
        boolean is_read
    }
```

## 3.3 Sales domain (Customers → Opportunities)

```mermaid
erDiagram
    customers ||--o{ customer_branches : "has"
    customers ||--o| customer_billing_policies : "one policy"
    customers ||--o{ customer_documents : "has"
    customers ||--o{ contact_persons : "has"
    customer_branches ||--o{ branch_holiday_years : "holiday years"
    branch_holiday_years ||--o{ holidays : "calendar dates"
    holiday_names ||--o{ holidays : "name FK"
    customer_branches ||--o{ contact_persons : "at"
    document_types ||--o{ customer_documents : "typed"
    customers ||--o{ opportunities : "for"
    customer_branches ||--o{ opportunities : "branch"
    contact_persons ||--o{ opportunities : "contact/hiring mgr"
    opportunities ||--o{ opportunity_skills : "requires"
    skills ||--o{ opportunity_skills : "used in"
    opportunities ||--o{ opportunity_activity_log : "logs"
    opportunities ||--o{ opportunity_attachments : "files"
    registration_data ||--o{ opportunities : "created_by"

    customers {
        int id PK
        string name UK
        enum status "Active|Inactive"
    }
    customer_branches {
        int id PK
        int customer_id FK
        string gstin
        boolean is_primary
    }
    branch_holiday_years {
        int id PK
        int branch_id FK
        int calendar_year
        boolean is_freeze
    }
    holiday_names {
        int id PK
        string name UK
        boolean is_active
    }
    holidays {
        int id PK
        int holiday_name_id FK
        string name
        date holiday_date
        string holiday_type "National|Regional|Customer"
        string observance "Mandatory|Optional"
        int branch_id FK
        int holiday_calendar_id FK
        int year
        boolean is_active
    }
    contact_persons {
        int id PK
        int customer_id FK
        int branch_id FK
        string role "from contact_roles master"
        string contact_priority "Primary|Secondary"
        string notification "Email|SMS|Both|None"
        boolean is_hiring_manager
    }
    contact_roles {
        int id PK
        string name UK
        boolean is_active
    }
    opportunities {
        int id PK
        string opp_id UK "OPP-2026-001"
        int customer_id FK
        enum opp_type "T&M|Work_Package|Fixed_Price|Retainer"
        enum pipeline_stage "New|Active|On_Hold|Closed_Won|Closed_Lost|Closed_Partial|Rejected|Archived"
        int created_by FK
    }
    opportunity_attachments {
        int id PK
        int opportunity_id FK
        string kind "customer_jd|general"
        string file_url
    }
    opportunity_skills {
        int id PK
        int opportunity_id FK
        int skill_id FK
        boolean is_mandatory
    }
```

## 3.4 Recruitment domain (Requirements → Resumes → Candidates → Profiles)

```mermaid
erDiagram
    opportunities ||--o{ requirements : "spawns"
    customers ||--o{ requirements : "for"
    requirements ||--o{ requirement_skills : "needs"
    skills ||--o{ requirement_skills : "used in"
    requirements ||--o{ requirement_job_postings : "posted as"
    requirements ||--o{ requirement_activity_log : "logs"
    requirements ||--o{ requirement_attachments : "JD files"
    requirements ||--o{ resumes : "receives"
    candidates ||--o{ resumes : "submitted as"
    candidates ||--o{ candidate_education : "has"
    candidates ||--o{ candidate_experience : "has"
    candidates ||--o{ candidate_skills : "has"
    skills ||--o{ candidate_skills : "tagged"
    candidates ||--o{ candidate_profiles : "profiled in"
    opportunities ||--o{ candidate_profiles : "for"
    candidate_profiles ||--o{ skill_evaluations : "evaluated"
    skills ||--o{ skill_evaluations : "on"
    candidate_profiles ||--o{ offer_history : "offered"
    candidate_profiles ||--o{ candidate_profile_activity_log : "logs"

    requirements {
        int id PK
        string req_number UK "REQ-2026-001"
        int opportunity_id FK
        int customer_id FK
        text rmg_jd_text "RMG JD at eng-approve"
        enum status "Draft..Fulfilled|Closed|Cancelled"
        int created_by FK
        int sales_head_approved_by FK
        int engineering_reviewed_by FK
    }
    requirement_attachments {
        int id PK
        int requirement_id FK
        string kind "rmg_jd"
        string file_url
    }
    resumes {
        int id PK
        int requirement_id FK
        int candidate_id FK
        decimal ats_score
        enum ats_status "Pending_Scan|Scored|Shortlisted|Rejected"
        enum ai_interview_status "Not_Scheduled|Scheduled|Completed|Passed|Failed"
    }
    candidates {
        int id PK
        string email UK
        int designation_id FK
        int preferred_location_id FK
    }
    candidate_profiles {
        int id PK
        int candidate_id FK
        int opportunity_id FK
        enum pipeline_status "Sourcing..Joined|Rejected"
        boolean commercial_approved
    }
    skill_evaluations {
        int id PK
        int profile_id FK
        int skill_id FK
        int reviewer_rated "1..5"
    }
    offer_history {
        int id PK
        int profile_id FK
        decimal ctc
        enum status "Pending|Accepted|Expired|Rejected"
    }
```

## 3.5 Delivery domain (Projects → Employees → Timesheets)

```mermaid
erDiagram
    opportunities ||--o{ projects : "delivered as"
    customers ||--o{ projects : "for"
    customer_branches ||--o{ projects : "delivery branch"
    projects ||--o{ project_employees : "staffs"
    employees ||--o{ project_employees : "assigned"
    project_employees ||--o{ project_employee_leave_details : "leave"
    project_employees ||--o{ project_employee_rates : "rates"
    leave_policy_types ||--o{ project_employee_leave_details : "of type"
    customer_leave_policies ||--o{ project_employee_leave_details : "seeds (branch/customer)"
    project_leave_policies ||--o{ project_employee_leave_details : "seeds (project override)"
    projects ||--o{ project_leave_policies : "leave policy"
    leave_policy_types ||--o{ project_leave_policies : "of type"
    projects ||--o{ project_communication_matrix : "has"
    employees ||--o| registration_data : "user_id (1:1)"
    departments ||--o{ employees : "in"
    designations ||--o{ employees : "as"
    employees ||--o{ employees : "reports to (mgr/hr)"
    employees ||--o{ employee_leave_balances : "accrues"
    leave_policy_types ||--o{ employee_leave_balances : "of type"
    employees ||--o{ employee_project_history : "history"
    projects ||--o{ employee_project_history : "in"
    projects ||--o{ timesheets : "tracked by"
    employees ||--o{ timesheets : "files"
    project_employees ||--o{ timesheets : "optional pe_id"
    project_employees ||--o{ leave_applications : "optional pe_id"
    timesheets ||--o{ timesheet_entries : "contains"
    timesheets ||--o{ timesheet_attachments : "files"
    registration_data ||--o{ timesheets : "approved_by"
    registration_data ||--o{ timesheet_attachments : "uploaded_by"

    projects {
        int id PK
        int opportunity_id FK
        int customer_id FK
        int branch_id FK "nullable; explicit delivery branch"
        enum billing_frequency "Monthly|Bi_Weekly|Weekly|Quarterly|Yearly"
        bool recurring_billing
        bool holidays_billable
        bool weekoff_billable
        decimal hours_required_half_day
        decimal hours_required_full_day
        decimal hours_required_half_day_comp_off
        decimal hours_required_full_day_comp_off
        bool is_max_billable_hours_per_day
        decimal max_billable_hours_day
        bool is_max_billable_hours_per_month
        decimal max_billable_hours_month
        bool is_max_billable_days_per_month
        int max_billable_days_month
        bool is_initial_no_billing_period
        int initial_no_billing_qty "numeric count (UI Period)"
        string initial_no_billing_period "unit Hours|Days|… (UI QTY)"
        enum status "Active|Completed|On_Hold"
    }
    project_leave_policies {
        int id PK
        int project_id FK
        int leave_type_id FK
        string name
        string leave_credit_type "VARCHAR(64); Monthly|…|Credit Balance Every Month"
        string leave_credit_timing "Start_Of_Period|End_Of_Period"
        decimal leave_credit_balance
        decimal initial_credit_balance
        string leave_expire "Monthly|Quarterly|Annually|Carry Forward"
        string leave_expire_timing "Start_Of_Period|End_Of_Period; null if no expire"
        bool is_max_limit
        int maximum_carry_forward
        date effective_date "accrual lower bound with PE.onboarding_date"
        bool is_active
        datetime created_at
        datetime updated_at
    }
    project_employees {
        int id PK
        int project_id FK
        int employee_id FK
        decimal billing_rate
        enum billing_unit "Hourly|Daily|Monthly"
        decimal experience_years
        decimal project_experience_years
        bool is_exit
        date exit_date
        date billing_date
        string role_title
        bool settlement_pending
        bool is_active
    }
    project_employee_leave_details {
        int id PK
        int project_employee_id FK
        int leave_type_id FK
        int customer_leave_policy_id FK "nullable; exactly one of customer/project FK set"
        int project_leave_policy_id FK "nullable; set when seeded from project override"
        decimal initial_balance
        decimal opening_balance
        decimal leave_accrual
        decimal leave_consumed
        decimal leave_balance
    }
    project_employee_rates {
        int id PK
        int project_employee_id FK
        date effective_from
        decimal rate
        enum billing_unit "Hourly|Daily|Monthly"
        bool is_current_rate
    }
    employees {
        int id PK
        int user_id FK
        string email UK
        int reporting_manager_id FK
        int reporting_hr_id FK
        enum profile_type "Internal|External"
    }
    timesheets {
        int id PK
        int project_id FK
        int employee_id FK
        int project_employee_id FK
        int month
        int year
        enum status "Draft|Submitted|Approved|Rejected"
        timestamptz created_at
        timestamptz updated_at
    }
    timesheet_attachments {
        int id PK
        int timesheet_id FK
        string file_url
        string file_name
        string file_sha256
        int file_size
        string kind
        int uploaded_by FK
        timestamptz uploaded_at
    }
    timesheet_entries {
        int id PK
        int timesheet_id FK
        date entry_date
        enum day_type "Working|Week_Off|Holiday"
        enum attendance_status "Present|Absent|Half_Day|Leave|Holiday|Week_Off"
        string leave_type "nullable"
        enum leave_period "Full|Half_AM|Half_PM nullable"
        string leave_reason "nullable optional Apply-leave note ≤255"
        decimal hours_worked
        decimal billable_hours
        decimal billable_days
        enum entry_location "Remote|Onsite"
        bool view_flag
        int entry_project_id FK "nullable split-time"
    }
```

## 3.6 Finance domain (Purchase Orders → Invoices → Payments/TDS)

```mermaid
erDiagram
    customers ||--o{ purchase_orders : "issues"
    customer_branches ||--o{ purchase_orders : "billing/delivery"
    contact_persons ||--o{ purchase_orders : "contact"
    purchase_orders ||--o{ po_project_allocations : "allocated"
    projects ||--o{ po_project_allocations : "funded"
    purchase_orders ||--o{ invoices : "billed against"
    projects ||--o{ invoices : "for"
    invoices ||--o{ invoice_payments : "paid via"
    invoices ||--o| tds_records : "tds (1:1)"

    purchase_orders {
        int id PK
        string po_number UK
        int customer_id FK
        date start_date
        date end_date
        enum po_type "Standard|Blanket|Open PO|Regular PO"
        jsonb billing_address_snapshot
        jsonb delivery_address_snapshot
        decimal tax_slab
        decimal total_value
        decimal consumed_value
        enum status "Active|Exhausted|Cancelled"
    }
    po_project_allocations {
        int id PK
        int po_id FK
        int project_id FK
        decimal allocated_amount
    }
    invoices {
        int id PK
        string invoice_number UK
        int po_id FK
        int project_id FK
        string buyer_state_code "nullable 2-digit GST override"
        decimal grand_total
        enum payment_status "Unpaid|Partially_Paid|Paid"
    }
    invoice_payments {
        int id PK
        int invoice_id FK
        decimal amount
    }
    tds_records {
        int id PK
        int invoice_id FK
        decimal tds_amount
        enum tds_status "Pending|Partially_Paid|Paid"
    }
```

## 3.7 AI-Interview bridge & legacy tables

The CRM half is fully relational; the legacy interview half is a loosely-coupled
document/JSON store. They connect **only** through `ai_interview_links` (via
`invite_token`) and the shared `registration_data` users table. Dashed = soft/logical
join (no DB-level FK).

```mermaid
erDiagram
    candidates ||--o{ ai_interview_links : "for"
    opportunities ||--o{ ai_interview_links : "for"
    candidate_profiles ||--o{ ai_interview_links : "L1 of"
    requirements ||--o{ ai_interview_links : "against"
    resumes ||--o{ ai_interview_links : "from"
    registration_data ||--o{ ai_interview_links : "scheduled_by"

    ai_interview_links {
        int id PK
        string invite_token UK "join key"
        string schedule_id "soft ref interview_schedule.id"
        string interview_record_id "soft ref interview_records.id"
        int candidate_id FK
        int opportunity_id FK
        int profile_id FK
        int requirement_id FK
        int resume_id FK
        int scheduled_by FK
        string level "L1"
        decimal overall_score_percent
        string result "Pending|Passed|Failed"
    }
    interview_schedule {
        text id PK
        text invite_token UK
        text access_key
        text session_status "pending|verified|active|completed|terminated"
        text notes "packed __KARNEX_CFG__"
    }
    interview_records {
        text id PK
        text candidate_email
        boolean submitted
        json payload
    }
    interview_progress {
        text interview_id PK
        text invite_token UK
        int current_index
        json questions
        json answers
    }
    job_templates {
        text job_id PK
        text job_title
        json required_skills
        text interview_mode
    }
    hr_candidate_decisions {
        text candidate_id PK
        text decision "shortlist|reject|on_hold"
    }
    ai_prompt_logs {
        text id PK
        text call_type
        text interview_id "soft ref"
        int total_tokens
    }

    ai_interview_links }o..|| interview_schedule : "invite_token (soft)"
    ai_interview_links }o..o| interview_records : "interview_record_id (soft)"
    interview_schedule ||..o{ interview_progress : "invite_token (soft)"
    interview_records ||..o{ ai_prompt_logs : "interview_id (soft)"
```

## 3.8 Full FK reference

The complete list of foreign-key edges (source → target) is maintained inline in each
domain diagram above. Central hub tables by inbound-FK count: **`registration_data`**
(users, ~15 inbound), then `customers`, `opportunities`, `candidates`, `projects`,
`skills`, `requirements`, `candidate_profiles`, `employees`.
