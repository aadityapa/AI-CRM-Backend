"""customer_rate_cards.branch_id — rate cards are branch-wise now.

A customer's branches can quote different rates (metro vs non-metro, entity
differences), so the card moved from customer level to branch level. NULL
branch_id = a customer-wide default; the Opportunity form prefers the selected
branch's rows and falls back to NULL rows, so pre-0077 cards keep working
untouched. The band-uniqueness constraint widens to include the branch.

Revision ID: 0077
Revises: 0076
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("customer_rate_cards"):
        return
    columns = {c["name"] for c in inspector.get_columns("customer_rate_cards")}
    if "branch_id" not in columns:
        op.add_column(
            "customer_rate_cards",
            sa.Column("branch_id", sa.Integer(),
                      sa.ForeignKey("customer_branches.id"), nullable=True),
        )
        op.create_index("ix_customer_rate_cards_branch_id",
                        "customer_rate_cards", ["branch_id"])
    uqs = {u["name"] for u in inspector.get_unique_constraints("customer_rate_cards")}
    if "uq_rate_card_band" in uqs:
        op.drop_constraint("uq_rate_card_band", "customer_rate_cards", type_="unique")
    op.create_unique_constraint(
        "uq_rate_card_band", "customer_rate_cards",
        ["customer_id", "branch_id", "exp_min", "exp_max"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("customer_rate_cards"):
        return
    uqs = {u["name"] for u in inspector.get_unique_constraints("customer_rate_cards")}
    if "uq_rate_card_band" in uqs:
        op.drop_constraint("uq_rate_card_band", "customer_rate_cards", type_="unique")
    columns = {c["name"] for c in inspector.get_columns("customer_rate_cards")}
    if "branch_id" in columns:
        op.drop_index("ix_customer_rate_cards_branch_id", "customer_rate_cards")
        op.drop_column("customer_rate_cards", "branch_id")
    op.create_unique_constraint(
        "uq_rate_card_band", "customer_rate_cards",
        ["customer_id", "exp_min", "exp_max"],
    )
