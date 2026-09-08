"""HR: employees, leave balances, project history."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK, pg_enum


class ProfileType(str, enum.Enum):
    INTERNAL = "Internal"
    EXTERNAL = "External"


class Employee(Base):
    __tablename__ = "employees"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True, unique=True)
    first_name = sa.Column(sa.String(120), nullable=False)
    last_name = sa.Column(sa.String(120), nullable=True)
    email = sa.Column(sa.String(255), nullable=False, unique=True)
    phone = sa.Column(sa.String(32), nullable=True)
    department_id = sa.Column(sa.Integer, sa.ForeignKey("departments.id"), nullable=True)
    designation_id = sa.Column(sa.Integer, sa.ForeignKey("designations.id"), nullable=True)
    reporting_manager_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=True)
    reporting_hr_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=True)
    profile_type = sa.Column(pg_enum(ProfileType, "employee_profile_type"), nullable=False,
                             server_default=ProfileType.INTERNAL.value)
    portal_access = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    date_of_joining = sa.Column(sa.Date, nullable=True)
    pan = sa.Column(sa.String(10), nullable=True)
    aadhar = sa.Column(sa.String(12), nullable=True)
    bank_account_details = sa.Column(JSONB, nullable=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())

    # --- Tab 13 master-form fields (migration 0019) ---------------------------
    title = sa.Column(sa.String(16), nullable=True)            # Mr/Ms/Mrs/Dr
    middle_name = sa.Column(sa.String(120), nullable=True)
    display_name = sa.Column(sa.String(255), nullable=True)
    personal_email = sa.Column(sa.String(255), nullable=True)  # `email` stays the OFFICIAL one
    gender = sa.Column(sa.String(16), nullable=True)
    blood_group = sa.Column(sa.String(8), nullable=True)
    current_ctc = sa.Column(sa.Numeric(14, 2), nullable=True)
    cv_url = sa.Column(sa.String(1024), nullable=True)
    employee_code = sa.Column(sa.String(32), nullable=True, unique=True)  # human "Employee ID"
    emergency_number = sa.Column(sa.String(32), nullable=True)
    date_of_birth = sa.Column(sa.Date, nullable=True)
    present_address = sa.Column(JSONB, nullable=True)   # {line1,line2,city,state,postal_code,country,phone?}
    permanent_address = sa.Column(JSONB, nullable=True)
    work_location = sa.Column(sa.String(120), nullable=True)
    role_title = sa.Column(sa.String(120), nullable=True)
    skills = sa.Column(JSONB, nullable=True)            # list of skill names
    experience_years = sa.Column(sa.Numeric(4, 1), nullable=True)
    employment_type = sa.Column(sa.String(24), nullable=True)  # Full_Time/Part_Time/Contract
    is_resigned = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    date_of_resignation = sa.Column(sa.Date, nullable=True)
    notice_period_days = sa.Column(sa.Integer, nullable=True)
    last_working_day = sa.Column(sa.Date, nullable=True)
    candidate_profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=True)
    # Per-employee attendance rule (overrides project/customer policy defaults).
    min_hours_full_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    min_hours_half_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    normal_hours_per_day = sa.Column(sa.Numeric(4, 2), nullable=True)

    leave_balances = relationship("EmployeeLeaveBalance", back_populates="employee",
                                  cascade="all, delete-orphan")
    project_history = relationship("EmployeeProjectHistory", back_populates="employee",
                                   cascade="all, delete-orphan",
                                   order_by="EmployeeProjectHistory.start_date")
    education = relationship("EmployeeEducation", back_populates="employee",
                             cascade="all, delete-orphan",
                             order_by="EmployeeEducation.id")
    experience_details = relationship("EmployeeExperienceDetail", back_populates="employee",
                                      cascade="all, delete-orphan",
                                      order_by="EmployeeExperienceDetail.id")


class EmployeeLeaveBalance(Base):
    __tablename__ = "employee_leave_balances"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    year = sa.Column(sa.Integer, nullable=False)
    accrued = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    consumed = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    carry_forward = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    __table_args__ = (sa.UniqueConstraint("employee_id", "leave_type_id", "year",
                                          name="uq_employee_leave_year"),)

    employee = relationship("Employee", back_populates="leave_balances")
    leave_type = relationship("LeavePolicyType")


class EmployeeEducation(Base):
    __tablename__ = "employee_education"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    course = sa.Column(sa.String(255), nullable=False)
    branch_specialization = sa.Column(sa.String(255), nullable=True)
    start_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    university = sa.Column(sa.String(255), nullable=True)
    city = sa.Column(sa.String(120), nullable=True)
    certificate_url = sa.Column(sa.String(1024), nullable=True)

    employee = relationship("Employee", back_populates="education")


class EmployeeExperienceDetail(Base):
    __tablename__ = "employee_experience_details"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    company_name = sa.Column(sa.String(255), nullable=False)
    job_title = sa.Column(sa.String(255), nullable=True)
    currently_working = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    date_of_joining = sa.Column(sa.Date, nullable=True)
    date_of_relieving = sa.Column(sa.Date, nullable=True)
    city = sa.Column(sa.String(120), nullable=True)
    certificate_url = sa.Column(sa.String(1024), nullable=True)

    employee = relationship("Employee", back_populates="experience_details")


class EmployeeProjectHistory(Base):
    __tablename__ = "employee_project_history"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    start_date = sa.Column(sa.Date, nullable=False)
    end_date = sa.Column(sa.Date, nullable=True)
    role = sa.Column(sa.String(120), nullable=True)

    employee = relationship("Employee", back_populates="project_history")
    project = relationship("Project")
