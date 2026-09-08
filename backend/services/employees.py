"""Employee module service helpers: lookups (404s), validation, serialization."""
from __future__ import annotations

import calendar
from datetime import date, datetime

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    CandidateProfile,
    Customer,
    Department,
    Designation,
    Employee,
    EmployeeEducation,
    EmployeeExperienceDetail,
    EmployeeLeaveBalance,
    EmployeeProjectHistory,
    Invoice,
    LeaveAccrualEvent,
    LeaveApplication,
    LeavePolicyType,
    Project,
    ProjectEmployee,
    Role,
    Timesheet,
    TimesheetEntry,
    TimesheetStatus,
    UserRole,
)


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _num(value):
    return float(value) if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


def full_name(emp: Employee) -> str:
    return " ".join(part for part in (emp.first_name, emp.last_name) if part)


# ---------------------------------------------------------------------------
# Lookups / validation
# ---------------------------------------------------------------------------

def get_employee_or_404(db: Session, employee_id: int) -> Employee:
    emp = db.get(Employee, employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    return emp


def ensure_unique_email(db: Session, email: str, exclude_id: int | None = None) -> None:
    stmt = select(Employee.id).where(sa.func.lower(Employee.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400, detail=f"Employee email '{email}' already exists")


def validate_user_link(db: Session, user_id: int | None,
                       exclude_employee_id: int | None = None) -> None:
    """Optional link to the legacy users table (registration_data) — checked via raw SQL."""
    if user_id is None:
        return
    row = db.execute(
        sa.text("SELECT id FROM registration_data WHERE id = :uid"), {"uid": user_id}
    ).first()
    if not row:
        raise HTTPException(status_code=400, detail="user_id does not exist in registration_data")
    stmt = select(Employee.id).where(Employee.user_id == user_id)
    if exclude_employee_id is not None:
        stmt = stmt.where(Employee.id != exclude_employee_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400, detail="user_id is already linked to another employee")


def validate_department(db: Session, department_id: int | None) -> None:
    if department_id is not None and db.get(Department, department_id) is None:
        raise HTTPException(status_code=400, detail="Invalid department_id")


def validate_designation(db: Session, designation_id: int | None) -> None:
    if designation_id is not None and db.get(Designation, designation_id) is None:
        raise HTTPException(status_code=400, detail="Invalid designation_id")


def validate_manager(db: Session, manager_id: int | None, field: str,
                     self_id: int | None = None) -> None:
    if manager_id is None:
        return
    if self_id is not None and manager_id == self_id:
        raise HTTPException(status_code=400, detail=f"{field} cannot be the employee itself")
    if db.get(Employee, manager_id) is None:
        raise HTTPException(status_code=400, detail=f"Invalid {field}")


def validate_leave_type(db: Session, leave_type_id: int) -> LeavePolicyType:
    lt = db.get(LeavePolicyType, leave_type_id)
    if lt is None:
        raise HTTPException(status_code=400, detail=f"Invalid leave_type_id {leave_type_id}")
    return lt


def ensure_unique_employee_code(db: Session, code: str, exclude_id: int | None = None) -> None:
    stmt = select(Employee.id).where(sa.func.lower(Employee.employee_code) == code.lower())
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    if db.execute(stmt).first():
        raise HTTPException(status_code=400,
                            detail=f"employee_code '{code}' already exists")


def validate_candidate_profile(db: Session, candidate_profile_id: int | None) -> None:
    if candidate_profile_id is not None and db.get(CandidateProfile, candidate_profile_id) is None:
        raise HTTPException(status_code=400, detail="Invalid candidate_profile_id")


def get_education_or_404(db: Session, education_id: int) -> EmployeeEducation:
    row = db.get(EmployeeEducation, education_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Education record not found")
    return row


def get_experience_or_404(db: Session, experience_id: int) -> EmployeeExperienceDetail:
    row = db.get(EmployeeExperienceDetail, experience_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Experience record not found")
    return row


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def current_experience_years(emp: Employee) -> float | None:
    """Years from date_of_joining to today, 1 decimal (None without a DOJ)."""
    if not emp.date_of_joining:
        return None
    days = (date.today() - emp.date_of_joining).days
    return round(days / 365.25, 1) if days > 0 else 0.0


def user_role_names(db: Session, user_id: int | None) -> list[str]:
    """CRM role names for the linked login user (empty when unlinked)."""
    if not user_id:
        return []
    rows = db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    ).scalars().all()
    return sorted(_ev(r) for r in rows)


def employee_projects(db: Session, employee_id: int) -> list[dict]:
    """Projects section of the master form: live ProjectEmployee assignments."""
    rows = db.execute(
        select(ProjectEmployee, Project.name)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .where(ProjectEmployee.employee_id == employee_id)
        .order_by(ProjectEmployee.id)
    ).all()
    return [{
        "project_id": pe.project_id,
        "project_name": name,
        "onboarding_date": _iso(pe.onboarding_date),
        "experience_years": _num(pe.experience_years),
        "work_mode": _ev(pe.work_mode),
        "is_active": pe.is_active,
        "billing_unit": _ev(pe.billing_unit),
    } for pe, name in rows]


def serialize_employee(emp: Employee, db: Session | None = None, detail: bool = False) -> dict:
    data = {
        "id": emp.id,
        "user_id": emp.user_id,
        "first_name": emp.first_name,
        "last_name": emp.last_name,
        "full_name": full_name(emp),
        "email": emp.email,
        "phone": emp.phone,
        "department_id": emp.department_id,
        "designation_id": emp.designation_id,
        "reporting_manager_id": emp.reporting_manager_id,
        "reporting_hr_id": emp.reporting_hr_id,
        "profile_type": _ev(emp.profile_type),
        "portal_access": emp.portal_access,
        "date_of_joining": _iso(emp.date_of_joining),
        "is_active": emp.is_active,
        "role_title": emp.role_title,
    }
    if detail and db is not None:
        data["pan"] = emp.pan
        data["aadhar"] = emp.aadhar
        data["bank_account_details"] = emp.bank_account_details
        dept = db.get(Department, emp.department_id) if emp.department_id else None
        desig = db.get(Designation, emp.designation_id) if emp.designation_id else None
        manager = db.get(Employee, emp.reporting_manager_id) if emp.reporting_manager_id else None
        hr = db.get(Employee, emp.reporting_hr_id) if emp.reporting_hr_id else None
        data["department_name"] = dept.name if dept else None
        data["designation_name"] = desig.name if desig else None
        data["reporting_manager_name"] = full_name(manager) if manager else None
        data["reporting_hr_name"] = full_name(hr) if hr else None
        # --- Tab 13 master-form fields ---
        data["title"] = emp.title
        data["middle_name"] = emp.middle_name
        data["display_name"] = emp.display_name
        data["personal_email"] = emp.personal_email
        data["gender"] = emp.gender
        data["blood_group"] = emp.blood_group
        data["current_ctc"] = _num(emp.current_ctc)
        data["cv_url"] = emp.cv_url
        data["employee_code"] = emp.employee_code
        data["emergency_number"] = emp.emergency_number
        data["date_of_birth"] = _iso(emp.date_of_birth)
        data["present_address"] = emp.present_address
        data["permanent_address"] = emp.permanent_address
        data["work_location"] = emp.work_location
        data["role_title"] = emp.role_title
        data["skills"] = emp.skills
        data["experience_years"] = _num(emp.experience_years)
        data["employment_type"] = emp.employment_type
        data["is_resigned"] = bool(emp.is_resigned)
        data["date_of_resignation"] = _iso(emp.date_of_resignation)
        data["notice_period_days"] = emp.notice_period_days
        data["last_working_day"] = _iso(emp.last_working_day)
        data["candidate_profile_id"] = emp.candidate_profile_id
        data["min_hours_full_day"] = _num(emp.min_hours_full_day)
        data["min_hours_half_day"] = _num(emp.min_hours_half_day)
        data["normal_hours_per_day"] = _num(emp.normal_hours_per_day)
        # Computed / derived
        data["current_experience_years"] = current_experience_years(emp)
        data["user_roles"] = user_role_names(db, emp.user_id)
        data["projects"] = employee_projects(db, emp.id)
        data["is_exit"] = bool(emp.is_resigned)
        data["exit_date"] = _iso(emp.last_working_day)
    return data


def serialize_leave_balance(row: EmployeeLeaveBalance, leave_type_name: str | None = None) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "leave_type_id": row.leave_type_id,
        "leave_type_name": leave_type_name,
        "year": row.year,
        "accrued": _num(row.accrued),
        "consumed": _num(row.consumed),
        "balance": _num(row.balance),
        "carry_forward": _num(row.carry_forward),
    }


def serialize_project_history(row: EmployeeProjectHistory, project_name: str | None = None) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "project_id": row.project_id,
        "project_name": project_name,
        "start_date": _iso(row.start_date),
        "end_date": _iso(row.end_date),
        "role": row.role,
    }


def serialize_education(row: EmployeeEducation) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "course": row.course,
        "branch_specialization": row.branch_specialization,
        "start_date": _iso(row.start_date),
        "end_date": _iso(row.end_date),
        "university": row.university,
        "city": row.city,
        "certificate_url": row.certificate_url,
    }


