"""candidates.created_by_id / created_by_name (+ index on the existing created_at).

Candidates tab TA + date filters (11 Sep 2026, user request): "who added this
candidate, and when" was never stored on the candidate — only on the profile
it later got. Backfilled from the candidate's EARLIEST profile (its TA owner
and applied date), else the Zoho source date, else now.

Revision ID: 0101
Revises: 0100
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # candidates.created_at already exists (TimestampMixin, since 0001) — only
    # the attribution columns are new. IF NOT EXISTS so a half-applied run
    # (this revision first shipped trying to re-add created_at) can be re-run.
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS created_by_id INTEGER")
    op.execute("ALTER TABLE candidates ADD COLUMN IF NOT EXISTS created_by_name VARCHAR(255)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_candidates_created_at ON candidates (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_candidates_created_by_id ON candidates (created_by_id)")
    op.execute("""
        UPDATE candidates c SET
            created_by_id   = p.ta_owner_id,
            created_by_name = p.ta_owner_name,
            created_at      = COALESCE(p.applied_on, p.created_at, c.source_created_date::timestamptz, c.created_at)
        FROM (
            SELECT DISTINCT ON (candidate_id) candidate_id, ta_owner_id, ta_owner_name, applied_on, created_at
            FROM candidate_profiles
            ORDER BY candidate_id, COALESCE(applied_on, created_at) ASC, id ASC
        ) p
        WHERE p.candidate_id = c.id
    """)
    op.execute("""
        UPDATE candidates SET created_at = source_created_date::timestamptz
        WHERE source_created_date IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM candidate_profiles cp WHERE cp.candidate_id = candidates.id)
    """)


def downgrade() -> None:
    op.drop_index("ix_candidates_created_by_id", table_name="candidates")
    op.drop_index("ix_candidates_created_at", table_name="candidates")
    op.drop_column("candidates", "created_by_name")
    op.drop_column("candidates", "created_by_id")
