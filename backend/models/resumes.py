"""Resume / ATS pipeline table."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK, pg_enum


class AtsStatus(str, enum.Enum):
    PENDING_SCAN = "Pending_Scan"
    SCORED = "Scored"
    SHORTLISTED = "Shortlisted"
    REJECTED = "Rejected"


class AiInterviewStatus(str, enum.Enum):
    NOT_SCHEDULED = "Not_Scheduled"
    SCHEDULED = "Scheduled"
    COMPLETED = "Completed"
    PASSED = "Passed"
    FAILED = "Failed"


class Resume(Base):
    __tablename__ = "resumes"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=True, index=True)
    candidate_name = sa.Column(sa.String(255), nullable=False)
    email = sa.Column(sa.String(255), nullable=True)
    phone = sa.Column(sa.String(32), nullable=True)
    source_portal = sa.Column(sa.String(64), nullable=True)
    applicant_experience = sa.Column(sa.String(64), nullable=True)  # self-reported years, from apply form
    # Extended fields captured by the public apply form (education, notice period,
    # technical domain, skills, current/expected CTC, preferred location, ...).
    application_details = sa.Column(JSONB, nullable=True)
    resume_file_url = sa.Column(sa.String(1024), nullable=False)
    # SHA-256 of the uploaded file bytes — integrity (verify on read) + dedupe.
    file_sha256 = sa.Column(sa.String(64), nullable=True, index=True)
    file_size = sa.Column(sa.Integer, nullable=True)
    received_date = sa.Column(sa.Date, server_default=sa.func.current_date(), nullable=False)
    ats_score = sa.Column(sa.Numeric(5, 2), nullable=True)
    ats_score_breakdown = sa.Column(JSONB, nullable=True)
    ats_status = sa.Column(pg_enum(AtsStatus, "ats_status"), nullable=False,
                           server_default=AtsStatus.PENDING_SCAN.value, index=True)
    screened_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True, index=True)
    ai_interview_status = sa.Column(pg_enum(AiInterviewStatus, "ai_interview_status"), nullable=False,
                                    server_default=AiInterviewStatus.NOT_SCHEDULED.value, index=True)
    ai_interview_scheduled_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    # Bulk upload held this resume as a possible duplicate of this existing
    # candidate (email/phone match). Persisted so the review queue survives the
    # results dialog; cleared when the TA resolves it (apply-duplicate).
    possible_duplicate_of = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"),
                                      nullable=True, index=True)
    # Dismissed hold (0087): keep the CV, hide the row from the applicant list.
    duplicate_dismissed = sa.Column(sa.Boolean, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    requirement = relationship("Requirement")
    candidate = relationship("Candidate", foreign_keys=[candidate_id])


class ResumeParseCache(Base):
    """AI extraction results keyed by file SHA-256 — a repeat upload of the
    same bytes must never pay for a second model call."""
    __tablename__ = "resume_parse_cache"
    file_sha256 = sa.Column(sa.String(64), primary_key=True)
    parsed = sa.Column(JSONB, nullable=False)
    model = sa.Column(sa.String(64), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