def serialize_experience(row: EmployeeExperienceDetail) -> dict:
    return {
        "id": row.id,
        "employee_id": row.employee_id,
        "company_name": row.company_name,
        "job_title": row.job_title,
        "currently_working": bool(row.currently_working),
        "date_of_joining": _iso(row.date_of_joining),
        "date_of_relieving": _iso(row.date_of_relieving),
        "city": row.city,
        "certificate_url": row.certificate_url,
    }


# ---------------------------------------------------------------------------
# Leave matrix (Tab 13 "Leaves" section)
# ---------------------------------------------------------------------------

# (code, label, normalized key) — display order fixed.
# Keys match `_normalize_leave_type_key` output (short stem, e.g. "casual").
LEAVE_MATRIX_ROWS = [
    ("CL", "Casual Leave", "casual"),
    ("SL", "Sick Leave", "sick"),
    ("EL", "Earned Leave", "earned"),
    ("Paternity", "Paternity Leave", "paternity"),
    ("Comp-Off", "Comp-Off", "comp-off"),
    ("Maternity", "Maternity Leave", "maternity"),
]

LOSS_OF_PAY_NAME = "loss of pay"
LOSS_OF_PAY_DISPLAY = "Loss of Pay"


def _normalize_leave_type_key(name: str) -> str:
    """Map LeavePolicyType.name variants to a stable stem for matrix lookup.

    "Casual" / "Casual Leave" → "casual"; "Loss Off Pay" → "loss of pay".
    """
    n = str(name or "").strip().lower().replace("_", " ")
    n = " ".join(n.split())
    if n in ("loss of pay", "loss off pay", "lop"):
        return LOSS_OF_PAY_NAME
    if n in ("comp-off", "comp off", "compoff"):
        return "comp-off"
    if n.endswith(" leave"):
        n = n[: -len(" leave")].strip()
    return n


