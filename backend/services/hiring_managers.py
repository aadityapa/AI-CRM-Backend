"""The Hiring Manager (22 Oct 2026, migration 0145).

A Hiring Manager is a Karnex employee whose login holds the custom role
**"Hiring Manager"**. GM / RMG put them on a position at RMG approval (or later
from the position's team panel); several per position are allowed. From then
on they:

1. confirm the JD & skills before TA sources (`Pending_HM_Review` →
   Open_For_Sourcing, or back to RMG with what to change);
2. review each candidate RMG hands to Sales before the profile goes to the
   customer (`hm_review_status`, the gate on Sales Screening → Customer
   Screening);
3. take interviews and record feedback — My Interviews, exactly like any panel
   member (nothing here);
4. may ask for a headcount change (`requirement.positions.request`);
5. set priority / hold on their positions (`crm_deps.hm_or`);
6. hear about each of these (bell + email, events in `email_flows.EVENTS`);
7. sign off the final selection + terms before the Sales Head approves them
   (`hm_final_status`, the gate on Customer Approval → HR Screening).

Visibility: a login is **HM-scoped** when it holds the Hiring Manager role and
none of the built-in workflow roles (and does not screen as RMG). Such a login
sees ONLY the positions it is on — requirements, their opportunities and the
candidates applied to them; everything else answers 404, so existence never
leaks. Someone who is also Sales / RMG / … keeps their own view.

Every gate is OFF for a position with no Hiring Manager: the two profile
columns stay NULL and the flow is exactly what it was before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (CandidateProfile, CandidateProfileActivityLog, PipelineStatus as PS,
                    Requirement, RequirementActivityLog, RequirementHiringManager,
                    RequirementStatus)
from services.crm_common import log_activity
from services.requirement_assignments import (HM_ASSIGNED_ACTION, _names, inactive_user_ids,
                                              members_by_requirement, replace_members)

logger = logging.getLogger("karnex.crm.hiring_managers")

HM_ROLE = "Hiring Manager"
#: Built-in roles whose holder keeps their own (wider) view even with the HM role.
_WORKFLOW_ROLES = frozenset({"Admin", "CEO", "Sales", "Sales_Head", "RMG", "TA", "HR", "Finance"})

PENDING, APPROVED, REJECTED = "Pending", "Approved", "Rejected"
DECISIONS = ("approve", "reject")
MIN_NOTE = 5

#: Activity rows.
JD_APPROVED_ACTION = "HM_JD_APPROVED"
JD_CHANGES_ACTION = "HM_JD_CHANGES"
REVIEW_ACTION = "HM_REVIEW"
SIGNOFF_ACTION = "HM_SIGNOFF"
#: A rejection by the HM — read by `candidate_status` (TA_CLOSE_ACTIONS) so the
#: status says WHO closed it and in which phase.
REJECTED_AT_REVIEW = "HM_REJECTED"
REJECTED_AT_SIGNOFF = "HM_SIGNOFF_REJECTED"

#: Notification events (listed in `routers/crm/email_flows.EVENTS`).
EVENT_ASSIGNED = "requirement.hm_assigned"
EVENT_JD_REVIEW = "requirement.hm_jd_review"
EVENT_JD_DECIDED = "requirement.hm_jd_decided"
EVENT_REVIEW_REQUESTED = "profile.hm_review_requested"
EVENT_REVIEW_DECIDED = "profile.hm_review_decided"
EVENT_SIGNOFF_REQUESTED = "profile.hm_signoff_requested"
EVENT_SIGNOFF_DECIDED = "profile.hm_signoff_decided"


# ---------------------------------------------------------------- who is one ---

def is_hm_role_name(name) -> bool:
    return str(name or "").strip().lower() == HM_ROLE.lower()


def is_hiring_manager(user) -> bool:
    roles = set(getattr(user, "held_roles", None) or ()) | set(getattr(user, "roles", None) or ())
    return any(is_hm_role_name(r) for r in roles)


#: Roles that may sit beside the HM role without widening the view: the panel
#: login role (an HM takes interviews too).
_COMPANION_ROLES = frozenset({HM_ROLE.lower(), "interviewer"})


def hm_scoped(user, db: Session | None = None) -> bool:
    """True for a login that sees only its assigned positions (see module doc).

    23 Oct 2026 (user report — an HM login read "nothing to show" and saw every
    opportunity): the rule is now WHICH ROLES the login holds, never which
    approvals its role carries. A login holding the HM role and nothing else but
    the Interviewer role is scoped — even when an admin ticked approvals such as
    the RMG screening on the HM role. Anyone also holding another role (built-in,
    or a custom one such as GM) keeps that role's view. `db` is accepted for the
    existing call sites and no longer read."""
    if user is None or not is_hiring_manager(user) or getattr(user, "is_admin", False):
        return False
    held = set(getattr(user, "held_roles", None) or ()) | set(getattr(user, "roles", None) or ())
    if held & _WORKFLOW_ROLES:
        return False
    return all(str(r or "").strip().lower() in _COMPANION_ROLES for r in held)


def hm_options(db: Session) -> list[dict]:
    """Every ACTIVE login holding the Hiring Manager role — the picker's choices."""
    from services.notify import _user_ids_in_role
    ids = _user_ids_in_role(db, HM_ROLE)
    if not ids:
        return []
    gone = inactive_user_ids(db)
    names = _names(db, ids)
    out = [{"id": int(i), "name": names.get(int(i), f"User #{i}")} for i in ids if int(i) not in gone]
    return sorted(out, key=lambda r: r["name"].lower())


