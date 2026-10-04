"""Requirement headcount change requests (21 Sep 2026, user flow).

`requirements.no_of_positions` is the sourcing target: TA sources against it
and `check_and_mark_fulfilled` measures Joined candidates against it. Sales
owns the customer conversation that changes it, but RMG owns delivery, so the
number never moves silently:

  GET  /api/requirements/position-requests/pending          RMG's queue (all requirements)
  GET  /api/requirements/{id}/position-requests             this requirement's history
  POST /api/requirements/{id}/position-requests             Sales / Sales Head ask, WITH a reason
  POST /api/requirements/{id}/position-requests/{prid}/approve   RMG (Admin/CEO always)
  POST /api/requirements/{id}/position-requests/{prid}/reject    same approvers, note required
  POST /api/requirements/{id}/position-requests/{prid}/cancel    the requester withdraws

Everything the USER sees says "opportunity", never "requirement": Sales has no
Requirements page (the sub-tab was removed Aug 2026) and reaches this from the
opportunity, so the internal noun must not leak into the copy.

Rules the sourcing target depends on:
  * one pending request per requirement (DB partial index + a checked read);
  * a requester never approves their own — except Admin/CEO, the escalation
    path, whose own requests apply at once but are still logged and announced;
  * the count may never drop below candidates already Joined — checked when
    the request is made AND again at approval, because people join in between;
  * Closed / Cancelled requirements are refused (those are human decisions —
    reopen the requirement itself first). Fulfilled is allowed: an approved
    INCREASE puts it back to In Progress, which is how you reopen hiring.
    A DECREASE that meets the new target fulfils it on the spot.

⚠️ `/position-requests/pending` is declared BEFORE `/{requirement_id}/…` —
both are two segments, so the literal must win. Pinned by a test.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write_action, get_crm_db, screener_or
from models import (
    Customer, Opportunity, Requirement, RequirementActivityLog, RequirementPositionRequest,
    RequirementStatus,
)
from schemas.common import envelope
from services.crm_common import log_activity
from services.requirements import (
    POSITION_CHANGE_BLOCKED_STATUSES, POSITION_REOPEN_STATUS, TA_VISIBLE_STATUSES,
    check_and_mark_fulfilled, joined_count, positions_summary,
    # It returns the parent opportunity's opp_id (C-2026-00089), never REQ-xxxx —
    # the local name says so, because every message here names the opportunity.
    requirement_label as opportunity_label,
)

router = APIRouter(prefix="/api/requirements", tags=["CRM: Requirement positions"])

#: A screener (RMG by role, GM by approval) reads positions even when the
#: custom role / template never granted the `requirements` tab (1 Oct 2026).
POS_READ = screener_or(gated_read("requirements", "TA", "RMG", "Sales", "Sales_Head"))
#: Who may ASK for a change — Sales owns the customer conversation.
POS_REQUEST = gated_write_action("requirement.positions.request", "requirements", "Sales", "Sales_Head")
#: Who may APPROVE — RMG only by default; Admin/CEO always (user decision,
#: 21 Sep 2026). Editable in Users ▸ Action permissions without a deploy.
POS_APPROVE = gated_write_action("requirement.positions.approve", "requirements", "RMG")

MIN_REASON = 10
#: A sane ceiling so a typo ("50" for "5") cannot turn into a 500-person target.
MAX_POSITIONS = 999


class PositionRequestIn(BaseModel):
    to_positions: int = Field(ge=1, le=MAX_POSITIONS)
    reason: str = Field(min_length=1)


class DecisionIn(BaseModel):
    note: str | None = None


# ------------------------------------------------------------------ helpers


def _iso(v):
    return v.isoformat() if v is not None else None


def _val(v):
    return getattr(v, "value", v)


def serialize_request(r: RequirementPositionRequest) -> dict:
    delta = int(r.to_positions) - int(r.from_positions)
    return {
        "id": r.id,
        "requirement_id": r.requirement_id,
        "status": r.status,
        "from_positions": r.from_positions,
        "to_positions": r.to_positions,
        "delta": delta,
        "direction": "increase" if delta > 0 else "decrease",
        "reason": r.reason,
        "status_before": r.status_before,
        "status_after": r.status_after,
        "joined_at_decision": r.joined_at_decision,
        "requested_by": r.requested_by,
        "requested_by_name": r.requested_by_name,
        "requested_at": _iso(r.requested_at),
        "decided_by": r.decided_by,
        "decided_by_name": r.decided_by_name,
        "decided_at": _iso(r.decided_at),
        "decision_note": r.decision_note,
    }


def _visible_requirement(db: Session, requirement_id: int, user: CurrentUser) -> Requirement:
    """The requirement whose headcount this is, scoped the way the OPPORTUNITY is.

    Sales reaches positions from the opportunity page (there is no Requirements
    sub-tab for them), and `GET /api/opportunities` is NOT creator-scoped — every
    Sales user sees every deal. Requirement visibility, however, scopes Sales to
    `created_by`, so the generic `ensure_visible` 404'd a Sales user looking at a
    colleague's opportunity and the panel could only say "Requirement not found"
    (reported 21 Sep 2026). Positions therefore follow the opportunity: Sales and
    the see-all roles get it, TA stays limited to sourcing-onward statuses.

    The word "requirement" never reaches the user here — Sales thinks in
    opportunities, so every message names the opportunity instead.
    """
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404,
                            detail="Positions are not set up for this opportunity yet — it has no "
                                   "approved hiring requirement.")
    if user.is_admin or user.has_any("Sales", "Sales_Head", "RMG"):
        return req
    if "TA" in user.roles and req.status in TA_VISIBLE_STATUSES:
        return req
    # A GM (custom role) reads it like RMG (29 Sep 2026 report: "Your role cannot
    # view positions" on every opportunity): whoever screens as RMG, or whose
    # template / custom role opens the requirements list (the SAME rules the
    # desk and the requirement list use), sees the headcount.
    from services.action_permissions import screens_as_rmg
    from services.requirements import sees_all_requirements
    if screens_as_rmg(db, user) or sees_all_requirements(db, user):
        return req
    raise HTTPException(status_code=403, detail="Your role cannot view positions for this opportunity")


def _pending(db: Session, requirement_id: int) -> RequirementPositionRequest | None:
    return db.execute(
        select(RequirementPositionRequest).where(
            RequirementPositionRequest.requirement_id == requirement_id,
            RequirementPositionRequest.status == "Pending",
        )
    ).scalars().first()


def _can_approve(user: CurrentUser, db: Session | None = None) -> bool:
    """Same answer as the POS_APPROVE gate (`action_permissions.user_may`):
    the user's template / role Approvals, else the action's role list."""
    from services.action_permissions import user_may
    # No session (unit tests): no template to read, so the role list decides.
    return user_may(db, user, "requirement.positions.approve", None if db is not None else {})


def _guard_change(db: Session, req: Requirement, to_positions: int) -> int:
    """Shared by request and approve — the second call is the one that matters,
    because candidates keep joining while a request waits. Returns Joined count."""
    if req.status in POSITION_CHANGE_BLOCKED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Hiring for {opportunity_label(req)} is {_val(req.status).replace('_', ' ')} — "
                   f"reopen it before changing the number of positions.",
        )
    joined = joined_count(db, req)
    if to_positions < joined:
        raise HTTPException(
            status_code=400,
            detail=f"{joined} candidate(s) have already joined on this opportunity, so the count "
                   f"cannot go below {joined}.",
        )
    return joined


def _apply(db: Session, req: Requirement, pr: RequirementPositionRequest, user: CurrentUser,
           joined: int) -> None:
    """Move the headcount and re-derive the requirement's status.

    An increase on a Fulfilled requirement reopens sourcing (Fulfilled is
    derived state, so it is ours to undo); anything else re-runs the normal
    fulfilment check, which closes a requirement whose new, lower target is
    already met. Pre-sourcing statuses (Draft, approvals) are left alone.
    """
    before = _val(req.status)
    was_increase = int(pr.to_positions) > int(req.no_of_positions or 1)
    req.no_of_positions = int(pr.to_positions)

    if was_increase and req.status == RequirementStatus.FULFILLED and pr.to_positions > joined:
        req.status = POSITION_REOPEN_STATUS
        log_activity(
            db, RequirementActivityLog, "requirement_id", req.id, user.id, "REOPENED",
            f"Positions raised to {pr.to_positions}; {joined} joined — back to "
            f"{_val(POSITION_REOPEN_STATUS)} for sourcing",
        )
    else:
        check_and_mark_fulfilled(db, req.id, user.id)
        db.flush()

    synced = _sync_opportunity_positions(db, req, int(pr.to_positions))

    pr.status_before = before
    pr.status_after = _val(req.status)
    pr.joined_at_decision = joined
    log_activity(
        db, RequirementActivityLog, "requirement_id", req.id, user.id, "POSITIONS_CHANGED",
        f"Positions {pr.from_positions} → {pr.to_positions} "
        f"(approved by {pr.decided_by_name or user.full_name or user.username}). Reason: {pr.reason}"
        + (f". Also updated: {synced}." if synced else ""),
    )


#: The opportunity form's headcount field. It is the SAME fact as
#: `requirements.no_of_positions` (the requirement is seeded from it at
#: approval — `services.opportunities.requirement_fields_from_opportunity`),
#: so after an approved change the two must not disagree: the opportunity page
#: shows both, and a reader trusts whichever they see first.
OPP_POSITIONS_KEY = "tm_positions_count"


def _sync_opportunity_positions(db: Session, req: Requirement, to_positions: int) -> str:
    """Carry an approved headcount back onto the parent opportunity.

    "Positions (Count)" on the opportunity seeded this requirement and is still
    printed in Time & Material Details, so leaving it behind means one page
    shows 1 and 2 for the same question (reported 21 Sep 2026).

    It also drives **RFI Value = annual revenue × period/12 × positions**, which
    is strictly LINEAR in the count — so an approved 1 → 2 exactly doubles the
    deal's value. We scale it by the same ratio rather than recomputing (that
    preserves any figure Sales adjusted by hand) and only when a value exists;
    a blank RFI stays blank. Both moves are returned for the activity log, so
    the change to a commercial number is never silent.
    """
    if not req.opportunity_id:
        return ""
    opp = db.get(Opportunity, req.opportunity_id)
    if opp is None:
        return ""

    notes: list[str] = []
    details = dict(opp.details or {})
    old_raw = details.get(OPP_POSITIONS_KEY)
    try:
        old_count = int(float(old_raw))
    except (TypeError, ValueError):
        old_count = None
    if old_count != int(to_positions):
        details[OPP_POSITIONS_KEY] = int(to_positions)
        # Reassign (never mutate in place): SQLAlchemy does not track JSONB edits.
        opp.details = details
        notes.append(f"opportunity Positions (Count) {old_raw if old_raw is not None else '—'} "
                     f"→ {to_positions}")

        if opp.rfi_value is not None and old_count and old_count > 0:
            from decimal import Decimal

            before_rfi = Decimal(str(opp.rfi_value))
            opp.rfi_value = (before_rfi * Decimal(int(to_positions)) / Decimal(old_count)).quantize(
                Decimal("0.01"))
            notes.append(f"RFI value ₹{before_rfi:,.0f} → ₹{Decimal(str(opp.rfi_value)):,.0f} "
                         f"(same value per position)")
    return "; ".join(notes)


def _context(db: Session, req: Requirement) -> tuple[str, str]:
    """(label, customer name) for notification copy."""
    opp = db.get(Opportunity, req.opportunity_id) if req.opportunity_id else None
    customer = db.get(Customer, req.customer_id) if req.customer_id else None
    title = req.title or (opp.title if opp else "")
    return f"{opportunity_label(req)} — {title}", (customer.name if customer else "")


def _notify(db: Session, req: Requirement, pr: RequirementPositionRequest, user: CurrentUser, *,
            event: str, title: str, message: str, roles, also_user_id: int | None = None) -> None:
    """Best-effort: a notification must never fail the decision it announces."""
    try:
        with db.begin_nested():
            from services.notify import notify_roles, notify_user

            link = f"/admin/?view=crm&p=requirements/{req.id}"
            notify_roles(db, list(roles), title, message, link, exclude_user_id=user.id, actor=user,
                         event=event, dedupe_prefix=f"{event}:{pr.id}",
                         related_type="requirement", related_id=req.id)
            if also_user_id and also_user_id != user.id:
                notify_user(db, also_user_id, title, message, link, actor=user, event=event,
                            related_type="requirement", related_id=req.id)
    except Exception:  # noqa: BLE001 — see docstring
        pass


def _summary(pr: RequirementPositionRequest) -> str:
    verb = "increase" if pr.to_positions > pr.from_positions else "reduce"
    return f"{verb} positions from {pr.from_positions} to {pr.to_positions}"


# ---------------------------------------------------------------- endpoints
# ⚠️ literal path first — see the module docstring.


@router.get("/position-requests/pending")
def pending_position_requests(db: Session = Depends(get_crm_db),
                              user: CurrentUser = Depends(POS_READ)):
    """Every headcount change awaiting a decision — RMG's queue."""
    rows = db.execute(
        select(RequirementPositionRequest, Requirement, Customer)
        .join(Requirement, Requirement.id == RequirementPositionRequest.requirement_id)
        .outerjoin(Customer, Customer.id == Requirement.customer_id)
        .where(RequirementPositionRequest.status == "Pending")
        .order_by(RequirementPositionRequest.requested_at.asc())
    ).all()
    out = []
    for pr, req, customer in rows:
        try:
            _visible_requirement(db, req.id, user)
        except HTTPException:
            continue
        out.append({
            **serialize_request(pr),
            "opportunity_label": opportunity_label(req),
            "requirement_label": opportunity_label(req),   # kept for the RMG queue
            "requirement_title": req.title,
            "requirement_status": _val(req.status),
            "customer_name": customer.name if customer else None,
            **positions_summary(req.no_of_positions, joined_count(db, req)),
        })
    return envelope(data=out, meta={"can_approve": _can_approve(user, db), "count": len(out)})


