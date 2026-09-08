"""Requirement workflow: requirements, skills, job postings, activity log."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK, WorkMode, pg_enum


class Priority(str, enum.Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class RequirementStatus(str, enum.Enum):
    DRAFT = "Draft"
    PENDING_SALES_HEAD_APPROVAL = "Pending_Sales_Head_Approval"
    SALES_HEAD_REJECTED = "Sales_Head_Rejected"
    PENDING_ENGINEERING_REVIEW = "Pending_Engineering_Review"
    ENGINEERING_REJECTED = "Engineering_Rejected"
    OPEN_FOR_SOURCING = "Open_For_Sourcing"
    POSTED_ON_PORTALS = "Posted_On_Portals"
    IN_PROGRESS = "In_Progress"
    #: RMG paused sourcing (25 Aug 2026). Resume returns to held_from_status.
    ON_HOLD = "On_Hold"
    FULFILLED = "Fulfilled"
    CLOSED = "Closed"
    CANCELLED = "Cancelled"


class JobPostingStatus(str, enum.Enum):
    ACTIVE = "Active"
    EXPIRED = "Expired"
    REMOVED = "Removed"


class Requirement(Base, TimestampMixin):
    __tablename__ = "requirements"
    id = sa.Column(sa.Integer, primary_key=True)
    req_number = sa.Column(sa.String(32), nullable=False, unique=True)  # REQ-2026-001
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    title = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.Text, nullable=True)
    # RMG job description (typed at engineering-approve); file JD lives in RequirementAttachment.
    rmg_jd_text = sa.Column(sa.Text, nullable=True)
    # Optional per-requirement ATS component weights, e.g. {"experience": 30,
    # "mandatory": 40}. NULL = use the scorer's default weights. Missing keys fall
    # back to defaults; only configured criteria enter the score denominator.
    ats_weights = sa.Column(JSONB, nullable=True)
    no_of_positions = sa.Column(sa.Integer, nullable=False, server_default="1")
    experience_min = sa.Column(sa.Numeric(4, 1), nullable=True)
    experience_max = sa.Column(sa.Numeric(4, 1), nullable=True)
    budget_ctc_min = sa.Column(sa.Numeric(14, 2), nullable=True)
    budget_ctc_max = sa.Column(sa.Numeric(14, 2), nullable=True)
    work_mode = sa.Column(pg_enum(WorkMode, "work_mode"), nullable=True)
    location_id = sa.Column(sa.Integer, sa.ForeignKey("locations.id"), nullable=True)
    priority = sa.Column(pg_enum(Priority, "requirement_priority"), nullable=False,
                         server_default=Priority.MEDIUM.value)
    target_closure_date = sa.Column(sa.Date, nullable=True)
    status = sa.Column(pg_enum(RequirementStatus, "requirement_status"), nullable=False,
                       server_default=RequirementStatus.DRAFT.value, index=True)
    #: RMG hold (25 Aug 2026): the status Resume returns to, and why it was held.
    held_from_status = sa.Column(sa.String(60), nullable=True)
    held_reason = sa.Column(sa.String(1000), nullable=True)
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    sales_head_approved_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    sales_head_approved_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    sales_head_rejection_reason = sa.Column(sa.Text, nullable=True)
    engineering_reviewed_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    engineering_reviewed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    engineering_rejection_reason = sa.Column(sa.Text, nullable=True)

    #: Read-only link to the parent opportunity (18 Aug 2026) — the requirement
    #: displays and is searchable by the opportunity's ID, so every role tracks
    #: ONE id from create to close. No cascade: the opportunity owns itself.
    opportunity = relationship("Opportunity", foreign_keys=[opportunity_id], viewonly=True)

    skills = relationship("RequirementSkill", back_populates="requirement", cascade="all, delete-orphan")
    job_postings = relationship("RequirementJobPosting", back_populates="requirement",
                                cascade="all, delete-orphan")
    activity_log = relationship("RequirementActivityLog", back_populates="requirement",
                                cascade="all, delete-orphan", order_by="RequirementActivityLog.timestamp")
    attachments = relationship("RequirementAttachment", back_populates="requirement",
                               cascade="all, delete-orphan")


class RequirementAttachment(Base):
    """Files on a requirement — primarily RMG JD uploads at engineering approve."""
    __tablename__ = "requirement_attachments"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    file_url = sa.Column(sa.String(1024), nullable=False)
    file_name = sa.Column(sa.String(255), nullable=True)
    file_sha256 = sa.Column(sa.String(64), nullable=True, index=True)
    file_size = sa.Column(sa.Integer, nullable=True)
    # "rmg_jd" (default) | future kinds
    kind = sa.Column(sa.String(32), nullable=True, index=True)
    uploaded_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    uploaded_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    requirement = relationship("Requirement", back_populates="attachments")


class RequirementSkill(Base):
    __tablename__ = "requirement_skills"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    skill_id = sa.Column(sa.Integer, sa.ForeignKey("skills.id"), nullable=False)
    is_mandatory = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    min_rating = sa.Column(sa.Integer, nullable=True)  # 1..5
    __table_args__ = (sa.UniqueConstraint("requirement_id", "skill_id", name="uq_req_skill"),)

    requirement = relationship("Requirement", back_populates="skills")


class RequirementJobPosting(Base):
    __tablename__ = "requirement_job_postings"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    portal_name = sa.Column(sa.String(64), nullable=False)  # Naukri / LinkedIn / Indeed / Other
    job_post_url = sa.Column(sa.String(1024), nullable=False)
    posted_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    posted_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    status = sa.Column(pg_enum(JobPostingStatus, "job_posting_status"), nullable=False,
                       server_default=JobPostingStatus.ACTIVE.value)

    requirement = relationship("Requirement", back_populates="job_postings")


class RequirementActivityLog(Base):
    __tablename__ = "requirement_activity_log"
    id = sa.Column(sa.Integer, primary_key=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    action_type = sa.Column(sa.String(64), nullable=False)
    comment = sa.Column(sa.Text, nullable=True)
    timestamp = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    requirement = relationship("Requirement", back_populates="activity_log")
