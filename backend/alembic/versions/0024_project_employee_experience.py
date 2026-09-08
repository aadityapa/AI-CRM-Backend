"""Add project_experience_years on project_employees.

Revision ID: 0024
Revises: 0023
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_employees",
        sa.Column("project_experience_years", sa.Numeric(4, 1), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("project_employees", "project_experience_years")
