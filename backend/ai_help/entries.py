"""Per-tab Ask AI help KB — seed for every CRM tab.

Edit this module to improve answers. Keys match CRM route `p=` paths
(dashboard = empty path). Branch uses key `branch` (UI route branch-policy/:id).
"""
from __future__ import annotations

from ai_help.schema import HelpEntry

# Shared deep-rule IDs used by delivery tabs
_LEAVE_RULES = [
    "leave_seeding",
    "effective_date",
    "leave_billing_policy",
    "comp_off",
    "loss_of_pay",
]
_TS_RULES = [
    "billable_days",
    "comp_off",
    "loss_of_pay",
    "holiday_scoping",
    "leave_billing_policy",
    "effective_date",
]

ENTRIES: list[HelpEntry] = [
    {
        "key": "dashboard",
        "title": "Dashboard",
        "purpose": (
            "CRM home overview: pipeline and delivery snapshots, shortcuts into "
            "customers, opportunities, timesheets, and finance depending on role."
        ),
        "key_fields": [
            "Summary cards and charts",
            "Role-filtered widgets",
            "Quick links into CRM tabs",
        ],
        "common_tasks": [
            "Scan open opportunities and delivery risk from the widgets shown for your role.",
            "Use sidebar or Ask AI navigation to jump to a specific tab.",
            "Open Reports for deeper exports if the dashboard summary is not enough.",
        ],
        "gotchas": [
            "What you see depends on your CRM roles and Access Template tab visibility.",
            "Dashboard does not edit records — open the owning tab to change data.",
        ],
        "related_routes": ["opportunities", "projects", "timesheets", "reports"],
        "suggested_prompts": [
            "What can I do from the Dashboard?",
            "Where do I manage timesheets?",
            "How do I open Projects?",
            "Who sees which widgets?",
        ],
    },
    {
        "key": "customers",
        "title": "Customers",
        "purpose": (
            "Master list of client organizations. Own contacts, branches, billing "
            "policy, leave policies, and holiday calendars that downstream projects inherit."
        ),
        "key_fields": [
            "Customer name / status",
            "Branches",
            "Billing policy flags (leave/holiday/comp-off billable)",
            "Leave policies per leave type",
            "Contacts",
        ],
        "common_tasks": [
            "Create or open a customer, then manage Branches from the customer detail.",
            "Set customer-level billing and leave policies; branch overrides when set.",
            "Add contacts used on opportunities and outreach.",
        ],
        "gotchas": [
            "Deleting a customer can cascade policies/holidays — prefer deactivate when unsure.",
            "Branch policy blank fields inherit from the customer; set a field only to override.",
        ],
        "related_routes": ["branch", "opportunities", "projects", "holidays"],
        "suggested_prompts": [
            "How do I add a branch to a customer?",
            "What is Leave Billing Policy?",
            "Where are holidays defined?",
            "How do billing flags inherit to projects?",
        ],
        "rule_ids": ["leave_billing_policy", "holiday_scoping", "comp_off"],
    },
    {
        "key": "branch",
        "title": "Branch",
        "purpose": (
            "Per-customer branch (location) policy: working hours, billable flags, "
            "and branch-scoped holidays/leave policies that projects can inherit."
        ),
        "key_fields": [
            "Branch name",
            "holidays_billable / weekoff_billable / leave_billable / comp_off_billable",
            "Hours thresholds (full/half day)",
            "Branch leave policies",
        ],
        "common_tasks": [
            "Open from Customers → Branches (route branch-policy/:id).",
            "Override only the policy fields that differ from the customer default.",
            "Confirm holiday calendar year is scoped to this branch.",
        ],
        "gotchas": [
            "There is no top-level Branch sidebar tab — always enter via a Customer.",
            "Null/blank override fields mean inherit — they are not False.",
        ],
        "related_routes": ["customers", "holidays", "projects", "timesheets"],
        "suggested_prompts": [
            "How does branch policy override the customer?",
            "What does Comp Off Billable mean?",
            "How are holidays scoped to a branch?",
            "Go to Customers",
        ],
        "rule_ids": ["holiday_scoping", "comp_off", "leave_billing_policy", "billable_days"],
    },
    {
        "key": "opportunities",
        "title": "Opportunities",
        "purpose": (
            "Sales pipeline deals linked to a customer (and often a branch). Drive "
            "requirements, candidates, and eventually projects."
        ),
        "key_fields": [
            "Stage / status",
            "Customer and branch",
            "Value and owner",
            "Linked requirements",
        ],
        "common_tasks": [
            "Create an opportunity under a customer; set stage as the deal progresses.",
            "Attach requirements and move candidates through the hiring funnel.",
            "Convert or link to a Project when delivery starts.",
        ],
        "gotchas": [
            "Branch on the opportunity influences which holiday/billing branch projects inherit.",
            "Stage transitions may be validated — invalid jumps are rejected.",
        ],
        "related_routes": ["customers", "candidates", "projects", "profiles"],
        "suggested_prompts": [
            "How do I create an opportunity?",
            "How does branch on an opportunity affect billing?",
            "Where do requirements live?",
            "Go to Projects",
        ],
    },
    {
        "key": "candidates",
        "title": "Candidates",
        "purpose": (
            "Talent pool records for recruiting. Link to opportunities/requirements "
            "and advance toward profiles and AI interviews."
        ),
        "key_fields": [
            "Name, email, phone",
            "Status / stage",
            "Resume",
            "Linked opportunity / requirement",
        ],
        "common_tasks": [
            "Add or import a candidate and attach a resume.",
            "Associate with a requirement and update recruiting status.",
            "Create or open a Candidate Profile for packaging to the client.",
        ],
        "gotchas": [
            "Deleting a candidate can cascade linked profiles — confirm before delete.",
            "AI interview invites are managed from Candidate Profiles, not this list alone.",
        ],
        "related_routes": ["profiles", "template-requests", "opportunities"],
        "suggested_prompts": [
            "How do I add a candidate?",
            "Where do AI interviews start?",
            "What are Template Requests for?",
            "Go to Candidate Profiles",
        ],
    },
    {
        "key": "template-requests",
        "title": "Template Requests",
        "purpose": (
            "Requests for interview question templates (skills/difficulty) that TA/RMG "
            "fulfill before AI interviews can run."
        ),
        "key_fields": [
            "Requested skills / role",
            "Status (open / fulfilled)",
            "Linked template",
            "Requester",
        ],
        "common_tasks": [
            "Raise a template request when the needed skill pack is missing.",
            "Fulfill by linking an existing interview template.",
            "Track open requests so AI interviews are not blocked.",
        ],
        "gotchas": [
            "An interview may be blocked until the matching template request is fulfilled.",
            "Fulfill links the template — it does not invent questions here.",
        ],
        "related_routes": ["candidates", "profiles"],
        "suggested_prompts": [
            "How do I fulfill a template request?",
            "Who can create template requests?",
            "What happens if a template is missing?",
            "Go to Candidates",
        ],
    },
    {
        "key": "profiles",
        "title": "Candidate Profiles",
        "purpose": (
            "Client-ready candidate packages and AI interview linkage. Bridge between "
            "recruiting and interview scheduling."
        ),
        "key_fields": [
            "Profile status",
            "Skills / experience summary",
            "AI interview links",
            "Opportunity linkage",
        ],
        "common_tasks": [
            "Build or update a profile for client sharing.",
            "Launch or review AI interview invites from the profile.",
            "Track pass threshold vs Settings AI interview pass threshold.",
        ],
        "gotchas": [
            "Pass threshold is a global setting (Admin), not per-profile.",
            "Interview Schedule (top bar) opens HR Setup for calendar invites.",
        ],
        "related_routes": ["candidates", "template-requests", "opportunities"],
        "suggested_prompts": [
            "How do I start an AI interview from a profile?",
            "What is the pass threshold?",
            "How do Template Requests relate?",
            "Go to Candidates",
        ],
    },
    {
        "key": "projects",
        "title": "Projects",
        "purpose": (
            "Delivery engagements under a customer/opportunity. Own billing overrides, "
            "branch, leave billing policy, rates, and project employees."
        ),
        "key_fields": [
            "Customer / opportunity / branch",
            "Billing frequency and overrides",
            "Leave Billing Policy per type",
            "Comp Off Billable and hour thresholds",
            "Status",
        ],
        "common_tasks": [
            "Create a project from an opportunity; confirm branch for holiday/billing scope.",
            "Set project-level overrides only where they differ from branch/customer.",
            "Map employees under Project Employees to seed leave and enable timesheets.",
        ],
        "gotchas": [
            "Unset project policy fields seed/inherit from branch then customer.",
            "Deleting a project can cascade PE/timesheets/unpaid invoices — use care.",
        ],
        "related_routes": ["project-employees", "timesheets", "customers", "pos"],
        "suggested_prompts": [
            "How do I set Leave Billing Policy on a project?",
            "What does Effective Date mean on leave policy?",
            "How does Comp Off Billable work?",
            "Go to Project Employees",
        ],
        "rule_ids": _LEAVE_RULES + ["billable_days", "holiday_scoping", "rate_effective_date"],
    },
    {
        "key": "project-employees",
        "title": "Project Employees",
        "purpose": (
            "Maps an employee onto a project: onboarding date, rates, leave balances "
            "(seeded from customer/branch leave policy), and timesheet rollups."
        ),
        "key_fields": [
            "Employee / project",
            "Onboarding date",
            "Leave details & balances by type",
            "Effective-dated commercial rates",
            "PO linkage / utilization",
        ],
        "common_tasks": [
            "Map an employee to a project to seed leave rows from policy.",
            "Review leave balances, credit history, and eligibility on the PE detail.",
            "Maintain rate rows with effective dates for correct invoicing.",
        ],
        "gotchas": [
            "Leave seed is a snapshot — changing customer policy later does not rewrite existing PE leave rows.",
            "Accrual start uses max(policy.effective_date, onboarding_date).",
            "Loss of Pay is excluded from paid leave pool totals.",
        ],
        "related_routes": ["projects", "my-leave", "leave-applications", "timesheets"],
        "suggested_prompts": [
            "How is leave seeded when I map an employee?",
            "What is Effective Date on leave policy?",
            "How do commercial rate effective dates work?",
            "What is Loss of Pay?",
        ],
        "rule_ids": _LEAVE_RULES + ["rate_effective_date"],
    },
    {
        "key": "my-leave",
        "title": "My Leave",
        "purpose": (
            "Self-service leave balances and applications for the logged-in employee "
            "across their project mappings."
        ),
        "key_fields": [
            "Balances by leave type",
            "Pending / approved applications",
            "Project context when multiple mappings exist",
        ],
        "common_tasks": [
            "Check remaining casual/sick/earned/comp-off balances.",
            "Apply for leave against the correct project when you have multiple PEs.",
            "Track approval status of your applications.",
        ],
        "gotchas": [
            "Balances are per project mapping, not a single company-wide pool.",
            "Comp-Off may show differently from paid leave (can be negative).",
            "HR approves via Leave Applications — My Leave is the employee view.",
        ],
        "related_routes": ["leave-applications", "project-employees", "holidays"],
        "suggested_prompts": [
            "How do I apply for leave?",
            "Why are my balances split by project?",
            "What is Comp-Off credit?",
            "Go to Leave Applications",
        ],
        "rule_ids": ["comp_off", "loss_of_pay", "leave_seeding"],
    },
    {
        "key": "leave-applications",
        "title": "Leave Applications",
        "purpose": (
            "HR/Admin queue to review, approve, or reject leave requests. Approvals "
            "debit the matching Project Employee leave balance."
        ),
        "key_fields": [
            "Applicant / dates / leave type",
            "Project (required when active PE)",
            "Status",
            "Comp-off type when applicable",
        ],
        "common_tasks": [
            "Filter pending applications and approve or reject with reason.",
            "Confirm the project mapping so the correct PE balance is debited.",
            "Handle Comp-Off and LOP types carefully vs paid leave.",
        ],
        "gotchas": [
            "Approve debits PE leave_balance; wrong project = wrong balance.",
            "Rejection usually requires a reason.",
        ],
        "related_routes": ["my-leave", "project-employees", "timesheets"],
        "suggested_prompts": [
            "What happens when I approve leave?",
            "How is Comp-Off handled on applications?",
            "What is Loss of Pay?",
            "Go to Project Employees",
        ],
        "rule_ids": ["comp_off", "loss_of_pay", "leave_seeding"],
    },
    {
        "key": "holidays",
        "title": "Holidays",
        "purpose": (
            "Customer-branch holiday calendars by year. Drive timesheet day type "
            "Holiday and billable-day subtraction unless holidays_billable is on."
        ),
        "key_fields": [
            "Holiday name / date",
            "Branch",
            "Calendar year",
            "Optional billable via branch/project policy",
        ],
        "common_tasks": [
            "Add holidays for the correct branch and year.",
            "Reuse holiday names from the shared holiday-names list when available.",
            "Verify projects pointing at that branch pick up the calendar.",
        ],
        "gotchas": [
            "Holidays are branch-scoped — another branch's holiday does not apply.",
            "If holidays_billable is true, holiday days still count as billable.",
        ],
        "related_routes": ["customers", "branch", "timesheets", "projects"],
        "suggested_prompts": [
            "How are holidays scoped by branch?",
            "Do holidays reduce billable days?",
            "How do I add a holiday?",
            "Go to Timesheets",
        ],
        "rule_ids": ["holiday_scoping", "billable_days"],
    },
    {
        "key": "timesheets",
        "title": "Timesheets",
        "purpose": (
            "Monthly attendance and billing input per employee/project. Day types "
            "(Working, Week_Off, Holiday, Leave, Comp-Off) drive billable days, "
            "leave consumption, and Comp-Off credit on approve."
        ),
        "key_fields": [
            "Period / employee / project",
            "Day type and hours",
            "Leave type on leave days",
            "billing_policy (incl. comp_off_billable)",
            "leave_billable_by_type / leave_balances_by_type",
            "Status: Draft → Submitted → Approved",
        ],
        "common_tasks": [
            "Generate days for the month (auto Working / Week_Off / Holiday).",
            "Mark leave or Comp-Off on a day and pick the leave type; watch balances.",
            "Submit for approval; approvers grant Comp-Off credit when Comp Off Billable is OFF.",
            "Use Effective Date context from leave/rate policy when balances look wrong.",
        ],
        "gotchas": [
            "Client calendar holidays are locked on entries — do not treat them as editable working days.",
            "Submit never blocks on PO availability.",
            "Billable display often uses hours÷8 (or max_billable_hours_day).",
            "Loss of Pay does not consume paid leave balances.",
        ],
        "related_routes": ["project-employees", "holidays", "leave-applications", "projects"],
        "suggested_prompts": [
            "How do I mark leave / Comp-Off on a timesheet?",
            "What is Effective Date?",
            "How are billable days calculated?",
            "When does Comp-Off earn credit vs get billed?",
        ],
        "rule_ids": _TS_RULES,
    },
    {
        "key": "pos",
        "title": "Purchase Orders",
        "purpose": (
            "Customer POs that fund project delivery. Track value, allocations to "
            "projects, expiry, and linkage to invoices."
        ),
        "key_fields": [
            "PO number / value / currency",
            "Expiry",
            "Project allocations",
            "Status (active / expired / cancelled)",
            "Attachments",
        ],
        "common_tasks": [
            "Create a PO and allocate value to one or more projects.",
            "Monitor expiring and expired POs from finance views.",
            "Link invoices against remaining PO capacity.",
        ],
        "gotchas": [
            "Timesheet submit does not block on PO — finance still tracks utilization.",
            "Deleting a PO can cascade unpaid invoices/TDS — prefer cancel when unsure.",
        ],
        "related_routes": ["invoices", "projects", "tds"],
        "suggested_prompts": [
            "How do I allocate a PO to a project?",
            "What happens when a PO expires?",
            "How do invoices draw from a PO?",
            "Go to Invoices",
        ],
    },
    {
        "key": "invoices",
        "title": "Invoices",
        "purpose": (
            "Bill customers for delivered work. Amounts often derive from billable "
            "days × effective rates; may reference POs and spawn TDS records."
        ),
        "key_fields": [
            "Invoice number / status",
            "Project / PO linkage",
            "Line amounts / grand total",
            "PDF / payment proof",
        ],
        "common_tasks": [
            "Create an invoice from project/PO context.",
            "Generate PDF and record payments / payment proof.",
            "Track unpaid invoices and related TDS.",
        ],
        "gotchas": [
            "Rate effective dates on the PE determine which rate applies to work dates.",
            "Deleting invoices can cascade unpaid TDS — use care.",
        ],
        "related_routes": ["pos", "tds", "projects", "timesheets"],
        "suggested_prompts": [
            "How is invoice amount related to billable days?",
            "How do I record a payment?",
            "Where does TDS come from?",
            "Go to Purchase Orders",
        ],
        "rule_ids": ["rate_effective_date", "billable_days"],
    },
    {
        "key": "tds",
        "title": "TDS",
        "purpose": (
            "Tax Deducted at Source records tied to invoices — amounts withheld and "
            "payment tracking for finance compliance."
        ),
        "key_fields": [
            "Invoice linkage",
            "TDS amount",
            "Payment status / dates",
        ],
        "common_tasks": [
            "Record TDS against an invoice.",
            "Mark TDS payment when remitted.",
            "Reconcile open TDS with unpaid invoices.",
        ],
        "gotchas": [
            "TDS rows are finance-owned; deleting an invoice may cascade unpaid TDS.",
        ],
        "related_routes": ["invoices", "pos"],
        "suggested_prompts": [
            "How do I record TDS on an invoice?",
            "How do I mark TDS as paid?",
            "Go to Invoices",
            "Who can access TDS?",
        ],
    },
    {
        "key": "employees",
        "title": "Employees",
        "purpose": (
            "HR employee master (identity, employment, education/experience). "
            "Employees are mapped onto projects via Project Employees for leave and timesheets."
        ),
        "key_fields": [
            "Name / employee code",
            "Employment status",
            "Leave type master (incl. Comp-Off, Loss of Pay)",
            "Education / experience rows",
        ],
        "common_tasks": [
            "Create or update an employee record.",
            "Prefer deactivate over hard delete when someone leaves.",
            "Map the employee to projects from Project Employees for delivery.",
        ],
        "gotchas": [
            "Company employee master ≠ project leave balances — balances live on PE.",
            "Loss of Pay and Comp-Off are special leave type names recognized by billing.",
        ],
        "related_routes": ["project-employees", "my-leave", "timesheets"],
        "suggested_prompts": [
            "How do I add an employee?",
            "Difference between Employees and Project Employees?",
            "What is Loss of Pay?",
            "Go to Project Employees",
        ],
        "rule_ids": ["loss_of_pay", "comp_off"],
    },
    {
        "key": "reports",
        "title": "Reports",
        "purpose": (
            "Cross-module reporting and exports for sales, delivery, HR, and finance "
            "roles (timesheet due/approvals, utilization, etc.)."
        ),
        "key_fields": [
            "Report type filters",
            "Date / project / customer scope",
            "Export actions where available",
        ],
        "common_tasks": [
            "Pick a report matching your role (e.g. timesheet approvals for RMG/HR).",
            "Filter by period and export if needed.",
            "Drill into source tabs (Timesheets, Projects) to fix underlying data.",
        ],
        "gotchas": [
            "Reports are read-oriented; corrections happen in the owning module.",
            "Visibility follows CRM roles and Access Templates.",
        ],
        "related_routes": ["timesheets", "projects", "invoices", "dashboard"],
        "suggested_prompts": [
            "Which reports can I access?",
            "Where do timesheet approval reports live?",
            "Go to Timesheets",
            "Go to Dashboard",
        ],
    },
]
