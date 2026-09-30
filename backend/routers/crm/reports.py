"""CRM reports — any CRM role (Admin implicit); every report supports ?format=csv."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import AppSetting
from schemas.common import envelope
from services import reports as svc
from services.placements_report import placements_report
from services.revenue_export import build_revenue_workbook
from services.revenue_report import (
    TARGET_FY_KEY, TARGET_FY_PREFIX, TARGET_MONTH_KEY, TARGET_MONTH_PREFIX, Month, Period, fy_for,
    revenue_report,
)
from services.crm_common import rows_to_csv

router = APIRouter(prefix="/api/reports", tags=["CRM: Reports"])

# Any CRM role may pull reports (Admin passes implicitly via role_required).
ALL_CRM_ROLES = ("Sales", "Sales_Head", "RMG", "TA", "HR", "Finance")


def _respond(rows: list[dict], format: str | None, filename: str):
    if (format or "").strip().lower() == "csv":
        return rows_to_csv(rows, filename)  # csv bypasses the envelope
    return envelope(rows)


@router.get("/opportunities")
def opportunities_report(
    team: str | None = Query(None, description="Sales | RMG | TA (creator holds this role)"),
    status: str | None = Query(None, description="UI group (Active/On Hold/Rejected/Closed/Archived) or exact stage"),
    format: str | None = Query(None, description="csv for file download"),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*ALL_CRM_ROLES)),
):
    rows = svc.opportunities_report(db, team=team, status=status)
    return _respond(rows, format, "opportunities_report.csv")


@router.get("/candidate-profiles")
def candidate_profiles_report(
    team: str | None = Query(None, description="Sales | RMG | TA (profile creator via earliest activity log)"),
    status: str | None = Query(None, description="UI group (Active/Rejected/Joined) or exact pipeline status"),
    format: str | None = Query(None, description="csv for file download"),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*ALL_CRM_ROLES)),
):
    rows = svc.candidate_profiles_report(db, team=team, status=status)
    if (format or "").strip().lower() == "csv":
        # A file gets the status WORDS, not the badge object.
        rows = [{**{k: v for k, v in r.items() if k != "candidate_status"},
                 "status": (r.get("candidate_status") or {}).get("label")} for r in rows]
    return _respond(rows, format, "candidate_profiles_report.csv")


@router.get("/recruiter-productivity")
def recruiter_productivity_report(
    date_from: date | None = Query(None, alias="from", description="Range start (YYYY-MM-DD)"),
    date_to: date | None = Query(None, alias="to", description="Range end (YYYY-MM-DD)"),
    format: str | None = Query(None, description="csv for file download"),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required(*ALL_CRM_ROLES)),
):
    rows = svc.recruiter_productivity_report(db, date_from=date_from, date_to=date_to)
    return _respond(rows, format, "recruiter_productivity_report.csv")


@router.get("/revenue")
def revenue(
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default month)"),
    customer_id: int | None = Query(None, ge=1, description="Narrow the whole report to one customer"),
    project_id: int | None = Query(None, ge=1, description="Narrow the whole report to one project"),
    db: Session = Depends(get_crm_db),
    # CEO view (18 Sep 2026): Admin/CEO ONLY — role_required() with no roles is
    # the admin-only gate; templates cannot widen it and no other role sees it.
    user: CurrentUser = Depends(role_required()),
):
    """`month` is the ANCHOR at every zoom: `?month=2026-09&period=quarter` is
    the quarter containing September 2026, so existing `?month=` links (the
    month-close email, the dashboard tile) keep resolving unchanged."""
    try:
        return envelope(revenue_report(db, month, period_raw=period,
                                       customer_id=customer_id, project_id=project_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/revenue/placements")
def revenue_placements(
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default month)"),
    date_from: str | None = Query(None, description="Custom range start YYYY-MM-DD (overrides the zoom)"),
    date_to: str | None = Query(None, description="Custom range end YYYY-MM-DD"),
    customer_id: int | None = Query(None, ge=1),
    project_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required()),   # Admin/CEO only, like the page
):
    """Customer placements split internal (redeployed) vs external (new hire).

    Takes the page's zoom and filters, or its own custom date range — see
    `services.placements_report` for the rules every row is labelled by."""
    try:
        return envelope(placements_report(db, month_raw=month, period_raw=period,
                                          date_from=date_from, date_to=date_to,
                                          customer_id=customer_id, project_id=project_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class RevenueTargetsIn(BaseModel):
    """Targets are rupees excl. GST. `null` clears a value; omitted = unchanged.
    `month` scopes `month_target` to one month (YYYY-MM), `quarter_target` to
    the FY quarter holding it (spread evenly over its three months as
    overrides — targets are STORED per month) and `fy_target` to the financial
    year holding it; without `month` the DEFAULT monthly / FY targets are set."""
    month: str | None = None
    month_target: float | None = Field(default=None, ge=0)
    quarter_target: float | None = Field(default=None, ge=0)
    fy_target: float | None = Field(default=None, ge=0)
    clear_month_target: bool = False
    clear_quarter_target: bool = False
    clear_fy_target: bool = False


def _put_setting(db: Session, key: str, value: float | None, description: str) -> None:
    row = db.get(AppSetting, key)
    if value is None:
        if row is not None:
            db.delete(row)
        return
    text = f"{value:.2f}"
    if row is None:
        db.add(AppSetting(key=key, value=text, description=description))
    else:
        row.value = text


@router.put("/revenue/targets")
def revenue_targets(
    body: RevenueTargetsIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required()),   # Admin/CEO only — same gate as the report
):
    """Set the monthly / financial-year revenue targets the report measures against."""
    from datetime import date as _date

    from services.org_settings import invalidate

    month_key: str | None = None
    if body.month:
        try:
            month_key = Month.parse(body.month, _date.today()).key
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if body.month_target is not None or body.clear_month_target:
        key = (TARGET_MONTH_PREFIX + month_key) if month_key else TARGET_MONTH_KEY
        _put_setting(db, key, None if body.clear_month_target else body.month_target,
                     f"Revenue target (excl. GST) for {month_key or 'every month'}")
    if body.quarter_target is not None or body.clear_quarter_target:
        if not month_key:
            raise HTTPException(status_code=400, detail="A quarter target needs the month it belongs to")
        quarter = Period("quarter", Month.parse(month_key, _date.today()))
        share = None if body.clear_quarter_target else round(body.quarter_target / 3, 2)
        for i, key in enumerate(quarter.month_keys):
            value = share
            if share is not None and i == 2:          # the last month absorbs the rounding paise
                value = round(body.quarter_target - 2 * share, 2)
            _put_setting(db, TARGET_MONTH_PREFIX + key, value,
                         f"Revenue target (excl. GST) for {key} — a third of {quarter.short_label}")
    if body.fy_target is not None or body.clear_fy_target:
        value = None if body.clear_fy_target else body.fy_target
        if month_key:
            fy_first, _last, fy_label = fy_for(Month.parse(month_key, _date.today()))
            _put_setting(db, TARGET_FY_PREFIX + str(fy_first.year), value,
                         f"{fy_label} revenue target (excl. GST)")
        else:
            _put_setting(db, TARGET_FY_KEY, value, "Financial-year revenue target (excl. GST)")
    db.commit()
    invalidate()
    return envelope(revenue_report(db, month_key)["targets"], message="Revenue targets saved")


@router.get("/revenue/export.xlsx")
def revenue_export(
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default month)"),
    customer_id: int | None = Query(None, ge=1),
    project_id: int | None = Query(None, ge=1),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required()),   # Admin/CEO only
):
    """Period-close workbook: every section of the report, one sheet each.

    It takes the same four parameters as the page, so the download is always the
    workbook of what is on screen — a filtered view never exports unfiltered."""
    try:
        report = revenue_report(db, month, period_raw=period,
                                customer_id=customer_id, project_id=project_id)
        placements = placements_report(db, month_raw=month, period_raw=period,
                                       customer_id=customer_id, project_id=project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=build_revenue_workbook(report, placements),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="karnex-revenue-{report["period_key"]}.xlsx"'},
    )
