"""HR leave modules: holiday calendar, customer leave policies,
leave applications and the leave accrual/consumption event ledger.

All tables here are ADDITIVE (migration 0021) — nothing existing is altered.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK


class HolidayName(Base):
    """Master list of holiday names (Customer Holiday Calendar spec §1)."""

    __tablename__ = "holiday_names"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


class Holiday(Base):
    """Company/customer/branch holiday calendar row.

    customer_id NULL  = applies to every customer (e.g. national holiday).
    branch_id   NULL  = applies to every branch of the customer (or globally
    when customer_id is also NULL). Rows are soft-deactivated (is_active),
    never hard-deleted.
    """

    __tablename__ = "holidays"
    id = sa.Column(sa.Integer, primary_key=True)
    holiday_name_id = sa.Column(sa.Integer, sa.ForeignKey("holiday_names.id"), nullable=True, index=True)
    name = sa.Column(sa.String(120), nullable=False)
    holiday_date = sa.Column(sa.Date, nullable=False, index=True)
    holiday_type = sa.Column(sa.String(24), nullable=False,
                             server_default="National")  # National | Regional | Customer
    observance = sa.Column(sa.String(16), nullable=False,
                           server_default="Mandatory")  # Mandatory | Optional
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=True, index=True)
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    holiday_calendar_id = sa.Column(sa.Integer, sa.ForeignKey("branch_holiday_years.id"),
                                    nullable=True, index=True)
    year = sa.Column(sa.Integer, nullable=False)  # denormalized from holiday_date
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    holiday_name = relationship("HolidayName")
    holiday_calendar = relationship("BranchHolidayYear", back_populates="holidays")


class CustomerLeavePolicy(Base):
    """Per-customer (optionally per-branch) leave crediting rules for one leave type."""

    __tablename__ = "customer_leave_policies"
    id = sa.Column(sa.Integer, primary_key=True)
    customer_id = sa.Column(sa.Integer, sa.ForeignKey("customers.id"), nullable=False, index=True)
    branch_id = sa.Column(sa.Integer, sa.ForeignKey("customer_branches.id"), nullable=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    # 64 chars: UI labels like "Credit Balance Every Month" exceed the old VARCHAR(24).
    leave_credit_type = sa.Column(sa.String(64), nullable=False,
                                  server_default="Monthly")  # Monthly|…|Credit Balance Every Month
    leave_expire = sa.Column(sa.String(24), nullable=True)  # spec §5 dropdown, e.g. "Days"; NULL = no expiry
    is_max_limit = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    max_limit = sa.Column(sa.Numeric(5, 2), nullable=True)
    prorate_balance_credit = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    leave_credit_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    initial_credit_balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    maximum_carry_forward = sa.Column(sa.Numeric(5, 2), nullable=True)
    leave_credit_timing = sa.Column(sa.String(24), nullable=False,
                                    server_default="Start_Of_Period")  # Start_Of_Period|End_Of_Period
    # When within the expiry cycle (leave_expire Monthly/Quarterly/Yearly) the
    # unused remainder lapses: Start_Of_Period|End_Of_Period. Batch rollovers
    # apply either at the cycle boundary; the field records the contractual
    # intent and is surfaced on PE leave details.
    leave_expire_timing = sa.Column(sa.String(24), nullable=True)
    effective_date = sa.Column(sa.Date, nullable=True)
    is_billable = sa.Column(sa.Boolean, nullable=True)  # spec §5 "Billable Leave Policy" flag; NULL = unset
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    __table_args__ = (sa.UniqueConstraint("customer_id", "branch_id", "leave_type_id",
                                          name="uq_customer_leave_policy"),)

    customer = relationship("Customer")
    leave_type = relationship("LeavePolicyType")
    concepts = relationship("LeaveCreditConcept", back_populates="policy",
                            cascade="all, delete-orphan", order_by="LeaveCreditConcept.id")


class LeaveCreditConcept(Base):
    """Date-ranged credit concept rows under a CustomerLeavePolicy (subform grid)."""

    __tablename__ = "leave_credit_concepts"
    id = sa.Column(sa.Integer, primary_key=True)
    policy_id = sa.Column(sa.Integer,
                          sa.ForeignKey("customer_leave_policies.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    from_date = sa.Column(sa.Date, nullable=True)
    end_date = sa.Column(sa.Date, nullable=True)
    balance = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())

    policy = relationship("CustomerLeavePolicy", back_populates="concepts")


class LeaveApplication(Base):
    """Employee leave request with an HR approval workflow.

    days is SERVER-computed: Multi_Day = inclusive day count, Half_Day = 0.5,
    Full_Day = 1. Approval consumes the EmployeeLeaveBalance for the year of
    from_date and writes a LeaveAccrualEvent ledger row.
    """

    __tablename__ = "leave_applications"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=True, index=True)
    project_employee_id = sa.Column(sa.Integer, sa.ForeignKey("project_employees.id"),
                                    nullable=True, index=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    leave_period_type = sa.Column(sa.String(16), nullable=False,
                                  server_default="Full_Day")  # Full_Day|Half_Day|Multi_Day
    from_date = sa.Column(sa.Date, nullable=False)
    to_date = sa.Column(sa.Date, nullable=False)  # == from_date for single-day requests
    days = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    comp_off_type = sa.Column(sa.String(24), nullable=True)  # Earned|Consumed (comp-off type only)
    reason = sa.Column(sa.Text, nullable=True)
    status = sa.Column(sa.String(16), nullable=False, server_default="Pending",
                       index=True)  # Pending|Approved|Rejected|Cancelled
    rejection_reason = sa.Column(sa.Text, nullable=True)
    applied_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    decided_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    decided_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)

    employee = relationship("Employee")
    leave_type = relationship("LeavePolicyType")
    project = relationship("Project")
    project_employee = relationship("ProjectEmployee")


class LeaveAccrualEvent(Base):
    """Append-only ledger of every leave balance movement (credit or debit)."""

    __tablename__ = "leave_accrual_events"
    id = sa.Column(sa.Integer, primary_key=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    leave_type_id = sa.Column(sa.Integer, sa.ForeignKey("leave_policy_types.id"), nullable=False)
    event_type = sa.Column(sa.String(24),
                           nullable=False)  # Accrual|Consumption|Comp_Off_Credit|Adjustment|Carry_Forward
    amount = sa.Column(sa.Numeric(5, 2), nullable=False)  # positive credit, negative debit
    balance_after = sa.Column(sa.Numeric(5, 2), nullable=False, server_default="0")
    source = sa.Column(sa.String(64), nullable=True)  # e.g. "timesheet:12", "leave_application:5"
    note = sa.Column(sa.String(255), nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    employee = relationship("Employee")
    leave_type = relationship("LeavePolicyType")
