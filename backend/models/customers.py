"""Customer, branches, billing policy, documents, contact persons."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, pg_enum


class CustomerStatus(str, enum.Enum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"


class Customer(Base, TimestampMixin):
    __tablename__ = "customers"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(255), nullable=False, unique=True)
    legal_entity_name = sa.Column(sa.String(255), nullable=True)
    customer_type = sa.Column(sa.String(60), nullable=True)  # e.g. Direct / Partner / MSP / Enterprise
    # Registered/primary address (branches keep their own billing addresses).
    address_line_1 = sa.Column(sa.String(255), nullable=True)
    address_line_2 = sa.Column(sa.String(255), nullable=True)
    city = sa.Column(sa.String(120), nullable=True)
    state = sa.Column(sa.String(120), nullable=True)
    pincode = sa.Column(sa.String(16), nullable=True)
    country = sa.Column(sa.String(120), nullable=True)
    status = sa.Column(pg_enum(CustomerStatus, "customer_status"), nullable=False,
                       server_default=CustomerStatus.ACTIVE.value)

    branches = relationship("CustomerBranch", back_populates="customer", cascade="all, delete-orphan")
    billing_policy = relationship("CustomerBillingPolicy", back_populates="customer",
                                  uselist=False, cascade="all, delete-orphan")
    contacts = relationship("ContactPerson", back_populates="customer", cascade="all, delete-orphan")
    documents = relationship("CustomerDocument", back_populates="customer", cascade="all, delete-orphan")


class CustomerBranch(Base):
    __tablename__ = "customer_branches"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    branch_name = sa.Column(sa.String(255), nullable=False)
    branch_legal_name = sa.Column(sa.String(255), nullable=True)
    billing_address = sa.Column(sa.Text, nullable=True)          # Address Line 1
    address_line_2 = sa.Column(sa.String(255), nullable=True)
    delivery_address = sa.Column(sa.Text, nullable=True)         # separate ship-to / delivery address
    city = sa.Column(sa.String(120), nullable=True)
    state = sa.Column(sa.String(120), nullable=True)
    pincode = sa.Column(sa.String(16), nullable=True)
    country = sa.Column(sa.String(120), nullable=True)
    gstin = sa.Column(sa.String(15), nullable=True)
    pan = sa.Column(sa.String(10), nullable=True)
    is_primary = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())

    # --- Leave & holiday billing policy (branch-level) ---
    # Tri-state booleans: NULL = "not set here — inherit the customer's default
    # billing policy" (see services.timesheets.effective_billing_policy).
    holidays_billable = sa.Column(sa.Boolean, nullable=True)
    weekoff_billable = sa.Column(sa.Boolean, nullable=True)
    #: NULL = inherit customer default / built-in Sat+Sun. (0072)
    week_off_days = sa.Column(sa.String(20), nullable=True)
    leave_billable = sa.Column(sa.Boolean, nullable=True)
    comp_off_billable = sa.Column(sa.Boolean, nullable=True)
    #: NULL = inherit customer default. Paid leaves/year billed by customer (0078).
    billable_leaves_per_year = sa.Column(sa.Numeric(5, 2), nullable=True)
    hours_required_half_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_full_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    working_hours_per_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_half_day_comp_off = sa.Column(sa.Numeric(4, 2), nullable=True)
    hours_required_full_day_comp_off = sa.Column(sa.Numeric(4, 2), nullable=True)

    # --- Billing properties ---
    # Per_Hour / Per_Day / Per_Month / Per_Year; NULL = inherit customer default (migration 0036)
    billing_type = sa.Column(sa.String(16), nullable=True)
    billing_frequency = sa.Column(sa.String(40), nullable=True)  # Weekly / Monthly / ...
    billing_cycle_start_day = sa.Column(sa.Integer, nullable=True)  # day of month 1..31
    billing_cycle_end_day = sa.Column(sa.Integer, nullable=True)
    is_max_billable_hours_per_day = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    max_billable_hours_per_day = sa.Column(sa.Numeric(5, 2), nullable=True)
    is_max_billable_hours_per_month = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    max_billable_hours_per_month = sa.Column(sa.Numeric(7, 2), nullable=True)
    is_max_billable_days_per_month = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    max_billable_days_per_month = sa.Column(sa.Numeric(5, 2), nullable=True)
    is_initial_no_billing_period = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    initial_no_billing_qty = sa.Column(sa.Integer, nullable=True)
    initial_no_billing_period = sa.Column(sa.String(40), nullable=True)  # Days / Months

    customer = relationship("Customer", back_populates="branches")
    holiday_years = relationship(
        "BranchHolidayYear", back_populates="branch", cascade="all, delete-orphan"
    )


class BranchHolidayYear(Base):
    """Per-branch calendar-year header for holiday lists (spec §2).

    Holiday count is derived from ``holidays`` (branch_id + year), not stored.
    When ``is_freeze`` is true the year is locked for edit.
    """

    __tablename__ = "branch_holiday_years"
    id = sa.Column(sa.Integer, primary_key=True)
    branch_id = sa.Column(
        sa.Integer,
        sa.ForeignKey("customer_branches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    calendar_year = sa.Column(sa.Integer, nullable=False)
    is_freeze = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    created_at = sa.Column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    __table_args__ = (
        sa.UniqueConstraint("branch_id", "calendar_year", name="uq_branch_holiday_year"),
    )

    branch = relationship("CustomerBranch", back_populates="holiday_years")
    holidays = relationship("Holiday", back_populates="holiday_calendar")


class CustomerBillingPolicy(Base):
    """Attendance-driven billing rules — these drive invoice computation."""

    __tablename__ = "customer_billing_policies"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, unique=True)
    week_off_billable = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    #: CSV of Python weekday numbers (0=Mon..6=Sun) that count as week-off,
    #: e.g. "5,6". NULL = built-in Sat+Sun. (0072)
    week_off_days = sa.Column(sa.String(20), nullable=True)
    leave_billable = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    holidays_billable = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    min_hours_full_day = sa.Column(sa.Numeric(4, 2), nullable=False, server_default="8.00")
    min_hours_half_day = sa.Column(sa.Numeric(4, 2), nullable=False, server_default="4.00")
    # Per_Hour / Per_Day / Per_Month / Per_Year; NULL = not set (migration 0036)
    billing_type = sa.Column(sa.String(16), nullable=True)

    # Comp-off section
    comp_off_billable = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    comp_off_balance = sa.Column(sa.Numeric(7, 2), nullable=True)
    comp_off_balance_initial = sa.Column(sa.Numeric(7, 2), nullable=True)
    comp_off_max_limit = sa.Column(sa.Numeric(7, 2), nullable=True)
    comp_off_max_carry_forward = sa.Column(sa.Numeric(7, 2), nullable=True)

    #: Paid leaves per year the CUSTOMER covers (the "APTIV rule", 0078):
    #: these many leave days are billed even when leave_billable is off —
    #: the CTC slab adds them back to the billable-days base.
    billable_leaves_per_year = sa.Column(sa.Numeric(5, 2), nullable=True)

    # Attendance rule
    normal_hours_per_day = sa.Column(sa.Numeric(4, 2), nullable=True)
    user_role = sa.Column(sa.String(120), nullable=True)
    operation = sa.Column(sa.String(120), nullable=True)

    customer = relationship("Customer", back_populates="billing_policy")


class CustomerDocument(Base):
    __tablename__ = "customer_documents"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    document_type_id = sa.Column(sa.Integer, sa.ForeignKey("document_types.id"), nullable=False)
    file_url = sa.Column(sa.String(1024), nullable=False)
    start_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    status = sa.Column(sa.String(32), nullable=False, server_default="Active")

    customer = relationship("Customer", back_populates="documents")


class CustomerRateCard(Base):
    """Experience-band pricing quoted by a customer (0076).

    One row per band (1–2 yrs, 2–3 yrs, … up to 15). Every rate column is
    NULLABLE on purpose: most customers quote only the unit they bill in —
    hourly for one, monthly for another — and a blank cell must read as "not
    quoted", never as zero. The Opportunity form's Candidate CTC Slab pulls
    the column matching the opportunity's billing type.
    """
    __tablename__ = "customer_rate_cards"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"),
                            nullable=False, index=True)
    #: Branch-wise since 0077. NULL = customer-wide default; the Opportunity
    #: form prefers the selected branch's rows and falls back to NULL rows.
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"),
                          nullable=True, index=True)
    exp_min = sa.Column(sa.Numeric(4, 1), nullable=False)   # years
    exp_max = sa.Column(sa.Numeric(4, 1), nullable=False)
    #: Slab VERSIONING (0079): the ladder applies FROM this date. A newer
    #: ladder (later effective_from <= today) supersedes the whole older one —
    #: nothing is deleted, so old opportunities stay explainable. NULL =
    #: legacy rows, treated as effective since forever.
    effective_from = sa.Column(sa.Date, nullable=True)
    rate_hourly = sa.Column(sa.Numeric(12, 2), nullable=True)
    rate_daily = sa.Column(sa.Numeric(12, 2), nullable=True)
    rate_weekly = sa.Column(sa.Numeric(12, 2), nullable=True)
    rate_monthly = sa.Column(sa.Numeric(12, 2), nullable=True)
    rate_yearly = sa.Column(sa.Numeric(12, 2), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now())
    __table_args__ = (
        sa.UniqueConstraint("customer_id", "branch_id", "effective_from",
                            "exp_min", "exp_max", name="uq_rate_card_band"),
        sa.CheckConstraint("exp_max > exp_min", name="ck_rate_card_band_order"),
    )

    customer = relationship("Customer")


class ContactPerson(Base):
    __tablename__ = "contact_persons"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    name = sa.Column(sa.String(255), nullable=False)
    email = sa.Column(sa.String(255), nullable=True)
    phone = sa.Column(sa.String(32), nullable=True)
    designation = sa.Column(sa.String(120), nullable=True)
    # Finance | Operational | Procurement | HR (string; nullable)
    role = sa.Column(sa.String(40), nullable=True)
    # Primary | Secondary (nullable)
    contact_priority = sa.Column(sa.String(40), nullable=True)
    # Email | SMS | Both | None (nullable)
    notification = sa.Column(sa.String(40), nullable=True)
    is_hiring_manager = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    customer = relationship("Customer", back_populates="contacts")