@router.get("/{requirement_id}/position-requests")
def list_position_requests(requirement_id: int, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(POS_READ)):
    req = _visible_requirement(db, requirement_id, user)
    rows = db.execute(
        select(RequirementPositionRequest)
        .where(RequirementPositionRequest.requirement_id == req.id)
        .order_by(RequirementPositionRequest.requested_at.desc(), RequirementPositionRequest.id.desc())
    ).scalars().all()
    pending = next((r for r in rows if r.status == "Pending"), None)
    joined = joined_count(db, req)
    can_approve = _can_approve(user, db)
    return envelope(data=[serialize_request(r) for r in rows], meta={
        **positions_summary(req.no_of_positions, joined),
        "pending_id": pending.id if pending else None,
        "pending": serialize_request(pending) if pending else None,
        "can_request": bool(user.is_admin or user.has_any("Sales", "Sales_Head")),
        "can_approve": bool(can_approve and pending is not None
                            and (pending.requested_by != user.id or user.is_admin)),
        "is_requester": bool(pending and pending.requested_by == user.id),
        "min_positions": joined,
        "max_positions": MAX_POSITIONS,
        "change_blocked": req.status in POSITION_CHANGE_BLOCKED_STATUSES,
    })


@router.post("/{requirement_id}/position-requests")
def request_position_change(requirement_id: int, body: PositionRequestIn,
                            db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(POS_REQUEST)):
    req = _visible_requirement(db, requirement_id, user)
    reason = (body.reason or "").strip()
    if len(reason) < MIN_REASON:
        raise HTTPException(
            status_code=400,
            detail=f"Please give a reason of at least {MIN_REASON} characters — RMG sees it and it "
                   f"stays in this opportunity's history",
        )
    current = int(req.no_of_positions or 1)
    if int(body.to_positions) == current:
        raise HTTPException(status_code=400,
                            detail=f"This opportunity already has {current} position(s)")
    if _pending(db, req.id) is not None:
        raise HTTPException(status_code=409,
                            detail="A position change is already awaiting RMG approval on this opportunity")
    joined = _guard_change(db, req, int(body.to_positions))

    who = user.full_name or user.username
    pr = RequirementPositionRequest(
        requirement_id=req.id, status="Pending", from_positions=current,
        to_positions=int(body.to_positions), reason=reason,
        requested_by=user.id, requested_by_name=who,
    )
    db.add(pr)
    db.flush()
    label, customer = _context(db, req)
    where = f" ({customer})" if customer else ""

    if user.is_admin:
        # Admin/CEO are the escalation path: applied at once, still recorded.
        pr.status = "Approved"
        pr.decided_by = user.id
        pr.decided_by_name = who
        pr.decided_at = datetime.now(timezone.utc)
        pr.decision_note = "Applied directly by Admin/CEO"
        _apply(db, req, pr, user, joined)
        db.commit()
        db.refresh(req)
        _notify(db, req, pr, user,
                event="requirement.positions_approved",
                title=f"Positions changed: {label}",
                message=f"{who} (Admin/CEO) set {label}{where} to {pr.to_positions} position(s) "
                        f"(was {pr.from_positions}). Reason: {reason}. "
                        f"{max(0, pr.to_positions - joined)} still open for sourcing.",
                roles=("RMG", "TA", "Sales_Head"))
        db.commit()
        return envelope(
            data={"request": serialize_request(pr),
                  **positions_summary(req.no_of_positions, joined),
                  "requirement_status": _val(req.status)},
            message="Positions updated and recorded",
        )

    db.commit()
    _notify(db, req, pr, user,
            event="requirement.positions_requested",
            title=f"Position change to approve: {label}",
            message=f"{who} asks to {_summary(pr)} on {label}{where}. Reason: {reason}. "
                    f"{joined} candidate(s) joined so far. Approve or reject it on the opportunity.",
            roles=("RMG", "Sales_Head"))
    db.commit()
    return envelope(data={"request": serialize_request(pr)},
                    message="Sent to RMG for approval")


