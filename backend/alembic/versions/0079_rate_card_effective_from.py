"""customer_rate_cards.effective_from — versioned CTC slab ladders.

A branch's slab is now entered as a whole LADDER with an effective date.
Entering a new ladder does not delete the old one: the version with the
latest effective_from on-or-before today wins, older versions read as expired
history, and a future-dated ladder waits its turn. NULL = legacy rows,
effective since forever. Band-uniqueness widens to include the version.

Revision ID: 0079
Revises: 0078
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("customer_rate_cards"):
        return
    columns = {c["name"] for c in inspector.get_columns("customer_rate_cards")}
    if "effective_from" not in columns:
        op.add_column("customer_rate_cards",
                      sa.Column("effective_from", sa.Date(), nullable=True))
    uqs = {u["name"] for u in inspector.get_unique_constraints("customer_rate_cards")}
    if "uq_rate_card_band" in uqs:
        op.drop_constraint("uq_rate_card_band", "customer_rate_cards", type_="unique")
    op.create_unique_constraint(
        "uq_rate_card_band", "customer_rate_cards",
        ["customer_id", "branch_id", "effective_from", "exp_min", "exp_max"],
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
    if "effective_from" in columns:
        op.drop_column("customer_rate_cards", "effective_from")
    op.create_unique_constraint(
        "uq_rate_card_band", "customer_rate_cards",
        ["customer_id", "branch_id", "exp_min", "exp_max"],
    )
