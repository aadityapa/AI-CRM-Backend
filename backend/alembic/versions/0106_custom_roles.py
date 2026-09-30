"""custom_roles + user_custom_roles — Admin/CEO-defined roles (23 Sep 2026).

A role the business names ("GM") with its own tab permissions, without
touching the `role_name` Postgres enum behind the eight built-in roles. See
models/custom_roles.py for how it takes effect.

Revision ID: 0106
Revises: 0105
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("custom_roles"):
        op.create_table(
            "custom_roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(40), nullable=False, unique=True),
            sa.Column("description", sa.String(512), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("tab_access", sa.JSON(), nullable=True),
            sa.Column("field_access", sa.JSON(), nullable=True),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not insp.has_table("user_custom_roles"):
        op.create_table(
            "user_custom_roles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=False, index=True),
            sa.Column("custom_role_id", sa.Integer(), sa.ForeignKey("custom_roles.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.UniqueConstraint("user_id", "custom_role_id", name="uq_user_custom_role"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("user_custom_roles"):
        op.drop_table("user_custom_roles")
    if insp.has_table("custom_roles"):
        op.drop_table("custom_roles")