def _get_pending_or_404(db: Session, req: Requirement, prid: int) -> RequirementPositionRequest:
    pr = db.get(RequirementPositionRequest, prid)
    if pr is None or pr.requirement_id != req.id:
        raise HTTPException(status_code=404,
                            detail="That position change no longer exists on this opportunity")
    if pr.status != "Pending":
        raise HTTPException(status_code=409,
                            detail=f"This position change is already {pr.status.lower()}")
    return pr


@router.post("/{requirement_id}/position-requests/{prid}/approve")
def approve_position_change(requirement_id: int, prid: int, body: DecisionIn | None = None,
                            db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(POS_APPROVE)):
    req = _visible_requirement(db, requirement_id, user)
    pr = _get_pending_or_404(db, req, prid)
    if pr.requested_by == user.id and not user.is_admin:
        raise HTTPException(status_code=403,
                            detail="You cannot approve your own request — RMG, Admin or CEO must")
    # Re-check against TODAY's numbers: candidates join while a request waits.
    joined = _guard_change(db, req, int(pr.to_positions))

    who = user.full_name or user.username
    pr.status = "Approved"
    pr.decided_by = user.id
    pr.decided_by_name = who
    pr.decided_at = datetime.now(timezone.utc)
    pr.decision_note = ((body.note if body else None) or "").strip() or None
    _apply(db, req, pr, user, joined)
    db.commit()
    db.refresh(req)

    label, customer = _context(db, req)
    where = f" ({customer})" if customer else ""
    reopened = pr.status_before != pr.status_after
    tail = (f" The requirement moved from {pr.status_before} to {pr.status_after}." if reopened else "")
    _notify(db, req, pr, user,
            event="requirement.positions_approved",
            title=f"Positions changed: {label}",
            message=f"{who} approved {pr.requested_by_name or 'the'} request to {_summary(pr)} on "
                    f"{label}{where}. Reason: {pr.reason}. "
                    f"{max(0, int(pr.to_positions) - joined)} position(s) now open for sourcing.{tail}",
            roles=("TA", "Sales_Head"), also_user_id=pr.requested_by)
    db.commit()
    return envelope(
        data={"request": serialize_request(pr),
              **positions_summary(req.no_of_positions, joined),
              "requirement_status": _val(req.status)},
        message="Position change approved",
    )


