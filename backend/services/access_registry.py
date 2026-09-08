"""Registry of grantable CRM tabs + fields, and validation for Access Templates.

This is the catalogue the template editor renders (real checkboxes) and the server
validates against, so a template can never grant an unknown tab/field/mode.

MODES form a LADDER: "view" < "edit" < "create". A higher grant satisfies every
lower requirement — someone who may create records on a tab can obviously edit
and view them, so the editor asks one question per tab, not three. Fields carry
only "view" | "edit": creation is a record-level act, there is no such thing as
creating a single field.

Tab keys mirror the CRM router nav; field keys mirror each page's primary form
fields. Extend freely — add tabs/fields here and they light up in the editor
automatically.
"""
from __future__ import annotations

MODES = ("view", "edit", "create")
#: Field grants stop at edit — see module docstring.
#: "hidden" (25 Aug 2026) exists for the `tab:*` SUB-TAB entries: a template can
#: remove a detail-page tab (e.g. Requirements → Interview Slots) from a role's
#: view entirely. For ordinary form fields "view"/"edit" remain the ladder.
FIELD_MODES = ("hidden", "view", "edit")

_MODE_RANK = {"view": 1, "edit": 2, "create": 3}


def mode_satisfies(granted: str | None, required: str) -> bool:
    """True when a granted mode covers the required one (ladder semantics)."""
    if granted is None:
        return False
    return _MODE_RANK.get(granted, 0) >= _MODE_RANK.get(required, 99)


# ------------------------------------------------------------------ tabs
# key -> human label. Keys match the CRM query-param routes (?view=crm&p=<key>).
TABS: dict[str, str] = {
    "dashboard": "Dashboard",
    "customers": "Customers",
    "rate-cards": "Rate Card",
    "opportunities": "Opportunities",
    "candidates": "Candidates",
    "template-requests": "Template Requests",
    "profiles": "Candidate Profiles",
    # Like "dashboard", calendar has no form so no FIELDS_BY_TAB entry. The
    # frontend has always offered this key (MANAGEABLE_TABS) — before it existed
    # here, granting Calendar silently saved nothing (_strip_removed_keys).
    "calendar": "Interview Calendar",
    # Like dashboard/calendar: no form, so no FIELDS_BY_TAB entry.
    "emails": "Emails",
    "activity-log": "Activity Log",
    "projects": "Projects",
    "project-employees": "Project Employees",
    "branch-policy": "Branch Policy",
    "my-leave": "My Leave",
    "leave-applications": "Leave Applications",
    "holidays": "Holidays",
    "timesheets": "Timesheets",
    "pos": "Purchase Orders",
    "invoices": "Invoices",
    "tds": "TDS",
    "employees": "Employees",
    "reports": "Reports",
    "users": "Users",
    "settings": "Settings",
    # Completing the catalogue (25 Aug 2026): these three were routed pages the
    # frontend gate-checks (`crmTabVisibleFromMe`) but could never be granted
    # from the editor — the same silent-drop trap calendar used to be.
    "requirements": "Requirements (Sourcing)",
    "finance-reports": "Finance Reports",
    "payroll": "Payroll",
}

