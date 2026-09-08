"""Projects API: CRUD, team (project employees), timesheets view, communication matrix."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (  # noqa: F401
    gated_write_action,
    CurrentUser, PageParams, gated_create, gated_read, gated_write, get_crm_db, page_params,
)
from models import (
    BillingFrequency, Customer, CustomerLeavePolicy, Employee, LeavePolicyType, Project,
    ProjectCommunicationMatrix, ProjectEmployee, ProjectEmployeeLeaveDetail,
    ProjectEmployeeRate, ProjectLeavePolicy, ProjectStatus, Timesheet, TimesheetStatus,
)
from schemas.common import envelope
from schemas.leave import apply_leave_expire_timing_consistency
from schemas.projects import (
    CommMatrixIn, ProjectCreate, ProjectEmployeeIn, ProjectEmployeeLeaveDetailUpdate,
    ProjectEmployeeRateIn, ProjectEmployeeRateUpdate, ProjectEmployeeUpdate,
    ProjectLeavePolicyCreate, ProjectLeavePolicyUpdate, ProjectUpdate,
)
from services.crm_common import paginate
from services.project_employees import (
    apply_rate_rows, clear_other_current_rates, ensure_initial_rate,
    exit_project_employee,
    get_pe_or_404, group_pe_rows_by_employee, leave_detail_out, project_employee_detail_out,
    rate_out, seed_leave_details_from_customer_policy, sync_pe_billing_from_current_rate,
    sync_pe_leave_from_customer_policy,
)
from services.projects import (
    add_history_entry, comm_entry_out, get_project_or_404, open_history_row,
    project_detail_out, project_employee_out, project_history, project_leave_policy_out,
    project_out, validate_project_refs,
)
from services.timesheets import timesheet_out

router = APIRouter(prefix="/api/projects", tags=["CRM: Projects"])

read_projects = gated_read("projects")
write_projects = gated_write("projects", "Sales_Head", "Finance")
create_projects = gated_create("projects", "Sales_Head", "Finance")
read_pe = gated_read("project-employees")
# Role list admin-editable: Users tab -> Action Permissions ("Map / edit
# project employees"). Admin/CEO always pass.
write_pe = gated_write_action("project_employee.manage", "project-employees", "Sales_Head", "HR", "Finance")


def _get_leave_policy_or_404(db: Session, policy_id: int) -> ProjectLeavePolicy:
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Leave policy not found")
    return policy


def _apply_leave_policy_update(
    db: Session, policy: ProjectLeavePolicy, body: ProjectLeavePolicyUpdate,
) -> ProjectLeavePolicy:
    changes = body.model_dump(exclude_unset=True)
    if "leave_type_id" in changes and changes["leave_type_id"] is not None:
        if db.get(LeavePolicyType, changes["leave_type_id"]) is None:
            raise HTTPException(status_code=400, detail="Leave policy type not found")
        other = db.execute(
            select(ProjectLeavePolicy).where(
                ProjectLeavePolicy.project_id == policy.project_id,
                ProjectLeavePolicy.leave_type_id == changes["leave_type_id"],
                ProjectLeavePolicy.id != policy.id,
            ).limit(1)
        ).scalars().first()
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail="A leave policy for this project/leave type already exists",
            )
    apply_leave_expire_timing_consistency(changes, existing_expire=policy.leave_expire)
    for field, value in changes.items():
        setattr(policy, field, value)
    db.commit()
    db.refresh(policy)
    return policy


# Flat leave-policy routes MUST be registered before /{project_id} so FastAPI
# does not try to coerce "leave-policies" into an int project_id.
@router.put("/leave-policies/{policy_id}")
def update_project_leave_policy_by_id(
    policy_id: int,
    body: ProjectLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    policy = _get_leave_policy_or_404(db, policy_id)
    policy = _apply_leave_policy_update(db, policy, body)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy updated")


@router.delete("/leave-policies/{policy_id}")
def delete_project_leave_policy_by_id(
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    policy = _get_leave_policy_or_404(db, policy_id)
    policy.is_active = False
    db.commit()
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy deactivated")


# ---------------------------------------------------------------- CRUD

@router.get("")
def list_projects(
    status: str | None = None,
    customer_id: int | None = None,
    opportunity_id: int | None = None,
    billing_frequency: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    stmt = select(Project).order_by(Project.id.desc())
    if billing_frequency:
        try:
            stmt = stmt.where(Project.billing_frequency == BillingFrequency(billing_frequency))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid billing_frequency: {billing_frequency}")
    if status:
        try:
            stmt = stmt.where(Project.status == ProjectStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    if customer_id is not None:
        stmt = stmt.where(Project.customer_id == customer_id)
    if opportunity_id is not None:
        stmt = stmt.where(Project.opportunity_id == opportunity_id)
    if params.search:
        stmt = stmt.where(Project.name.ilike(f"%{params.search}%"))
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[project_out(p) for p in items], meta=meta)


@router.post("")
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(create_projects),
):
    validate_project_refs(db, body.opportunity_id, body.customer_id)
    # Resolve explicit branch: client branch_id (wizard) → opportunity.branch_id.
    from models import CustomerBranch, Opportunity
    resolved_branch_id = body.branch_id
    if resolved_branch_id is not None:
        branch_row = db.get(CustomerBranch, resolved_branch_id)
        if branch_row is None or branch_row.customer_id != body.customer_id:
            raise HTTPException(status_code=400, detail="Branch not found for this customer")
    else:
        # Branch fallback via the opportunity only applies when one was linked.
        opp = db.get(Opportunity, body.opportunity_id) if body.opportunity_id else None
        if opp is not None and opp.branch_id is not None:
            opp_branch = db.get(CustomerBranch, opp.branch_id)
            # Never copy a foreign-customer opportunity.branch_id onto the project.
            if opp_branch is not None and opp_branch.customer_id == body.customer_id:
                resolved_branch_id = opp.branch_id
            else:
                resolved_branch_id = None
    # Apply only fields the client sent so unset policy columns can seed from branch.
    provided = body.model_fields_set
    project = Project(
        opportunity_id=body.opportunity_id,
        customer_id=body.customer_id,
        branch_id=resolved_branch_id,
        name=body.name,
        billing_cycle_start_day=body.billing_cycle_start_day,
        billing_cycle_end_day=body.billing_cycle_end_day,
        billing_frequency=body.billing_frequency,
        recurring_billing=body.recurring_billing,
        max_billable_hours_day=body.max_billable_hours_day,
        max_billable_hours_month=body.max_billable_hours_month,
        max_billable_days_month=body.max_billable_days_month,
        no_billing_period_days=body.no_billing_period_days,
        status=body.status,
    )
    for attr in (
        "holidays_billable", "weekoff_billable", "leave_billable", "comp_off_billable",
        "hours_required_half_day", "hours_required_full_day",
        "hours_required_half_day_comp_off", "hours_required_full_day_comp_off",
        "working_hours_per_day",
        "is_max_billable_hours_per_day", "is_max_billable_hours_per_month",
        "is_max_billable_days_per_month", "is_initial_no_billing_period",
        "initial_no_billing_qty", "initial_no_billing_period",
    ):
        if attr in provided:
            setattr(project, attr, getattr(body, attr))
    db.add(project)
    # Seed unset billing props from the branch this project bills against
    # (project.branch_id → opportunity.branch_id). Only fills fields the client left unset;
    # never overwrites a provided value. Fields the branch doesn't set are left as-is.
    from services.timesheets import _project_branch
    branch = _project_branch(db, project)
    if branch is not None:
        provided = body.model_fields_set
        # (Project column, CustomerBranch column) — only pairs where both exist.
        for proj_attr, branch_attr in (
            ("billing_cycle_start_day", "billing_cycle_start_day"),
            ("billing_cycle_end_day", "billing_cycle_end_day"),
            ("max_billable_hours_day", "max_billable_hours_per_day"),
            ("max_billable_hours_month", "max_billable_hours_per_month"),
            ("max_billable_days_month", "max_billable_days_per_month"),
            ("no_billing_period_days", "initial_no_billing_qty"),
            ("holidays_billable", "holidays_billable"),
            ("weekoff_billable", "weekoff_billable"),
            ("leave_billable", "leave_billable"),
            ("comp_off_billable", "comp_off_billable"),
            ("is_max_billable_hours_per_day", "is_max_billable_hours_per_day"),
            ("is_max_billable_hours_per_month", "is_max_billable_hours_per_month"),
            ("is_max_billable_days_per_month", "is_max_billable_days_per_month"),
            ("hours_required_half_day", "hours_required_half_day"),
            ("hours_required_full_day", "hours_required_full_day"),
            ("hours_required_half_day_comp_off", "hours_required_half_day_comp_off"),
            ("hours_required_full_day_comp_off", "hours_required_full_day_comp_off"),
            ("working_hours_per_day", "working_hours_per_day"),
            ("is_initial_no_billing_period", "is_initial_no_billing_period"),
            ("initial_no_billing_qty", "initial_no_billing_qty"),
            ("initial_no_billing_period", "initial_no_billing_period"),
        ):
            if proj_attr in provided:
                continue
            val = getattr(branch, branch_attr, None)
            if val is not None:
                setattr(project, proj_attr, val)
        # billing_frequency is free-text on the branch; map to the project enum.
        # Accept both Bi_Weekly (canonical) and Bi-Weekly (UI label) from branch.
        if "billing_frequency" not in provided and branch.billing_frequency:
            raw = str(branch.billing_frequency).strip().replace("-", "_")
            try:
                project.billing_frequency = BillingFrequency(raw)
            except ValueError:
                # try title-case Monthly/Weekly variants already matching enum values
                try:
                    project.billing_frequency = BillingFrequency(branch.billing_frequency)
                except ValueError:
                    pass
    db.commit()
    db.refresh(project)
    return envelope(data=project_out(project), message="Project created")


@router.get("/all-employees")
def list_all_project_employees(
    p: PageParams = Depends(page_params),
    project_id: int | None = None,
    customer_id: int | None = None,
    opportunity_id: int | None = None,
    status: str | None = None,  # active | exited | all
    group_by: str | None = None,  # employee → nested groups (UC-12)
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    """Global list of every Project Employee mapping across all projects (the
    Project Employees tab). One row per (employee × project) mapping — so the
    same person on two projects shows as two rows, each with its own rate.
    Pass group_by=employee for nested groups (one card per person, mappings[]).
    Defined BEFORE /{project_id} so this literal path wins."""
    base = (
        select(ProjectEmployee, Employee, Project, Customer.name.label("customer_name"))
        .join(Employee, Employee.id == ProjectEmployee.employee_id)
        .join(Project, Project.id == ProjectEmployee.project_id)
        .join(Customer, Customer.id == Project.customer_id, isouter=True)
    )
    if project_id is not None:
        base = base.where(ProjectEmployee.project_id == project_id)
    if customer_id is not None:
        base = base.where(Project.customer_id == customer_id)
    if opportunity_id is not None:
        # Opportunity detail tab: employees on projects born from this opportunity.
        base = base.where(Project.opportunity_id == opportunity_id)
    st = (status or "all").strip().lower()
    if st == "active":
        base = base.where(ProjectEmployee.is_active.is_(True), ProjectEmployee.is_exit.is_(False))
    elif st in ("exited", "exit", "inactive"):
        base = base.where(or_(ProjectEmployee.is_exit.is_(True), ProjectEmployee.is_active.is_(False)))
    if p.search:
        like = f"%{p.search.lower()}%"
        base = base.where(
            or_(
                func.lower(func.coalesce(Employee.first_name, "")).like(like),
                func.lower(func.coalesce(Employee.last_name, "")).like(like),
                func.lower(func.coalesce(Employee.email, "")).like(like),
                func.lower(Project.name).like(like),
                func.lower(func.coalesce(Customer.name, "")).like(like),
            )
        )
    grouped = (group_by or "").strip().lower() == "employee"
    if grouped:
        order = (Employee.first_name.asc(), Employee.last_name.asc(), Project.name.asc())
    else:
        order = (Project.name.asc(), Employee.first_name.asc())
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar() or 0
    from sqlalchemy.orm import selectinload

    rows = db.execute(
        base.order_by(*order)
        .limit(p.limit).offset(p.offset)
        # One extra query for ALL rate histories on the page instead of a lazy
        # load per row — this list used to fire 3-4 queries per mapping.
        .options(selectinload(ProjectEmployee.rates))
    ).all()

    # Batch the per-row lookups ONCE for the page: designations, leave totals,
    # and PO summaries (per project, since several rows share a project).
    from models.masters import Designation
    from services.project_employees import pe_leave_balance_totals, project_po_summary

    desig_ids = {e.designation_id for _, e, _, _ in rows if e is not None and e.designation_id}
    desig_names = dict(db.execute(
        select(Designation.id, Designation.name).where(Designation.id.in_(desig_ids))
    ).all()) if desig_ids else {}
    leave_totals = pe_leave_balance_totals(db, [pe.id for pe, _, _, _ in rows])
    po_by_project: dict[int, dict] = {}

    flat: list[dict] = []
    for pe, emp, proj, customer_name in rows:
        d = project_employee_out(pe, emp)
        # Spec: role_title from PE.role_title, else employee designation name,
        # else employee.role_title (already folded into project_employee_out).
        if not d.get("role_title") and emp is not None and emp.designation_id:
            name = desig_names.get(emp.designation_id)
            if name:
                d["role_title"] = name
        d["project_name"] = proj.name
        d["customer_id"] = proj.customer_id
        d["customer_name"] = customer_name
        d["leave_balance_total"] = leave_totals.get(pe.id, 0.0)
        if pe.project_id not in po_by_project:
            po_by_project[pe.project_id] = project_po_summary(db, pe.project_id)
        po = po_by_project[pe.project_id]
        d["po_status"] = po.get("po_status")
        d["po_utilization_pct"] = po.get("utilization_pct")
        d["po_number"] = po.get("po_number")
        flat.append(d)
    pages = (total + p.limit - 1) // p.limit if p.limit else 1
    meta = {"page": p.page, "limit": p.limit, "total": total, "pages": pages,
            "group_by": "employee" if grouped else None}
    if grouped:
        data = group_pe_rows_by_employee(flat)
        return envelope(data=data, message="Project employees (grouped by employee)", meta=meta)
    return envelope(data=flat, message="Project employees", meta=meta)


# ---------------------------------------------------------------- PE detail / rates (literal /employees/... before /{project_id})

@router.get("/employees/{pe_id}")
def get_project_employee_detail(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    pe = get_pe_or_404(db, pe_id)
    return envelope(data=project_employee_detail_out(db, pe))


@router.post("/employees/{pe_id}/leave/sync")
def sync_project_employee_leave_policies(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "Admin", "HR")),
):
    """Back-fill missing PE leave types from customer/branch policy (idempotent).

    Existing balances/consumed are never modified — only new leave types are added.
    """
    pe = get_pe_or_404(db, pe_id)
    result = sync_pe_leave_from_customer_policy(db, pe)
    db.commit()
    detail = project_employee_detail_out(db, pe)
    added_names = [a.get("leave_type_name") or f"#{a.get('leave_type_id')}" for a in result["added"]]
    msg = (
        f"Added {result['added_count']} leave type(s): {', '.join(added_names)}"
        if result["added_count"]
        else "Leave policies already in sync — nothing to add"
    )
    return envelope(
        data={
            **result,
            "leave_details": detail.get("leave_details"),
            "leave_summary": detail.get("leave_summary"),
            "leave_eligibility": detail.get("leave_eligibility"),
            "leave_balance_total": detail.get("leave_balance_total"),
        },
        message=msg,
    )


@router.put("/employees/{pe_id}")
def update_project_employee_by_id(
    pe_id: int,
    body: ProjectEmployeeUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    pe = get_pe_or_404(db, pe_id)
    changes = body.model_dump(exclude_unset=True)
    exiting = (not pe.is_exit and changes.get("is_exit") is True) or (
        pe.is_active and changes.get("is_active") is False and changes.get("is_exit", True) is not False
    )
    deactivating = pe.is_active and changes.get("is_active") is False
    rate_changed = "billing_rate" in changes or "billing_unit" in changes
    for field, value in changes.items():
        setattr(pe, field, value)
    exit_summary = None
    if exiting or (pe.is_exit and deactivating):
        exit_summary = exit_project_employee(db, pe, exit_date=pe.exit_date or date.today())
        history = open_history_row(db, pe.project_id, pe.employee_id)
        if history:
            history.end_date = pe.exit_date or date.today()
    elif deactivating:
        history = open_history_row(db, pe.project_id, pe.employee_id)
        if history:
            history.end_date = date.today()
    if rate_changed:
        # UPSERT through apply_rate_rows instead of blindly adding a row.
        # The old code appended a NEW rate at billing_date on every save of
        # this form, so repeated edits stacked duplicate effective dates —
        # histories grew twins like three rates all starting 01 Apr. Same
        # date now refines the existing row; a different date starts a new
        # one, exactly like the Map Employee wizard.
        from schemas.projects import MapRateIn
        apply_rate_rows(db, pe, [MapRateIn(
            effective_from=pe.billing_date or date.today(),
            rate=pe.billing_rate,
        )])
    db.commit()
    db.refresh(pe)
    data = project_employee_detail_out(db, pe)
    if exit_summary:
        data["exit_summary"] = exit_summary
    return envelope(data=data, message="Project employee updated")


@router.post("/employees/{pe_id}/exit")
def exit_project_employee_endpoint(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
    exit_date: date | None = None,
):
    """Explicit exit: stop accrual, flag open periods, mark leave for settlement."""
    pe = get_pe_or_404(db, pe_id)
    summary = exit_project_employee(db, pe, exit_date=exit_date or date.today())
    history = open_history_row(db, pe.project_id, pe.employee_id)
    if history:
        history.end_date = pe.exit_date or date.today()
    db.commit()
    db.refresh(pe)
    data = project_employee_detail_out(db, pe)
    data["exit_summary"] = summary
    return envelope(data=data, message="Project employee exited")


@router.delete("/employees/{pe_id}")
def delete_project_employee(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    """Hard-delete a PE mapping.

    Cascades draft/non-approved timesheets and deletable invoices (+ unpaid TDS)
    linked to this assignment. Blocks when approved timesheets remain after
    invoice cascade fails, or when invoices have payments / credit notes
    (message lists invoice numbers).
    """
    from models import LeaveApplication
    from services.crm_common import commit_or_conflict
    from services.crm_delete import (
        cascade_delete_invoices,
        cascade_pe_draft_timesheets,
        invoices_for_timesheet_ids,
        pe_timesheet_query,
        purge_timesheet,
    )

    pe = get_pe_or_404(db, pe_id)
    ts_rows = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())
    ts_ids = [t.id for t in ts_rows]
    linked_invoices = invoices_for_timesheet_ids(db, ts_ids)
    if linked_invoices:
        # Cascade unpaid invoices; payments / credit notes raise 409 with numbers.
        cascade_delete_invoices(db, linked_invoices)
        db.flush()
        # Refresh timesheet list after invoice removal.
        ts_rows = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())

    # Cascade draft / rejected sheets; then purge remaining approved (invoices gone).
    cascade_pe_draft_timesheets(db, pe_id, pe.project_id, pe.employee_id)
    remaining = list(db.execute(pe_timesheet_query(pe_id, pe.project_id, pe.employee_id)).scalars().all())
    approved_left = [t for t in remaining if (
        t.status.value if hasattr(t.status, "value") else str(t.status)
    ) == TimesheetStatus.APPROVED.value]
    # After invoices cascaded, approved sheets are safe to purge for cleanup.
    for ts in approved_left:
        purge_timesheet(db, ts)

    approved_leave = db.execute(
        select(func.count()).select_from(LeaveApplication).where(
            LeaveApplication.project_employee_id == pe_id,
            LeaveApplication.status == "Approved",
        )
    ).scalar() or 0
    if approved_leave:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: {approved_leave} approved leave application(s) exist. "
                "Cancel or remove them first."
            ),
        )
    for app in db.execute(
        select(LeaveApplication).where(LeaveApplication.project_employee_id == pe_id)
    ).scalars().all():
        app.project_employee_id = None
    db.delete(pe)
    commit_or_conflict(
        db,
        "Cannot delete: project employee is still referenced by other records.",
    )
    return envelope(data={"id": pe_id}, message="Project employee deleted")


@router.put("/employees/{pe_id}/leave/{leave_id}")
def update_pe_leave_detail(
    pe_id: int,
    leave_id: int,
    body: ProjectEmployeeLeaveDetailUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("project-employees", "HR", "Sales_Head")),
):
    get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeLeaveDetail, leave_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Leave detail not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    policy = (
        db.get(CustomerLeavePolicy, row.customer_leave_policy_id)
        if row.customer_leave_policy_id else None
    )
    return envelope(
        data=leave_detail_out(row, policy=policy),
        message="Leave detail updated",
    )

@router.get("/employees/{pe_id}/rates")
def list_pe_rates(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    get_pe_or_404(db, pe_id)
    rows = db.execute(
        select(ProjectEmployeeRate)
        .where(ProjectEmployeeRate.project_employee_id == pe_id)
        .order_by(ProjectEmployeeRate.effective_from.desc(), ProjectEmployeeRate.id.desc())
    ).scalars().all()
    return envelope(data=[rate_out(r) for r in rows])


@router.post("/employees/{pe_id}/rates")
def create_pe_rate(
    pe_id: int,
    body: ProjectEmployeeRateIn,
    db: Session = Depends(get_crm_db),
    # RMG added: they own invoice generation, and the Add Rate button on the
    # PO selection panel must work for the person raising the invoice.
    user: CurrentUser = Depends(gated_write_action("project_employee.rates", "project-employees", "Sales_Head", "Finance", "HR", "RMG")),
):
    pe = get_pe_or_404(db, pe_id)
    if body.is_current_rate:
        clear_other_current_rates(db, pe.id)
    row = ProjectEmployeeRate(
        project_employee_id=pe.id,
        effective_from=body.effective_from,
        rate=body.rate,
        billing_unit=body.billing_unit or pe.billing_unit,
        is_current_rate=body.is_current_rate,
    )
    db.add(row)
    db.flush()
    if row.is_current_rate:
        sync_pe_billing_from_current_rate(pe, row)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_out(row), message="Rate added")


@router.put("/employees/{pe_id}/rates/{rate_id}")
def update_pe_rate(
    pe_id: int,
    rate_id: int,
    body: ProjectEmployeeRateUpdate,
    db: Session = Depends(get_crm_db),
    # RMG added for the same reason as create_pe_rate (Edit Rate on PO panel).
    user: CurrentUser = Depends(gated_write_action("project_employee.rates", "project-employees", "Sales_Head", "Finance", "HR", "RMG")),
):
    pe = get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeRate, rate_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Rate not found")
    changes = body.model_dump(exclude_unset=True)
    make_current = changes.pop("is_current_rate", None)
    for field, value in changes.items():
        setattr(row, field, value)
    if make_current is True:
        clear_other_current_rates(db, pe.id, keep_id=row.id)
        row.is_current_rate = True
        sync_pe_billing_from_current_rate(pe, row)
    elif make_current is False:
        row.is_current_rate = False
    elif row.is_current_rate:
        sync_pe_billing_from_current_rate(pe, row)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_out(row), message="Rate updated")


@router.delete("/employees/{pe_id}/rates/{rate_id}")
def delete_pe_rate(
    pe_id: int,
    rate_id: int,
    db: Session = Depends(get_crm_db),
    # RMG matches create/update: whoever manages rates from the invoice flow
    # must be able to remove a wrong row too.
    user: CurrentUser = Depends(gated_write_action("project_employee.rates", "project-employees", "Sales_Head", "Finance", "HR", "RMG")),
):
    pe = get_pe_or_404(db, pe_id)
    row = db.get(ProjectEmployeeRate, rate_id)
    if not row or row.project_employee_id != pe_id:
        raise HTTPException(status_code=404, detail="Rate not found")
    was_current = row.is_current_rate
    db.delete(row)
    db.flush()
    if was_current:
        replacement = db.execute(
            select(ProjectEmployeeRate)
            .where(ProjectEmployeeRate.project_employee_id == pe.id)
            .order_by(ProjectEmployeeRate.effective_from.desc())
        ).scalars().first()
        if replacement:
            replacement.is_current_rate = True
            sync_pe_billing_from_current_rate(pe, replacement)
    db.commit()
    return envelope(message="Rate deleted")


@router.get("/{project_id}")
def get_project(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    project = get_project_or_404(db, project_id)
    return envelope(data=project_detail_out(db, project))


@router.get("/{project_id}/history")
def get_project_history(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    """Project 360°: aggregated read-only history/health view of one project."""
    project = get_project_or_404(db, project_id)
    return envelope(data=project_history(db, project))


@router.put("/{project_id}")
def update_project(
    project_id: int,
    body: ProjectUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    project = get_project_or_404(db, project_id)
    changes = body.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), 'projects', changes, {"name": "name", "customer_id": "customer_id", "branch_id": "branch_id",
        "status": "status", "billing_frequency": "billing_frequency",
        "billing_cycle_start_day": "billing_cycle_start_day",
        "billing_cycle_end_day": "billing_cycle_end_day",
        "recurring_billing": "recurring_billing",
        "week_off_billable": "billability", "holidays_billable": "billability",
        "leave_billable": "billability", "comp_off_billable": "billability",
        "hours_required_half_day": "thresholds", "hours_required_full_day": "thresholds",
        "max_billable_hours_day": "caps", "max_billable_hours_month": "caps"})

    validate_project_refs(db, changes.get("opportunity_id"), changes.get("customer_id"))
    if "branch_id" in changes and changes["branch_id"] is not None:
        from models import CustomerBranch
        cust_id = changes.get("customer_id", project.customer_id)
        branch_row = db.get(CustomerBranch, changes["branch_id"])
        if branch_row is None or branch_row.customer_id != cust_id:
            raise HTTPException(status_code=400, detail="Branch not found for this customer")
    # Guard hour threshold ordering when both (or one + existing) are present.
    half = changes.get("hours_required_half_day", project.hours_required_half_day)
    full = changes.get("hours_required_full_day", project.hours_required_full_day)
    if half is not None and full is not None and Decimal(str(half)) > Decimal(str(full)):
        raise HTTPException(
            status_code=400,
            detail="hours_required_half_day cannot exceed hours_required_full_day",
        )
    for field, value in changes.items():
        setattr(project, field, value)
    # If opportunity changed and branch_id was not explicitly set, adopt opp.branch_id
    # only when same-customer. Never clear an existing non-null project.branch_id
    # when the new opportunity points at a foreign/missing branch.
    if "opportunity_id" in changes and "branch_id" not in changes:
        from models import Opportunity
        opp = db.get(Opportunity, project.opportunity_id) if project.opportunity_id else None
        if opp is not None and opp.branch_id is not None:
            opp_branch = db.get(CustomerBranch, opp.branch_id)
            if opp_branch is not None and opp_branch.customer_id == project.customer_id:
                project.branch_id = opp.branch_id
            # else: keep existing project.branch_id (do not null it)
    db.commit()
    db.refresh(project)
    return envelope(data=project_out(project), message="Project updated")


@router.delete("/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Hard-delete a project.

    Cascades team assignments, timesheets, and deletable invoices (+ unpaid TDS).
    Approved leave apps still block. Invoices with payments / credit notes 409
    with invoice numbers.
    """
    from models import (
        EmployeeProjectHistory, LeaveApplication, POProjectAllocation, TimesheetEntry,
    )
    from services.crm_common import commit_or_conflict
    from services.crm_delete import (
        cascade_delete_invoices,
        cascade_project_timesheets,
        invoices_for_project,
        purge_timesheet,
    )

    project = get_project_or_404(db, project_id)
    approved_leave = db.execute(
        select(func.count()).select_from(LeaveApplication).where(
            LeaveApplication.project_id == project_id,
            LeaveApplication.status == "Approved",
        )
    ).scalar() or 0
    if approved_leave:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: {approved_leave} approved leave application(s) exist. "
                "Remove them first."
            ),
        )

    # Cascade deletable invoices first (payments / CNs block with numbers).
    invs = invoices_for_project(db, project_id)
    if invs:
        cascade_delete_invoices(db, invs)
        db.flush()

    # Draft sheets, then any remaining (approved) after invoices are gone.
    cascade_project_timesheets(db, project_id, include_approved=False)
    for ts in db.execute(
        select(Timesheet).where(Timesheet.project_id == project_id)
    ).scalars().all():
        purge_timesheet(db, ts)

    # HR project history has no cascade — drop with the project.
    for hist in db.execute(
        select(EmployeeProjectHistory).where(EmployeeProjectHistory.project_id == project_id)
    ).scalars().all():
        db.delete(hist)
    # Soft-clear leave apps that still point at this project (non-Approved only).
    for app in db.execute(
        select(LeaveApplication).where(LeaveApplication.project_id == project_id)
    ).scalars().all():
        app.project_id = None
        app.project_employee_id = None
    # Split-billing entries on *other* timesheets may still reference this project.
    for entry in db.execute(
        select(TimesheetEntry).where(TimesheetEntry.entry_project_id == project_id)
    ).scalars().all():
        entry.entry_project_id = None
    # Purge all PE rows (active + inactive) after finance/timesheet cascade.
    for pe in db.execute(
        select(ProjectEmployee).where(ProjectEmployee.project_id == project_id)
    ).scalars().all():
        for app in db.execute(
            select(LeaveApplication).where(LeaveApplication.project_employee_id == pe.id)
        ).scalars().all():
            app.project_employee_id = None
        db.delete(pe)
    # Drop PO allocations that only reference this project (non-financial orphan).
    allocs = db.execute(
        select(POProjectAllocation).where(POProjectAllocation.project_id == project_id)
    ).scalars().all()
    for a in allocs:
        db.delete(a)
    db.delete(project)
    commit_or_conflict(
        db,
        "Cannot delete: project is still referenced by other records. Remove dependencies first.",
    )
    return envelope(data={"id": project_id}, message="Project deleted")


