"""RMG timesheet reports: created_at, role_title, multi-attachments.

- timesheets.created_at / updated_at (TimestampMixin)
- project_employees.role_title (assignment label)
- timesheet_attachments table; back-fill legacy file_attachment_url rows

Revision ID: 0033
Revises: 0032
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "timesheets",
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "timesheets",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.execute(
        "UPDATE timesheets SET created_at = COALESCE(submitted_at, approved_at, NOW()) "
        "WHERE created_at IS NULL"
    )
    op.execute(
        "UPDATE timesheets SET updated_at = COALESCE(approved_at, submitted_at, created_at, NOW()) "
        "WHERE updated_at IS NULL"
    )

    op.add_column("project_employees", sa.Column("role_title", sa.String(length=255), nullable=True))

    op.create_table(
        "timesheet_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("timesheet_id", sa.Integer(), nullable=False),
        sa.Column("file_url", sa.String(length=1024), nullable=False),
        sa.Column("file_name", sa.String(length=512), nullable=True),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=64), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["timesheet_id"], ["timesheets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["registration_data.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_timesheet_attachments_timesheet_id", "timesheet_attachments", ["timesheet_id"])

    op.execute(
        """
        INSERT INTO timesheet_attachments (timesheet_id, file_url, file_name, uploaded_at)
        SELECT id, file_attachment_url, 'legacy-attachment', COALESCE(submitted_at, created_at, NOW())
        FROM timesheets
        WHERE file_attachment_url IS NOT NULL AND TRIM(file_attachment_url) != ''
        """
    )


def downgrade() -> None:
    op.drop_index("ix_timesheet_attachments_timesheet_id", table_name="timesheet_attachments")
    op.drop_table("timesheet_attachments")
    op.drop_column("project_employees", "role_title")
    op.drop_column("timesheets", "updated_at")
    op.drop_column("timesheets", "created_at")
