"""project_leave_policies: audit timestamps + integer maximum_carry_forward.

Revision ID: 0039
Revises: 0038
"""
from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_leave_policies",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "project_leave_policies",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # Spec: Maximum Carry Forward is an integer (was Numeric(5,2) in 0038).
    op.alter_column(
        "project_leave_policies",
        "maximum_carry_forward",
        existing_type=sa.Numeric(5, 2),
        type_=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
        postgresql_using="ROUND(maximum_carry_forward)::integer",
    )


def downgrade() -> None:
    op.alter_column(
        "project_leave_policies",
        "maximum_carry_forward",
        existing_type=sa.Integer(),
        type_=sa.Numeric(5, 2),
        existing_nullable=False,
        existing_server_default="0",
        postgresql_using="maximum_carry_forward::numeric(5,2)",
    )
    op.drop_column("project_leave_policies", "updated_at")
    op.drop_column("project_leave_policies", "created_at")
