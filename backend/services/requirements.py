"""Requirement workflow services: visibility rules, serialization, fulfilment check."""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import Select, and_, or_, select
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
#: What TA WORKS ON — the default list. A held requirement stays in (the badge
#: and reason are shown, sourcing actions are locked) because hiding it makes
#: TAs think it vanished.
TA_LIVE_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
    RequirementStatus.ON_HOLD,
    RequirementStatus.FULFILLED,
)

#: Settled work TA may LOOK UP but never sees by default (22 Sep 2026). Closing
#: a deal has to take its requirement out of the working queue — that was the
#: bug — but "why did this disappear?" still needs an answer, so these remain
#: reachable behind an explicit status filter.
TA_ARCHIVE_STATUSES = (
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)

#: Everything TA may see at all. Used by `ensure_visible`, so a deep link or a
#: bell notification to a closed requirement opens instead of 404-ing.
TA_VISIBLE_STATUSES = TA_LIVE_STATUSES + TA_ARCHIVE_STATUSES

#: The statuses where a candidate can be PUT FORWARD (29 Sep 2026, user report:
#: TA's "Apply to Opportunity" listed C-2026-00099 / 00098, which neither the
#: Sales Head nor RMG had approved). Engineering-approved, not held, not settled.
SOURCING_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)

#: Held-from values that mean "never reached sourcing". Since 29 Sep 2026 the
#: opportunity-stage cascade also pauses a requirement still waiting for an
#: approval; TA must not see those just because they now read On_Hold.
_PRE_SOURCING_HELD_FROM = (
    "Draft", "Pending_Sales_Head_Approval", "Sales_Head_Rejected",
    "Pending_Engineering_Review", "Engineering_Rejected",
)


def ta_visible_clause():
    """SQL twin of `ta_may_see` — TA's statuses, minus a hold that never sourced."""
    return and_(
        Requirement.status.in_(TA_VISIBLE_STATUSES),
        or_(Requirement.status != RequirementStatus.ON_HOLD,
            Requirement.held_from_status.is_(None),
            Requirement.held_from_status.notin_(_PRE_SOURCING_HELD_FROM)),
    )


def ta_may_see(req) -> bool:
    if req.status not in TA_VISIBLE_STATUSES:
        return False
    return not (req.status == RequirementStatus.ON_HOLD
                and (req.held_from_status or "") in _PRE_SOURCING_HELD_FROM)


def sourcing_opportunity_clause():
    """`Opportunity.id IN (…)` — deals open for applications right now: a live
    stage (New / Active) AND a requirement in `SOURCING_STATUSES`. Used by the
    Apply-to-Opportunity picker and enforced on `POST /api/candidate-profiles`
    for a TA."""
    from models import Opportunity, PipelineStage
    return and_(
        Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE)),
        Opportunity.id.in_(
            select(Requirement.opportunity_id)
            .where(Requirement.status.in_(SOURCING_STATUSES))),
    )


#: Requirement statuses that mean "an approval is still owed" — Sales Head
#: (requirement-level) or RMG. The Opportunities "Pending Approval" tab.
AWAITING_APPROVAL_STATUSES = (
    RequirementStatus.PENDING_SALES_HEAD_APPROVAL,
    RequirementStatus.PENDING_ENGINEERING_REVIEW,
)


def awaiting_approval_clause():
    """Deals still waiting on ANY approval (5 Oct 2026, user report: two positions
    waiting for RMG approval sat under Active while Pending Approval was empty):
    the opportunity itself awaits the Sales Head, OR it is approved and live
    (New / Active) with a requirement awaiting the Sales Head / RMG. Negate it
    for the stage tabs so every deal lives in exactly one tab."""
    from models import Opportunity, OpportunityApprovalStatus, PipelineStage
    return or_(
        Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL,
        and_(
            Opportunity.approval_status == OpportunityApprovalStatus.APPROVED,
            Opportunity.pipeline_stage.in_((PipelineStage.NEW, PipelineStage.ACTIVE)),
            Opportunity.id.in_(
                select(Requirement.opportunity_id)
                .where(Requirement.status.in_(AWAITING_APPROVAL_STATUSES))),
        ),
    )