# ---------------------------------------------------------------- the team ---

def hms_by_requirement(db: Session, requirement_ids) -> dict[int, list[dict]]:
    """`{requirement_id: [HM…]}` for a whole page — ONE query."""
    return members_by_requirement(db, RequirementHiringManager, requirement_ids)


def hm_user_ids(db: Session, requirement_id: int) -> list[int]:
    """Active HMs of one position."""
    return [m["user_id"] for m in hms_by_requirement(db, [requirement_id]).get(int(requirement_id), [])]


def requirement_ids_for(db: Session, user_id: int) -> list[int]:
    return [int(r) for r in db.execute(
        select(RequirementHiringManager.requirement_id)
        .where(RequirementHiringManager.user_id == int(user_id))).scalars().all()]


def opportunity_ids_for(db: Session, user_id: int) -> list[int]:
    return [int(r) for r in db.execute(
        select(Requirement.opportunity_id).distinct()
        .join(RequirementHiringManager, RequirementHiringManager.requirement_id == Requirement.id)
        .where(RequirementHiringManager.user_id == int(user_id))).scalars().all() if r]


def is_assigned(db: Session, user, requirement_id: int) -> bool:
    return bool(user is not None and db.execute(
        select(RequirementHiringManager.id).where(
            RequirementHiringManager.requirement_id == int(requirement_id),
            RequirementHiringManager.user_id == int(user.id)).limit(1)).first())


def opportunity_hm_ids(db: Session, opportunity_id: int | None) -> list[int]:
    """Active HMs on any position of a deal (one deal has one live requirement)."""
    if not opportunity_id:
        return []
    ids = db.execute(
        select(RequirementHiringManager.user_id).distinct()
        .join(Requirement, Requirement.id == RequirementHiringManager.requirement_id)
        .where(Requirement.opportunity_id == int(opportunity_id))).scalars().all()
    gone = inactive_user_ids(db) if ids else set()
    return [int(u) for u in ids if int(u) not in gone]


