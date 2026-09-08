"""notification_routes.label / description — admin-created email drafts.

User request 3 Sep 2026: "if I want any another email in Email Drafts I also
do". A draft the admin creates themselves (an offer-letter cover note, a
document-request mail, a rejection note…) is a `notification_routes` row keyed
`custom.<slug>`: it already has the subject/body columns and the audit stamp,
it only lacked a human name. Both NULL for every code-defined event (their
label lives in `routers/crm/email_flows.EVENTS`).

Custom drafts are for MANUAL sending — the composer on the Emails tab and the
candidate Email buttons offer them as "Use a draft"; nothing fires them
automatically, so `roles`/`enabled` stay unused for them.

Revision ID: 0093
Revises: 0092
"""
from alembic import op
import sqlalchemy as sa

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notification_routes", sa.Column("label", sa.String(160), nullable=True))
    op.add_column("notification_routes", sa.Column("description", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("notification_routes", "description")
    op.drop_column("notification_routes", "label")
