"""Interview-template request workflow (TA → RMG → TA).

TA raises a request against a requirement asking RMG to build an interview
template (for the role / skill-set / experience level). RMG creates the template
(existing Interview Templates page) and fulfils the request by linking it by name
(and optional job template id). It returns to TA, who prepares the L1 screening
(candidate email + chosen template). No automatic email is sent — TA generates the
actual interview invite via the existing Schedule AI L1 flow.
"""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK, pg_enum


class TemplateRequestStatus(str, enum.Enum):
    PENDING_RMG = "Pending_RMG"        # raised by TA, waiting for RMG to build the template
    TEMPLATE_READY = "Template_Ready"  # RMG linked a template, back with TA
    PREPARED = "Prepared"              # TA attached candidate + template for L1
    CANCELLED = "Cancelled"


class TemplateRequest(Base, TimestampMixin):
    __tablename__ = "template_requests"
    id = sa.Column(sa.Integer, primary_key=True)
    tr_number = sa.Column(sa.String(32), nullable=False, unique=True)  # TR-2026-001
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=False, index=True)
    # Denormalised from the requirement so fulfill/L1 can stamp the job template
    # without re-joining. Nullable for rows created before migration 0031.
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=True, index=True)

    role_title = sa.Column(sa.String(255), nullable=False)
    skills = sa.Column(sa.Text, nullable=True)
    experience_level = sa.Column(sa.String(120), nullable=True)
    notes = sa.Column(sa.Text, nullable=True)

    status = sa.Column(pg_enum(TemplateRequestStatus, "template_request_status"), nullable=False,
                       server_default=TemplateRequestStatus.PENDING_RMG.value, index=True)

    requested_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)

    # Filled by RMG on fulfil — real legacy job_templates.job_id + display name.
    template_name = sa.Column(sa.String(255), nullable=True)
    template_job_id = sa.Column(sa.String(64), nullable=True)  # legacy job_templates.job_id
    fulfilled_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    fulfilled_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    # Filled by TA on prepare.
    candidate_name = sa.Column(sa.String(255), nullable=True)
    candidate_email = sa.Column(sa.String(255), nullable=True)
    prepared_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    prepared_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    requirement = relationship("Requirement")
    opportunity = relationship("Opportunity")