def recruiter_only(user) -> bool:
    """TA with no role that may raise, approve or screen work — the login the
    sourcing-only rule applies to. Sales / RMG / heads / admins keep their
    wider reach (they may apply to their own deal before it is published)."""
    if getattr(user, "is_admin", False):
        return False
    return "TA" in user.roles and not user.has_any("Sales", "Sales_Head", "RMG")
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


#: The two roles whose requirement list is SCOPED (own deals / sourcing statuses)
#: rather than everything. Anyone else who is let in sees the whole list, like RMG.
SCOPED_ROLES = ("Sales", "TA")
#: Access-Template / custom-role tabs that admit a user to requirements.
REQUIREMENT_TABS = ("requirements", "opportunities")


def sees_all_requirements(db: Session | None, user: CurrentUser) -> bool:
    """RMG / Sales Head / Admin by ROLE — or anyone whose Access Template or
    custom role GRANTS the requirements / opportunities tab (26 Sep 2026: the
    GM). Templates are authoritative at the gate; the Sales-own / TA-status
    scoping below is a business rule for those two roles only, so a user
    admitted by a grant who holds neither is treated like RMG. Reported: the
    GM's Opportunities tab answered 403 "Your role cannot view requirements".
    `db=None` keeps the pure role rule for callers without a session."""
    if user.has_any(*SEE_ALL_ROLES):
        return True
    # A manager rung (Sales Manager) sees the whole team's deals (29 Sep 2026).
    from services.role_implications import sees_team
    if sees_team(user.roles):
        return True
    if db is None or user.has_any(*SCOPED_ROLES):
        return False
    from services.access_templates import effective_access
    acc = effective_access(db, user.id, set(user.roles))
    if acc.get("full"):
        return True
    tabs = acc.get("tabs") or {}
    return any(tabs.get(t) for t in REQUIREMENT_TABS)


def apply_visibility(stmt: Select, user: CurrentUser,
                     requested_status: str | None = None, *,
                     db: Session | None = None) -> Select:
    """Restrict a select(Requirement) statement to what this user may see (403 if nothing).

    TA sees every status they are entitled to (`TA_VISIBLE_STATUSES`), settled
    work included. **The default view is narrowed by the TAB, not here**
    (22 Sep 2026): hiding rows inside the visibility layer made "All statuses"
    quietly not mean all, which is exactly what the user reported. Scoping
    belongs where the user can see and change it.

    `requested_status` is accepted and ignored — kept so the router's call site
    stays honest about what it passes. `db` lets a template / custom-role grant
    count (`sees_all_requirements`).
    """
    if sees_all_requirements(db, user):
        return stmt
    conditions = []
    if "Sales" in user.roles:
        conditions.append(Requirement.created_by == user.id)
    if "TA" in user.roles:
        conditions.append(ta_visible_clause())
    if not conditions:
        raise HTTPException(status_code=403, detail="Your role cannot view requirements")
    return stmt.where(or_(*conditions))


def ensure_visible(user: CurrentUser, req: Requirement, *, db: Session | None = None) -> None:
    """May this user OPEN one requirement? 404 (not 403) so existence is never leaked.

    ⚠️ Sales opens ANY requirement (29 Sep 2026, user report: a Sales user's
    bell / Applied-Candidates link answered "Requirement not found" because a
    colleague had raised the deal). Every Sales user already opens every
    opportunity (`GET /api/opportunities` is not creator-scoped) and the
    requirement is that opportunity's sourcing record — the same rule the
    positions panel follows (`requirement_positions._visible_requirement`).
    Only the Sales LIST stays scoped to their own deals (`apply_visibility`);
    writes keep their own gates (creator-only edit, approvals)."""
    if sees_all_requirements(db, user):
        return
    if not user.has_any(*SCOPED_ROLES):
        raise HTTPException(status_code=403, detail="Your role cannot view requirements")
    if "Sales" in user.roles:
        return
    if "TA" in user.roles and ta_may_see(req):
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


