"""Interview-template request workflow table (TA -> RMG -> TA).

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-09
"""
from alembic import op

from models.template_requests import TemplateRequest

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Creates the table + its pg enum type (SQLAlchemy handles the enum on create).
    TemplateRequest.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    TemplateRequest.__table__.drop(bind=bind, checkfirst=True)
    # Drop the enum type created alongside the table.
    op.execute("DROP TYPE IF EXISTS template_request_status")
