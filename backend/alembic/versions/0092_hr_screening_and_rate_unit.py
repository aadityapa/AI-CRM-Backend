"""HR_Screening pipeline stage + offer rate unit.

User flow 2 Sep 2026:

  * PipelineStatus gains ``HR_Screening`` — between Sales Head's approval and
    Preboarding. TA schedules the HR round, HR records the verdict and moves
    the candidate on. Native enum: ALTER TYPE in an autocommit block, same as
    0083's On_Hold. Postgres cannot drop an enum value, so downgrade leaves it.
  * ``offer_history.rate_unit`` / ``rate_value`` — the candidate rate as Sales
    typed it (Hourly / Monthly / Yearly + figure). ``ctc`` stays the annualised
    rupee amount every downstream calculation already reads.

Revision ID: 0092
Revises: 0091
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE profile_pipeline_status ADD VALUE IF NOT EXISTS 'HR_Screening'")
    inspector = sa.inspect(bind)
    if inspector.has_table("offer_history"):
        columns = {c["name"] for c in inspector.get_columns("offer_history")}
        if "rate_unit" not in columns:
            op.add_column("offer_history", sa.Column("rate_unit", sa.String(16), nullable=True))
        if "rate_value" not in columns:
            op.add_column("offer_history", sa.Column("rate_value", sa.Numeric(14, 2), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("offer_history"):
        columns = {c["name"] for c in inspector.get_columns("offer_history")}
        for col in ("rate_value", "rate_unit"):
            if col in columns:
                op.drop_column("offer_history", col)
