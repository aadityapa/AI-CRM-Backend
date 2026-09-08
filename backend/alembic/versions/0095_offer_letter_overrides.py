"""offer_history.letter_overrides — HR's edits to the generated offer letter.

User request 4 Sep 2026: HR views the generated offer letter in the CRM,
edits the wording or a field for this candidate, saves, and downloads the
PDF / Word with those edits. The edits live ON the offer row as JSON
(``{"fields": {...}, "paragraphs": [...]}``); NULL means "the default letter
built from the profile". Nothing else about the offer changes.

Revision ID: 0095
Revises: 0094
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("offer_history"):
        existing = {c["name"] for c in inspector.get_columns("offer_history")}
        if "letter_overrides" not in existing:
            op.add_column("offer_history", sa.Column("letter_overrides", JSONB, nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("offer_history"):
        existing = {c["name"] for c in inspector.get_columns("offer_history")}
        if "letter_overrides" in existing:
            op.drop_column("offer_history", "letter_overrides")
