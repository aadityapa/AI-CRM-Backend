"""Indexes for the bell's notification feed (7 Oct 2026).

The bell polls every 30 s from every open tab. Its two reads were served by the
single-column ``user_id`` index plus a sort / filter over every row the user
ever received:

* ``ix_notifications_user_feed`` (user_id, created_at DESC, id DESC) — the
  newest-first page and the ``since_id`` poll read straight off the index.
* ``ix_notifications_user_unread`` (user_id) WHERE NOT is_read — the unread
  badge count touches only unread rows.

``IF NOT EXISTS`` so a re-run (or a hand-made index of the same name) is a no-op.

Revision ID: 0122
Revises: 0121
"""
from __future__ import annotations

from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_feed "
        "ON notifications (user_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_user_unread "
        "ON notifications (user_id) WHERE is_read = false"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_user_unread")
    op.execute("DROP INDEX IF EXISTS ix_notifications_user_feed")
