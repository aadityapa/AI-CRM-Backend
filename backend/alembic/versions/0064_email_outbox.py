"""Durable outbox for outbound notification email.

Before this, the only emails the system sent went to candidates, inline, on the
request thread, with no record of whether they arrived. Staff notifications
existed only as bell rows in `notifications`, which an employee on leave or a
contractor with no login account could never see.

`email_outbox` is written in the same transaction as the business change and
drained by a background worker, so a submit that rolls back never mails anyone,
and a transient O365 failure is retried rather than swallowed.

Revision ID: 0064
Revises: 0063
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None

ENUM_NAME = "email_outbox_status"
STATUSES = ("Queued", "Sent", "Failed", "Skipped")

#: create_type=False is load-bearing. A plain sa.Enum on a create_table column
#: makes SQLAlchemy emit its own CREATE TYPE as part of the table DDL, which
#: collides with the explicit create() below:
#:     DuplicateObject: type "email_outbox_status" already exists
#: So the type is created once, explicitly and idempotently, and the column just
#: references it.
status_enum = postgresql.ENUM(*STATUSES, name=ENUM_NAME, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()

    # Idempotent: survives a re-run after a partially applied attempt.
    postgresql.ENUM(*STATUSES, name=ENUM_NAME).create(bind, checkfirst=True)

    if sa.inspect(bind).has_table("email_outbox"):
        return

    op.create_table(
        "email_outbox",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("to_email", sa.String(length=320), nullable=False),
        sa.Column("to_name", sa.String(length=255), nullable=True),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("from_name", sa.String(length=255), nullable=True),
        sa.Column("reply_to_email", sa.String(length=320), nullable=True),
        sa.Column("reply_to_name", sa.String(length=255), nullable=True),
        sa.Column("status", status_enum, nullable=False, server_default="Queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(length=255), nullable=True),
        sa.Column("related_type", sa.String(length=48), nullable=True),
        sa.Column("related_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("dedupe_key", name="uq_email_outbox_dedupe_key"),
    )
    op.create_index("ix_email_outbox_event", "email_outbox", ["event"])
    op.create_index("ix_email_outbox_status", "email_outbox", ["status"])
    op.create_index("ix_email_outbox_next_attempt_at", "email_outbox", ["next_attempt_at"])
    # The worker's hot query: due, unsent rows, oldest first.
    op.create_index("ix_email_outbox_pending", "email_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("email_outbox"):
        op.drop_index("ix_email_outbox_pending", table_name="email_outbox")
        op.drop_index("ix_email_outbox_next_attempt_at", table_name="email_outbox")
        op.drop_index("ix_email_outbox_status", table_name="email_outbox")
        op.drop_index("ix_email_outbox_event", table_name="email_outbox")
        op.drop_table("email_outbox")
    postgresql.ENUM(name=ENUM_NAME).drop(bind, checkfirst=True)
