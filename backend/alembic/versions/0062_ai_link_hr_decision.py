"""Carry the recruiter's interview decision into the CRM.

The interview report page lets a recruiter press Shortlist / On Hold / Reject.
That decision was written only to the legacy auth DB (hr_candidate_decisions and
interview_records.payload.hr_interview_status). The CRM reads a different
database entirely, so its "Result" column kept showing the AI's score-threshold
verdict — a candidate marked Selected still read "Failed · 57.2%".

These columns give the CRM somewhere to store the override. `result` keeps the
AI verdict untouched: both are facts and the audit trail needs both.

Revision ID: 0062
Revises: 0061
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    return column in {c["name"] for c in sa.inspect(conn).get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("ai_interview_links"):
        return

    for name, col in (
        ("hr_decision", sa.Column("hr_decision", sa.String(16), nullable=True)),
        ("hr_decision_by", sa.Column("hr_decision_by", sa.String(255), nullable=True)),
        ("hr_decision_at", sa.Column("hr_decision_at", sa.DateTime(timezone=True), nullable=True)),
    ):
        if not _has_column(conn, "ai_interview_links", name):
            op.add_column("ai_interview_links", col)

    # The profile page filters "which interviews has a human ruled on"; without
    # this it is a seq scan over every link row.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_ai_links_hr_decision "
        "ON ai_interview_links (hr_decision) WHERE hr_decision IS NOT NULL"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("ai_interview_links"):
        return
    op.execute("DROP INDEX IF EXISTS ix_ai_links_hr_decision")
    for name in ("hr_decision_at", "hr_decision_by", "hr_decision"):
        if _has_column(conn, "ai_interview_links", name):
            op.drop_column("ai_interview_links", name)
