"""Project policy: recurring_billing, BillingFrequency Quarterly/Yearly,
project_leave_policies table, seed leave_policy_types.

Revision ID: 0038
Revises: 0037
"""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

# Canonical leave-type master rows for Edit Project §2 Leave Name dropdown.
_SEED_LEAVE_TYPES = (
    "Casual Leave",
    "Comp-Off",
    "Earned Leave",
    "Loss of Pay",
    "Maternity Leave",
    "Paid Leave",
    "Paternity Leave",
    "Sick Leave",
)


def upgrade() -> None:
    bind = op.get_bind()

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction on PostgreSQL.
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE billing_frequency ADD VALUE IF NOT EXISTS 'Quarterly'")
            op.execute("ALTER TYPE billing_frequency ADD VALUE IF NOT EXISTS 'Yearly'")

    op.add_column(
        "projects",
        sa.Column(
            "recurring_billing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )

    op.create_table(
        "project_leave_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("leave_type_id", sa.Integer(),
                  sa.ForeignKey("leave_policy_types.id"), nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("leave_credit_type", sa.String(40), nullable=False,
                  server_default="Monthly"),
        sa.Column("leave_credit_balance", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("initial_credit_balance", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("leave_expire", sa.String(40), nullable=False,
                  server_default="Annually"),
        sa.Column("is_max_limit", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("maximum_carry_forward", sa.Numeric(5, 2), nullable=False,
                  server_default="0"),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.UniqueConstraint("project_id", "leave_type_id",
                            name="uq_project_leave_policy"),
    )

    # Seed leave-type masters (idempotent — skip names that already exist).
    for name in _SEED_LEAVE_TYPES:
        op.execute(
            sa.text(
                "INSERT INTO leave_policy_types (name) "
                "SELECT :name WHERE NOT EXISTS ("
                "  SELECT 1 FROM leave_policy_types WHERE name = :name"
                ")"
            ).bindparams(name=name)
        )


def downgrade() -> None:
    op.drop_table("project_leave_policies")
    op.drop_column("projects", "recurring_billing")
    # PostgreSQL cannot remove enum values safely; leave Quarterly/Yearly in place.
