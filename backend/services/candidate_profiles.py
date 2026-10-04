"""Candidate profile pipeline: server-side transition map, role authority, serializers.

The transition map and stage-authority map live HERE (server side) — the UI only
renders what GET /api/candidate-profiles/{id} returns in allowed_next_statuses.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, CandidateSkill, Customer,
    InterviewEvent, OfferHistory, OfferStatus, Opportunity, OpportunityCtcSlab,
    PipelineStatus, Requirement, Skill, SkillEvaluation,
)
from services.candidate_status import (
    SOURCING_STAGE, STAGE_LABEL as CANDIDATE_STAGE_LABEL, TA_CLOSE_ACTIONS, TA_HOLD,
    stage_label as candidate_stage_label, statuses_for as candidate_statuses_for,
)
from services.crm_common import log_activity
from services.report_links import ai_report_link

logger = logging.getLogger("karnex.crm.profiles")

PS = PipelineStatus

#: Statuses from which NO further transition is possible.
TERMINAL_STATUSES: set[str] = {
    PS.JOINED.value, PS.SALES_REJECTED.value, PS.RMG_REJECTED.value,
    PS.CUSTOMER_REJECTED.value,
    PS.CUSTOMER_SCREEN_REJECTED.value, PS.CUSTOMER_L1_REJECTED.value,
    PS.CUSTOMER_L2_REJECTED.value,
    PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
}

#: Happy-path forward moves per stage.
_FORWARD: dict[str, list[str]] = {
    PS.SOURCING.value: [PS.TECHNICAL_SCREENING.value],
    PS.TECHNICAL_SCREENING.value: [PS.RMG_REVIEW.value],
    PS.RMG_REVIEW.value: [PS.SALES_SCREENING.value],
    PS.SALES_SCREENING.value: [PS.CUSTOMER_SCREENING.value],
    PS.CUSTOMER_SCREENING.value: [PS.CUSTOMER_INTERVIEW.value],
    # The customer's own ladder: interview happens, then its first round's
    # feedback lands, then its second's, then they shortlist.
    PS.CUSTOMER_INTERVIEW.value: [PS.L1_FEEDBACK.value],
    # Not every customer runs two rounds, so L1 feedback may go straight to a
    # shortlist. Forcing a fictional L2 stage would make the pipeline lie.
    PS.L1_FEEDBACK.value: [PS.L2_FEEDBACK.value, PS.SHORTLISTED.value],
    PS.L2_FEEDBACK.value: [PS.SHORTLISTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_APPROVAL.value],
    # Sales Head's approval hands the candidate to HR for their own round
    # (HR_Screening, 2 Sep 2026); HR moves them on to Preboarding after it.
    # Preboarding is still reachable directly for profiles approved before
    # the stage existed.
    PS.CUSTOMER_APPROVAL.value: [PS.HR_SCREENING.value, PS.PREBOARDING.value],
    # HR Screening → HR Interviewing when TA books the HR round (3 Sep 2026);
    # HR's verdict then moves on to Preboarding. The direct jump stays for
    # profiles whose HR round was recorded before the stage existed.
    PS.HR_SCREENING.value: [PS.HR_INTERVIEWING.value, PS.PREBOARDING.value],
    PS.HR_INTERVIEWING.value: [PS.PREBOARDING.value],
    PS.PREBOARDING.value: [PS.JOINED.value],
}

#: Allowed BACKWARD moves (send a profile back a stage for another look).
_BACKWARD: dict[str, list[str]] = {
    # Customer Screening can bounce the profile back to the Sales team.
    PS.CUSTOMER_SCREENING.value: [PS.SALES_SCREENING.value],
    # Customer Interviewing can send the profile back to Customer Screening
    # (e.g. interview postponed / another shortlist round needed).
    PS.CUSTOMER_INTERVIEW.value: [PS.CUSTOMER_SCREENING.value],
    # A customer round can be re-run — bounce back to the interview stage
    # rather than stranding the profile on a feedback it has superseded.
    PS.L1_FEEDBACK.value: [PS.CUSTOMER_INTERVIEW.value],
    PS.L2_FEEDBACK.value: [PS.L1_FEEDBACK.value],
    # Sales Head sends the offer BACK to Sales to redo the terms (2 Sep 2026)
    # — a wrong rate is not a rejected candidate, and the only exit before
    # this was the terminal Customer_Rejected.
    PS.CUSTOMER_APPROVAL.value: [PS.SHORTLISTED.value],
    # The HR round fell through (candidate no-show, rescheduling) — back to
    # HR Screening so TA can book it again.
    PS.HR_INTERVIEWING.value: [PS.HR_SCREENING.value],
}

#: Stage-specific rejection moves.
_STAGE_REJECTIONS: dict[str, list[str]] = {
    PS.RMG_REVIEW.value: [PS.RMG_REJECTED.value],
    PS.SALES_SCREENING.value: [PS.SALES_REJECTED.value],
    # At Customer Screening the drop can be either side: the customer says no on
    # the RESUME alone (Customer_Screen_Rejected) or Sales pulls the submission
    # (Sales_Rejected). Round-specific values (Aug 2026) replaced the generic
    # Customer_Rejected here so a screen "no" is distinguishable from an
    # interview "no"; the generic stays for Shortlisted / Customer_Approval.
    PS.CUSTOMER_SCREENING.value: [PS.CUSTOMER_SCREEN_REJECTED.value, PS.SALES_REJECTED.value],
    PS.CUSTOMER_INTERVIEW.value: [PS.CUSTOMER_L1_REJECTED.value],
    PS.L1_FEEDBACK.value: [PS.CUSTOMER_L1_REJECTED.value],
    PS.L2_FEEDBACK.value: [PS.CUSTOMER_L2_REJECTED.value],
    PS.SHORTLISTED.value: [PS.CUSTOMER_REJECTED.value],
    PS.CUSTOMER_APPROVAL.value: [PS.CUSTOMER_REJECTED.value],
}

#: Every non-terminal stage can also end in generic withdrawal/rejection —
#: except stages listed here, whose dropdown is kept to its specific set.
_ALWAYS: list[str] = [PS.SELF_WITHDRAWN.value, PS.REJECTED.value]
_NO_GENERIC: set[str] = {PS.CUSTOMER_SCREENING.value}

#: Full transition map: stage -> ordered list of allowed next statuses.
TRANSITION_MAP: dict[str, list[str]] = {
    stage: _FORWARD[stage]
    + _BACKWARD.get(stage, [])
    + _STAGE_REJECTIONS.get(stage, [])
    + ([] if stage in _NO_GENERIC else _ALWAYS)
    for stage in _FORWARD
}

#: Which roles may move a profile OUT of each stage (Admin always may).
STAGE_AUTHORITY: dict[str, set[str]] = {
    PS.SOURCING.value: {"TA"},
    PS.TECHNICAL_SCREENING.value: {"TA"},
    PS.RMG_REVIEW.value: {"RMG"},
    PS.SALES_SCREENING.value: {"Sales"},
    PS.CUSTOMER_SCREENING.value: {"Sales"},
    PS.CUSTOMER_INTERVIEW.value: {"Sales", "Sales_Head"},
    # Sales owns the customer relationship, so Sales records what the customer
    # said at each of its rounds.
    PS.L1_FEEDBACK.value: {"Sales", "Sales_Head"},
    PS.L2_FEEDBACK.value: {"Sales", "Sales_Head"},
    PS.SHORTLISTED.value: {"Sales", "Sales_Head"},
    # Sales Head ONLY (re-confirmed 2 Sep 2026, user decision). This stage is
    # "offer terms awaiting Sales Head's sign-off": Sales submits the
    # candidate's rate and the customer onboarding date, and the person who
    # proposes the terms must not be the one who approves them. Sales Head
    # approves (→ Preboarding, HR notified), sends it back to Sales to redo
    # the terms (→ Shortlisted), or rejects.
    PS.CUSTOMER_APPROVAL.value: {"Sales_Head"},
    # HR's own round (2 Sep 2026): HR records the verdict and moves on.
    PS.HR_SCREENING.value: {"HR", "Sales_Head"},
    PS.HR_INTERVIEWING.value: {"HR", "Sales_Head"},
    PS.PREBOARDING.value: {"HR", "Sales_Head"},
}

#: Stages a profile may not ENTER without meeting a precondition.
#: Enforced in perform_transition, with the reason surfaced to the user.
#:
#: Customer Approval means "these are the terms we are asking the customer to
#: approve". Without an offer on record there are no terms, and Sales Head
#: would be approving an empty proposal.
ENTRY_REQUIREMENTS: dict[str, str] = {
    PS.CUSTOMER_APPROVAL.value: (
        "Record the offer first — the candidate's rate and joining date are what "
        "Sales Head is being asked to approve. Add it on the Offers tab."
    ),
    # Joined creates the Employees record, and that record's address is the
    # official mailbox (user decision, 2 Sep 2026). Without one the employee
    # would be created on the candidate's personal email and need fixing.
    PS.JOINED.value: (
        "Enter the candidate's official (Karnex) email in the Workflow block first — "
        "it becomes their Employees record's address."
    ),
}

#: The rejection/withdrawal states (used by the ?bucket=rejected list filter).
REJECTED_BUCKET: set[str] = {
    PS.SALES_REJECTED.value, PS.RMG_REJECTED.value, PS.CUSTOMER_REJECTED.value,
    PS.CUSTOMER_SCREEN_REJECTED.value, PS.CUSTOMER_L1_REJECTED.value,
    PS.CUSTOMER_L2_REJECTED.value,
    PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
}

#: Which pipeline stages each role should SEE in the profiles list.
#:
#: Distinct from STAGE_AUTHORITY, which is about who may *move* a profile. This
#: is about whose work it is to look at. Sales previously saw every profile from
#: Sourcing onward — candidates TA was still sourcing and RMG was still
#: screening — so the list was mostly other people's in-progress work.
#:
#: Sales sees candidates from the moment RMG hands them over. By then the
#: candidate has cleared AI L1 and any L2 round RMG asked for, which is exactly
#: the "L1 and L2 done" list. Their own rejections stay visible so a Sales
#: rejection does not vanish from the person who made it.
_SALES_VISIBLE: set[str] = {
    PS.SALES_SCREENING.value,
    PS.CUSTOMER_SCREENING.value,
    PS.CUSTOMER_INTERVIEW.value,
    PS.L1_FEEDBACK.value,
    PS.L2_FEEDBACK.value,
    PS.SHORTLISTED.value,
    PS.CUSTOMER_APPROVAL.value,
    PS.HR_SCREENING.value,
    PS.HR_INTERVIEWING.value,
    PS.PREBOARDING.value,
    PS.JOINED.value,
    PS.SALES_REJECTED.value,
    PS.CUSTOMER_REJECTED.value,
    PS.SELF_WITHDRAWN.value,
}

#: Roles whose profile list is scoped. Any role absent from this map sees
#: everything — TA and RMG work across the early stages and need the full view,
#: and Admin/CEO/HR/Finance are unrestricted by design.
#:
#: EMPTY since 18 Aug 2026 (user decision): every CRM role now sees the whole
#: pipeline. The Sales scope above meant a candidate TA had just applied (stage
#: = Sourcing) was invisible to Sales — they could see the opportunity but not
#: who was being lined up for it, which read as data loss. `_SALES_VISIBLE` is
#: kept as the documented definition of "the stages Sales owns" (used by the
#: stage filter chips); re-adding the entries below restores the old scoping.
PROFILE_VISIBILITY: dict[str, set[str]] = {}


def visible_statuses_for(user: CurrentUser) -> set[str] | None:
    """Pipeline statuses this user may see, or None for "everything".

    A user with several roles sees the union of their roles' scopes, and any
    unscoped role (TA, RMG, HR, Finance) lifts the restriction entirely — a
    Sales person who is also RMG must not lose their RMG view.
    """
    if getattr(user, "is_admin", False):
        return None
    roles = list(getattr(user, "roles", []) or [])
    if not roles:
        return None
    allowed: set[str] = set()
    for role in roles:
        if role not in PROFILE_VISIBILITY:
            return None  # an unscoped role — full visibility
        allowed |= PROFILE_VISIBILITY[role]
    return allowed or None


def _status_value(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def allowed_next_statuses(current_status) -> list[str]:
    """All allowed next statuses from a stage (regardless of role)."""
    return list(TRANSITION_MAP.get(_status_value(current_status), []))


def user_may_transition_from(current_status, user: CurrentUser, db: Session | None = None) -> bool:
    if user.is_admin:
        return True
    owners = STAGE_AUTHORITY.get(_status_value(current_status), set())
    if user.roles & owners:
        return True
    # A GM (custom role) holding the screening approval acts as RMG on the
    # stages RMG owns (28 Sep 2026) — see action_permissions.screens_as_rmg.
    if db is not None and "RMG" in owners:
        from services.action_permissions import screens_as_rmg
        return screens_as_rmg(db, user)
    return False


def may_mark_self_withdrawn(profile, user: CurrentUser) -> bool:
    """Target-specific rule for Self_Withdrawn (Aug 2026) — bypasses stage authority.

    The people who HEAR "I'm out" are the TA who sourced the candidate and the
    Sales side that owns the customer conversation — regardless of which stage
    the pipeline currently sits in. So: Admin/CEO, Sales, Sales_Head, and TA —
    but TA only for their OWN profiles (ta_owner_id), except that profiles with
    no recorded owner (pre-attribution rows) are open to any TA.
    """
    if user.is_admin:
        return True
    if user.roles & {"Sales", "Sales_Head"}:
        return True
    if "TA" in user.roles:
        owner = getattr(profile, "ta_owner_id", None)
        return owner is None or owner == user.id
    return False


def allowed_next_statuses_for_user(current_status, user: CurrentUser,
                                   profile=None, db: Session | None = None) -> list[str]:
    """What the transition dropdown should show for THIS user.

    Stage authority decides the normal moves; Self_Withdrawn has its own rule
    (see may_mark_self_withdrawn) — it is ADDED for eligible users even when
    they don't own the stage, and REMOVED for everyone else.
    """
    current = _status_value(current_status)
    base = list(allowed_next_statuses(current)) if user_may_transition_from(current, user, db) else []
    sw = PS.SELF_WITHDRAWN.value
    if sw in TRANSITION_MAP.get(current, []) and profile is not None:
        if may_mark_self_withdrawn(profile, user):
            if sw not in base:
                base.append(sw)
        elif sw in base:
            base.remove(sw)
    return base


def compute_hike_percent(current_ctc, expected_ctc):
    """(expected - current) / current * 100, rounded to 2 decimals; None if not computable."""
    if current_ctc is None or expected_ctc is None:
        return None
    current = Decimal(str(current_ctc))
    expected = Decimal(str(expected_ctc))
    if current == 0:
        return None
    return ((expected - current) / current * Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_profile_or_404(db: Session, profile_id: int) -> CandidateProfile:
    profile = db.get(CandidateProfile, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return profile


def backfill_profile_commercials(db: Session, profile: CandidateProfile) -> bool:
    """Pre-fill a profile's commercials from their real sources when still empty:

      * Current CTC / Expected CTC  ← the Candidate (captured by TA when adding).
      * CTC Approval Amount         ← the linked opportunity's Candidate CTC Slab
                                      (set by Sales — the approved budget).
      * Hike %                      ← derived from Current → Expected.

    Only NULL fields are filled, so RMG's own edits are never overwritten.
    Idempotent; returns True when something changed (caller commits)."""
    changed = False

    if (profile.current_ctc is None or profile.expected_ctc is None) and profile.candidate_id:
        cand = db.get(Candidate, profile.candidate_id)
        if cand is not None:
            # Second-chance source: the candidate's latest resume application
            # details ("12 LPA" etc, from the apply link / TA upload form).
            # Heal the CANDIDATE record too so every page and role sees it.
            if cand.current_ctc is None or cand.expected_ctc is None:
                from models import Resume
                from services.slot_booking import _ctc_from_str
                resume = db.execute(
                    select(Resume)
                    .where(Resume.candidate_id == cand.id, Resume.application_details.isnot(None))
                    .order_by(Resume.id.desc())
                ).scalars().first()
                d = (resume.application_details or {}) if resume is not None else {}
                if cand.current_ctc is None:
                    parsed = _ctc_from_str(d.get("current_ctc"))
                    if parsed is not None:
                        cand.current_ctc = parsed
                        changed = True
                if cand.expected_ctc is None:
                    parsed = _ctc_from_str(d.get("expected_ctc"))
                    if parsed is not None:
                        cand.expected_ctc = parsed
                        changed = True
            if profile.current_ctc is None and cand.current_ctc is not None:
                profile.current_ctc = cand.current_ctc
                changed = True
            if profile.expected_ctc is None and cand.expected_ctc is not None:
                profile.expected_ctc = cand.expected_ctc
                changed = True

    if profile.ctc_approval_amount is None and profile.opportunity_id:
        slabs = db.execute(
            select(OpportunityCtcSlab)
            .where(OpportunityCtcSlab.opportunity_id == profile.opportunity_id)
            .order_by(OpportunityCtcSlab.position)
        ).scalars().all()
        # Pick the band that matches THIS candidate's experience. Taking the first
        # row gave every candidate on an opportunity the junior-most budget.
        cand = db.get(Candidate, profile.candidate_id)
        slab = select_ctc_slab(slabs, getattr(cand, "experience_years", None) if cand else None)
        if slab is None and slabs:
            slab = slabs[0]  # no experience recorded — fall back to the first band
        if slab is not None and slab.approved_ctc_lac is not None:
            profile.ctc_approval_amount = slab.approved_ctc_lac
            changed = True

    if (profile.hike_percent is None
            and profile.current_ctc is not None and profile.expected_ctc is not None):
        hp = compute_hike_percent(profile.current_ctc, profile.expected_ctc)
        if hp is not None:
            profile.hike_percent = hp
            changed = True

    return changed


#: Who takes over when a profile ARRIVES at each stage. Used to tell the next
#: role there is work waiting for them.
#:
#: This was the missing link in the TA -> RMG -> Sales handoff: RMG was notified
#: when an AI interview passed, but when RMG then forwarded the candidate,
#: nothing told Sales. Sales had to notice by browsing the profiles list, which
#: is how handoffs quietly stall.
_ARRIVAL_NOTIFY_ROLE: dict[str, str] = {
    PS.RMG_REVIEW.value: "RMG",
    PS.SALES_SCREENING.value: "Sales",
    PS.CUSTOMER_SCREENING.value: "Sales",
    PS.CUSTOMER_INTERVIEW.value: "Sales",
    PS.L1_FEEDBACK.value: "Sales",
    PS.L2_FEEDBACK.value: "Sales",
    # Customer Approval is now Sales Head's decision, so it is Sales Head who
    # needs telling that an offer is waiting on them.
    PS.CUSTOMER_APPROVAL.value: "Sales_Head",
    PS.HR_SCREENING.value: "HR",
    PS.PREBOARDING.value: "HR",
}

#: Human wording for the arrival message, so each role is told what to DO rather
#: than just that a status changed.
_ARRIVAL_ACTION: dict[str, str] = {
    PS.RMG_REVIEW.value: "Review the interview report and decide: request an L2 round, "
                         "or submit to the Sales team.",
    PS.SALES_SCREENING.value: "RMG has cleared this candidate. Review and submit to the customer.",
    PS.CUSTOMER_SCREENING.value: "Submitted to the customer — track the response.",
    PS.CUSTOMER_INTERVIEW.value: "A customer interview is due for this candidate.",
    PS.L1_FEEDBACK.value: "The customer's first-round feedback is in. Record it, then move "
                          "to their second round or straight to a shortlist.",
    PS.L2_FEEDBACK.value: "The customer's second-round feedback is in. Record it, then "
                          "shortlist or reject.",
    PS.CUSTOMER_APPROVAL.value: "Offer terms are ready for your approval. Review the rate "
                                "and onboarding date, edit if needed, then approve to hand "
                                "this candidate to HR (HR Discussion) — or send the terms back.",
    PS.HR_SCREENING.value: "Approved by Sales Head. Review the candidate's details, then click "
                           "\"Request HR round\" so TA books it with the candidate. Once it is "
                           "held, record Hire / Not Recommend on the Interviews tab.",
    PS.PREBOARDING.value: "HR round done — re-check the CTCs and the customer onboarding date. "
                          "In budget: complete onboarding and mark Joined. Not in budget: flag it "
                          "to Sales from the profile.",
}


#: Stages decided by an APPROVAL — the arrival notice also reaches whoever
#: `user_may` do it (a template's / custom role's Approvals), not only the role.
_ARRIVAL_APPROVAL: dict[str, str] = {
    PS.CUSTOMER_APPROVAL.value: "profile.sales_head_decision",
}


def stage_arrival_event(status: str) -> str:
    """The notification-route key for a candidate arriving at `status`.

    e.g. "Preboarding" -> "candidate.stage_arrival.preboarding". Shared by the
    sender here and the Email Flows catalogue, so the Users tab lists exactly
    the keys that fire.
    """
    return f"candidate.stage_arrival.{status.lower()}"


def _notify_stage_owner(db: Session, profile: CandidateProfile, previous: str,
                        new_status: str, comment: str, user: CurrentUser) -> None:
    """Tell whoever owns the new stage that a candidate has arrived there.

    Best-effort: a notification failure must never roll back a legitimate status
    change. The candidate has moved either way, and losing the transition would
    be far worse than losing the alert.
    """
    role = _ARRIVAL_NOTIFY_ROLE.get(new_status)
    if not role:
        return
    try:
        from services.notify import notify_role

        name = _candidate_display_name(db, profile)
        # Everyone who may ACT at the new stage hears it, not only the role by
        # name (29 Sep 2026): a Sales Manager / GM whose approval comes from a
        # template or custom role decides "Pending Sales Head Approval" too.
        approvers: list[int] = []
        action = _ARRIVAL_APPROVAL.get(new_status)
        if action:
            from services.action_permissions import user_ids_who_may
            approvers = [uid for uid in user_ids_who_may(db, action) if uid != user.id]
        notify_role(
            db, role,
            f"{candidate_stage_label(new_status)}: {name}",
            f"{_ARRIVAL_ACTION.get(new_status, 'This candidate needs your attention.')} "
            f"(moved from {candidate_stage_label(previous)} — “{comment}”)",
            (applied_candidates_link(db, profile) if role in ("RMG", "TA")
             else f"/admin/?view=crm&p=profiles/{profile.id}"),
            # Don't notify the person who just made the change.
            exclude_user_id=user.id,
            actor=user,
            # ONE EVENT KEY PER STAGE (2 Sep 2026, user report: HR never heard
            # about a Pre Onboarding hand-off). Every arrival used to share
            # "candidate.stage_arrival", so a single admin-edited route for it
            # replaced the receiving role for EVERY stage — Sales's route
            # silently ate HR's hand-off. Now each stage has its own key, so a
            # route for one cannot redirect another, and an unrouted stage
            # falls back to the owner in code.
            event=stage_arrival_event(new_status),
            user_ids=approvers or None,
        )
    except Exception:  # pragma: no cover — never break a transition
        logger.warning("Could not notify %s about profile %s", role, profile.id, exc_info=True)


def _candidate_display_name(db: Session, profile: CandidateProfile) -> str:
    try:
        candidate = db.get(Candidate, profile.candidate_id)
        if candidate is None:
            return f"Candidate #{profile.candidate_id}"
        name = " ".join(
            p for p in (candidate.first_name, candidate.last_name) if p
        ).strip()
        return name or f"Candidate #{candidate.id}"
    except Exception:
        return f"Profile #{profile.id}"


def _check_entry_requirement(db: Session, profile: CandidateProfile, new_status: str) -> None:
    """Block entry to a stage whose precondition is unmet, with the reason."""
    reason = ENTRY_REQUIREMENTS.get(new_status)
    if not reason:
        return
    if new_status == PS.CUSTOMER_APPROVAL.value:
        has_offer = db.execute(
            select(OfferHistory.id).where(OfferHistory.profile_id == profile.id).limit(1)
        ).first()
        if not has_offer:
            raise HTTPException(status_code=400, detail=reason)
    elif new_status == PS.JOINED.value:
        if not (getattr(profile, "official_email", None) or "").strip():
            raise HTTPException(status_code=400, detail=reason)


#: Destination status -> the customer's verdict, in the Interview_Result
#: vocabulary. Only outcomes that genuinely express a decision map; a bounce
#: back to an earlier stage is a reschedule, not a verdict.
_CUSTOMER_VERDICT: dict[str, str] = {
    PS.SHORTLISTED.value: "Hire",
    PS.CUSTOMER_APPROVAL.value: "Hire",
    PS.CUSTOMER_REJECTED.value: "No Hire",
    # Round-specific rejections close the ladder with the same "No Hire"
    # verdict — the note is the customer's feedback, recorded on that round.
    PS.CUSTOMER_L1_REJECTED.value: "No Hire",
    PS.CUSTOMER_L2_REJECTED.value: "No Hire",
}

#: Arriving at one of these means that customer round's feedback is in, so the
#: text typed on the transition IS that round's feedback. Stored on the round's
#: `stage` column so the customer's first and second rounds stay distinct
#: without inventing new interview kinds.
_FEEDBACK_STAGE_ROUND: dict[str, str] = {
    PS.L1_FEEDBACK.value: "L1",
    PS.L2_FEEDBACK.value: "L2",
}


def _record_customer_round_from_transition(db: Session, profile: CandidateProfile,
                                           previous: str, new_status: str,
                                           feedback: str, user: CurrentUser) -> None:
    """Persist the customer's closing verdict typed on a transition.

    LEAVING the customer's ladder with a decision (shortlist / approve /
    reject) — the text is the closing verdict, applied to the most recent
    customer round. (Arriving at L1/L2 Feedback used to write a round too —
    removed 29 Sep 2026, see below.)

    Without this the Interviews tab showed RMG's technical rounds and then
    stopped, while the customer's actual words lived only in the activity log.

    Best-effort: bookkeeping must never roll back a legitimate status change.
    """
    # ⚠️ 29 Sep 2026 (user report): ARRIVING at L1 / L2 Feedback no longer
    # writes a round. The UI names those stages "Customer L1 / L2 Interview",
    # so Sales uses the move to line the round UP (with the customer's slots),
    # not to report it — and the recorder created a "Completed" Customer L2 row
    # with no time and no verdict, which read as "Customer L2 – Scheduled ·
    # Time not set" and hid TA's "Schedule Customer L2" button. A round is
    # booked by TA and its verdict recorded on the round itself; the note on
    # the move stays in the activity log (STATUS_CHANGE). Migration 0113
    # removes the placeholders already written.
    try:
        if new_status in _FEEDBACK_STAGE_ROUND:
            return

        # Closing decision — only meaningful if the customer actually saw them.
        if previous not in _CUSTOMER_LADDER:
            return
        verdict = _CUSTOMER_VERDICT.get(new_status)
        if not verdict:
            return
        _upsert_customer_round(db, profile, user,
                               stage=_FEEDBACK_STAGE_ROUND.get(previous),
                               feedback=feedback, verdict=verdict)
    except Exception:  # pragma: no cover — never break a transition
        logger.warning("Could not record customer round for profile %s", profile.id, exc_info=True)


#: Stages at which the customer has the candidate in front of them.
_CUSTOMER_LADDER = {
    PS.CUSTOMER_INTERVIEW.value,
    PS.L1_FEEDBACK.value,
    PS.L2_FEEDBACK.value,
}


#: The round KIND that owns each customer stage. The Interviews form writes
#: these kinds, so the transition recorder must reuse them — matching on
#: `stage` alone created a SECOND row for the same conversation (the duplicate
#: "Customer L1 / Customer L2" cards reported 27 Aug 2026).
_CUSTOMER_ROUND_KIND = {"L1": "Customer_Interview", "L2": "Customer_L2"}


def _upsert_customer_round(db: Session, profile: CandidateProfile, user: CurrentUser, *,
                           stage: str | None, feedback: str, verdict: str | None) -> None:
    """Create or update the customer round for this stage.

    ONE row per customer round: whatever Sales already saved from the
    Interviews tab is updated in place, whether it was stored with the round's
    kind (the form) or only with a stage (older transition-recorded rows).
    """
    from sqlalchemy import and_ as sa_and, or_ as sa_or

    kind = _CUSTOMER_ROUND_KIND.get(stage or "", "Customer_Interview")
    if stage:
        # Same conversation whether it was saved as the kind (form) or as a
        # stage-tagged legacy row (old transition recorder).
        match = sa_or(
            InterviewEvent.kind == kind,
            sa_and(InterviewEvent.kind == "Customer_Interview",
                   InterviewEvent.stage == stage),
        )
    else:
        match = InterviewEvent.kind.in_(tuple(_CUSTOMER_ROUND_KIND.values()))
    existing = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id == profile.id, match)
        .order_by(InterviewEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    if existing is not None:
        if verdict:
            existing.result = existing.result or verdict
        existing.status = existing.status or "Completed"
        # Append rather than overwrite — Sales may have written the round up
        # in detail already, and a one-line decision note should not replace it.
        if feedback and feedback not in (existing.feedback or ""):
            existing.feedback = (
                f"{existing.feedback}\n\n{feedback}".strip() if existing.feedback else feedback
            )
        return

    db.add(InterviewEvent(
        profile_id=profile.id,
        candidate_id=profile.candidate_id,
        created_by=user.id,
        kind=kind,
        stage=stage,
        interview_category="External",
        status="Completed",
        result=verdict,
        feedback=feedback,
        user_role="Customer",
    ))


#: Customer round -> the pipeline stage its feedback proves has happened.
#: Saving Customer L1/L2 feedback IS the status change (user decision,
#: 27 Aug 2026) — Sales had to record the round and then repeat themselves in
#: the status dropdown, and the two drifted apart whenever they forgot.
_ROUND_ADVANCES_TO = {
    "Customer_Interview": PS.L1_FEEDBACK.value,
    "Customer_L2": PS.L2_FEEDBACK.value,
}

#: The customer ladder, in order — used to walk a profile forward one hop at a
#: time so every intermediate stage is stamped and logged honestly.
_CUSTOMER_LADDER_ORDER = [
    PS.SALES_SCREENING.value,
    PS.CUSTOMER_SCREENING.value,
    PS.CUSTOMER_INTERVIEW.value,
    PS.L1_FEEDBACK.value,
    PS.L2_FEEDBACK.value,
]


def advance_status_for_customer_round(db: Session, profile: CandidateProfile,
                                      kind: str, feedback: str, user: CurrentUser) -> str | None:
    """Move the profile to the stage a saved customer round proves.

    Customer L1 feedback → L1 Feedback; Customer L2 feedback → L2 Feedback,
    walking any intermediate stage so the timeline stays continuous. Applies
    the transition DIRECTLY rather than through perform_transition: the
    round-write permission already authorised the writer (Sales, Sales_Head or
    the coordinating TA), and perform_transition would re-record the round —
    the very duplication this change removes.

    NEVER moves backwards, never touches a terminal profile, and never raises:
    a bookkeeping convenience must not cost someone their feedback.
    Returns the new status when it moved, else None. Caller commits.
    """
    target = _ROUND_ADVANCES_TO.get(kind or "")
    if target is None:
        return None
    try:
        current = _status_value(profile.pipeline_status)
        if current in TERMINAL_STATUSES:
            return None
        if current not in _CUSTOMER_LADDER_ORDER:
            return None  # e.g. already Shortlisted / Customer Approval — leave it
        here = _CUSTOMER_LADDER_ORDER.index(current)
        want = _CUSTOMER_LADDER_ORDER.index(target)
        if want <= here:
            return None  # already at or past this round's stage
        with db.begin_nested():
            for step in _CUSTOMER_LADDER_ORDER[here + 1:want + 1]:
                previous = _status_value(profile.pipeline_status)
                profile.pipeline_status = PS(step)
                _stamp_workflow_dates(db, profile, step)
                log_activity(
                    db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                    "STATUS_CHANGE",
                    f"{previous} -> {step}: auto-updated from {round_label_for(kind)} feedback"
                    + (f" — {feedback.strip()[:160]}" if (feedback or "").strip() else ""),
                )
            _notify_stage_owner(db, profile, current, target, feedback or "", user)
        return target
    except Exception:  # pragma: no cover — never break saving feedback
        logger.warning("auto status advance failed for profile %s", profile.id, exc_info=True)
        return None


#: A round verdict that ENDS the candidacy (7 Sep 2026, user report: a
#: customer L1 "No Hire" left the profile at L1 Feedback, so TA was still
#: offered "Schedule Customer L2"). The customer's or RMG's "No Hire" IS the
#: decision — the profile closes at that round's rejection status, which is
#: terminal, and the TA owner hears about it. "Leaning No" is not decisive
#: and leaves the stage alone for a human call.
NEGATIVE_ROUND_RESULTS = ("No Hire",)
_ROUND_REJECTS_TO: dict[str, str] = {
    "Customer_Interview": PS.CUSTOMER_L1_REJECTED.value,
    "Customer_L2": PS.CUSTOMER_L2_REJECTED.value,
    "L1_Interview": PS.RMG_REJECTED.value,
    "L2_F2F": PS.RMG_REJECTED.value,
}


def reject_on_round_verdict(db: Session, profile: CandidateProfile, kind: str, result: str,
                            feedback: str, user: CurrentUser) -> str | None:
    """A "No Hire" on a customer or RMG round closes the profile at that
    round's rejection status. Walks the customer ladder first when needed
    (a customer L1 verdict recorded straight from Customer Screening still
    lands on Customer_L1_Rejected). Never touches a terminal profile, never
    raises. Returns the new status when it moved, else None. Caller commits.
    """
    if (result or "").strip() not in NEGATIVE_ROUND_RESULTS:
        return None
    target = _ROUND_REJECTS_TO.get(kind or "")
    if target is None:
        return None
    try:
        current = _status_value(profile.pipeline_status)
        if current in TERMINAL_STATUSES:
            return None
        # Reach a stage from which this rejection is a legal transition.
        if target not in TRANSITION_MAP.get(current, []):
            stage_for = _ROUND_ADVANCES_TO.get(kind)
            if stage_for and current in _CUSTOMER_LADDER_ORDER and stage_for in _CUSTOMER_LADDER_ORDER:
                advance_status_for_customer_round(db, profile, kind, feedback, user)
                current = _status_value(profile.pipeline_status)
        if target not in TRANSITION_MAP.get(current, []):
            logger.info("round %s No Hire on profile %s at %s: no rejection path, left as is",
                        kind, profile.id, current)
            return None
        note = f"{round_label_for(kind)}: No Hire" + (
            f" — {feedback.strip()[:160]}" if (feedback or "").strip() else "")
        with db.begin_nested():
            profile.pipeline_status = PS(target)
            _stamp_workflow_dates(db, profile, target)
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                         "STATUS_CHANGE", f"{current} -> {target}: {note}")
            _notify_rejection(db, profile, target, note, user)
        return target
    except Exception:  # pragma: no cover — never break saving feedback
        logger.warning("round-verdict rejection failed for profile %s", profile.id, exc_info=True)
        return None


def _notify_rejection(db: Session, profile: CandidateProfile, new_status: str,
                      note: str, user: CurrentUser) -> None:
    """Tell the TA who sourced the candidate (and Sales, for customer rounds)
    that the candidacy closed — rejections used to notify nobody."""
    try:
        from services.notify import notify_role, notify_user

        name = _candidate_display_name(db, profile)
        title = f"{new_status.replace('_', ' ')}: {name}"
        link = applied_candidates_link(db, profile)
        if profile.ta_owner_id and profile.ta_owner_id != user.id:
            notify_user(db, profile.ta_owner_id, title, note, link, actor=user,
                        event="candidate.round_rejected")
        if new_status.startswith("Customer_"):
            notify_role(db, "Sales", title, note, link, exclude_user_id=user.id, actor=user,
                        event="candidate.round_rejected")
    except Exception:  # pragma: no cover
        logger.warning("Could not notify rejection for profile %s", profile.id, exc_info=True)


#: HR-round verdicts (3 Sep 2026, user decision). Either one moves the
#: profile to Preboarding; "Not Recommend" means HR expects a CTC / joining
#: issue and marks the budget flag so the Pre-Onboarding banner asks for it.
HR_ROUND_RESULTS = ("Hire", "Not Recommend", "Drop")
#: The candidate declined (better offer elsewhere) — closes the profile.
HR_VERDICT_DROP = "Drop"

BUDGET_CONCERN = "Concern"
BUDGET_OUT = "Out_of_Budget"
BUDGET_RESOLVED = "Resolved"


def _auto_move(db: Session, profile: CandidateProfile, target: str, user: CurrentUser,
               reason: str, *, allow_exit: bool = False) -> str | None:
    """Move ONE forward step without the stage-authority check.

    For system moves that follow an action the caller was already authorised
    to take (TA booking the HR round, HR saving the verdict). Validated
    against the transition map, never backwards, never from a terminal stage,
    and never raises — bookkeeping must not cost the action. Returns the new
    status, or None when nothing moved. Caller commits.

    `allow_exit` widens the check to the full transition map so a verdict can
    close the profile (HR's "Drop" → Self Withdrawn); still never from a
    terminal stage.
    """
    try:
        current = _status_value(profile.pipeline_status)
        if current in TERMINAL_STATUSES or current == target:
            return None
        allowed = TRANSITION_MAP.get(current, []) if allow_exit else _FORWARD.get(current, [])
        if target not in allowed:
            return None
        with db.begin_nested():
            profile.pipeline_status = PS(target)
            _stamp_workflow_dates(db, profile, target)
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                         "STATUS_CHANGE", f"{current} -> {target}: {reason}")
            _notify_stage_owner(db, profile, current, target, reason, user)
        return target
    except Exception:  # pragma: no cover — never break the triggering action
        logger.warning("auto move to %s failed for profile %s", target, profile.id, exc_info=True)
        return None


def advance_on_hr_round_scheduled(db: Session, profile: CandidateProfile,
                                  user: CurrentUser, when: str = "") -> str | None:
    """TA booked the HR round → HR Screening becomes HR Interviewing."""
    if _status_value(profile.pipeline_status) != PS.HR_SCREENING.value:
        return None
    return _auto_move(db, profile, PS.HR_INTERVIEWING.value, user,
                      "HR round scheduled" + (f" for {when}" if when else ""))


def advance_on_hr_verdict(db: Session, profile: CandidateProfile, result: str,
                          feedback: str, user: CurrentUser) -> str | None:
    """HR saved the round's verdict → Preboarding, whichever way it went.

    "Not Recommend" additionally raises the budget CONCERN flag: per the user
    flow it means the expected CTC or the joining date is the problem, and HR
    will flag it to Sales from Pre-Onboarding with the corrected figures.

    "Drop" is the exception: the candidate is not continuing (a better offer
    elsewhere), so the profile closes as Self Withdrawn instead.
    """
    current = _status_value(profile.pipeline_status)
    if current not in (PS.HR_SCREENING.value, PS.HR_INTERVIEWING.value):
        return None
    if result == HR_VERDICT_DROP:
        note = "HR round verdict: Drop — candidate not continuing" + (
            f" — {feedback.strip()[:160]}" if (feedback or "").strip() else "")
        return _auto_move(db, profile, PS.SELF_WITHDRAWN.value, user, note, allow_exit=True)
    if result == "Not Recommend" and profile.budget_status in (None, TA_HOLD):
        profile.budget_status = BUDGET_CONCERN
        profile.budget_note = (feedback or "").strip() or None
    note = f"HR round verdict: {result}" + (f" — {feedback.strip()[:160]}" if (feedback or "").strip() else "")
    if current == PS.HR_SCREENING.value:
        # A verdict recorded before the schedule step (legacy or same-day): walk
        # through HR Interviewing so the timeline stays continuous.
        _auto_move(db, profile, PS.HR_INTERVIEWING.value, user, "HR round held")
    return _auto_move(db, profile, PS.PREBOARDING.value, user, note)


#: Stages a candidate can be handed to RMG from — everything before the AI L1
#: has run. Sourcing is included because a candidate applied from the
#: Candidates page never passes through Technical_Screening at all.
_PRE_RMG_STAGES = [PS.SOURCING.value, PS.TECHNICAL_SCREENING.value]


def hand_off_to_rmg_review(db: Session, profile: CandidateProfile,
                           user: CurrentUser, reason: str) -> str | None:
    """Move a pre-RMG profile to RMG Review, walking every stage in between.

    THE AI L1 IS OPTIONAL (user decision, 28 Aug 2026): RMG may skip it and
    review the candidate on their CV alone, then run the manual rounds. This
    is the door into that path — and the same walk the passed-L1 hand-off
    uses, so both arrive at one stage with one meaning.

    Applied directly rather than through perform_transition: stage authority
    for Sourcing/Technical_Screening belongs to TA, and this is deliberately
    RMG's own call. Returns the new status, or None when nothing moved.
    """
    current = _status_value(profile.pipeline_status)
    if current == PS.RMG_REVIEW.value:
        return None  # already there — the caller just re-clicked
    if current not in _PRE_RMG_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Only a candidate still before RMG review can be handed over "
                   f"(this one is at '{current.replace('_', ' ')}').",
        )
    for step in _PRE_RMG_STAGES[_PRE_RMG_STAGES.index(current) + 1:] + [PS.RMG_REVIEW.value]:
        previous = _status_value(profile.pipeline_status)
        profile.pipeline_status = PS(step)
        _stamp_workflow_dates(db, profile, step)
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "STATUS_CHANGE", f"{previous} -> {step}: {reason}")
    return PS.RMG_REVIEW.value


def round_label_for(kind: str) -> str:
    """Human label for a round kind, without importing the whole vocabulary."""
    from services.interview_rounds import round_label
    try:
        return round_label(kind)
    except Exception:
        return kind


#: Minimum length when a note IS required.
MIN_COMMENT_LENGTH = 5

# ---------------------------------------------------------------------------
# RMG screening gate (25 Aug 2026): TA applies → RMG clears → AI L1 unlocked.
# A PARALLEL status, not a pipeline stage. NULL = legacy profile, not gated.
# ---------------------------------------------------------------------------

RMG_SCREENING_PENDING = "Pending"
RMG_SCREENING_SHORTLISTED = "Shortlisted"
RMG_SCREENING_REJECTED = "Rejected"
RMG_SCREENING_VALUES = (RMG_SCREENING_PENDING, RMG_SCREENING_SHORTLISTED, RMG_SCREENING_REJECTED)


def rmg_gate_enabled() -> bool:
    """Admin toggle (Settings → hiring.rmg_screening_gate). Off = no gating,
    no Pending stamping, no RMG notifications — rollout without a deploy."""
    try:
        from services.org_settings import setting_bool
        return setting_bool("hiring.rmg_screening_gate")
    except Exception:
        return True  # the gate is the safe default


def rmg_screening_blocks_l1(profile) -> str | None:
    """The reason AI-L1 actions are blocked for this profile, or None.

    Enforced SERVER-SIDE (the greyed buttons in the UI are a convenience, not
    a boundary): scheduling the AI interview and sending slot invites both
    refuse until RMG / GM shortlisted — not yet sent by TA (28 Sep 2026),
    Pending, or Rejected.
    """
    if not rmg_gate_enabled():
        return None
    status = getattr(profile, "rmg_screening_status", None)
    if status is None and _status_value(getattr(profile, "pipeline_status", None)) in TA_DECISION_STAGES:
        return ("TA has not sent this candidate for Technical Screening yet. Press "
                "\"Technical Screening\" on the Applied Candidates row — RMG / GM decide "
                "whether they fit before any L1 is scheduled.")
    if status == RMG_SCREENING_PENDING:
        return ("This candidate is awaiting RMG screening. RMG must review and "
                "mark them Shortlisted before the AI L1 interview can be scheduled.")
    if status == RMG_SCREENING_REJECTED:
        note = (getattr(profile, "rmg_screening_note", None) or "").strip()
        return ("RMG rejected this candidate at screening"
                + (f": {note}" if note else "")
                + ". The AI L1 interview cannot be scheduled.")
    return None


def manual_route_blocks_slot_invite(db: Session, profile) -> str | None:
    """The reason the AI-L1 slot invite must NOT go out for this profile, or None.

    3 Sep 2026 (user report): RMG chose the manual interview route for a
    candidate — "Go manual" logs L1_REQUESTED and TA books a human L1 — and
    the candidate STILL received "pick your interview slot" with the AI
    booking link, because the slot invite fires from the resume side (ATS
    auto-threshold, TA's Send-invite button) with no idea what RMG decided on
    the profile side. The manual route is recorded as either the L1_REQUESTED
    activity or an actual L1_Interview round; either one means no AI L1, so
    no slot link. Also true once the candidate is past the AI stage
    entirely — a booking link after RMG Review is only confusing.
    """
    if profile is None:
        return None
    from models import CandidateProfileActivityLog, InterviewEvent
    manual = db.execute(
        select(CandidateProfileActivityLog.id)
        .where(CandidateProfileActivityLog.profile_id == profile.id,
               CandidateProfileActivityLog.action_type == "L1_REQUESTED")
        .limit(1)
    ).first() is not None
    if not manual:
        manual = db.execute(
            select(InterviewEvent.id)
            .where(InterviewEvent.profile_id == profile.id,
                   InterviewEvent.kind == "L1_Interview")
            .limit(1)
        ).first() is not None
    if manual:
        return ("RMG chose the MANUAL interview route for this candidate — the AI L1 "
                "slot invite is not sent. TA schedules the L1 interview instead "
                "(Applied Candidates → Schedule L1).")
    status = _status_value(getattr(profile, "pipeline_status", None))
    past_ai = {
        PipelineStatus.RMG_REVIEW.value, PipelineStatus.SALES_SCREENING.value,
        PipelineStatus.CUSTOMER_SCREENING.value, PipelineStatus.CUSTOMER_INTERVIEW.value,
        PipelineStatus.L1_FEEDBACK.value, PipelineStatus.L2_FEEDBACK.value,
        PipelineStatus.SHORTLISTED.value, PipelineStatus.CUSTOMER_APPROVAL.value,
        PipelineStatus.HR_SCREENING.value, PipelineStatus.PREBOARDING.value,
        PipelineStatus.JOINED.value,
    }
    if status in past_ai:
        return (f"This candidate is already at {status.replace('_', ' ')} — past the AI L1 "
                "stage, so the slot invite is not sent.")
    return None


def applied_candidates_link(db: Session, profile, tab: str = "resumes") -> str:
    """Deep link for TA / RMG recruiting notifications (8 Sep 2026, user
    report): the Applied Candidates tab of the requirement the profile sits
    on, with the search prefilled to the candidate — that row carries the
    Shortlist / Reject / Schedule / feedback / Submit-to-Sales buttons the
    mail asks the reader to press. The profile page was where every link
    landed before, and the reader then had to hunt for the requirement.
    Falls back to the profile page when the opportunity has no requirement
    (e.g. a deal still pending approval)."""
    try:
        from urllib.parse import quote
        from models import Requirement
        req_id = db.execute(
            select(Requirement.id).where(Requirement.opportunity_id == profile.opportunity_id)
            .order_by(Requirement.id.desc()).limit(1)
        ).scalar()
        if req_id:
            cand = db.get(Candidate, profile.candidate_id)
            q = (getattr(cand, "email", None) or "").strip()
            if not q or q.lower().endswith("@import.karnex.in"):
                q = " ".join(x for x in (getattr(cand, "first_name", None),
                                         getattr(cand, "last_name", None)) if x).strip()
            return (f"/admin/?view=crm&p=requirements/{req_id}&tab={tab}"
                    + (f"&q={quote(q)}" if q else ""))
    except Exception:
        logger.debug("applied_candidates_link fallback for profile %s", getattr(profile, "id", "?"), exc_info=True)
    return f"/admin/?view=crm&p=profiles/{profile.id}"


#: The routable event behind every "applicants are waiting for screening"
#: notice — the single apply, the bulk-ZIP summary and the SLA reminder.
RMG_SCREENING_REQUESTED_EVENT = "profile.rmg_screening_requested"
RMG_SCREENING_SLA_EVENT = "profile.rmg_screening_sla"


def screening_notify_user_ids(db: Session) -> set[int]:
    """Everyone who may actually SCREEN (`profile.rmg_screening`) — the RMG role,
    the GM custom role, and anyone whose template's Approvals grant it.

    Reported 26 Sep 2026: a TA applied a candidate and neither RMG nor the GM
    heard. The notice was addressed to the role NAME "RMG" alone, so a GM (a
    custom role) and an RMG whose access comes from a template never made the
    list. The same `user_may` that gates the Shortlist button decides here, so
    the recipients can never drift from who can act (the 25 Sep timesheet rule).
    Never raises — a lookup failure means "nobody extra", not a failed apply."""
    from services.action_permissions import user_ids_who_may
    return user_ids_who_may(db, "profile.rmg_screening")


def notify_rmg_new_applicant(db: Session, profile, actor=None) -> None:
    """Bell + email to everyone who screens (RMG · GM · templated approvers)
    when a TA applies a candidate to an opportunity.

    Carries the candidate's details for THIS opportunity so the screener can
    decide from the notification alone; the link lands on the requirement's
    Applied Candidates tab, on the candidate's row, where the Shortlist / Reject
    buttons live. Best-effort — never blocks the apply."""
    try:
        from services.notify import notify_role

        candidate = db.get(Candidate, profile.candidate_id)
        opp = db.get(Opportunity, profile.opportunity_id)
        cname = " ".join(p for p in (
            getattr(candidate, "first_name", None), getattr(candidate, "last_name", None),
        ) if p) or f"Candidate #{profile.candidate_id}"
        opp_label = f"{getattr(opp, 'opp_id', '')} — {getattr(opp, 'title', '')}".strip(" —")
        skills = db.execute(
            select(Skill.name)
            .join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
            .where(CandidateSkill.candidate_id == profile.candidate_id)
            .limit(12)
        ).scalars().all()
        details = [
            f"Opportunity: {opp_label}",
            f"Email: {getattr(candidate, 'email', None) or '—'}",
            f"Phone: {getattr(candidate, 'phone', None) or '—'}",
            f"Experience: {getattr(candidate, 'experience_years', None) or '—'} yrs",
            f"Skills: {', '.join(skills) if skills else '—'}",
            f"Applied by: {getattr(profile, 'ta_owner_name', None) or 'TA'}",
        ]
        notify_role(
            db, "RMG",
            f"RMG screening needed: {cname}",
            " · ".join(details) + " — review on the Screening Desk and Shortlist to choose "
            "the interview route (AI L1 or manual L1).",
            applied_candidates_link(db, profile),
            event=RMG_SCREENING_REQUESTED_EVENT,
            actor=actor,
            dedupe_prefix=f"rmg_screen:{profile.id}",
            related_type="candidate",
            related_id=profile.candidate_id,
            user_ids=screening_notify_user_ids(db),
        )
    except Exception:
        logger.warning("notify_rmg_new_applicant failed for profile %s",
                       getattr(profile, "id", "?"), exc_info=True)


def comment_required_for(current: str, new_status: str) -> bool:
    """Does this transition need a written note?

    Requiring one on EVERY move made the field noise on routine progress —
    "moving to Customer Screening" adds nothing the status change does not
    already say, so people type "ok" to get past it, and the habit devalues
    the notes that matter.

    A note is required where it carries information nothing else records:

      * rejections and withdrawals — why someone was dropped is not
        recoverable from the status alone
      * backward moves — going back is an exception and needs explaining
      * closing the customer's ladder — the note is the customer's verdict

    Moving on to the customer's L1 / L2 round is routine progress (29 Sep 2026,
    user decision): the round's feedback is recorded on the round, so the move
    asks for none.
    """
    if new_status in REJECTED_BUCKET:
        return True
    if new_status in _BACKWARD.get(current, []):
        return True
    # Closing the customer's ladder: the note is the customer's verdict.
    if current in _CUSTOMER_LADDER and new_status in _CUSTOMER_VERDICT:
        return True
    return False


#: Workflow dates the pipeline already knows and should stamp for itself.
#: Each is "the day this profile was handed to that team", so the day of the
#: move is the correct value — asking a human to retype it invites drift.
_STAGE_DATE_STAMPS: dict[str, str] = {
    PS.TECHNICAL_SCREENING.value: "technical_submission_date",
    PS.SALES_SCREENING.value: "sales_submission_date",
    PS.CUSTOMER_SCREENING.value: "customer_submission_date",
}


def stamp_technical_submission(profile: CandidateProfile) -> None:
    """The day the candidate reached Technical Screening (29 Sep 2026).

    Since TA hands candidates over with the "Technical Screening" button, the
    profile stays at the Sourcing STAGE while RMG / GM screen it, so the stage
    stamp below never fired and the Hand-offs panel read "Technical screening —
    Not yet" for a candidate already with Sales. The hand-over itself (send ·
    a screening decision · a screener's own add · a fast-track) now stamps it.
    Only ever fills a blank, like every workflow date."""
    if getattr(profile, "technical_submission_date", None) is None:
        profile.technical_submission_date = date.today()


def _stamp_workflow_dates(db: Session, profile: CandidateProfile, new_status: str) -> None:
    """Fill the workflow dates this transition establishes.

    Only ever fills a blank. Stepping back and forward again must not overwrite
    the first submission date — that original is what turnaround time is
    measured from, and silently resetting it would flatter the numbers.
    """
    field = _STAGE_DATE_STAMPS.get(new_status)
    if field and getattr(profile, field, None) is None:
        setattr(profile, field, date.today())

    # Onboarding is a planned future date, not the date of this move, so it is
    # taken from the offer rather than stamped as today.
    if new_status == PS.PREBOARDING.value and profile.customer_onboarding_date is None:
        joining = db.execute(
            select(OfferHistory.joining_date)
            .where(OfferHistory.profile_id == profile.id,
                   OfferHistory.joining_date.isnot(None))
            .order_by(OfferHistory.offer_date.desc(), OfferHistory.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if joining is not None:
            profile.customer_onboarding_date = joining


def record_stage_arrival(db: Session, profile: CandidateProfile, previous: str,
                         new_status: str, comment: str, user: CurrentUser) -> None:
    """The side effects of arriving at a stage WITHOUT the transition map.

    For the one sanctioned jump the map does not model — an internal candidate
    fast-tracked from TA / RMG straight to Sales (Screening Desk, 25 Sep 2026).
    Stamps the stage's workflow date and tells the stage owner, exactly as
    `perform_transition` would; the caller validates, sets the status, logs
    and commits.
    """
    _stamp_workflow_dates(db, profile, new_status)
    _notify_stage_owner(db, profile, previous, new_status, comment, user)


#: Who hears that a candidate JOINED (2 Sep 2026, user request): everyone
#: who carried them through the pipeline. Admin/CEO are included by name —
#: notify_roles resolves roles literally, and a joining is the one milestone
#: leadership asked to see. HR is not on the list because HR is the actor.
JOINED_NOTIFY_ROLES = ("TA", "RMG", "Sales", "Sales_Head", "Admin", "CEO")


def _broadcast_joined(db: Session, profile: CandidateProfile, comment: str, user) -> None:
    """Bell + email to every team that worked the candidate, once each.

    Goes through `notify_roles` so the recipients are editable under Users →
    Email Flows ("Candidate joined"). Best-effort: the join itself must never
    fail because a notification did.
    """
    try:
        from services.notify import notify_roles

        name = _candidate_display_name(db, profile)
        opp = db.get(Opportunity, profile.opportunity_id)
        opp_label = (f"{getattr(opp, 'opp_id', '')} — {getattr(opp, 'title', '')}".strip(" —")
                     if opp else "")
        cust = db.get(Customer, opp.customer_id) if opp and opp.customer_id else None
        where = ", ".join(p for p in (opp_label, getattr(cust, "name", None)) if p)
        when = getattr(profile, "karnex_onboarding_date", None)
        notify_roles(
            db, list(JOINED_NOTIFY_ROLES),
            f"Joined: {name}",
            f"{name} has joined Karnex" + (f" for {where}" if where else "")
            + (f" (joining date {when.isoformat()})" if when else "")
            + ". An Employees record has been created — map them to the project to start timesheets."
            + (f" Note: {comment}" if comment else ""),
            f"/admin/?view=crm&p=profiles/{profile.id}",
            exclude_user_id=getattr(user, "id", None),
            actor=user,
            event="candidate.joined",
            related_type="candidate", related_id=profile.candidate_id,
        )
    except Exception:  # pragma: no cover — never break a join
        logger.warning("Could not broadcast join for profile %s", profile.id, exc_info=True)


def accept_offers_on_join(db: Session, profile: CandidateProfile) -> int:
    """Mark this profile's PENDING offers Accepted. Returns how many moved.

    Someone who joined obviously accepted their offer, so HR should not have to
    say it twice (user request, 2 Sep 2026). Deliberately narrow:

      * only PENDING rows move — an Expired or Rejected offer beside a joined
        candidate is a data problem worth SEEING, not one to paper over;
      * `acceptance_date` is the offer's own joining date when it has one, else
        today. Inventing a date the offer never carried would be worse than the
        blank it replaces;
      * an already-Accepted offer keeps its original acceptance date.
    """
    rows = db.execute(
        select(OfferHistory).where(
            OfferHistory.profile_id == profile.id,
            OfferHistory.status == OfferStatus.PENDING,
        )
    ).scalars().all()
    for offer in rows:
        offer.status = OfferStatus.ACCEPTED
        if offer.acceptance_date is None:
            offer.acceptance_date = offer.joining_date or date.today()
    return len(rows)


def perform_transition(db: Session, profile: CandidateProfile, new_status: str,
                       comment: str | None, user: CurrentUser) -> str:
    """Validate + apply one pipeline transition. Caller commits. Returns the old status."""
    clean_comment = (comment or "").strip()
    current = _status_value(profile.pipeline_status)

    if comment_required_for(current, new_status) and len(clean_comment) < MIN_COMMENT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=(
                f"This move needs a note of at least {MIN_COMMENT_LENGTH} characters — "
                "it is the only record of why."
            ),
        )
    valid_values = {m.value for m in PS}
    if new_status not in valid_values:
        raise HTTPException(status_code=400,
                            detail=f"Unknown pipeline status '{new_status}'. "
                                   f"Valid values: {', '.join(sorted(valid_values))}")

    if current in TERMINAL_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"'{current}' is a terminal status; no further transitions are allowed")

    allowed = allowed_next_statuses(current)
    if new_status not in allowed:
        raise HTTPException(status_code=400,
                            detail=f"Invalid transition {current} -> {new_status}. "
                                   f"Allowed next statuses: {', '.join(allowed)}")

    if new_status == PS.SELF_WITHDRAWN.value:
        # Target-specific rule: the owning TA (any TA on unowned profiles),
        # Sales, Sales_Head and Admin/CEO may record a withdrawal from ANY
        # stage — the candidate telling us "I'm out" doesn't wait for the
        # stage owner to be available.
        if not may_mark_self_withdrawn(profile, user):
            raise HTTPException(
                status_code=403,
                detail="Only the TA who added this candidate (or Sales / Sales Head / "
                       "Admin) can mark them Self Withdrew.",
            )
    elif not user_may_transition_from(current, user, db):
        required = sorted(STAGE_AUTHORITY.get(current, set()) | {"Admin"})
        raise HTTPException(status_code=403,
                            detail=f"Your role(s) cannot move a profile out of '{current}'. "
                                   f"Requires one of: {', '.join(required)}")

    _check_entry_requirement(db, profile, new_status)

    if new_status == PS.SELF_WITHDRAWN.value:
        # Remember WHICH stage they withdrew from — the UI renders
        # "Self Withdrew (RMG Review)" from this.
        profile.withdrawn_from_status = current

    profile.pipeline_status = PS(new_status)
    if profile.budget_status == TA_HOLD:
        # TA's hold only means something while TA owns the candidate; any move
        # on (or out) ends it, so it cannot linger into the HR budget flag.
        profile.budget_status = None
    _stamp_workflow_dates(db, profile, new_status)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "STATUS_CHANGE", f"{current} -> {new_status}: {clean_comment}")

    # A customer decision typed here is real interview feedback. Record it as a
    # round so it sits with the others on the Interviews tab rather than being
    # findable only by scrolling the activity log.
    _record_customer_round_from_transition(db, profile, current, new_status, clean_comment, user)

    _notify_stage_owner(db, profile, current, new_status, clean_comment, user)

    if new_status == PS.JOINED.value:
        # Joining IS accepting (2 Sep 2026, user request): HR was marking the
        # candidate Joined and then hand-editing the offer's status dropdown to
        # Accepted. Two records of one fact, and the offer sat on "Pending"
        # whenever the second edit was forgotten.
        accepted = accept_offers_on_join(db, profile)
        if accepted:
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id,
                         getattr(user, "id", None), "OFFER_ACCEPTED",
                         f"{accepted} pending offer{'s' if accepted != 1 else ''} marked "
                         "Accepted — the candidate joined")
        _broadcast_joined(db, profile, clean_comment, user)
        # The candidate is staff now: make sure they exist in the Employees tab,
        # so project mapping / timesheets / PO / invoicing can proceed.
        employee = ensure_employee_for_joined_profile(db, profile, user)
        if employee is not None:
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id,
                          getattr(user, "id", None), "EMPLOYEE_CREATED",
                          f"Added to Employees as {employee.first_name} "
                          f"{employee.last_name or ''}".rstrip()
                          + f" (employee #{employee.id}) — map them to a project to start timesheets")
        # Lazy import: requirements service is owned by another module; never fail the join.
        try:
            from services.requirements import check_and_mark_fulfilled
            req_ids = db.execute(
                select(Requirement.id).where(Requirement.opportunity_id == profile.opportunity_id)
            ).scalars().all()
            for rid in req_ids:
                try:
                    check_and_mark_fulfilled(db, rid, user.id)
                except Exception:
                    continue
        except Exception:
            pass

    return current


#: Straight profile-field -> employee-column copies applied at Joined.
#:
#: Declarative on purpose (22 Sep 2026): the previous version was a wall of
#: `if x: emp.y = x`, and every field HR added to the Workflow section needed a
#: new four-line block — which is exactly why four of them were never copied at
#: all and HR's work stayed stranded on the candidate profile.
#:
#: ONLY fields HR actually filled are touched. A blank in the Workflow section
#: means "not captured", never "erase what the employee record already has" —
#: this runs against a LIVE employee who may have better data than the profile.
_PROFILE_TO_EMPLOYEE: tuple[tuple[str, str], ...] = (
    ("designation_id", "designation_id"),
    ("department_id", "department_id"),
    # Karnex joining date — NOT the customer's onboarding date. This drives
    # payroll and leave accrual, so it must never take the customer's value.
    ("karnex_onboarding_date", "date_of_joining"),
    ("total_experience_years", "experience_years"),
    # Added 0105 — previously had nowhere to go.
    ("offer_letter_reference", "offer_letter_reference"),
    ("resignation_certificate_url", "resignation_certificate_url"),
    ("customer_onboarding_date", "customer_onboarding_date"),
    ("relocation_applicable", "relocation_applicable"),
    # NB: work_location is NOT in this map — the "Customer Location" HR sees is
    # read-only, derived from the OPPORTUNITY's `tm_work_location`, so it is
    # resolved separately below rather than pretending to be a profile field.
)

#: Fields where a blank IS meaningful and False is a real answer, so the
#: "only if filled" rule is `is not None` rather than truthiness. Without this
#: an explicit "no, relocation does not apply" would be silently dropped.
_KEEP_FALSE = frozenset({"relocation_applicable", "total_experience_years"})


def _sync_employee_from_joined_profile(db: Session, emp, profile: CandidateProfile,
                                       candidate: Candidate, offer) -> None:
    """Re-hire / internal placement (11 Sep 2026): copy what HR entered before
    Joined onto the EXISTING Employees record — the trainee becomes an
    Engineer, gets the new Karnex joining date, official mailbox, Emp ID and
    CTC — and is re-activated.

    Only fields HR actually filled are touched; see `_PROFILE_TO_EMPLOYEE`.
    """
    from models import Employee
    if emp.candidate_profile_id is None:
        emp.candidate_profile_id = profile.id
    emp.is_active = True

    for src, dest in _PROFILE_TO_EMPLOYEE:
        if not hasattr(emp, dest):
            continue          # column not migrated yet — skip, never crash a join
        value = getattr(profile, src, None)
        blank = value is None if src in _KEEP_FALSE else not value
        if blank:
            continue
        setattr(emp, dest, value)

    # HR's offered CTC (0116) wins over the Sales Head-approved terms.
    from services.hr_offer import employee_ctc
    ctc = employee_ctc(profile, offer)
    if ctc:
        emp.current_ctc = ctc

    # Where the customer wants them — the opportunity's Work Location, the same
    # string the profile screen shows read-only. Never overwritten with a blank.
    if hasattr(emp, "work_location"):
        site = _opportunity_location(getattr(getattr(profile, "opportunity", None), "details", None))
        if site:
            emp.work_location = site[:120]

    # Emp ID and official email are UNIQUE, so each needs a clash check before
    # it is written — a collision would 500 the whole join.
    ref = (getattr(profile, "employee_ref", None) or "").strip()
    if ref and (emp.employee_code or "").lower() != ref.lower():
        clash = db.execute(select(Employee.id).where(
            func.lower(Employee.employee_code) == ref.lower(), Employee.id != emp.id)).first()
        if not clash:
            emp.employee_code = ref[:32]
    official = (getattr(profile, "official_email", None) or "").strip().lower()
    if official and (emp.email or "").lower() != official:
        clash = db.execute(select(Employee.id).where(
            func.lower(Employee.email) == official, Employee.id != emp.id)).first()
        if not clash:
            # The address they had becomes their personal one rather than being
            # overwritten — it is usually the only way to reach them.
            if not emp.personal_email and emp.email and emp.email.lower() != official:
                emp.personal_email = emp.email
            emp.email = official[:255]

    # The CV is only filled in when the employee has none: an HR-uploaded CV on
    # the employee record is likely newer than the one the candidate applied with.
    if candidate.cv_url and not emp.cv_url:
        emp.cv_url = candidate.cv_url
    db.add(emp)
    db.flush()


def ensure_employee_for_joined_profile(db: Session, profile: CandidateProfile,
                                       user: CurrentUser | None = None):
    """A JOINED candidate becomes an Employee (31 Aug 2026, user bug report).

    Until now `employees.candidate_profile_id` existed but nothing ever filled
    it: a candidate could complete the whole pipeline and never appear in the
    Employees tab, which blocks everything downstream (project mapping →
    timesheets → PO → invoice).

    Rules:
      * idempotent — an employee already linked to this profile, or one with
        the same email, is REUSED (never a second record);
      * profile_type INTERNAL (2 Sep 2026, user decision — this docstring said
        EXTERNAL until 22 Sep 2026 and contradicted the code below): a joined
        candidate is on Karnex payroll, deployed to the customer. "External"
        means someone NOT on our payroll at all;
      * date_of_joining is the KARNEX onboarding date (0088) — payroll starts
        when they join us, not when the customer onboards them onto the
        project — falling back to the offer's joining date on older profiles;
      * best-effort — a join must never fail because employee creation did.
    Returns the Employee, or None when it could not be created.
    """
    from models import Employee, OfferHistory, ProfileType
    from services.hr_offer import employee_ctc

    try:
        candidate = db.get(Candidate, profile.candidate_id)
        if candidate is None:
            return None
        offer = db.execute(
            select(OfferHistory).where(OfferHistory.profile_id == profile.id)
            .order_by(OfferHistory.id.desc())
        ).scalars().first()
        emp_ref = (getattr(profile, "employee_ref", None) or "").strip()

        existing = db.execute(
            select(Employee).where(Employee.candidate_profile_id == profile.id)
        ).scalars().first()
        if existing is None and emp_ref:
            # An EXISTING employee (11 Sep 2026, user scenario: an internal
            # trainee who cleared the customer rounds) is identified by the
            # Emp ID HR typed in the Workflow section — that record is
            # updated (designation, department, joining date …), never
            # duplicated.
            existing = db.execute(
                select(Employee).where(func.lower(Employee.employee_code) == emp_ref.lower())
            ).scalars().first()
        if existing is None:
            # An internal candidate applied with their OWN address, which the
            # Employees record keeps as the personal email (29 Sep 2026: the
            # Screening Desk already calls them internal on this key — the join
            # must update that record, not create a second one). Same-name
            # guard as the official-email match below.
            own = (candidate.email or "").strip().lower()
            if own and "@import.karnex.in" not in own and "@noemail" not in own:
                match = db.execute(
                    select(Employee).where(func.lower(Employee.personal_email) == own)
                ).scalars().first()
                if match is not None and ((match.first_name or "").strip().lower()
                                          == (candidate.first_name or "").strip().lower()):
                    existing = match
        if existing is not None:
            _sync_employee_from_joined_profile(db, existing, profile, candidate, offer)
            return existing
        # The OFFICIAL mailbox HR issued before Joined is the employee's address
        # (0090, user decision) — the candidate's own email is their personal
        # one and is kept as such below. Older profiles with none recorded
        # fall back to the candidate email as before.
        official = (getattr(profile, "official_email", None) or "").strip().lower()
        email = official or (candidate.email or "").strip().lower()
        # Placeholder addresses (import/no-email resumes) are not real emails,
        # and employees.email is UNIQUE — synthesize a stable local address.
        if not email or "@import.karnex.in" in email or "@noemail" in email:
            email = f"candidate{profile.candidate_id}@pending.karnex.local"

        by_email = db.execute(
            select(Employee).where(func.lower(Employee.email) == email)
        ).scalars().first()
        if by_email is not None:
            # Same person (a re-hire) → reuse. A DIFFERENT name on that email
            # (2 Sep 2026: a candidate entered with a shared mailbox such as
            # it_support@) must not be silently merged into that employee —
            # the joiner would never appear in the Employees tab. Create them
            # under a placeholder address instead; HR corrects the email.
            same_person = (
                (by_email.first_name or "").strip().lower()
                == (candidate.first_name or "").strip().lower()
            )
            if same_person:
                _sync_employee_from_joined_profile(db, by_email, profile, candidate, offer)
                return by_email
            email = f"candidate{profile.candidate_id}@pending.karnex.local"

        if emp_ref and db.execute(
            select(Employee.id).where(func.lower(Employee.employee_code) == emp_ref.lower())
        ).first():
            emp_ref = ""   # taken by someone else — leave blank for HR to fix

        emp = Employee(
            first_name=(candidate.first_name or "Candidate")[:120],
            last_name=(candidate.last_name or None),
            email=email[:255],
            phone=(candidate.phone or None),
            # INTERNAL (2 Sep 2026, user decision): a joined candidate is a
            # Karnex employee on Karnex payroll, deployed to the customer —
            # the same as every other row in the Employees tab. "External"
            # is for people who are not on our payroll at all.
            profile_type=ProfileType.INTERNAL,
            candidate_profile_id=profile.id,
            department_id=getattr(profile, "department_id", None),
            # The employee record's joining date is the KARNEX one (0088) —
            # payroll starts when they join us, not when the customer onboards
            # them onto the project. The offer's date is the fallback for
            # profiles saved before that field existed.
            date_of_joining=(getattr(profile, "karnex_onboarding_date", None)
                             or getattr(offer, "joining_date", None)),
            # HR's offered CTC (0116), else the approved terms, else what they asked.
            current_ctc=employee_ctc(profile, offer) or candidate.expected_ctc,
            cv_url=candidate.cv_url,
            experience_years=candidate.experience_years,
            date_of_birth=getattr(candidate, "date_of_birth", None),
            gender=getattr(candidate, "gender", None),
            work_location=getattr(candidate, "city", None),
            role_title=getattr(candidate, "roles", None),
            designation_id=(getattr(profile, "designation_id", None)
                            or getattr(candidate, "designation_id", None)),
            personal_email=(candidate.email or None),
            employee_code=(emp_ref[:32] or None),
        )
        db.add(emp)
        db.flush()
        return emp
    except Exception:
        logger.warning("ensure_employee_for_joined_profile failed for profile %s",
                       getattr(profile, "id", "?"), exc_info=True)
        return None


def upsert_skill_evaluations(db: Session, profile: CandidateProfile,
                             items: list, user: CurrentUser) -> int:
    """Upsert per (profile_id, skill_id); only overwrite fields the payload provided."""
    from services.candidates import ensure_skills_exist  # shared validator

    ensure_skills_exist(db, [item.skill_id for item in items])
    touched = 0
    for item in items:
        provided = item.model_dump(exclude_unset=True)
        provided.pop("skill_id", None)
        row = db.execute(
            select(SkillEvaluation).where(SkillEvaluation.profile_id == profile.id,
                                          SkillEvaluation.skill_id == item.skill_id)
        ).scalar_one_or_none()
        if row is None:
            row = SkillEvaluation(profile_id=profile.id, skill_id=item.skill_id)
            db.add(row)
        for field, value in provided.items():
            setattr(row, field, value)
        touched += 1
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "SKILL_EVALUATION", f"Upserted skill evaluation for {touched} skill(s)")
    return touched


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------

def _num(value):
    return float(value) if value is not None else None


def _dt(value):
    return value.isoformat() if value is not None else None


def _candidate_full_name(candidate: Candidate | None) -> str | None:
    if candidate is None:
        return None
    parts = [candidate.first_name, candidate.middle_name, candidate.last_name]
    name = " ".join(p for p in parts if p)
    return name or None


def select_ctc_slab(slabs: list, experience_years):
    """The Candidate CTC Slab band that covers this much experience.

    Opportunities define bands like 5-6 / 7-9 / 10+ years, each with its own
    approved CTC budget. A candidate's budget is the band their experience falls
    into — not simply the first row, which is what the old backfill used.

    Bounds are inclusive. A missing exp_min/exp_max makes that end open, so a
    final "10+" row with no max still catches a 20-year candidate. When the
    experience sits outside every band the nearest edge band is used, so a
    budget is still shown rather than a blank.
    """
    if not slabs:
        return None
    if experience_years is None:
        return None
    try:
        exp = Decimal(str(experience_years))
    except (ArithmeticError, ValueError, TypeError):
        return None

    def lo(s):
        return Decimal(str(s.exp_min)) if s.exp_min is not None else Decimal("-999999")

    def hi(s):
        return Decimal(str(s.exp_max)) if s.exp_max is not None else Decimal("999999")

    for slab in slabs:
        if lo(slab) <= exp <= hi(slab):
            return slab
    # Experience fell in a gap between bands (e.g. 6.8 with bands 5-6 and 7-9),
    # or above the top band. Round DOWN to the highest band the candidate has
    # cleared — they do not earn the 7-9 budget until they actually have 7 years.
    at_or_below = [s for s in slabs if lo(s) <= exp]
    if at_or_below:
        return max(at_or_below, key=lo)
    # Below every band — use the lowest rather than showing nothing.
    return min(slabs, key=lo)


def approved_ctc_budgets(db: Session, profiles: list[CandidateProfile],
                         candidates: dict[int, Candidate]) -> dict[int, dict]:
    """profile.id -> {"approved_ctc_budget", "ctc_slab_band"} for a page of rows.

    Every slab for the page's opportunities is fetched in ONE query — doing it
    per row would be an N+1 on the busiest list in the CRM.
    """
    if not profiles:
        return {}
    opp_ids = {p.opportunity_id for p in profiles if p.opportunity_id}
    if not opp_ids:
        return {}
    slabs_by_opp: dict[int, list] = {}
    for slab in db.execute(
        select(OpportunityCtcSlab)
        .where(OpportunityCtcSlab.opportunity_id.in_(opp_ids))
        .order_by(OpportunityCtcSlab.opportunity_id, OpportunityCtcSlab.position)
    ).scalars().all():
        slabs_by_opp.setdefault(slab.opportunity_id, []).append(slab)

    out: dict[int, dict] = {}
    for profile in profiles:
        cand = candidates.get(profile.candidate_id)
        slab = select_ctc_slab(slabs_by_opp.get(profile.opportunity_id, []),
                               getattr(cand, "experience_years", None) if cand else None)
        if slab is None:
            continue
        band = None
        if slab.exp_min is not None or slab.exp_max is not None:
            low = f"{_num(slab.exp_min):g}" if slab.exp_min is not None else "0"
            band = f"{low}+" if slab.exp_max is None else f"{low}-{_num(slab.exp_max):g}"
        out[profile.id] = {
            "approved_ctc_budget": _num(slab.approved_ctc_lac),
            "ctc_slab_band": band,
        }
    return out


#: Shape returned for a profile with no AI interview, so every row has the same
#: keys and the frontend never has to distinguish "absent" from "not scheduled".
_EMPTY_AI_STATUS: dict = {
    "ai_interview_status": None,
    "ai_interview_result": None,
    "ai_overall_score_percent": None,
    "ai_hr_decision": None,
    "ai_hr_decision_label": None,
    "ai_effective_result": None,
    "ai_is_overridden": False,
    "ai_interview_completed_at": None,
    "ai_report_link": None,
}


def notice_periods_from_applications(db: Session, candidate_ids: set[int]) -> dict[int, str]:
    """Notice period as captured on the application form, per candidate.

    There are two places this value can live:

      * `candidates.notice_period` — the master record, what this list reads
      * `resumes.application_details["notice_period"]` — what the apply form
        writes, and what the Opportunity > Resumes tab displays

    Until recently the apply form never copied its answer onto the candidate,
    so the master field is NULL for everyone who applied online — which is most
    people. That is why the Notice Period column read "—" while the very same
    candidate showed "Notice 30 days" one screen away.

    The forward path is fixed and `scripts/backfill_notice_period.py` repairs
    history, but neither helps a list rendered before the backfill is run. So
    the serialiser falls back to the application answer.

    One query for the whole page, newest application first.
    """
    if not candidate_ids:
        return {}
    from models import Resume

    rows = db.execute(
        select(Resume.candidate_id, Resume.application_details)
        .where(Resume.candidate_id.in_(candidate_ids),
               Resume.application_details.isnot(None))
        .order_by(Resume.candidate_id.asc(), Resume.id.desc())
    ).all()

    out: dict[int, str] = {}
    for candidate_id, details in rows:
        if candidate_id in out or not isinstance(details, dict):
            continue  # first row per candidate is the newest, per the ordering
        value = str(details.get("notice_period") or "").strip()
        if value:
            out[candidate_id] = value[:60]
    return out


def latest_ai_interviews(db: Session, profiles: list[CandidateProfile]) -> dict[int, dict]:
    """Most recent AI L1 outcome per profile, in one query.

    `effective_result` surfaces a recruiter's override when they disagreed with
    the AI's score-threshold verdict, so this list agrees with the candidate
    profile page and the Resumes tab rather than showing a stale "Failed".
    """
    from models import AiInterviewLink
    from models.ai_links import hr_decision_label

    profile_ids = [p.id for p in profiles if p.id]
    if not profile_ids:
        return {}

    rows = db.execute(
        select(AiInterviewLink, Candidate.email)
        .join(Candidate, Candidate.id == AiInterviewLink.candidate_id, isouter=True)
        .where(AiInterviewLink.profile_id.in_(profile_ids))
        # Completed first, then newest — so a finished interview always wins over
        # a later-scheduled one that has not happened yet.
        .order_by(
            AiInterviewLink.profile_id.asc(),
            AiInterviewLink.completed_at.desc().nullslast(),
            AiInterviewLink.id.desc(),
        )
    ).all()

    out: dict[int, dict] = {}
    for link, email in rows:
        if link.profile_id in out:
            continue  # first row per profile is the one we want, per the ordering
        clean_email = (email or "").strip().lower()
        out[link.profile_id] = {
            "ai_interview_status": link.result,
            "ai_interview_result": link.result,
            "ai_overall_score_percent": (
                float(link.overall_score_percent)
                if link.overall_score_percent is not None else None
            ),
            "ai_hr_decision": link.hr_decision,
            "ai_hr_decision_label": hr_decision_label(link.hr_decision),
            "ai_effective_result": link.effective_result,
            "ai_is_overridden": bool(link.hr_decision) and link.effective_result != link.result,
            "ai_interview_completed_at": (
                link.completed_at.isoformat() if link.completed_at else None
            ),
            "ai_report_link": ai_report_link(clean_email, link.interview_record_id),
        }
    return out


def _interview_rows(db: Session, profiles: list[CandidateProfile]) -> list[InterviewEvent]:
    """Every interview of a page of profiles, oldest first — ONE query shared by
    the "latest round" columns and the round ladder."""
    if not profiles:
        return []
    return db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id.in_([p.id for p in profiles]))
        .order_by(InterviewEvent.profile_id,
                  InterviewEvent.scheduled_at.asc().nullsfirst(),
                  InterviewEvent.id.asc())
    ).scalars().all()


def latest_interviews(db: Session, profiles: list[CandidateProfile],
                      rows: list[InterviewEvent] | None = None) -> dict[int, dict]:
    """profile.id -> the most recent interview round, for the list columns.

    "Most recent" = latest scheduled_at, falling back to the newest row when a
    round has no date. One query for the whole page, not one per row.
    """
    if not profiles:
        return {}
    rows = _interview_rows(db, profiles) if rows is None else rows
    latest: dict[int, InterviewEvent] = {}
    counts: dict[int, int] = {}
    for ev in rows:
        latest[ev.profile_id] = ev  # ordered ascending, so the last wins
        counts[ev.profile_id] = counts.get(ev.profile_id, 0) + 1
    return {
        pid: {
            "interview_round": ev.kind,
            "interview_status": getattr(ev, "status", None),
            "interview_datetime": _dt(ev.scheduled_at) or getattr(ev, "raw_when", None),
            "interview_result": getattr(ev, "result", None),
            "interview_count": counts.get(pid, 0),
        }
        for pid, ev in latest.items()
    }


#: The round columns of the Candidate Profiles list (29 Sep 2026, user ask:
#: "all rounds' feedback, the interview date and time, choose the columns").
#: key → (label, event kinds). A customer round stored as Customer_Interview
#: with stage "L2" is the customer's second round (the pre-Customer_L2 data).
ROUND_COLUMNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("tech_l1", "Technical L1", ("L1_Interview",)),
    ("tech_l2", "Technical L2", ("L2_F2F",)),
    ("tech_l3", "Technical L3 / L4", ("L3_Interview", "L4_Interview")),
    ("cust_l1", "Customer L1", ("Customer_Interview",)),
    ("cust_l2", "Customer L2", ("Customer_L2",)),
    ("hr", "HR round", ("HR_Interview",)),
)
#: Feedback shown in a list cell (the full text is on the profile's Interviews tab).
ROUND_FEEDBACK_CHARS = 220


def round_column_key(kind: str | None, stage: str | None) -> str | None:
    """Which list column an interview belongs to. PURE."""
    if kind == "Customer_Interview" and (stage or "").strip().upper() == "L2":
        return "cust_l2"
    return next((key for key, _label, kinds in ROUND_COLUMNS if kind in kinds), None)


def round_ladder(rows: list[InterviewEvent], now: datetime | None = None) -> dict[int, dict]:
    """profile.id → {"rounds": {column → the latest HELD round}, "next_interview"}.

    PURE over the rows `_interview_rows` returned. A round that did not happen
    (cancelled, no-show, rescheduled) never fills a column; `next_interview` is
    the earliest round still ahead with no verdict.
    """
    from services.interview_rounds import NOT_HELD_STATUSES

    now = now or datetime.now(timezone.utc)
    out: dict[int, dict] = {}
    for ev in rows:
        slot = out.setdefault(ev.profile_id, {"rounds": {}, "next_interview": None})
        if (ev.status or "") in NOT_HELD_STATUSES:
            continue
        key = round_column_key(ev.kind, getattr(ev, "stage", None))
        at = ev.scheduled_at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        feedback = (ev.feedback or "").strip()
        entry = {
            "event_id": ev.id,
            "when": at.isoformat() if at else ((ev.raw_when or "").strip() or None),
            "result": (ev.result or "").strip() or None,
            "status": ev.status,
            "interviewer": (ev.interviewer or "").strip() or None,
            "mode": getattr(ev, "mode", None),
            "feedback": (feedback[:ROUND_FEEDBACK_CHARS] + "…") if len(feedback) > ROUND_FEEDBACK_CHARS else feedback or None,
            "upcoming": bool(at and at > now and not (ev.result or "").strip()),
        }
        if key:
            slot["rounds"][key] = entry        # oldest first, so the latest wins
        if entry["upcoming"]:
            nxt = slot["next_interview"]
            if nxt is None or entry["when"] < nxt["when"]:
                label = next((lab for k, lab, _ in ROUND_COLUMNS if k == key), ev.kind)
                slot["next_interview"] = {"round": label, "round_key": key, "when": entry["when"],
                                          "interviewer": entry["interviewer"], "event_id": ev.id,
                                          "meeting_link": bool((ev.meeting_link or "").strip())}
    return out


def profile_to_dict(
    profile: CandidateProfile,
    *,
    candidate: Candidate | None = None,
    opportunity: Opportunity | None = None,
) -> dict:
    data = {
        "id": profile.id,
        "candidate_id": profile.candidate_id,
        "opportunity_id": profile.opportunity_id,
        "current_ctc": _num(profile.current_ctc),
        "expected_ctc": _num(profile.expected_ctc),
        "hike_percent": _num(profile.hike_percent),
        "pipeline_status": _status_value(profile.pipeline_status),
        "commercial_approved": bool(profile.commercial_approved),
        "ctc_approval_amount": _num(profile.ctc_approval_amount),
        "created_at": _dt(profile.created_at),
        "updated_at": _dt(profile.updated_at),
        # When the candidate actually applied (Zoho). Falls back to the row's
        # insert time so imported and app-created profiles sort together.
        "applied_on": _dt(getattr(profile, "applied_on", None)) or _dt(profile.created_at),
        "ta_owner_name": getattr(profile, "ta_owner_name", None),
        "ta_owner_id": getattr(profile, "ta_owner_id", None),
        # RMG screening gate (25 Aug 2026). NULL = legacy profile, not gated.
        "rmg_screening_status": getattr(profile, "rmg_screening_status", None),
        "rmg_screening_note": getattr(profile, "rmg_screening_note", None),
        "rmg_screening_at": (profile.rmg_screening_at.isoformat()
                             if getattr(profile, "rmg_screening_at", None) else None),
        # --- provenance + workflow fields (migration 0059) ------------------
        "source": getattr(profile, "source", None),
        "is_hidden": bool(getattr(profile, "is_hidden", False)),
        "sales_submission_date": _dt(getattr(profile, "sales_submission_date", None)),
        "technical_submission_date": _dt(getattr(profile, "technical_submission_date", None)),
        "customer_submission_date": _dt(getattr(profile, "customer_submission_date", None)),
        "customer_onboarding_date": _dt(getattr(profile, "customer_onboarding_date", None)),
        "karnex_onboarding_date": _dt(getattr(profile, "karnex_onboarding_date", None)),
        "total_experience_years": _num(getattr(profile, "total_experience_years", None)),
        "relocation_applicable": getattr(profile, "relocation_applicable", None),
        "official_email": getattr(profile, "official_email", None),
        "department_id": getattr(profile, "department_id", None),
        "designation_id": (getattr(profile, "designation_id", None)
                           or (getattr(candidate, "designation_id", None) if candidate else None)),
        # The Pre-Onboarding budget hold (0094).
        "budget_status": getattr(profile, "budget_status", None),
        "budget_note": getattr(profile, "budget_note", None),
        "budget_flagged_by": getattr(profile, "budget_flagged_by", None),
        "budget_flagged_at": _dt(getattr(profile, "budget_flagged_at", None)),
        "budget_resolution_note": getattr(profile, "budget_resolution_note", None),
        "budget_resolved_by": getattr(profile, "budget_resolved_by", None),
        "budget_resolved_at": _dt(getattr(profile, "budget_resolved_at", None)),
        "commercial_approval_status": getattr(profile, "commercial_approval_status", None),
        "approved_ctc": _num(getattr(profile, "approved_ctc", None)),
        "offer_letter_reference": getattr(profile, "offer_letter_reference", None),
        # The application-level file first, else the candidate's own (2 Sep
        # 2026, user report: "why no files here"). The profile columns are only
        # ever filled by the Zoho import; everything uploaded IN the app — the
        # CV on the candidate record, the resignation certificate — lives on
        # the candidate, so a profile created here always showed dashes.
        "resume_url": (getattr(profile, "resume_url", None)
                       or (getattr(candidate, "cv_url", None) if candidate else None)),
        "cv_original_filename": getattr(profile, "cv_original_filename", None),
        "resignation_certificate_url": (
            getattr(profile, "resignation_certificate_url", None)
            or (getattr(candidate, "resignation_certificate_url", None) if candidate else None)),
        "stage": getattr(profile, "stage", None),
        "withdrawn_from_status": getattr(profile, "withdrawn_from_status", None),
        "candidate_pre_status": getattr(profile, "candidate_pre_status", None),
        "employee_ref": getattr(profile, "employee_ref", None),
        "created_by_name": getattr(profile, "created_by_name", None),
        "user_role": getattr(profile, "user_role", None),
        "comments_text": getattr(profile, "comments_text", None),
    }
    if candidate is not None:
        data["candidate_name"] = _candidate_full_name(candidate)
        data["email"] = candidate.email
        data["phone"] = candidate.phone
        data["experience_years"] = _num(candidate.experience_years)
        data["notice_period"] = candidate.notice_period
        data["technical_domain"] = candidate.technical_domain
        data["cv_url"] = candidate.cv_url
        # Candidate-master CTCs (profile commercials remain above).
        data["candidate_current_ctc"] = _num(candidate.current_ctc)
        data["candidate_expected_ctc"] = _num(candidate.expected_ctc)
    if opportunity is not None:
        data["opportunity_opp_id"] = opportunity.opp_id
        data["opportunity_title"] = opportunity.title
        data["customer_id"] = opportunity.customer_id
    return data


def enrich_profiles_list(db: Session, profiles: list[CandidateProfile]) -> list[dict]:
    """Serialize a page of profiles with candidate + opportunity summaries (no N+1)."""
    if not profiles:
        return []
    cand_ids = {p.candidate_id for p in profiles}
    opp_ids = {p.opportunity_id for p in profiles}
    candidates = {
        c.id: c
        for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all()
    }
    opportunities = {
        o.id: o
        for o in db.execute(select(Opportunity).where(Opportunity.id.in_(opp_ids))).scalars().all()
    }
    # Customer names in one query — the grouped view and the Customer column both
    # need them, and a lookup per row would be an N+1 on the busiest list.
    # Opportunity's REQUIRED experience band (28 Aug 2026, user request): the
    # min/max across its CTC slab rows — same figures the opportunity header
    # shows — so the list answers "does this candidate fit?" without a detour.
    from sqlalchemy import func as _f
    from models import OpportunityCtcSlab as _Slab
    exp_by_opp: dict[int, tuple] = {}
    if opp_ids:
        for _oid, _mn, _mx in db.execute(
            select(_Slab.opportunity_id, _f.min(_Slab.exp_min), _f.max(_Slab.exp_max))
            .where(_Slab.opportunity_id.in_(opp_ids))
            .group_by(_Slab.opportunity_id)
        ).all():
            exp_by_opp[_oid] = (_mn, _mx)

    customer_ids = {o.customer_id for o in opportunities.values() if o.customer_id}
    customers = {
        cid: name
        for cid, name in db.execute(
            select(Customer.id, Customer.name).where(Customer.id.in_(customer_ids))
        ).all()
    } if customer_ids else {}
    budgets = approved_ctc_budgets(db, profiles, candidates)
    events = _interview_rows(db, profiles)
    interviews = latest_interviews(db, profiles, events)
    ladder = round_ladder(events)
    ai_status = latest_ai_interviews(db, profiles)
    # The ONE status every screen shows (services/candidate_status.py).
    statuses = candidate_statuses_for(db, profiles)
    # Fallback for candidates whose notice period only exists on their
    # application — see notice_periods_from_applications for why.
    applied_notice = notice_periods_from_applications(db, cand_ids)
    # ATS score per (candidate, opportunity) — from the resume row on that
    # opportunity's requirement (2 Sep 2026: RMG screens on the Applicants
    # tab and wanted the score there, not only on Applied Candidates).
    ats_by_key: dict[tuple[int, int], dict] = {}
    try:
        from models import Requirement as _Req, Resume as _Res
        rows = db.execute(
            select(_Res.candidate_id, _Req.opportunity_id, _Res.id, _Res.ats_score, _Res.ats_status)
            .join(_Req, _Req.id == _Res.requirement_id)
            .where(_Req.opportunity_id.in_(opp_ids), _Res.candidate_id.in_(cand_ids))
            .order_by(_Res.id)
        ).all()
        for cid, oid, rid, score, status in rows:
            ats_by_key[(cid, oid)] = {
                "resume_id": rid,
                "ats_score": float(score) if score is not None else None,
                "ats_status": getattr(status, "value", status),
            }
    except Exception:  # pragma: no cover — the list must never fail on a lookup
        ats_by_key = {}
    out = []
    for p in profiles:
        data = profile_to_dict(
            p,
            candidate=candidates.get(p.candidate_id),
            opportunity=opportunities.get(p.opportunity_id),
        )
        # Approved CTC budget for this candidate's experience band, from the
        # opportunity's Candidate CTC Slab.
        data.update(budgets.get(p.id, {"approved_ctc_budget": None, "ctc_slab_band": None}))
        # Latest interview round / status / date, for the list columns.
        data.update(interviews.get(p.id, {
            "interview_round": None, "interview_status": None,
            "interview_datetime": None, "interview_result": None, "interview_count": 0,
        }))
        # AI L1 outcome. Previously absent from this endpoint entirely, which is
        # why neither the profiles list nor the Opportunity applicants table
        # showed an AI interview status at all.
        data.update(ai_status.get(p.id, _EMPTY_AI_STATUS))
        # The application's own resume, else fall back to the candidate's CV.
        cand = candidates.get(p.candidate_id)
        data["resume_url"] = getattr(p, "resume_url", None) or (cand.cv_url if cand else None)
        # Resignation certificate lives on the CANDIDATE (a person resigns once),
        # so every application they make shows the same proof. A profile-level
        # override still wins if one was imported.
        data["resignation_certificate_url"] = (
            getattr(p, "resignation_certificate_url", None)
            or (getattr(cand, "resignation_certificate_url", None) if cand else None)
        )
        opp = opportunities.get(p.opportunity_id)
        data["customer_name"] = customers.get(opp.customer_id) if opp else None
        _band = exp_by_opp.get(p.opportunity_id)
        data["opportunity_exp_min"] = _num(_band[0]) if _band else None
        data["opportunity_exp_max"] = _num(_band[1]) if _band else None
        # The candidate record wins when it has a value — a recruiter who typed
        # a notice period by hand is more current than an old application.
        if not (data.get("notice_period") or "").strip():
            data["notice_period"] = applied_notice.get(p.candidate_id)
        data["resignation_status"] = bool(getattr(cand, "resignation_status", False)) if cand else False
        data["last_working_day"] = _dt(getattr(cand, "last_working_day", None)) if cand else None
        data.update(ats_by_key.get((p.candidate_id, p.opportunity_id),
                                   {"resume_id": None, "ats_score": None, "ats_status": None}))
        data["candidate_status"] = statuses.get(p.id)
        # Every round's verdict / time / panel / feedback, and the next one due.
        lad = ladder.get(p.id) or {}
        data["rounds"] = lad.get("rounds") or {}
        data["next_interview"] = lad.get("next_interview")
        out.append(data)
    return out


def offer_to_dict(offer: OfferHistory) -> dict:
    return {
        "id": offer.id,
        "profile_id": offer.profile_id,
        "offer_date": _dt(offer.offer_date),
        "ctc": _num(offer.ctc),
        "joining_date": _dt(offer.joining_date),
        "offer_letter_url": offer.offer_letter_url,
        "acceptance_date": _dt(offer.acceptance_date),
        "expiry_date": _dt(offer.expiry_date),
        "status": _status_value(offer.status),
        # The rate as Sales typed it (0092); `ctc` above is the annualised figure.
        "rate_unit": getattr(offer, "rate_unit", None),
        "rate_value": _num(getattr(offer, "rate_value", None)),
    }


def _opportunity_location(details) -> str | None:
    """`tm_work_location` from the opportunity form as one display string —
    a city, or "Chennai, Bangalore" for a multi-city role. None when unset."""
    if not isinstance(details, dict):
        return None
    raw = details.get("tm_work_location")
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
        return ", ".join(parts) or None
    text = str(raw).strip()
    return text or None


def profile_detail(db: Session, profile: CandidateProfile, user: CurrentUser) -> dict:
    candidate = db.get(Candidate, profile.candidate_id)
    # Pass the candidate so the detail carries the same candidate-master
    # fields the LIST does (experience_years, notice_period, …). It was
    # serialised without one, so the HR screen's Total Experience could not
    # default from the TA-entered figure the list plainly showed (2 Sep 2026).
    data = profile_to_dict(profile, candidate=candidate)

    data["candidate"] = {
        "id": candidate.id,
        "full_name": " ".join(p for p in (candidate.first_name, candidate.last_name) if p),
        "email": candidate.email,
        "phone": candidate.phone,
        "technical_domain": candidate.technical_domain,
        "cv_url": candidate.cv_url,
        # Where the candidate is (TA entered it on the record) — shown on the
        # HR screen beside the customer's location (2 Sep 2026).
        "city": candidate.city,
        # Where they are willing to go — TA's entry, editable by HR from the
        # profile (writes back to the candidate, one source of truth).
        "preferred_locations": candidate.preferred_locations,
        "resignation_certificate_url": candidate.resignation_certificate_url,
    } if candidate else None

    opp_row = db.execute(
        select(Opportunity.id, Opportunity.opp_id, Opportunity.title, Customer.name,
               Opportunity.details)
        .join(Customer, Customer.id == Opportunity.customer_id)
        .where(Opportunity.id == profile.opportunity_id)
    ).first()
    data["opportunity"] = {
        "id": opp_row[0], "opp_id": opp_row[1], "title": opp_row[2], "customer_name": opp_row[3],
        # The Work Location the Sales team set on the opportunity form — a city
        # name, or a list of them for multi-city roles. Read straight from the
        # opportunity so it can never disagree with the opportunity page.
        "location": _opportunity_location(opp_row[4]),
    } if opp_row else None

    eval_rows = db.execute(
        select(SkillEvaluation, Skill.name)
        .join(Skill, Skill.id == SkillEvaluation.skill_id)
        .where(SkillEvaluation.profile_id == profile.id)
        .order_by(Skill.name)
    ).all()
    data["skill_evaluations"] = [
        {
            "id": ev.id,
            "skill_id": ev.skill_id,
            "skill_name": skill_name,
            "required_level": ev.required_level,
            "self_rated": ev.self_rated,
            "reviewer_rated": ev.reviewer_rated,
        }
        for ev, skill_name in eval_rows
    ]

    budget = approved_ctc_budgets(db, [profile], {candidate.id: candidate} if candidate else {})
    data.update(budget.get(profile.id, {"approved_ctc_budget": None, "ctc_slab_band": None}))

    data["offers"] = [offer_to_dict(o) for o in profile.offers]
    data["interview_events"] = interview_events_for_profile(db, profile.id)
    # Human-round state (1 Sep 2026) — the same four facts per round the
    # Applied Candidates row uses, so the profile banner and the row can never
    # disagree about whether a manual L1 was asked for, booked or judged.
    try:
        from services.resumes import manual_round_state
        data.update(manual_round_state(db, [profile.id]).get(profile.id, {}))
    except Exception:  # pragma: no cover — never break the detail page
        pass
    data["candidate_status"] = candidate_statuses_for(db, [profile]).get(profile.id)
    data["allowed_next_statuses"] = allowed_next_statuses_for_user(profile.pipeline_status, user, profile, db)
    return data


#: Display order for interview rounds — L1 before L2 before customer rounds.
_ROUND_ORDER = {
    "L1_Interview": 1, "L2_F2F": 2, "L3_Interview": 3, "L4_Interview": 4,
    "HR_Interview": 5, "Customer_Interview": 6, "Other": 9,
}


def interview_event_to_dict(event: InterviewEvent) -> dict:
    """One interview round, as the Interviews tab renders it."""
    return {
        "id": event.id,
        "kind": event.kind,
        "scheduled_at": _dt(event.scheduled_at),
        "raw_when": event.raw_when,
        "meeting_link": event.meeting_link,
        "stage": getattr(event, "stage", None),
        "mode": getattr(event, "mode", None),
        "status": getattr(event, "status", None),
        "result": getattr(event, "result", None),
        "interviewer": getattr(event, "interviewer", None),
        "feedback": getattr(event, "feedback", None),
        "interview_category": getattr(event, "interview_category", None),
        "duration_minutes": getattr(event, "duration_minutes", None),
        "user_role": getattr(event, "user_role", None),
        "employee_id": getattr(event, "employee_id", None),
        "note": event.note,
        "created_at": _dt(event.created_at),
    }


def interview_events_for_profile(db: Session, profile_id: int) -> list[dict]:
    """Every human interview round recorded against a profile — L1/L2/L3/L4,
    customer and HR rounds, whether entered in the app or imported from Zoho.

    Ordered NEWEST-first by the scheduled time (2 Sep 2026, user decision) —
    the round that matters is the latest one; rounds with no date (imported
    without one) sort last, newest id first.
    """
    rows = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id == profile_id)
        .order_by(InterviewEvent.scheduled_at.desc().nullslast(), InterviewEvent.id.desc())
    ).scalars().all()
    out = [interview_event_to_dict(e) for e in rows]
    out.sort(key=lambda r: (r["scheduled_at"] is None,
                            r["scheduled_at"] or "",
                            _ROUND_ORDER.get(r["kind"], 9)))
    return out


# ---------------------------------------------------------------------------
# TA's calls on a fresh applicant (28 Sep 2026, user request)
# ---------------------------------------------------------------------------
# A TA upload lands at SOURCING, with TA. From the Applied Candidates row TA
# then sends the candidate for Technical Screening (RMG / GM screen the CV),
# parks them (Hold — not reachable, over budget …), rejects them, or records a
# Self Withdraw. Screening, hold and reject are TA's while TA owns the
# candidacy (Sourcing / Technical_Screening); a withdrawal can be recorded at
# any live stage (perform_transition's own rule decides).

#: `interested` / `not_interested` (1 Oct 2026) answer the opening email a bulk
#: upload sends (`services/opening_interest`).
TA_DECISIONS = ("screen", "hold", "release", "reject", "withdraw", "reapply",
                "interested", "not_interested")
#: The withdrawal note when the candidate says no to the opening email.
NOT_INTERESTED_NOTE = "Not interested in this opening (replied to the opening email)"

#: Activity row written when a withdrawn candidate re-applies (1 Oct 2026).
REAPPLIED_ACTION = "REAPPLIED"


def reapply_candidacy(db: Session, profile: CandidateProfile, note: str, user: CurrentUser) -> str:
    """Reopen a CLOSED candidacy at Sourcing (1 Oct 2026, user ask: "after Self
    Withdrawn I need a button to apply to this opportunity again", widened the
    same day to "if we want to apply again after a rejection").

    Any candidacy in `REJECTED_BUCKET` reopens (409 otherwise). A withdrawal
    needs no reason; re-applying over a REJECTION — somebody else's decision —
    needs one (≥ `MIN_COMMENT_LENGTH`), and it is logged beside the rejection's
    own note so the history reads both. The candidate starts the flow afresh: back
    with TA at Sourcing, the RMG screening cleared (TA sends them for
    Technical Screening again), any hold / withdrawn-from note cleared, and
    an Archive flag lifted. Interview history stays — it happened. Logged as
    a STATUS_CHANGE plus `REAPPLIED`. The caller commits.
    """
    from services.candidate_status import RESTORED_ACTION, archived_profile_ids

    current = profile.pipeline_status.value if hasattr(profile.pipeline_status, "value") \
        else str(profile.pipeline_status or "")
    if current not in REJECTED_BUCKET:
        raise HTTPException(status_code=409,
                            detail="Only a closed candidacy (withdrawn or rejected) can re-apply to "
                                   "this opportunity.")
    if current != PS.SELF_WITHDRAWN.value and len(note or "") < MIN_COMMENT_LENGTH:
        raise HTTPException(status_code=400,
                            detail="Say why the candidate should be considered again despite the "
                                   f"rejection (at least {MIN_COMMENT_LENGTH} characters).")
    uid = getattr(user, "id", None)
    actor = getattr(user, "full_name", None) or getattr(user, "username", None) or "TA"
    was_archived = profile.id in archived_profile_ids(db, [profile.id])
    profile.pipeline_status = PS.SOURCING
    profile.withdrawn_from_status = None
    profile.rmg_screening_status = None
    profile.rmg_screening_note = None
    profile.rmg_screening_by = None
    profile.rmg_screening_at = None
    if profile.budget_status == TA_HOLD:
        profile.budget_status = None
    why = f": {note}" if note else ""
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, "STATUS_CHANGE",
                 f"{current} -> {PS.SOURCING.value}: Re-applied{why}")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, REAPPLIED_ACTION,
                 f"{actor} re-applied the candidate to this opportunity{why}")
    if was_archived:
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, RESTORED_ACTION,
                     f"{actor} restored the candidate to Applied Candidates (re-applied)")
    return "Re-applied — the candidate is back at Sourcing with you"
#: Activity row written when TA hands a candidate to RMG / GM.
SENT_FOR_SCREENING = "SENT_FOR_SCREENING"

#: A send of up to this many candidates is ATS-scored inside the request; more
#: go to a background thread after commit.
SCREENING_SCORE_INLINE = 3


def ta_user_ids(db: Session, profile) -> list[int]:
    """The TAs who work this candidate: the TA owner (who applied them) AND the
    TA who sent them for Technical Screening — often a different person
    (28 Sep 2026 report: Gargee sent Mohammed's candidate, RMG chose the manual
    L1, and only Mohammed heard). One query; owner first, no duplicates. Used
    by every "tell the TA" notice and by the feedback-due reminders."""
    ids: list[int] = [profile.ta_owner_id] if profile.ta_owner_id else []
    sender = db.execute(
        select(CandidateProfileActivityLog.user_id)
        .where(CandidateProfileActivityLog.profile_id == profile.id,
               CandidateProfileActivityLog.action_type == SENT_FOR_SCREENING,
               CandidateProfileActivityLog.user_id.isnot(None))
        .order_by(CandidateProfileActivityLog.id.desc()).limit(1)
    ).scalar()
    if sender and sender not in ids:
        ids.append(int(sender))
    return ids


#: The activity row that carries the slots the customer offered, passed by Sales
#: to TA with Customer Interviewing (29 Sep 2026) — TA books the round from them.
CUSTOMER_SLOTS_PROPOSED = "CUSTOMER_SLOTS_PROPOSED"
#: The customer rounds a slot offer can be for (the kind names the round TA books).
CUSTOMER_SLOT_KINDS = ("Customer_Interview", "Customer_L2")
_SLOT_LABEL_FMT = "%d %b %Y, %I:%M %p"
_SLOT_HEADER = " — slots offered by the customer:"


def fmt_slot_ist(raw: str | None) -> str:
    """'2026-09-15T11:00' → '15 Sep 2026, 11:00 AM IST'. PURE. The zone is spelled
    out because the string is the IST wall clock Sales typed; anything that does
    not parse is returned as typed."""
    try:
        return datetime.fromisoformat(str(raw)).strftime(_SLOT_LABEL_FMT) + " IST"
    except (ValueError, TypeError):
        return raw or ""


def customer_slots_text(sched) -> str:
    """The customer's slots as the lines TA reads (numbered, IST, link, panel).
    PURE. `parse_customer_slots` reads exactly this shape back."""
    lines = []
    for i, slot in enumerate(sched.all_slots(), start=1):
        line = f"{i}. {fmt_slot_ist(slot.scheduled_at)}"
        link = (slot.meeting_link or sched.meeting_link or "").strip()
        if link:
            line += f" — {link}"
        lines.append(line)
    extra = []
    if sched.interviewer:
        extra.append(f"panel {sched.interviewer.strip()[:200]}")
    if sched.duration_minutes:
        extra.append(f"{sched.duration_minutes} min")
    if extra:
        lines.append("(" + ", ".join(extra) + ")")
    return "\n".join(lines)


def customer_slots_comment(kind: str, sched, note: str = "") -> str:
    """The `CUSTOMER_SLOTS_PROPOSED` activity text: header naming the round,
    the slot lines, and Sales' note."""
    from services.interview_rounds import round_label
    return (f"{round_label(kind)}{_SLOT_HEADER}\n{customer_slots_text(sched)}"
            + (f"\nNote: {note}" if note else ""))


def parse_customer_slots(comment: str | None) -> dict | None:
    """Read a `customer_slots_comment` back into data TA's scheduling form can
    use. PURE — the round-trip is pinned by a test, so the two cannot drift.

    → {"kind", "slots": [{"scheduled_at" ("YYYY-MM-DDTHH:MM" IST wall clock, or
    None when Sales typed something unparseable), "label", "meeting_link"}],
    "interviewer", "duration_minutes", "note"}; None when there is no slot."""
    import re

    from services.interview_rounds import round_label
    lines = (comment or "").splitlines()
    if not lines:
        return None
    head = lines[0].split(_SLOT_HEADER, 1)[0].strip()
    kind = next((k for k in CUSTOMER_SLOT_KINDS if round_label(k) == head), CUSTOMER_SLOT_KINDS[0])
    out: dict = {"kind": kind, "slots": [], "interviewer": None, "duration_minutes": None, "note": None}
    for raw in lines[1:]:
        line = raw.strip()
        m = re.match(r"^\d+\.\s+(.*)$", line)
        if m:
            label, _, link = m.group(1).partition(" — ")
            label = label.strip()
            try:
                when = datetime.strptime(label.removesuffix(" IST"), _SLOT_LABEL_FMT).strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                when = None
            out["slots"].append({"scheduled_at": when, "label": label,
                                 "meeting_link": link.strip() or None})
        elif line.startswith("(") and line.endswith(")"):
            # "(panel <names — may hold commas>, <N> min)", either part optional
            inner = line[1:-1].strip()
            dur = re.search(r"(?:^|,\s*)(\d+) min$", inner)
            if dur:
                out["duration_minutes"] = int(dur.group(1))
                inner = inner[:dur.start()].strip()
            if inner.startswith("panel "):
                out["interviewer"] = inner[6:].strip() or None
        elif line.startswith("Note:"):
            out["note"] = line[5:].strip() or None
    return out if out["slots"] else None


def latest_customer_slots(db: Session, profile_ids) -> dict[int, dict]:
    """{profile id: the LATEST customer-slot offer, parsed} — one query for a
    page. Each value adds `proposed_at` (ISO) and `proposed_by_id`. Used by TA's
    scheduling form, the Applied Candidates rows and the work desk."""
    ids = [int(i) for i in (profile_ids or ()) if i is not None]
    if not ids:
        return {}
    out: dict[int, dict] = {}
    for pid, uid, comment, ts in db.execute(
        select(CandidateProfileActivityLog.profile_id, CandidateProfileActivityLog.user_id,
               CandidateProfileActivityLog.comment, CandidateProfileActivityLog.timestamp)
        .where(CandidateProfileActivityLog.profile_id.in_(ids),
               CandidateProfileActivityLog.action_type == CUSTOMER_SLOTS_PROPOSED)
        .order_by(CandidateProfileActivityLog.id.desc())
    ).all():
        if pid in out:
            continue
        parsed = parse_customer_slots(comment)
        if parsed:
            parsed["proposed_at"] = ts.isoformat() if ts else None
            parsed["proposed_by_id"] = uid
            out[pid] = parsed
    return out


TA_DECISION_STAGES = (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value)
_TA_REJECT_ACTION = next(iter(TA_CLOSE_ACTIONS))


def budget_fit(expected_ctc, budget_max) -> dict:
    """{"expected_ctc", "budget_ctc_max", "over_budget"} for one row. PURE.

    Over budget only when BOTH figures are known — an unknown CTC is not a
    reason to stop anyone.
    """
    exp = float(expected_ctc) if expected_ctc is not None else None
    cap = float(budget_max) if budget_max else None
    return {"expected_ctc": exp, "budget_ctc_max": cap,
            "over_budget": exp is not None and cap is not None and exp > cap}


def _need_reason(clean: str) -> None:
    if len(clean) < MIN_COMMENT_LENGTH:
        raise HTTPException(status_code=400,
                            detail=f"Give a reason of at least {MIN_COMMENT_LENGTH} characters.")


def ta_decision(db: Session, profile: CandidateProfile, decision: str,
                note: str | None, user: CurrentUser) -> str:
    """Apply TA's call; returns the message to show. Caller commits.

    screen   → RMG screening Pending + everyone who may screen is told
               (`notify_rmg_new_applicant`); a hold is lifted on the way.
    hold     → parks the candidate (`budget_status = TA_Hold`): off the
               Screening Desk, status "On Hold". Reason optional.
    release  → back to where they were.
    reject   → Rejected through `perform_transition` (same checks, log and
               notifications as any rejection) + a `TA_REJECTED` row, so the
               status reads "Rejected by TA". Reason required.
    withdraw → Self_Withdrawn through `perform_transition`. Reason required.
    reapply  → a Self_Withdrawn candidacy reopens at Sourcing (`reapply_candidacy`).
    interested     → the candidate said yes to the opening email: the reply is
                     logged and they go for Technical Screening (as `screen`).
    not_interested → they said no: logged, then Self_Withdrawn (note optional).
    """
    if decision not in TA_DECISIONS:
        raise HTTPException(status_code=400,
                            detail=f"Decision must be one of: {', '.join(TA_DECISIONS)}")
    clean = (note or "").strip()
    uid = getattr(user, "id", None)
    actor = getattr(user, "full_name", None) or getattr(user, "username", None) or "TA"
    if decision == "withdraw":
        _need_reason(clean)
        perform_transition(db, profile, PS.SELF_WITHDRAWN.value, f"Self withdrew: {clean}", user)
        notify_screeners_of_ta_close(db, profile, "withdrew", clean, user)
        return "Recorded — the candidate withdrew"
    if decision == "reapply":
        return reapply_candidacy(db, profile, clean, user)
    # Technical Screening, Hold and Reject belong to the SOURCING phase
    # (28 Sep 2026, user rule): once the candidate is with RMG / GM or has an
    # interview asked for, only Self Withdraw is TA's to record.
    stage = _ta_stage(db, [profile]).get(profile.id)
    if stage != SOURCING_STAGE:
        raise HTTPException(status_code=409, detail=_not_at_sourcing(stage))
    if decision in ("interested", "not_interested"):
        from services.opening_interest import record_reply
        if decision == "not_interested":
            record_reply(db, profile, False, clean, user)
            perform_transition(db, profile, PS.SELF_WITHDRAWN.value,
                               f"Self withdrew: {clean or NOT_INTERESTED_NOTE}", user)
            return "Recorded — the candidate is not interested (Self Withdrawn)"
        record_reply(db, profile, True, clean, user)
        sent, refused = send_for_screening(db, [profile], user, clean)
        if refused:
            raise HTTPException(status_code=409, detail=refused[0]["reason"])
        return "Interested — sent for Technical Screening, RMG / GM notified"
    if decision == "screen":
        sent, refused = send_for_screening(db, [profile], user, clean)
        if refused:
            raise HTTPException(status_code=409, detail=refused[0]["reason"])
        return "Sent for Technical Screening — RMG / GM notified"
    if decision == "hold":
        profile.budget_status = TA_HOLD
        profile.budget_note = clean or None
        profile.budget_flagged_by = uid
        profile.budget_flagged_at = datetime.now(timezone.utc)
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, "TA_HOLD",
                     f"{actor} put the candidate on hold" + (f": {clean}" if clean else ""))
        return "Candidate on hold — nobody screens them until you release the hold"
    if decision == "release":
        if profile.budget_status != TA_HOLD:
            raise HTTPException(status_code=400, detail="This candidate is not on hold.")
        profile.budget_status = None
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid,
                     "TA_HOLD_RELEASED", f"{actor} released the hold" + (f": {clean}" if clean else ""))
        return "Hold released — the candidate is back in the flow"
    _need_reason(clean)
    perform_transition(db, profile, PS.REJECTED.value, f"Rejected by TA: {clean}", user)  # ends any hold
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid,
                 _TA_REJECT_ACTION, f"Rejected by TA: {clean}")
    notify_screeners_of_ta_close(db, profile, "rejected", clean, user)
    return "Candidate rejected — RMG / GM notified"