def set_hiring_managers(db: Session, req: Requirement, user_ids: list[int], note: str | None,
                        actor) -> dict:
    """Replace the position's Hiring Managers (no commit). Newly added HMs hear
    about it; if the team ends up EMPTY, candidates waiting on an HM decision
    are released (a gate with nobody to open it would strand them)."""
    allowed = {o["id"]: o["name"] for o in hm_options(db)}
    added, removed = replace_members(db, req, RequirementHiringManager, allowed, user_ids, note,
                                     actor, word="Hiring Manager", action=HM_ASSIGNED_ACTION)
    team = hms_by_requirement(db, [req.id]).get(req.id, [])
    if removed and not team:
        _release_waiting(db, req, getattr(actor, "id", None))
    for uid in added:
        _tell(db, uid, f"You are the Hiring Manager for {_label(req)}",
              f"{_who(actor)} made you a Hiring Manager for {req.title} ({_label(req)}). "
              "You will review the JD & skills, the candidates before they go to the customer, "
              "and sign off the final selection.",
              _req_link(req), actor, EVENT_ASSIGNED, f"hm_assigned:{req.id}:{uid}", req.id)
    return {"added": added, "removed": removed, "hiring_managers": team}


def _release_waiting(db: Session, req: Requirement, actor_id: int | None) -> None:
    """Clear HM gates still Pending on this deal once it has no HM left."""
    if req.status == RequirementStatus.PENDING_HM_REVIEW:
        req.status = RequirementStatus.OPEN_FOR_SOURCING
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, actor_id, JD_APPROVED_ACTION,
                     "No Hiring Manager left on the position — opened for sourcing")
    for col in (CandidateProfile.hm_review_status, CandidateProfile.hm_final_status):
        db.execute(sa.update(CandidateProfile)
                   .where(CandidateProfile.opportunity_id == req.opportunity_id, col == PENDING)
                   .values({col.key: None}))


# ---------------------------------------------------------------- scoping ---

def requirement_clause(db: Session, user):
    return Requirement.id.in_(requirement_ids_for(db, user.id) or [-1])


def opportunity_clause(db: Session, user, column):
    return column.in_(opportunity_ids_for(db, user.id) or [-1])


def profile_clause(db: Session, user):
    return opportunity_clause(db, user, CandidateProfile.opportunity_id)


def ensure_requirement(db: Session, user, requirement_id: int) -> None:
    """404 unless an HM-scoped login is on this position (no-op for others)."""
    if hm_scoped(user, db) and not is_assigned(db, user, requirement_id):
        raise HTTPException(status_code=404, detail="Requirement not found")


def ensure_opportunity(db: Session, user, opportunity_id: int | None) -> None:
    if hm_scoped(user, db) and int(opportunity_id or 0) not in set(opportunity_ids_for(db, user.id)):
        raise HTTPException(status_code=404, detail="Opportunity not found")


def ensure_profile(db: Session, user, profile) -> None:
    if hm_scoped(user, db) and int(profile.opportunity_id or 0) not in set(opportunity_ids_for(db, user.id)):
        raise HTTPException(status_code=404, detail="Candidate profile not found")


# ---------------------------------------------------------------- JD review ---

def to_hm_review(db: Session, req: Requirement, actor) -> bool:
    """RMG approved and the position has an HM → wait for the HM's confirmation
    of the JD & skills instead of opening for sourcing. Returns True when it did."""
    hms = hm_user_ids(db, req.id)
    if not hms:
        return False
    req.status = RequirementStatus.PENDING_HM_REVIEW
    for uid in hms:
        _tell(db, uid, f"Confirm the JD & skills: {_label(req)}",
              f"{_who(actor)} approved {req.title} ({_label(req)}). Check the JD and the skills "
              "TA will source against, then approve — or send it back to RMG with what to change.",
              _req_link(req), actor, EVENT_JD_REVIEW, f"hm_jd:{req.id}:{uid}:{_stamp()}", req.id)
    return True


def _require_jd_turn(db: Session, req: Requirement, user) -> None:
    if req.status != RequirementStatus.PENDING_HM_REVIEW:
        raise HTTPException(status_code=409, detail="This position is not waiting for the Hiring Manager.")
    if not (getattr(user, "is_admin", False) or is_assigned(db, user, req.id)):
        raise HTTPException(status_code=403, detail="Only a Hiring Manager of this position can decide.")


