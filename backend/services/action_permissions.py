"""Admin-editable action permissions: WHO MAY DO, not just who may see.

Same philosophy as email routing (services.org_settings / notification
routes): the role tuple in code is only the DEFAULT. When Admin/CEO save a
row in `action_permissions`, that row decides which roles may perform the
action — no code change, no redeploy, effective within the cache TTL
(immediately for the admin who saved, via invalidate()).

Two safety properties are non-negotiable:

* Admin/CEO ALWAYS pass — mirrored from role_required(allow_admin=True).
  An empty saved role list therefore means "admins only", never "nobody",
  so an admin cannot lock themselves out of their own application.
* A lookup can never fail closed by accident: any database problem falls
  back to the code default, same as before this table existed.
"""
from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger("karnex.action_permissions")

#: Every admin-editable action: key -> (label, description, default roles).
#: Adding a new gated_write_action() call site needs one line here to appear
#: in the Users tab panel.
ACTIONS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "timesheet.approve": (
        "Approve timesheets",
        "Approve a submitted monthly timesheet.",
        ("RMG", "Sales")),
    "timesheet.reject": (
        "Reject timesheets",
        "Reject a submitted timesheet back to the employee.",
        ("RMG", "Sales")),
    "timesheet.generate_invoice": (
        "Generate invoices from timesheets",
        "Pick the PO and generate the invoice for an approved timesheet.",
        ("Finance", "RMG")),
    "project_employee.manage": (
        "Map / edit project employees",
        "Map an employee onto a project and edit the mapping.",
        ("Sales_Head", "HR", "Finance")),
    "project_employee.rates": (
        "Edit commercial rates",
        "Add, edit or delete effective-dated billing rates.",
        ("Sales_Head", "Finance", "HR", "RMG")),
    "po.manage": (
        "Create / edit purchase orders",
        "Create, edit and allocate purchase orders.",
        ("Finance",)),
    "invoice.manage": (
        "Create / edit invoices",
        "Create and edit invoices and payments.",
        ("Finance",)),
    "candidate.email": (
        "Bulk-email suggested candidates",
        "Send the hiring-interest email to selected suggested candidates.",
        ("TA", "Sales", "Sales_Head", "RMG")),
    "leave.approve": (
        "Approve leave applications",
        "Approve a pending leave application.",
        ("HR",)),
    "leave.reject": (
        "Reject leave applications",
        "Reject a pending leave application.",
        ("HR",)),
}

_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, list[str]] = {}
_cache_at: float = 0.0


def invalidate() -> None:
    global _cache_at
    with _lock:
        _cache_at = 0.0


def _load_rows() -> dict[str, list[str]]:
    try:
        from sqlalchemy import text

        from crm_db import get_session_factory

        session = get_session_factory()()
        try:
            rows = session.execute(
                text("SELECT action, roles FROM action_permissions")).all()
            out: dict[str, list[str]] = {}
            for action, roles in rows:
                out[action] = [str(r) for r in (roles or [])]
            return out
        finally:
            session.close()
    except Exception as exc:
        logger.debug("action_permissions load failed (using code defaults): %s", exc)
        return {}


def roles_for_action(action: str, default_roles) -> list[str]:
    """Effective role list for one action: saved row, else the code default.

    NOTE: an existing row with an EMPTY list is honoured as "admins only" —
    the empty list is a deliberate admin choice here, unlike email routing
    where empty falls back (an event with no receiver is a mistake there).
    """
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        stale = now - _cache_at > _TTL_SECONDS
    if stale:
        try:
            rows = _load_rows()
        except Exception:
            rows = {}
        with _lock:
            _cache = rows
            _cache_at = now
    with _lock:
        if action in _cache:
            return list(_cache[action])
    return [r for r in (default_roles or [])]


def effective(db) -> list[dict]:
    """All actions with label/default/current for the admin panel (fresh read)."""
    from sqlalchemy import text

    try:
        rows = dict(db.execute(text("SELECT action, roles FROM action_permissions")).all())
    except Exception:
        rows = {}
    out = []
    for action, (label, description, defaults) in ACTIONS.items():
        saved = rows.get(action)
        out.append({
            "action": action,
            "label": label,
            "description": description,
            "default_roles": list(defaults),
            "roles": [str(r) for r in saved] if saved is not None else list(defaults),
            "customized": saved is not None,
        })
    return out
