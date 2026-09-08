"""Holiday names master, observance, branch calendar FK on holidays.

Revision ID: 0035
Revises: 0034
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

SEED_HOLIDAY_NAMES = (
    "Annual Shutdown",
    "Ayudha Pooja",
    "Bakrid",
    "Bridge Holiday",
    "Christmas",
    "Diwali",
    "New Year",
    "Independence Day",
    "Republic Day",
    "Gandhi Jayanti",
    "Good Friday",
    "Holi",
    "Maha Shivaratri",
    "Ram Navami",
    "Easter",
    "Pongal",
    "Onam",
    "Vishu",
    "Dussehra",
    "Milad-un-Nabi",
)


def upgrade() -> None:
    op.create_table(
        "holiday_names",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_holiday_names_name"),
    )

    names_table = sa.table(
        "holiday_names",
        sa.column("name", sa.String),
        sa.column("is_active", sa.Boolean),
    )
    op.bulk_insert(
        names_table,
        [{"name": n, "is_active": True} for n in SEED_HOLIDAY_NAMES],
    )

    op.add_column(
        "holidays",
        sa.Column("holiday_name_id", sa.Integer(), sa.ForeignKey("holiday_names.id"), nullable=True),
    )
    op.add_column(
        "holidays",
        sa.Column(
            "observance",
            sa.String(16),
            nullable=False,
            server_default="Mandatory",
        ),
    )
    op.add_column(
        "holidays",
        sa.Column(
            "holiday_calendar_id",
            sa.Integer(),
            sa.ForeignKey("branch_holiday_years.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_holidays_holiday_name_id", "holidays", ["holiday_name_id"])
    op.create_index("ix_holidays_holiday_calendar_id", "holidays", ["holiday_calendar_id"])


def downgrade() -> None:
    op.drop_index("ix_holidays_holiday_calendar_id", table_name="holidays")
    op.drop_index("ix_holidays_holiday_name_id", table_name="holidays")
    op.drop_column("holidays", "holiday_calendar_id")
    op.drop_column("holidays", "observance")
    op.drop_column("holidays", "holiday_name_id")
    op.drop_table("holiday_names")
