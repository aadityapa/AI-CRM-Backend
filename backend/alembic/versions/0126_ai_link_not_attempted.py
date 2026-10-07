"""`ai_interview_links.not_attempted` — "never happened" is not "failed" (7 Oct 2026).

A candidate who opened the AI L1 link late, lost the connection or never
answered a scored question gets a report that says "Not Attempted"
(`services/interview_outcome`), but the CRM link only knew Pending / Passed /
Failed, so every screen printed "Failed" and TA had no way to tell "could not
sit it" from "sat it and failed" — nor a button to send a fresh link.

`result` is deliberately left as "Failed" (every existing reader keeps working);
this flag is what the rows, the derived status and the reschedule button read.
Existing rows are backfilled by the next report save (the sync is idempotent on
score + verdict + this flag), not here — the report store is a different database.

Revision ID: 0126
Revises: 0125
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_interview_links",
        sa.Column("not_attempted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("ai_interview_links", "not_attempted")