_LEAVE_CODE_TO_STEM = {code.lower(): key for code, _label, key in LEAVE_MATRIX_ROWS}


def resolve_leave_policy_type(db: Session, name: str) -> LeavePolicyType | None:
    """Resolve a timesheet/UI leave label to a LeavePolicyType row.

    Accepts display names ("Casual Leave"), stems ("casual"), and matrix
    codes ("CL") so approvals/classify do not 400 when entries store a stem.
    """
    raw = (name or "").strip()
    if not raw:
        return None
    key = raw.lower()
    want = _normalize_leave_type_key(raw)
    stem = _LEAVE_CODE_TO_STEM.get(key) or _LEAVE_CODE_TO_STEM.get(want) or want
    candidates: set[str] = {key}
    if want:
        candidates.add(want)
    if stem:
        candidates.add(stem)
        candidates.add(f"{stem} leave")
    lt = db.execute(
        select(LeavePolicyType).where(
            sa.func.lower(LeavePolicyType.name).in_(tuple(sorted(candidates)))
        )
    ).scalars().first()
    if lt is not None:
        return lt
    if not stem:
        return None
    for row in db.execute(select(LeavePolicyType)).scalars().all():
        if _normalize_leave_type_key(row.name or "") == stem:
            return row
    return None


def is_loss_of_pay_name(name: str | None) -> bool:
    return _normalize_leave_type_key(name or "") == LOSS_OF_PAY_NAME


def is_comp_off_name(name: str | None) -> bool:
    return _normalize_leave_type_key(name or "") == "comp-off"


