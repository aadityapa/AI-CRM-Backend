"""Opportunity Sales Head approval workflow.

Adds an approval gate to opportunities so a Sales-created opportunity must be
approved by a Sales Head before it is usable. Existing opportunities are
backfilled to 'Approved' (server_default) so nothing that already exists is
suddenly pending — only opportunities created after this migration go through
the new Pending -> Approved/Rejected flow.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APPROVAL_ENUM_NAME = "opportunity_approval_status"
APPROVAL_VALUES = ("Pending_Sales_Head_Approval", "Approved", "Rejected")


def upgrade() -> None:
    bind = op.get_bind()

    # Create the enum type once (checkfirst so re-runs are safe). create_type=False
    # on the column below prevents Alembic from trying to create it a second time.
    approval_enum = postgresql.ENUM(*APPROVAL_VALUES, name=APPROVAL_ENUM_NAME, create_type=False)
    approval_enum.create(bind, checkfirst=True)

    op.add_column(
        "opportunities",
        sa.Column(
            "approval_status",
            approval_enum,
            nullable=False,
            server_default="Approved",  # backfill: existing rows are treated as approved
        ),
    )
    op.create_index(
        "ix_opportunities_approval_status", "opportunities", ["approval_status"]
    )
    op.add_column(
        "opportunities",
        sa.Column("sales_head_approved_by", sa.Integer(), nullable=True),
    )
    op.add_column(
        "opportunities",
        sa.Column("sales_head_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "opportunities",
        sa.Column("approval_rejection_reason", sa.Text(), nullable=True),
    )
    op.create_foreign_key(
        "fk_opportunities_sales_head_approved_by",
        "opportunities",
        "registration_data",
        ["sales_head_approved_by"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_opportunities_sales_head_approved_by", "opportunities", type_="foreignkey"
    )
    op.drop_column("opportunities", "approval_rejection_reason")
    op.drop_column("opportunities", "sales_head_approved_at")
    op.drop_column("opportunities", "sales_head_approved_by")
    op.drop_index("ix_opportunities_approval_status", table_name="opportunities")
    op.drop_column("opportunities", "approval_status")
    postgresql.ENUM(name=APPROVAL_ENUM_NAME).drop(op.get_bind(), checkfirst=True)