@router.post("/{requirement_id}/position-requests/{prid}/reject")
def reject_position_change(requirement_id: int, prid: int, body: DecisionIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(POS_APPROVE)):
    req = _visible_requirement(db, requirement_id, user)
    pr = _get_pending_or_404(db, req, prid)
    note = (body.note or "").strip()
    if len(note) < MIN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Please say why (at least {MIN_REASON} characters) — the requester sees it")
    who = user.full_name or user.username
    pr.status = "Rejected"
    pr.decided_by = user.id
    pr.decided_by_name = who
    pr.decided_at = datetime.now(timezone.utc)
    pr.decision_note = note
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "POSITIONS_REJECTED",
                 f"Rejected {_summary(pr)} (asked by {pr.requested_by_name or '—'}). Note: {note}")
    db.commit()

    label, _customer = _context(db, req)
    _notify(db, req, pr, user,
            event="requirement.positions_rejected",
            title=f"Position change rejected: {label}",
            message=f"{who} rejected the request to {_summary(pr)} on {label}. Note: {note}",
            roles=("Sales_Head",), also_user_id=pr.requested_by)
    db.commit()
    return envelope(data={"request": serialize_request(pr)}, message="Position change rejected")


@router.post("/{requirement_id}/position-requests/{prid}/cancel")
def cancel_position_change(requirement_id: int, prid: int, db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(POS_REQUEST)):
    req = _visible_requirement(db, requirement_id, user)
    pr = _get_pending_or_404(db, req, prid)
    if pr.requested_by != user.id and not user.is_admin:
        raise HTTPException(status_code=403,
                            detail="Only the requester (or Admin/CEO) can withdraw this request")
    pr.status = "Rejected"
    pr.decided_by = user.id
    pr.decided_by_name = user.full_name or user.username
    pr.decided_at = datetime.now(timezone.utc)
    pr.decision_note = "Withdrawn by the requester"
    db.commit()
    return envelope(data={"request": serialize_request(pr)}, message="Request withdrawn")