#: Event for "TA closed a candidacy" (in `email_flows.EVENTS`, RMG + GM).
TA_CLOSED_EVENT = "profile.ta_closed"


def notify_screeners_of_ta_close(db: Session, profile: CandidateProfile, how: str, reason: str,
                                 user: CurrentUser) -> None:
    """Tell everyone who may SCREEN that TA closed a candidacy (29 Sep 2026, user
    report: "once TA rejects a candidate, RMG / GM get no email"). Same
    recipients as the screening notices (`screening_notify_user_ids`), so a GM
    or a template-granted RMG hears it too; the actor is never told. Bell +
    email, deduped per profile and outcome. Best-effort, savepointed."""
    try:
        from services.notify import notify_roles
        with db.begin_nested():
            uid = getattr(user, "id", None)
            actor = getattr(user, "full_name", None) or getattr(user, "username", None) or "TA"
            name = _candidate_display_name(db, profile)
            opp = db.get(Opportunity, profile.opportunity_id) if profile.opportunity_id else None
            where = " — ".join(x for x in (getattr(opp, "opp_id", None), getattr(opp, "title", None)) if x)
            title = f"Rejected by TA: {name}" if how == "rejected" else f"Candidate withdrew: {name}"
            message = (f"{actor} recorded that {name}"
                       + (f" ({where})" if where else "")
                       + (" was rejected" if how == "rejected" else " withdrew")
                       + f". Reason: {reason}")
            notify_roles(db, ["RMG", "GM"], title, message,
                         f"/admin/?view=crm&p=profiles/{profile.id}",
                         exclude_user_id=uid, actor=user, event=TA_CLOSED_EVENT,
                         dedupe_prefix=f"ta_closed:{profile.id}:{how}",
                         related_type="candidate", related_id=profile.candidate_id,
                         user_ids=[i for i in screening_notify_user_ids(db) if i != uid] or None)
    except Exception:  # pragma: no cover — a notice never undoes the decision
        logger.warning("Could not notify screeners of TA close for profile %s", profile.id, exc_info=True)


