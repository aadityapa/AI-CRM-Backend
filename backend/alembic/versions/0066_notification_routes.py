"""Admin-editable email routing + per-user email pause.

notification_routes: event -> roles/extra emails/enabled, edited from the
Users tab so changing who receives what never needs a code change again.
user_notify_prefs: pause a leaver's email without touching their account.

Revision ID: 0066
Revises: 0065
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("notification_routes"):
        op.create_table(
            "notification_routes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("event", sa.String(length=64), nullable=False),
            sa.Column("roles", JSONB(), nullable=False, server_default="[]"),
            sa.Column("extra_emails", JSONB(), nullable=False, server_default="[]"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_by", sa.Integer(), nullable=True),
            sa.UniqueConstraint("event", name="uq_notification_routes_event"),
        )
    if not insp.has_table("user_notify_prefs"):
        op.create_table(
            "user_notify_prefs",
            sa.Column("user_id", sa.Integer(), primary_key=True),
            sa.Column("email_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("updated_by", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("user_notify_prefs"):
        op.drop_table("user_notify_prefs")
    if insp.has_table("notification_routes"):
        op.drop_table("notification_routes")
