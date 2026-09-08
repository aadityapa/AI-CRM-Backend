"""CRM candidate master + education / experience / skills."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidates"
    id = sa.Column(sa.Integer, primary_key=True)
    salutation = sa.Column(sa.String(10), nullable=True)  # Mr / Ms / Mrs / Dr ...
    first_name = sa.Column(sa.String(120), nullable=False)
    middle_name = sa.Column(sa.String(120), nullable=True)
    last_name = sa.Column(sa.String(120), nullable=True)
    email = sa.Column(sa.String(255), nullable=False, unique=True)
    phone = sa.Column(sa.String(32), nullable=True)
    date_of_birth = sa.Column(sa.Date, nullable=True)
    gender = sa.Column(sa.String(20), nullable=True)
    experience_years = sa.Column(sa.Numeric(4, 1), nullable=True)  # total years of experience
    notice_period = sa.Column(sa.String(60), nullable=True)
    current_address = sa.Column(sa.Text, nullable=True)
    permanent_address = sa.Column(sa.Text, nullable=True)
    technical_domain = sa.Column(sa.String(120), nullable=True)
    roles = sa.Column(sa.String(255), nullable=True)  # comma-separated technical roles
    designation_id = sa.Column(sa.Integer, sa.ForeignKey("designations.id"), nullable=True)
    cv_url = sa.Column(sa.String(1024), nullable=True)
    linkedin_url = sa.Column(sa.String(1024), nullable=True)
    resignation_status = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    last_working_day = sa.Column(sa.Date, nullable=True)
    resignation_certificate_url = sa.Column(sa.String(1024), nullable=True)
    current_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    expected_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    preferred_location_id = sa.Column(sa.Integer, sa.ForeignKey("locations.id"), nullable=True)
    # --- Zoho NEXUS candidate export (migration 0057) ------------------------
    # Durable external id: re-imports and the applied-opportunities bridge key on
    # this rather than email, which users can edit in the app.
    zoho_candidate_id = sa.Column(sa.String(32), nullable=True)
    city = sa.Column(sa.String(120), nullable=True)
    # Full preferred-location list, comma separated. preferred_location_id still
    # holds the primary one so existing filters keep working.
    preferred_locations = sa.Column(sa.String(500), nullable=True)
    recruiter_email = sa.Column(sa.String(255), nullable=True)
    cv_original_filename = sa.Column(sa.String(255), nullable=True)
    source_created_date = sa.Column(sa.Date, nullable=True)

    education = relationship("CandidateEducation", back_populates="candidate", cascade="all, delete-orphan")
    experience = relationship("CandidateExperience", back_populates="candidate", cascade="all, delete-orphan")
    skills = relationship("CandidateSkill", back_populates="candidate", cascade="all, delete-orphan")
    profiles = relationship("CandidateProfile", back_populates="candidate")


class CandidateEducation(Base):
    __tablename__ = "candidate_education"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    course = sa.Column(sa.String(255), nullable=False)
    institution = sa.Column(sa.String(255), nullable=True)
    start_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    certificate_url = sa.Column(sa.String(1024), nullable=True)

    candidate = relationship("Candidate", back_populates="education")


class CandidateExperience(Base):
    __tablename__ = "candidate_experience"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    company_name = sa.Column(sa.String(255), nullable=False)
    job_title = sa.Column(sa.String(255), nullable=True)
    start_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    certificate_url = sa.Column(sa.String(1024), nullable=True)
    is_current = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())

    candidate = relationship("Candidate", back_populates="experience")


class CandidateSkill(Base):
    __tablename__ = "candidate_skills"
    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    skill_id = sa.Column(sa.Integer, sa.ForeignKey("skills.id"), nullable=False)
    __table_args__ = (sa.UniqueConstraint("candidate_id", "skill_id", name="uq_candidate_skill"),)

    candidate = relationship("Candidate", back_populates="skills")
