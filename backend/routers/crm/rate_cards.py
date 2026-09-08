"""Customer Rate Card API (/api/rate-cards).

Per-customer experience-band pricing (1–2 yrs … 14–15 yrs) with five OPTIONAL
rate columns. Access is deliberately narrow — Sales, Sales_Head (+ Admin/CEO
always) — because these numbers ARE the commercial position with a customer.
The gates run through the Access-Template machinery like every other tab, so a
template can widen or narrow this per user.

The Opportunity form's Candidate CTC Slab reads the card via the list endpoint
and auto-fills the rate matching the row's experience band and the
opportunity's billing type. Blank columns mean "customer didn't quote this
unit" — the slab then leaves the rate for manual entry.
"""
from __future__ import annotations

from datetime import date

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_create, gated_read, gated_write, get_crm_db
from models import Customer, CustomerBranch, CustomerRateCard
from schemas.common import envelope

router = APIRouter(prefix="/api/rate-cards", tags=["CRM: Rate Cards"])

RC_VIEW = gated_read("rate-cards", "Sales", "Sales_Head")
RC_EDIT = gated_write("rate-cards", "Sales", "Sales_Head")
RC_CREATE = gated_create("rate-cards", "Sales", "Sales_Head")


class RateCardIn(BaseModel):
    customer_id: int
    branch_id: int | None = None  # NULL = customer-wide default (pre-0077 rows)
    #: Slab version this band belongs to (0079). NULL = effective since forever.
    effective_from: date | None = None
    exp_min: Decimal = Field(ge=0, le=60)
    exp_max: Decimal = Field(gt=0, le=60)
    rate_hourly: Decimal | None = Field(default=None, ge=0)
    rate_daily: Decimal | None = Field(default=None, ge=0)
    rate_weekly: Decimal | None = Field(default=None, ge=0)
    rate_monthly: Decimal | None = Field(default=None, ge=0)
    rate_yearly: Decimal | None = Field(default=None, ge=0)


class RateCardUpdate(BaseModel):
    exp_min: Decimal | None = Field(default=None, ge=0, le=60)
    exp_max: Decimal | None = Field(default=None, gt=0, le=60)
    rate_hourly: Decimal | None = Field(default=None, ge=0)
    rate_daily: Decimal | None = Field(default=None, ge=0)
    rate_weekly: Decimal | None = Field(default=None, ge=0)
    rate_monthly: Decimal | None = Field(default=None, ge=0)
    rate_yearly: Decimal | None = Field(default=None, ge=0)


def _num(v) -> float | None:
    return float(v) if v is not None else None


def rate_card_out(row: CustomerRateCard) -> dict:
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "branch_id": row.branch_id,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "exp_min": float(row.exp_min),
        "exp_max": float(row.exp_max),
        "rate_hourly": _num(row.rate_hourly),
        "rate_daily": _num(row.rate_daily),
        "rate_weekly": _num(row.rate_weekly),
        "rate_monthly": _num(row.rate_monthly),
        "rate_yearly": _num(row.rate_yearly),
    }


def _get_or_404(db: Session, row_id: int) -> CustomerRateCard:
    row = db.get(CustomerRateCard, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Rate card row not found")
    return row


def _validate_band(exp_min: Decimal, exp_max: Decimal) -> None:
    if exp_max <= exp_min:
        raise HTTPException(status_code=400,
                            detail="Exp Max must be greater than Exp Min")


def _reject_overlap(db: Session, customer_id: int, branch_id: int | None,
                    exp_min: Decimal, exp_max: Decimal,
                    effective_from: date | None = None,
                    exclude_id: int | None = None) -> None:
    """Bands must not overlap WITHIN one scope — a branch AND a slab version
    (effective_from). A NEW ladder with the same bands as the old one is the
    normal case (rate revision), so versions never conflict with each other;
    only bands inside one version fight. Touching boundaries are fine."""
    rows = db.execute(
        select(CustomerRateCard).where(
            CustomerRateCard.customer_id == customer_id,
            CustomerRateCard.branch_id.is_(None) if branch_id is None
            else CustomerRateCard.branch_id == branch_id,
            CustomerRateCard.effective_from.is_(None) if effective_from is None
            else CustomerRateCard.effective_from == effective_from,
        )
    ).scalars().all()
    for r in rows:
        if exclude_id is not None and r.id == exclude_id:
            continue
        if Decimal(r.exp_min) < exp_max and exp_min < Decimal(r.exp_max):
            raise HTTPException(
                status_code=400,
                detail=f"Band {exp_min:g}–{exp_max:g} overlaps the existing "
                       f"{Decimal(r.exp_min):g}–{Decimal(r.exp_max):g} band",
            )


@router.get("")
def list_rate_card(
    customer_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RC_VIEW),
):
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    rows = db.execute(
        select(CustomerRateCard)
        .where(CustomerRateCard.customer_id == customer_id)
        .order_by(CustomerRateCard.effective_from.desc().nullslast(),
                  CustomerRateCard.exp_min)
    ).scalars().all()
    return envelope(data=[rate_card_out(r) for r in rows])


