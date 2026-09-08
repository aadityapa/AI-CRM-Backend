"""CRM reports — any CRM role (Admin implicit); every report supports ?format=csv."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from services import reports as svc
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
