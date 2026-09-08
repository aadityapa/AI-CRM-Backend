"""HR module: employees, leave balances, project history.

Writes: HR (Admin implicit). Reads: HR + Finance + Sales_Head.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_create, gated_read, gated_write, get_crm_db,
    page_params, role_required,
)
from models import (
    Designation,
    Employee,
    EmployeeEducation,
    EmployeeExperienceDetail,
    EmployeeLeaveBalance,
    EmployeeProjectHistory,
    LeavePolicyType,
    ProfileType,
    ProjectEmployee,
    Timesheet,
)
from schemas.common import envelope
from schemas.employees import (
    EmployeeCreate,
    EmployeeEducationCreate,
    EmployeeEducationUpdate,
    EmployeeExperienceCreate,
    EmployeeExperienceUpdate,
    EmployeeUpdate,
    LeaveBalanceUpsertItem,
)
from services.crm_common import paginate, save_upload
from services.employees import (
    employee_history,
    ensure_unique_email,
    ensure_unique_employee_code,
    get_education_or_404,
    get_employee_or_404,
    get_experience_or_404,
    leave_matrix,
    project_names,
    serialize_education,
    serialize_employee,
    serialize_experience,
    serialize_leave_balance,
    serialize_project_history,
    validate_candidate_profile,
    validate_department,
    validate_designation,
    validate_leave_type,
    validate_manager,
    validate_user_link,
)

router = APIRouter(prefix="/api/employees", tags=["CRM: Employees"])
# Flat routes for education/experience row operations (spec: /api/education/{id} ...).
subform_router = APIRouter(prefix="/api", tags=["CRM: Employees"])

EMP_WRITE = gated_write("employees", "HR")
EMP_CREATE = gated_create("employees", "HR")
# Read floor includes Sales & RMG so the Access Template can grant them the
# Employees tab (Admin/CEO always pass). Without them here the role check
# rejects the tab before the template is consulted (sidebar shows, data 403s).
# Writes stay HR-only.
EMP_READ = gated_read("employees", "HR", "Finance", "Sales_Head", "Sales", "RMG")


# ===========================================================================
# Employees CRUD
# ===========================================================================

@router.post("")
def create_employee(body: EmployeeCreate, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(EMP_CREATE)):
    ensure_unique_email(db, body.email)
    validate_user_link(db, body.user_id)
    validate_department(db, body.department_id)
    validate_designation(db, body.designation_id)
    validate_manager(db, body.reporting_manager_id, "reporting_manager_id")
    validate_manager(db, body.reporting_hr_id, "reporting_hr_id")
    if body.employee_code:
        ensure_unique_employee_code(db, body.employee_code)
    validate_candidate_profile(db, body.candidate_profile_id)

    emp = Employee(**body.model_dump())
    db.add(emp)
    db.commit()
    db.refresh(emp)
    return envelope(serialize_employee(emp, db, detail=True), "Employee created")


@router.get("")
def list_employees(is_active: bool | None = None, department_id: int | None = None,
                   profile_type: str | None = None,
                   pp: PageParams = Depends(page_params),
                   db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_READ)):
    stmt = select(Employee)
    if is_active is not None:
        stmt = stmt.where(Employee.is_active.is_(is_active))
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if profile_type:
        try:
            stmt = stmt.where(Employee.profile_type == ProfileType(profile_type))
        except ValueError:
            allowed = ", ".join(m.value for m in ProfileType)
            raise HTTPException(status_code=400,
                                detail=f"Invalid profile_type '{profile_type}'. Allowed: {allowed}")
    if pp.search:
        needle = f"%{pp.search}%"
        stmt = stmt.where(or_(
            Employee.first_name.ilike(needle),
            Employee.last_name.ilike(needle),
            (Employee.first_name + " " + sa.func.coalesce(Employee.last_name, "")).ilike(needle),
            Employee.email.ilike(needle),
        ))
    stmt = stmt.order_by(Employee.id.desc())
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope([serialize_employee(e) for e in items], meta=meta)


@router.get("/interviewer-names")
def interviewer_names(db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(
                          role_required("TA", "RMG", "HR", "Sales", "Sales_Head"))):
    """Active employees, as a name picker for the interview-round forms.

    Deliberately NOT `EMP_READ` (1 Sep 2026): TA schedules the interview but has
    no Employees access, so the full list 403'd and the picker came back empty.
    This returns only what naming an interviewer needs — id, name, email,
    designation — never salary, address or any other employee field.
    """
    rows = db.execute(
        select(Employee).where(Employee.is_active.is_(True))
        .order_by(Employee.first_name, Employee.last_name)
    ).scalars().all()
    desigs = {d.id: d.name for d in db.execute(select(Designation)).scalars().all()}
    return envelope([
        {
            "id": e.id,
            "name": " ".join(p for p in (e.first_name, e.last_name) if p) or f"Employee #{e.id}",
            "email": e.email,
            "designation": desigs.get(e.designation_id),
        }
        for e in rows
    ])


# --- Excel import/export (26 Aug 2026) ------------------------------------
# NOTE: literal routes — MUST stay above GET /{employee_id} or FastAPI tries
# to coerce "import-template" / "export" into an int.

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/import-template")
def employee_import_template(db: Session = Depends(get_crm_db),
                             user: CurrentUser = Depends(EMP_READ)):
    """A fillable .xlsx with every employee field + a Reference sheet of the
    valid Departments / Designations / manager codes. Fill → Bulk upload."""
    from fastapi import Response
    from services.employee_excel import build_template_workbook
    data = build_template_workbook(db)
    return Response(data, media_type=_XLSX_MIME,
                    headers={"Content-Disposition": 'attachment; filename="employee-import-template.xlsx"'})


@router.get("/export")
def employee_export(db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(EMP_READ)):
    """Every employee in the import's own column layout (plus ID), so an
    export can be reviewed offline or extended with new rows and re-imported."""
    from fastapi import Response
    from services.employee_excel import build_export_workbook
    data = build_export_workbook(db)
    return Response(data, media_type=_XLSX_MIME,
                    headers={"Content-Disposition": 'attachment; filename="employees.xlsx"'})


@router.post("/bulk-import")
def employee_bulk_import(file: UploadFile = File(...),
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(EMP_CREATE)):
    """Create employees from a filled template. Create-only: existing emails
    are skipped, never overwritten. Per-row savepoints — one bad row fails
    alone with a message naming the problem."""
    from services.employee_excel import import_workbook
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Upload the filled .xlsx template")
    content = file.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 5 MB)")
    try:
        result = import_workbook(db, content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return envelope(result, message=result["summary"])


@router.get("/{employee_id}")
def get_employee(employee_id: int, db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    return envelope(serialize_employee(emp, db, detail=True))


@router.get("/{employee_id}/history")
def get_employee_history(employee_id: int, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(EMP_READ)):
    """Employee 360-degree history: profile, projects, timesheets, billing,
    leave, education, experience and a chronological event timeline."""
    emp = get_employee_or_404(db, employee_id)
    return envelope(employee_history(db, emp))


@router.put("/{employee_id}")
def update_employee(employee_id: int, body: EmployeeUpdate, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(EMP_WRITE)):
    emp = get_employee_or_404(db, employee_id)
    data = body.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), "employees", data, {
        "title": "name", "first_name": "name", "middle_name": "name", "last_name": "name",
        "display_name": "name",
        "email": "email", "personal_email": "email", "phone": "phone",
        "employee_code": "employee_code", "profile_type": "profile_type",
        "department_id": "department_id", "designation_id": "designation_id",
        "reporting_manager_id": "reporting", "reporting_hr_id": "reporting",
        "employment_type": "employment_type",
        "current_ctc": "current_ctc", "bank_account_details": "bank_details",
        "present_address": "addresses", "permanent_address": "addresses",
        "is_resigned": "separation", "date_of_resignation": "separation",
        "last_working_day": "separation",
    })

    if "email" in data and data["email"]:
        ensure_unique_email(db, data["email"], exclude_id=emp.id)
    if "user_id" in data:
        validate_user_link(db, data["user_id"], exclude_employee_id=emp.id)
    if "department_id" in data:
        validate_department(db, data["department_id"])
    if "designation_id" in data:
        validate_designation(db, data["designation_id"])
    if "reporting_manager_id" in data:
        validate_manager(db, data["reporting_manager_id"], "reporting_manager_id", self_id=emp.id)
    if "reporting_hr_id" in data:
        validate_manager(db, data["reporting_hr_id"], "reporting_hr_id", self_id=emp.id)
    if "employee_code" in data and data["employee_code"]:
        ensure_unique_employee_code(db, data["employee_code"], exclude_id=emp.id)
    if "candidate_profile_id" in data:
        validate_candidate_profile(db, data["candidate_profile_id"])

    for field, value in data.items():
        setattr(emp, field, value)
    db.commit()
    db.refresh(emp)
    return envelope(serialize_employee(emp, db, detail=True), "Employee updated")


@router.delete("/{employee_id}")
def delete_employee(employee_id: int, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(EMP_WRITE)):
    from models import LeaveAccrualEvent, LeaveApplication
    from services.crm_common import commit_or_conflict

    emp = get_employee_or_404(db, employee_id)
    linked_project = db.execute(
        select(ProjectEmployee.id).where(ProjectEmployee.employee_id == emp.id).limit(1)
    ).first()
    linked_timesheet = db.execute(
        select(Timesheet.id).where(Timesheet.employee_id == emp.id).limit(1)
    ).first()
    leave_apps = db.execute(
        select(func.count()).select_from(LeaveApplication).where(
            LeaveApplication.employee_id == emp.id
        )
    ).scalar() or 0
    leave_events = db.execute(
        select(func.count()).select_from(LeaveAccrualEvent).where(
            LeaveAccrualEvent.employee_id == emp.id
        )
    ).scalar() or 0
    if linked_project or linked_timesheet or leave_apps or leave_events:
        parts = []
        if linked_project:
            parts.append("project assignment(s)")
        if linked_timesheet:
            parts.append("timesheet(s)")
        if leave_apps:
            parts.append(f"{leave_apps} leave application(s)")
        if leave_events:
            parts.append(f"{leave_events} leave ledger event(s)")
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: employee has {', '.join(parts)}. "
                "Deactivate (is_active=false) instead, or remove those records first."
            ),
        )
    # Clear self-FK manager pointers so peers don't block the delete.
    for other in db.execute(
        select(Employee).where(
            or_(
                Employee.reporting_manager_id == emp.id,
                Employee.reporting_hr_id == emp.id,
            )
        )
    ).scalars().all():
        if other.reporting_manager_id == emp.id:
            other.reporting_manager_id = None
        if other.reporting_hr_id == emp.id:
            other.reporting_hr_id = None
    db.delete(emp)
    commit_or_conflict(db, "Cannot delete: employee is still referenced by other records.")
    return envelope(message="Employee deleted")


# ===========================================================================
# Leave balances
# ===========================================================================

@router.get("/{employee_id}/leave-balances")
def get_leave_balances(employee_id: int, year: int | None = None,
                       db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    target_year = year or datetime.now().year
    rows = db.execute(
        select(EmployeeLeaveBalance, LeavePolicyType.name)
        .join(LeavePolicyType, LeavePolicyType.id == EmployeeLeaveBalance.leave_type_id)
        .where(EmployeeLeaveBalance.employee_id == emp.id,
               EmployeeLeaveBalance.year == target_year)
        .order_by(EmployeeLeaveBalance.leave_type_id)
    ).all()
    data = [serialize_leave_balance(row[0], leave_type_name=row[1]) for row in rows]
    return envelope(data, meta={"year": target_year})


@router.put("/{employee_id}/leave-balances")
def upsert_leave_balances(employee_id: int, body: list[LeaveBalanceUpsertItem],
                          db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(EMP_WRITE)):
    emp = get_employee_or_404(db, employee_id)
    if not body:
        raise HTTPException(status_code=400, detail="At least one leave balance entry is required")

    results = []
    for item in body:
        validate_leave_type(db, item.leave_type_id)
        row = db.execute(
            select(EmployeeLeaveBalance).where(
                EmployeeLeaveBalance.employee_id == emp.id,
                EmployeeLeaveBalance.leave_type_id == item.leave_type_id,
                EmployeeLeaveBalance.year == item.year,
            )
        ).scalar_one_or_none()
        if row is None:
            row = EmployeeLeaveBalance(
                employee_id=emp.id, leave_type_id=item.leave_type_id, year=item.year,
                accrued=Decimal("0"), consumed=Decimal("0"), carry_forward=Decimal("0"),
            )
            db.add(row)

        if item.accrued is not None:
            row.accrued = item.accrued
        if item.consumed is not None:
            row.consumed = item.consumed
        if item.carry_forward is not None:
            row.carry_forward = item.carry_forward

        accrued = Decimal(str(row.accrued or 0))
        consumed = Decimal(str(row.consumed or 0))
        carry_forward = Decimal(str(row.carry_forward or 0))
        if consumed > accrued + carry_forward:
            raise HTTPException(
                status_code=400,
                detail=(f"Consumed ({consumed}) cannot exceed accrued + carry_forward "
                        f"({accrued + carry_forward}) for leave_type_id {item.leave_type_id}, "
                        f"year {item.year}"),
            )
        # Balance is ALWAYS recomputed server-side; client-supplied balances are ignored.
        row.balance = accrued + carry_forward - consumed
        results.append(row)

    db.commit()
    type_names = {
        lt.id: lt.name
        for lt in db.execute(
            select(LeavePolicyType).where(
                LeavePolicyType.id.in_({r.leave_type_id for r in results}))
        ).scalars().all()
    }
    data = [serialize_leave_balance(r, leave_type_name=type_names.get(r.leave_type_id))
            for r in results]
    return envelope(data, "Leave balances saved")


# ===========================================================================
# Project history
# ===========================================================================

@router.get("/{employee_id}/project-history")
def get_project_history(employee_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    rows = db.execute(
        select(EmployeeProjectHistory)
        .where(EmployeeProjectHistory.employee_id == emp.id)
        .order_by(EmployeeProjectHistory.start_date, EmployeeProjectHistory.id)
    ).scalars().all()
    names = project_names(db, {r.project_id for r in rows})
    data = [serialize_project_history(r, project_name=names.get(r.project_id)) for r in rows]
    return envelope(data)


# ===========================================================================
# Leave matrix (Tab 13 "Leaves" section)
# ===========================================================================

@router.get("/{employee_id}/leave-matrix")
def get_leave_matrix(employee_id: int, year: int | None = None,
                     db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    target_year = year or datetime.now().year
    return envelope(leave_matrix(db, emp.id, target_year), meta={"year": target_year})


# ===========================================================================
# CV upload
# ===========================================================================

@router.post("/{employee_id}/cv")
def upload_cv(employee_id: int, file: UploadFile = File(...),
              db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_WRITE)):
    emp = get_employee_or_404(db, employee_id)
    emp.cv_url = save_upload(file, "employee_cv")
    db.commit()
    return envelope({"cv_url": emp.cv_url}, "CV uploaded")


# ===========================================================================
# Education subform
# ===========================================================================

def _check_range(start, end, start_label: str, end_label: str) -> None:
    if start and end and end < start:
        raise HTTPException(status_code=400,
                            detail=f"{end_label} cannot be before {start_label}")


@router.get("/{employee_id}/education")
def list_education(employee_id: int, db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    rows = db.execute(
        select(EmployeeEducation)
        .where(EmployeeEducation.employee_id == emp.id)
        .order_by(EmployeeEducation.id)
    ).scalars().all()
    return envelope([serialize_education(r) for r in rows])


@router.post("/{employee_id}/education")
def add_education(employee_id: int, body: EmployeeEducationCreate,
                  db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_WRITE)):
    emp = get_employee_or_404(db, employee_id)
    _check_range(body.start_date, body.end_date, "start_date", "end_date")
    row = EmployeeEducation(employee_id=emp.id, **body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return envelope(serialize_education(row), "Education added")


@subform_router.put("/education/{education_id}")
def update_education(education_id: int, body: EmployeeEducationUpdate,
                     db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_WRITE)):
    row = get_education_or_404(db, education_id)
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    _check_range(row.start_date, row.end_date, "start_date", "end_date")
    db.commit()
    db.refresh(row)
    return envelope(serialize_education(row), "Education updated")


@subform_router.delete("/education/{education_id}")
def delete_education(education_id: int, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(EMP_WRITE)):
    row = get_education_or_404(db, education_id)
    db.delete(row)
    db.commit()
    return envelope(message="Education deleted")


@subform_router.post("/education/{education_id}/certificate")
def upload_education_certificate(education_id: int, file: UploadFile = File(...),
                                 db: Session = Depends(get_crm_db),
                                 user: CurrentUser = Depends(EMP_WRITE)):
    row = get_education_or_404(db, education_id)
    row.certificate_url = save_upload(file, "employee_education_certs")
    db.commit()
    return envelope({"certificate_url": row.certificate_url}, "Certificate uploaded")


# ===========================================================================
# Experience subform
# ===========================================================================

@router.get("/{employee_id}/experience")
def list_experience(employee_id: int, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(EMP_READ)):
    emp = get_employee_or_404(db, employee_id)
    rows = db.execute(
        select(EmployeeExperienceDetail)
        .where(EmployeeExperienceDetail.employee_id == emp.id)
        .order_by(EmployeeExperienceDetail.id)
    ).scalars().all()
    return envelope([serialize_experience(r) for r in rows])


@router.post("/{employee_id}/experience")
def add_experience(employee_id: int, body: EmployeeExperienceCreate,
                   db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_WRITE)):
    emp = get_employee_or_404(db, employee_id)
    _check_range(body.date_of_joining, body.date_of_relieving,
                 "date_of_joining", "date_of_relieving")
    row = EmployeeExperienceDetail(employee_id=emp.id, **body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return envelope(serialize_experience(row), "Experience added")


@subform_router.put("/experience/{experience_id}")
def update_experience(experience_id: int, body: EmployeeExperienceUpdate,
                      db: Session = Depends(get_crm_db), user: CurrentUser = Depends(EMP_WRITE)):
    row = get_experience_or_404(db, experience_id)
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    _check_range(row.date_of_joining, row.date_of_relieving,
                 "date_of_joining", "date_of_relieving")
    db.commit()
    db.refresh(row)
    return envelope(serialize_experience(row), "Experience updated")


@subform_router.delete("/experience/{experience_id}")
def delete_experience(experience_id: int, db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(EMP_WRITE)):
    row = get_experience_or_404(db, experience_id)
    db.delete(row)
    db.commit()
    return envelope(message="Experience deleted")


@subform_router.post("/experience/{experience_id}/certificate")
def upload_experience_certificate(experience_id: int, file: UploadFile = File(...),
                                  db: Session = Depends(get_crm_db),
                                  user: CurrentUser = Depends(EMP_WRITE)):
    row = get_experience_or_404(db, experience_id)
    row.certificate_url = save_upload(file, "employee_experience_certs")
    db.commit()
    return envelope({"certificate_url": row.certificate_url}, "Certificate uploaded")
