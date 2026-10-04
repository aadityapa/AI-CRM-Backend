"""requirement_ta_assignments — RMG / GM assign a position to one or more
TAs (1 Oct 2026, user request).

Revision ID: 0117
Revises: 0116
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

TABLE = "requirement_ta_assignments"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requirement_id", sa.Integer(),
                  sa.ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"),
                  nullable=False, index=True),
        sa.Column("user_name", sa.String(255), nullable=True),
        sa.Column("assigned_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("assigned_by_name", sa.String(255), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("requirement_id", "user_id", name="uq_req_ta_assignment"),
    )


def downgrade() -> None:
    op.drop_table(TABLE)
