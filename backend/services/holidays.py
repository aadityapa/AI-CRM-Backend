"""Holiday calendar helpers — name resolution and branch-year scoped rows.

Customer/regional holiday dates are owned by a **customer branch**. The Holidays
tab and the branch Holiday Billing Policy panel both read/write the same
`holidays` rows; Holidays-tab writes must upsert `branch_holiday_years` and set
`holiday_calendar_id` so the branch UI year headers and counts stay in sync.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import BranchHolidayYear, CustomerBranch, Holiday, HolidayName
from services.crm_common import to_dict


def resolve_holiday_name(
    db: Session,
    *,
    holiday_name_id: int | None,
    name: str | None,
) -> tuple[int | None, str]:
    """Return (holiday_name_id, denormalized name). Requires one of the inputs."""
    if holiday_name_id is not None:
        row = db.get(HolidayName, holiday_name_id)
        if row is None or not row.is_active:
            raise HTTPException(status_code=400, detail="Holiday name not found or inactive")
        return row.id, row.name
    cleaned = (name or "").strip()
    if cleaned:
        return None, cleaned
    raise HTTPException(status_code=400, detail="holiday_name_id or name is required")


def get_branch_year_or_404(
    db: Session,
    branch_id: int,
    calendar_year: int,
) -> BranchHolidayYear:
    row = db.execute(
        select(BranchHolidayYear).where(
            BranchHolidayYear.branch_id == branch_id,
            BranchHolidayYear.calendar_year == calendar_year,
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Holiday year not found for this branch")
    return row


def ensure_branch_holiday_year(
    db: Session,
    branch_id: int,
    calendar_year: int,
    *,
    create_if_missing: bool = True,
) -> BranchHolidayYear:
    """Return the branch year header; optionally create it (Holidays-tab sync)."""
    row = db.execute(
        select(BranchHolidayYear).where(
            BranchHolidayYear.branch_id == branch_id,
            BranchHolidayYear.calendar_year == calendar_year,
        )
    ).scalars().first()
    if row is not None:
        return row
    if not create_if_missing:
        raise HTTPException(status_code=404, detail="Holiday year not found for this branch")
    row = BranchHolidayYear(
        branch_id=branch_id,
        calendar_year=calendar_year,
        is_freeze=False,
    )
    db.add(row)
    db.flush()
    return row


def require_branch_for_customer_scope(
    *,
    holiday_type: str | None,
    customer_id: int | None,
    branch_id: int | None,
    branch_ids: list[int] | None = None,
    all_branches: bool = False,
) -> None:
    """Customer-scoped holidays must name branch(es) (or All Branch) so UIs stay in sync."""
    scoped = (holiday_type or "").strip() == "Customer" or customer_id is not None
    has_branches = branch_id is not None or bool(branch_ids)
    if scoped and not has_branches and not all_branches:
        raise HTTPException(
            status_code=400,
            detail=(
                "Select one or more customer branches for this holiday, or choose All Branch. "
                "Customer holidays are managed per branch so they appear in both "
                "the Holidays tab and the branch Holiday Billing Policy."
            ),
        )
    if all_branches and customer_id is None:
        raise HTTPException(
            status_code=400,
            detail="Select a customer before choosing All Branch.",
        )
    if has_branches and customer_id is None:
        raise HTTPException(
            status_code=400,
            detail="Select a customer when mapping branches.",
        )


def resolve_target_branches(
    db: Session,
    *,
    customer_id: int,
    branch_id: int | None = None,
    branch_ids: list[int] | None = None,
    all_branches: bool = False,
) -> list[CustomerBranch]:
    """Return the customer branches a holiday should be mapped onto."""
    if all_branches:
        rows = db.execute(
            select(CustomerBranch)
            .where(CustomerBranch.customer_id == customer_id)
            .order_by(CustomerBranch.branch_name.asc())
        ).scalars().all()
        if not rows:
            raise HTTPException(status_code=400, detail="This customer has no branches to map")
        return list(rows)

    ids = list(dict.fromkeys([*(branch_ids or []), *([branch_id] if branch_id is not None else [])]))
    if not ids:
        return []
    rows = db.execute(
        select(CustomerBranch).where(
            CustomerBranch.customer_id == customer_id,
            CustomerBranch.id.in_(ids),
        ).order_by(CustomerBranch.branch_name.asc())
    ).scalars().all()
    found = {b.id for b in rows}
    missing = [i for i in ids if i not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Branch id(s) {missing} do not belong to the selected customer",
        )
    return list(rows)


def create_holiday_rows_for_branches(
    db: Session,
    *,
    branches: list[CustomerBranch],
    holiday_name_id: int | None,
    name: str,
    holiday_date,
    holiday_type: str,
    observance: str,
    is_active: bool = True,
) -> list[Holiday]:
    """Create one linked holiday row per branch (skip active duplicates on same date)."""
    created: list[Holiday] = []
    for branch in branches:
        dup = db.execute(
            select(Holiday).where(
                Holiday.branch_id == branch.id,
                Holiday.holiday_date == holiday_date,
                Holiday.is_active.is_(True),
            )
        ).scalars().first()
        if dup is not None:
            # Refresh name/type/observance on the existing row so All Branch stays consistent.
            dup.holiday_name_id = holiday_name_id
            dup.name = name
            dup.holiday_type = holiday_type
            dup.observance = observance
            link_holiday_to_branch_calendar(db, dup, check_freeze=True)
            created.append(dup)
            continue
        obj = Holiday(
            holiday_name_id=holiday_name_id,
            name=name,
            holiday_date=holiday_date,
            holiday_type=holiday_type,
            observance=observance,
            customer_id=branch.customer_id,
            branch_id=branch.id,
            year=holiday_date.year,
            is_active=is_active,
        )
        link_holiday_to_branch_calendar(db, obj)
        db.add(obj)
        created.append(obj)
    db.flush()
    return created


def link_holiday_to_branch_calendar(
    db: Session,
    holiday: Holiday,
    *,
    check_freeze: bool = True,
) -> None:
    """Ensure branch year exists and attach holiday_calendar_id / customer_id.

    No-op for global holidays (no branch_id).
    """
    if holiday.branch_id is None:
        holiday.holiday_calendar_id = None
        return
    branch = db.get(CustomerBranch, holiday.branch_id)
    if branch is None:
        raise HTTPException(status_code=400, detail="Customer branch not found")
    if holiday.customer_id is None:
        holiday.customer_id = branch.customer_id
    elif holiday.customer_id != branch.customer_id:
        raise HTTPException(
            status_code=400,
            detail="Branch does not belong to the given customer",
        )
    calendar_year = holiday.holiday_date.year
    holiday.year = calendar_year
    year_row = ensure_branch_holiday_year(db, branch.id, calendar_year)
    if check_freeze:
        from services.branch_policy import ensure_year_editable
        ensure_year_editable(db, branch.id, calendar_year)
    holiday.holiday_calendar_id = year_row.id


def branch_holidays_for_year(
    db: Session,
    branch_id: int,
    calendar_year: int,
    *,
    is_active: bool = True,
) -> list[Holiday]:
    return db.execute(
        select(Holiday).where(
            Holiday.branch_id == branch_id,
            Holiday.year == calendar_year,
            Holiday.is_active.is_(is_active),
        ).order_by(Holiday.holiday_date.asc(), Holiday.id.asc())
    ).scalars().all()


def create_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    *,
    holiday_name_id: int | None,
    name: str | None,
    holiday_date,
    observance: str,
    holiday_type: str = "Customer",
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    ensure_year_editable(db, branch.id, calendar_year)
    if holiday_date.year != calendar_year:
        raise HTTPException(
            status_code=400,
            detail=f"Holiday date must fall in calendar year {calendar_year}",
        )
    year_row = get_branch_year_or_404(db, branch.id, calendar_year)
    name_id, resolved_name = resolve_holiday_name(db, holiday_name_id=holiday_name_id, name=name)
    obj = Holiday(
        holiday_name_id=name_id,
        name=resolved_name,
        holiday_date=holiday_date,
        holiday_type=holiday_type,
        observance=observance,
        customer_id=branch.customer_id,
        branch_id=branch.id,
        holiday_calendar_id=year_row.id,
        year=calendar_year,
        is_active=True,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


def update_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    holiday: Holiday,
    data: dict,
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    if holiday.branch_id != branch.id or holiday.year != calendar_year:
        raise HTTPException(status_code=404, detail="Holiday not found in this branch year")
    ensure_year_editable(db, branch.id, calendar_year)
    if "holiday_name_id" in data or "name" in data:
        name_id, resolved_name = resolve_holiday_name(
            db,
            holiday_name_id=data.get("holiday_name_id", holiday.holiday_name_id),
            name=data.get("name", holiday.name if "holiday_name_id" not in data else None),
        )
        holiday.holiday_name_id = name_id
        holiday.name = resolved_name
        data.pop("holiday_name_id", None)
        data.pop("name", None)
    for field, value in data.items():
        setattr(holiday, field, value)
    if holiday.holiday_date.year != calendar_year:
        raise HTTPException(
            status_code=400,
            detail=f"Holiday date must fall in calendar year {calendar_year}",
        )
    holiday.year = holiday.holiday_date.year
    # Keep calendar link if the year header still matches.
    link_holiday_to_branch_calendar(db, holiday, check_freeze=False)
    db.commit()
    db.refresh(holiday)
    return holiday


def deactivate_branch_holiday(
    db: Session,
    branch: CustomerBranch,
    calendar_year: int,
    holiday: Holiday,
) -> Holiday:
    from services.branch_policy import ensure_year_editable

    if holiday.branch_id != branch.id or holiday.year != calendar_year:
        raise HTTPException(status_code=404, detail="Holiday not found in this branch year")
    ensure_year_editable(db, branch.id, calendar_year)
    holiday.is_active = False
    db.commit()
    db.refresh(holiday)
    return holiday


def holiday_out(holiday: Holiday) -> dict:
    return to_dict(holiday)