def approve_jd(db: Session, req: Requirement, comment: str | None, user) -> None:
    """HM confirms the JD & skills → Open_For_Sourcing; TA is told (no commit)."""
    _require_jd_turn(db, req, user)
    req.status = RequirementStatus.OPEN_FOR_SOURCING
    note = (comment or "").strip()
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id, JD_APPROVED_ACTION,
                 "Hiring Manager confirmed the JD & skills" + (f": {note}" if note else ""))
    from services.requirements import announce_open_for_sourcing
    announce_open_for_sourcing(db, req, user)
    if req.engineering_reviewed_by and req.engineering_reviewed_by != user.id:
        _tell(db, req.engineering_reviewed_by, f"Hiring Manager approved the JD: {_label(req)}",
              f"{_who(user)} confirmed the JD & skills — {req.title} is open for sourcing."
              + (f" Note: {note}" if note else ""), _req_link(req), user, EVENT_JD_DECIDED,
              f"hm_jd_ok:{req.id}:{_stamp()}", req.id)


def request_jd_changes(db: Session, req: Requirement, reason: str, user) -> None:
    """HM sends the JD back to RMG review with what to change (no commit)."""
    _require_jd_turn(db, req, user)
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(status_code=400, detail="Say what to change (at least 10 characters).")
    req.status = RequirementStatus.PENDING_ENGINEERING_REVIEW
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id, JD_CHANGES_ACTION,
                 f"Hiring Manager asked for JD / skill changes: {reason}")
    from services.notify import notify_role
    try:
        with db.begin_nested():
            notify_role(db, "RMG", f"Hiring Manager wants JD changes: {_label(req)}",
                        f"{_who(user)}: {reason}", _req_link(req), exclude_user_id=user.id,
                        actor=user, event=EVENT_JD_DECIDED,
                        dedupe_prefix=f"hm_jd_back:{req.id}:{_stamp()}",
                        user_ids=[req.engineering_reviewed_by] if req.engineering_reviewed_by else None,
                        related_type="requirement", related_id=req.id)
    except Exception:  # noqa: BLE001
        logger.warning("JD-changes notice failed for requirement %s", req.id, exc_info=True)


# ------------------------------------------------- candidate review + sign-off ---

def on_stage_arrival(db: Session, profile, new_status: str, user) -> None:
    """Open the HM gate when a candidate reaches Sales Screening (review) or
    Customer Approval (sign-off) on a position that has an HM. Never raises."""
    try:
        if new_status == PS.SALES_SCREENING.value:
            col, event, ask = "review", EVENT_REVIEW_REQUESTED, (
                "RMG handed this candidate to Sales. Review the profile and interview feedback, "
                "then approve it for the customer — or reject it.")
        elif new_status == PS.CUSTOMER_APPROVAL.value:
            col, event, ask = "final", EVENT_SIGNOFF_REQUESTED, (
                "The customer selected this candidate and Sales submitted the terms. Sign off the "
                "selection before the Sales Head approves it — or reject it.")
        else:
            return
        hms = opportunity_hm_ids(db, profile.opportunity_id)
        if not hms:
            return
        with db.begin_nested():
            setattr(profile, f"hm_{col}_status", PENDING)
            setattr(profile, f"hm_{col}_note", None)
            setattr(profile, f"hm_{col}_by", None)
            setattr(profile, f"hm_{col}_at", None)
            name = _candidate_name(db, profile)
            title = (f"Review candidate: {name}" if col == "review" else f"Sign off the selection: {name}")
            for uid in hms:
                if uid == getattr(user, "id", None):
                    continue
                _tell(db, uid, title, ask, _profile_link(profile), user, event,
                      f"hm_{col}:{profile.id}:{uid}:{_stamp()}", None)
    except Exception:  # noqa: BLE001 — a notice never costs a move
        logger.warning("HM gate on arrival failed for profile %s", profile.id, exc_info=True)


def entry_block(db: Session, profile, new_status: str) -> str | None:
    """Why `new_status` cannot be entered yet (an HM decision is due), else None."""
    if new_status == PS.CUSTOMER_SCREENING.value and profile.hm_review_status == PENDING:
        if opportunity_hm_ids(db, profile.opportunity_id):
            return ("The Hiring Manager has not reviewed this candidate yet — the profile goes to "
                    "the customer once they approve it.")
    if new_status == PS.HR_SCREENING.value and profile.hm_final_status == PENDING:
        if opportunity_hm_ids(db, profile.opportunity_id):
            return ("Waiting for the Hiring Manager's sign-off on this selection — the Sales Head "
                    "approves after it.")
    return None


