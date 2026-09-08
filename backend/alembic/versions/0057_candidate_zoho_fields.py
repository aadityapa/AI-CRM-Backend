"""candidates: durable Zoho id + the fields the NEXUS candidates export carries.

`zoho_candidate_id` is the important one — it lets the applied-opportunities
import resolve a candidate directly instead of bridging Zoho id -> CSV -> email,
and makes re-imports idempotent even when someone edits an email in the app.

Revision ID: 0057
Revises: 0056
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidates", sa.Column("zoho_candidate_id", sa.String(32), nullable=True))
    # Partial unique: many candidates created in the app have no Zoho id at all.
    op.create_index(
        "uq_candidates_zoho_candidate_id",
        "candidates",
        ["zoho_candidate_id"],
        unique=True,
        postgresql_where=sa.text("zoho_candidate_id IS NOT NULL"),
    )
    op.add_column("candidates", sa.Column("city", sa.String(120), nullable=True))
    # Full preferred-location list; preferred_location_id keeps the primary one so
    # existing filters and joins are unaffected.
    op.add_column("candidates", sa.Column("preferred_locations", sa.String(500), nullable=True))
    op.add_column("candidates", sa.Column("recruiter_email", sa.String(255), nullable=True))
    op.add_column("candidates", sa.Column("cv_original_filename", sa.String(255), nullable=True))
    op.add_column("candidates", sa.Column("source_created_date", sa.Date(), nullable=True))


def downgrade() -> None:
    for col in ("source_created_date", "cv_original_filename", "recruiter_email",
                "preferred_locations", "city"):
        op.drop_column("candidates", col)
    op.drop_index("uq_candidates_zoho_candidate_id", table_name="candidates")
    op.drop_column("candidates", "zoho_candidate_id")
