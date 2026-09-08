"""Opportunity service helpers: pipeline stage machine, FK validation, skills, serialization."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    ContactPerson,
    Customer,
    CustomerBranch,
    Location,
    Opportunity,
    OpportunityCtcSlab,
    OpportunitySkill,
    PipelineStage,
    Requirement,
    Skill,
    WorkMode,
)

# ---------------------------------------------------------------------------
# Server-side pipeline stage machine (Archived is terminal)
# ---------------------------------------------------------------------------

# Sales drives the outcome from the opportunity page (8 Sep 2026, user
# request): Close Won / Close Lost / Close Partial / Customer Hold (On_Hold) /
# Sales Hold, plus Reactivate. A hold or a close is reversible — a wrong click
# or a deal that comes back must not need a DBA — only Archived is terminal.
_OUTCOMES = ["Closed_Won", "Closed_Lost", "Closed_Partial"]
_HOLDS = ["On_Hold", "Sales_Hold"]
STAGE_TRANSITIONS: dict[str, list[str]] = {
    "New": ["Active", *_HOLDS, *_OUTCOMES, "Rejected"],
    "Active": [*_HOLDS, *_OUTCOMES, "Rejected"],
    "On_Hold": ["Active", "Sales_Hold", *_OUTCOMES, "Rejected"],
    "Sales_Hold": ["Active", "On_Hold", *_OUTCOMES, "Rejected"],
    "Closed_Won": ["Active", "Closed_Lost", "Closed_Partial", "Archived"],
    "Closed_Lost": ["Active", "Closed_Won", "Closed_Partial", "Archived"],
    "Closed_Partial": ["Active", "Closed_Won", "Closed_Lost", "Archived"],
    "Rejected": ["Active", "Archived"],
    "Archived": [],
}


def _ev(value):
    return value.value if hasattr(value, "value") else value


def validate_stage_transition(current_stage, new_stage: str) -> None:
    """400 with the allowed next stages when the requested move is illegal."""
    current = _ev(current_stage)
    valid_stages = {s.value for s in PipelineStage}
    if new_stage not in valid_stages:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown stage '{new_stage}'. Valid stages: {', '.join(s.value for s in PipelineStage)}",
        )
    allowed = STAGE_TRANSITIONS.get(current, [])
    if new_stage not in allowed:
        allowed_txt = ", ".join(allowed) if allowed else "none (terminal stage)"
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transition from '{current}' to '{new_stage}'. Allowed next stages: {allowed_txt}",
        )


# ---------------------------------------------------------------------------
# Lookups & FK validation
# ---------------------------------------------------------------------------

def get_opportunity_or_404(db: Session, opportunity_id: int) -> Opportunity:
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opp


def validate_refs(db: Session, customer_id: int, branch_id: int | None,
                  contact_person_id: int | None, hiring_manager_id: int | None) -> None:
    """Validate that customer exists and branch/contacts belong to that customer."""
    if db.get(Customer, customer_id) is None:
        raise HTTPException(status_code=400, detail="Invalid customer_id")
    if branch_id is not None:
        branch = db.get(CustomerBranch, branch_id)
        if branch is None or branch.customer_id != customer_id:
            raise HTTPException(status_code=400, detail="branch_id does not belong to this customer")
    for label, contact_id in (("contact_person_id", contact_person_id),
                              ("hiring_manager_id", hiring_manager_id)):
        if contact_id is not None:
            contact = db.get(ContactPerson, contact_id)
            if contact is None or contact.customer_id != customer_id:
                raise HTTPException(status_code=400, detail=f"{label} does not belong to this customer")


# ---------------------------------------------------------------------------
# Skills (replace the whole set)
# ---------------------------------------------------------------------------

def replace_skills(db: Session, opp: Opportunity, items: list) -> None:
    """Replace the opportunity's skill set. items: OpportunitySkillIn list. Caller commits."""
    dedup: dict[int, object] = {}
    for item in items:
        dedup[item.skill_id] = item
    if dedup:
        found = set(db.execute(select(Skill.id).where(Skill.id.in_(dedup.keys()))).scalars().all())
        missing = sorted(set(dedup.keys()) - found)
        if missing:
            raise HTTPException(status_code=400,
                                detail=f"Unknown skill id(s): {', '.join(str(m) for m in missing)}")
    for existing in list(opp.skills):
        db.delete(existing)
    db.flush()
    for skill_id, item in dedup.items():
        db.add(OpportunitySkill(
            opportunity_id=opp.id,
            skill_id=skill_id,
            is_mandatory=bool(getattr(item, "is_mandatory", False)),
            required_level=getattr(item, "required_level", None),
            comment=getattr(item, "comment", None),
        ))


