"""requirement_position_requests — Sales asks to change a requirement's
headcount, RMG (or Admin/CEO) approves (21 Sep 2026, user request).

Revision ID: 0104
Revises: 0103
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("requirement_position_requests"):
        return
    op.create_table(
        "requirement_position_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requirement_id", sa.Integer(),
                  sa.ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="Pending", index=True),
        sa.Column("from_positions", sa.Integer(), nullable=False),
        sa.Column("to_positions", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status_before", sa.String(60), nullable=True),
        sa.Column("status_after", sa.String(60), nullable=True),
        sa.Column("joined_at_decision", sa.Integer(), nullable=True),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("registration_data.id"),
                  nullable=True, index=True),
        sa.Column("requested_by_name", sa.String(255), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("decided_by_name", sa.String(255), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
    )
    # One pending request per requirement, enforced by the DB rather than by a
    # read-then-write race in the router (Postgres partial index).
    if bind.dialect.name == "postgresql":
        op.create_index(
            "uq_req_position_request_one_pending",
            "requirement_position_requests", ["requirement_id"],
            unique=True, postgresql_where=sa.text("status = 'Pending'"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("uq_req_position_request_one_pending",
                      table_name="requirement_position_requests")
    op.drop_table("requirement_position_requests")
