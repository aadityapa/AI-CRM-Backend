"""Karnex CRM router registry — main.py calls register_crm_routers(app) once."""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("karnex.api")

#: Registration order. Names, not objects, so one unimportable module costs
#: only its own routes.
#:
#: This used to be a single `from routers.crm import a, b, c, ...` statement.
#: That makes registration all-or-nothing: any one module raising on import —
#: a bad name, a 3.11-only typing import — aborts the whole statement, and
#: because main.py catches and logs the failure the app still starts and still
#: answers health checks. The only symptom is that all ~266 CRM endpoints 404.
#: That has now happened twice, and both times it looked like a frontend bug.
_MODULES: tuple[str, ...] = (
    "access_templates", "me", "me_profile", "masters", "settings", "users_admin",
    "notifications", "files", "customers", "opportunities", "opportunity_attachments",
    "requirements", "resumes", "candidates", "candidate_profiles", "ai_interviews",
    "calendar", "table_preferences", "projects", "timesheets", "finance",
    "finance_reports", "credit_notes", "tax_invoice", "employees", "dashboards",
    "reports", "apply", "template_requests", "slots", "outreach", "holidays",
    "leave_policies", "leave_applications", "ai_assist", "email_outbox", "email_flows",
    "payroll", "rate_cards", "activity_log", "public_invoice", "customer_receipts",
)


def register_crm_routers(app) -> None:
    """Register every CRM router, isolating failures to the module that caused them."""
    loaded: dict[str, object] = {}
    failed: list[tuple[str, str]] = []

    for name in _MODULES:
        try:
            module = importlib.import_module(f"routers.crm.{name}")
            app.include_router(module.router)
            loaded[name] = module
        except Exception as exc:  # noqa: BLE001 - one bad module must not sink the rest
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            log.exception("CRM router '%s' failed to register", name)

    # Extra routers on already-loaded modules.
    if "holidays" in loaded:
        app.include_router(loaded["holidays"].names_router)
    if "employees" in loaded:
        # Flat education/experience row routes for the Employees master form
        # (PUT/DELETE /api/education/{id}, /api/experience/{id} + certificate uploads).
        app.include_router(loaded["employees"].subform_router)

    if failed:
        # Loud and specific: the previous one-line "routers failed to register"
        # gave no clue which module or how much of the API was missing.
        log.error(
            "CRM registration incomplete — %d of %d modules failed: %s",
            len(failed), len(_MODULES),
            "; ".join(f"{name} ({err})" for name, err in failed),
        )
    else:
        log.info("CRM routers registered (%d modules).", len(loaded))
