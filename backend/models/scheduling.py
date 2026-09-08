"""Automated candidate pipeline tables: interview slots, slot bookings, outreach log.

InterviewSlot   — TA-published interview windows on a requirement (capacity-based).
SlotBooking     — one per resume: an opaque-token public booking link the candidate
                  uses to pick a slot; once confirmed it stores the AI-interview
                  invite token/url created for that candidate.
CandidateOutreach — free-form contact log (Call / Email / WhatsApp / LinkedIn / ...).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK


class InterviewSlot(Base):
    __tablename__ = "interview_slots"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    slot_at = sa.Column(sa.DateTime(timezone=True), nullable=False)
    capacity = sa.Column(sa.Integer, nullable=False, server_default="1")
    booked_count = sa.Column(sa.Integer, nullable=False, server_default="0")
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    requirement = relationship("Requirement")


class SlotBooking(Base):
    __tablename__ = "slot_bookings"
    id = sa.Column(sa.Integer, primary_key=True)
    # Opaque HMAC-style token (secrets.token_urlsafe) used in the public /book/{token} URL.
    token = sa.Column(sa.String(64), nullable=False, unique=True)
    resume_id = sa.Column(sa.Integer, sa.ForeignKey("resumes.id"), nullable=False, unique=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=True)
    slot_id = sa.Column(sa.Integer, sa.ForeignKey("interview_slots.id"), nullable=True)
    # Pending / Confirmed / Expired / Cancelled
    status = sa.Column(sa.String(16), nullable=False, server_default="Pending")
    # Set once the AI interview session is created on confirmation.
    invite_token = sa.Column(sa.String(64), nullable=True)
    invite_url = sa.Column(sa.String(512), nullable=True)
    # The TA who sent the invite (0084) — the slot-confirmed notification goes
    # to exactly this person, not the whole TA role. NULL on legacy bookings.
    invited_by = sa.Column(sa.Integer, sa.ForeignKey("registration_data.id"), nullable=True, index=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    confirmed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    resume = relationship("Resume")
    slot = relationship("InterviewSlot")


class InterviewEvent(Base):
    """Human interview rounds (L2 face-to-face, customer interviews) scheduled on
    a candidate profile — feeds the RMG/Sales interview calendar and .ics invites."""
    __tablename__ = "interview_events"
    id = sa.Column(sa.Integer, primary_key=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=True)
    # L2_F2F / Customer_Interview / Other
    kind = sa.Column(sa.String(32), nullable=False, server_default="L2_F2F")
    scheduled_at = sa.Column(sa.DateTime(timezone=True), nullable=True, index=True)
    raw_when = sa.Column(sa.String(64), nullable=True)  # original free-form input
    meeting_link = sa.Column(sa.String(1024), nullable=True)
    note = sa.Column(sa.Text, nullable=True)
    # Structured outcome of the round (Zoho Interview_Round subform). Kept alongside
    # `note` so imported history is queryable instead of buried in free text.
    stage = sa.Column(sa.String(120), nullable=True)
    mode = sa.Column(sa.String(60), nullable=True)
    status = sa.Column(sa.String(60), nullable=True)
    result = sa.Column(sa.String(60), nullable=True)
    interviewer = sa.Column(sa.String(200), nullable=True)
    feedback = sa.Column(sa.Text, nullable=True)
    zoho_round_id = sa.Column(sa.String(32), nullable=True)
    # --- RMG interview-feedback form (migration 0056) ------------------------
    interview_category = sa.Column(sa.String(20), nullable=True)   # Internal / External
    duration_minutes = sa.Column(sa.Integer, nullable=True)
    user_role = sa.Column(sa.String(40), nullable=True)            # who conducted the round
    # The panel member, when they exist in the Employees tab. `interviewer` always
    # keeps the display text (external panellists have no employee row).
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=True, index=True)
    # --- Zoho interviews.json (migration 0059) -------------------------------
    #: Durable external id so interviews.json upserts instead of duplicating.
    zoho_interview_id = sa.Column(sa.String(32), nullable=True)
    interviewer_email = sa.Column(sa.String(255), nullable=True)
    interviewer_phone = sa.Column(sa.String(32), nullable=True)
    external_panel = sa.Column(sa.String(255), nullable=True)
    scheduled_end = sa.Column(sa.DateTime(timezone=True), nullable=True)
    weightage = sa.Column(sa.String(60), nullable=True)
    score_card_reference = sa.Column(sa.String(255), nullable=True)
    venue = sa.Column(sa.String(255), nullable=True)
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    profile = relationship("CandidateProfile")
    candidate = relationship("Candidate")


class CandidateOutreach(Base):
    __tablename__ = "candidate_outreach"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=True)
    # Call / Email / WhatsApp / LinkedIn / Other
    channel = sa.Column(sa.String(16), nullable=False)
    note = sa.Column(sa.Text, nullable=False)
    outcome = sa.Column(sa.String(120), nullable=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    candidate = relationship("Candidate")
