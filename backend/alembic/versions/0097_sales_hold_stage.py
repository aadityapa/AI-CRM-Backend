"""opportunity_pipeline_stage gains Sales_Hold.

Sales can park a deal themselves (8 Sep 2026, user request): the header
buttons on the opportunity page are Close Won / Close Lost / Customer Hold
(= On_Hold) / Close Partial / Sales Hold. New enum member only; no data
rewrite.

Revision ID: 0097
Revises: 0096
"""
from __future__ import annotations

from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE opportunity_pipeline_stage ADD VALUE IF NOT EXISTS 'Sales_Hold'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; rows on Sales_Hold would need a
    # rewrite first. Left in place deliberately.
    pass
