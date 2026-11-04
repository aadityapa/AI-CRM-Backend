"""requirements.careers_hidden — RMG's switch for the public careers page (3 Nov 2026).

Every position open for sourcing is listed on Karnex Careers by itself
(services/careers.py). This column lets RMG take one off the page without
changing the position's status. Default FALSE = listed, so nothing changes on
deploy.

Revision ID: 0151
Revises: 0150
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("requirements", sa.Column("careers_hidden", sa.Boolean(), nullable=False,
                                            server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("requirements", "careers_hidden")