# ---------------------------------------------------------------- leave policies (project-scoped)

@router.get("/{project_id}/leave-policies")
def list_project_leave_policies(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    get_project_or_404(db, project_id)
    pols = db.execute(
        select(ProjectLeavePolicy)
        .where(ProjectLeavePolicy.project_id == project_id,
               ProjectLeavePolicy.is_active.is_(True))
        .order_by(ProjectLeavePolicy.id)
    ).scalars().all()
    return envelope(
        data=[project_leave_policy_out(db, p) for p in pols],
        message="Project leave policies",
    )


@router.post("/{project_id}/leave-policies")
def create_project_leave_policy(
    project_id: int,
    body: ProjectLeavePolicyCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    get_project_or_404(db, project_id)
    if db.get(LeavePolicyType, body.leave_type_id) is None:
        raise HTTPException(status_code=400, detail="Leave policy type not found")
    dup = db.execute(
        select(ProjectLeavePolicy).where(
            ProjectLeavePolicy.project_id == project_id,
            ProjectLeavePolicy.leave_type_id == body.leave_type_id,
        ).limit(1)
    ).scalars().first()
    data = apply_leave_expire_timing_consistency(body.model_dump())
    if dup is not None:
        if not dup.is_active:
            # Reactivate soft-deleted row with new values
            for field, value in data.items():
                setattr(dup, field, value)
            dup.is_active = True
            db.commit()
            db.refresh(dup)
            return envelope(data=project_leave_policy_out(db, dup),
                            message="Leave policy reactivated")
        raise HTTPException(
            status_code=409,
            detail="A leave policy for this project/leave type already exists",
        )
    policy = ProjectLeavePolicy(project_id=project_id, **data)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy created")


@router.put("/{project_id}/leave-policies/{policy_id}")
def update_project_leave_policy(
    project_id: int,
    policy_id: int,
    body: ProjectLeavePolicyUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Nested alias — prefer PUT /api/projects/leave-policies/{id}."""
    get_project_or_404(db, project_id)
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None or policy.project_id != project_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this project")
    policy = _apply_leave_policy_update(db, policy, body)
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy updated")


@router.delete("/{project_id}/leave-policies/{policy_id}")
def delete_project_leave_policy(
    project_id: int,
    policy_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_projects),
):
    """Nested alias — prefer DELETE /api/projects/leave-policies/{id}."""
    get_project_or_404(db, project_id)
    policy = db.get(ProjectLeavePolicy, policy_id)
    if policy is None or policy.project_id != project_id:
        raise HTTPException(status_code=404, detail="Leave policy not found for this project")
    policy.is_active = False
    db.commit()
    return envelope(data=project_leave_policy_out(db, policy), message="Leave policy deactivated")


# ---------------------------------------------------------------- team

@router.post("/{project_id}/employees")
def add_project_employee(
    project_id: int,
    body: ProjectEmployeeIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    project = get_project_or_404(db, project_id)
    employee = db.get(Employee, body.employee_id)
    if not employee:
        raise HTTPException(status_code=400, detail="Employee not found")
    existing = db.execute(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == project.id,
            ProjectEmployee.employee_id == body.employee_id,
        )
    ).scalars().first()
    if existing and existing.is_active:
        raise HTTPException(status_code=409, detail="Employee is already assigned to this project")
    if existing:
        # Re-activate the previous (deactivated) assignment with the new terms.
        existing.onboarding_date = body.onboarding_date
        existing.experience_years = body.experience_years
        existing.project_experience_years = body.project_experience_years
        existing.work_mode = body.work_mode
        existing.billing_rate = body.billing_rate
        existing.billing_unit = body.billing_unit
        existing.is_active = True
        existing.is_exit = body.is_exit
        existing.exit_date = body.exit_date
        existing.billing_date = body.billing_date
        existing.settlement_pending = False
        pe = existing
    else:
        pe = ProjectEmployee(
            project_id=project.id,
            employee_id=body.employee_id,
            onboarding_date=body.onboarding_date,
            experience_years=body.experience_years,
            project_experience_years=body.project_experience_years,
            work_mode=body.work_mode,
            billing_rate=body.billing_rate,
            billing_unit=body.billing_unit,
            is_active=True,
            is_exit=body.is_exit,
            exit_date=body.exit_date,
            billing_date=body.billing_date,
            settlement_pending=False,
        )
        db.add(pe)
    db.flush()
    if body.rates:
        # The wizard's Commercial Details ARE the rate history; billing_rate is
        # re-derived from whichever row is in force today.
        apply_rate_rows(db, pe, body.rates)
    else:
        ensure_initial_rate(db, pe)
    seed_leave_details_from_customer_policy(db, pe, project)
    add_history_entry(db, project.id, employee, body.onboarding_date)
    db.commit()
    db.refresh(pe)
    return envelope(data=project_employee_out(pe, employee), message="Employee assigned to project")


@router.put("/{project_id}/employees/{pe_id}")
def update_project_employee(
    project_id: int,
    pe_id: int,
    body: ProjectEmployeeUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    pe = db.get(ProjectEmployee, pe_id)
    if not pe or pe.project_id != project_id:
        raise HTTPException(status_code=404, detail="Project employee not found")
    changes = body.model_dump(exclude_unset=True)
    deactivating = pe.is_active and changes.get("is_active") is False
    rate_changed = "billing_rate" in changes or "billing_unit" in changes
    for field, value in changes.items():
        setattr(pe, field, value)
    if deactivating:
        history = open_history_row(db, project_id, pe.employee_id)
        if history:
            history.end_date = date.today()
    if rate_changed:
        # UPSERT through apply_rate_rows instead of blindly adding a row.
        # The old code appended a NEW rate at billing_date on every save of
        # this form, so repeated edits stacked duplicate effective dates —
        # histories grew twins like three rates all starting 01 Apr. Same
        # date now refines the existing row; a different date starts a new
        # one, exactly like the Map Employee wizard.
        from schemas.projects import MapRateIn
        apply_rate_rows(db, pe, [MapRateIn(
            effective_from=pe.billing_date or date.today(),
            rate=pe.billing_rate,
        )])
    db.commit()
    db.refresh(pe)
    return envelope(data=project_employee_out(pe), message="Project employee updated")


# ---------------------------------------------------------------- timesheets

def _coverage_today() -> date:
    """Seam for tests to freeze 'today' without patching the module's `date`
    (FastAPI resolves `date | None` annotations lazily, so a MagicMock there
    breaks route building)."""
    return date.today()


def _due_timesheet_rows(
    db: Session,
    project_id: int,
    existing: set[tuple[int, int, int]],
    employee_id: int | None,
) -> list[dict]:
    """Placeholder rows for every month a PE SHOULD have a timesheet but none
    exists — from onboarding month through the current month (or exit month).

    An onboarded employee with no sheet used to be invisible on this tab: the
    list showed what was filed, never what was missing, so "January exists"
    read as "everything is fine" even in June. Coverage is derived from the
    project-employee mapping, not from the sheets, so a month that was never
    created still shows — as `status: "Due"` with no id.
    """
    today = _coverage_today()
    pes = db.execute(
        select(ProjectEmployee).where(ProjectEmployee.project_id == project_id)
    ).scalars().all()

    rows: list[dict] = []
    seen: set[tuple[int, int, int]] = set()
    for pe in pes:
        if employee_id is not None and pe.employee_id != employee_id:
            continue
        start = pe.onboarding_date
        if start is None:
            continue
        # Exited employees owe sheets only up to their exit month.
        end = pe.exit_date if (pe.is_exit and pe.exit_date) else today
        y, m = start.year, start.month
        while (y, m) <= (end.year, end.month):
            key = (pe.employee_id, y, m)
            if key not in existing and key not in seen:
                seen.add(key)
                rows.append({
                    "id": None,
                    "project_id": project_id,
                    "employee_id": pe.employee_id,
                    "project_employee_id": pe.id,
                    "month": m,
                    "year": y,
                    "status": "Due",
                    "status_label": "Due — not created",
                    "rejection_reason": None,
                    "submitted_at": None,
                    "approved_by": None,
                    "approved_at": None,
                })
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return rows


@router.get("/{project_id}/timesheets")
def project_timesheets(
    project_id: int,
    month: int | None = None,
    year: int | None = None,
    status: str | None = None,
    employee_id: int | None = None,
    include_missing: bool = False,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("timesheets", "HR", "Finance", "Sales_Head")),
):
    get_project_or_404(db, project_id)
    stmt = select(Timesheet).where(Timesheet.project_id == project_id).order_by(Timesheet.id.desc())
    if month is not None:
        stmt = stmt.where(Timesheet.month == month)
    if year is not None:
        stmt = stmt.where(Timesheet.year == year)
    if employee_id is not None:
        # One person's full timesheet history on this project — the review
        # flow is per-employee, not per-page-of-everyone.
        stmt = stmt.where(Timesheet.employee_id == employee_id)
    # "Due" is a synthetic status (a sheet that does not exist); every stored
    # status filters the real rows as before.
    due_only = status == "Due"
    if status and not due_only:
        try:
            stmt = stmt.where(Timesheet.status == TimesheetStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if not (include_missing or due_only):
        items, meta = paginate(db, stmt, params.page, params.limit)
        return envelope(data=[timesheet_out(ts) for ts in items], meta=meta)

    # Coverage view: merge real sheets with Due placeholders, newest first.
    real = [] if due_only else [timesheet_out(ts) for ts in db.execute(stmt).scalars().all()]
    # The existence check must ignore the month/year filters, or a filtered
    # request would resurrect months that do have sheets.
    all_keys = {
        (ts.employee_id, ts.year, ts.month)
        for ts in db.execute(
            select(Timesheet).where(Timesheet.project_id == project_id)
        ).scalars().all()
    }
    due = _due_timesheet_rows(db, project_id, all_keys, employee_id)
    if month is not None:
        due = [r for r in due if r["month"] == month]
    if year is not None:
        due = [r for r in due if r["year"] == year]

    combined = sorted(real + due, key=lambda r: (r["year"], r["month"]), reverse=True)
    total = len(combined)
    start_i = (params.page - 1) * params.limit
    page_rows = combined[start_i:start_i + params.limit]
    pages = (total + params.limit - 1) // params.limit if params.limit else 1
    meta = {"page": params.page, "limit": params.limit, "total": total, "pages": pages}
    return envelope(data=page_rows, meta=meta)


# ---------------------------------------------------------------- communication matrix

@router.get("/{project_id}/communication-matrix")
def list_communication_matrix(
    project_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_projects),
):
    get_project_or_404(db, project_id)
    rows = db.execute(
        select(ProjectCommunicationMatrix)
        .where(ProjectCommunicationMatrix.project_id == project_id)
        .order_by(ProjectCommunicationMatrix.id)
    ).scalars().all()
    return envelope(data=[comm_entry_out(e) for e in rows])


@router.post("/{project_id}/communication-matrix")
def add_communication_entry(
    project_id: int,
    body: CommMatrixIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    entry = ProjectCommunicationMatrix(
        project_id=project_id,
        name=body.name,
        role=body.role,
        responsible_person=body.responsible_person,
        email=body.email,
        phone=body.phone,
        type=body.type,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return envelope(data=comm_entry_out(entry), message="Communication matrix entry added")


@router.delete("/{project_id}/communication-matrix/{entry_id}")
def delete_communication_entry(
    project_id: int,
    entry_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_pe),
):
    get_project_or_404(db, project_id)
    entry = db.get(ProjectCommunicationMatrix, entry_id)
    if not entry or entry.project_id != project_id:
        raise HTTPException(status_code=404, detail="Communication matrix entry not found")
    db.delete(entry)
    db.commit()
    return envelope(message="Communication matrix entry deleted")


@router.get("/employees/{pe_id}/invoices")
def get_project_employee_invoices(
    pe_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_pe),
):
    """Read-only invoice preview per timesheet period for one Project Employee
    mapping (billable days × rate / split rates, PO drawdown, linked invoice)."""
    from services.project_employees import invoice_rollups_for_pe
    pe = get_pe_or_404(db, pe_id)
    return envelope(data=invoice_rollups_for_pe(db, pe))
