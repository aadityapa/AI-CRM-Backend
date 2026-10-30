"""Suggested tab permissions per built-in CRM role (Phase C, 27 Aug 2026).

One dict per role, keys from services.access_registry.TABS, values from MODES.
They used to be seeded as "Default — <Role>" Access Templates; since the
templates were merged into Roles (27 Oct 2026) they are the starting point the
Roles page offers when an admin sets a built-in role's permissions
("Start from the suggested permissions"). Admin/CEO need none.

A tab OMITTED here means the role does not see it once configured.
Pinned by tests/test_role_template_defaults.py so a registry rename can never
silently orphan a grant.
"""
from __future__ import annotations

ROLE_TEMPLATE_DEFAULTS: dict[str, dict[str, str]] = {
    "Sales": {
        "dashboard": "view", "customers": "create", "opportunities": "create",
        "requirements": "view", "candidates": "edit", "profiles": "edit",
        "template-requests": "view", "calendar": "view", "emails": "view",
        "activity-log": "view", "projects": "view", "my-leave": "view",
        "timesheets": "edit", "reports": "view", "branch-policy": "edit",
    },
    "Sales_Head": {
        "dashboard": "view", "customers": "create", "opportunities": "create",
        "requirements": "edit", "candidates": "edit", "profiles": "edit",
        "template-requests": "view", "calendar": "view", "emails": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "timesheets": "edit", "pos": "view",
        "invoices": "view", "tds": "view", "finance-reports": "view",
        "reports": "view", "rate-cards": "edit", "branch-policy": "edit",
        "talent-pool": "view",
    },
    "RMG": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "requirements": "edit", "candidates": "edit", "profiles": "edit",
        "template-requests": "edit", "calendar": "edit", "emails": "edit",
        "activity-log": "view", "projects": "view", "my-leave": "view",
        "timesheets": "edit", "reports": "view", "branch-policy": "view",
        "talent-pool": "view",
    },
    "TA": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "requirements": "edit", "candidates": "create", "profiles": "create",
        "template-requests": "create", "calendar": "edit", "emails": "create",
        "activity-log": "view", "my-leave": "view", "timesheets": "view",
        "reports": "view", "branch-policy": "view", "talent-pool": "view",
    },
    "HR": {
        "dashboard": "view", "candidates": "view", "profiles": "view",
        "template-requests": "view", "calendar": "view", "emails": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "leave-applications": "edit", "holidays": "create",
        "timesheets": "edit", "employees": "create", "payroll": "view",
        "reports": "view", "branch-policy": "edit",
    },
    "Finance": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "timesheets": "create", "pos": "create",
        "invoices": "create", "tds": "create", "finance-reports": "view",
        "payroll": "view", "employees": "view", "reports": "view",
        "branch-policy": "view",
    },
}