#: Deal stages that OVERRIDE the sourcing status in the UI badge.
#:
#: Reported 22 Sep 2026: Sales set "Close Lost" and TA's screen said "Cancelled"
#: — two vocabularies for one fact. When the DEAL is settled or parked, that is
#: the fact that matters to everyone, so every role reads the Sales wording.
#: While the deal is live (New / Active) the sourcing status is what TA needs
#: ("Open For Sourcing" vs "Pending Engineering Review" is their whole day), so
#: the requirement's own status still wins there.
STAGE_OVERRIDES_REQUIREMENT_BADGE = frozenset({
    "On_Hold", "Sales_Hold",
    "Closed_Won", "Closed_Lost", "Closed_Partial", "Rejected", "Archived",
})


def display_status_for(req_status: str | None, opp_stage: str | None) -> str:
    """The ONE status string every role shows for a piece of work.

    Pure — no DB — so the rule is testable and the frontend can mirror it
    without a second source of truth.
    """
    if opp_stage and opp_stage in STAGE_OVERRIDES_REQUIREMENT_BADGE:
        return opp_stage
    return req_status or ""


def serialize_requirement(req: Requirement, skills: list[dict] | None = None) -> dict:
    opp_stage = _val(getattr(getattr(req, "opportunity", None), "pipeline_stage", None))
    return {
        "id": req.id,
        "req_number": req.req_number,
        "opportunity_id": req.opportunity_id,
        # One ID across roles (18 Aug 2026): the parent opportunity's public
        # ID travels with every requirement so the number Sales quoted is the
        # number RMG/TA see and search.
        "opportunity_opp_id": getattr(req.opportunity, "opp_id", None),
        # The parent deal's stage travels with the requirement so no role has to
        # guess why sourcing stopped (22 Sep 2026).
        "opportunity_stage": opp_stage,
        # What the UI badges. See `display_status_for` for the precedence.
        "display_status": display_status_for(_val(req.status), opp_stage),
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


# --------------------------------------------------------------- positions (21 Sep 2026)

#: Statuses in which a headcount change may be requested at all. Closed and
#: Cancelled are deliberate human decisions — reopen the requirement first.
#: Fulfilled IS allowed: raising the target is exactly how you reopen it.
POSITION_CHANGE_BLOCKED_STATUSES = (
    RequirementStatus.CLOSED,
    RequirementStatus.CANCELLED,
)
#: After an approved INCREASE, a Fulfilled requirement goes back to sourcing.
#: (Fulfilled is derived state — `check_and_mark_fulfilled` set it — so it is
#: ours to undo; Closed/Cancelled are not, which is why they are blocked above.)
POSITION_REOPEN_STATUS = RequirementStatus.IN_PROGRESS


def joined_counts_for_opportunities(db: Session, opportunity_ids) -> dict[int, int]:
    """{opportunity_id: Joined profile count} in ONE query (list endpoints)."""
    ids = [int(i) for i in set(opportunity_ids or ()) if i]
    if not ids:
        return {}
    rows = db.execute(
        select(CandidateProfile.opportunity_id, sa.func.count())
        .where(CandidateProfile.opportunity_id.in_(ids),
               CandidateProfile.pipeline_status == PipelineStatus.JOINED)
        .group_by(CandidateProfile.opportunity_id)
    ).all()
    return {int(r[0]): int(r[1]) for r in rows}


def joined_count(db: Session, req: Requirement) -> int:
    """Joined candidates counting against this requirement's headcount."""
    return int(joined_counts_for_opportunities(db, [req.opportunity_id]).get(req.opportunity_id, 0))


def positions_summary(total, joined: int) -> dict:
    """The three numbers every screen shows: total · filled · still open."""
    total = int(total or 1)
    joined = int(joined or 0)
    return {
        "positions_total": total,
        "positions_joined": joined,
        "positions_open": max(0, total - joined),
    }


def positions_by_opportunity(db: Session, opportunity_ids) -> dict[int, dict]:
    """Per-opportunity headcount for the Opportunities list — three queries in
    total (requirements, Joined counts, pending change requests), never per row.

    An opportunity with no requirement yet (still awaiting approval) is absent
    from the result: the caller renders "—" rather than inventing a zero.
    """
    from models import RequirementPositionRequest

    ids = [int(i) for i in set(opportunity_ids or ()) if i]
    if not ids:
        return {}
    from models import Location, Opportunity
    reqs = db.execute(
        select(Requirement.id, Requirement.opportunity_id, Requirement.no_of_positions,
               Requirement.status, Requirement.priority, Requirement.experience_min,
               Requirement.experience_max, Requirement.budget_ctc_min, Requirement.budget_ctc_max,
               Requirement.target_closure_date, Requirement.work_mode, Requirement.location_id,
               Opportunity.pipeline_stage)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .where(Requirement.opportunity_id.in_(ids))
    ).all()
    if not reqs:
        return {}
    from services.requirement_assignments import assignments_by_requirement
    assigned = assignments_by_requirement(db, [r[0] for r in reqs])
    loc_ids = {r[11] for r in reqs if r[11]}
    loc_names = {
        lid: ", ".join(p for p in (city, state) if p) or None
        for lid, city, state in (db.execute(
            select(Location.id, Location.city, Location.state).where(Location.id.in_(loc_ids))
        ).all() if loc_ids else [])
    }
    joined_by_opp = joined_counts_for_opportunities(db, [r[1] for r in reqs])
    pending = {
        int(r[0]) for r in db.execute(
            select(RequirementPositionRequest.requirement_id)
            .where(RequirementPositionRequest.requirement_id.in_([r[0] for r in reqs]),
                   RequirementPositionRequest.status == "Pending")
        ).all()
    }
    out: dict[int, dict] = {}
    for (req_id, opp_id, total, status, priority, exp_min, exp_max, bud_min, bud_max,
         target, work_mode, loc_id, opp_stage) in reqs:
        # One opportunity spawns exactly one requirement, but be defensive:
        # if a second ever exists, the positions add up rather than overwrite.
        summary = positions_summary(total, joined_by_opp.get(int(opp_id), 0))
        prev = out.get(int(opp_id))
        if prev:
            summary = positions_summary(
                prev["positions_total"] + summary["positions_total"], prev["positions_joined"])
        out[int(opp_id)] = {
            **summary,
            "requirement_id": int(req_id),
            "requirement_status": _val(status),
            # The position's urgency + the TAs working it (1 Oct 2026) — the
            # opportunity page prints both for RMG / GM, who never open the
            # requirement page.
            "requirement_priority": _val(priority),
            "assigned_tas": assigned.get(int(req_id), []),
            # The position row's facts (1 Oct 2026): the Opportunities list
            # prints the same row as TA's list for every role — band, budget,
            # work mode · location, target date and the ONE status wording
            # (`display_status_for`: Sales words once the deal is settled).
            "requirement_experience_min": float(exp_min) if exp_min is not None else None,
            "requirement_experience_max": float(exp_max) if exp_max is not None else None,
            "requirement_budget_ctc_min": float(bud_min) if bud_min is not None else None,
            "requirement_budget_ctc_max": float(bud_max) if bud_max is not None else None,
            "requirement_target_closure_date": target.isoformat() if target else None,
            "requirement_work_mode": _val(work_mode),
            "requirement_location_name": loc_names.get(loc_id),
            "requirement_display_status": display_status_for(_val(status), _val(opp_stage)),
            "positions_change_pending": int(req_id) in pending or bool(prev and prev.get("positions_change_pending")),
        }
    return out
