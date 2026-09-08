"""Leave applications API (/api/leave-applications).

Self-service: an employee applies against their own linked profile; HR/Admin
may file for anyone. days is server-computed (Multi_Day = inclusive day count,
Half_Day = 0.5, Full_Day = 1). Approval mutates EmployeeLeaveBalance for the
year of from_date and appends a LeaveAccrualEvent ledger row; the employee is
notified via the bell feed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_write_action, get_crm_db, get_current_user, page_params, role_required,
)
from crm_deps import gated_write, gated_write_action
from models import (
    Employee, EmployeeLeaveBalance, LeaveAccrualEvent, LeaveApplication, LeavePolicyType,
    Project, ProjectEmployee,
)
from schemas.common import RejectIn, envelope
from schemas.leave import LEAVE_APP_STATUSES, LeaveApplicationCreate, LeaveApplicationUpdate
from services.crm_common import paginate, to_dict
from services.notify import notify_employee, notify_role, notify_user
from services.project_employees import consume_pe_leave, credit_pe_leave, pe_leave_detail_for
from services.timesheets import employee_display_name, employee_for_user

router = APIRouter(prefix="/api/leave-applications", tags=["CRM: Leave Applications"])

hr_only = gated_write("leave-applications", "HR")
# Decide actions are template-governed + admin-editable (Users -> Action Permissions).
leave_approve = gated_write_action("leave.approve", "leave-applications", "HR")
leave_reject = gated_write_action("leave.reject", "leave-applications", "HR")

ZERO = Decimal("0")


def _is_hr(user: CurrentUser) -> bool:
    return user.is_admin or user.has_any("HR")


def _get_or_404(db: Session, application_id: int) -> LeaveApplication:
    obj = db.get(LeaveApplication, application_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Leave application not found")
    return obj


def _is_owner(db: Session, user: CurrentUser, app: LeaveApplication) -> bool:
    emp = employee_for_user(db, user.id)
    return bool(emp and emp.id == app.employee_id)


def _is_loss_of_pay(leave_type: LeavePolicyType) -> bool:
    return bool(re.search(r"loss.*pay", (leave_type.name or "").lower()))


def _is_comp_off_type(leave_type: LeavePolicyType) -> bool:
    return bool(re.search(r"comp.*off", (leave_type.name or "").lower()))


def _compute_days(period_type: str, from_date, to_date) -> Decimal:
    if period_type == "Half_Day":
        return Decimal("0.5")
    if period_type == "Full_Day":
        return Decimal("1")
    return Decimal((to_date - from_date).days + 1)


def _balance_row(db: Session, employee_id: int, leave_type_id: int,
                 year: int) -> EmployeeLeaveBalance | None:
    return db.execute(
        select(EmployeeLeaveBalance).where(
            EmployeeLeaveBalance.employee_id == employee_id,
            EmployeeLeaveBalance.leave_type_id == leave_type_id,
            EmployeeLeaveBalance.year == year,
        )
    ).scalars().first()


def _app_out(db: Session, app: LeaveApplication) -> dict:
    data = to_dict(app)
    emp = db.get(Employee, app.employee_id)
    leave_type = db.get(LeavePolicyType, app.leave_type_id)
    data["employee_name"] = employee_display_name(emp)
    data["leave_type_name"] = leave_type.name if leave_type else None
    return data


def _validate_period(body_period: str, from_date, to_date):
    """Normalize (from_date, to_date) and compute days for one application."""
    to_date = to_date or from_date
    if body_period in ("Full_Day", "Half_Day"):
        to_date = from_date  # single-day request
    if to_date < from_date:
        raise HTTPException(status_code=400, detail="to_date must not be before from_date")
    return to_date, _compute_days(body_period, from_date, to_date)


def _validate_comp_off(comp_off_type: str | None, leave_type: LeavePolicyType) -> None:
    if comp_off_type and not _is_comp_off_type(leave_type):
        raise HTTPException(
            status_code=400,
            detail="comp_off_type is only valid for a Comp-Off leave type",
        )


def _check_balance(db: Session, employee_id: int, leave_type: LeavePolicyType,
                   year: int, days: Decimal, comp_off_type: str | None,
                   project_employee_id: int | None = None) -> None:
    """400 when the requested days exceed available balance.

    PE-scoped apps check project_employee_leave_details; otherwise employee_leave_balances.
    Skipped for Loss of Pay and any Comp-Off leave type (UC-10).
    """
    if _is_loss_of_pay(leave_type) or _is_comp_off_type(leave_type) or comp_off_type == "Earned":
        return
    if project_employee_id is not None:
        row = pe_leave_detail_for(db, project_employee_id, leave_type.id)
        available = Decimal(row.leave_balance or 0) if row else ZERO
        if days > available:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient PE {leave_type.name} balance: "
                       f"requested {float(days):g} day(s), available {float(available):g}",
            )
        return
    row = _balance_row(db, employee_id, leave_type.id, year)
    available = Decimal(row.balance or 0) if row else ZERO
    if days > available:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient {leave_type.name} balance for {year}: "
                   f"requested {float(days):g} day(s), available {float(available):g}",
        )


def _employee_has_active_pe(db: Session, employee_id: int) -> bool:
    return db.execute(
        select(ProjectEmployee.id).where(
            ProjectEmployee.employee_id == employee_id,
            ProjectEmployee.is_active.is_(True),
            ProjectEmployee.is_exit.is_(False),
        ).limit(1)
    ).first() is not None


def _require_project_when_mapped(db: Session, employee_id: int,
                                 pe: ProjectEmployee | None) -> None:
    """UC-03: leave form requires project selection when the employee has active PE mappings."""
    if pe is not None:
        return
    if _employee_has_active_pe(db, employee_id):
        raise HTTPException(
            status_code=400,
            detail="Project selection is required when the employee has active project mappings",
        )


def _resolve_pe(db: Session, project_employee_id: int | None, employee_id: int,
                project_id: int | None) -> ProjectEmployee | None:
    if project_employee_id is not None:
        pe = db.get(ProjectEmployee, project_employee_id)
        if pe is None:
            raise HTTPException(status_code=400, detail="Project employee not found")
        if pe.employee_id != employee_id:
            raise HTTPException(
                status_code=400,
                detail="project_employee_id does not belong to this employee",
            )
        return pe
    if project_id is not None:
        return db.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == project_id,
                ProjectEmployee.employee_id == employee_id,
                ProjectEmployee.is_active.is_(True),
            )
        ).scalars().first()
    return None


# ---------------------------------------------------------------- create / list

@router.post("")
def create_leave_application(
    body: LeaveApplicationCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    own_emp = employee_for_user(db, user.id)
    if body.employee_id and _is_hr(user):
        employee = db.get(Employee, body.employee_id)
        if employee is None:
            raise HTTPException(status_code=400, detail="Employee not found")
    else:
        if own_emp is None:
            raise HTTPException(
                status_code=403,
                detail="Requires HR/Admin role or an employee profile linked to your user",
            )
        if body.employee_id and body.employee_id != own_emp.id:
            raise HTTPException(
                status_code=403,
                detail="Only HR/Admin can apply for another employee",
            )
        employee = own_emp
    leave_type = db.get(LeavePolicyType, body.leave_type_id)
    if leave_type is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")
    if body.project_id is not None and db.get(Project, body.project_id) is None:
        raise HTTPException(status_code=400, detail="Project not found")
    pe = _resolve_pe(db, body.project_employee_id, employee.id, body.project_id)
    _require_project_when_mapped(db, employee.id, pe)
    project_id = body.project_id or (pe.project_id if pe else None)
    pe_id = pe.id if pe else body.project_employee_id
    _validate_comp_off(body.comp_off_type, leave_type)
    to_date, days = _validate_period(body.leave_period_type, body.from_date, body.to_date)
    _check_balance(db, employee.id, leave_type, body.from_date.year, days, body.comp_off_type,
                   project_employee_id=pe_id)

    app = LeaveApplication(
        employee_id=employee.id,
        project_id=project_id,
        project_employee_id=pe_id,
        leave_type_id=body.leave_type_id,
        leave_period_type=body.leave_period_type,
        from_date=body.from_date,
        to_date=to_date,
        days=days,
        comp_off_type=body.comp_off_type,
        reason=body.reason,
        status="Pending",
    )
    db.add(app)
    db.commit()
    db.refresh(app)

    # Approve and reject already notified the employee; submit notified nobody,
    # so an application could sit in Pending with no HR user aware of it.
    notify_role(
        db, "HR",
        f"Leave application — {employee_display_name(employee)}",
        f"{employee_display_name(employee)} applied for {days} day(s) of "
        f"{leave_type.name} from {body.from_date} to {to_date}.",
        f"leave-applications/{app.id}",
        exclude_user_id=user.id,
        actor=user,
        event="leave.submitted",
        rows=[
            ("Employee", employee_display_name(employee)),
            ("Leave type", leave_type.name),
            ("From", str(body.from_date)),
            ("To", str(to_date)),
            ("Days", str(days)),
            ("Reason", body.reason or ""),
        ],
        dedupe_prefix=f"leave.submitted:{app.id}",
    )
    db.commit()

    return envelope(data=_app_out(db, app), message="Leave application submitted")


@router.get("")
def list_leave_applications(
    employee_id: int | None = None,
    status: str | None = None,
    project_id: int | None = None,
    project_employee_id: int | None = None,
    leave_type_id: int | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(LeaveApplication).order_by(LeaveApplication.id.desc())
    if not _is_hr(user):
        emp = employee_for_user(db, user.id)
        if not emp:
            return envelope(data=[], meta={"page": params.page, "limit": params.limit,
                                           "total": 0, "pages": 0})
        stmt = stmt.where(LeaveApplication.employee_id == emp.id)
    if employee_id is not None:
        stmt = stmt.where(LeaveApplication.employee_id == employee_id)
    if project_id is not None:
        stmt = stmt.where(LeaveApplication.project_id == project_id)
    if project_employee_id is not None:
        stmt = stmt.where(LeaveApplication.project_employee_id == project_employee_id)
    if leave_type_id is not None:
        stmt = stmt.where(LeaveApplication.leave_type_id == leave_type_id)
    if status:
        if status not in LEAVE_APP_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        stmt = stmt.where(LeaveApplication.status == status)
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[_app_out(db, a) for a in items], meta=meta)


# ---------------------------------------------------------------- detail / edit

@router.get("/{application_id}")
def get_leave_application(
    application_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    app = _get_or_404(db, application_id)
    if not (_is_hr(user) or _is_owner(db, user, app)):
        raise HTTPException(status_code=403, detail="Not allowed to access this leave application")
    return envelope(data=_app_out(db, app))


@router.put("/{application_id}")
def update_leave_application(
    application_id: int,
    body: LeaveApplicationUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    app = _get_or_404(db, application_id)
    if not (_is_hr(user) or _is_owner(db, user, app)):
        raise HTTPException(status_code=403, detail="Not allowed to edit this leave application")
    if app.status != "Pending":
        raise HTTPException(status_code=400,
                            detail="Only Pending leave applications can be edited")
    data = body.model_dump(exclude_unset=True)
    if "project_id" in data and data["project_id"] is not None \
            and db.get(Project, data["project_id"]) is None:
        raise HTTPException(status_code=400, detail="Project not found")
    if "project_employee_id" in data and data["project_employee_id"] is not None:
        pe = db.get(ProjectEmployee, data["project_employee_id"])
        if pe is None:
            raise HTTPException(status_code=400, detail="Project employee not found")
        if pe.employee_id != app.employee_id:
            raise HTTPException(status_code=400,
                                detail="project_employee_id does not belong to this employee")
        data.setdefault("project_id", pe.project_id)
    leave_type = db.get(LeavePolicyType, data.get("leave_type_id", app.leave_type_id))
    if leave_type is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")
    for field, value in data.items():
        setattr(app, field, value)
    _validate_comp_off(app.comp_off_type, leave_type)
    app.to_date, app.days = _validate_period(app.leave_period_type, app.from_date, app.to_date)
    _check_balance(db, app.employee_id, leave_type, app.from_date.year,
                   Decimal(app.days), app.comp_off_type,
                   project_employee_id=app.project_employee_id)
    db.commit()
    db.refresh(app)
    return envelope(data=_app_out(db, app), message="Leave application updated")


# ---------------------------------------------------------------- workflow

def _notify_employee(db: Session, app: LeaveApplication, title: str, message: str,
                     actor=None, event: str = "leave.decision") -> None:
    """Bell where possible, email always.

    Email is the channel that actually matters here: someone whose leave was
    just approved is, by definition, often not in the app — and an employee with
    no login account (employees.user_id IS NULL) could never see the bell at all,
    which is exactly the population this used to drop on the floor.
    """
    emp = db.get(Employee, app.employee_id)
    if emp is None:
        return
    leave_type = db.get(LeavePolicyType, app.leave_type_id)
    notify_employee(
        db, emp, title, message, f"leave-applications/{app.id}",
        actor=actor,
        event=event,
        rows=[
            ("Leave type", getattr(leave_type, "name", "") or ""),
            ("From", str(app.from_date)),
            ("To", str(app.to_date)),
            ("Days", str(app.days)),
            ("Status", str(app.status)),
        ],
        dedupe_key=f"{event}:{app.id}:{app.status}",
    )


@router.post("/{application_id}/approve")
def approve_leave_application(
    application_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(leave_approve),
):
    app = _get_or_404(db, application_id)
    if app.status != "Pending":
        raise HTTPException(status_code=400,
                            detail="Only Pending leave applications can be approved")
    leave_type = db.get(LeavePolicyType, app.leave_type_id)
    days = Decimal(app.days or 0)
    year = app.from_date.year

    # PE-scoped leave reduces project_employee_leave_details; also keep employee
    # ledger so bench/global views stay consistent when pe_id is set.
    if app.project_employee_id is not None:
        if app.comp_off_type == "Earned":
            pe_row = credit_pe_leave(db, app.project_employee_id, app.leave_type_id, days)
            event_type, amount = "Comp_Off_Credit", days
            balance_after = pe_row.leave_balance
        else:
            # Comp-Off leave types bypass the balance gate (UC-10); allow drawdown.
            allow_neg = bool(leave_type and _is_comp_off_type(leave_type))
            pe_row = consume_pe_leave(
                db, app.project_employee_id, app.leave_type_id, days,
                allow_negative=allow_neg,
            )
            event_type, amount = "Consumption", -days
            balance_after = pe_row.leave_balance
        db.add(LeaveAccrualEvent(
            employee_id=app.employee_id,
            leave_type_id=app.leave_type_id,
            event_type=event_type,
            amount=amount,
            balance_after=balance_after,
            source=f"leave_application:{app.id}",
            note=f"PE#{app.project_employee_id} "
                 f"{leave_type.name if leave_type else 'Leave'} approved "
                 f"({app.from_date.isoformat()} → {app.to_date.isoformat()})",
        ))
    else:
        balance = _balance_row(db, app.employee_id, app.leave_type_id, year)
        if balance is None:
            balance = EmployeeLeaveBalance(
                employee_id=app.employee_id, leave_type_id=app.leave_type_id, year=year,
                accrued=0, consumed=0, balance=0, carry_forward=0,
            )
            db.add(balance)
            db.flush()

        if app.comp_off_type == "Earned":
            balance.accrued = Decimal(balance.accrued or 0) + days
            event_type, amount = "Comp_Off_Credit", days
        else:
            balance.consumed = Decimal(balance.consumed or 0) + days
            event_type, amount = "Consumption", -days
        balance.balance = (Decimal(balance.accrued or 0) + Decimal(balance.carry_forward or 0)
                           - Decimal(balance.consumed or 0))
        db.add(LeaveAccrualEvent(
            employee_id=app.employee_id,
            leave_type_id=app.leave_type_id,
            event_type=event_type,
            amount=amount,
            balance_after=balance.balance,
            source=f"leave_application:{app.id}",
            note=f"{leave_type.name if leave_type else 'Leave'} application approved "
                 f"({app.from_date.isoformat()} → {app.to_date.isoformat()})",
        ))
    app.status = "Approved"
    app.decided_at = datetime.now(timezone.utc)
    app.decided_by = user.id
    _notify_employee(
        db, app, "Leave application approved",
        f"Your {leave_type.name if leave_type else 'leave'} application for "
        f"{float(days):g} day(s) ({app.from_date.isoformat()} to {app.to_date.isoformat()}) "
        f"was approved.",
    )
    db.commit()
    db.refresh(app)
    return envelope(data=_app_out(db, app), message="Leave application approved")


@router.post("/{application_id}/reject")
def reject_leave_application(
    application_id: int,
    body: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(leave_reject),
):
    app = _get_or_404(db, application_id)
    if app.status != "Pending":
        raise HTTPException(status_code=400,
                            detail="Only Pending leave applications can be rejected")
    try:
        reason = body.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    app.status = "Rejected"
    app.rejection_reason = reason
    app.decided_at = datetime.now(timezone.utc)
    app.decided_by = user.id
    _notify_employee(
        db, app, "Leave application rejected",
        f"Your leave application ({app.from_date.isoformat()} to {app.to_date.isoformat()}) "
        f"was rejected: {reason}",
    )
    db.commit()
    db.refresh(app)
    return envelope(data=_app_out(db, app), message="Leave application rejected")


@router.post("/{application_id}/cancel")
def cancel_leave_application(
    application_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    app = _get_or_404(db, application_id)
    if not _is_owner(db, user, app):
        raise HTTPException(status_code=403,
                            detail="Only the applicant can cancel their leave application")
    if app.status != "Pending":
        raise HTTPException(status_code=400,
                            detail="Only Pending leave applications can be cancelled")
    app.status = "Cancelled"
    app.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(app)
    return envelope(data=_app_out(db, app), message="Leave application cancelled")


@router.delete("/{application_id}")
def delete_leave_application(
    application_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    """Hard-delete a leave application. Blocks Approved (balance already mutated)."""
    from services.crm_common import commit_or_conflict

    app = _get_or_404(db, application_id)
    if not (_is_hr(user) or _is_owner(db, user, app)):
        raise HTTPException(status_code=403, detail="Not allowed to delete this leave application")
    if app.status == "Approved":
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: leave application is Approved (balances already updated). Cancel is not available — contact HR.",
        )
    db.delete(app)
    commit_or_conflict(db, "Cannot delete: leave application is still referenced by other records.")
    return envelope(data={"id": application_id}, message="Leave application deleted")
