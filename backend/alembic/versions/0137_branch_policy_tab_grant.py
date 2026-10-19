"""Grant the branch page to every template / custom role that links to it (16 Oct 2026).

Reported: a Finance login opened Customers ▸ Branches ▸ "Received amount" and got
"You do not have access to the 'branch-policy' tab". The Received Amount tab
lives on the branch page, and no role template had ever listed `branch-policy`.

The server now implies `branch-policy: view` for a login whose grants include
`customers` or `invoices` (`access_registry.IMPLIED_TABS`). This migration
writes the same grant into the SAVED rows, so Access Control shows it ticked
and an admin can raise or remove it like any other tab. An existing grant is
never touched (never lowered). Downgrade is a no-op.

Revision ID: 0137
Revises: 0136
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0137"
down_revision = "0136"
branch_labels = None
depends_on = None

#: Frozen copy of access_registry.IMPLIED_TABS @ 0137.
_IMPLIED = {"branch-policy": ("customers", "invoices")}


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


def _bare(key: str) -> str:
    k = str(key or "")
    return k[4:] if k.startswith("crm:") else k


def _fill(tabs: dict) -> dict | None:
    present = {_bare(k) for k in tabs}
    changed = False
    for implied, sources in _IMPLIED.items():
        if implied in present:
            continue
        if any(tabs.get(src) or tabs.get(f"crm:{src}") for src in sources):
            tabs[implied] = "view"
            changed = True
    return tabs if changed else None


def _backfill(bind, table: str) -> None:
    insp = sa.inspect(bind)
    if table not in insp.get_table_names():
        return
    if "tab_access" not in {c["name"] for c in insp.get_columns(table)}:
        return
    rows = bind.execute(sa.text(f"SELECT id, tab_access FROM {table}")).all()
    for row_id, raw in rows:
        tabs = _loads(raw)
        if not tabs:
            continue
        new = _fill(tabs)
        if new is None:
            continue
        bind.execute(sa.text(f"UPDATE {table} SET tab_access = :t WHERE id = :id"),
                     {"t": json.dumps(new), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()
    _backfill(bind, "access_templates")
    _backfill(bind, "custom_roles")


def downgrade() -> None:
    pass
