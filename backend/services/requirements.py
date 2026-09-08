"""Requirement workflow services: visibility rules, serialization, fulfilment check."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import (
    CandidateProfile, PipelineStatus, Requirement, RequirementActivityLog,
    RequirementSkill, RequirementStatus, Skill,
)
from services.crm_common import log_activity
from services.notify import notify_user

# TA only ever sees requirements that reached sourcing. On_Hold stays VISIBLE
# (user decision, 25 Aug 2026): hiding a held requirement makes TAs think it
# vanished — they see the badge and the reason, with sourcing actions locked.
TA_VISIBLE_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
    RequirementStatus.ON_HOLD,
    RequirementStatus.FULFILLED,
)
def ensure_not_on_hold(req) -> None:
    """400 when the requirement is held — new sourcing activity is paused.
    Used by uploads, slot invites and AI-L1 scheduling; candidates already
    mid-pipeline are deliberately unaffected."""
    from fastapi import HTTPException
    if req is not None and req.status == RequirementStatus.ON_HOLD:
        reason = (getattr(req, "held_reason", None) or "").strip()
        raise HTTPException(
            status_code=400,
            detail="This requirement is ON HOLD" + (f": {reason}" if reason else "")
                   + ". Sourcing actions resume when RMG resumes it.",
        )


# Sales may edit / resubmit only from these states.
EDITABLE_STATUSES = (
    RequirementStatus.DRAFT,
    RequirementStatus.SALES_HEAD_REJECTED,
    RequirementStatus.ENGINEERING_REJECTED,
)
TERMINAL_STATUSES = (
    RequirementStatus.FULFILLED,
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)
SEE_ALL_ROLES = ("Admin", "Sales_Head", "RMG")


def get_requirement_or_404(db: Session, requirement_id: int) -> Requirement:
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return req


def apply_visibility(stmt: Select, user: CurrentUser) -> Select:
    """Restrict a select(Requirement) statement to what this user may see (403 if nothing)."""
    if user.has_any(*SEE_ALL_ROLES):
        return stmt
    conditions = []
    if "Sales" in user.roles:
        conditions.append(Requirement.created_by == user.id)
    if "TA" in user.roles:
        conditions.append(Requirement.status.in_(TA_VISIBLE_STATUSES))
    if not conditions:
        raise HTTPException(status_code=403, detail="Your role cannot view requirements")
    return stmt.where(or_(*conditions))


def ensure_visible(user: CurrentUser, req: Requirement) -> None:
    """Same visibility as the list; 404 (not 403) so existence is never leaked."""
    if user.has_any(*SEE_ALL_ROLES):
        return
    if not user.has_any("Sales", "TA"):
        raise HTTPException(status_code=403, detail="Your role cannot view requirements")
    if "Sales" in user.roles and req.created_by == user.id:
        return
    if "TA" in user.roles and req.status in TA_VISIBLE_STATUSES:
        return
    raise HTTPException(status_code=404, detail="Requirement not found")


def _num(v):
    return float(v) if v is not None else None


def _iso(v):
    return v.isoformat() if v is not None else None


def _val(v):
    return v.value if hasattr(v, "value") else v


def skills_by_requirement(db: Session, requirement_ids: list[int]) -> dict[int, list[dict]]:
    """One query: requirement_id -> [{skill_id, name, is_mandatory, min_rating}]."""
    out: dict[int, list[dict]] = {}
    if not requirement_ids:
        return out
    rows = db.execute(
        select(RequirementSkill, Skill.name)
        .join(Skill, Skill.id == RequirementSkill.skill_id)
        .where(RequirementSkill.requirement_id.in_(requirement_ids))
        .order_by(RequirementSkill.id)
    ).all()
    for rs, name in rows:
        out.setdefault(rs.requirement_id, []).append({
            "skill_id": rs.skill_id,
            "name": name,
            "is_mandatory": bool(rs.is_mandatory),
            "min_rating": rs.min_rating,
        })
    return out


def serialize_attachment_row(a) -> dict:
    return {
        "id": a.id,
        "file_url": a.file_url,
        "file_name": a.file_name,
        "file_sha256": getattr(a, "file_sha256", None),
        "file_size": getattr(a, "file_size", None),
        "kind": getattr(a, "kind", None) or "general",
        "uploaded_by": a.uploaded_by,
        "uploaded_at": _iso(a.uploaded_at),
    }


def requirement_label(req: Requirement) -> str:
    """The id humans see for a requirement (18 Aug 2026).

    ONE id follows the deal from Sales to TA: the opportunity's own id
    (OPP-2026-007). `req_number` still exists as the internal key — nothing in
    the DB changed — but it no longer appears in the UI, emails or bell
    notifications, because two numbers for one piece of work meant every role
    quoted a different one. Falls back to req_number defensively.
    """
    return str(getattr(getattr(req, "opportunity", None), "opp_id", None) or req.req_number)


def serialize_requirement(req: Requirement, skills: list[dict] | None = None) -> dict:
    return {
        "id": req.id,
        "req_number": req.req_number,
        "opportunity_id": req.opportunity_id,
        # One ID across roles (18 Aug 2026): the parent opportunity's public
        # ID travels with every requirement so the number Sales quoted is the
        # number RMG/TA see and search.
        "opportunity_opp_id": getattr(req.opportunity, "opp_id", None),
        "customer_id": req.customer_id,
        "title": req.title,
        "description": req.description,
        "rmg_jd_text": req.rmg_jd_text,
        "ats_weights": req.ats_weights,
        "no_of_positions": req.no_of_positions,
        "experience_min": _num(req.experience_min),
        "experience_max": _num(req.experience_max),
        "budget_ctc_min": _num(req.budget_ctc_min),
        "budget_ctc_max": _num(req.budget_ctc_max),
        "work_mode": _val(req.work_mode),
        "location_id": req.location_id,
        "priority": _val(req.priority),
        "held_from_status": getattr(req, "held_from_status", None),
        "held_reason": getattr(req, "held_reason", None),
        "target_closure_date": _iso(req.target_closure_date),
        "status": _val(req.status),
        "created_by": req.created_by,
        "sales_head_approved_by": req.sales_head_approved_by,
        "sales_head_approved_at": _iso(req.sales_head_approved_at),
        "sales_head_rejection_reason": req.sales_head_rejection_reason,
        "engineering_reviewed_by": req.engineering_reviewed_by,
        "engineering_reviewed_at": _iso(req.engineering_reviewed_at),
        "engineering_rejection_reason": req.engineering_rejection_reason,
        "created_at": _iso(req.created_at),
        "updated_at": _iso(req.updated_at),
        "skills": skills if skills is not None else [],
    }


def enrich_requirement_jd(db, data: dict, req: Requirement) -> dict:
    """Attach customer JD (from opportunity) + RMG JD files for detail responses."""
    from models import OpportunityAttachment, RequirementAttachment

    cust = db.execute(
        select(OpportunityAttachment)
        .where(
            OpportunityAttachment.opportunity_id == req.opportunity_id,
            OpportunityAttachment.kind == "customer_jd",
        )
        .order_by(OpportunityAttachment.id.desc())
    ).scalars().all()
    rmg = db.execute(
        select(RequirementAttachment)
        .where(
            RequirementAttachment.requirement_id == req.id,
            RequirementAttachment.kind == "rmg_jd",
        )
        .order_by(RequirementAttachment.id.desc())
    ).scalars().all()
    data["customer_jd_attachments"] = [serialize_attachment_row(a) for a in cust]
    data["rmg_jd_attachments"] = [serialize_attachment_row(a) for a in rmg]
    return data


def serialize_job_posting(p) -> dict:
    return {
        "id": p.id,
        "requirement_id": p.requirement_id,
        "portal_name": p.portal_name,
        "job_post_url": p.job_post_url,
        "posted_by": p.posted_by,
        "posted_at": _iso(p.posted_at),
        "status": _val(p.status),
    }


def usernames_for(db: Session, user_ids: list[int]) -> dict[int, dict]:
    """id -> {username, full_name} from the legacy registration_data table."""
    ids = sorted({int(u) for u in user_ids if u is not None})
    if not ids:
        return {}
    stmt = sa.text(
        "SELECT id, username, full_name FROM registration_data WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    rows = db.execute(stmt, {"ids": ids}).mappings().all()
    return {r["id"]: {"username": r["username"], "full_name": r["full_name"] or ""} for r in rows}


def check_and_mark_fulfilled(db: Session, requirement_id: int, acting_user_id: int) -> bool:
    """If enough candidates Joined for this requirement's opportunity, mark it Fulfilled.

    Counts CandidateProfile rows with pipeline_status == Joined whose
    opportunity_id matches the requirement's opportunity. Transitions
    In_Progress -> Fulfilled (logged + creator notified). Caller commits.
    Returns True only when the transition happened in this call.
    """
    req = db.get(Requirement, requirement_id)
    if req is None:
        return False
    joined = db.execute(
        select(sa.func.count()).select_from(CandidateProfile).where(
            CandidateProfile.opportunity_id == req.opportunity_id,
            CandidateProfile.pipeline_status == PipelineStatus.JOINED,
        )
    ).scalar() or 0
    if req.status == RequirementStatus.IN_PROGRESS and joined >= (req.no_of_positions or 1):
        req.status = RequirementStatus.FULFILLED
        log_activity(
            db, RequirementActivityLog, "requirement_id", req.id, acting_user_id,
            "FULFILLED", f"{joined} candidate(s) joined; all {req.no_of_positions} position(s) filled",
        )
        notify_user(
            db, req.created_by,
            f"Requirement {requirement_label(req)} fulfilled",
            f"All {req.no_of_positions} position(s) have joined candidates.",
            f"/requirements/{req.id}",
        )
        return True
    return False
