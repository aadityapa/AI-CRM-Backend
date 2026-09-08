"""Monthly payroll extract — the attendance facts salary processing needs.

The app already computes every number payroll asks for (days worked, paid
leave, loss of pay, comp-off, week-offs, holidays) while producing timesheets
and invoices. Until now none of it left the system: whoever runs salary had to
open each timesheet and copy figures by hand.

This module aggregates those numbers ONE MONTH AT A TIME in two shapes:

* per (employee x project) — matching how timesheets are actually filed, and
* per employee — the shape a payroll sheet wants, because one person on two
  projects gets ONE salary.

Loss of Pay is the number that must never be wrong: it is the only field here
that directly reduces someone's pay. It is taken from
``timesheet_summary["total_loss_of_pay_days"]`` — the same figure the timesheet
screen and the invoice preview show — rather than recomputed, so payroll can
never disagree with what the employee and the client already saw.

Timesheets that are not APPROVED are included but FLAGGED, never silently
dropped: a missing timesheet is a payroll problem, and hiding it would turn it
into a silent one.
"""
from __future__ import annotations

import csv
import io
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Employee, Project, Timesheet, TimesheetEntry, TimesheetStatus

logger = logging.getLogger("karnex.payroll")


def _num(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _employee_name(emp: Employee | None) -> str:
    if emp is None:
        return ""
    parts = [getattr(emp, "first_name", ""), getattr(emp, "last_name", "")]
    return " ".join(p for p in parts if p).strip() or (emp.email or f"Employee #{emp.id}")


def payroll_rows(db: Session, month: int, year: int) -> list[dict]:
    """One row per (employee x project) timesheet for the period."""
    from services.timesheets import timesheet_summary

    sheets = db.execute(
        select(Timesheet, Employee, Project)
        .join(Employee, Employee.id == Timesheet.employee_id, isouter=True)
        .join(Project, Project.id == Timesheet.project_id, isouter=True)
        .where(Timesheet.month == month, Timesheet.year == year)
        .order_by(Employee.first_name, Employee.last_name, Project.name)
    ).all()

    # One query for every entry in the period instead of one per timesheet.
    ts_ids = [ts.id for ts, _, _ in sheets]
    entries_by_ts: dict[int, list[TimesheetEntry]] = {i: [] for i in ts_ids}
    if ts_ids:
        for entry in db.execute(
            select(TimesheetEntry)
            .where(TimesheetEntry.timesheet_id.in_(ts_ids))
            .order_by(TimesheetEntry.entry_date)
        ).scalars():
            entries_by_ts.setdefault(entry.timesheet_id, []).append(entry)

    rows: list[dict] = []
    for ts, emp, project in sheets:
        status = getattr(ts.status, "value", ts.status)
        try:
            summary = timesheet_summary(db, ts, entries_by_ts.get(ts.id) or [])
        except Exception as exc:
            # A single broken timesheet must not sink the whole payroll run —
            # report it as an error row so it is visible, not missing.
            logger.warning("payroll.summary_failed timesheet=%s: %s", ts.id, exc)
            summary = {}
        rows.append({
            "timesheet_id": ts.id,
            "status": status,
            "approved": status == TimesheetStatus.APPROVED.value,
            "employee_id": ts.employee_id,
            "employee_code": getattr(emp, "employee_code", None) if emp else None,
            "employee_name": _employee_name(emp),
            "employee_email": getattr(emp, "email", None) if emp else None,
            "project_id": ts.project_id,
            "project_name": getattr(project, "name", None) if project else None,
            "total_days": _num(summary.get("total_days")),
            "working_days": _num(summary.get("working_days")),
            "days_worked": _num(summary.get("total_no_of_days_worked")),
            "present_days": _num(summary.get("present_days")),
            "absent_days": _num(summary.get("absent_days")),
            "half_days": _num(summary.get("half_days")),
            "leave_days": _num(summary.get("total_leave_days")),
            "loss_of_pay_days": _num(summary.get("total_loss_of_pay_days")),
            "comp_off_days": _num(summary.get("comp_off_days")),
            "week_offs": _num(summary.get("total_week_off")),
            "holidays": _num(summary.get("holidays")),
            "hours_worked": _num(summary.get("total_hours_worked")),
            "billable_days": _num(summary.get("actual_billable_days")),
            "billable_hours": _num(summary.get("actual_billable_hours")),
        })
    return rows


def payroll_by_employee(rows: list[dict]) -> list[dict]:
    """Collapse to ONE row per employee — a person on two projects still gets
    one salary. Day counts are summed; the calendar figures (week-offs,
    holidays, total days) are taken as the MAXIMUM rather than the sum, since
    two projects share the same calendar month and adding them would invent
    days that do not exist."""
    merged: dict[int, dict] = {}
    SUM_KEYS = ("days_worked", "present_days", "absent_days", "half_days", "leave_days",
                "loss_of_pay_days", "comp_off_days", "hours_worked",
                "billable_days", "billable_hours")
    MAX_KEYS = ("total_days", "working_days", "week_offs", "holidays")
    for row in rows:
        key = row["employee_id"]
        agg = merged.get(key)
        if agg is None:
            agg = {
                "employee_id": key,
                "employee_code": row["employee_code"],
                "employee_name": row["employee_name"],
                "employee_email": row["employee_email"],
                "projects": [],
                "timesheet_count": 0,
                "all_approved": True,
                "pending_statuses": [],
                **{k: 0.0 for k in SUM_KEYS},
                **{k: 0.0 for k in MAX_KEYS},
            }
            merged[key] = agg
        if row["project_name"]:
            agg["projects"].append(row["project_name"])
        agg["timesheet_count"] += 1
        if not row["approved"]:
            agg["all_approved"] = False
            agg["pending_statuses"].append(f"{row['project_name'] or row['project_id']}: {row['status']}")
        for k in SUM_KEYS:
            agg[k] = round(agg[k] + row[k], 2)
        for k in MAX_KEYS:
            agg[k] = max(agg[k], row[k])
    out = list(merged.values())
    for agg in out:
        agg["projects"] = ", ".join(dict.fromkeys(agg["projects"]))
        agg["pending"] = "; ".join(agg.pop("pending_statuses"))
    out.sort(key=lambda r: (r["employee_name"] or "").lower())
    return out


CSV_COLUMNS = [
    ("employee_code", "Employee ID"),
    ("employee_name", "Employee Name"),
    ("employee_email", "Email"),
    ("projects", "Projects"),
    ("total_days", "Calendar Days"),
    ("working_days", "Working Days"),
    ("days_worked", "Days Worked"),
    ("present_days", "Present"),
    ("half_days", "Half Days"),
    ("absent_days", "Absent"),
    ("leave_days", "Leave Days"),
    ("loss_of_pay_days", "Loss of Pay Days"),
    ("comp_off_days", "Comp Off Days"),
    ("week_offs", "Week Offs"),
    ("holidays", "Holidays"),
    ("hours_worked", "Hours Worked"),
    ("billable_days", "Billable Days"),
    ("timesheet_count", "Timesheets"),
    ("pending", "Not Approved"),
]


def payroll_csv(rows: list[dict]) -> str:
    """CSV for the payroll/accounting hand-off. Excel-friendly (CRLF)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([label for _, label in CSV_COLUMNS])
    for row in rows:
        writer.writerow([row.get(key, "") for key, _ in CSV_COLUMNS])
    return buf.getvalue()
