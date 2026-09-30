"""Projects get an end date, and closing one moves its team to the bench (25 Sep 2026).

Reported: a project that has ended had nowhere to record WHEN it ended, and its
people stayed "deployed" on it forever. A project can now be closed from its
page with an end date and a reason:

* `projects.end_date` — the last working day on the project. A FUTURE date is a
  scheduled close: the team keeps working and billing until then, and every
  open assignment's `exit_date` is capped at that day so timesheets, the
  revenue forecast and the roll-off radar already see the end coming.
* `projects.closure_capped_exits` (JSON {pe_id: previous exit date | null}) —
  the exit dates the close overwrote, so cancelling a scheduled close puts
  every one of them back exactly (an earlier personal plan is never lost).
* `projects.closed_reason / closed_at / closed_by` — who closed it and why.
  `closed_at` stays NULL while the close is only scheduled; the daily job
  (`scheduler.project_closures`) or the request itself (a date today or in
  the past) stamps it when the team is actually exited.

No backfill: projects already marked Completed keep a NULL end date — the
system never invents a date nobody entered.

Revision ID: 0109
Revises: 0108
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None

_TABLE = "projects"


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    have = _columns(op.get_bind(), _TABLE)
    if "end_date" not in have:
        op.add_column(_TABLE, sa.Column("end_date", sa.Date(), nullable=True))
        op.create_index("ix_projects_end_date", _TABLE, ["end_date"])
    if "closure_capped_exits" not in have:
        op.add_column(_TABLE, sa.Column("closure_capped_exits",
                                        postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    if "closed_reason" not in have:
        op.add_column(_TABLE, sa.Column("closed_reason", sa.Text(), nullable=True))
    if "closed_at" not in have:
        op.add_column(_TABLE, sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    if "closed_by" not in have:
        op.add_column(_TABLE, sa.Column("closed_by", sa.Integer(),
                                        sa.ForeignKey("registration_data.id"), nullable=True))


def downgrade() -> None:
    have = _columns(op.get_bind(), _TABLE)
    for col in ("closed_by", "closed_at", "closed_reason", "closure_capped_exits"):
        if col in have:
            op.drop_column(_TABLE, col)
    if "end_date" in have:
        op.drop_index("ix_projects_end_date", table_name=_TABLE)
        op.drop_column(_TABLE, "end_date")