def _ta_stage(db: Session, profiles: list[CandidateProfile]) -> dict[int, str]:
    """profile id → the derived phase key (`candidate_status.STAGES`), batched."""
    return {pid: (st.get("stage") or {}).get("key")
            for pid, st in candidate_statuses_for(db, profiles).items()}


def _not_at_sourcing(stage: str | None) -> str:
    return ("Technical Screening, Hold and Reject are TA's calls while the candidate is at "
            f"Sourcing — this one is at {CANDIDATE_STAGE_LABEL.get(stage or '', 'a later stage')}.")


def _screening_refusal(profile, stage: str | None) -> str | None:
    """Why this profile cannot be sent for Technical Screening, or None."""
    if stage != SOURCING_STAGE:
        return _not_at_sourcing(stage)
    status = profile.rmg_screening_status
    if status in (RMG_SCREENING_PENDING, RMG_SCREENING_SHORTLISTED):
        return "This candidate is already with RMG / GM for screening."
    if status == RMG_SCREENING_REJECTED:
        return "RMG / GM already rejected this candidate at screening."
    return None


def send_for_screening(db: Session, profiles: list[CandidateProfile], user,
                       note: str | None = None) -> tuple[list[CandidateProfile], list[dict]]:
    """TA hands candidates to RMG / GM (the "Technical Screening" button).

    Stamps screening Pending (the Screening Desk's queue), lifts a TA hold and
    logs `SENT_FOR_SCREENING`. Everyone who may screen hears ONCE: one notice
    per candidate for a single send, one summary for a batch — fifty bell rows
    for one bulk upload would be noise. Returns (sent, refused[{profile_id,
    reason}]); the caller commits.
    """
    uid = getattr(user, "id", None)
    actor = getattr(user, "full_name", None) or getattr(user, "username", None) or "TA"
    clean = (note or "").strip()
    sent: list[CandidateProfile] = []
    refused: list[dict] = []
    stages = _ta_stage(db, profiles)
    for profile in profiles:
        reason = _screening_refusal(profile, stages.get(profile.id))
        if reason:
            refused.append({"profile_id": profile.id, "reason": reason})
            continue
        if profile.budget_status == TA_HOLD:
            profile.budget_status = None
        profile.rmg_screening_status = RMG_SCREENING_PENDING
        profile.rmg_screening_note = None
        stamp_technical_submission(profile)
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid,
                     SENT_FOR_SCREENING,
                     f"{actor} sent the candidate for Technical Screening"
                     + (f": {clean}" if clean else ""))
        sent.append(profile)
    # ATS the moment a candidate reaches the Screening Desk (30 Sep 2026, user
    # ask) — RMG / GM open the row to a score, not "Not scored". A small send
    # is scored inline (each scan savepointed, never fails the send); a larger
    # batch is scored after commit by the caller (`score_profiles_in_background`).
    if 0 < len(sent) <= SCREENING_SCORE_INLINE:
        from services.resumes import auto_score_profile

        for profile in sent:
            auto_score_profile(db, profile, uid)
    if len(sent) == 1:
        notify_rmg_new_applicant(db, sent[0], actor=user)
    elif sent:
        _notify_screening_batch(db, sent, user, actor)
    return sent, refused


