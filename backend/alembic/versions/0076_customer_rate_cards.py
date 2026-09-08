"""customer_rate_cards — per-customer experience-band pricing.

One row per experience band (1–2 yrs … 14–15 yrs) with five OPTIONAL rate
columns (hourly/daily/weekly/monthly/yearly). Nullable by design: customers
quote only the unit they bill in, and a blank cell means "not quoted", never
zero. Feeds the Candidate CTC Slab auto-fill in the Opportunity form.

Revision ID: 0076
Revises: 0075
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("customer_rate_cards"):
        return
    op.create_table(
        "customer_rate_cards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(),
                  sa.ForeignKey("customers.id"), nullable=False, index=True),
        sa.Column("exp_min", sa.Numeric(4, 1), nullable=False),
        sa.Column("exp_max", sa.Numeric(4, 1), nullable=False),
        sa.Column("rate_hourly", sa.Numeric(12, 2), nullable=True),
        sa.Column("rate_daily", sa.Numeric(12, 2), nullable=True),
        sa.Column("rate_weekly", sa.Numeric(12, 2), nullable=True),
        sa.Column("rate_monthly", sa.Numeric(12, 2), nullable=True),
        sa.Column("rate_yearly", sa.Numeric(12, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("customer_id", "exp_min", "exp_max", name="uq_rate_card_band"),
        sa.CheckConstraint("exp_max > exp_min", name="ck_rate_card_band_order"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("customer_rate_cards"):
        op.drop_table("customer_rate_cards")
