"""Default Access-Template grants per CRM role (Phase C, 27 Aug 2026).

One dict per role, keys from services.access_registry.TABS, values from MODES.
Seeded as templates named "Default — <Role>" (role-tagged, so users_admin's
auto-assign picks them up for new users of that role). Admin/CEO need no
template — they bypass the gate.

A tab OMITTED here means the role does not see it at all when templated.
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
        "timesheets": "edit", "reports": "view",
    },
    "Sales_Head": {
        "dashboard": "view", "customers": "create", "opportunities": "create",
        "requirements": "edit", "candidates": "edit", "profiles": "edit",
        "template-requests": "view", "calendar": "view", "emails": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "timesheets": "edit", "pos": "view",
        "invoices": "view", "tds": "view", "finance-reports": "view",
        "reports": "view", "rate-cards": "edit",
    },
    "RMG": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "requirements": "edit", "candidates": "edit", "profiles": "edit",
        "template-requests": "edit", "calendar": "edit", "emails": "edit",
        "activity-log": "view", "projects": "view", "my-leave": "view",
        "timesheets": "edit", "reports": "view",
    },
    "TA": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "requirements": "edit", "candidates": "create", "profiles": "create",
        "template-requests": "create", "calendar": "edit", "emails": "create",
        "activity-log": "view", "my-leave": "view", "timesheets": "view",
        "reports": "view",
    },
    "HR": {
        "dashboard": "view", "candidates": "view", "profiles": "view",
        "template-requests": "view", "calendar": "view", "emails": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "leave-applications": "edit", "holidays": "create",
        "timesheets": "edit", "employees": "create", "payroll": "view",
        "reports": "view",
    },
    "Finance": {
        "dashboard": "view", "customers": "view", "opportunities": "view",
        "activity-log": "view", "projects": "edit", "project-employees": "edit",
        "my-leave": "view", "timesheets": "create", "pos": "create",
        "invoices": "create", "tds": "create", "finance-reports": "view",
        "payroll": "view", "employees": "view", "reports": "view",
    },
}


def seed_role_templates(db, *, overwrite: bool = False) -> dict:
    """Create (or with overwrite=True, update) the six role-default templates.

    Never touches a template an admin has customised unless overwrite is
    explicit. Returns {role: "created" | "updated" | "kept"}.
    """
    from models import AccessTemplate

    from sqlalchemy import select

    out: dict[str, str] = {}
    for role, tabs in ROLE_TEMPLATE_DEFAULTS.items():
        name = f"Default — {role.replace('_', ' ')}"
        existing = db.execute(
            select(AccessTemplate).where(AccessTemplate.name == name)
        ).scalars().first()
        if existing is None:
            t = AccessTemplate(name=name, tab_access=dict(tabs), field_access={},
                               is_active=True)
            if hasattr(t, "role"):
                t.role = role
            db.add(t)
            out[role] = "created"
        elif overwrite:
            existing.tab_access = dict(tabs)
            existing.is_active = True
            if hasattr(existing, "role"):
                existing.role = role
            out[role] = "updated"
        else:
            out[role] = "kept"
    return out
