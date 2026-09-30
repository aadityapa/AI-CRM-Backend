"""AI interview spend — Admin / CEO only (28 Sep 2026).

`GET /api/ai-costs/interviews` is the whole page (KPIs · trend · breakdowns ·
one page of interviews); `GET /api/ai-costs/interviews/export.csv` is the same
slice as a spreadsheet. Both are `role_required()` = Admin / CEO — money is a
management number and no Access Template can widen it (the same rule as the
Revenue report). The figures come from `services/ai_interview_costs`, which
reads the prompt-log store (the legacy auth DB, where every OpenAI call is
priced and attributed at log time) plus the CRM for customer / TA context.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ai import _db_target as prompt_log_target
from crm_deps import CurrentUser, get_crm_db, role_required
from schemas.common import envelope
from services.ai_interview_costs import (
    GRANULARITIES, interview_cost_report, interview_costs_csv, MAX_EXPORT_ROWS,
)

router = APIRouter(prefix="/api/ai-costs", tags=["ai-costs"])

_ADMIN_ONLY = role_required()   # Admin / CEO — deliberately not template-widenable


def _validated(granularity: str, sort: str) -> None:
    if granularity not in GRANULARITIES:
        raise HTTPException(status_code=400, detail=f"granularity must be one of {', '.join(GRANULARITIES)}")
    if sort not in ("cost", "date", "date_desc", "candidate", "duration", "tokens"):
        raise HTTPException(status_code=400, detail="Unknown sort")


@router.get("/interviews")
def list_interview_costs(
    date_from: str | None = None,
    date_to: str | None = None,
    granularity: str = "day",
    search: str = "",
    customer_id: int | None = None,
    ta: str = "",
    status: str = "",
    template: str = "",
    sort: str = "cost",
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(_ADMIN_ONLY),
):
    _validated(granularity, sort)
    report = interview_cost_report(
        prompt_log_target(), db, date_from=date_from, date_to=date_to, granularity=granularity,
        search=search, customer_id=customer_id, ta=ta, status=status, template=template,
        sort=sort, page=page, limit=min(max(1, limit), 200),
    )
    interviews = report.pop("interviews")
    meta = report.pop("meta")
    return envelope(data={**report, "interviews": interviews}, meta=meta)


@router.get("/interviews/export.csv")
def export_interview_costs(
    date_from: str | None = None,
    date_to: str | None = None,
    search: str = "",
    customer_id: int | None = None,
    ta: str = "",
    status: str = "",
    template: str = "",
    sort: str = "cost",
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(_ADMIN_ONLY),
):
    _validated("day", sort)
    report = interview_cost_report(
        prompt_log_target(), db, date_from=date_from, date_to=date_to, search=search,
        customer_id=customer_id, ta=ta, status=status, template=template, sort=sort,
        page=1, limit=MAX_EXPORT_ROWS,
    )
    name = f"ai-interview-costs_{report['period']['date_from']}_{report['period']['date_to']}.csv"
    return Response(
        content=interview_costs_csv(report),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
