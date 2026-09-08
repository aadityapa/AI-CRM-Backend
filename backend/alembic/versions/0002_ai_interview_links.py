"""AI interview link table (Phase 5 integration).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

from models import Base
from models.ai_links import AiInterviewLink

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    AiInterviewLink.__table__.create(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    AiInterviewLink.__table__.drop(bind=op.get_bind(), checkfirst=True)
