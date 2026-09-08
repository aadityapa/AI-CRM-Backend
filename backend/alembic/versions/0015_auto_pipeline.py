"""Automated candidate pipeline: interview slots, slot bookings, outreach log.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-10
"""
from alembic import op

from models.scheduling import CandidateOutreach, InterviewSlot, SlotBooking

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Parents first (slot_bookings FKs interview_slots). No pg enums involved.
    InterviewSlot.__table__.create(bind=bind, checkfirst=True)
    SlotBooking.__table__.create(bind=bind, checkfirst=True)
    CandidateOutreach.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    CandidateOutreach.__table__.drop(bind=bind, checkfirst=True)
    SlotBooking.__table__.drop(bind=bind, checkfirst=True)
    InterviewSlot.__table__.drop(bind=bind, checkfirst=True)
