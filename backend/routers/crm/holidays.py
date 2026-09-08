"""Holiday calendar API (/api/holidays, /api/holiday-names).

Feeds timesheet day generation: entries whose date matches an active mandatory
holiday for the project's customer/branch are pre-marked Holiday / non-working.
Read: any authenticated CRM user. Write: HR (Admin/CEO pass implicitly).
Rows can be soft-deactivated (`POST /{id}/deactivate`) or permanently removed
(`DELETE /{id}`). Customer-scoped holidays require a branch and are linked to
branch_holiday_years so the Holidays tab and branch Holiday Billing Policy stay
in sync.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import extract, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_create, get_crm_db, get_current_user, page_params,
    role_required, gated_write)
from crm_deps import gated_write
from models import Customer, CustomerBranch, Holiday, HolidayName
from schemas.common import envelope
from schemas.leave import HolidayCreate, HolidayNameCreate, HolidayUpdate
from services.crm_common import paginate, to_dict
from services.holidays import (
    create_holiday_rows_for_branches,
    holiday_out,
    link_holiday_to_branch_calendar,
    require_branch_for_customer_scope,
    resolve_holiday_name,
    resolve_target_branches,
)

router = APIRouter(prefix="/api/holidays", tags=["CRM: Holidays"])
names_router = APIRouter(prefix="/api/holiday-names", tags=["CRM: Holiday Names"])

hr_only = gated_write("holidays", "HR")
create_holidays_gate = gated_create("holidays", "HR")


def _get_or_404(db: Session, holiday_id: int) -> Holiday:
    obj = db.get(Holiday, holiday_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Holiday not found")
    return obj


def _validate_refs(db: Session, customer_id: int | None, branch_id: int | None) -> None:
    if customer_id is not None and db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="Customer not found")
    if branch_id is not None:
        branch = db.get(CustomerBranch, branch_id)
        if branch is None:
            raise HTTPException(status_code=400, detail="Customer branch not found")
        if customer_id is not None and branch.customer_id != customer_id:
            raise HTTPException(status_code=400,
                                detail="Branch does not belong to the given customer")


# ---------------------------------------------------------------- holiday names
@names_router.get("")
def list_holiday_names(
    is_active: bool = True,
    q: str | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(HolidayName).where(HolidayName.is_active == is_active)
    if q:
        stmt = stmt.where(HolidayName.name.ilike(f"%{q.strip()}%"))
    stmt = stmt.order_by(HolidayName.name.asc())
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[to_dict(n) for n in items], message="Holiday names", meta=meta)


@names_router.post("")
def create_holiday_name(
    body: HolidayNameCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    exists = db.execute(
        select(HolidayName).where(HolidayName.name == body.name.strip())
    ).scalars().first()
    if exists is not None:
        raise HTTPException(status_code=400, detail="Holiday name already exists")
    obj = HolidayName(name=body.name.strip(), is_active=body.is_active)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return envelope(data=to_dict(obj), message="Holiday name created")


# ---------------------------------------------------------------- holidays
@router.get("")
def list_holidays(
    year: int | None = None,
    month: int | None = None,
    customer_id: int | None = None,
    branch_id: int | None = None,
    holiday_type: str | None = None,
    observance: str | None = None,
    # Prefer status=active|inactive|all. is_active kept for older callers.
    status: str | None = None,
    is_active: bool | None = None,
    params: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = select(Holiday)
    status_key = (status or "").strip().lower()
    if status_key == "all":
        pass
    elif status_key == "inactive" or is_active is False:
        stmt = stmt.where(Holiday.is_active.is_(False))
    elif status_key == "active" or is_active is True or (status is None and is_active is None):
        stmt = stmt.where(Holiday.is_active.is_(True))
    if year is not None:
        stmt = stmt.where(Holiday.year == year)
    if month is not None:
        stmt = stmt.where(extract("month", Holiday.holiday_date) == month)
    if customer_id is not None:
        stmt = stmt.where(Holiday.customer_id == customer_id)
    if branch_id is not None:
        stmt = stmt.where(Holiday.branch_id == branch_id)
    if holiday_type:
        stmt = stmt.where(Holiday.holiday_type == holiday_type)
    if observance:
        stmt = stmt.where(Holiday.observance == observance)
    stmt = stmt.order_by(Holiday.holiday_date.asc(), Holiday.id.asc())
    items, meta = paginate(db, stmt, params.page, params.limit)
    return envelope(data=[holiday_out(h) for h in items], message="Holiday list", meta=meta)


@router.post("")
def create_holiday(
    body: HolidayCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(create_holidays_gate),
):
    all_branches = bool(body.all_branches)
    branch_ids = list(body.branch_ids or [])
    require_branch_for_customer_scope(
        holiday_type=body.holiday_type,
        customer_id=body.customer_id,
        branch_id=None if all_branches else body.branch_id,
        branch_ids=None if all_branches else branch_ids,
        all_branches=all_branches,
    )
    name_id, name = resolve_holiday_name(db, holiday_name_id=body.holiday_name_id, name=body.name)

    # Multi-branch mapping (All Branch or explicit branch_ids).
    if body.customer_id is not None and (all_branches or branch_ids):
        targets = resolve_target_branches(
            db,
            customer_id=body.customer_id,
            branch_id=None,
            branch_ids=[] if all_branches else branch_ids,
            all_branches=all_branches,
        )
        for b in targets:
            _validate_refs(db, body.customer_id, b.id)
        created = create_holiday_rows_for_branches(
            db,
            branches=targets,
            holiday_name_id=name_id,
            name=name,
            holiday_date=body.holiday_date,
            holiday_type=body.holiday_type,
            observance=body.observance,
            is_active=body.is_active,
        )
        db.commit()
        for obj in created:
            db.refresh(obj)
        out = [holiday_out(h) for h in created]
        return envelope(
            data=out if len(out) > 1 else out[0],
            message=(
                f"Holiday '{name}' mapped to {len(created)} branch(es)"
                if len(created) > 1
                else "Holiday created"
            ),
        )

    _validate_refs(db, body.customer_id, body.branch_id)
    obj = Holiday(
        holiday_name_id=name_id,
        name=name,
        holiday_date=body.holiday_date,
        holiday_type=body.holiday_type,
        observance=body.observance,
        customer_id=body.customer_id,
        branch_id=body.branch_id,
        year=body.holiday_date.year,
        is_active=body.is_active,
    )
    link_holiday_to_branch_calendar(db, obj)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return envelope(data=holiday_out(obj), message="Holiday created")


@router.get("/{holiday_id}")
def get_holiday(
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    return envelope(data=holiday_out(_get_or_404(db, holiday_id)))


@router.put("/{holiday_id}")
def update_holiday(
    holiday_id: int,
    body: HolidayUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    obj = _get_or_404(db, holiday_id)
    data = body.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), 'holidays', data, {"customer_id": "customer_id", "branch_id": "branch_id", "year": "year",
        "holiday_date": "holiday_date", "name": "name",
        "holiday_type": "holiday_type", "observance": "observance"})

    all_branches = bool(data.pop("all_branches", False) or body.all_branches)
    branch_ids = list(data.pop("branch_ids", None) or body.branch_ids or [])
    customer_id = data.get("customer_id", obj.customer_id)
    branch_id = None if (all_branches or branch_ids) else data.get("branch_id", obj.branch_id)
    holiday_type = data.get("holiday_type", obj.holiday_type)
    require_branch_for_customer_scope(
        holiday_type=holiday_type,
        customer_id=customer_id,
        branch_id=branch_id,
        branch_ids=branch_ids,
        all_branches=all_branches,
    )
    _validate_refs(db, customer_id, branch_id)
    if "holiday_name_id" in data or "name" in data:
        name_id, name = resolve_holiday_name(
            db,
            holiday_name_id=data.get("holiday_name_id", obj.holiday_name_id),
            name=data.get("name", obj.name if "holiday_name_id" not in data else None),
        )
        obj.holiday_name_id = name_id
        obj.name = name
        data.pop("holiday_name_id", None)
        data.pop("name", None)
    for field, value in data.items():
        setattr(obj, field, value)
    obj.year = obj.holiday_date.year

    if customer_id is not None and (all_branches or branch_ids):
        targets = resolve_target_branches(
            db,
            customer_id=customer_id,
            branch_id=None,
            branch_ids=[] if all_branches else branch_ids,
            all_branches=all_branches,
        )
        if obj.branch_id is None and targets:
            obj.branch_id = targets[0].id
            obj.customer_id = customer_id
        link_holiday_to_branch_calendar(db, obj)
        created = create_holiday_rows_for_branches(
            db,
            branches=targets,
            holiday_name_id=obj.holiday_name_id,
            name=obj.name,
            holiday_date=obj.holiday_date,
            holiday_type=obj.holiday_type,
            observance=obj.observance,
            is_active=bool(obj.is_active),
        )
        db.commit()
        for h in created:
            db.refresh(h)
        out = [holiday_out(h) for h in created]
        return envelope(
            data=out if len(out) > 1 else out[0],
            message=(
                f"Holiday '{obj.name}' mapped to {len(created)} branch(es)"
                if len(created) > 1
                else "Holiday updated"
            ),
        )

    link_holiday_to_branch_calendar(db, obj)
    db.commit()
    db.refresh(obj)
    return envelope(data=holiday_out(obj), message="Holiday updated")


@router.post("/{holiday_id}/deactivate")
def deactivate_holiday(
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    """Soft-deactivate — row stays for history; no longer feeds timesheets."""
    obj = _get_or_404(db, holiday_id)
    if obj.branch_id is not None:
        from services.branch_policy import ensure_year_editable
        ensure_year_editable(db, obj.branch_id, obj.year)
    obj.is_active = False
    db.commit()
    return envelope(data=holiday_out(obj), message="Holiday deactivated")


@router.delete("/{holiday_id}")
def delete_holiday(
    holiday_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(hr_only),
):
    """Permanently remove a holiday calendar row."""
    obj = _get_or_404(db, holiday_id)
    if obj.branch_id is not None:
        from services.branch_policy import ensure_year_editable
        ensure_year_editable(db, obj.branch_id, obj.year)
    snapshot = holiday_out(obj)
    db.delete(obj)
    db.commit()
    return envelope(data=snapshot, message=f"Holiday '{snapshot.get('name')}' deleted")
