"""Add opportunity_id to template_requests (denormalised from requirement).

Revision ID: 0031
Revises: 0030
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "template_requests",
        sa.Column("opportunity_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_template_requests_opportunity_id",
        "template_requests",
        "opportunities",
        ["opportunity_id"],
        ["id"],
    )
    op.create_index(
        "ix_template_requests_opportunity_id",
        "template_requests",
        ["opportunity_id"],
    )
    # Backfill from the linked requirement.
    op.execute(
        """
        UPDATE template_requests tr
        SET opportunity_id = r.opportunity_id
        FROM requirements r
        WHERE tr.requirement_id = r.id
          AND tr.opportunity_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_template_requests_opportunity_id", table_name="template_requests")
    op.drop_constraint("fk_template_requests_opportunity_id", "template_requests", type_="foreignkey")
    op.drop_column("template_requests", "opportunity_id")
