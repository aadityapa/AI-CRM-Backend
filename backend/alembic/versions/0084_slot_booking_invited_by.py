"""slot_bookings.invited_by — who sent the invite gets the confirmation.

Before this, a confirmed slot notified the whole TA role: the TA who actually
sent the invite got the same generic mail as every other TA, and the user saw
"why did neelam get MY candidate's confirmation?" (26 Aug 2026). Now the
sender is stamped on the booking and the confirmation goes to exactly them
(fallback: the resume's uploader, then the TA role for legacy bookings).

Revision ID: 0084
Revises: 0083
"""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "slot_bookings",
        sa.Column("invited_by", sa.Integer(),
                  sa.ForeignKey("registration_data.id"), nullable=True),
    )
    op.create_index("ix_slot_bookings_invited_by", "slot_bookings", ["invited_by"])


def downgrade() -> None:
    op.drop_index("ix_slot_bookings_invited_by", table_name="slot_bookings")
    op.drop_column("slot_bookings", "invited_by")