def _notify_screening_batch(db: Session, profiles: list[CandidateProfile], user, actor: str) -> None:
    """ONE "N candidates await screening" notice to everyone who may screen."""
    try:
        from services.notify import notify_role

        ids = [p.candidate_id for p in profiles]
        rows = db.execute(select(Candidate.first_name, Candidate.last_name)
                          .where(Candidate.id.in_(ids)).limit(5)).all()
        names = ", ".join(" ".join(x for x in r if x) for r in rows)
        more = f" and {len(profiles) - len(rows)} more" if len(profiles) > len(rows) else ""
        notify_role(
            db, "RMG",
            f"{len(profiles)} candidates await Technical Screening",
            f"{names}{more} — sent for screening by {actor}. Review them on the Screening "
            "Desk: Shortlist or Reject, then choose the AI or manual L1.",
            "/admin/?view=crm&p=screening-desk",
            event=RMG_SCREENING_REQUESTED_EVENT, actor=user,
            dedupe_prefix="screen_batch:" + ",".join(str(p.id) for p in profiles[:20]),
            user_ids=screening_notify_user_ids(db),
        )
    except Exception:
        logger.warning("screening batch notice failed", exc_info=True)


def l1_verdict_recorded(db: Session, profile_id: int) -> bool:
    """Has the L1 — manual or AI — got a verdict? An L2 is asked for after it.

    Manual: an `L1_Interview` event with a result. AI: a completed link (the
    report exists, whatever it said — RMG / GM may still take an L2 to be sure).
    """
    from models import AiInterviewLink

    manual = db.execute(
        select(InterviewEvent.id).where(
            InterviewEvent.profile_id == profile_id, InterviewEvent.kind == "L1_Interview",
            func.coalesce(InterviewEvent.result, "") != "").limit(1)
    ).first()
    if manual is not None:
        return True
    return db.execute(
        select(AiInterviewLink.id).where(
            AiInterviewLink.profile_id == profile_id,
            AiInterviewLink.completed_at.isnot(None)).limit(1)
    ).first() is not None


