"""Resume file integrity: SHA-256 checksum + size for dedupe/verify.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("file_sha256", sa.String(length=64), nullable=True))
    op.add_column("resumes", sa.Column("file_size", sa.Integer(), nullable=True))
    op.create_index("ix_resumes_file_sha256", "resumes", ["file_sha256"])
    # Helps the dedupe lookup on (requirement_id, file_sha256).
    op.create_index("ix_resumes_req_sha", "resumes", ["requirement_id", "file_sha256"])


def downgrade() -> None:
    op.drop_index("ix_resumes_req_sha", table_name="resumes")
    op.drop_index("ix_resumes_file_sha256", table_name="resumes")
    op.drop_column("resumes", "file_size")
    op.drop_column("resumes", "file_sha256")
