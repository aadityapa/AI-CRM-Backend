"""support_tickets + support_ticket_messages — the in-app Help & Support bot's
escalation path to Admin/CEO (14 Sep 2026, user request).

Revision ID: 0103
Revises: 0102
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("support_tickets"):
        op.create_table(
            "support_tickets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticket_no", sa.String(16), nullable=False, unique=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=False, index=True),
            sa.Column("user_name", sa.String(255), nullable=True),
            sa.Column("user_roles", sa.String(255), nullable=True),
            sa.Column("subject", sa.String(255), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("category", sa.String(32), nullable=False, server_default="Other"),
            sa.Column("priority", sa.String(16), nullable=False, server_default="Medium", index=True),
            sa.Column("status", sa.String(16), nullable=False, server_default="Open", index=True),
            sa.Column("page", sa.String(255), nullable=True),
            sa.Column("bot_transcript", JSONB(), nullable=True),
            sa.Column("assigned_to", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True, index=True),
            sa.Column("assigned_to_name", sa.String(255), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("rating", sa.SmallInteger(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )
    if not insp.has_table("support_ticket_messages"):
        op.create_table(
            "support_ticket_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("ticket_id", sa.Integer(), sa.ForeignKey("support_tickets.id", ondelete="CASCADE"),
                      nullable=False, index=True),
            sa.Column("author_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
            sa.Column("author_name", sa.String(255), nullable=True),
            sa.Column("is_staff", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    op.drop_table("support_ticket_messages")
    op.drop_table("support_tickets")