def serialize_skills(db: Session, opp: Opportunity) -> list[dict]:
    skill_ids = {s.skill_id for s in opp.skills}
    names: dict[int, str] = {}
    if skill_ids:
        rows = db.execute(select(Skill.id, Skill.name).where(Skill.id.in_(skill_ids))).all()
        names = {row[0]: row[1] for row in rows}
    return [
        {
            "id": s.id,
            "skill_id": s.skill_id,
            "skill_name": names.get(s.skill_id),
            "is_mandatory": s.is_mandatory,
            "required_level": getattr(s, "required_level", None),
            "comment": getattr(s, "comment", None),
        }
        for s in opp.skills
    ]


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def serialize_opportunity(db: Session, opp: Opportunity, detail: bool = False) -> dict:
    customer = db.get(Customer, opp.customer_id)
    data = {
        "id": opp.id,
        "opp_id": opp.opp_id,
        "title": opp.title,
        "customer_id": opp.customer_id,
        "customer_name": customer.name if customer else None,
        "branch_id": opp.branch_id,
        "contact_person_id": opp.contact_person_id,
        "hiring_manager_id": opp.hiring_manager_id,
        "opp_type": _ev(opp.opp_type),
        "rfi_value": float(opp.rfi_value) if opp.rfi_value is not None else None,
        "rfi_received_date": opp.rfi_received_date.isoformat() if opp.rfi_received_date else None,
        "pipeline_stage": _ev(opp.pipeline_stage),
        "approval_status": _ev(getattr(opp, "approval_status", None)),
        "sales_head_approved_by": getattr(opp, "sales_head_approved_by", None),
        "sales_head_approved_at": (
            opp.sales_head_approved_at.isoformat()
            if getattr(opp, "sales_head_approved_at", None) else None
        ),
        "approval_rejection_reason": getattr(opp, "approval_rejection_reason", None),
        "onboarding_status": opp.onboarding_status,
        "onboarded_count": getattr(opp, "onboarded_count", 0),
        "details": getattr(opp, "details", None) or {},
        "version": getattr(opp, "version", 1),
        "created_by": opp.created_by,
        "created_at": opp.created_at.isoformat() if opp.created_at else None,
        "updated_at": opp.updated_at.isoformat() if opp.updated_at else None,
    }
    if detail:
        branch = db.get(CustomerBranch, opp.branch_id) if opp.branch_id else None
        # Never show another customer's branch name on this opportunity.
        if branch is not None and branch.customer_id != opp.customer_id:
            branch = None
        contact = db.get(ContactPerson, opp.contact_person_id) if opp.contact_person_id else None
        hiring_mgr = db.get(ContactPerson, opp.hiring_manager_id) if opp.hiring_manager_id else None
        data["branch_name"] = branch.branch_name if branch else None
        data["branch_unlinked"] = bool(opp.branch_id and branch is None)
        data["contact_person_name"] = contact.name if contact else None
        data["hiring_manager_name"] = hiring_mgr.name if hiring_mgr else None
        data["skills"] = serialize_skills(db, opp)
        data["ctc_slab"] = _serialize_ctc_slab(opp)
        data["allowed_next_stages"] = STAGE_TRANSITIONS.get(_ev(opp.pipeline_stage), [])
    return data


def _serialize_ctc_slab(opp: Opportunity) -> list[dict]:
    def _f(v):
        return float(v) if v is not None else None
    rows = sorted(getattr(opp, "ctc_slab", []) or [], key=lambda r: getattr(r, "position", 0))
    return [
        {
            "exp_min": _f(r.exp_min), "exp_max": _f(r.exp_max), "target_exp": _f(r.target_exp),
            "rate": _f(r.rate), "revenue_monthly": _f(r.revenue_monthly), "revenue_annual": _f(r.revenue_annual),
            "management_cost_pct": _f(r.management_cost_pct), "engineering_budget": _f(r.engineering_budget),
            "hike_pct": _f(r.hike_pct), "appraisal_cycle": r.appraisal_cycle, "approved_ctc_lac": _f(r.approved_ctc_lac),
        }
        for r in rows
    ]


