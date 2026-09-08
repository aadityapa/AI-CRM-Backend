"""Project-level leave-policy overrides feed PE seeding.

Adds project_employee_leave_details.project_leave_policy_id so a PE leave row
can record that it was seeded from a PROJECT override (project wins over
branch/customer in the crediting chain). Existing rows keep their
customer_leave_policy_id linkage untouched.

Revision ID: 0048
Revises: 0047
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_employee_leave_details",
        sa.Column("project_leave_policy_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_pe_leave_detail_project_policy",
        "project_employee_leave_details",
        "project_leave_policies",
        ["project_leave_policy_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_pe_leave_detail_project_policy",
        "project_employee_leave_details",
        type_="foreignkey",
    )
    op.drop_column("project_employee_leave_details", "project_leave_policy_id")
