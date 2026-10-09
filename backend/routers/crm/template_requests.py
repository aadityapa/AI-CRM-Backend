"""Interview-template request workflow API (TA -> RMG -> TA).

Flow:
  1. TA raises a request against a requirement  -> Pending_RMG   (notifies RMG)
  2. RMG picks a real job template + fulfils     -> Template_Ready (stamps opportunityId; notifies TA)
  3. TA attaches candidate email + template       -> Prepared      (notifies RMG)
RBAC is enforced per endpoint (Admin passes everywhere via role_required).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, get_crm_db, page_params, gated_read, gated_write
from models import (
    Customer,
    Opportunity,
    Requirement,
    RequirementSkill,
    Skill,
    TemplateRequest,
    TemplateRequestStatus,
)
from schemas.common import envelope
from schemas.template_requests import (
    TemplateRequestCreate,
    TemplateRequestFulfill,
    TemplateRequestPrepare,
    TemplateRequestRelink,
)
from services.crm_common import next_sequence_number, paginate
from services.notify import notify_role, notify_user

router = APIRouter(prefix="/api/template-requests", tags=["CRM: Template Requests"])


def _now():
    return datetime.now(timezone.utc)


def _ev(v):
    return v.value if hasattr(v, "value") else v


def _legacy_db_target() -> str:
    from crm_db import crm_database_url
    url = crm_database_url()
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


def _requirement_skill_names(db: Session, requirement_id: int) -> str:
    names = db.execute(
        select(Skill.name)
        .join(RequirementSkill, RequirementSkill.skill_id == Skill.id)
        .where(RequirementSkill.requirement_id == requirement_id)
    ).scalars().all()
    return ", ".join(n for n in names if n)


def _experience_level(req: Requirement) -> str | None:
    lo, hi = req.experience_min, req.experience_max
    if lo is None and hi is None:
        return None
    lo_s = f"{float(lo):g}" if lo is not None else "0"
    hi_s = f"{float(hi):g}" if hi is not None else "+"
    return f"{lo_s}-{hi_s} yrs"


def _serialize(db: Session, tr: TemplateRequest) -> dict:
    req = db.get(Requirement, tr.requirement_id)
    opp = None
    if tr.opportunity_id:
        opp = db.get(Opportunity, tr.opportunity_id)
    elif req is not None:
        opp = db.get(Opportunity, req.opportunity_id)
    return {
        "id": tr.id,
        "tr_number": tr.tr_number,
        "requirement_id": tr.requirement_id,
        "requirement_number": req.req_number if req else None,
        "requirement_title": req.title if req else None,
        "opportunity_id": tr.opportunity_id or (req.opportunity_id if req else None),
        "opportunity_opp_id": opp.opp_id if opp else None,
        "opportunity_title": opp.title if opp else None,
        # For the Create-template prefill: RMG shouldn't retype the customer.
        "customer_name": (
            (db.get(Customer, opp.customer_id).name if db.get(Customer, opp.customer_id) else None)
            if opp and opp.customer_id else None
        ),
        "role_title": tr.role_title,
        "skills": tr.skills,
        "experience_level": tr.experience_level,
        "notes": tr.notes,
        "status": _ev(tr.status),
        "requested_by": tr.requested_by,
        "template_name": tr.template_name,
        "template_job_id": tr.template_job_id,
        "fulfilled_by": tr.fulfilled_by,
        "fulfilled_at": tr.fulfilled_at.isoformat() if tr.fulfilled_at else None,
        "candidate_name": tr.candidate_name,
        "candidate_email": tr.candidate_email,
        "prepared_by": tr.prepared_by,
        "prepared_at": tr.prepared_at.isoformat() if tr.prepared_at else None,
        "created_at": tr.created_at.isoformat() if tr.created_at else None,
        "updated_at": tr.updated_at.isoformat() if tr.updated_at else None,
    }


def _get_or_404(db: Session, tr_id: int) -> TemplateRequest:
    tr = db.get(TemplateRequest, tr_id)
    if tr is None:
        raise HTTPException(status_code=404, detail="Template request not found")
    return tr


def _require_status(tr: TemplateRequest, allowed: tuple, action: str) -> None:
    current = _ev(tr.status)
    allowed_vals = tuple(s.value for s in allowed)
    if current not in allowed_vals:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} a request in status '{current}'. Allowed: {', '.join(allowed_vals)}",
        )


def template_owner_conflict(tpl: dict, opp_id: str) -> str:
    """PURE: the OTHER opportunity a template already belongs to, else "".

    A job template carries ONE `opportunityId`. Linking it to a second request
    used to overwrite that stamp silently, so two deals ran the same questions
    and the report printed the other deal's role, opportunity and customer
    (8 Oct 2026: a Bluetooth Developer candidate was interviewed on the "AGM -
    R&D (ADAS & ARAS)" template of C-2026-00097)."""
    owner = " ".join(str(tpl.get("opportunityId") or "").split())
    return owner if owner and owner.lower() != str(opp_id or "").strip().lower() else ""


def clone_job_id(job_id: str, opp_id: str) -> str:
    """PURE: the id of a template copied for another opportunity."""
    tail = re.sub(r"[^A-Za-z0-9]+", "", str(opp_id or ""))[-12:] or "copy"
    return f"{str(job_id)[:40]}-{tail}".lower()


def _stamp_template_opportunity(job_id: str, opp: Opportunity, customer_name: str | None) -> dict:
    """Make the template THIS opportunity's (legacy job_templates.opportunityId
    + customerName), so AI L1 matching and the report name the right deal.

    A template that already belongs to ANOTHER opportunity is never re-stamped
    (8 Oct 2026): it is COPIED under a new job id for this one, so both deals
    keep their own template and their own report header. Returns the stamped
    template; `_cloned_from` says when a copy was made."""
    from auth_db import get_job_template, upsert_job_template

    tpl = get_job_template(_legacy_db_target(), job_id)
    if tpl is None:
        raise HTTPException(status_code=400, detail=f"Job template '{job_id}' not found")
    tpl = dict(tpl)
    owner = template_owner_conflict(tpl, opp.opp_id)
    if owner:
        new_id = clone_job_id(job_id, opp.opp_id)
        existing = get_job_template(_legacy_db_target(), new_id)
        if existing is not None and not template_owner_conflict(dict(existing), opp.opp_id):
            tpl = dict(existing)          # copied before for this deal — reuse it
        else:
            tpl["jobId"] = new_id
        tpl["_cloned_from"] = job_id
        tpl["_cloned_owner"] = owner
    tpl["opportunityId"] = opp.opp_id
    if customer_name:
        tpl["customerName"] = customer_name
    cloned_from, cloned_owner = tpl.pop("_cloned_from", None), tpl.pop("_cloned_owner", None)
    saved = dict(upsert_job_template(_legacy_db_target(), tpl))
    if cloned_from:
        saved["_cloned_from"] = cloned_from
        saved["_cloned_owner"] = cloned_owner
    return saved


def _link_template(db: Session, tr: TemplateRequest, job_id: str, template_name: str | None) -> tuple[dict, str]:
    """Stamp (or copy) the template for the request's opportunity and record it
    on the request. Returns (template, note about a copy or "")."""
    job_id = (job_id or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="template_job_id is required")
    if tr.opportunity_id is None:
        req = db.get(Requirement, tr.requirement_id)
        if req is not None:
            tr.opportunity_id = req.opportunity_id
    opp = db.get(Opportunity, tr.opportunity_id) if tr.opportunity_id else None
    if opp is None:
        raise HTTPException(status_code=400, detail="Template request has no linked opportunity")
    customer = db.get(Customer, opp.customer_id) if opp.customer_id else None
    stamped = _stamp_template_opportunity(job_id, opp, customer.name if customer else None)
    tr.template_job_id = str(stamped.get("jobId") or job_id)
    tr.template_name = (template_name or "").strip() or str(stamped.get("jobTitle") or job_id)
    note = ""
    if stamped.get("_cloned_from"):
        note = (f" The template belonged to {stamped['_cloned_owner']}, so a copy was made for "
                f"{opp.opp_id} — the other deal keeps its own.")
    return stamped, note


#: How an open request reads in the refusal and on the opportunity page.
_STATUS_WORDS = {
    TemplateRequestStatus.PENDING_RMG.value: "waiting for RMG",
    TemplateRequestStatus.TEMPLATE_READY.value: "template ready",
    TemplateRequestStatus.PREPARED.value: "template ready",
}


def open_request_for(db: Session, req: Requirement) -> TemplateRequest | None:
    """The live (not Cancelled) template request of this requirement's
    OPPORTUNITY — or of the requirement itself when it has none. A cancelled
    request does not count, so TA can ask again after RMG / TA cancels it."""
    cond = (TemplateRequest.opportunity_id == req.opportunity_id) if req.opportunity_id \
        else (TemplateRequest.requirement_id == req.id)
    return db.execute(
        select(TemplateRequest).where(cond, TemplateRequest.status != TemplateRequestStatus.CANCELLED)
        .order_by(TemplateRequest.id.desc()).limit(1)
    ).scalars().first()


@router.post("")
def create_request(
    payload: TemplateRequestCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("template-requests", "TA")),
):
    req = db.get(Requirement, payload.requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    # ONE live request per opportunity (1 Oct 2026, user report: TA pressed
    # "Request template" several times and RMG got a pile of duplicates).
    existing = open_request_for(db, req)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"A template was already requested for this opportunity ({existing.tr_number}, "
                   f"{_STATUS_WORDS.get(_ev(existing.status), _ev(existing.status))}). "
                   "Open Template Requests to follow it up.",
        )
    tr = TemplateRequest(
        tr_number=next_sequence_number(db, TemplateRequest, TemplateRequest.tr_number, "TR"),
        requirement_id=req.id,
        opportunity_id=req.opportunity_id,
        role_title=(payload.role_title or req.title).strip(),
        skills=(payload.skills or _requirement_skill_names(db, req.id)) or None,
        experience_level=payload.experience_level or _experience_level(req),
        notes=(payload.notes or None),
        status=TemplateRequestStatus.PENDING_RMG,
        requested_by=user.id,
    )
    db.add(tr)
    db.flush()
    notify_role(db, "RMG",
                f"Template request {tr.tr_number} for {tr.role_title}",
                f"TA requested an interview template (exp {tr.experience_level or 'n/a'}). Skills: {tr.skills or 'n/a'}.",
                f"/template-requests/{tr.id}", exclude_user_id=user.id,
                event="template_request.created", actor=user)
    db.commit()
    db.refresh(tr)
    return envelope(_serialize(db, tr), message=f"Template request {tr.tr_number} raised")


