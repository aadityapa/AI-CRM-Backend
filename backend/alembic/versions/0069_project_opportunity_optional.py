"""Projects no longer require an opportunity.

Every project used to be born from a won opportunity, so the FK was NOT NULL.
In practice delivery teams also run projects with no sales pipeline behind
them (internal work, direct engagements, migrated legacy projects), and the
form forced people to pick an arbitrary opportunity just to get past the
validator — which is worse than no link at all, because it fabricates a
sales lineage that reports then trust.

Existing rows keep their opportunity link; nothing is rewritten.

Revision ID: 0069
Revises: 0068
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("projects"):
        return
    op.alter_column("projects", "opportunity_id",
                    existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Rows created without an opportunity would violate NOT NULL — refuse
    # rather than invent links.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("projects"):
        return
    orphans = bind.execute(
        sa.text("SELECT COUNT(*) FROM projects WHERE opportunity_id IS NULL")
    ).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} project(s) have no opportunity; cannot restore NOT NULL."
        )
    op.alter_column("projects", "opportunity_id",
                    existing_type=sa.Integer(), nullable=False)
