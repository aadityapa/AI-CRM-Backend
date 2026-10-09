"""Role-specific CRM dashboards (read-only aggregates)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, gated_read, get_crm_db, role_required
from schemas.common import envelope
from services import dashboards as svc

router = APIRouter(prefix="/api/dashboard", tags=["CRM: Dashboards"])


@router.get("/executive")
def executive_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales_Head")),
):
    return envelope(svc.executive_dashboard(db))


@router.get("/ta-tracking")
def ta_tracking_dashboard(
    db: Session = Depends(get_crm_db),
    # Reads recruiter performance data → governed by the "reports" tab. No
    # default roles: untemplated users keep any-CRM-role access (ladder rule 4).
    user: CurrentUser = Depends(gated_read("reports")),
):
    """Per-TA scorecard. Admin/CEO see every TA; anyone else sees only their own."""
    only = None if user.is_admin else user.id
    return envelope(svc.ta_tracking(db, only_user_id=only))


@router.get("/rmg")
def rmg_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("RMG")),
):
    return envelope(svc.rmg_dashboard(db))


@router.get("/ta")
def ta_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("dashboard", "TA")),
):
    return envelope(svc.ta_dashboard(db))


@router.get("/finance")
def finance_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Finance")),
):
    return envelope(svc.finance_dashboard(db))


@router.get("/interviews")
def upcoming_interviews(
    days: int = 30,
    db: Session = Depends(get_crm_db),
    # Sales and Sales_Head removed (Aug 2026): the upcoming-interviews view moved
    # to the Interview Calendar tab, which they do not have. Leaving them on this
    # endpoint would keep the data reachable by URL after removing the widget.
    user: CurrentUser = Depends(role_required("RMG", "TA")),
):
    """Upcoming human interview rounds (L2 face-to-face / customer interviews).

    Superseded for display purposes by GET /api/calendar/interviews, which also
    includes AI L1 sessions. Kept because it is a simpler flat list and is still
    the cheapest way to ask "what is coming up".
    """
    return envelope(svc.upcoming_interview_events(db, days=max(1, min(days, 120))))


@router.get("/bench")
def bench_dashboard(
    days: int = 60,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("dashboard", "RMG", "Sales_Head")),
):
    """Project employees rolling off within `days` — the redeployment radar."""
    return envelope(svc.bench_rolloffs(db, days=max(1, min(days, 180))))


@router.get("/bench/{employee_id}/matches")
def bench_matches(
    employee_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("dashboard", "RMG", "Sales_Head")),
):
    """Ranked open requirements for a rolling-off employee (ATS-scored on their CV)."""
    return envelope(svc.bench_requirement_matches(db, employee_id))


@router.get("/requirements")
def requirements_dashboard(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales", "Sales_Head", "RMG")),
):
    return envelope(svc.requirements_dashboard(db))


@router.get("/my-work")
def my_work(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    """The user's to-do list, shaped by their roles AND their access template.

    Nobody needs training to read a to-do list — this is how the tool teaches
    each role its job. Every item is something the CALLER can act on today,
    with a count and the page it lives on. Items for tabs the user cannot see
    (template) are dropped, so the list never points somewhere it can't go.
    """
    return envelope(svc.my_work(db, user))


@router.get("/desk")
def work_desk(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
    full: bool = Query(False, description="Every item per tab (My Tasks filters), not the first 50"),
):
    """The login's daily tasks as tabs — feedback due · to schedule · awaiting your
    call · to screen · choose route · upcoming · my queues — only the tabs this
    login works (`services/work_desk.desk`)."""
    from services.work_desk import FULL_MAX_ITEMS, MAX_ITEMS, desk
    return envelope(desk(db, user, max_items=FULL_MAX_ITEMS if full else MAX_ITEMS))


class DeskMarksIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=500)
    done: bool = True


@router.post("/desk/marks")
def work_desk_marks(body: DeskMarksIn, db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    """Tick (or untick) My Tasks items as done — e.g. every invoice of one
    employee (8 Oct 2026, user ask: "work done, tick & close this employee").
    Personal to this login; the records behind the items never change."""
    from services.work_desk import set_marks
    changed = set_marks(db, user.id, body.keys, body.done)
    db.commit()
    return envelope({"changed": changed, "done": body.done},
                    message=(f"{changed} item(s) ticked as done" if body.done else f"{changed} item(s) back on your list"))


# --------------------------------------------------- role desk (14 Sep 2026)


@router.get("/today")
def today_tiles(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    """The four-ish numbers the caller's roles are judged on — the Today strip.
    Role- and template-aware like /my-work; multi-role users get a merged,
    de-duplicated set capped at eight."""
    from services import dashboard_desk
    return envelope(dashboard_desk.today_tiles(db, user))


@router.get("/upcoming")
def upcoming(
    days: int = 7,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    """Dated items in the next `days` days: rounds, AI L1 slots, joinings,
    roll-offs, PO expiries, invoice due dates — filtered by what the caller
    can open."""
    from services import dashboard_desk
    return envelope(dashboard_desk.upcoming(db, user, days=max(1, min(days, 60))))


@router.get("/team")
def team_overview(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("Sales_Head")),
):
    """The head's layer: where the company is stuck (with the owning team) and
    per-person rows for TA and Sales. Sales Head, Admin, CEO."""
    from services import dashboard_desk
    return envelope(dashboard_desk.team_overview(db, user))


# --------------------------------------------- CEO dashboard tabs (28 Sep 2026)


@router.get("/ceo")
def ceo_dashboard(
    tab: str = Query("finance", description="finance | customer | sales | people"),
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default fy)"),
    db: Session = Depends(get_crm_db),
    # Admin / CEO ONLY and not template-widenable: the Customer tab prints every
    # customer's revenue and every employee's cost side by side.
    user: CurrentUser = Depends(role_required()),
):
    """One tab of the CEO dashboard — Finance · Customer (→ project → employee) ·
    Sales (positions, internal vs external onboardings, pace) · People (headcount,
    bench, joiners / exits, roll-offs). `month` is the ANCHOR at every zoom; the
    default zoom is the financial year."""
    from services.executive_dashboard import executive_dashboard

    try:
        return envelope(executive_dashboard(db, tab, month, period))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/people")
def people_dashboard(
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default fy)"),
    db: Session = Depends(get_crm_db),
    # HR's desk (28 Sep 2026): the CEO dashboard's People tab, for whoever may
    # open the Employees tab — HR by role, or a template / custom role that
    # grants it. Nothing here HR cannot already read on the employee record.
    user: CurrentUser = Depends(gated_read("employees", "HR")),
):
    """The People tab of the CEO dashboard on its own — headcount, deployed vs
    bench, joiners / exits, attrition, tenure, roll-offs — for the HR desk."""
    from services.executive_dashboard import executive_dashboard

    try:
        return envelope(executive_dashboard(db, "people", month, period))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --------------------------------------------- hiring control tower (23 Sep 2026)


@router.get("/hiring")
def hiring_control_tower(
    month: str | None = Query(None, description="YYYY-MM anchor; blank = current month"),
    period: str | None = Query(None, description="month | quarter | fy (default quarter)"),
    db: Session = Depends(get_crm_db),
    # The Sales → TA numbers every desk reads. Governed by the "dashboard" tab
    # (mandatory on every template), so any CRM role — built-in or custom —
    # sees the same page; the numbers name no candidate and no rupee.
    user: CurrentUser = Depends(gated_read("dashboard")),
):
    """Pipeline positions · opportunities · onboardings · positions closed ·
    active / workable positions · active customers · pace vs the quarter's
    targets · hiring-stage delays. `month` is the ANCHOR at every zoom, exactly
    like the Revenue report; the default zoom is the quarter because targets
    are set per quarter."""
    from services.hiring_dashboard import hiring_dashboard

    try:
        return envelope(hiring_dashboard(db, month, period_raw=period or "quarter"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class HiringTargetsIn(BaseModel):
    """Company-level targets per FY quarter (positions / onboardings — whole
    numbers, but stored as numbers so a half-position target is not refused).
    `quarter` is the key the report prints (`2026-Q2`). A value sets, `clear_*`
    deletes, omitted leaves the row alone."""
    quarter: str | None = None
    sales_quarter: float | None = Field(default=None, ge=0)
    ta_quarter: float | None = Field(default=None, ge=0)
    sales_default: float | None = Field(default=None, ge=0)
    ta_default: float | None = Field(default=None, ge=0)
    clear: list[str] = Field(default_factory=list)


_CLEARABLE = {"sales_quarter", "ta_quarter", "sales_default", "ta_default"}


@router.put("/hiring/targets")
def hiring_targets(
    body: HiringTargetsIn,
    db: Session = Depends(get_crm_db),
    # Sales Head owns the Sales target and answers for TA fulfilment to the
    # CEO; Admin/CEO pass every role gate. Not template-widenable on purpose —
    # a target is a management number.
    user: CurrentUser = Depends(role_required("Sales_Head")),
):
    from services.hiring_dashboard import hiring_dashboard, parse_quarter_key, set_targets
    from services.org_settings import invalidate

    quarter = (body.quarter or "").strip() or None
    quarter_period = None
    if quarter:
        try:
            quarter_period = parse_quarter_key(quarter)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    unknown = set(body.clear) - _CLEARABLE
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"clear accepts only {', '.join(sorted(_CLEARABLE))}")
    if any(k.endswith("_quarter") for k in body.clear) and not quarter:
        raise HTTPException(status_code=400, detail="quarter is required to clear a quarter target")
    if (body.sales_quarter is not None or body.ta_quarter is not None) and not quarter:
        raise HTTPException(status_code=400, detail="quarter is required to set a quarter target")
    set_targets(db, quarter_key=quarter, sales_quarter=body.sales_quarter,
                ta_quarter=body.ta_quarter, sales_default=body.sales_default,
                ta_default=body.ta_default, clear=set(body.clear))
    db.commit()
    invalidate()
    # Echo the targets as the page will read them, anchored on the quarter just edited.
    anchor = quarter_period.anchor.key if quarter_period else None
    return envelope(hiring_dashboard(db, anchor, period_raw="quarter")["targets"],
                    message="Hiring targets saved")
