"""Requirements workflow router: CRUD, approval chain, job postings, activity log.

Server-enforced workflow:
Draft -> Pending_Sales_Head_Approval -> Pending_Engineering_Review -> Open_For_Sourcing
      -> Posted_On_Portals -> In_Progress -> Fulfilled
Rejects: Sales_Head_Rejected / Engineering_Rejected (Sales edits + resubmits).
Manual: Closed / Cancelled (Admin or Sales_Head, from any non-terminal state).
Every status change is written to requirement_activity_log and triggers notifications.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_create, get_crm_db, get_current_user, page_params,
    role_required, gated_read, gated_write, gated_write_action, screener_or)
from models import (
    Opportunity, PipelineStage, Priority, Requirement, RequirementActivityLog,
    RequirementAttachment,
    RequirementJobPosting, RequirementSkill, RequirementStatus, Skill, WorkMode,
)
from schemas.common import CommentIn, RejectIn, envelope
from schemas.requirements import (
    EngineeringApproveIn, JobPostingIn, RequirementCreate, RequirementSkillIn, RequirementUpdate,
)
from services.crm_common import log_activity, next_sequence_number, paginate, save_upload_hashed
from services.notify import notify_role, notify_user
from services.opportunities import backfill_requirement_from_opportunity
from services.requirements import (
    requirement_label,
    EDITABLE_STATUSES, TERMINAL_STATUSES, apply_visibility, enrich_requirement_jd, ensure_visible,
    get_requirement_or_404, serialize_attachment_row, serialize_job_posting, serialize_requirement,
    skills_by_requirement, usernames_for,
)

router = APIRouter(prefix="/api/requirements", tags=["CRM: Requirements"])

# Requirements are opportunity-spawned records; the "requirements" registry
# tab was removed (Aug 2026, unused in the UI), so creation is governed by the
# OPPORTUNITIES grant — whoever may create opportunities may create these.
create_requirements_gate = gated_create("opportunities", "Sales")

_STATUS_VALUES = {s.value for s in RequirementStatus}
_STAGE_VALUES = {s.value for s in PipelineStage}
_PRIORITY_VALUES = {p.value for p in Priority}
_JOB_POSTING_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


def _now():
    return datetime.now(timezone.utc)


def _require_status(req: Requirement, allowed: tuple, action: str) -> None:
    if req.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} a requirement in status '{req.status.value}'. "
                   f"Allowed: {', '.join(s.value for s in allowed)}",
        )


def _validated_skills(db: Session, skills: list[RequirementSkillIn]) -> list[RequirementSkillIn]:
    ids = [s.skill_id for s in skills]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Duplicate skill_id in skills list")
    if ids:
        existing = set(db.execute(select(Skill.id).where(Skill.id.in_(ids))).scalars().all())
        missing = sorted(set(ids) - existing)
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown skill_id(s): {missing}")
    return skills


def _one(db: Session, req: Requirement) -> dict:
    data = serialize_requirement(req, skills_by_requirement(db, [req.id]).get(req.id, []))
    data["ctc_bands"] = _safe_ctc_bands(db, req)
    _attach_names(db, data, req)
    from services.requirement_assignments import assignments_by_requirement
    data["assigned_tas"] = assignments_by_requirement(db, [req.id]).get(req.id, [])
    return enrich_requirement_jd(db, data, req)


def _attach_names(db: Session, data: dict, req: Requirement) -> None:
    """Customer + location NAMES on every single-requirement response (28 Sep 2026).

    The list endpoint already carried them; the detail page fetched the name from
    `/api/customers/{id}`, which is gated to the Customers tab — so TA / RMG / GM
    saw "Customer #60" in the page header. The payload is now self-sufficient,
    exactly like the list rows.
    """
    from models import Customer, Location

    data["customer_name"] = (
        db.execute(select(Customer.name).where(Customer.id == req.customer_id)).scalar_one_or_none()
        if req.customer_id else None
    )
    loc = None
    if req.location_id:
        row = db.execute(
            select(Location.city, Location.state).where(Location.id == req.location_id)
        ).first()
        if row:
            loc = ", ".join(p for p in row if p) or None
    data["location_name"] = loc


def _safe_ctc_bands(db: Session, req: Requirement) -> list[dict]:
    """Slab-wise budget for RMG/TA (18 Aug 2026): the experience columns plus
    Engineering Budget and Approved CTC. Everything that reveals the MARGIN —
    rate, monthly/annual revenue, management cost %, hike %, appraisal cycles
    — is withheld here and stays on the Sales-gated opportunity. This is the
    deliberate middle: enough to source the right candidate at the right
    money, nothing about what the customer is charged."""
    from models import OpportunityCtcSlab

    if not req.opportunity_id:
        return []
    rows = db.execute(
        select(OpportunityCtcSlab)
        .where(OpportunityCtcSlab.opportunity_id == req.opportunity_id)
        .order_by(OpportunityCtcSlab.position, OpportunityCtcSlab.id)
    ).scalars().all()
    def _f(v):
        return float(v) if v is not None else None
    return [
        {
            "exp_min": _f(r.exp_min),
            "exp_max": _f(r.exp_max),
            "target_exp": _f(r.target_exp),
            "engineering_budget": _f(r.engineering_budget),
            "approved_ctc_lac": _f(r.approved_ctc_lac),
        }
        for r in rows
    ]


_ATT_MAX_BYTES = 15 * 1024 * 1024
_REQ_ATT_READER = gated_read("requirements", "TA", "RMG", "Sales_Head", "Sales")


# ---------------------------------------------------------------- CRUD

@router.post("")
def create_requirement(
    payload: RequirementCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(create_requirements_gate),
):
    opp = db.get(Opportunity, payload.opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    skills = _validated_skills(db, payload.skills)
    req = Requirement(
        req_number=next_sequence_number(db, Requirement, Requirement.req_number, "REQ"),
        opportunity_id=opp.id,
        customer_id=payload.customer_id or opp.customer_id,
        title=payload.title,
        description=payload.description,
        no_of_positions=payload.no_of_positions,
        experience_min=payload.experience_min,
        experience_max=payload.experience_max,
        budget_ctc_min=payload.budget_ctc_min,
        budget_ctc_max=payload.budget_ctc_max,
        work_mode=WorkMode(payload.work_mode) if payload.work_mode else None,
        location_id=payload.location_id,
        priority=Priority(payload.priority),
        target_closure_date=payload.target_closure_date,
        status=RequirementStatus.DRAFT,
        created_by=user.id,
    )
    db.add(req)
    db.flush()
    for s in skills:
        db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id,
                                is_mandatory=s.is_mandatory, min_rating=s.min_rating))
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "CREATED", f"Requirement {requirement_label(req)} created as Draft")
    db.commit()
    db.refresh(req)
    return envelope(_one(db, req), message=f"Requirement {requirement_label(req)} created")


@router.get("")
def list_requirements(
    status: str | None = None,
    #: Deal-stage filter (22 Sep 2026) — CSV, because the Sales-style tabs group
    #: several stages ("Closed" is Won + Lost + Partial). Every role's list can
    #: now be sliced by what SALES did, not only by sourcing status.
    opportunity_stage: str | None = None,
    customer_id: int | None = None,
    priority: str | None = None,
    #: Only the positions a TA was ASSIGNED to (1 Oct 2026) — the "Assigned to
    #: me" switch on TA's Opportunities page. Never the default: every TA still
    #: sees every sourcing position.
    assigned_to_me: bool = False,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    stmt = apply_visibility(select(Requirement), user, status, db=db)
    if assigned_to_me:
        from services.requirement_assignments import assigned_requirement_ids
        stmt = stmt.where(Requirement.id.in_(assigned_requirement_ids(db, user.id) or [-1]))
    # ⚠️ `Opportunity` is the MODULE-level import. A `from models import
    # Opportunity` further down this function (the search branch) made the name
    # a local for the WHOLE function, so this line raised UnboundLocalError —
    # every stage-tab request 500'd (reported 23 Sep 2026). Never re-import a
    # module-level name inside a function body.
    joined_opportunity = False
    if opportunity_stage:
        wanted = [v.strip() for v in opportunity_stage.split(",") if v.strip()]
        bad = [v for v in wanted if v not in _STAGE_VALUES]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"Invalid opportunity stage filter '{bad[0]}'")
        stmt = (stmt.outerjoin(Opportunity, Opportunity.id == Requirement.opportunity_id)
                    .where(Opportunity.pipeline_stage.in_(
                        [PipelineStage(v) for v in wanted])))
        joined_opportunity = True
    if status:
        # CSV accepted (29 Sep 2026) so TA's Active tab asks for the three
        # sourcing statuses in ONE paginated request instead of a fan-out.
        wanted_st = [v.strip() for v in status.split(",") if v.strip()]
        bad_st = [v for v in wanted_st if v not in _STATUS_VALUES]
        if bad_st:
            raise HTTPException(status_code=400, detail=f"Invalid status filter '{bad_st[0]}'")
        stmt = stmt.where(Requirement.status.in_([RequirementStatus(v) for v in wanted_st]))
    if customer_id is not None:
        stmt = stmt.where(Requirement.customer_id == customer_id)
    if priority:
        if priority not in _PRIORITY_VALUES:
            raise HTTPException(status_code=400, detail=f"Invalid priority filter '{priority}'")
        stmt = stmt.where(Requirement.priority == Priority(priority))
    if p.search:
        like = f"%{p.search}%"
        # Searching by the OPPORTUNITY id must find the requirement too — all
        # roles track one number (18 Aug 2026). Outer join: never drops rows.
        # Join once: a second join of the same table is a SQL error.
        if not joined_opportunity:
            stmt = stmt.outerjoin(Opportunity, Opportunity.id == Requirement.opportunity_id)
            joined_opportunity = True
        stmt = stmt.where(
            or_(Requirement.title.ilike(like), Requirement.req_number.ilike(like),
                Opportunity.opp_id.ilike(like)))
    # Eager-load the parent opportunity: the serializer reads its opp_id for
    # every row — without this, a 100-row page costs 100 extra queries.
    from sqlalchemy.orm import selectinload
    stmt = stmt.options(selectinload(Requirement.opportunity))
    stmt = stmt.order_by(Requirement.created_at.desc(), Requirement.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    smap = skills_by_requirement(db, [r.id for r in items])
    rows = [serialize_requirement(r, smap.get(r.id, [])) for r in items]
    # Resolve customer + work-location NAMES here (batched, no N+1) so the list is
    # self-sufficient — a TA whose tab override omits Customers can't call
    # /api/customers, and would otherwise see only "#<id>".
    from models import Customer, Location
    cust_ids = {r.customer_id for r in items if r.customer_id}
    loc_ids = {r.location_id for r in items if r.location_id}
    cust_names = dict(db.execute(
        select(Customer.id, Customer.name).where(Customer.id.in_(cust_ids))
    ).all()) if cust_ids else {}
    loc_names = {
        lid: ", ".join([p for p in (city, state) if p])
        for lid, city, state in (db.execute(
            select(Location.id, Location.city, Location.state).where(Location.id.in_(loc_ids))
        ).all() if loc_ids else [])
    }
    from services.requirement_assignments import assignments_by_requirement
    assigned = assignments_by_requirement(db, [r.id for r in items])
    for row in rows:
        row["customer_name"] = cust_names.get(row.get("customer_id"))
        row["location_name"] = loc_names.get(row.get("location_id"))
        row["assigned_tas"] = assigned.get(row["id"], [])
    return envelope(rows, meta=meta)


@router.get("/{requirement_id}")
def get_requirement(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    req = get_requirement_or_404(db, requirement_id)
    ensure_visible(user, req, db=db)
    # Backfill Experience/Budget/Work mode/Location/Target closure from the
    # linked opportunity for requirements created before the carry-over existed.
    if backfill_requirement_from_opportunity(db, req):
        db.commit()
        db.refresh(req)
    data = _one(db, req)
    # Lets the UI hide the Job Postings tab until it is actually used
    # (28 Aug 2026, user decision) — portal logging is optional here.
    from models import RequirementJobPosting
    data["job_postings_count"] = db.execute(
        select(func.count()).select_from(RequirementJobPosting)
        .where(RequirementJobPosting.requirement_id == req.id)
    ).scalar() or 0
    return envelope(data)


@router.put("/{requirement_id}")
def update_requirement(
    requirement_id: int,
    payload: RequirementUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "Sales")),
):
    req = get_requirement_or_404(db, requirement_id)
    if not user.is_admin and req.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator (or Admin) can edit this requirement")
    _require_status(req, EDITABLE_STATUSES, "edit")

    data = payload.model_dump(exclude_unset=True)
    skills_in = data.pop("skills", None)
    if "priority" in data:
        value = data.pop("priority")
        if value is not None:
            req.priority = Priority(value)
    if "work_mode" in data:
        value = data.pop("work_mode")
        req.work_mode = WorkMode(value) if value else None
    for field, value in data.items():
        setattr(req, field, value)

    if skills_in is not None:
        skills = _validated_skills(db, payload.skills or [])
        db.execute(
            RequirementSkill.__table__.delete().where(RequirementSkill.requirement_id == req.id)
        )
        for s in skills:
            db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id,
                                    is_mandatory=s.is_mandatory, min_rating=s.min_rating))

    # Editing a rejected requirement keeps its status; resubmission is explicit via /submit.
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "UPDATED", "Requirement details updated")
    db.commit()
    db.refresh(req)
    return envelope(_one(db, req), message="Requirement updated")


# ---------------------------------------------------------------- workflow transitions

@router.post("/{requirement_id}/submit")
def submit_requirement(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "Sales")),
):
    req = get_requirement_or_404(db, requirement_id)
    if not user.is_admin and req.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator (or Admin) can submit this requirement")
    _require_status(req, EDITABLE_STATUSES, "submit")
    previous = req.status.value
    req.status = RequirementStatus.PENDING_SALES_HEAD_APPROVAL
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SUBMITTED", f"Submitted for Sales Head approval (was {previous})")
    notify_role(db, "Sales_Head",
                f"Requirement {requirement_label(req)} submitted for approval",
                f"'{req.title}' awaits your approval.",
                f"/requirements/{req.id}", exclude_user_id=user.id,
                event="requirement.submitted", actor=user)
    db.commit()
    return envelope(_one(db, req), message="Submitted for Sales Head approval")


@router.post("/{requirement_id}/sales-head-approve")
def sales_head_approve(
    requirement_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    # Approval buttons (Access Template ▸ Approvals) — Requirements: Edit alone
    # never implied the Sales Head / Engineering decision.
    user: CurrentUser = Depends(gated_write_action("requirement.sales_head_approve", "requirements")),
):
    req = get_requirement_or_404(db, requirement_id)
    _require_status(req, (RequirementStatus.PENDING_SALES_HEAD_APPROVAL,), "sales-head-approve")
    req.status = RequirementStatus.PENDING_ENGINEERING_REVIEW
    req.sales_head_approved_by = user.id
    req.sales_head_approved_at = _now()
    comment = (payload.comment if payload else None) or None
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SALES_HEAD_APPROVED", comment or "Approved by Sales Head")
    notify_role(db, "RMG",
                f"Requirement {requirement_label(req)} pending RMG review",
                f"'{req.title}' was approved by Sales Head and needs RMG review.",
                f"/requirements/{req.id}", exclude_user_id=user.id,
                event="requirement.sales_approved", actor=user)
    if req.created_by != user.id:
        notify_user(db, req.created_by,
                    f"Requirement {requirement_label(req)} approved by Sales Head",
                    "Moved to RMG review.", f"/requirements/{req.id}",
                    actor=user)
    db.commit()
    return envelope(_one(db, req), message="Approved; moved to RMG review")


@router.post("/{requirement_id}/sales-head-reject")
def sales_head_reject(
    requirement_id: int,
    payload: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write_action("requirement.sales_head_approve", "requirements")),
):
    try:
        reason = payload.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    req = get_requirement_or_404(db, requirement_id)
    _require_status(req, (RequirementStatus.PENDING_SALES_HEAD_APPROVAL,), "sales-head-reject")
    req.status = RequirementStatus.SALES_HEAD_REJECTED
    req.sales_head_rejection_reason = reason
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SALES_HEAD_REJECTED", reason)
    notify_user(db, req.created_by,
                f"Requirement {requirement_label(req)} rejected by Sales Head",
                reason, f"/requirements/{req.id}", actor=user)
    db.commit()
    return envelope(_one(db, req), message="Requirement rejected by Sales Head")


@router.post("/{requirement_id}/engineering-approve")
def engineering_approve(
    requirement_id: int,
    payload: EngineeringApproveIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write_action("requirement.engineering_approve", "requirements")),
):
    req = get_requirement_or_404(db, requirement_id)
    _require_status(req, (RequirementStatus.PENDING_ENGINEERING_REVIEW,), "engineering-approve")
    jd_text = ((payload.rmg_jd_text if payload else None) or "").strip()
    has_file = db.execute(
        select(RequirementAttachment.id).where(
            RequirementAttachment.requirement_id == req.id,
            RequirementAttachment.kind == "rmg_jd",
        ).limit(1)
    ).scalar_one_or_none() is not None
    if not jd_text and not has_file:
        raise HTTPException(
            status_code=400,
            detail="Add a JD (text or file) before approving",
        )
    # Skill Evaluation Details are equally mandatory: TA sources against these
    # skills and the ATS score is built on them (mandatory skills carry 50 of
    # 100 points) — approving without any produces an unsourceable requirement.
    if payload is not None and payload.skills is not None:
        if len(payload.skills) == 0:
            raise HTTPException(
                status_code=400,
                detail="Add at least one skill (Skill Evaluation Details) before approving",
            )
    else:
        has_skill = db.execute(
            select(RequirementSkill.id)
            .where(RequirementSkill.requirement_id == req.id).limit(1)
        ).first() is not None
        if not has_skill:
            raise HTTPException(
                status_code=400,
                detail="Add at least one skill (Skill Evaluation Details) before approving",
            )
    if jd_text:
        req.rmg_jd_text = jd_text
    # Skill Evaluation Details are RMG's to set at this stage. When `skills` is
    # provided it replaces the requirement's skill set (None = leave untouched).
    if payload is not None and payload.skills is not None:
        skills = _validated_skills(db, payload.skills)
        db.execute(
            RequirementSkill.__table__.delete().where(
                RequirementSkill.requirement_id == req.id
            )
        )
        for s in skills:
            db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id,
                                    is_mandatory=s.is_mandatory, min_rating=s.min_rating))
    # ATS component weights — RMG may tune them at review (None = leave unchanged).
    if payload is not None and payload.ats_weights is not None:
        req.ats_weights = payload.ats_weights or None
    req.status = RequirementStatus.OPEN_FOR_SOURCING
    req.engineering_reviewed_by = user.id
    req.engineering_reviewed_at = _now()
    comment = (payload.comment if payload else None) or None
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "ENGINEERING_APPROVED", comment or "Approved by Engineering (RMG)")
    notify_role(db, "TA",
                "New requirement open for sourcing",
                f"Requirement {requirement_label(req)} '{req.title}' is open for sourcing.",
                f"/requirements/{req.id}", exclude_user_id=user.id,
                event="requirement.engineering_approved", actor=user)
    if req.created_by != user.id:
        notify_user(db, req.created_by,
                    f"Requirement {requirement_label(req)} approved by Engineering",
                    "Now open for sourcing.", f"/requirements/{req.id}",
                    actor=user)
    # The sourcing team is picked in the same dialog (1 Oct 2026, user ask):
    # the assigned TAs hear "assigned to you" on top of the role-wide notice.
    assigned = None
    if payload is not None and payload.ta_user_ids is not None:
        from services.requirement_assignments import set_assignments
        try:
            assigned = set_assignments(db, req, payload.ta_user_ids, None, user)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    _rescore_after_jd_change(db, req, user.id)
    message = "Approved; open for sourcing"
    if assigned and assigned["added"]:
        message += f" — {len(assigned['added'])} TA(s) assigned"
    return envelope(_one(db, req), message=message)


@router.post("/{requirement_id}/engineering-reject")
def engineering_reject(
    requirement_id: int,
    payload: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write_action("requirement.engineering_approve", "requirements")),
):
    try:
        reason = payload.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    req = get_requirement_or_404(db, requirement_id)
    _require_status(req, (RequirementStatus.PENDING_ENGINEERING_REVIEW,), "engineering-reject")
    req.status = RequirementStatus.ENGINEERING_REJECTED
    req.engineering_rejection_reason = reason
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "ENGINEERING_REJECTED", reason)
    notify_user(db, req.created_by,
                f"Requirement {requirement_label(req)} rejected by Engineering",
                reason, f"/requirements/{req.id}", actor=user)
    db.commit()
    return envelope(_one(db, req), message="Requirement rejected by Engineering")


@router.post("/{requirement_id}/close")
def close_requirement(
    requirement_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "Sales_Head")),
):
    return _manual_terminal(db, requirement_id, user, RequirementStatus.CLOSED, "CLOSED",
                            (payload.comment if payload else None))


@router.post("/{requirement_id}/cancel")
def cancel_requirement(
    requirement_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    # RMG too (25 Aug 2026): RMG owns the sourcing run, so rejecting a
    # requirement they can no longer serve belongs to them as much as Sales.
    user: CurrentUser = Depends(gated_write("requirements", "RMG", "Sales_Head")),
):
    return _manual_terminal(db, requirement_id, user, RequirementStatus.CANCELLED, "CANCELLED",
                            (payload.comment if payload else None))


#: Sourcing statuses a hold can be taken FROM (and resumed back to).
_HOLDABLE_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


@router.post("/{requirement_id}/hold")
def hold_requirement(
    requirement_id: int,
    payload: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "RMG", "Sales_Head")),
):
    """RMG pauses sourcing (25 Aug 2026). New uploads, slot invites and AI-L1
    scheduling all refuse while held; candidates already mid-pipeline are left
    alone. A reason is mandatory — a hold nobody can explain is a leak."""
    req = get_requirement_or_404(db, requirement_id)
    if req.status not in _HOLDABLE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Only a sourcing-stage requirement can be put on hold (current: {req.status.value})",
        )
    try:
        reason = payload.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    req.held_from_status = req.status.value
    req.held_reason = reason
    req.status = RequirementStatus.ON_HOLD
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "ON_HOLD", f"Sourcing put on hold: {reason}")
    from services.notify import notify_role
    notify_role(db, "TA",
                f"Requirement on hold: {requirement_label(req)}",
                f"'{req.title}' was put on hold by {user.full_name or user.username}: {reason}. "
                "New uploads and interview scheduling are paused until it resumes.",
                f"/admin/?view=crm&p=requirements/{req.id}",
                event="requirement.on_hold", actor=user, exclude_user_id=user.id)
    if req.created_by != user.id:
        notify_user(db, req.created_by,
                    f"Requirement {requirement_label(req)} on hold",
                    reason, f"/admin/?view=crm&p=requirements/{req.id}", actor=user)
    db.commit()
    return envelope(_one(db, req), message="Requirement put on hold — TA notified")


@router.post("/{requirement_id}/resume")
def resume_requirement(
    requirement_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "RMG", "Sales_Head")),
):
    """Resume a held requirement — back to exactly the status it held from."""
    req = get_requirement_or_404(db, requirement_id)
    if req.status != RequirementStatus.ON_HOLD:
        raise HTTPException(status_code=400,
                            detail=f"Requirement is not on hold (current: {req.status.value})")
    try:
        back_to = RequirementStatus(req.held_from_status or "")
    except ValueError:
        back_to = RequirementStatus.OPEN_FOR_SOURCING
    req.status = back_to
    req.held_from_status = None
    req.held_reason = None
    comment = (payload.comment or "").strip() if payload else ""
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUMED", f"Sourcing resumed ({back_to.value})" + (f": {comment}" if comment else ""))
    from services.notify import notify_role
    notify_role(db, "TA",
                f"Requirement resumed: {requirement_label(req)}",
                f"'{req.title}' is back in sourcing ({back_to.value.replace('_', ' ')})."
                + (f" {comment}" if comment else ""),
                f"/admin/?view=crm&p=requirements/{req.id}",
                event="requirement.resumed", actor=user, exclude_user_id=user.id)
    db.commit()
    return envelope(_one(db, req), message="Requirement resumed — TA notified")


class PriorityIn(BaseModel):
    priority: str = Field(pattern="^(Low|Medium|High)$")


@router.patch("/{requirement_id}/priority")
def set_requirement_priority(
    requirement_id: int,
    payload: PriorityIn,
    db: Session = Depends(get_crm_db),
    # Deliberately NOT the full edit form: RMG sets urgency without gaining
    # access to budget fields.
    user: CurrentUser = Depends(screener_or(gated_write("requirements", "RMG", "Sales_Head"))),
):
    req = get_requirement_or_404(db, requirement_id)
    previous = getattr(req.priority, "value", req.priority)
    req.priority = Priority(payload.priority)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "PRIORITY", f"Priority {previous} -> {payload.priority}")
    db.commit()
    return envelope(_one(db, req), message=f"Priority set to {payload.priority}")


#: Who may write the JD text, the skills and the JD FILE — one tuple, so the
#: dialog's Save and its file drop zone can never disagree on who is let in.
#: (30 Sep 2026, user report: TA could open "Edit JD & skills" but the upload
#: answered 403 — the attachment route said RMG alone.)
JD_EDIT_ROLES = ("RMG", "Sales", "Sales_Head", "TA")
#: The ONE gate the three JD routes share. `screener_or` (2 Oct 2026, user ask:
#: "RMG / GM can add the missing skills, RMG JD and customer JD from here") also
#: admits whoever screens as RMG — a GM custom role, or an RMG whose access comes
#: from a template without the requirements Edit grant.
JD_EDIT_GATE = screener_or(gated_write("requirements", *JD_EDIT_ROLES))


class JdSkillsIn(BaseModel):
    """`None` = leave that part untouched; a value replaces it."""
    rmg_jd_text: str | None = None
    description: str | None = None
    skills: list[RequirementSkillIn] | None = None


@router.patch("/{requirement_id}/jd-skills")
def set_requirement_jd_skills(
    requirement_id: int,
    payload: JdSkillsIn,
    db: Session = Depends(get_crm_db),
    # Forgotten JD / skills (15 Sep 2026): RMG, Sales, Sales Head and Admin/CEO
    # may fill or fix them at ANY non-terminal status — unlike the full PUT,
    # which is creator-only and Draft/Rejected-only. Deliberately narrow: no
    # budget, positions or status fields can move through here. TA joined the
    # list on 30 Sep 2026 (user ask): a recruiter who has the JD in hand adds
    # it — the same roles that may attach the JD file below.
    user: CurrentUser = Depends(JD_EDIT_GATE),
):
    req = get_requirement_or_404(db, requirement_id)
    if req.status in TERMINAL_STATUSES:
        _require_status(req, tuple(s for s in RequirementStatus if s not in TERMINAL_STATUSES),
                        "edit the JD or skills of")
    changed: list[str] = []
    if payload.rmg_jd_text is not None:
        req.rmg_jd_text = payload.rmg_jd_text.strip() or None
        changed.append("JD")
    if payload.description is not None:
        req.description = payload.description.strip() or None
        changed.append("description")
    if payload.skills is not None:
        skills = _validated_skills(db, payload.skills)
        db.execute(
            RequirementSkill.__table__.delete().where(RequirementSkill.requirement_id == req.id)
        )
        for s in skills:
            db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id,
                                    is_mandatory=s.is_mandatory, min_rating=s.min_rating))
        changed.append(f"skills ({len(skills)})")
    if not changed:
        raise HTTPException(status_code=400, detail="Nothing to update")
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "JD_SKILLS", f"{user.full_name or user.username} updated: {', '.join(changed)}")
    db.commit()
    db.refresh(req)
    rescoring = _rescore_after_jd_change(db, req, user.id)
    return envelope({**_one(db, req), "rescoring": rescoring},
                    message="JD & skills updated" + (" — re-scoring the applicants' ATS" if rescoring else ""))


def _rescore_after_jd_change(db: Session, req, user_id: int | None) -> bool:
    """A JD / skills change re-scores the position's applicants in the
    background (30 Sep 2026). Nothing to score against yet → nothing started."""
    from services.resumes import has_ats_criteria, rescore_requirement_in_background

    try:
        if not has_ats_criteria(db, req):
            return False
        rescore_requirement_in_background(req.id, user_id)
        return True
    except Exception:
        return False


def _manual_terminal(db: Session, requirement_id: int, user: CurrentUser,
                     new_status: RequirementStatus, action: str, comment: str | None):
    req = get_requirement_or_404(db, requirement_id)
    if req.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Requirement is already in terminal status '{req.status.value}'",
        )
    previous = req.status.value
    req.status = new_status
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 action, comment or f"{new_status.value} (was {previous})")
    if req.created_by != user.id:
        notify_user(db, req.created_by,
                    f"Requirement {requirement_label(req)} {new_status.value.lower()}",
                    comment or "", f"/requirements/{req.id}", actor=user)
    db.commit()
    return envelope(_one(db, req), message=f"Requirement {new_status.value.lower()}")


# ---------------------------------------------------------------- JD attachments

@router.get("/{requirement_id}/attachments")
def list_requirement_attachments(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(_REQ_ATT_READER),
):
    req = get_requirement_or_404(db, requirement_id)
    ensure_visible(user, req, db=db)
    rows = db.execute(
        select(RequirementAttachment)
        .where(RequirementAttachment.requirement_id == requirement_id)
        .order_by(RequirementAttachment.id.desc())
    ).scalars().all()
    return envelope([serialize_attachment_row(a) for a in rows])


@router.post("/{requirement_id}/attachments")
def add_requirement_attachment(
    requirement_id: int,
    file: UploadFile = File(...),
    kind: str | None = Form(None),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(JD_EDIT_GATE),
):
    req = get_requirement_or_404(db, requirement_id)
    data = file.file.read()
    if len(data) > _ATT_MAX_BYTES:
        raise HTTPException(status_code=400, detail="File is larger than 15 MB")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    file.file.seek(0)
    kind_norm = (kind or "rmg_jd").strip().lower() or "rmg_jd"
    if kind_norm == "customer_jd":
        return _add_customer_jd(db, req, file, user)
    url, sha, size = save_upload_hashed(file, "requirement_attachments")
    att = RequirementAttachment(
        requirement_id=req.id,
        file_url=url,
        file_name=(file.filename or "")[:255] or None,
        file_sha256=sha,
        file_size=size,
        kind=kind_norm,
        uploaded_by=user.id,
    )
    db.add(att)
    log_activity(
        db, RequirementActivityLog, "requirement_id", req.id, user.id,
        "ATTACHMENT_ADDED", f"Attachment added ({kind_norm}): {att.file_name or 'file'}",
    )
    # A JD uploaded as PDF / Word (30 Sep 2026, user ask): the ATS and the AI
    # interview read `rmg_jd_text`, so the file's text is read out and — when
    # the field is still blank — becomes the JD text at once. The text is also
    # returned so the dialog can show it for review. Best-effort: a scan or an
    # unreadable file keeps the attachment and fills nothing.
    extracted, filled = None, False
    if kind_norm == "rmg_jd":
        extracted = _jd_text_from_file(url)
        if extracted and not (req.rmg_jd_text or "").strip():
            req.rmg_jd_text = extracted
            filled = True
            log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                         "JD_SKILLS", f"RMG JD text read from {att.file_name or 'the uploaded file'}")
    db.commit()
    db.refresh(att)
    rescoring = kind_norm == "rmg_jd" and _rescore_after_jd_change(db, req, user.id)
    msg = "JD text read from the file and saved" if filled else "Attachment added"
    return envelope({**serialize_attachment_row(att), "extracted_text": extracted, "jd_text_filled": filled,
                     "rescoring": rescoring},
                    message=msg + (" — re-scoring the applicants' ATS" if rescoring else ""))


def _add_customer_jd(db: Session, req, file: UploadFile, user: CurrentUser):
    """The customer's reference JD (2 Oct 2026, user ask). It belongs to the
    OPPORTUNITY (`opportunity_attachments`, kind customer_jd — where the
    opportunity form puts it and where both pages read it from), but whoever may
    write the position's JD may add it from the JD & skills card."""
    from models import OpportunityActivityLog, OpportunityAttachment

    url, sha, size = save_upload_hashed(file, "opportunity_attachments")
    att = OpportunityAttachment(
        opportunity_id=req.opportunity_id, file_url=url,
        file_name=(file.filename or "")[:255] or None, file_sha256=sha, file_size=size,
        kind="customer_jd", uploaded_by=user.id,
    )
    db.add(att)
    log_activity(db, OpportunityActivityLog, "opportunity_id", req.opportunity_id, user.id,
                 "Attachment_Added", f"Customer JD added: {att.file_name or 'file'}")
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "ATTACHMENT_ADDED", f"Customer JD added: {att.file_name or 'file'}")
    db.commit()
    db.refresh(att)
    return envelope({**serialize_attachment_row(att), "kind": "customer_jd"},
                    message="Customer JD added")


