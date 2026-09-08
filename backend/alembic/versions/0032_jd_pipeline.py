"""Customer JD kind on opportunity_attachments + RMG JD on requirements.

- opportunity_attachments.kind ('customer_jd' | 'general')
- requirements.rmg_jd_text
- requirement_attachments table (RMG JD files)

Revision ID: 0032
Revises: 0031
Create Date: 2026-07-17
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "opportunity_attachments",
        sa.Column("kind", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_opportunity_attachments_kind", "opportunity_attachments", ["kind"])
    op.execute("UPDATE opportunity_attachments SET kind = 'general' WHERE kind IS NULL")

    op.add_column("requirements", sa.Column("rmg_jd_text", sa.Text(), nullable=True))

    op.create_table(
        "requirement_attachments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("requirements.id"), nullable=False),
        sa.Column("file_url", sa.String(length=1024), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_requirement_attachments_requirement_id", "requirement_attachments", ["requirement_id"])
    op.create_index("ix_requirement_attachments_kind", "requirement_attachments", ["kind"])
    op.create_index("ix_requirement_attachments_file_sha256", "requirement_attachments", ["file_sha256"])


def downgrade() -> None:
    op.drop_index("ix_requirement_attachments_file_sha256", table_name="requirement_attachments")
    op.drop_index("ix_requirement_attachments_kind", table_name="requirement_attachments")
    op.drop_index("ix_requirement_attachments_requirement_id", table_name="requirement_attachments")
    op.drop_table("requirement_attachments")
    op.drop_column("requirements", "rmg_jd_text")
    op.drop_index("ix_opportunity_attachments_kind", table_name="opportunity_attachments")
    op.drop_column("opportunity_attachments", "kind")