def set_applied_archive(db: Session, profile: CandidateProfile, archived: bool, user) -> bool:
    """RMG / GM move a CLOSED candidacy to the Applied Candidates Archive tab,
    or bring it back (30 Sep 2026, user rule: nothing is archived on its own).

    Archiving a live candidacy is refused (409) — it has to be rejected /
    withdrawn first. Idempotent: the same state again writes nothing and
    returns False. Logged as APPLIED_ARCHIVED / APPLIED_RESTORED (the latest
    wins). The caller commits.
    """
    from services.candidate_status import (
        ARCHIVED_ACTION, RESTORED_ACTION, archived_profile_ids,
    )

    stage = profile.pipeline_status.value if hasattr(profile.pipeline_status, "value") \
        else str(profile.pipeline_status or "")
    if archived and stage not in REJECTED_BUCKET:
        raise HTTPException(status_code=409,
                            detail="Only a rejected or withdrawn candidate can be archived.")
    now = profile.id in archived_profile_ids(db, [profile.id])
    if now == archived:
        return False
    actor = getattr(user, "full_name", None) or getattr(user, "username", None) or "RMG"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, getattr(user, "id", None),
                 ARCHIVED_ACTION if archived else RESTORED_ACTION,
                 f"{actor} {'moved the candidate to Archive' if archived else 'restored the candidate to Applied Candidates'}")
    return True