#: A JD longer than this is clipped — the ATS keyword pass and the interview
#: prompt need the substance, not a 40-page appendix.
JD_TEXT_MAX_CHARS = 20_000


def _jd_text_from_file(url: str) -> str | None:
    """The text of an uploaded JD (PDF / DOCX / TXT), or None when unreadable."""
    from services.resumes import extract_resume_text

    try:
        text = extract_resume_text(url)
    except Exception:
        return None
    text = (text or "").strip()
    return text[:JD_TEXT_MAX_CHARS] or None


@router.delete("/attachments/{attachment_id}")
def delete_requirement_attachment(
    attachment_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(JD_EDIT_GATE),
):
    att = db.get(RequirementAttachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    req_id = att.requirement_id
    db.delete(att)
    log_activity(
        db, RequirementActivityLog, "requirement_id", req_id, user.id,
        "ATTACHMENT_REMOVED", f"Attachment #{attachment_id} removed",
    )
    db.commit()
    return envelope({"id": attachment_id}, message="Attachment removed")


# ---------------------------------------------------------------- job postings

@router.post("/{requirement_id}/job-postings")
def add_job_posting(
    requirement_id: int,
    payload: JobPostingIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    req = get_requirement_or_404(db, requirement_id)
    _require_status(req, _JOB_POSTING_STATUSES, "add a job posting to")
    posting = RequirementJobPosting(
        requirement_id=req.id,
        portal_name=payload.portal_name,
        job_post_url=payload.job_post_url,
        posted_by=user.id,
    )
    db.add(posting)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "JOB_POSTING_ADDED", f"Posted on {payload.portal_name}: {payload.job_post_url}")
    if req.status == RequirementStatus.OPEN_FOR_SOURCING:
        req.status = RequirementStatus.POSTED_ON_PORTALS
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                     "STATUS_CHANGED", "Auto-moved Open_For_Sourcing -> Posted_On_Portals (first job posting)")
    db.commit()
    db.refresh(posting)
    return envelope(serialize_job_posting(posting), message="Job posting added")