@router.get("")
def list_requests(
    status: str | None = None,
    requirement_id: int | None = None,
    opportunity_id: int | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    stmt = select(TemplateRequest)
    if status:
        valid = {s.value for s in TemplateRequestStatus}
        if status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Invalid status. Allowed: {', '.join(sorted(valid))}")
        stmt = stmt.where(TemplateRequest.status == status)
    if requirement_id is not None:
        stmt = stmt.where(TemplateRequest.requirement_id == requirement_id)
    if opportunity_id is not None:
        stmt = stmt.where(TemplateRequest.opportunity_id == opportunity_id)
    if p.search:
        # Role title or TR number — the two things people quote back (25 Aug 2026).
        like = f"%{p.search}%"
        from sqlalchemy import or_ as _or
        stmt = stmt.where(_or(TemplateRequest.role_title.ilike(like),
                              TemplateRequest.tr_number.ilike(like)))
    stmt = stmt.order_by(TemplateRequest.id.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope([_serialize(db, t) for t in items], meta=meta)


@router.get("/{tr_id}")
def get_request(
    tr_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    return envelope(_serialize(db, _get_or_404(db, tr_id)), message="Template request fetched")


@router.post("/{tr_id}/fulfill")
def fulfill_request(
    tr_id: int,
    payload: TemplateRequestFulfill,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("template-requests", "RMG")),
):
    tr = _get_or_404(db, tr_id)
    _require_status(tr, (TemplateRequestStatus.PENDING_RMG,), "fulfil")

    _, note = _link_template(db, tr, payload.template_job_id, payload.template_name)
    if payload.notes:
        tr.notes = (f"{tr.notes}\n" if tr.notes else "") + f"RMG: {payload.notes.strip()}"
    tr.status = TemplateRequestStatus.TEMPLATE_READY
    tr.fulfilled_by = user.id
    tr.fulfilled_at = _now()
    if tr.requested_by != user.id:
        notify_user(db, tr.requested_by,
                    f"Template ready for {tr.tr_number}",
                    f"RMG linked template '{tr.template_name}' ({tr.template_job_id}). Trigger AI L1 when ready.",
                    f"/template-requests/{tr.id}", actor=user)
    db.commit()
    db.refresh(tr)
    return envelope(_serialize(db, tr), message="Template linked to opportunity; back to TA" + note)


@router.post("/{tr_id}/relink")
def relink_request(
    tr_id: int,
    payload: TemplateRequestRelink,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("template-requests", "RMG")),
):
    """Change the template of a request that already has one (8 Oct 2026).

    RMG linked the wrong template and every AI L1 since asked another role's
    questions; until now only a PENDING request could be fulfilled, so the
    mistake could not be undone. A reason is required and kept in the notes;
    interviews already run on the old template are NOT changed — TA sends those
    candidates a fresh link and voids the old one.
    """
    tr = _get_or_404(db, tr_id)
    _require_status(tr, (TemplateRequestStatus.TEMPLATE_READY, TemplateRequestStatus.PREPARED), "change the template of")
    old_name, old_id = tr.template_name, tr.template_job_id
    if (payload.template_job_id or "").strip() == (old_id or ""):
        raise HTTPException(status_code=400, detail="That template is already linked to this request")
    _, note = _link_template(db, tr, payload.template_job_id, payload.template_name)
    reason = payload.reason.strip()
    tr.notes = (f"{tr.notes}\n" if tr.notes else "") + \
        f"RMG changed the template from '{old_name or old_id}' to '{tr.template_name}': {reason}"
    tr.fulfilled_by = user.id
    tr.fulfilled_at = _now()
    if tr.requested_by and tr.requested_by != user.id:
        notify_user(db, tr.requested_by,
                    f"Template changed for {tr.tr_number}",
                    f"RMG replaced '{old_name or old_id}' with '{tr.template_name}' ({reason}). "
                    "Candidates already interviewed on the old template need a fresh AI L1 link "
                    "(Reschedule ▸ void the previous one).",
                    f"/template-requests/{tr.id}", actor=user)
    db.commit()
    db.refresh(tr)
    return envelope(_serialize(db, tr), message=f"Template changed to '{tr.template_name}'." + note)


@router.post("/{tr_id}/prepare")
def prepare_request(
    tr_id: int,
    payload: TemplateRequestPrepare,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("template-requests", "TA")),
):
    tr = _get_or_404(db, tr_id)
    _require_status(tr, (TemplateRequestStatus.TEMPLATE_READY,), "prepare")
    email = payload.candidate_email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="A valid candidate email is required")
    tr.candidate_email = email
    tr.candidate_name = (payload.candidate_name or "").strip() or None
    tr.status = TemplateRequestStatus.PREPARED
    tr.prepared_by = user.id
    tr.prepared_at = _now()
    if tr.fulfilled_by and tr.fulfilled_by != user.id:
        notify_user(db, tr.fulfilled_by,
                    f"L1 prepared for {tr.tr_number}",
                    f"TA attached candidate {email} to template '{tr.template_name}'.",
                    f"/template-requests/{tr.id}", actor=user)
    db.commit()
    db.refresh(tr)
    return envelope(
        _serialize(db, tr),
        message=f"L1 screening prepared with template '{tr.template_name}' for {email}. "
                f"Generate the interview invite from the candidate's resume (Schedule AI L1).",
    )


@router.post("/{tr_id}/cancel")
def cancel_request(
    tr_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("template-requests", "TA")),
):
    tr = _get_or_404(db, tr_id)
    _require_status(tr, (TemplateRequestStatus.PENDING_RMG, TemplateRequestStatus.TEMPLATE_READY), "cancel")
    tr.status = TemplateRequestStatus.CANCELLED
    db.commit()
    db.refresh(tr)
    return envelope(_serialize(db, tr), message="Template request cancelled")


@router.delete("/{tr_id}")
def delete_request(
    tr_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("template-requests", "TA", "RMG")),
):
    """Hard-delete a template request. Blocks Prepared (downstream L1 may reference it)."""
    from services.crm_common import commit_or_conflict

    tr = _get_or_404(db, tr_id)
    status = _ev(tr.status)
    if status == TemplateRequestStatus.PREPARED.value:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: request is Prepared (candidate L1 already attached).",
        )
    db.delete(tr)
    commit_or_conflict(db, "Cannot delete: template request is still referenced by other records.")
    return envelope(data={"id": tr_id}, message="Template request deleted")