def ensure_loss_of_pay_type(db: Session) -> LeavePolicyType:
    """Lookup-or-create the canonical seeded Loss of Pay leave type."""
    row = db.execute(
        select(LeavePolicyType).where(
            sa.func.lower(LeavePolicyType.name).in_(
                ("loss of pay", "loss off pay", "lop")
            )
        )
    ).scalars().first()
    if row is not None:
        if (row.name or "").strip() != LOSS_OF_PAY_DISPLAY:
            row.name = LOSS_OF_PAY_DISPLAY
        return row
    row = LeavePolicyType(
        name=LOSS_OF_PAY_DISPLAY,
        accrual_rule="Unpaid — deducted from salary",
        carry_forward_rule="Not applicable",
    )
    db.add(row)
    db.flush()
    return row


def leave_matrix(db: Session, employee_id: int, year: int) -> dict:
    """Fixed-order leave matrix for one employee/year.

    Carry-forward semantics: in this codebase a year-row's carry_forward is the
    amount brought INTO that year from the previous year (the leave-balances
    upsert computes balance = accrued + carry_forward - consumed). So the
    "carry forward from last year" figures read the requested year's own row.
    Missing leave types serialize as zeros. loss_of_pay is the consumed figure
    of the "Loss of Pay" leave type (seeded; unpaid leave, salary-deducted).
    """
    rows = db.execute(
        select(EmployeeLeaveBalance, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == EmployeeLeaveBalance.leave_type_id)
        .where(EmployeeLeaveBalance.employee_id == employee_id,
               EmployeeLeaveBalance.year == year)
    ).all()
    by_name = {_normalize_leave_type_key(name): bal for bal, name in rows}

    matrix_rows = []
    for code, label, key in LEAVE_MATRIX_ROWS:
        bal = by_name.get(key)
        matrix_rows.append({
            "code": code,
            "label": label,
            "accrual": _num(bal.accrued) if bal else 0.0,
            "consumed": _num(bal.consumed) if bal else 0.0,
            "balance": _num(bal.balance) if bal else 0.0,
        })

    el = by_name.get("earned")
    comp_off = by_name.get("comp-off")
    lop = by_name.get(LOSS_OF_PAY_NAME)
    return {
        "rows": matrix_rows,
        "el_carry_forward_last_year": _num(el.carry_forward) if el else 0.0,
        "comp_off_carry_forward_last_year": _num(comp_off.carry_forward) if comp_off else 0.0,
        "loss_of_pay": _num(lop.consumed) if lop else 0.0,
    }


def project_names(db: Session, project_ids: set[int]) -> dict[int, str]:
    if not project_ids:
        return {}
    rows = db.execute(select(Project.id, Project.name).where(Project.id.in_(project_ids))).all()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# Employee 360-degree history (GET /api/employees/{id}/history)
# ---------------------------------------------------------------------------

def _short_period(year: int, month: int) -> str:
    """Abbreviated period label, e.g. "Aug 2026"."""
    return f"{calendar.month_abbr[month]} {year}"


def _as_date(value):
    """date | datetime | None -> date | None (for timeline sorting)."""
    if isinstance(value, datetime):
        return value.date()
    return value


