"""Timesheets and daily entries (attendance-driven billing source)."""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK, pg_enum


class TimesheetStatus(str, enum.Enum):
    DRAFT = "Draft"
    SUBMITTED = "Submitted"
    APPROVED = "Approved"
    REJECTED = "Rejected"


class AttendanceStatus(str, enum.Enum):
    PRESENT = "Present"
    ABSENT = "Absent"
    HALF_DAY = "Half_Day"
    LEAVE = "Leave"
    HOLIDAY = "Holiday"
    WEEK_OFF = "Week_Off"


class DayType(str, enum.Enum):
    WORKING = "Working"
    WEEK_OFF = "Week_Off"
    HOLIDAY = "Holiday"


class LeavePeriod(str, enum.Enum):
    FULL = "Full"
    HALF_AM = "Half_AM"
    HALF_PM = "Half_PM"


class EntryLocation(str, enum.Enum):
    REMOTE = "Remote"
    ONSITE = "Onsite"


class Timesheet(TimestampMixin, Base):
    __tablename__ = "timesheets"
    id = sa.Column(sa.Integer, primary_key=True)
    project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=False, index=True)
    employee_id = sa.Column(sa.Integer, sa.ForeignKey("employees.id"), nullable=False, index=True)
    project_employee_id = sa.Column(sa.Integer, sa.ForeignKey("project_employees.id"),
                                    nullable=True, index=True)
    month = sa.Column(sa.Integer, nullable=False)  # 1..12
    year = sa.Column(sa.Integer, nullable=False)
    status = sa.Column(pg_enum(TimesheetStatus, "timesheet_status"), nullable=False,
                       server_default=TimesheetStatus.DRAFT.value, index=True)
    rejection_reason = sa.Column(sa.Text, nullable=True)
    submitted_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    approved_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    approved_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    file_attachment_url = sa.Column(sa.String(1024), nullable=True)
    # Comp-off credit already granted for this timesheet (idempotency marker:
    # re-approval after reject/resubmit applies only the delta vs this value).
    #: Editable period override (0073). NULL = derived from the PE's
    #: onboarding/exit window. Only meaningful within the sheet's month.
    period_start_date = sa.Column(sa.Date, nullable=True)
    period_end_date = sa.Column(sa.Date, nullable=True)
    comp_off_accrued = sa.Column(sa.Numeric(5, 2), nullable=True)
    #: Frozen invoice figures (0075): the line items + sub-total + summary as
    #: they stood at APPROVAL. Every read recomputes billables from the
    #: CURRENT policy, so without this a policy edit after approval silently
    #: changed what an approved sheet would invoice. The reviewer approved
    #: THESE numbers; generate-invoice bills them and flags any live drift.
    #: NULL = not approved yet (or legacy pre-0075 approval → live figures).
    approved_figures = sa.Column(JSONB, nullable=True)
    __table_args__ = (
        sa.UniqueConstraint("project_id", "employee_id", "month", "year", name="uq_timesheet_period"),
        sa.CheckConstraint("month >= 1 AND month <= 12", name="ck_timesheet_month"),
    )

    entries = relationship("TimesheetEntry", back_populates="timesheet", cascade="all, delete-orphan",
                           order_by="TimesheetEntry.entry_date")
    attachments = relationship("TimesheetAttachment", back_populates="timesheet", cascade="all, delete-orphan",
                               order_by="TimesheetAttachment.uploaded_at")
    project = relationship("Project")
    employee = relationship("Employee")
    project_employee = relationship("ProjectEmployee")


class TimesheetAttachment(Base):
    __tablename__ = "timesheet_attachments"
    id = sa.Column(sa.Integer, primary_key=True)
    timesheet_id = sa.Column(sa.Integer, sa.ForeignKey("timesheets.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    file_url = sa.Column(sa.String(1024), nullable=False)
    file_name = sa.Column(sa.String(512), nullable=True)
    file_sha256 = sa.Column(sa.String(64), nullable=True)
    file_size = sa.Column(sa.Integer, nullable=True)
    kind = sa.Column(sa.String(64), nullable=True)
    uploaded_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    uploaded_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    timesheet = relationship("Timesheet", back_populates="attachments")


class TimesheetActivityLog(Base):
    """Per-timesheet audit trail (po_activity_log style, migration 0021)."""

    __tablename__ = "timesheet_activity_log"
    id = sa.Column(sa.Integer, primary_key=True)
    timesheet_id = sa.Column(sa.Integer, sa.ForeignKey("timesheets.id"), nullable=False, index=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False)
    action_type = sa.Column(sa.String(64), nullable=False)
    comment = sa.Column(sa.Text, nullable=True)
    timestamp = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    timesheet = relationship("Timesheet")


class TimesheetEntry(Base):
    __tablename__ = "timesheet_entries"
    id = sa.Column(sa.Integer, primary_key=True)
    timesheet_id = sa.Column(sa.Integer, sa.ForeignKey("timesheets.id"), nullable=False, index=True)
    entry_date = sa.Column(sa.Date, nullable=False)
    day_of_week = sa.Column(sa.String(12), nullable=True)
    is_working = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    hours_worked = sa.Column(sa.Numeric(4, 2), nullable=False, server_default="0")
    attendance_status = sa.Column(pg_enum(AttendanceStatus, "attendance_status"), nullable=False,
                                  server_default=AttendanceStatus.PRESENT.value)
    leave_type = sa.Column(sa.String(64), nullable=True)  # casual / sick / earned / comp-off / ...
    leave_period = sa.Column(pg_enum(LeavePeriod, "leave_period"), nullable=True)
    leave_reason = sa.Column(sa.String(255), nullable=True)  # optional note from Apply-leave dialog
    billable_hours = sa.Column(sa.Numeric(4, 2), nullable=False, server_default="0")
    billable_days = sa.Column(sa.Numeric(3, 2), nullable=False, server_default="0")
    location = sa.Column(pg_enum(EntryLocation, "entry_location"), nullable=True)
    day_type = sa.Column(pg_enum(DayType, "day_type"), nullable=False,
                         server_default=DayType.WORKING.value)
    view_flag = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    entry_project_id = sa.Column(sa.Integer, sa.ForeignKey("projects.id"), nullable=True, index=True)
    __table_args__ = (sa.UniqueConstraint("timesheet_id", "entry_date", name="uq_timesheet_entry_date"),)

    timesheet = relationship("Timesheet", back_populates="entries")
    split_project = relationship("Project", foreign_keys=[entry_project_id])