def _require_profile_turn(db: Session, profile, user, col: str, stage: str) -> None:
    from services.candidate_profiles import _status_value
    if _status_value(profile.pipeline_status) != stage or getattr(profile, f"hm_{col}_status") != PENDING:
        raise HTTPException(status_code=409,
                            detail=("This candidate is not waiting for the Hiring Manager's "
                                     + ("review." if col == "review" else "sign-off.")))
    if getattr(user, "is_admin", False):
        return
    if int(user.id) not in set(opportunity_hm_ids(db, profile.opportunity_id)):
        raise HTTPException(status_code=403, detail="Only a Hiring Manager of this position can decide.")


def decide(db: Session, profile, kind: str, decision: str, note: str | None, user) -> str:
    """The HM's review (`kind="review"`) or sign-off (`kind="signoff"`) of one
    candidate (no commit). Approve opens the next step; reject closes the
    candidacy as "Rejected by Hiring Manager". Returns the message."""
    if kind not in ("review", "signoff") or decision not in DECISIONS:
        raise HTTPException(status_code=400, detail="Unknown decision")
    col = "review" if kind == "review" else "final"
    stage = PS.SALES_SCREENING.value if kind == "review" else PS.CUSTOMER_APPROVAL.value
    _require_profile_turn(db, profile, user, col, stage)
    note = (note or "").strip()
    if decision == "reject" and len(note) < MIN_NOTE:
        raise HTTPException(status_code=400, detail=f"Give the reason (at least {MIN_NOTE} characters).")
    setattr(profile, f"hm_{col}_status", APPROVED if decision == "approve" else REJECTED)
    setattr(profile, f"hm_{col}_note", note or None)
    setattr(profile, f"hm_{col}_by", user.id)
    setattr(profile, f"hm_{col}_at", datetime.now(timezone.utc))
    word = "review" if kind == "review" else "sign-off"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 REVIEW_ACTION if kind == "review" else SIGNOFF_ACTION,
                 f"Hiring Manager {word}: {'approved' if decision == 'approve' else 'rejected'}"
                 + (f" — {note}" if note else ""))
    name = _candidate_name(db, profile)
    if decision == "reject":
        from services.candidate_profiles import _auto_move
        _auto_move(db, profile, PS.REJECTED.value, user,
                   f"Rejected by the Hiring Manager at {word}: {note}", allow_exit=True)
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     REJECTED_AT_REVIEW if kind == "review" else REJECTED_AT_SIGNOFF, note)
        _announce(db, profile, user, ["Sales", "RMG"] + (["Sales_Head"] if kind == "signoff" else []),
                  f"Hiring Manager rejected {name}", f"{_who(user)} rejected {name} at {word}: {note}",
                  kind)
        return f"{name} rejected — Sales, RMG and TA were told"
    if kind == "review":
        _announce(db, profile, user, ["Sales"], f"Hiring Manager approved {name} for the customer",
                  f"{_who(user)} approved {name}. Submit the profile to the customer."
                  + (f" Note: {note}" if note else ""), kind)
        return f"{name} approved — Sales can submit the profile to the customer"
    approvers = None
    try:
        from services.action_permissions import user_ids_who_may
        approvers = list(user_ids_who_may(db, "profile.sales_head_decision")) or None
    except Exception:  # noqa: BLE001
        approvers = None
    _announce(db, profile, user, ["Sales_Head", "Sales"], f"Hiring Manager signed off {name}",
              f"{_who(user)} signed off the selection of {name}. The Sales Head can approve the terms."
              + (f" Note: {note}" if note else ""), kind, extra_ids=approvers)
    return f"{name} signed off — the Sales Head can approve the terms"