def employee_history(db: Session, emp: Employee) -> dict:
    """Aggregate the full 360-degree history for one employee.

    Read-only; a fixed number of queries regardless of data volume:
      1. project assignments (+ project + customer, single joined select),
      2. timesheets (+ project title, single joined select),
      3. per-timesheet entry totals (ONE grouped aggregate over TimesheetEntry),
      4. invoices linked via Invoice.timesheet_id IN (employee timesheet ids),
      5. leave matrix (existing helper, one select),
      6. leave applications (+ type name, single joined select),
      7. leave accrual events (+ type name, single joined select, cap 100),
      8. education rows, 9. experience rows.
    """
    today = date.today()
    is_exit = bool(emp.is_resigned)
    exit_date = emp.last_working_day
    tenure_end = exit_date if (is_exit and exit_date) else today
    tenure_days = (tenure_end - emp.date_of_joining).days if emp.date_of_joining else None

    dept = db.get(Department, emp.department_id) if emp.department_id else None
    desig = db.get(Designation, emp.designation_id) if emp.designation_id else None
    employee_block = {
        "id": emp.id,
        "employee_code": emp.employee_code,
        "full_name": full_name(emp),
        "designation": desig.name if desig else None,
        "department": dept.name if dept else None,
        "date_of_joining": _iso(emp.date_of_joining),
        "employment_type": emp.employment_type,
        "is_exit": is_exit,
        "exit_date": _iso(exit_date),
        "last_working_day": _iso(emp.last_working_day),
        "notice_period_days": emp.notice_period_days,
        "tenure_days": tenure_days,
    }

    # ------------------------------------------------------------- projects
    assignment_rows = db.execute(
        select(ProjectEmployee, Project, Customer.name)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .outerjoin(Customer, Customer.id == Project.customer_id)
        .where(ProjectEmployee.employee_id == emp.id)
        .order_by(sa.nullslast(sa.desc(ProjectEmployee.onboarding_date)),
                  ProjectEmployee.id.desc())
    ).all()
    projects = [{
        "assignment_id": pe.id,
        "project_id": pe.project_id,
        "project_title": project.name,
        "customer_name": customer_name,
        "status": _ev(project.status),
        "onboarding_date": _iso(pe.onboarding_date),
        "billing_date": _iso(pe.billing_date),
        "billing_rate": _num(pe.billing_rate),
        "billing_unit": _ev(pe.billing_unit),
        "work_mode": _ev(pe.work_mode),
        "is_active": bool(pe.is_active),
        "is_exit": bool(pe.is_exit),
        "exit_date": _iso(pe.exit_date),
    } for pe, project, customer_name in assignment_rows]

    # ----------------------------------------------------------- timesheets
    ts_rows = db.execute(
        select(Timesheet, Project.name)
        .outerjoin(Project, Project.id == Timesheet.project_id)
        .where(Timesheet.employee_id == emp.id)
        .order_by(Timesheet.year.desc(), Timesheet.month.desc(), Timesheet.id.desc())
    ).all()
    ts_ids = [ts.id for ts, _ in ts_rows]

    # Single grouped aggregate over TimesheetEntry — no per-timesheet queries.
    entry_totals: dict[int, tuple] = {}
    if ts_ids:
        agg_rows = db.execute(
            select(
                TimesheetEntry.timesheet_id,
                sa.func.coalesce(sa.func.sum(TimesheetEntry.hours_worked), 0),
                sa.func.coalesce(sa.func.sum(TimesheetEntry.billable_hours), 0),
                sa.func.coalesce(sa.func.sum(TimesheetEntry.billable_days), 0),
            )
            .where(TimesheetEntry.timesheet_id.in_(ts_ids))
            .group_by(TimesheetEntry.timesheet_id)
        ).all()
        entry_totals = {row[0]: (row[1], row[2], row[3]) for row in agg_rows}

    timesheets = []
    ts_by_id: dict[int, Timesheet] = {}
    ts_project_title: dict[int, str | None] = {}
    for ts, project_title in ts_rows:
        ts_by_id[ts.id] = ts
        ts_project_title[ts.id] = project_title
        hours, b_hours, b_days = entry_totals.get(ts.id, (0, 0, 0))
        timesheets.append({
            "id": ts.id,
            "month": ts.month,
            "year": ts.year,
            "period_label": _short_period(ts.year, ts.month),
            "project_id": ts.project_id,
            "project_title": project_title,
            "status": _ev(ts.status),
            "hours_worked": _num(hours),
            "billable_hours": _num(b_hours),
            "billable_days": _num(b_days),
            "comp_off_accrued": _num(ts.comp_off_accrued),
            "approved_at": ts.approved_at.isoformat() if ts.approved_at else None,
        })

    # -------------------------------------------------------------- billing
    invoice_rows = []
    if ts_ids:
        invoice_rows = db.execute(
            select(Invoice)
            .where(Invoice.timesheet_id.in_(ts_ids))
            .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())
        ).scalars().all()
    invoices = []
    total_invoiced = 0.0
    for inv in invoice_rows:
        linked_ts = ts_by_id.get(inv.timesheet_id)
        invoices.append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "invoice_date": _iso(inv.invoice_date),
            "period_label": _short_period(linked_ts.year, linked_ts.month) if linked_ts else None,
            "grand_total": _num(inv.grand_total),
            "payment_status": _ev(inv.payment_status),
        })
        total_invoiced += float(inv.grand_total or 0)
    billing = {
        "total_billable_hours": round(sum(float(t[1]) for t in entry_totals.values()), 2),
        "total_billable_days": round(sum(float(t[2]) for t in entry_totals.values()), 2),
        "total_invoiced_amount": round(total_invoiced, 2),
        "invoices": invoices,
    }

    # ---------------------------------------------------------------- leave
    balances = leave_matrix(db, emp.id, today.year)
    application_rows = db.execute(
        select(LeaveApplication, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveApplication.leave_type_id)
        .where(LeaveApplication.employee_id == emp.id)
        .order_by(LeaveApplication.from_date.desc(), LeaveApplication.id.desc())
    ).all()
    applications = [{
        "id": la.id,
        "leave_type_name": type_name,
        "leave_period_type": la.leave_period_type,
        "from_date": _iso(la.from_date),
        "to_date": _iso(la.to_date),
        "days": _num(la.days),
        "status": la.status,
        "decided_at": la.decided_at.isoformat() if la.decided_at else None,
    } for la, type_name in application_rows]
    event_rows = db.execute(
        select(LeaveAccrualEvent, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == LeaveAccrualEvent.leave_type_id)
        .where(LeaveAccrualEvent.employee_id == emp.id)
        .order_by(LeaveAccrualEvent.created_at.desc(), LeaveAccrualEvent.id.desc())
        .limit(100)
    ).all()
    accrual_events = [{
        "id": ev.id,
        "event_type": ev.event_type,
        "leave_type_name": type_name,
        "amount": _num(ev.amount),
        "balance_after": _num(ev.balance_after),
        "source": ev.source,
        "note": ev.note,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    } for ev, type_name in event_rows]

    # ------------------------------------------- education / experience rows
    education_rows = db.execute(
        select(EmployeeEducation).where(EmployeeEducation.employee_id == emp.id)
        .order_by(EmployeeEducation.id)
    ).scalars().all()
    experience_rows = db.execute(
        select(EmployeeExperienceDetail).where(EmployeeExperienceDetail.employee_id == emp.id)
        .order_by(EmployeeExperienceDetail.id)
    ).scalars().all()

    # ------------------------------------------------------------- timeline
    timeline: list[dict] = []

    def add_event(when, event_type: str, label: str) -> None:
        d = _as_date(when)
        if d is None:
            return
        timeline.append({"date": d.isoformat(), "type": event_type, "label": label,
                         "_sort": d})

    add_event(emp.date_of_joining, "JOINED", "Joined the company")
    for pe, project, _customer_name in assignment_rows:
        add_event(pe.onboarding_date, "PROJECT_ONBOARDED", f"Onboarded to {project.name}")
        add_event(pe.billing_date, "PROJECT_BILLING_STARTED",
                  f"Billing started on {project.name}")
        add_event(pe.exit_date, "PROJECT_EXITED", f"Exited {project.name}")
    for ts in ts_by_id.values():
        if ts.status == TimesheetStatus.APPROVED and ts.approved_at:
            title = ts_project_title.get(ts.id) or f"Project #{ts.project_id}"
            add_event(ts.approved_at, "TIMESHEET_APPROVED",
                      f"{_short_period(ts.year, ts.month)} — {title} approved")
    for la, type_name in application_rows:
        if la.status == "Approved":
            add_event(la.from_date, "LEAVE_APPROVED",
                      f"{type_name} approved ({float(la.days or 0):g} day(s))")
    for inv in invoice_rows:
        add_event(inv.invoice_date, "INVOICE_RAISED", f"Invoice {inv.invoice_number} raised")
    add_event(emp.date_of_resignation, "RESIGNED", "Resignation recorded")
    add_event(emp.last_working_day, "LAST_WORKING_DAY", "Last working day")

    timeline.sort(key=lambda item: item["_sort"])
    for item in timeline:
        del item["_sort"]
    timeline = timeline[:200]

    return {
        "employee": employee_block,
        "projects": projects,
        "timesheets": timesheets,
        "billing": billing,
        "leave": {
            # Frontend contract: balances is the ARRAY of per-type rows
            # (leave_matrix returns {rows, carry-forward extras} — unwrap).
            "balances": balances.get("rows", []) if isinstance(balances, dict) else balances,
            "applications": applications,
            "accrual_events": accrual_events,
        },
        "education": [serialize_education(r) for r in education_rows],
        "experience": [serialize_experience(r) for r in experience_rows],
        "timeline": timeline,
    }
