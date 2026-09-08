"""Admin-editable action permissions (who may approve / invoice / manage).

One row per configurable write action; no row means the code default. An
empty saved role list means Admin/CEO only — admins always pass the gate,
so this table can never lock the administrators out.

Revision ID: 0067
Revises: 0066
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("action_permissions"):
        return
    op.create_table(
        "action_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("roles", JSONB(), nullable=False, server_default="[]"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.UniqueConstraint("action", name="uq_action_permissions_action"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("action_permissions"):
        op.drop_table("action_permissions")
