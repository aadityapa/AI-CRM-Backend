"""Per-event email templates — wording becomes admin data, not code.

Event ROUTING (who gets it) has been admin-editable since notification_routes
landed; the WORDING still lived in Python, so every "can we rephrase this
mail" was a deploy. Two nullable columns close that gap: a subject template
and a body template per event, with placeholders filled at queue time. NULL
means "use the code-composed text exactly as before" — so nothing changes
until an admin deliberately writes a template.

Revision ID: 0071
Revises: 0070
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("notification_routes"):
        return
    columns = {c["name"] for c in inspector.get_columns("notification_routes")}
    if "subject_template" not in columns:
        op.add_column("notification_routes",
                      sa.Column("subject_template", sa.String(length=255), nullable=True))
    if "body_template" not in columns:
        op.add_column("notification_routes",
                      sa.Column("body_template", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("notification_routes"):
        return
    columns = {c["name"] for c in inspector.get_columns("notification_routes")}
    if "body_template" in columns:
        op.drop_column("notification_routes", "body_template")
    if "subject_template" in columns:
        op.drop_column("notification_routes", "subject_template")
