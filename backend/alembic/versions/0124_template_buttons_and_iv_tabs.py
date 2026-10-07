"""One template decides every tab and every button (7 Oct 2026).

Two things became template-configurable in `services.access_registry` /
`services.action_permissions`, and this backfill makes sure NOTHING changes
for existing users on deploy:

1. **Interview Platform tabs** (`iv:dashboard`, `iv:templates`, `iv:candidates`,
   `iv:ats`, `iv:integrityLogs`) are grantable. A template speaks for the
   Interview Platform only once it names at least one of them (the shell's
   rule), so every access template that names none gets the tabs its people
   see TODAY by role: the union of the role tag and the built-in roles of
   every user assigned to it, mapped through the role → tab defaults frozen
   below (the mirror of the frontend's `INTERVIEW_VIEW_ROLES`). A template
   with no tag and no users gets nothing — there is nobody to keep working.

2. **Manage buttons** join the Approvals list. A template / custom role whose
   `action_access` list was ever SAVED now decides every button by that list,
   so each one gets the manage buttons its tab grants already implied (the
   gate's old rule: tab at Edit or better). NULL lists keep following the
   code defaults and need nothing.

Nothing is ever removed. The maps are SNAPSHOTS at this revision on purpose
(see 0108): a later change to the registry must ship its own backfill.

Revision ID: 0124
Revises: 0123
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

#: Interview Platform tab -> roles that open it by default (registry @ 0124).
_IV_TAB_ROLES: dict[str, tuple[str, ...]] = {
    "iv:dashboard": ("TA", "HR", "RMG"),
    "iv:templates": ("RMG",),
    "iv:candidates": ("TA", "HR", "RMG"),
    "iv:ats": ("TA", "HR", "RMG"),
    "iv:integrityLogs": ("TA", "HR", "RMG"),
}
#: custom role name (lower-cased) -> the built-in role it acts as on the
#: platform (registry `IV_ROLE_ALIASES` @ 0124): the GM screens as RMG.
_IV_ROLE_ALIASES: dict[str, tuple[str, ...]] = {"gm": ("RMG",)}
#: manage button -> the CRM tab its gate reads (registry @ 0124).
_BUTTON_TABS: dict[str, str] = {
    "project_employee.manage": "project-employees",
    "project_employee.rates": "project-employees",
    "po.manage": "pos",
    "invoice.manage": "invoices",
    "invoice.revision.request": "invoices",
    "candidate.email": "candidates",
    "project.close": "projects",
    "requirement.positions.request": "requirements",
}
_EDIT_OR_BETTER = {"edit", "create"}


def _loads(raw, kind):
    if raw is None:
        return None
    value = raw
    if not isinstance(raw, (list, dict)):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
    return value if isinstance(value, kind) else None


def _bare(key: str) -> str:
    k = str(key or "")
    return k[4:] if k.startswith("crm:") else k


def _has_cols(insp, table: str, *cols: str) -> bool:
    if not insp.has_table(table):
        return False
    names = {c["name"] for c in insp.get_columns(table)}
    return all(c in names for c in cols)


def _backfill_buttons(bind, table: str) -> None:
    insp = sa.inspect(bind)
    if not _has_cols(insp, table, "action_access", "tab_access"):
        return
    rows = bind.execute(sa.text(
        f"SELECT id, tab_access, action_access FROM {table} WHERE action_access IS NOT NULL")).all()
    for row_id, tabs_raw, actions_raw in rows:
        actions = _loads(actions_raw, list)
        if actions is None:
            continue
        tabs = {_bare(k): v for k, v in (_loads(tabs_raw, dict) or {}).items()}
        wanted = [k for k, tab in _BUTTON_TABS.items()
                  if k not in actions and tabs.get(tab) in _EDIT_OR_BETTER]
        if wanted:
            bind.execute(sa.text(f"UPDATE {table} SET action_access = :a WHERE id = :id"),
                         {"a": json.dumps(actions + wanted), "id": row_id})


def _backfill_iv_tabs(bind) -> None:
    insp = sa.inspect(bind)
    if not _has_cols(insp, "access_templates", "tab_access", "role"):
        return
    members: dict[int, set[str]] = {}
    if _has_cols(insp, "user_profiles", "access_template_id") and insp.has_table("user_roles") \
            and insp.has_table("roles"):
        for tid, role in bind.execute(sa.text(
            "SELECT p.access_template_id, r.name FROM user_profiles p "
            "JOIN user_roles ur ON ur.user_id = p.user_id "
            "JOIN roles r ON r.id = ur.role_id "
            "WHERE p.access_template_id IS NOT NULL")).all():
            members.setdefault(int(tid), set()).add(str(role))
    rows = bind.execute(sa.text("SELECT id, role, tab_access FROM access_templates")).all()
    for row_id, role, tabs_raw in rows:
        tabs = _loads(tabs_raw, dict)
        if tabs is None:
            continue
        if any(str(k).startswith("iv:") for k in tabs):
            continue                      # the template already speaks for the platform
        names = set(members.get(int(row_id), set()))
        if role:
            names.add(str(role))
        grant = {k: "view" for k, allowed in _IV_TAB_ROLES.items() if names & set(allowed)}
        if grant:
            tabs = dict(tabs)
            tabs.update(grant)
            bind.execute(sa.text("UPDATE access_templates SET tab_access = :t WHERE id = :id"),
                         {"t": json.dumps(tabs), "id": row_id})


def _backfill_custom_role_iv_tabs(bind) -> None:
    """A custom role names no built-in role, so its people would lose the
    Interview Platform the moment the role's grants start to speak for it.
    Give every custom role that names no `iv:` key the tabs of the built-in
    role it acts as (`_IV_ROLE_ALIASES`, e.g. GM → RMG)."""
    insp = sa.inspect(bind)
    if not _has_cols(insp, "custom_roles", "name", "tab_access"):
        return
    rows = bind.execute(sa.text("SELECT id, name, tab_access FROM custom_roles")).all()
    for row_id, name, tabs_raw in rows:
        tabs = _loads(tabs_raw, dict)
        if tabs is None or any(str(k).startswith("iv:") for k in tabs):
            continue
        names = set(_IV_ROLE_ALIASES.get(str(name or "").strip().lower(), ()))
        grant = {k: "view" for k, allowed in _IV_TAB_ROLES.items() if names & set(allowed)}
        if grant:
            tabs = dict(tabs)
            tabs.update(grant)
            bind.execute(sa.text("UPDATE custom_roles SET tab_access = :t WHERE id = :id"),
                         {"t": json.dumps(tabs), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()
    _backfill_buttons(bind, "access_templates")
    _backfill_buttons(bind, "custom_roles")
    _backfill_iv_tabs(bind)
    _backfill_custom_role_iv_tabs(bind)


def downgrade() -> None:
    # Grants are never revoked on downgrade (an admin may have ticked them by
    # hand since); the older code simply ignores the extra keys.
    pass
