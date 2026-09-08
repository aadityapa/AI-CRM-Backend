"""Upgrade pre-ladder template grants: "edit" -> "create".

Until Aug 2026 the template editor's highest mode was labelled "Insert / Edit"
and stored as "edit" — and creation endpoints enforced only edit, so an "edit"
grant COULD create records. The new mode ladder (view < edit < create) put
creation behind a distinct "create" grant, which silently demoted every
template saved under the old editor: users who could create projects yesterday
lost the button today.

This migration restores the authored intent: every stored tab grant of "edit"
becomes "create" (what "Insert / Edit" meant). Admins who want the new
edit-without-create level can lower individual tabs afterwards — that level
simply did not exist when these templates were written, so no stored "edit"
can mean it. Field grants are untouched: fields never had a create level.

Revision ID: 0070
Revises: 0069
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def _convert(tab_access, upgrade: bool) -> tuple[dict, bool]:
    src, dst = ("edit", "create") if upgrade else ("create", "edit")
    data = tab_access if isinstance(tab_access, dict) else json.loads(tab_access or "{}")
    changed = False
    out = {}
    for tab, mode in (data or {}).items():
        if mode == src:
            out[tab] = dst
            changed = True
        else:
            out[tab] = mode
    return out, changed


def _run(bind, upgrade: bool) -> None:
    inspector = sa.inspect(bind)
    if not inspector.has_table("access_templates"):
        return
    rows = bind.execute(sa.text("SELECT id, tab_access FROM access_templates")).all()
    for row_id, tab_access in rows:
        converted, changed = _convert(tab_access, upgrade)
        if changed:
            bind.execute(
                sa.text("UPDATE access_templates SET tab_access = :ta WHERE id = :id"),
                {"ta": json.dumps(converted), "id": row_id},
            )


def upgrade() -> None:
    _run(op.get_bind(), upgrade=True)


def downgrade() -> None:
    # Lossy on purpose: templates authored AFTER the ladder with a genuine
    # "create" grant also fold back to "edit" — which is exactly what the old
    # world's single write level expressed.
    _run(op.get_bind(), upgrade=False)
