"""Opportunities, opportunity skills, activity log."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK, pg_enum


class OppType(str, enum.Enum):
    T_AND_M = "T&M"
    WORK_PACKAGE = "Work_Package"
    FIXED_PRICE = "Fixed_Price"
    RETAINER = "Retainer"


class PipelineStage(str, enum.Enum):
    NEW = "New"
    ACTIVE = "Active"
    ON_HOLD = "On_Hold"            # shown as "Customer Hold"
    SALES_HOLD = "Sales_Hold"      # Sales parked it (8 Sep 2026, user request)
    CLOSED_WON = "Closed_Won"
    CLOSED_LOST = "Closed_Lost"
    CLOSED_PARTIAL = "Closed_Partial"
    REJECTED = "Rejected"
    ARCHIVED = "Archived"


class OpportunityApprovalStatus(str, enum.Enum):
    """Sales Head sign-off gate for a newly-created opportunity.

    Sales-created opportunities start PENDING_SALES_HEAD_APPROVAL and only become
    usable once a Sales Head APPROVES. Opportunities created by Sales_Head/Admin are
    auto-APPROVED. Existing rows (pre-migration) are backfilled to APPROVED.
    """
    PENDING_SALES_HEAD_APPROVAL = "Pending_Sales_Head_Approval"
    APPROVED = "Approved"
    REJECTED = "Rejected"


class Opportunity(Base, TimestampMixin):
    __tablename__ = "opportunities"
    id = sa.Column(sa.Integer, primary_key=True)
    opp_id = sa.Column(sa.String(32), nullable=False, unique=True)  # e.g. OPP-2026-001
    title = sa.Column(sa.String(255), nullable=False)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    contact_person_id = sa.Column(sa.Integer, sa.ForeignKey("contact_persons.id"), nullable=True)
    hiring_manager_id = sa.Column(sa.Integer, sa.ForeignKey("contact_persons.id"), nullable=True)
    opp_type = sa.Column(pg_enum(OppType, "opp_type"), nullable=False)
    rfi_value = sa.Column(sa.Numeric(14, 2), nullable=True)
    rfi_received_date = sa.Column(sa.Date, nullable=True)
    pipeline_stage = sa.Column(pg_enum(PipelineStage, "opportunity_pipeline_stage"),
                               nullable=False, server_default=PipelineStage.NEW.value)
    onboarding_status = sa.Column(sa.String(120), nullable=True)
    onboarded_count = sa.Column(sa.Integer, nullable=False, server_default="0")
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)

    # Type-specific fields (T&M / Leave & Holiday / Commercial extras / Project Scope)
    # live here as JSON, validated server-side against opportunity_form_schema for the
    # opportunity's opp_type. Core columns above remain first-class/queryable.
    details = sa.Column(JSONB, nullable=True)
    # Optimistic-concurrency guard: bumped on every write; a stale client version is rejected.
    version = sa.Column(sa.Integer, nullable=False, server_default="1")

    # Sales Head approval gate (mirrors requirements approval audit fields).
    approval_status = sa.Column(
        pg_enum(OpportunityApprovalStatus, "opportunity_approval_status"),
        nullable=False, server_default=OpportunityApprovalStatus.APPROVED.value, index=True,
    )
    sales_head_approved_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    sales_head_approved_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    approval_rejection_reason = sa.Column(sa.Text, nullable=True)

    skills = relationship("OpportunitySkill", back_populates="opportunity", cascade="all, delete-orphan")
    activity_log = relationship("OpportunityActivityLog", back_populates="opportunity",
                                cascade="all, delete-orphan", order_by="OpportunityActivityLog.timestamp")
    ctc_slab = relationship("OpportunityCtcSlab", back_populates="opportunity",
                            cascade="all, delete-orphan", order_by="OpportunityCtcSlab.position")


class OpportunitySkill(Base):
    __tablename__ = "opportunity_skills"
    id = sa.Column(sa.Integer, primary_key=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    skill_id = sa.Column(sa.Integer, sa.ForeignKey("skills.id"), nullable=False)
    is_mandatory = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    required_level = sa.Column(sa.Integer, nullable=True)  # 1..5 (Required Skill Level)
    comment = sa.Column(sa.Text, nullable=True)
    __table_args__ = (sa.UniqueConstraint("opportunity_id", "skill_id", name="uq_opp_skill"),)

    opportunity = relationship("Opportunity", back_populates="skills")


class OpportunityActivityLog(Base):
    __tablename__ = "opportunity_activity_log"
    id = sa.Column(sa.Integer, primary_key=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    action_type = sa.Column(sa.String(64), nullable=False)
    comment = sa.Column(sa.Text, nullable=True)
    timestamp = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    opportunity = relationship("Opportunity", back_populates="activity_log")


class OpportunityAttachment(Base):
    __tablename__ = "opportunity_attachments"
    id = sa.Column(sa.Integer, primary_key=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    file_url = sa.Column(sa.String(1024), nullable=False)
    file_name = sa.Column(sa.String(255), nullable=True)
    # SHA-256 of the uploaded bytes (integrity + dedupe); verified on read.
    file_sha256 = sa.Column(sa.String(64), nullable=True, index=True)
    file_size = sa.Column(sa.Integer, nullable=True)
    # "customer_jd" | "general" (nullable legacy rows treated as general)
    kind = sa.Column(sa.String(32), nullable=True, index=True)
    uploaded_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    uploaded_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


class OpportunityCtcSlab(Base):
    """Candidate CTC Slab rows (repeating table). Revenue (Annual) is derived
    (Monthly × 12) on the client and re-derived/validated server-side."""
    __tablename__ = "opportunity_ctc_slab"
    id = sa.Column(sa.Integer, primary_key=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    position = sa.Column(sa.Integer, nullable=False, server_default="0")  # row order (drag-reorder)
    exp_min = sa.Column(sa.Numeric(5, 2), nullable=True)
    exp_max = sa.Column(sa.Numeric(5, 2), nullable=True)
    target_exp = sa.Column(sa.Numeric(5, 2), nullable=True)
    rate = sa.Column(sa.Numeric(14, 2), nullable=True)
    revenue_monthly = sa.Column(sa.Numeric(14, 2), nullable=True)
    revenue_annual = sa.Column(sa.Numeric(16, 2), nullable=True)  # derived = monthly * 12
    management_cost_pct = sa.Column(sa.Numeric(6, 2), nullable=True)
    engineering_budget = sa.Column(sa.Numeric(16, 2), nullable=True)
    hike_pct = sa.Column(sa.Numeric(6, 2), nullable=True)
    appraisal_cycle = sa.Column(sa.String(60), nullable=True)
    approved_ctc_lac = sa.Column(sa.Numeric(10, 2), nullable=True)

    opportunity = relationship("Opportunity", back_populates="ctc_slab")
