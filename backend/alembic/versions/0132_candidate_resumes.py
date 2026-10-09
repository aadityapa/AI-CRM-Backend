"""`candidate_resumes` — one candidate, several resumes (9 Oct 2026).

TA keeps more than one resume per candidate and sees which open positions each
fits (`services/candidate_resumes.py`). The library is seeded here from what is
already on file: the candidate record's CV (primary) and every distinct resume
file uploaded for a position. Re-runnable: the (candidate, file) pair is unique
and the seed inserts only what is missing.

Revision ID: 0132
Revises: 0131
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = postgresql.JSONB() if bind.dialect.name == "postgresql" else sa.JSON()
    op.create_table(
        "candidate_resumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("candidate_id", sa.Integer(), sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_url", sa.String(1024), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=True),
        sa.Column("label", sa.String(120), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("file_sha256", sa.String(64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("ai_reviews", json_type, nullable=True),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=True),
        sa.Column("uploaded_by_name", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("candidate_id", "file_url", name="uq_candidate_resumes_file"),
    )
    op.create_index("ix_candidate_resumes_candidate_id", "candidate_resumes", ["candidate_id"])
    op.create_index("ix_candidate_resumes_file_sha256", "candidate_resumes", ["file_sha256"])
    if bind.dialect.name != "postgresql":
        return
    # The record's CV — the primary version.
    op.execute(sa.text("""
        INSERT INTO candidate_resumes (candidate_id, file_url, original_filename, label, source, is_primary)
        SELECT c.id, c.cv_url, c.cv_original_filename, 'CV on record', 'cv', TRUE
        FROM candidates c
        WHERE COALESCE(TRIM(c.cv_url), '') <> ''
        ON CONFLICT (candidate_id, file_url) DO NOTHING
    """))
    # Every distinct resume uploaded for a position (newest upload of each file).
    op.execute(sa.text("""
        INSERT INTO candidate_resumes (candidate_id, file_url, label, source, file_sha256, file_size, is_primary, created_at)
        SELECT DISTINCT ON (r.candidate_id, r.resume_file_url)
               r.candidate_id, r.resume_file_url,
               LEFT('Uploaded for ' || COALESCE(q.req_number, 'a position'), 120),
               'application', r.file_sha256, r.file_size, FALSE, r.created_at
        FROM resumes r
        LEFT JOIN requirements q ON q.id = r.requirement_id
        WHERE r.candidate_id IS NOT NULL AND COALESCE(TRIM(r.resume_file_url), '') <> ''
        ORDER BY r.candidate_id, r.resume_file_url, r.id DESC
        ON CONFLICT (candidate_id, file_url) DO NOTHING
    """))
    # A candidate with no CV on record: the newest uploaded resume is primary.
    op.execute(sa.text("""
        UPDATE candidate_resumes cr SET is_primary = TRUE
        WHERE cr.id IN (
            SELECT DISTINCT ON (candidate_id) id FROM candidate_resumes
            WHERE candidate_id NOT IN (SELECT candidate_id FROM candidate_resumes WHERE is_primary)
            ORDER BY candidate_id, created_at DESC, id DESC)
    """))


def downgrade() -> None:
    op.drop_index("ix_candidate_resumes_file_sha256", table_name="candidate_resumes")
    op.drop_index("ix_candidate_resumes_candidate_id", table_name="candidate_resumes")
    op.drop_table("candidate_resumes")
