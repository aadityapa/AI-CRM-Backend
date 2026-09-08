"""resumes.possible_duplicate_of + resume_parse_cache — bulk upload hardening.

Two pieces of the 20 Aug 2026 bulk-ZIP work:

* ``resumes.possible_duplicate_of`` — the existing candidate a bulk-uploaded
  resume matched on email/phone. Persisting it means the "held for review"
  queue survives the results dialog: the Resumes tab can badge and filter
  these rows any time, instead of the information dying with the popup.
  Cleared when the TA resolves the hold (apply-duplicate endpoint).

* ``resume_parse_cache`` — AI extraction results keyed by the file's SHA-256.
  TAs recycle the same CVs across opportunities constantly; a repeat file must
  never pay for a second model call. The sha is already computed for the
  integrity/dedupe check, so the cache key is free.

Revision ID: 0081
Revises: 0080
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("resumes"):
        columns = {c["name"] for c in inspector.get_columns("resumes")}
        if "possible_duplicate_of" not in columns:
            op.add_column(
                "resumes",
                sa.Column("possible_duplicate_of", sa.Integer(),
                          sa.ForeignKey("candidates.id"), nullable=True),
            )
            op.create_index("ix_resumes_possible_duplicate_of", "resumes",
                            ["possible_duplicate_of"])

    if not inspector.has_table("resume_parse_cache"):
        op.create_table(
            "resume_parse_cache",
            sa.Column("file_sha256", sa.String(64), primary_key=True),
            sa.Column("parsed", JSONB, nullable=False),
            sa.Column("model", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True),
                      server_default=sa.func.now(), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("resume_parse_cache"):
        op.drop_table("resume_parse_cache")
    if inspector.has_table("resumes"):
        columns = {c["name"] for c in inspector.get_columns("resumes")}
        if "possible_duplicate_of" in columns:
            op.drop_index("ix_resumes_possible_duplicate_of", table_name="resumes")
            op.drop_column("resumes", "possible_duplicate_of")