# ------------------------------------------------------------------ fields per tab
# tab_key -> { field_key: label }. The primary form fields of each page — what a
# template can independently show or lock. Grouped keys (e.g. "address") stand
# for the whole section where locking individual sub-inputs would be noise.
FIELDS_BY_TAB: dict[str, dict[str, str]] = {
    "rate-cards": {
        "band": "Experience Band", "rate_hourly": "Hourly Rate",
        "rate_daily": "Daily Rate", "rate_weekly": "Weekly Rate",
        "rate_monthly": "Monthly Rate", "rate_yearly": "Yearly Rate",
    },
    "customers": {
        "name": "Customer Name", "legal_entity_name": "Legal Entity Name",
        "customer_type": "Customer Type", "status": "Status",
        "address": "Address", "branches": "Branches", "billing_policy": "Billing Policy",
        "leave_policy": "Leave Billing Policy", "comp_off": "Comp Off",
        "attendance_rule": "Attendance Rule",
        "contacts": "Contacts", "documents": "Documents",
    },
    "opportunities": {
        "title": "Opportunity Title", "customer_id": "Customer", "branch_id": "Branch",
        "opp_type": "Type", "pipeline_stage": "Pipeline Stage",
        "sales_stage": "Sales Stage", "onboarding_status": "Onboarding Status",
        "rfi_value": "RFI Value", "rfi_received_date": "RFI Received Date",
        "customer_type": "Customer Type", "contact_email": "Contact Email",
        "hiring_manager_email": "Hiring Manager Email",
        "ctc_slab": "CTC Slab", "skills": "Skills", "attachments": "Attachments",
        "details": "Type-specific Details",
    },
    "candidates": {
        "name": "Name", "email": "Email", "phone": "Phone",
        "experience_years": "Experience", "notice_period": "Notice Period",
        "current_ctc": "Current CTC", "expected_ctc": "Expected CTC",
        "cv": "Resume", "education": "Education", "experience_history": "Work Experience",
        "skills": "Skills", "outreach": "Outreach Log",
        "resignation": "Resignation Details",
    },
    "profiles": {
        "candidate": "Candidate", "opportunity": "Opportunity",
        "pipeline_status": "Pipeline Status", "status_transition": "Stage Transitions",
        "current_ctc": "Current CTC", "expected_ctc": "Expected CTC",
        "approved_ctc": "Approved CTC", "hike_percent": "Hike %",
        "skill_evaluation": "Skill Evaluation", "offers": "Offers",
        "interview_rounds": "Interview Rounds", "ai_interviews": "AI Interviews",
        "submit_to_customer": "Submit to Customer",
        # The Workflow block on the profile Overview (offer letter ref,
        # onboarding dates, official email, department/designation, locations,
        # experience, relocation, CV/resignation docs). One key for the whole
        # block (3 Sep 2026, user request): Admin/CEO decide per template
        # which logins see it, and "edit" is what HR needs at Pre Onboarding.
        "workflow": "Workflow (onboarding details)",
    },
    "template-requests": {
        "title": "Title", "requirement_id": "Requirement", "status": "Status",
        "template_body": "Body", "fulfill": "Fulfill (RMG)", "prepare": "Prepare (TA)",
    },
    "projects": {
        "name": "Project Name", "customer_id": "Customer", "branch_id": "Branch",
        "status": "Status",
        "billing_frequency": "Billing Frequency", "billing_cycle_start_day": "Cycle Start",
        "billing_cycle_end_day": "Cycle End", "recurring_billing": "Recurring Billing",
        "billability": "Billability Flags", "thresholds": "Hour Thresholds",
        "caps": "Billing Caps", "leave_policies": "Leave Billing Policy",
        "team": "Team", "communication_matrix": "Communication Matrix",
        "create_po": "Create PO",
    },
    "project-employees": {
        "employee_id": "Employee", "project_id": "Project", "onboarding_date": "Onboarding Date",
        "billing_rate": "Billing Rate", "billing_unit": "Billing Unit",
        "work_mode": "Work Mode", "location": "Location",
        "leave_details": "Leave", "leave_sync": "Leave Sync",
        "rates": "Commercial Rate", "is_exit": "Exit / Settlement",
    },
    "branch-policy": {
        "identity": "Branch Identity", "holiday_years": "Holiday Years",
        "billability": "Billability Flags", "thresholds": "Hour Thresholds",
        "caps": "Billing Caps", "leave_policies": "Billable Leave Policy",
    },
    "my-leave": {
        "balance": "Leave Balance", "projects": "Project Leave",
    },
    "leave-applications": {
        "employee_id": "Employee", "project_id": "Project", "leave_type": "Leave Type",
        "period_type": "Period Type", "dates": "Dates", "reason": "Reason",
        "status": "Status", "approve": "Approve/Reject",
    },
    "holidays": {
        "customer_id": "Customer", "branch_id": "Branch", "year": "Year",
        "holiday_date": "Date", "name": "Holiday Name",
        "holiday_type": "Type", "observance": "Observance",
    },
    "timesheets": {
        "period": "Period", "status": "Status", "entries": "Daily Entries",
        "hours_worked": "Hours Worked", "attendance": "Attendance",
        "leave_applied": "Leave Applied", "billable_days": "Billable Days",
        "attachments": "Attachments", "approve": "Approve/Reject",
        "generate_invoice": "Generate Invoice",
    },
    "pos": {
        "po_number": "PO Number", "customer_id": "Customer", "branches": "Branches",
        "contact_person_id": "Contact", "po_type": "PO Type",
        "dates": "Dates", "payment_terms": "Payment Terms",
        "tax_slab": "Tax Slab", "total_value": "Total Value",
        "allocations": "Project Allocations", "attachments": "Attachments",
        "status": "Status", "renew": "Renew", "cancel": "Cancel",
    },
    "invoices": {
        "invoice_number": "Invoice Number", "po_id": "PO", "project_id": "Project",
        "invoice_date": "Invoice Date", "due_date": "Due Date",
        "lines": "Line Items", "sub_total": "Sub Total",
        "tax_amount": "Tax", "grand_total": "Grand Total",
        "payment_status": "Payment Status", "record_payment": "Record Payment",
        "record_tds": "Record TDS", "pdf": "Invoice PDF",
    },
    "tds": {
        "tds_amount": "TDS Amount", "payment_status": "Payment Status", "payments": "Payments",
    },
    "employees": {
        "name": "Name", "email": "Email", "phone": "Phone",
        "employee_code": "Employee Code", "profile_type": "Profile Type",
        "department_id": "Department", "designation_id": "Designation",
        "reporting": "Reporting Manager / HR", "employment_type": "Employment Type",
        "current_ctc": "CTC", "bank_details": "Bank Details",
        "addresses": "Addresses", "education": "Education",
        "experience": "Experience", "leave_balances": "Leave Balances",
        "attendance_rule": "Attendance Rule", "separation": "Separation",
        "cv": "CV",
    },
    "reports": {
        "opportunities": "Opportunity Report", "profiles": "Profile Report",
        "productivity": "Recruiter Productivity", "export": "CSV Export",
    },
    "users": {
        "roles": "Roles", "tab_access": "Tab Access", "access_template": "Access Template",
        "activation": "Activation", "portal_access": "Portal Access",
        "email_flows": "Email Flows", "action_permissions": "Action Permissions",
    },
    "settings": {
        "masters": "Masters (Departments, Skills…)", "organisation": "Organisation",
        "operations": "Operations / Scheduler", "app_settings": "App Settings",
    },
    # Requirements (sourcing) — the fields a template can scope on the detail
    # page. calendar / finance-reports / payroll are view-only pages with no
    # form, so like dashboard they carry no field catalogue.
    # `tab:*` keys are the detail page's SUB-TABS (25 Aug 2026): set one to
    # "hidden" and that tab disappears for the templated user.
    "requirements": {
        "title": "Title", "description": "Description", "no_of_positions": "Positions",
        "experience": "Experience Range", "budget_ctc": "Budget CTC",
        "work_mode": "Work Mode", "location": "Location", "priority": "Priority",
        "target_closure_date": "Target Closure", "jd": "Job Description",
        "tab:details": "Sub-tab: Details", "tab:postings": "Sub-tab: Job Postings",
        "tab:resumes": "Sub-tab: Applied Candidates", "tab:slots": "Sub-tab: Interview Slots",
        "tab:applicants": "Sub-tab: Applicants", "tab:suggested": "Sub-tab: Suggested Candidates",
        "tab:activity": "Sub-tab: Activity Log",
    },
}

