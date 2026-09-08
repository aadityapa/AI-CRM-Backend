"""Payroll extract endpoints: monthly attendance facts for salary processing."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, get_crm_db
from schemas.common import envelope
from services.payroll import payroll_by_employee, payroll_csv, payroll_rows

router = APIRouter(prefix="/api/payroll", tags=["CRM: Payroll"])

# Payroll is HR + Finance work; Admin/CEO pass as always. Read-only module —
# nothing here writes, so there is no write gate to configure.
payroll_read = gated_read("timesheets", "HR", "Finance")


def _check_period(month: int, year: int) -> None:
    if not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if not 2000 <= year <= 2100:
        raise HTTPException(status_code=400, detail="year is out of range")


@router.get("/summary")
def payroll_summary(
    month: int = Query(..., ge=1, le=12),
    year: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(payroll_read),
):
    """Monthly payroll extract: per-employee totals plus the per-project detail.

    `pending` on a row lists timesheets that are not Approved yet — payroll
    should chase those before paying, so they are surfaced rather than hidden.
    """
    _check_period(month, year)
    rows = payroll_rows(db, month, year)
    people = payroll_by_employee(rows)
    not_approved = [p for p in people if not p["all_approved"]]
    return envelope(
        data={
            "month": month,
            "year": year,
            "employees": people,
            "detail": rows,
            "totals": {
                "employees": len(people),
                "timesheets": len(rows),
                "not_approved": len(not_approved),
                "loss_of_pay_days": round(sum(p["loss_of_pay_days"] for p in people), 2),
                "days_worked": round(sum(p["days_worked"] for p in people), 2),
            },
        },
        message=f"Payroll extract for {month:02d}/{year}",
    )


@router.get("/export.csv")
def payroll_export_csv(
    month: int = Query(..., ge=1, le=12),
    year: int = Query(..., ge=2000, le=2100),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(payroll_read),
):
    """The same extract as a CSV download for the payroll/accounting team."""
    _check_period(month, year)
    people = payroll_by_employee(payroll_rows(db, month, year))
    csv_text = payroll_csv(people)
    filename = f"karnex-payroll-{year}-{month:02d}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
