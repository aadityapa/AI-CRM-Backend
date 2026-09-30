"""Screening Desk approvals (25 Sep 2026).

Two approval changes, both in `services.action_permissions`:

* `profile.rmg_screening` now defaults to RMG **and GM** — both work the new
  Screening Desk (Opportunities-wise queue of TA-applied candidates).
* New `profile.fast_track_internal` (RMG, GM): send an internal candidate (an
  existing Karnex employee) straight to Sales for customer screening, skipping
  the L1 / L2 rounds.

A template or custom role whose Approvals list was ever SAVED decides alone
(`user_may`), so a code default alone would never reach it. Backfill: every
access template TAGGED RMG / GM and every custom role NAMED RMG / GM whose
`action_access` is not NULL gets the keys it lacks. NULL lists need nothing —
they already resolve to the code defaults. Nothing is ever removed.

The role map below is a SNAPSHOT at this revision on purpose (see 0108).

Revision ID: 0111
Revises: 0110
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None

#: approval action -> roles whose saved Approvals list gets it (registry @ 0111).
#: Only the entries this revision CHANGED or ADDED; 0108 holds the rest.
_APPROVAL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "profile.rmg_screening": ("RMG", "GM"),
    "profile.fast_track_internal": ("RMG", "GM"),
}


def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _backfill(bind, table: str, name_col: str) -> None:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return
    if "action_access" not in {c["name"] for c in insp.get_columns(table)}:
        return
    rows = bind.execute(sa.text(
        f"SELECT id, {name_col}, action_access FROM {table} "
        "WHERE action_access IS NOT NULL")).all()
    for row_id, name, raw in rows:
        current = _loads(raw)
        if current is None:
            continue
        wanted = [k for k, roles in _APPROVAL_DEFAULTS.items()
                  if (name or "") in roles and k not in current]
        if not wanted:
            continue
        bind.execute(sa.text(f"UPDATE {table} SET action_access = :a WHERE id = :id"),
                     {"a": json.dumps(current + wanted), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()
    _backfill(bind, "access_templates", "role")
    _backfill(bind, "custom_roles", "name")


def downgrade() -> None:
    # Grants are not revoked on downgrade: an admin may have ticked them by
    # hand since, and a downgrade must never silently take access away.
    pass
