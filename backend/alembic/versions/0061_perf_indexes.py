"""Indexes for the queries that run on every list request.

Two tables hold ~90% of the rows (candidates 6.7k, candidate_profiles 8k) and
neither had an index supporting its default list shape or its sorts.

The candidate search is the worst offender: it is a leading-wildcard ILIKE over a
concatenated expression, which no btree can serve. pg_trgm + GIN fixes it, and
the index expression must match services/candidates.candidate_full_name_expr()
exactly or Postgres will not use it.

Revision ID: 0061
Revises: 0060
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

#: Must match candidate_full_name_expr() in services/candidates.py.
_FULL_NAME_SQL = (
    "(coalesce(first_name,'') || ' ' || coalesce(middle_name,'') "
    "|| ' ' || coalesce(last_name,''))"
)


def upgrade() -> None:
    conn = op.get_bind()

    # --- trigram search -------------------------------------------------
    # Requires CREATE EXTENSION privileges. If the role cannot, the searches
    # still work (sequential scan) — so warn rather than fail the migration.
    try:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        has_trgm = True
    except Exception:  # noqa: BLE001 - non-fatal, see above
        has_trgm = False

    if has_trgm:
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_candidates_fullname_trgm "
            f"ON candidates USING gin ({_FULL_NAME_SQL} gin_trgm_ops)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_candidates_email_trgm "
            "ON candidates USING gin (email gin_trgm_ops)"
        ))
        conn.execute(sa.text(
            "CREATE INDEX IF NOT EXISTS ix_candidates_phone_trgm "
            "ON candidates USING gin (phone gin_trgm_ops)"
        ))

    # --- candidates -----------------------------------------------------
    # The unique index on email is case-SENSITIVE, so the duplicate check
    # (lower(email) == ...) could not use it.
    conn.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_candidates_email_lower "
        "ON candidates (lower(email))"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_candidates_has_cv "
        "ON candidates (id) WHERE cv_url IS NOT NULL AND cv_url <> ''"
    ))

    # --- candidate_profiles --------------------------------------------
    # Covers the default list: WHERE is_hidden = false [AND pipeline_status = ?]
    # ORDER BY id DESC.
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_cp_hidden_status_id "
        "ON candidate_profiles (is_hidden, pipeline_status, id DESC)"
    ))
    for column in ("applied_on", "created_at", "customer_submission_date",
                   "customer_onboarding_date"):
        conn.execute(sa.text(
            f"CREATE INDEX IF NOT EXISTS ix_cp_{column} "
            f"ON candidate_profiles ({column} DESC NULLS LAST, id DESC)"
        ))

    # --- interview_events ----------------------------------------------
    # candidate_id is joined by the dashboards and the delete cascade.
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_interview_events_candidate_id "
        "ON interview_events (candidate_id)"
    ))
    # The per-profile "latest round" lookup orders by scheduled_at within a profile.
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_interview_events_profile_scheduled "
        "ON interview_events (profile_id, scheduled_at DESC NULLS LAST)"
    ))

    # --- requirements ---------------------------------------------------
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_requirements_created_at "
        "ON requirements (created_at DESC, id DESC)"
    ))
    conn.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS ix_requirements_status "
        "ON requirements (status)"
    ))


def downgrade() -> None:
    conn = op.get_bind()
    for name in (
        "ix_requirements_status", "ix_requirements_created_at",
        "ix_interview_events_profile_scheduled", "ix_interview_events_candidate_id",
        "ix_cp_customer_onboarding_date", "ix_cp_customer_submission_date",
        "ix_cp_created_at", "ix_cp_applied_on", "ix_cp_hidden_status_id",
        "ix_candidates_has_cv", "uq_candidates_email_lower",
        "ix_candidates_phone_trgm", "ix_candidates_email_trgm",
        "ix_candidates_fullname_trgm",
    ):
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))
