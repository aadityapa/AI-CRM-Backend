"""Projects, project employees, communication matrix."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, WorkMode, pg_enum


class BillingFrequency(str, enum.Enum):
    MONTHLY = "Monthly"
    BI_WEEKLY = "Bi_Weekly"
    WEEKLY = "Weekly"
    QUARTERLY = "Quarterly"
    YEARLY = "Yearly"


class ProjectStatus(str, enum.Enum):
    ACTIVE = "Active"
    COMPLETED = "Completed"
    ON_HOLD = "On_Hold"


class BillingUnit(str, enum.Enum):
    """The UI labels these Per Hour / Per Day / Per Month / Per Year.

    YEARLY stores the ANNUAL price; invoicing bills its monthly equivalent
    (rate / 12) — see timesheet_invoice_preview.
    """

    HOURLY = "Hourly"
    DAILY = "Daily"
    MONTHLY = "Monthly"
    YEARLY = "Yearly"


class CommEntryType(str, enum.Enum):
    CUSTOMER = "Customer"
    INTERNAL = "Internal"


class Project(Base):
    __tablename__ = "projects"
    id = sa.Column(sa.Integer, primary_key=True)
    #: Optional since migration 0069 — projects can exist without a sales
    #: opportunity (internal work, direct engagements, migrated data).
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=True, index=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    # Explicit delivery branch (nullable for legacy rows until backfilled).
    # Prefer this over opportunity.branch_id for branch-policy / linked-projects scoping.
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    billing_cycle_start_day = sa.Column(sa.Integer, nullable=False, server_default="1")
    billing_cycle_end_day = sa.Column(sa.Integer, nullable=False, server_default="31")
    billing_frequency = sa.Column(pg_enum(BillingFrequency, "billing_frequency"), nullable=False,
                                  server_default=BillingFrequency.MONTHLY.value)
    recurring_billing = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    max_billable_hours_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    max_billable_hours_month = sa.Column(sa.Numeric(6, 2), nullable=True)
    max_billable_days_month = sa.Column(sa.Integer, nullable=True)
    no_billing_period_days = sa.Column(sa.Integer, nullable=True)
    # --- Branch-policy overrides (spec §6): NULL = inherit branch default ---
    holidays_billable = sa.Column(sa.Boolean, nullable=True)
    weekoff_billable = sa.Column(sa.Boolean, nullable=True)
    #: NULL = inherit branch/customer week-off pattern. (0072)
    week_off_days = sa.Column(sa.String(20), nullable=True)
    leave_billable = sa.Column(sa.Boolean, nullable=True)
    comp_off_billable = sa.Column(sa.Boolean, nullable=True)
    hours_required_half_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_full_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_half_day_comp_off = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_full_day_comp_off = sa.Column(sa.Numeric(4, 2), nullable=True)
    working_hours_per_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    is_max_billable_hours_per_day = sa.Column(sa.Boolean, nullable=True)
    is_max_billable_hours_per_month = sa.Column(sa.Boolean, nullable=True)
    is_max_billable_days_per_month = sa.Column(sa.Boolean, nullable=True)
    is_initial_no_billing_period = sa.Column(sa.Boolean, nullable=True)
    # Column semantics (SOURCE UI labels are swapped vs these names):
    #   initial_no_billing_qty     = numeric count (UI "Initial No Billing Period")
    #   initial_no_billing_period  = unit string Hours|Days|Week|Month|Year
    #                                (UI "Initial No Billing QTY")
    initial_no_billing_qty = sa.Column(sa.Integer, nullable=True)
    initial_no_billing_period = sa.Column(sa.String(40), nullable=True)
    status = sa.Column(pg_enum(ProjectStatus, "project_status"), nullable=False,
                       server_default=ProjectStatus.ACTIVE.value, index=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    employees = relationship("ProjectEmployee", back_populates="project", cascade="all, delete-orphan")
    communication_matrix = relationship("ProjectCommunicationMatrix", back_populates="project",
                                        cascade="all, delete-orphan")
    leave_policies = relationship("ProjectLeavePolicy", back_populates="project",
                                  cascade="all, delete-orphan")


class ProjectLeavePolicy(Base):
    """Per-project leave crediting rules for one leave type (Edit Project §2).

    Distinct from customer_leave_policies (customer/branch scope) and from
    project_employee_leave_details (per-assignment balances).
    """

    __tablename__ = "project_leave_policies"
    id = sa.Column(sa.Integer, primary_key=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    name = sa.Column(sa.String(255), nullable=True)
    leave_credit_type = sa.Column(sa.String(40), nullable=False, server_default="Monthly")
    leave_credit_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    initial_credit_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    leave_expire = sa.Column(sa.String(40), nullable=False, server_default="Annually")
    # Start_Of_Period | End_Of_Period — when the cycle credit is granted / when
    # the unused remainder lapses (mirrors customer_leave_policies).
    leave_credit_timing = sa.Column(sa.String(24), nullable=True)
    leave_expire_timing = sa.Column(sa.String(24), nullable=True)
    is_max_limit = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    maximum_carry_forward = sa.Column(sa.Integer, nullable=False, server_default="0")
    effective_date = sa.Column(sa.Date, nullable=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now(), nullable=False)
    __table_args__ = (sa.UniqueConstraint("project_id", "leave_type_id",
                                          name="uq_project_leave_policy"),)

    project = relationship("Project", back_populates="leave_policies")
    leave_type = relationship("LeavePolicyType")


class ProjectEmployee(Base):
    __tablename__ = "project_employees"
    id = sa.Column(sa.Integer, primary_key=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    onboarding_date = sa.Column(sa.Date, nullable=True)
    experience_years = sa.Column(sa.Numeric(4, 1), nullable=True)
    project_experience_years = sa.Column(sa.Numeric(4, 1), nullable=True)
    work_mode = sa.Column(pg_enum(WorkMode, "work_mode"), nullable=True)
    billing_rate = sa.Column(sa.Numeric(12, 2), nullable=False)
    billing_unit = sa.Column(pg_enum(BillingUnit, "billing_unit"), nullable=False,
                             server_default=BillingUnit.MONTHLY.value)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    is_exit = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    exit_date = sa.Column(sa.Date, nullable=True)
    billing_date = sa.Column(sa.Date, nullable=True)  # first billable date
    role_title = sa.Column(sa.String(255), nullable=True)  # assignment label for reports
    # Set on exit when leave balances remain to settle; cleared on remap (UC-09).
    settlement_pending = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    __table_args__ = (sa.UniqueConstraint("project_id", "employee_id", name="uq_project_employee"),)

    project = relationship("Project", back_populates="employees")
    employee = relationship("Employee")
    leave_details = relationship("ProjectEmployeeLeaveDetail", back_populates="project_employee",
                                 cascade="all, delete-orphan")
    rates = relationship("ProjectEmployeeRate", back_populates="project_employee",
                         cascade="all, delete-orphan",
                         order_by="ProjectEmployeeRate.effective_from")


class ProjectEmployeeLeaveDetail(Base):
    """Leave balances owned by a Project Employee mapping (client policy while deployed)."""

    __tablename__ = "project_employee_leave_details"
    id = sa.Column(sa.Integer, primary_key=True)
    project_employee_id = sa.Column(sa.Integer, sa.ForeignKey("project_employees.id", ondelete="CASCADE"),
                                    nullable=False, index=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    customer_leave_policy_id = sa.Column(sa.Integer, sa.ForeignKey("customer_leave_policies.id"),
                                         nullable=True)
    # Set when this row was seeded from a PROJECT-level override (project wins
    # over branch/customer in the crediting chain). Exactly one of
    # project_leave_policy_id / customer_leave_policy_id is set per row.
    project_leave_policy_id = sa.Column(sa.Integer,
                                        sa.ForeignKey("project_leave_policies.id"),
                                        nullable=True)
    initial_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    opening_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    leave_accrual = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    leave_consumed = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    leave_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    __table_args__ = (sa.UniqueConstraint("project_employee_id", "leave_type_id",
                                          name="uq_pe_leave_detail"),)

    project_employee = relationship("ProjectEmployee", back_populates="leave_details")
    leave_type = relationship("LeavePolicyType")
    customer_leave_policy = relationship("CustomerLeavePolicy")
    project_leave_policy = relationship("ProjectLeavePolicy")


class ProjectEmployeeRate(Base):
    """Effective-dated commercial rate for a Project Employee mapping."""

    __tablename__ = "project_employee_rates"
    id = sa.Column(sa.Integer, primary_key=True)
    project_employee_id = sa.Column(sa.Integer, sa.ForeignKey("project_employees.id", ondelete="CASCADE"),
                                    nullable=False, index=True)
    effective_from = sa.Column(sa.Date, nullable=False)
    rate = sa.Column(sa.Numeric(12, 2), nullable=False)
    billing_unit = sa.Column(pg_enum(BillingUnit, "billing_unit"), nullable=True)
    is_current_rate = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    project_employee = relationship("ProjectEmployee", back_populates="rates")


class ProjectCommunicationMatrix(Base):
    __tablename__ = "project_communication_matrix"
    id = sa.Column(sa.Integer, primary_key=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    name = sa.Column(sa.String(255), nullable=False)
    role = sa.Column(sa.String(120), nullable=True)
    responsible_person = sa.Column(sa.String(255), nullable=True)
    email = sa.Column(sa.String(255), nullable=True)
    phone = sa.Column(sa.String(32), nullable=True)
    type = sa.Column(pg_enum(CommEntryType, "comm_entry_type"), nullable=False,
                     server_default=CommEntryType.CUSTOMER.value)

    project = relationship("Project", back_populates="communication_matrix")
