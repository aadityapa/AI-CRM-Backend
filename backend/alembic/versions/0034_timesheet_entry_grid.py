"""Timesheet entry grid: day_type, Week_Off attendance, view flag, split project.

Revision ID: 0034
Revises: 0033
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

DAY_TYPE = sa.Enum("Working", "Week_Off", "Holiday", name="day_type")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE attendance_status ADD VALUE IF NOT EXISTS 'Week_Off'")

    DAY_TYPE.create(bind, checkfirst=True)
    op.add_column(
        "timesheet_entries",
        sa.Column(
            "day_type",
            DAY_TYPE,
            nullable=False,
            server_default="Working",
        ),
    )
    op.add_column(
        "timesheet_entries",
        sa.Column("view_flag", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "timesheet_entries",
        sa.Column("entry_project_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_timesheet_entries_entry_project_id",
        "timesheet_entries",
        "projects",
        ["entry_project_id"],
        ["id"],
    )
    op.create_index(
        "ix_timesheet_entries_entry_project_id",
        "timesheet_entries",
        ["entry_project_id"],
    )

    # Back-fill day_type from existing rows (cast required for Postgres enums).
    op.execute(
        """
        UPDATE timesheet_entries
        SET day_type = (
            CASE
                WHEN attendance_status::text = 'Holiday' THEN 'Holiday'
                WHEN is_working = FALSE THEN 'Week_Off'
                ELSE 'Working'
            END
        )::day_type
        """
    )


def downgrade() -> None:
    op.drop_index("ix_timesheet_entries_entry_project_id", table_name="timesheet_entries")
    op.drop_constraint("fk_timesheet_entries_entry_project_id", "timesheet_entries", type_="foreignkey")
    op.drop_column("timesheet_entries", "entry_project_id")
    op.drop_column("timesheet_entries", "view_flag")
    op.drop_column("timesheet_entries", "day_type")
    bind = op.get_bind()
    DAY_TYPE.drop(bind, checkfirst=True)
