"""Opportunity pipeline API: CRUD, skills, stage transitions, activity log.

Create/Update: Sales / Sales_Head (Admin implicit). Reads: any CRM role —
Sales sees ALL opportunities (the spec restricts requirements, not opportunities).
Moves to Archived: Sales_Head / Admin only. Every mutation writes to
opportunity_activity_log.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, gated_create, gated_read, gated_write,
    gated_write_action, get_crm_db, page_params, role_required,
)
from crm_deps import gated_read, gated_write, gated_write_action
from models import (
    Customer,
    Candidate,
    CandidateOutreach,
    Opportunity,
    OpportunityActivityLog,
    OpportunityApprovalStatus,
    OpportunityCtcSlab,
    OpportunitySkill,
    OppType,
    PipelineStage,
    Priority,
    Requirement,
    RequirementActivityLog,
    RequirementSkill,
    RequirementStatus,
)
from services.opportunity_form_schema import OpportunitySchemaError, strip_details, validate_details
from schemas.common import CommentIn, RejectIn, envelope
from schemas.opportunities import (
    OpportunityCreate,
    OpportunitySkillIn,
    OpportunityUpdate,
    StageTransitionIn,
)
from services.crm_common import log_activity, next_sequence_number, paginate
from services.notify import notify_role, notify_user
from services.opportunity_ctc import derive_ctc_row, normalize_tm_billing_details
from services.opportunities import (
    fetch_activity_log,
    get_opportunity_or_404,
    replace_skills,
    requirement_fields_from_opportunity,
    serialize_opportunity,
    serialize_skills,
    sync_requirement_from_opportunity,
    validate_refs,
    validate_stage_transition,
)

router = APIRouter(prefix="/api/opportunities", tags=["CRM: Opportunities"])

read_opportunities = gated_read("opportunities")
write_opportunities = gated_write("opportunities", "Sales", "Sales_Head")
create_opportunities = gated_create("opportunities", "Sales", "Sales_Head")

_SORTABLE = {"opp_id": Opportunity.opp_id, "title": Opportunity.title,
             "pipeline_stage": Opportunity.pipeline_stage,
             "created_at": Opportunity.created_at, "id": Opportunity.id,
             # Column filters (4 Sep 2026): the list sorts by the joined
             # customer name and the RFI value too.
             "customer_name": Customer.name, "rfi_value": Opportunity.rfi_value,
             "opp_type": Opportunity.opp_type}


def _now():
    return datetime.now(timezone.utc)


def _spawn_requirement_from_opportunity(db: Session, opp: Opportunity, approver: CurrentUser) -> Requirement:
    """Bridge: an approved opportunity flows into the existing Requirements chain.

    Creates a Requirement linked to the opportunity, already past Sales Head
    approval (the opportunity itself was approved) and sitting in
    Pending_Engineering_Review so it lands in RMG's Engineering Review Queue.
    Skills are copied from the opportunity. Idempotent per opportunity: if a
    requirement already exists for it, nothing is created.
    """
    existing = db.execute(
        select(Requirement.id).where(Requirement.opportunity_id == opp.id)
    ).first()
    if existing:
        return None  # a requirement already exists for this opportunity

    # Carry the Sales-entered details onto the requirement so RMG (Engineering
    # Review) and TA see Experience / Budget / Work mode / Location / Target
    # closure without re-keying. Single source of truth in services.opportunities
    # (also used to backfill requirements created before this carry-over existed).
    carry = requirement_fields_from_opportunity(db, opp)

    req = Requirement(
        req_number=next_sequence_number(db, Requirement, Requirement.req_number, "REQ"),
        opportunity_id=opp.id,
        customer_id=opp.customer_id,
        title=opp.title,
        description=carry["description"],
        no_of_positions=(carry["no_of_positions"] or 1),
        experience_min=carry["experience_min"],
        experience_max=carry["experience_max"],
        budget_ctc_min=carry["budget_ctc_min"],
        budget_ctc_max=carry["budget_ctc_max"],
        work_mode=carry["work_mode"],
        location_id=carry["location_id"],
        target_closure_date=carry["target_closure_date"],
        priority=Priority.MEDIUM,
        status=RequirementStatus.PENDING_ENGINEERING_REVIEW,
        created_by=opp.created_by,
        sales_head_approved_by=approver.id,
        sales_head_approved_at=_now(),
    )
    db.add(req)
    db.flush()
    opp_skills = db.execute(
        select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)
    ).scalars().all()
    for s in opp_skills:
        db.add(RequirementSkill(requirement_id=req.id, skill_id=s.skill_id, is_mandatory=s.is_mandatory))
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, approver.id,
                 "CREATED",
                 f"Requirement for {opp.opp_id} auto-created from the approved opportunity; "
                 f"pending engineering review")
    notify_role(db, "RMG",
                f"Requirement {opp.opp_id} pending engineering review",
                f"'{req.title}' (from opportunity {opp.opp_id}) needs engineering review.",
                f"/requirements/{req.id}", exclude_user_id=approver.id,
                event="opportunity.approved", actor=approver)
    return req


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_opportunities(
    pipeline_stage: str | None = None,
    approval_status: str | None = None,
    customer_id: int | None = None,
    #: Branch hub (18 Aug 2026) — deals belonging to ONE customer branch.
    branch_id: int | None = None,
    opp_type: str | None = None,
    #: Per-column header filters (4 Sep 2026, user request). Server-side —
    #: the list is paginated, so a client-side filter would only see one page.
    opp_id: str | None = None,
    title: str | None = None,
    rfi_min: float | None = None,
    rfi_max: float | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    # Outer join so an opportunity without a customer row still lists; the
    # join exists for sorting/filtering by customer name.
    stmt = select(Opportunity).outerjoin(Customer, Customer.id == Opportunity.customer_id)
    if opp_id:
        stmt = stmt.where(Opportunity.opp_id.ilike(f"%{opp_id.strip()}%"))
    if title:
        stmt = stmt.where(Opportunity.title.ilike(f"%{title.strip()}%"))
    if rfi_min is not None:
        stmt = stmt.where(Opportunity.rfi_value >= rfi_min)
    if rfi_max is not None:
        stmt = stmt.where(Opportunity.rfi_value <= rfi_max)
    if created_from is not None:
        stmt = stmt.where(func.date(Opportunity.created_at) >= created_from)
    if created_to is not None:
        stmt = stmt.where(func.date(Opportunity.created_at) <= created_to)
    if pipeline_stage:
        # CSV accepted (18 Aug 2026) so the list's "All stages" default can ask
        # for the tab's stages in ONE query and keep server-side pagination.
        wanted = [s.strip() for s in pipeline_stage.split(",") if s.strip()]
        valid = {s.value for s in PipelineStage}
        bad = [s for s in wanted if s not in valid]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"Invalid pipeline_stage. Allowed: {', '.join(s.value for s in PipelineStage)}")
        if wanted:
            stmt = stmt.where(Opportunity.pipeline_stage.in_(wanted))
    if approval_status:
        valid_appr = {s.value for s in OpportunityApprovalStatus}
        if approval_status not in valid_appr:
            raise HTTPException(status_code=400,
                                detail=f"Invalid approval_status. Allowed: {', '.join(s.value for s in OpportunityApprovalStatus)}")
        stmt = stmt.where(Opportunity.approval_status == approval_status)
    if customer_id is not None:
        stmt = stmt.where(Opportunity.customer_id == customer_id)
    if branch_id is not None:
        stmt = stmt.where(Opportunity.branch_id == branch_id)
    if opp_type:
        # "SOW" = everything that is NOT T&M (Work Package / Fixed Price /
        # Retainer) — the Pipeline T&M / Pipeline SOW workspace split.
        if opp_type == "SOW":
            stmt = stmt.where(Opportunity.opp_type != OppType.T_AND_M)
        else:
            valid_types = {t.value for t in OppType}
            if opp_type not in valid_types:
                raise HTTPException(status_code=400,
                                    detail=f"Invalid opp_type. Allowed: SOW, "
                                           f"{', '.join(sorted(valid_types))}")
            stmt = stmt.where(Opportunity.opp_type == OppType(opp_type))
    if p.search:
        like = f"%{p.search}%"
        # The search box also matches the customer's name (4 Sep 2026): "aptiv"
        # finds every APTIV deal without opening the Customer column filter.
        stmt = stmt.where(or_(Opportunity.title.ilike(like), Opportunity.opp_id.ilike(like),
                              Customer.name.ilike(like)))
    order_col = _SORTABLE.get(p.sort_by or "", Opportunity.id)
    stmt = stmt.order_by(order_col.asc() if p.sort_dir == "asc" else order_col.desc())
    items, meta = paginate(db, stmt, p.page, p.limit)
    return envelope(data=[serialize_opportunity(db, o) for o in items],
                    message="Opportunities fetched", meta=meta)


def _opp_type_value(opp_type) -> str:
    return opp_type.value if hasattr(opp_type, "value") else str(opp_type)


def _replace_ctc_slab(db: Session, opp: Opportunity, rows: list) -> None:
    details = opp.details or {}
    db.query(OpportunityCtcSlab).filter(OpportunityCtcSlab.opportunity_id == opp.id).delete()
    for idx, row in enumerate(rows or []):
        data = derive_ctc_row(
            row.model_dump() if hasattr(row, "model_dump") else dict(row),
            opportunity_type=_opp_type_value(opp.opp_type),
            details=details,
        )
        db.add(OpportunityCtcSlab(opportunity_id=opp.id, position=idx, **data))


_CTC_FIELDS = (
    "exp_min", "exp_max", "target_exp", "rate", "revenue_monthly",
    "revenue_annual", "management_cost_pct", "engineering_budget",
    "hike_pct", "appraisal_cycle", "approved_ctc_lac",
)


def _existing_ctc_rows(db: Session, opportunity_id: int) -> list[dict]:
    rows = db.execute(
        select(OpportunityCtcSlab)
        .where(OpportunityCtcSlab.opportunity_id == opportunity_id)
        .order_by(OpportunityCtcSlab.position, OpportunityCtcSlab.id)
    ).scalars().all()
    return [{field: getattr(row, field) for field in _CTC_FIELDS} for row in rows]


# ------------------------------------------------------------------ audit diff
# The activity log used to record only "Fields updated: details" — useless for
# answering "who changed the CTC and from what to what?". Updates now log a
# field-level OLD → NEW diff (17 Aug 2026) so an accidental edit is traceable
# to a person, a field and both values. Purely descriptive — the log write
# already existed; only the comment got richer.

def _validated_opp_id(db: Session, raw: str | None,
                      exclude_id: int | None = None) -> str | None:
    """Custom opportunity IDs (18 Aug 2026): auto-numbered by default, but the
    user may type their own — the customer's reference often IS the ID people
    search by. Uniqueness is the only hard rule; format is theirs to choose."""
    value = (raw or "").strip()
    if not value:
        return None
    q = select(Opportunity.id).where(Opportunity.opp_id == value)
    if exclude_id is not None:
        q = q.where(Opportunity.id != exclude_id)
    if db.execute(q.limit(1)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=400,
                            detail=f"Opportunity ID '{value}' is already in use")
    return value


_UPDATE_LABELS = {
    "opp_id": "Opportunity ID",
    "title": "Title", "customer_id": "Customer", "branch_id": "Branch",
    "contact_person_id": "Contact person", "hiring_manager_id": "Hiring manager",
    "opp_type": "Type", "pipeline_stage": "Pipeline stage",
    "onboarding_status": "Onboarding status", "rfi_value": "RFI value",
    "rfi_received_date": "RFI received date",
}


def _audit_val(v) -> str:
    """One comparable, human-readable token for a diff line."""
    if v is None or v == "":
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if hasattr(v, "value"):  # Enum
        v = v.value
    if isinstance(v, Decimal):
        v = format(v.normalize(), "f")
    if isinstance(v, float) and v == int(v):
        v = int(v)
    s = str(v)
    return s if len(s) <= 80 else s[:77] + "…"


def _audit_diff_lines(old_top: dict, new_top: dict,
                      old_details: dict, new_details: dict,
                      old_slab: list[dict] | None,
                      new_slab: list[dict] | None) -> list[str]:
    lines: list[str] = []
    for f in sorted(new_top):
        o, n = _audit_val(old_top.get(f)), _audit_val(new_top[f])
        if o != n:
            lines.append(f"{_UPDATE_LABELS.get(f, f.replace('_', ' '))}: {o} → {n}")
    for k in sorted(set(old_details) | set(new_details)):
        o, n = _audit_val(old_details.get(k)), _audit_val(new_details.get(k))
        if o != n:
            lines.append(f"{k.replace('_', ' ')}: {o} → {n}")
    if old_slab is not None and new_slab is not None:
        if len(old_slab) != len(new_slab):
            lines.append(f"CTC Slab: {len(old_slab)} row(s) → {len(new_slab)} row(s)")
        for i, (o_row, n_row) in enumerate(zip(old_slab, new_slab), start=1):
            for f in _CTC_FIELDS:
                o, n = _audit_val(o_row.get(f)), _audit_val(n_row.get(f))
                if o != n:
                    lines.append(f"CTC Slab row {i} {f.replace('_', ' ')}: {o} → {n}")
    return lines


@router.post("")
def create_opportunity(
    payload: OpportunityCreate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(create_opportunities),
):
    validate_refs(db, payload.customer_id, payload.branch_id,
                  payload.contact_person_id, payload.hiring_manager_id)
    # Never trust the client: reject detail fields not valid for this opportunity type.
    try:
        validate_details(payload.details, _opp_type_value(payload.opp_type))
    except OpportunitySchemaError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    opp_type_value = _opp_type_value(payload.opp_type)
    clean_details = strip_details(payload.details, opp_type_value)
    if opp_type_value == "T&M":
        clean_details = normalize_tm_billing_details(clean_details)
    opp = Opportunity(
        opp_id=_validated_opp_id(db, payload.opp_id)
        or next_sequence_number(db, Opportunity, Opportunity.opp_id, "OPP"),
        title=payload.title.strip(),
        customer_id=payload.customer_id,
        branch_id=payload.branch_id,
        contact_person_id=payload.contact_person_id,
        hiring_manager_id=payload.hiring_manager_id,
        opp_type=payload.opp_type,
        rfi_value=payload.rfi_value,
        rfi_received_date=payload.rfi_received_date,
        # The form no longer asks (Aug 2026): every new opportunity starts at
        # Sales Validation. Kept as a payload fallback so API callers that DO
        # send a status still win.
        onboarding_status=payload.onboarding_status or "Sales Validation",
        onboarded_count=payload.onboarded_count or 0,
        details=clean_details or None,
        pipeline_stage=PipelineStage.NEW,
        created_by=user.id,
    )
    # Sales-created opportunities need Sales Head approval; a Sales Head (or Admin)
    # creating one signs off on it immediately.
    privileged = user.has_any("Sales_Head", "Admin")
    if privileged:
        opp.approval_status = OpportunityApprovalStatus.APPROVED
        opp.sales_head_approved_by = user.id
        opp.sales_head_approved_at = _now()
    else:
        opp.approval_status = OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL
    db.add(opp)
    db.flush()
    if payload.skills:
        replace_skills(db, opp, payload.skills)
    if payload.ctc_slab is not None:
        _replace_ctc_slab(db, opp, payload.ctc_slab)
    if privileged:
        log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                     "Created", f"Opportunity {opp.opp_id} created (auto-approved)")
        _spawn_requirement_from_opportunity(db, opp, user)
    else:
        log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                     "Created", f"Opportunity {opp.opp_id} created; submitted for Sales Head approval")
        notify_role(db, "Sales_Head",
                    f"Opportunity {opp.opp_id} awaiting approval",
                    f"'{opp.title}' was created and needs your approval.",
                    f"/opportunities/{opp.id}", exclude_user_id=user.id,
                    event="opportunity.submitted", actor=user)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity created")


@router.get("/next-id")
def preview_next_opp_id(
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    """The ID the next auto-numbered opportunity would take (18 Aug 2026).

    A PREVIEW for the create form, which shows it instead of an empty box.
    Not a reservation: if the user leaves it untouched the form drops it and
    the server numbers the record at save time, so two people creating at
    once can never collide on the previewed value.

    Declared BEFORE `/{opportunity_id}` — literal routes must precede
    parametric ones or FastAPI matches "next-id" as an id.
    """
    return envelope(data={
        "opp_id": next_sequence_number(db, Opportunity, Opportunity.opp_id, "OPP"),
    })


@router.get("/check-id")
def check_opp_id(
    opp_id: str,
    exclude_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    """Live duplicate check for the create form's Opportunity ID field (26 Aug
    2026). Previously a duplicate id surfaced only at SAVE, after the whole
    form was filled — this lets the field warn the moment it is typed.
    Case-insensitive; `exclude_id` lets edit mode skip the record itself.

    Declared BEFORE `/{opportunity_id}` — literal routes must precede
    parametric ones or FastAPI matches "check-id" as an id.
    """
    value = (opp_id or "").strip()
    if not value:
        return envelope(data={"available": True, "existing": None})
    stmt = select(Opportunity).where(func.lower(Opportunity.opp_id) == value.lower())
    if exclude_id is not None:
        stmt = stmt.where(Opportunity.id != exclude_id)
    existing = db.execute(stmt).scalars().first()
    if existing is None:
        return envelope(data={"available": True, "existing": None})
    from models import Customer
    customer = db.get(Customer, existing.customer_id) if existing.customer_id else None
    return envelope(data={
        "available": False,
        "existing": {
            "id": existing.id,
            "opp_id": existing.opp_id,
            "title": existing.title,
            "customer_name": customer.name if customer else None,
        },
    })


@router.get("/{opportunity_id}/suggested-candidates")
def suggested_candidates(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    """Database scan: who already fits this position?

    Deterministic scoring over skills / experience band / pipeline history /
    contactability — see services/candidate_match.py for the exact weights.
    Candidates already applied here are excluded; ones currently
    Joined/Preboarding elsewhere come back flagged ``engaged``.
    """
    from services.candidate_match import suggest_candidates

    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return envelope(data=suggest_candidates(db, opp),
                    message="Suggested candidates")


class EmailCandidatesIn(BaseModel):
    candidate_ids: list[int]
    subject: str | None = None
    message: str | None = None
    log_outreach: bool = True


def _real_candidate_email(email: str | None) -> str:
    """Blank out empty and synthesized import placeholders (@import.karnex.in)."""
    e = (email or "").strip()
    if not e or "@import.karnex.in" in e.lower():
        return ""
    return e


def _candidate_name(c) -> str:
    """"First Last" from the Candidate row. NOTE: there is no `name` column."""
    return " ".join(
        p for p in (getattr(c, "first_name", None), getattr(c, "last_name", None)) if p
    ).strip()


#: The editable hiring-interest template. Placeholders are rendered PER CANDIDATE
#: at send time, so a recruiter can edit the wording in the composer and a bulk
#: send still greets each person by their own name. Served to the UI by
#: GET /{id}/candidate-email-template so there is exactly one copy of this text.
EMAIL_PLACEHOLDERS = ("first_name", "full_name", "role", "customer", "sender")

CANDIDATE_EMAIL_SUBJECT = "Exciting opportunity: {{role}}"

CANDIDATE_EMAIL_BODY = (
    "Hi {{first_name}},\n\n"
    "We're currently hiring for a {{role}} role, and your profile looks "
    "like a strong match. Would you be interested in exploring this opportunity?\n\n"
    "If yes, simply reply to this email and our team will share the details and "
    "the next steps. If the timing isn't right, a quick 'not interested' reply "
    "also helps us — no problem at all.\n\n"
    "Looking forward to hearing from you.\n\n"
    "Best regards,\n{{sender}}\nKarnex Talent Team"
)


#: One pass, so a substituted value is never itself re-scanned. That matters:
#: candidate names arrive from the PUBLIC apply form, and a two-pass renderer
#: would let a candidate called "{{sender}}" be greeted with the recruiter's
#: name. `\w+` also means a stray brace can never match a token.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def _render_email_template(text: str, ctx: dict[str, str]) -> str:
    """Substitute {{token}} placeholders in a single left-to-right pass.

    Never str.format — same reasoning as the notification templates in
    migration 0071: a typo'd or unknown token must render literally rather than
    raise, because the alternative is silently dropping a candidate's email over
    a stray brace. Tolerates any inner spacing and any case.
    """
    def sub(m: "re.Match[str]") -> str:
        key = m.group(1).lower()
        # Unknown token → leave the original text untouched, so the recruiter
        # sees their typo in the sent mail instead of a mystery blank.
        return ctx.get(key, m.group(0)) if key in EMAIL_PLACEHOLDERS else m.group(0)

    return _PLACEHOLDER_RE.sub(sub, text or "")


@router.get("/{opportunity_id}/candidate-email-template")
def candidate_email_template(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("opportunities", "TA", "Sales", "Sales_Head", "RMG")),
):
    """The default hiring-interest subject/body, placeholders intact.

    The composer prefills from this so the recruiter edits real text instead of
    an empty box — and so the wording lives in one place instead of being
    duplicated into the frontend.
    """
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    # Admin-authored default (Settings → Email Drafts, 28 Aug 2026): the
    # composer prefill honours it; {{placeholders}} still render per candidate.
    from services.candidate_comms import candidate_template_override
    subj, body_txt, _ = candidate_template_override(
        db, "candidate.hiring_interest", CANDIDATE_EMAIL_SUBJECT, CANDIDATE_EMAIL_BODY, {})
    return envelope(data={
        "subject": subj,
        "message": body_txt,
        "placeholders": list(EMAIL_PLACEHOLDERS),
        "sample": {
            "role": opp.title or "an open position",
            "customer": _opportunity_customer_name(db, opp),
            "sender": user.full_name or user.username,
        },
    }, message="Candidate email template")


def _opportunity_customer_name(db: Session, opp) -> str:
    from models import Customer
    if not getattr(opp, "customer_id", None):
        return ""
    row = db.execute(select(Customer.name).where(Customer.id == opp.customer_id)).first()
    return (row[0] if row else "") or ""


@router.post("/{opportunity_id}/email-candidates")
def email_candidates(
    opportunity_id: int,
    payload: EmailCandidatesIn,
    db: Session = Depends(get_crm_db),
    # Template-governed + admin-editable at runtime (Users → Action Permissions).
    user: CurrentUser = Depends(
        gated_write_action("candidate.email", "candidates", "TA", "Sales", "Sales_Head", "RMG")
    ),
):
    """Send a hiring-interest email to a batch of candidates for this opportunity.

    Powers the Suggested Candidates "Email selected" action: TA ticks candidates
    and sends one "we're hiring for this role — are you interested?" email to all
    of them. Emails are QUEUED on the durable outbox (five retries + an audit row
    in the Email Outbox screen) and only go out when this request commits.

    `subject`/`message` are the recruiter's edited template. Both are rendered
    PER CANDIDATE through `_render_email_template`, so an edited bulk message
    still greets each person by their own name. Blank falls back to the default
    template — the same text `GET /{id}/candidate-email-template` serves.
    """
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    ids = sorted({int(cid) for cid in payload.candidate_ids})
    if not ids:
        raise HTTPException(status_code=400, detail="Select at least one candidate")
    if len(ids) > 100:
        raise HTTPException(status_code=400, detail="Too many candidates in one send (max 100)")

    # Only candidates this opportunity actually suggested may be mailed.
    # Without this the endpoint is a bulk mailer over the whole candidate table:
    # `candidate_ids` is client-supplied, so is the body, and any TA could
    # enumerate ids 100 at a time and send arbitrary text from the company
    # address. The UI can only ever tick rows the matcher returned, so this
    # costs a legitimate caller nothing.
    from services.candidate_match import suggest_candidates

    allowed = {int(s["candidate_id"]) for s in suggest_candidates(db, opp)}
    outside = [i for i in ids if i not in allowed]
    if outside:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(outside)} candidate(s) are not suggested for this opportunity "
                "and cannot be emailed from here. Refresh the list and try again."
            ),
        )

    candidates = db.execute(select(Candidate).where(Candidate.id.in_(ids))).scalars().all()
    found = {c.id for c in candidates}
    not_found = [i for i in ids if i not in found]
    role_title = opp.title or "an open position"
    sender = user.full_name or user.username
    customer_name = _opportunity_customer_name(db, opp)
    # A newline in a Subject makes email_smtp raise HeaderParseError at DRAIN
    # time, so the outbox row would burn all five retries and die silently.
    # Reachable through the API even though the UI uses a single-line <input>.
    subject_tpl = " ".join((payload.subject or "").split()) or CANDIDATE_EMAIL_SUBJECT
    body_tpl = (payload.message or "").strip() or CANDIDATE_EMAIL_BODY

    from services.candidate_comms import send_candidate_email

    sent = 0
    skipped: list[dict] = []
    failed: list[dict] = []
    for c in candidates:
        to = _real_candidate_email(getattr(c, "email", None))
        name = _candidate_name(c)
        first = name.split(" ")[0] if name else "there"
        if not to:
            skipped.append({"candidate_id": c.id, "name": name or f"#{c.id}", "reason": "no_email"})
            continue
        ctx = {
            "first_name": first,
            "full_name": name or "there",
            "role": role_title,
            "customer": customer_name,
            "sender": sender,
        }
        subject = _render_email_template(subject_tpl, ctx)
        body_text = _render_email_template(body_tpl, ctx)
        res = send_candidate_email(
            to, subject, body_text, db=db,
            event="candidate.hiring_interest", actor=user, to_name=name,
            candidate_id=c.id,
        )
        if res.get("sent"):
            sent += 1
            if payload.log_outreach:
                db.add(CandidateOutreach(
                    candidate_id=c.id, requirement_id=None, channel="Email",
                    note=f"Hiring-interest email — {role_title}", outcome="Sent",
                    user_id=user.id,
                ))
        else:
            failed.append({"candidate_id": c.id, "name": name or f"#{c.id}",
                           "reason": res.get("error") or "send_failed"})
    db.commit()
    parts = [f"Queued {sent} email(s)"]
    if skipped:
        parts.append(f"{len(skipped)} skipped (no email)")
    if failed:
        parts.append(f"{len(failed)} failed")
    # Report ids that matched no candidate row explicitly. Silently dropping them
    # makes `requested` disagree with sent+skipped+failed and leaves the sender
    # believing a mail went out that never did.
    if not_found:
        parts.append(f"{len(not_found)} not found")
    return envelope(
        data={"sent": sent, "skipped": skipped, "failed": failed,
              "not_found": not_found, "requested": len(ids)},
        message=", ".join(parts),
    )


@router.get("/{opportunity_id}")
def get_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity fetched")


@router.put("/{opportunity_id}")
def update_opportunity(
    opportunity_id: int,
    payload: OpportunityUpdate,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    changes = payload.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    # Every type-specific detail travels inside the `details` JSONB, so the
    # single "details" registry key governs the whole schema-driven form body.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), "opportunities", changes, {
        "title": "title", "customer_id": "customer_id", "branch_id": "branch_id",
        "opp_type": "opp_type", "pipeline_stage": "pipeline_stage",
        "onboarding_status": "onboarding_status",
        "rfi_value": "rfi_value", "rfi_received_date": "rfi_received_date",
        "details": "details", "ctc_slab": "ctc_slab",
    })
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Optimistic concurrency: reject a stale write instead of clobbering a newer one.
    client_version = changes.pop("version", None)
    if client_version is not None and client_version != opp.version:
        raise HTTPException(
            status_code=409,
            detail="This opportunity was changed by someone else. Reload and re-apply your edits.",
        )

    # Custom-ID edit: blank means "keep as is"; a new value must be unused.
    if "opp_id" in changes:
        changes["opp_id"] = _validated_opp_id(
            db, changes["opp_id"], exclude_id=opp.id) or opp.opp_id

    # Audit snapshots BEFORE any mutation — the log compares against these.
    audit_top_old = {f: getattr(opp, f) for f in changes
                     if f not in ("details", "ctc_slab") and hasattr(opp, f)}
    audit_details_old = dict(opp.details or {})
    audit_slab_old = _existing_ctc_rows(db, opp.id)

    target_customer = changes.get("customer_id", opp.customer_id)
    validate_refs(
        db,
        target_customer,
        changes.get("branch_id", opp.branch_id),
        changes.get("contact_person_id", opp.contact_person_id),
        changes.get("hiring_manager_id", opp.hiring_manager_id),
    )
    if "title" in changes and changes["title"] is not None:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise HTTPException(status_code=400, detail="Title cannot be empty")

    # Effective type after this update (may be unchanged).
    current_type = _opp_type_value(opp.opp_type)
    eff_type = _opp_type_value(changes.get("opp_type", opp.opp_type))
    type_changed = eff_type != current_type

    # details: MERGE a partial payload into the stored object (never null unsent keys),
    # then validate + strip against the effective type.
    incoming_details = changes.pop("details", None)
    details_changed = incoming_details is not None or "opp_type" in changes
    if incoming_details is not None:
        # A type switch starts a fresh type-specific detail set so hidden T&M
        # billing inputs can never fail validation or leak into another branch.
        merged = {} if type_changed else dict(opp.details or {})
        merged.update(incoming_details)
        try:
            validate_details(merged, eff_type)
        except OpportunitySchemaError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        clean_merged = strip_details(merged, eff_type)
        if eff_type == "T&M":
            clean_merged = normalize_tm_billing_details(clean_merged)
        opp.details = clean_merged or None
    elif details_changed and eff_type == "T&M":
        opp.details = normalize_tm_billing_details(strip_details(opp.details, eff_type))
    elif details_changed:
        opp.details = strip_details(opp.details, eff_type) or None

    ctc_provided = "ctc_slab" in changes
    changes.pop("ctc_slab", None)

    for field, value in changes.items():
        setattr(opp, field, value)
    if ctc_provided:
        _replace_ctc_slab(db, opp, payload.ctc_slab or [])
    elif details_changed:
        # Opportunity-type and source-detail changes refresh the complete chain.
        _replace_ctc_slab(db, opp, _existing_ctc_rows(db, opp.id))
    opp.version = (opp.version or 1) + 1  # bump for the next optimistic-concurrency check

    # Keep the linked requirement (seen by RMG/TA) in sync with the opportunity's
    # title, customer and carried details — they're denormalized copies.
    sync_requirement_from_opportunity(db, opp)

    # Field-level OLD → NEW audit trail. Slab rows are diffed only when the
    # save touched them (directly or via a details re-derivation) — flush so
    # the freshly replaced rows are queryable.
    db.flush()
    slab_touched = ctc_provided or details_changed
    audit_lines = _audit_diff_lines(
        audit_top_old,
        {f: getattr(opp, f) for f in audit_top_old},
        audit_details_old, dict(opp.details or {}),
        audit_slab_old if slab_touched else None,
        _existing_ctc_rows(db, opp.id) if slab_touched else None,
    )
    comment = "\n".join(audit_lines) if audit_lines else (
        f"Saved with no visible change (fields sent: "
        f"{', '.join(sorted(changes.keys())) or 'ctc_slab'})"
    )
    if len(comment) > 1800:
        comment = comment[:1797] + "…"
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Updated", comment)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity updated")


@router.delete("/{opportunity_id}")
def delete_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    from models import Project
    from services.crm_common import commit_or_conflict
    from services.crm_delete import hard_delete_opportunities

    opp = get_opportunity_or_404(db, opportunity_id)
    # Projects carry finance/timesheet history — keep as hard blocker with counts.
    proj_n = db.execute(
        select(func.count()).select_from(Project).where(Project.opportunity_id == opportunity_id)
    ).scalar() or 0
    if proj_n:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot delete: {proj_n} project(s) exist. "
                "Delete or reassign those projects first."
            ),
        )
    # Everything recruiting-side goes with it (requirement, applicant profiles,
    # resumes, rounds, AI links, slots, attachments, CTC slab) — explicit
    # FK-ordered SQL; the ORM cascade missed imported resumes and 409'd.
    hard_delete_opportunities(db, [opportunity_id])
    commit_or_conflict(
        db,
        "Cannot delete: opportunity is still referenced by other records. Remove dependencies first.",
    )
    return envelope(data={"id": opportunity_id}, message="Opportunity deleted")


# ---------------------------------------------------------------------------
# Sales Head approval workflow
# ---------------------------------------------------------------------------

def _require_approval_status(opp: Opportunity, allowed: tuple, action: str) -> None:
    current = opp.approval_status.value if hasattr(opp.approval_status, "value") else str(opp.approval_status)
    allowed_vals = tuple(s.value for s in allowed)
    if current not in allowed_vals:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {action} an opportunity with approval status '{current}'. "
                   f"Allowed: {', '.join(allowed_vals)}",
        )


@router.post("/{opportunity_id}/approve")
def approve_opportunity(
    opportunity_id: int,
    payload: CommentIn | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    _require_approval_status(opp, (OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL,), "approve")
    opp.approval_status = OpportunityApprovalStatus.APPROVED
    opp.sales_head_approved_by = user.id
    opp.sales_head_approved_at = _now()
    opp.approval_rejection_reason = None
    comment = (payload.comment if payload else None) or None
    # NN customers (18 Aug 2026): a brand-new-relationship deal isn't won on
    # approval — it enters BIDDING. Sales Head's approve moves an NN
    # opportunity's onboarding status to "Sales Bidding" automatically
    # (reject stays reject). The deal's own customer_type detail wins; the
    # customer master is the fallback.
    ctype = str((opp.details or {}).get("customer_type") or "").strip().upper()
    if not ctype:
        from models import Customer
        cust = db.get(Customer, opp.customer_id)
        ctype = str(getattr(cust, "customer_type", "") or "").strip().upper()
    if ctype == "NN":
        opp.onboarding_status = "Sales Bidding"
        comment = (comment + " — " if comment else "") + "NN customer → moved to Sales Bidding"
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Approved", comment or "Approved by Sales Head")
    # Bridge into the Requirements chain → RMG (Engineering Review) → TA.
    spawned = _spawn_requirement_from_opportunity(db, opp, user)
    if opp.created_by != user.id:
        notify_user(db, opp.created_by,
                    f"Opportunity {opp.opp_id} approved",
                    "Approved by Sales Head; sent to engineering review."
                    if spawned else "Approved by Sales Head.",
                    f"/opportunities/{opp.id}", actor=user)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity approved")


@router.post("/{opportunity_id}/reject")
def reject_opportunity(
    opportunity_id: int,
    payload: RejectIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("opportunities", "Sales_Head")),
):
    try:
        reason = payload.validated_reason()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    opp = get_opportunity_or_404(db, opportunity_id)
    _require_approval_status(opp, (OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL,), "reject")
    opp.approval_status = OpportunityApprovalStatus.REJECTED
    opp.approval_rejection_reason = reason
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Rejected", reason)
    if opp.created_by != user.id:
        notify_user(db, opp.created_by,
                    f"Opportunity {opp.opp_id} rejected by Sales Head",
                    reason, f"/opportunities/{opp.id}", actor=user)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity rejected")


@router.post("/{opportunity_id}/resubmit")
def resubmit_opportunity(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    if not user.is_admin and opp.created_by != user.id:
        raise HTTPException(status_code=403, detail="Only the creator (or Admin) can resubmit this opportunity")
    _require_approval_status(opp, (OpportunityApprovalStatus.REJECTED,), "resubmit")
    opp.approval_status = OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL
    opp.approval_rejection_reason = None
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Resubmitted", "Resubmitted for Sales Head approval")
    notify_role(db, "Sales_Head",
                f"Opportunity {opp.opp_id} resubmitted for approval",
                f"'{opp.title}' was resubmitted and needs your approval.",
                f"/opportunities/{opp.id}", exclude_user_id=user.id,
                event="opportunity.submitted", actor=user)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True), message="Opportunity resubmitted for approval")


# ---------------------------------------------------------------------------
# Stage transition (server-side validated state machine)
# ---------------------------------------------------------------------------

#: Stages where the deal is over — its applicant profiles leave the lists.
_CLOSED_STAGES = {
    PipelineStage.CLOSED_WON.value, PipelineStage.CLOSED_LOST.value,
    PipelineStage.CLOSED_PARTIAL.value, PipelineStage.REJECTED.value, PipelineStage.ARCHIVED.value,
}


def _set_profiles_hidden(db: Session, opportunity_id: int, hidden: bool) -> int:
    """Hide (or restore) every candidate profile on the opportunity. Returns
    the number of rows that actually changed. Nothing is deleted."""
    from models import CandidateProfile
    rows = db.execute(
        select(CandidateProfile).where(CandidateProfile.opportunity_id == opportunity_id,
                                       CandidateProfile.is_hidden.is_(not hidden))
    ).scalars().all()
    for prof in rows:
        prof.is_hidden = hidden
    return len(rows)


@router.post("/{opportunity_id}/stage-transition")
def stage_transition(
    opportunity_id: int,
    payload: StageTransitionIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    new_stage = (payload.new_stage or "").strip()
    validate_stage_transition(opp.pipeline_stage, new_stage)
    if new_stage == PipelineStage.ARCHIVED.value and not user.has_any("Sales_Head", "Admin"):
        raise HTTPException(status_code=403,
                            detail="Only Sales_Head or Admin can archive an opportunity")
    old_stage = opp.pipeline_stage.value if hasattr(opp.pipeline_stage, "value") else str(opp.pipeline_stage)
    opp.pipeline_stage = PipelineStage(new_stage)
    comment = f"Stage changed: {old_stage} -> {new_stage}"
    if payload.comment and payload.comment.strip():
        comment += f" | {payload.comment.strip()}"
    # Closing a deal takes its applicants off the Candidate Profiles tab for
    # every role (8 Sep 2026, user request) — hidden, never deleted: the
    # candidates stay on the Candidates tab with their history, and reopening
    # the deal brings the profiles back.
    hidden = _set_profiles_hidden(db, opp.id, new_stage in _CLOSED_STAGES)
    if hidden:
        comment += f" | {hidden} candidate profile(s) {'hidden' if new_stage in _CLOSED_STAGES else 'restored'}"
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Stage_Transition", comment)
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_opportunity(db, opp, detail=True),
                    message=f"Opportunity moved to {new_stage}")


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@router.get("/{opportunity_id}/activity-log")
def get_activity_log(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(read_opportunities),
):
    get_opportunity_or_404(db, opportunity_id)
    return envelope(data=fetch_activity_log(db, opportunity_id), message="Activity log fetched")


# ---------------------------------------------------------------------------
# Skills (replace the whole set)
# ---------------------------------------------------------------------------

@router.post("/{opportunity_id}/skills")
def set_skills(
    opportunity_id: int,
    payload: list[OpportunitySkillIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(write_opportunities),
):
    opp = get_opportunity_or_404(db, opportunity_id)
    replace_skills(db, opp, payload)
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Skills_Updated", f"Skill set replaced ({len(payload)} skill(s))")
    db.commit()
    db.refresh(opp)
    return envelope(data=serialize_skills(db, opp), message="Skills updated")
