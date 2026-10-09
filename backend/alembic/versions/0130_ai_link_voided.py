"""`ai_interview_links.voided_*` — an AI L1 that must not count (8 Oct 2026).

Reported: a candidate applied for a Bluetooth Developer position sat an AI L1
on the "AGM - Research & Development (ADAS & ARAS)" template — RMG had linked
the wrong template to the request — and PASSED it. A pass could never be
rescheduled ("nothing to reschedule"), so the wrong verdict was stuck on the
candidacy. TA / RMG may now send a fresh link that VOIDS the previous one with
a reason; the old link stays on record, labelled "Voided", never deleted.

Revision ID: 0130
Revises: 0129
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_interview_links", sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_interview_links", sa.Column("voided_by", sa.Integer(), nullable=True))
    op.add_column("ai_interview_links", sa.Column("voided_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_interview_links", "voided_reason")
    op.drop_column("ai_interview_links", "voided_by")
    op.drop_column("ai_interview_links", "voided_at")
