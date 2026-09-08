"""purchase_orders.employee_id — which employee this PO is raised for.

User request 26 Aug 2026: the New PO form gets an Employee field (picked from
the Employees master by name search) so Finance can see whose engagement a
purchase order funds. Nullable — existing POs and POs not tied to one person
stay valid.

Revision ID: 0086
Revises: 0085
"""
from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "purchase_orders",
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id"), nullable=True),
    )
    op.create_index("ix_purchase_orders_employee_id", "purchase_orders", ["employee_id"])


def downgrade() -> None:
    op.drop_index("ix_purchase_orders_employee_id", table_name="purchase_orders")
    op.drop_column("purchase_orders", "employee_id")
