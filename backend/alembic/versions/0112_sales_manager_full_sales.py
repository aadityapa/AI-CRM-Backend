"""The Sales Manager gets the whole of Sales (29 Sep 2026).

User rule: "Sales has some access, but the Sales Manager and the Sales Head have
the whole of Sales." In code the Sales Manager custom role now CARRIES the
built-in Sales and Sales_Head roles (`services/role_implications.ROLE_IMPLIES`),
which covers role checks, role gates, notifications and — for a role whose
Approvals were never configured — the approvals. What code cannot reach is the
DATA an admin saved, so this revision updates the grants ("update the template
according to this change"):

* every custom role NAMED Sales Manager, and every access template TAGGED
  Sales Manager, gets the tab / field grants of the templates tagged
  Sales_Head or Sales merged in — the higher rung wins per tab (view < edit <
  create), and nothing it already had is lowered or removed;
* where such a row has a SAVED Approvals list (`action_access` not NULL — a
  saved list decides alone in `user_may`), the Sales / Sales Head approvals it
  lacks are appended. NULL lists need nothing: they resolve through the
  implication.

The approval list below is a SNAPSHOT at this revision on purpose (see 0108).
Downgrade is a no-op: grants merged into live roles cannot be told apart from
ones an admin added afterwards.

Revision ID: 0112
Revises: 0111
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None

TARGET = "sales manager"
SOURCE_TAGS = ("Sales_Head", "Sales")
#: approvals whose default named Sales or Sales_Head (registry @ 0112).
_SALES_APPROVALS = (
    "invoice.revision.approve",
    "opportunity.approve",
    "requirement.sales_head_approve",
    "profile.sales_head_decision",
    "profile.budget_resolve",
)
_RANK = {"view": 0, "edit": 1, "create": 2}


def _obj(raw):
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _merge_tabs(into: dict, extra: dict) -> dict:
    out = dict(into or {})
    for tab, mode in (extra or {}).items():
        if _RANK.get(mode, -1) > _RANK.get(out.get(tab), -1):
            out[tab] = mode
    return out


def _merge_fields(into: dict, extra: dict) -> dict:
    out = {t: dict(f or {}) for t, f in (into or {}).items()}
    for tab, fields in (extra or {}).items():
        cur = out.setdefault(tab, {})
        for field, mode in (fields or {}).items():
            if _RANK.get(mode, -1) > _RANK.get(cur.get(field), -1):
                cur[field] = mode
    return out


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    return {c["name"] for c in insp.get_columns(table)} if insp.has_table(table) else set()


def upgrade() -> None:
    bind = op.get_bind()
    tpl_cols = _columns(bind, "access_templates")
    if not tpl_cols:
        return
    tabs: dict = {}
    fields: dict = {}
    for tab_raw, field_raw in bind.execute(sa.text(
            "SELECT tab_access, field_access FROM access_templates "
            "WHERE role IN :tags AND COALESCE(is_active, TRUE)")
            .bindparams(sa.bindparam("tags", expanding=True)), {"tags": list(SOURCE_TAGS)}).all():
        tabs = _merge_tabs(tabs, _obj(tab_raw) or {})
        fields = _merge_fields(fields, _obj(field_raw) or {})

    targets = []
    role_cols = _columns(bind, "custom_roles")
    if role_cols:
        targets.append(("custom_roles", "name", role_cols))
    targets.append(("access_templates", "role", tpl_cols))
    for table, name_col, cols in targets:
        has_actions = "action_access" in cols
        rows = bind.execute(sa.text(
            f"SELECT id, tab_access, field_access{', action_access' if has_actions else ''} "
            f"FROM {table} WHERE LOWER(COALESCE({name_col}, '')) = :t"), {"t": TARGET}).all()
        for row in rows:
            new_tabs = _merge_tabs(_obj(row[1]) or {}, tabs)
            new_fields = _merge_fields(_obj(row[2]) or {}, fields)
            params = {"id": row[0], "tabs": json.dumps(new_tabs), "fields": json.dumps(new_fields)}
            sets = "tab_access = :tabs, field_access = :fields"
            if has_actions:
                saved = _obj(row[3])
                if isinstance(saved, list):
                    params["actions"] = json.dumps(saved + [k for k in _SALES_APPROVALS if k not in saved])
                    sets += ", action_access = :actions"
            bind.execute(sa.text(f"UPDATE {table} SET {sets} WHERE id = :id"), params)


def downgrade() -> None:
    """No-op — see the module docstring."""
