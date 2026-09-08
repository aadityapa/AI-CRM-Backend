"""Add Closed_Partial to opportunity_pipeline_stage enum.

Revision ID: 0030
Revises: 0029
Create Date: 2026-07-17
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # ALTER TYPE ... ADD VALUE cannot run inside a transaction on PostgreSQL.
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE opportunity_pipeline_stage ADD VALUE IF NOT EXISTS 'Closed_Partial'"
            )


def downgrade() -> None:
    # PostgreSQL cannot remove enum values safely; leave Closed_Partial in place.
    pass
