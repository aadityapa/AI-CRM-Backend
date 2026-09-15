"""invoice_revisions — change requests on generated invoices.

User flow 11 Sep 2026: a customer asks for a correction after the invoice
went out → Sales/Finance request the change with a reason → Sales / Sales
Head (Admin/CEO always) approve or reject → only an approved request
changes the invoice. Every request and decision, with the before/after of
each field, stays here as the invoice's change history.

Revision ID: 0099
Revises: 0098
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("invoice_revisions"):
        return
    op.create_table(
        "invoice_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="Pending", index=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("changes", JSONB(), nullable=False),
        sa.Column("snapshot_before", JSONB(), nullable=True),
        sa.Column("snapshot_after", JSONB(), nullable=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True, index=True),
        sa.Column("requested_by_name", sa.String(255), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("decided_by_name", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("invoice_revisions"):
        op.drop_table("invoice_revisions")
