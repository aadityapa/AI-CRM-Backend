"""New Map Employee wizard vocabulary: Off-Shore location, Yearly billing unit.

The form's Location dropdown offers On Site / Off-Shore / Remote. "On Site" and
"Remote" already exist as work_mode values ('Onsite', 'Remote'); 'Off-Shore' is
new. Unit gains 'Yearly' (labelled "Per Year" in the UI) alongside
Hourly/Daily/Monthly. 'Hybrid' stays valid for rows that already use it.

ADD VALUE is append-only and idempotent (IF NOT EXISTS), so this migration is
safe to re-run and never touches existing rows.

Revision ID: 0065
Revises: 0064
"""
from __future__ import annotations

from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE work_mode ADD VALUE IF NOT EXISTS 'Off-Shore'")
    op.execute("ALTER TYPE billing_unit ADD VALUE IF NOT EXISTS 'Yearly'")


def downgrade() -> None:
    # Postgres cannot drop a single enum value without rebuilding the type and
    # every column that uses it. The added values are harmless if unused, so
    # downgrade deliberately leaves them in place.
    pass