def fetch_activity_log(db: Session, opportunity_id: int) -> list[dict]:
    """Chronological activity log with usernames joined from registration_data."""
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, r.full_name, l.action_type, l.comment, l.timestamp "
            "FROM opportunity_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.opportunity_id = :oid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"oid": opportunity_id},
    ).mappings().all()
    return [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "full_name": row["full_name"],
            "action_type": row["action_type"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Opportunity → Requirement field carry-over
# ---------------------------------------------------------------------------

# Requirement fields backfilled from the opportunity (null → filled).
_REQ_CARRY_KEYS = (
    "experience_min", "experience_max", "budget_ctc_min", "budget_ctc_max",
    "work_mode", "location_id", "target_closure_date", "description",
)


def requirement_fields_from_opportunity(db: Session, opp: Opportunity) -> dict:
    """Requirement field values carried from an opportunity's T&M details +
    Candidate CTC Slab, so RMG (Engineering Review) and TA see Experience /
    Budget / Work mode / Location / Target closure without re-keying.
    Non-T&M opportunities simply yield Nones for the missing keys."""
    details = opp.details or {}

    def _dec(key):
        v = details.get(key)
        if v in (None, ""):
            return None
        try:
            return Decimal(str(v))
        except (InvalidOperation, ValueError):
            return None

    target_closure = None
    tv = details.get("tm_closing_date")
    if tv:
        try:
            target_closure = date.fromisoformat(str(tv)[:10])
        except ValueError:
            target_closure = None

    positions = None
    try:
        pc = int(float(details.get("tm_positions_count")))
        if pc >= 1:
            positions = pc
    except (TypeError, ValueError):
        positions = None

    work_mode = None
    wm = details.get("tm_wfo_remote")
    if wm:
        try:
            work_mode = WorkMode(str(wm))
        except ValueError:
            work_mode = None

    # Opportunity stores the Work Location as a CITY NAME. Resolve to a Location
    # row; create one (country defaults to India) when the master lacks it so the
    # requirement can always show the location the sales team selected.
    location_id = None
    city = details.get("tm_work_location")
    if city:
        city = str(city).strip()
        location_id = db.execute(
            select(Location.id).where(func.lower(Location.city) == city.lower())
        ).scalar_one_or_none()
        if location_id is None:
            loc = Location(city=city)
            db.add(loc)
            db.flush()
            location_id = loc.id

    # Budget CTC ← Candidate CTC Slab "Approved CTC" (min/max). The stored value
    # is already a rupee amount (the derived revenue chain), NOT lakhs — no scaling.
    ctc_vals = [
        Decimal(str(s.approved_ctc_lac))
        for s in db.execute(
            select(OpportunityCtcSlab).where(OpportunityCtcSlab.opportunity_id == opp.id)
        ).scalars().all()
        if s.approved_ctc_lac is not None
    ]

    return {
        "experience_min": _dec("tm_exp_min"),
        "experience_max": _dec("tm_exp_max"),
        "budget_ctc_min": (min(ctc_vals) if ctc_vals else None),
        "budget_ctc_max": (max(ctc_vals) if ctc_vals else None),
        "work_mode": work_mode,
        "location_id": location_id,
        "target_closure_date": target_closure,
        "no_of_positions": positions,
        "description": (details.get("tm_position_title") or None),
    }


# A budget above this (₹100 crore/yr) can only be the old ×100000 scaling bug.
_IMPLAUSIBLE_BUDGET = Decimal("1000000000")


def backfill_requirement_from_opportunity(db: Session, req) -> bool:
    """Keep the requirement in sync with its opportunity on read: always mirror
    the (opportunity-owned) title, fill still-empty carried fields, and correct a
    budget corrupted by the earlier ×100000 scaling bug. Idempotent; returns True
    when something changed (caller commits)."""
    if getattr(req, "opportunity_id", None) is None:
        return False
    opp = db.get(Opportunity, req.opportunity_id)
    if opp is None:
        return False
    changed = False
    # Title is opportunity-owned — always keep the requirement's copy current so
    # Sales' edits show for Sales Head / RMG / TA.
    if opp.title and req.title != opp.title:
        req.title = opp.title
        changed = True
    corrupt_budget = (
        (req.budget_ctc_min is not None and req.budget_ctc_min > _IMPLAUSIBLE_BUDGET)
        or (req.budget_ctc_max is not None and req.budget_ctc_max > _IMPLAUSIBLE_BUDGET)
    )
    if not corrupt_budget and all(getattr(req, k) is not None for k in _REQ_CARRY_KEYS):
        return changed  # carried fields already set & sane; title handled above
    vals = requirement_fields_from_opportunity(db, opp)
    for k in _REQ_CARRY_KEYS:
        new = vals.get(k)
        if new is None:
            continue
        cur = getattr(req, k)
        fix_budget = (
            k in ("budget_ctc_min", "budget_ctc_max")
            and cur is not None and cur > _IMPLAUSIBLE_BUDGET
        )
        if cur is None or fix_budget:
            setattr(req, k, new)
            changed = True
    # positions: only override the default 1 when the opportunity has a real count.
    pc = vals.get("no_of_positions")
    if pc and req.no_of_positions in (None, 1) and req.no_of_positions != pc:
        req.no_of_positions = pc
        changed = True
    return changed


def sync_requirement_from_opportunity(db: Session, opp: Opportunity) -> None:
    """Push opportunity edits (title, customer, and the carried details) onto the
    linked requirement so Sales Head / RMG / TA always see what Sales edited — the
    requirement stores denormalized copies that would otherwise go stale. The
    requirement's own fields (rmg_jd_text, skills, status, priority) are untouched.
    Call inside the opportunity-update transaction (before commit)."""
    req = db.execute(
        select(Requirement).where(Requirement.opportunity_id == opp.id)
    ).scalar_one_or_none()
    if req is None:
        return
    if opp.title:
        req.title = opp.title
    if opp.customer_id:
        req.customer_id = opp.customer_id
    vals = requirement_fields_from_opportunity(db, opp)
    for k in _REQ_CARRY_KEYS:
        new = vals.get(k)
        if new is not None:
            setattr(req, k, new)
    pc = vals.get("no_of_positions")
    if pc:
        req.no_of_positions = pc