def _announce(db: Session, profile, user, roles: list[str], title: str, body: str, kind: str,
              extra_ids=None) -> None:
    try:
        from services.candidate_profiles import ta_user_ids
        from services.notify import notify_roles
        ids = set(extra_ids or ()) | set(ta_user_ids(db, profile))
        with db.begin_nested():
            notify_roles(db, roles, title, body, _profile_link(profile), exclude_user_id=user.id,
                         actor=user, user_ids=sorted(ids) or None,
                         event=EVENT_REVIEW_DECIDED if kind == "review" else EVENT_SIGNOFF_DECIDED,
                         dedupe_prefix=f"hm_{kind}_done:{profile.id}:{_stamp()}",
                         related_type="candidate", related_id=profile.candidate_id)
    except Exception:  # noqa: BLE001
        logger.warning("HM decision notice failed for profile %s", profile.id, exc_info=True)


def payload(db: Session, profile) -> dict | None:
    """What the profile page prints about the HM gates (None = no HM on the deal)."""
    hms = opportunity_hm_ids(db, profile.opportunity_id)
    if not hms and not profile.hm_review_status and not profile.hm_final_status:
        return None
    names = _names(db, set(hms) | {profile.hm_review_by, profile.hm_final_by} - {None})

    def step(col):
        by = getattr(profile, f"hm_{col}_by")
        at = getattr(profile, f"hm_{col}_at")
        return {"status": getattr(profile, f"hm_{col}_status"), "note": getattr(profile, f"hm_{col}_note"),
                "by": by, "by_name": names.get(by) if by else None,
                "at": at.isoformat() if at else None}

    return {"hiring_managers": [{"user_id": u, "name": names.get(u, f"User #{u}")} for u in hms],
            "review": step("review"), "signoff": step("final")}


# ---------------------------------------------------------------- dashboard ---

#: Stage keys the position cards draw as their little funnel (live phases).
FUNNEL_STAGES = ("sourcing", "technical_screening", "technical_interview", "sales_screening",
                 "customer_screening", "customer_interviewing", "selection", "hr_screening",
                 "hr_interviewing", "onboarding", "joined")
RECENT_MOVES = 12


