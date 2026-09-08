"""Add projects.branch_id FK and backfill from opportunities.

Explicit Project→Branch link so branch policy linked_projects and timesheet
branch resolution do not depend solely on opportunity.branch_id.

Revision ID: 0045
Revises: 0044
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("branch_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_branch_id_customer_branches",
        "projects",
        "customer_branches",
        ["branch_id"],
        ["id"],
    )
    op.create_index("ix_projects_branch_id", "projects", ["branch_id"])
    # Backfill from opportunity.branch_id (legacy linkage).
    op.execute(
        """
        UPDATE projects AS p
        SET branch_id = o.branch_id
        FROM opportunities AS o
        WHERE p.opportunity_id = o.id
          AND o.branch_id IS NOT NULL
          AND p.branch_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_projects_branch_id", table_name="projects")
    op.drop_constraint(
        "fk_projects_branch_id_customer_branches",
        "projects",
        type_="foreignkey",
    )
    op.drop_column("projects", "branch_id")
