"""Add settlement_pending on project_employees (UC-09 exit settlement flag).

Revision ID: 0025
Revises: 0024
Create Date: 2026-07-14
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "project_employees",
        sa.Column(
            "settlement_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("project_employees", "settlement_pending")