def dashboard(db: Session, user, *, now: datetime | None = None) -> dict:
    """The Hiring Manager's Dashboard (23 Oct 2026, user report: "his Dashboard
    reads nothing to show"). Everything is about the positions THIS login is on:

    * `tiles` — positions · open headcount · candidates live · waiting for you ·
      interviews this week · joined;
    * `decisions` — JD to confirm, candidates to review, selections to sign off
      (oldest first), each with where to act;
    * `positions` — one card per position: status, priority, target date,
      filled / total, the candidates per phase, the TA team;
    * `stages` — candidates per phase across all their positions;
    * `interviews` — their own panel rounds (feedback due + next 7 days);
    * `recent` — the last stage moves on their candidates.

    Batched: requirements · headcount · profiles · statuses · activity, never per
    row. An HM on no position gets the same shape, empty, and `assigned: False`."""
    from datetime import timedelta

    from models import Candidate, Customer, Opportunity
    from services.candidate_profiles import REJECTED_BUCKET
    from services.candidate_status import STAGE_LABEL, statuses_for
    from services.requirements import positions_by_opportunity, requirement_label

    now = now or datetime.now(timezone.utc)
    req_ids = requirement_ids_for(db, user.id)
    out = {"assigned": bool(req_ids), "tiles": {}, "decisions": [], "positions": [],
           "stages": [], "interviews": {"due": [], "upcoming": [], "linked": False},
           "recent": [], "as_of": now.isoformat()}
    rows = db.execute(
        select(Requirement, Opportunity, Customer.name)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .where(Requirement.id.in_(req_ids or [-1]))
        .order_by(Requirement.id.desc())).all()
    opp_ids = [o.id for _, o, _ in rows]
    heads = positions_by_opportunity(db, opp_ids)
    profiles = db.execute(
        select(CandidateProfile, Candidate)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .where(CandidateProfile.opportunity_id.in_(opp_ids or [-1]),
               sa.or_(CandidateProfile.is_hidden.is_(False),
                      CandidateProfile.pipeline_status == PS.JOINED))).all()
    statuses = statuses_for(db, [p for p, _ in profiles]) if profiles else {}

    def val(x):
        return getattr(x, "value", x)

    def cname(c):
        return " ".join(x for x in (c.first_name, c.last_name) if x) or f"Candidate #{c.id}"

    per_opp: dict[int, dict[str, int]] = {}
    totals: dict[str, int] = {}
    live = 0
    for p, _c in profiles:
        st = statuses.get(p.id) or {}
        key = ((st.get("stage") or {}).get("key")) or "sourcing"
        closed = val(p.pipeline_status) in REJECTED_BUCKET
        if closed:
            key = "closed"
        else:
            live += 1 if key != "joined" else 0
        per_opp.setdefault(int(p.opportunity_id), {})
        per_opp[int(p.opportunity_id)][key] = per_opp[int(p.opportunity_id)].get(key, 0) + 1
        totals[key] = totals.get(key, 0) + 1

    # -- what is waiting for this HM
    decisions: list[dict] = []
    for r, o, cust in rows:
        if r.status == RequirementStatus.PENDING_HM_REVIEW:
            decisions.append({
                "kind": "jd", "title": f"{requirement_label(r)} — {r.title}", "opportunity_id": o.id,
                "subtitle": "RMG approved the position — confirm the JD & skills",
                "customer": cust, "since": r.engineering_reviewed_at.isoformat() if r.engineering_reviewed_at else None,
                "path": f"opportunities/{o.id}", "action": "Review JD & skills"})
    opp_by_id = {o.id: (r, o, cust) for r, o, cust in rows}
    for p, c in profiles:
        stage = val(p.pipeline_status)
        kind = ("review" if stage == PS.SALES_SCREENING.value and p.hm_review_status == PENDING else
                "signoff" if stage == PS.CUSTOMER_APPROVAL.value and p.hm_final_status == PENDING else None)
        if not kind:
            continue
        r, o, cust = opp_by_id.get(int(p.opportunity_id), (None, None, None))
        decisions.append({
            "kind": kind, "title": cname(c), "profile_id": p.id, "opportunity_id": int(p.opportunity_id),
            "subtitle": (f"{o.opp_id} — {o.title}" if o else ""),
            "customer": cust, "since": p.updated_at.isoformat() if p.updated_at else None,
            "path": f"profiles/{p.id}",
            "action": "Review candidate" if kind == "review" else "Sign off"})
    decisions.sort(key=lambda d: d.get("since") or "")
    out["decisions"] = decisions

    # -- the position cards
    positions = []
    open_heads = joined = 0
    for r, o, cust in rows:
        h = heads.get(o.id, {})
        counts = per_opp.get(o.id, {})
        open_heads += int(h.get("positions_open", 0) or 0) if val(r.status) not in ("Closed", "Cancelled", "Fulfilled") else 0
        joined += int(h.get("positions_joined", 0) or 0)
        positions.append({
            "requirement_id": r.id, "opportunity_id": o.id, "label": requirement_label(r),
            "title": r.title, "customer": cust,
            "status": val(r.status), "display_status": h.get("requirement_display_status") or val(r.status),
            "priority": val(r.priority), "target_date": r.target_closure_date.isoformat() if r.target_closure_date else None,
            "positions_total": h.get("positions_total", r.no_of_positions or 1),
            "positions_joined": h.get("positions_joined", 0), "positions_open": h.get("positions_open", 0),
            "experience_min": h.get("requirement_experience_min"), "experience_max": h.get("requirement_experience_max"),
            "location": h.get("requirement_location_name"), "work_mode": h.get("requirement_work_mode"),
            "assigned_tas": h.get("assigned_tas", []),
            "candidates": sum(v for k, v in counts.items() if k != "closed"),
            "closed": counts.get("closed", 0),
            "stages": [{"key": k, "label": STAGE_LABEL.get(k, k), "count": counts.get(k, 0)} for k in FUNNEL_STAGES],
            "waiting": sum(1 for d in decisions if d.get("opportunity_id") == o.id),
        })
    out["positions"] = positions
    out["stages"] = [{"key": k, "label": STAGE_LABEL.get(k, k), "count": totals.get(k, 0)}
                     for k in (*FUNNEL_STAGES, "closed")]

    # -- their own interviews (panel)
    week = 0
    try:
        from services.panel_interviews import my_rounds
        with db.begin_nested():
            mine = my_rounds(db, user, "all", now=now, limit=200)
        out["interviews"]["linked"] = bool(mine.get("linked"))
        horizon = (now + timedelta(days=7)).isoformat()
        for row in mine.get("rows", []):
            slim = {k: row.get(k) for k in ("id", "round_label", "scheduled_at", "raw_when", "meeting_link",
                                             "phase", "profile_id")}
            slim["candidate"] = (row.get("candidate") or {}).get("name")
            slim["position"] = row.get("position")
            if row.get("phase") == "pending":
                out["interviews"]["due"].append(slim)
            elif row.get("phase") == "upcoming" and (row.get("scheduled_at") or "") <= horizon:
                out["interviews"]["upcoming"].append(slim)
                week += 1
        out["interviews"]["upcoming"].sort(key=lambda x: x.get("scheduled_at") or "")
    except Exception:  # noqa: BLE001 — the panel half never blanks the dashboard
        logger.warning("HM dashboard: panel rounds failed for %s", user.id, exc_info=True)

    # -- recent stage moves on their candidates
    try:
        ids = [p.id for p, _ in profiles]
        if ids:
            names = {p.id: cname(c) for p, c in profiles}
            with db.begin_nested():
                moves = db.execute(
                    select(CandidateProfileActivityLog)
                    .where(CandidateProfileActivityLog.profile_id.in_(ids),
                           CandidateProfileActivityLog.action_type.in_(
                               ("STATUS_CHANGE", REVIEW_ACTION, SIGNOFF_ACTION, "RESULT_RECORDED")))
                    .order_by(CandidateProfileActivityLog.timestamp.desc())
                    .limit(RECENT_MOVES)).scalars().all()
            who = _names(db, {m.user_id for m in moves if m.user_id})
            out["recent"] = [{
                "profile_id": m.profile_id, "candidate": names.get(m.profile_id),
                "action": m.action_type, "comment": (m.comment or "")[:220],
                "by": who.get(m.user_id) if m.user_id else None,
                "at": m.timestamp.isoformat() if m.timestamp else None} for m in moves]
    except Exception:  # noqa: BLE001
        logger.warning("HM dashboard: recent moves failed for %s", user.id, exc_info=True)

    out["tiles"] = {
        "positions": len(positions), "open_positions": open_heads, "candidates": live,
        "waiting": len(decisions), "interviews_week": week,
        "feedback_due": len(out["interviews"]["due"]), "joined": joined,
    }
    return out


# ---------------------------------------------------------------- helpers ---

def _label(req) -> str:
    from services.requirements import requirement_label
    return requirement_label(req)


def _who(actor) -> str:
    return getattr(actor, "full_name", None) or getattr(actor, "username", None) or "RMG"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M")


def _req_link(req) -> str:
    return f"/admin/?view=crm&p=requirements/{req.id}"


def _profile_link(profile) -> str:
    return f"/admin/?view=crm&p=profiles/{profile.id}"


def _candidate_name(db: Session, profile) -> str:
    from services.candidate_profiles import _candidate_display_name
    return _candidate_display_name(db, profile)


def _tell(db: Session, user_id, title, body, link, actor, event, dedupe, req_id) -> None:
    """Bell + email to one person, best-effort in a savepoint."""
    try:
        from services.notify import notify_user
        with db.begin_nested():
            notify_user(db, int(user_id), title, body, link, actor=actor, event=event,
                        dedupe_key=dedupe, related_type="requirement" if req_id else None,
                        related_id=req_id)
    except Exception:  # noqa: BLE001 — a notice never undoes the action
        logger.warning("HM notice %s to %s failed", event, user_id, exc_info=True)