# Sub-tab entries for detail pages that already have field catalogues.
FIELDS_BY_TAB["profiles"].update({
    "tab:overview": "Sub-tab: Overview", "tab:interviews": "Sub-tab: Interviews",
    "tab:skills": "Sub-tab: Skill Evaluation", "tab:offers": "Sub-tab: Offers",
    "tab:activity": "Sub-tab: Activity Log", "tab:ai": "Sub-tab: AI Interview",
})
FIELDS_BY_TAB["opportunities"].update({
    "tab:details": "Sub-tab: Details", "tab:applicants": "Sub-tab: Applicants",
    "tab:skill-eval": "Sub-tab: Skill Evaluation", "tab:activity": "Sub-tab: Activity Log",
})


def tab_keys() -> list[str]:
    return list(TABS.keys())


def is_valid_tab(key: str) -> bool:
    return key in TABS


def field_keys(tab: str) -> list[str]:
    return list(FIELDS_BY_TAB.get(tab, {}).keys())


def is_valid_field(tab: str, field: str) -> bool:
    return field in FIELDS_BY_TAB.get(tab, {})


def registry() -> dict:
    """Full catalogue for the template-editor UI."""
    return {
        "modes": list(MODES),
        "field_modes": list(FIELD_MODES),
        "tabs": [
            {"key": k, "label": v, "fields": [{"key": fk, "label": fl}
                                              for fk, fl in FIELDS_BY_TAB.get(k, {}).items()]}
            for k, v in TABS.items()
        ],
    }


def validate_access(tab_access: dict | None, field_access: dict | None) -> None:
    """Raise ValueError on any unknown tab / field / mode."""
    for k, mode in (tab_access or {}).items():
        if not is_valid_tab(k):
            raise ValueError(f"Unknown tab key: {k}")
        if mode not in MODES:
            raise ValueError(f"Invalid mode {mode!r} for tab {k}")
    for tab, fields in (field_access or {}).items():
        if not is_valid_tab(tab):
            raise ValueError(f"Unknown tab key in field_access: {tab}")
        for fk, mode in (fields or {}).items():
            if not is_valid_field(tab, fk):
                raise ValueError(f"Unknown field {fk!r} for tab {tab}")
            if mode not in FIELD_MODES:
                raise ValueError(f"Invalid mode {mode!r} for field {tab}.{fk}")
