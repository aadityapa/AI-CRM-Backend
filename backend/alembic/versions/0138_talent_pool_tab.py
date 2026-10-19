"""Grant the new Talent Pool tab to the people it is for (16 Oct 2026).

The Talent Pool (Phase 1) is a new grantable tab, `talent-pool`. A templated
login sees only what its template names, so without this every TA / RMG / Sales
Head on a saved template would get 403 on the new page. User decision: TA,
RMG / GM, Sales Head and Admin / CEO open it.

Writes `talent-pool: view` into SAVED rows that do not name it yet:
  * access templates TAGGED with, or assigned to people who hold, TA · RMG ·
    Sales_Head (the members' built-in roles — the 0124 rule);
  * custom roles that act as one of them (`_ROLE_ALIASES`: GM → RMG — frozen
    copy of access_registry.IV_ROLE_ALIASES @ 0138).
An existing grant is never touched. Downgrade is a no-op (an admin may have
ticked it by hand since).

Revision ID: 0138
Revises: 0137
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0138"
down_revision = "0137"
branch_labels = None
depends_on = None

#: Frozen @ 0138.
_TAB = "talent-pool"
_ROLES = ("TA", "RMG", "Sales_Head")
_ROLE_ALIASES = {"gm": ("RMG",)}


def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _names(tabs: dict) -> set[str]:
    return {str(k)[4:] if str(k).startswith("crm:") else str(k) for k in tabs}


def _has_cols(insp, table: str, *cols: str) -> bool:
    if not insp.has_table(table):
        return False
    names = {c["name"] for c in insp.get_columns(table)}
    return all(c in names for c in cols)


def _grant_templates(bind) -> None:
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
    for row_id, role, raw in bind.execute(sa.text("SELECT id, role, tab_access FROM access_templates")).all():
        tabs = _loads(raw)
        if tabs is None or _TAB in _names(tabs):
            continue
        holders = set(members.get(int(row_id), set()))
        if role:
            holders.add(str(role))
        if holders & set(_ROLES):
            tabs[_TAB] = "view"
            bind.execute(sa.text("UPDATE access_templates SET tab_access = :t WHERE id = :id"),
                         {"t": json.dumps(tabs), "id": row_id})


def _grant_custom_roles(bind) -> None:
    insp = sa.inspect(bind)
    if not _has_cols(insp, "custom_roles", "name", "tab_access"):
        return
    for row_id, name, raw in bind.execute(sa.text("SELECT id, name, tab_access FROM custom_roles")).all():
        tabs = _loads(raw)
        if tabs is None or _TAB in _names(tabs):
            continue
        acts_as = set(_ROLE_ALIASES.get(str(name or "").strip().lower(), ()))
        if acts_as & set(_ROLES):
            tabs[_TAB] = "view"
            bind.execute(sa.text("UPDATE custom_roles SET tab_access = :t WHERE id = :id"),
                         {"t": json.dumps(tabs), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()
    _grant_templates(bind)
    _grant_custom_roles(bind)


def downgrade() -> None:
    pass
