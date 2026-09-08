"""Branch-wise Leave & Holiday Policy — schema (spec sections 2, 5, 6).

- §2: branch_holiday_years header (per branch, per calendar year, IsFreeze).
      Holiday Count is DERIVED from holidays (branch_id + year), not stored.
- §5: customer_leave_policies.leave_expire boolean -> string dropdown
      ("Days"; NULL = no expiry). Also newly accepts leave_credit_timing
      value "Start_of_Month" (no DB change — free String column).
- §6: Project branch-policy override columns (NULL = inherit the branch default).

Revision ID: 0028
Revises: 0025
"""
from alembic import op
import sqlalchemy as sa

revision = "0028"
# 0026/0027 were planned but never landed in this tree; chain continues from 0025.
down_revision = "0025"
branch_labels = None
depends_on = None


_PROJECT_OVERRIDES = [
    ("holidays_billable", sa.Boolean()),
    ("weekoff_billable", sa.Boolean()),
    ("leave_billable", sa.Boolean()),
    ("comp_off_billable", sa.Boolean()),
    ("hours_required_half_day", sa.Numeric(4, 2)),
    ("hours_required_full_day", sa.Numeric(4, 2)),
    ("hours_required_half_day_comp_off", sa.Numeric(4, 2)),
    ("hours_required_full_day_comp_off", sa.Numeric(4, 2)),
    ("working_hours_per_day", sa.Numeric(4, 2)),
    ("is_max_billable_hours_per_day", sa.Boolean()),
    ("is_max_billable_hours_per_month", sa.Boolean()),
    ("is_max_billable_days_per_month", sa.Boolean()),
    ("is_initial_no_billing_period", sa.Boolean()),
    ("initial_no_billing_qty", sa.Integer()),
    ("initial_no_billing_period", sa.String(length=40)),
]


def upgrade() -> None:
    # ---- §2 branch holiday-year header ----
    op.create_table(
        "branch_holiday_years",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("branch_id", sa.Integer(),
                  sa.ForeignKey("customer_branches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("calendar_year", sa.Integer(), nullable=False),
        sa.Column("is_freeze", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("branch_id", "calendar_year", name="uq_branch_holiday_year"),
    )
    op.create_index("ix_branch_holiday_years_branch_id", "branch_holiday_years", ["branch_id"])

    # ---- §5 leave_expire boolean -> string ("Days" / NULL) ----
    op.alter_column(
        "customer_leave_policies", "leave_expire",
        existing_type=sa.Boolean(),
        type_=sa.String(length=24),
        existing_nullable=False,
        nullable=True,
        existing_server_default=sa.false(),
        server_default=None,
        postgresql_using="CASE WHEN leave_expire THEN 'Days' ELSE NULL END",
    )

    # ---- §6 project override columns ----
    for name, coltype in _PROJECT_OVERRIDES:
        op.add_column("projects", sa.Column(name, coltype, nullable=True))


def downgrade() -> None:
    for name, _ in reversed(_PROJECT_OVERRIDES):
        op.drop_column("projects", name)

    op.alter_column(
        "customer_leave_policies", "leave_expire",
        existing_type=sa.String(length=24),
        type_=sa.Boolean(),
        existing_nullable=True,
        nullable=False,
        server_default=sa.false(),
        postgresql_using="CASE WHEN leave_expire IS NOT NULL THEN true ELSE false END",
    )

    op.drop_index("ix_branch_holiday_years_branch_id", table_name="branch_holiday_years")
    op.drop_table("branch_holiday_years")