@router.get("/{requirement_id}/job-postings")
def list_job_postings(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    req = get_requirement_or_404(db, requirement_id)
    ensure_visible(user, req, db=db)
    postings = db.execute(
        select(RequirementJobPosting)
        .where(RequirementJobPosting.requirement_id == req.id)
        .order_by(RequirementJobPosting.posted_at.asc(), RequirementJobPosting.id.asc())
    ).scalars().all()
    return envelope([serialize_job_posting(p) for p in postings])


# ---------------------------------------------------------------- activity log

@router.get("/{requirement_id}/activity-log")
def get_activity_log(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(get_current_user),
):
    req = get_requirement_or_404(db, requirement_id)
    ensure_visible(user, req, db=db)
    logs = db.execute(
        select(RequirementActivityLog)
        .where(RequirementActivityLog.requirement_id == req.id)
        .order_by(RequirementActivityLog.timestamp.asc(), RequirementActivityLog.id.asc())
    ).scalars().all()
    users = usernames_for(db, [l.user_id for l in logs])
    data = [{
        "id": l.id,
        "requirement_id": l.requirement_id,
        "user_id": l.user_id,
        "username": users.get(l.user_id, {}).get("username"),
        "full_name": users.get(l.user_id, {}).get("full_name"),
        "action_type": l.action_type,
        "comment": l.comment,
        "timestamp": l.timestamp.isoformat() if l.timestamp else None,
    } for l in logs]
    return envelope(data)