@router.post("")
def create_rate_card_row(
    payload: RateCardIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RC_CREATE),
):
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    if payload.branch_id is not None:
        branch = db.get(CustomerBranch, payload.branch_id)
        if branch is None or branch.customer_id != payload.customer_id:
            raise HTTPException(status_code=400,
                                detail="Branch does not belong to this customer")
    _validate_band(payload.exp_min, payload.exp_max)
    _reject_overlap(db, payload.customer_id, payload.branch_id,
                    payload.exp_min, payload.exp_max,
                    effective_from=payload.effective_from)
    row = CustomerRateCard(**payload.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_card_out(row), message="Rate card row added")


class CopyFromBranchIn(BaseModel):
    customer_id: int
    #: Branch to copy FROM (usually the primary). NULL = customer-wide rows.
    source_branch_id: int | None = None
    #: Branch to copy INTO.
    target_branch_id: int
    #: Version date for the copied ladder on the target branch.
    effective_from: date | None = None


@router.post("/copy-from-branch")
def copy_slab_from_branch(
    payload: CopyFromBranchIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RC_CREATE),
):
    """Copy a branch's CURRENT slab version into another branch, as a new
    version there — 'same as primary branch' without retyping six bands.

    A COPY, not a link, on purpose: the target branch can diverge later
    without silently repricing the source, and each branch's history stays
    its own. Copies the source's current version (latest effective_from
    on-or-before today; NULL counts as oldest)."""
    if db.get(Customer, payload.customer_id) is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    target = db.get(CustomerBranch, payload.target_branch_id)
    if target is None or target.customer_id != payload.customer_id:
        raise HTTPException(status_code=400,
                            detail="Target branch does not belong to this customer")
    if payload.source_branch_id is not None:
        source = db.get(CustomerBranch, payload.source_branch_id)
        if source is None or source.customer_id != payload.customer_id:
            raise HTTPException(status_code=400,
                                detail="Source branch does not belong to this customer")
    if payload.source_branch_id == payload.target_branch_id:
        raise HTTPException(status_code=400,
                            detail="Source and target branch are the same")

    src_rows = db.execute(
        select(CustomerRateCard).where(
            CustomerRateCard.customer_id == payload.customer_id,
            CustomerRateCard.branch_id.is_(None) if payload.source_branch_id is None
            else CustomerRateCard.branch_id == payload.source_branch_id,
        )
    ).scalars().all()
    if not src_rows:
        raise HTTPException(status_code=400,
                            detail="The source branch has no CTC slab to copy")
    # Current version of the source: latest effective_from <= today (None = oldest).
    today = date.today()
    eligible = {r.effective_from for r in src_rows
                if r.effective_from is None or r.effective_from <= today}
    if not eligible:
        raise HTTPException(status_code=400,
                            detail="The source branch has no slab in effect yet "
                                   "(only future-dated ladders)")
    dated = sorted(d for d in eligible if d is not None)
    current_key = dated[-1] if dated else None
    current = [r for r in src_rows if r.effective_from == current_key]

    # Target must not already hold a version at the copy's effective date.
    for r in current:
        _reject_overlap(db, payload.customer_id, payload.target_branch_id,
                        Decimal(r.exp_min), Decimal(r.exp_max),
                        effective_from=payload.effective_from)

    created = []
    for r in current:
        row = CustomerRateCard(
            customer_id=payload.customer_id,
            branch_id=payload.target_branch_id,
            effective_from=payload.effective_from,
            exp_min=r.exp_min, exp_max=r.exp_max,
            rate_hourly=r.rate_hourly, rate_daily=r.rate_daily,
            rate_weekly=r.rate_weekly, rate_monthly=r.rate_monthly,
            rate_yearly=r.rate_yearly,
        )
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    return envelope(data=[rate_card_out(r) for r in created],
                    message=f"Copied {len(created)} band(s)")


@router.put("/{row_id}")
def update_rate_card_row(
    row_id: int,
    payload: RateCardUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RC_EDIT),
):
    row = _get_or_404(db, row_id)
    changes = payload.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), 'rate-cards', changes, {"exp_min": "band", "exp_max": "band", "rate_hourly": "rate_hourly",
        "rate_daily": "rate_daily", "rate_weekly": "rate_weekly",
        "rate_monthly": "rate_monthly", "rate_yearly": "rate_yearly"})

    exp_min = Decimal(str(changes.get("exp_min", row.exp_min)))
    exp_max = Decimal(str(changes.get("exp_max", row.exp_max)))
    _validate_band(exp_min, exp_max)
    _reject_overlap(db, row.customer_id, row.branch_id, exp_min, exp_max,
                    effective_from=row.effective_from, exclude_id=row.id)
    for key, value in changes.items():
        setattr(row, key, value)
    db.commit()
    db.refresh(row)
    return envelope(data=rate_card_out(row), message="Rate card row updated")


@router.delete("/{row_id}")
def delete_rate_card_row(
    row_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(RC_EDIT),
):
    row = _get_or_404(db, row_id)
    db.delete(row)
    db.commit()
    return envelope(data={"id": row_id}, message="Rate card row deleted")
