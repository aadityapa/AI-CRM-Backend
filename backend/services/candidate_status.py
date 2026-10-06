"""The ONE candidate status every screen shows (25 Sep 2026).

The stored `candidate_profiles.pipeline_status` says which STAGE a candidate is
in ("RMG Review", "Customer L1 Interview"). It never said what had happened
IN that stage: whether the round was asked for, booked, passed or failed. So
the business ran a sheet on the side ("Manual L1 – Scheduled", "Customer L2 –
Failed", "HR Discussion", …). This module derives exactly that status from
facts the CRM already records, so no migration and no second source of truth:

  * the pipeline stage (the workflow — unchanged, still drives permissions),
  * the latest human round of each kind (`interview_events`: the Manual L1/L2
    RMG runs, the customer's L1/L2),
  * whether RMG asked TA to arrange a manual round (activity log),
  * the latest AI L1 (`ai_interview_links`) — the machine alternative to the
    manual L1, so a candidate with an AI interview booked never reads
    "Manual L1 – Yet to Schedule",
  * RMG's screening decision on a fresh applicant.

The front of the flow (user rule, 28 Sep 2026): **Sourcing** while the
candidate is with TA and not yet in RMG / GM's queue → **Technical Screening**
while RMG / GM screen the CV (screening Pending — a TA upload lands here at
once when the screening gate is on) → **Technical Interview** once RMG / GM
shortlist, until an L1 (AI or manual) is asked for or booked; from then the
round's own status (``AI L1 – Scheduled``, ``Manual L1 – Passed`` …) takes over.

Naming convention: ``<who runs the round> – <result>`` where the result is one
of Yet to Schedule · Scheduled · Passed · Failed, and the prefix (Manual L1,
Manual L2, AI L1, Customer L1, Customer L2) always says who ran it.

`derive_status` is PURE (no DB) so every branch is testable; the loaders
below batch the facts for a whole page in three queries. `STATUS_DEFS` is the
catalogue the UI's filter, badges and exports all read — ordered as the flow.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field, replace
from typing import Iterable

from models.profiles import PipelineStatus as PS

# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

#: Badge colours. The UI maps each to one token pair; nothing else picks colour.
NEUTRAL, INFO, WARN, OK, BAD = "neutral", "info", "warn", "ok", "bad"

#: TA's own calls on a fresh applicant (28 Sep 2026). A hold parks the
#: candidate in `candidate_profiles.budget_status` (the column the TA hold has
#: shared with the Pre-Onboarding budget flag since it began as a budget hold);
#: a TA rejection is a move to Rejected whose activity row says TA made it.
TA_HOLD = "TA_Hold"
TA_CLOSE_ACTIONS = {"TA_REJECTED": "ta"}

#: Filter groups, in display order.
GROUPS: list[tuple[str, str]] = [
    ("sourcing", "Sourcing"),
    ("screening", "Technical screening"),
    ("internal", "Technical interview & internal rounds"),
    ("customer", "Sales & customer"),
    ("selection", "Selection & joining"),
    ("parked", "Opportunity on hold"),
    ("closed", "Closed"),
]

_INTERNAL = frozenset({PS.SOURCING.value, PS.TECHNICAL_SCREENING.value, PS.RMG_REVIEW.value})
#: Before RMG Review: where the screening gate decides the status.
_PRE_REVIEW = frozenset({PS.SOURCING.value, PS.TECHNICAL_SCREENING.value})
_CUSTOMER = frozenset({PS.CUSTOMER_SCREENING.value, PS.CUSTOMER_INTERVIEW.value,
                       PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value})


@dataclass(frozen=True)
class StatusDef:
    key: str
    label: str
    tone: str
    group: str
    hint: str
    #: Pipeline stages this status can be derived from. Lets the list filter
    #: pre-narrow in SQL; pinned against `derive_status` by the tests.
    stages: frozenset[str] = field(default_factory=frozenset)

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "tone": self.tone,
                "group": self.group, "hint": self.hint}


def _d(key, label, tone, group, hint, stages) -> StatusDef:
    return StatusDef(key, label, tone, group, hint, frozenset(stages))


def _round_defs(prefix: str, name: str, group: str, stages: set[str],
                failed_extra: set[str], who: str, pending: bool = True) -> list[StatusDef]:
    """Yet to Schedule / Scheduled / Passed / Failed for one round."""
    out = []
    if pending:
        out.append(_d(f"{prefix}_pending", f"{name} – Yet to Schedule", NEUTRAL, group,
                      f"{name} is wanted but not booked yet. TA agrees a time with the candidate.",
                      stages))
    out += [
        _d(f"{prefix}_scheduled", f"{name} – Scheduled", WARN, group,
           f"{name} is booked. {who} records the verdict after the interview.", stages),
        _d(f"{prefix}_passed", f"{name} – Passed", OK, group,
           f"{name} passed. The next round or hand-off is due.", stages),
        _d(f"{prefix}_failed", f"{name} – Failed", BAD, group,
           f"{name} did not go well.", stages | failed_extra),
    ]
    return out


#: `candidate_profiles.budget_status` values (mirrors BUDGET_* there — this
#: module stays import-free of the service; pinned equal by a test).
BUDGET_CONCERN, BUDGET_OUT, BUDGET_RESOLVED = "Concern", "Out_of_Budget", "Resolved"
_BUDGET_STATUS_KEY = {BUDGET_CONCERN: "budget_concern", BUDGET_OUT: "budget_flagged",
                      BUDGET_RESOLVED: "budget_replied"}
#: Where the Pre-Onboarding budget hold can sit (HR's flag is allowed at both).
_BUDGET_STAGES = {PS.PREBOARDING.value, PS.HR_INTERVIEWING.value}
#: Activity rows that say whether Sales Head's last word on the terms was
#: "send back" (a resubmission clears it) — read by `load_facts`.
TERMS_SENT_BACK = "OFFER_SENT_BACK"
TERMS_SUBMITTED = "SUBMITTED_FOR_APPROVAL"

#: Opportunity stages that PARK every live candidacy on the deal (1 Oct 2026).
#: `Opportunity.pipeline_stage` values — "On_Hold" is "Customer Hold" in the UI.
DEAL_HOLD_STATUS_KEY: dict[str, str] = {"On_Hold": "deal_customer_hold",
                                        "Sales_Hold": "deal_sales_hold"}
#: Stored stages a deal hold never overrides: the candidacy is already settled.
_SETTLED = frozenset({
    PS.JOINED.value, PS.SALES_REJECTED.value, PS.RMG_REJECTED.value, PS.CUSTOMER_REJECTED.value,
    PS.CUSTOMER_SCREEN_REJECTED.value, PS.CUSTOMER_L1_REJECTED.value, PS.CUSTOMER_L2_REJECTED.value,
    PS.SELF_WITHDRAWN.value, PS.REJECTED.value,
})
_LIVE_PIPELINES = frozenset(p.value for p in PS) - _SETTLED
_HOLD_HINT = ("Sales put the opportunity on {who} — the candidacy is parked where it was, not closed. "
              "It resumes when the deal is reactivated; the candidate can be applied to other "
              "opportunities meanwhile.")

STATUS_DEFS: list[StatusDef] = [
    _d("sourcing", "Sourcing", INFO, "sourcing",
       "With TA — not sent to RMG / GM for screening yet.",
       {PS.SOURCING.value}),
    _d("ta_hold", "On Hold", WARN, "sourcing",
       "TA parked the candidate (not reachable, over budget, waiting on documents …) — the reason is "
       "in the history. Release the hold to continue.",
       _PRE_REVIEW),
    _d("technical_screening", "Technical Screening", WARN, "screening",
       "With RMG / GM — they screen the CV and shortlist or reject it on the Screening Desk.",
       _PRE_REVIEW),
    _d("rmg_rejected", "RMG Rejected", BAD, "screening",
       "RMG / GM rejected the candidate at screening or review.",
       _INTERNAL | {PS.RMG_REJECTED.value}),
    _d("technical_interview", "Technical Interview", INFO, "internal",
       "RMG / GM shortlisted the CV. Next: schedule the AI L1 or ask TA to book a manual L1.",
       _PRE_REVIEW),
    *_round_defs("ai_l1", "AI L1", "internal", set(_INTERNAL), {PS.RMG_REJECTED.value},
                 "The AI interview"),
    _d("ai_l1_review", "AI L1 – Under Review", WARN, "internal",
       "The AI interview is done; a recruiter put the result on hold / review.", _INTERNAL),
    *_round_defs("manual_l1", "Manual L1", "internal", set(_INTERNAL), {PS.RMG_REJECTED.value}, "RMG"),
    *_round_defs("manual_l2", "Manual L2", "internal", set(_INTERNAL), {PS.RMG_REJECTED.value}, "RMG"),
    _d("sales_review", "With Sales – Ready to Submit", INFO, "customer",
       "Internal rounds cleared. Sales reviews and submits the profile to the customer.",
       {PS.SALES_SCREENING.value}),
    _d("submitted_to_customer", "Submitted to Customer", INFO, "customer",
       "The profile is with the customer. Sales records whether they shortlist it.",
       {PS.CUSTOMER_SCREENING.value}),
    _d("customer_interviewing", "Customer Interviewing", WARN, "customer",
       "The customer is interviewing the candidate. Sales records Pass / Fail.",
       {PS.CUSTOMER_INTERVIEW.value}),
    _d("customer_l1_shortlisted", "Customer L1 – Profile Shortlisted", NEUTRAL, "customer",
       "The customer shortlisted the profile; the L1 interview is not booked yet.", _CUSTOMER),
    *_round_defs("customer_l1", "Customer L1", "customer", set(_CUSTOMER),
                 {PS.CUSTOMER_L1_REJECTED.value, PS.CUSTOMER_REJECTED.value}, "Sales", pending=False),
    *_round_defs("customer_l2", "Customer L2", "customer", set(_CUSTOMER),
                 {PS.CUSTOMER_L2_REJECTED.value, PS.CUSTOMER_REJECTED.value}, "Sales"),
    _d("candidate_selected", "Customer Shortlisted", OK, "selection",
       "The customer selected the candidate. Sales submits the rate and onboarding date.",
       {PS.SHORTLISTED.value}),
    _d("terms_sent_back", "Terms Sent Back", WARN, "selection",
       "Sales Head sent the rate / onboarding date back — Sales revises and resubmits.",
       {PS.SHORTLISTED.value}),
    _d("sales_head_approval", "Pending Sales Head Approval", WARN, "selection",
       "Sales submitted the terms. Only the Sales Head approves, sends back or rejects.",
       {PS.CUSTOMER_APPROVAL.value}),
    _d("hr_discussion", "HR Discussion", INFO, "selection",
       "Terms approved. HR reviews the candidate's details and requests the HR round.",
       {PS.HR_SCREENING.value}),
    _d("hr_round", "HR Round", WARN, "selection",
       "The HR round is booked. HR records the verdict, which moves the candidate on.",
       {PS.HR_INTERVIEWING.value}),
    _d("pre_onboarding", "Pre-Onboarding", INFO, "selection",
       "Offer stage. HR completes the joining formalities and marks Joined.",
       {PS.PREBOARDING.value}),
    _d("budget_concern", "Pre-Onboarding – Budget Concern", WARN, "selection",
       "HR's verdict was Not Recommend — HR checks the CTC / joining date and flags it to Sales if it "
       "does not fit.", _BUDGET_STAGES),
    _d("budget_flagged", "Out of Budget – With Sales", BAD, "selection",
       "HR flagged the terms out of budget — Sales talks to the customer and replies to HR.",
       _BUDGET_STAGES),
    _d("budget_replied", "Budget Reply – With HR", INFO, "selection",
       "Sales replied to HR's budget flag — HR decides whether to complete onboarding.", _BUDGET_STAGES),
    _d("joined", "Joined", OK, "selection", "The candidate has joined.", {PS.JOINED.value}),
    _d("deal_customer_hold", "Customer Hold", WARN, "parked", _HOLD_HINT.format(who="Customer Hold"),
       _LIVE_PIPELINES),
    _d("deal_sales_hold", "Sales Hold", WARN, "parked", _HOLD_HINT.format(who="Sales Hold"),
       _LIVE_PIPELINES),
    _d("sales_rejected", "Sales Rejected", BAD, "closed",
       "Sales decided not to submit the candidate to the customer.", {PS.SALES_REJECTED.value}),
    _d("customer_rejected", "Customer Rejected", BAD, "closed",
       "The customer rejected the candidate.",
       {PS.CUSTOMER_REJECTED.value, PS.CUSTOMER_SCREEN_REJECTED.value}),
    _d("self_withdrawn", "Self Withdrawn", BAD, "closed",
       "The candidate withdrew.", {PS.SELF_WITHDRAWN.value}),
    _d("rejected", "Rejected", BAD, "closed", "Closed as rejected.", {PS.REJECTED.value}),
    _d("ta_rejected", "Rejected by TA", BAD, "closed",
       "TA closed the candidate before screening — the reason is in the history.", {PS.REJECTED.value}),
]

STATUS_BY_KEY: dict[str, StatusDef] = {d.key: d for d in STATUS_DEFS}

#: Friendly stage names in the same vocabulary (used for "Self Withdrawn (…)").
STAGE_LABELS: dict[str, str] = {
    PS.SOURCING.value: "Sourcing",
    PS.TECHNICAL_SCREENING.value: "Technical Interviewing",
    PS.RMG_REVIEW.value: "RMG Review",
    PS.SALES_SCREENING.value: "Sales Screening",
    PS.CUSTOMER_SCREENING.value: "Submitted to Customer",
    PS.CUSTOMER_INTERVIEW.value: "Customer Interviewing",
    PS.L1_FEEDBACK.value: "Customer L1",
    PS.L2_FEEDBACK.value: "Customer L2",
    PS.SHORTLISTED.value: "Customer Shortlisted",
    PS.CUSTOMER_APPROVAL.value: "Pending Sales Head Approval",
    PS.HR_SCREENING.value: "HR Discussion",
    PS.HR_INTERVIEWING.value: "HR Round",
    PS.PREBOARDING.value: "Pre-Onboarding",
    PS.JOINED.value: "Joined",
}


def stage_label(value: str | None) -> str:
    return STAGE_LABELS.get(value or "", (value or "").replace("_", " "))


# ---------------------------------------------------------------------------
# Facts → status (pure)
# ---------------------------------------------------------------------------

#: Round states. "held" = the interview happened, no verdict recorded yet.
PENDING, SCHEDULED, HELD, PASSED, FAILED, REVIEW = (
    "pending", "scheduled", "held", "passed", "failed", "review")

_PASS_RESULTS = {"hire", "strong hire", "leaning hire", "selected", "pass", "passed", "shortlisted"}
_FAIL_RESULTS = {"no hire", "leaning no", "rejected", "reject", "fail", "failed", "not selected"}
#: A round in one of these statuses did not (and will not) happen — ignored.
_VOID_STATUSES = {"cancelled", "canceled", "no show"}


def round_state(status: str | None, result: str | None, has_time: bool) -> str | None:
    """One interview row → pending / scheduled / held / passed / failed (None = void)."""
    res = (result or "").strip().lower()
    if res in _PASS_RESULTS:
        return PASSED
    if res in _FAIL_RESULTS:
        return FAILED
    st = (status or "").strip().lower()
    if st in _VOID_STATUSES:
        return None
    if st == "completed":
        return HELD
    if has_time or st in {"scheduled", "in-progress"} or st.startswith("rescheduled"):
        return SCHEDULED
    return PENDING


def ai_state(effective_result: str | None) -> str:
    """AiInterviewLink.effective_result → round state."""
    value = (effective_result or "").strip().lower()
    if value in {"passed", "selected"}:
        return PASSED
    if value in {"failed", "rejected"}:
        return FAILED
    if value in {"on hold", "pending review"}:
        return REVIEW
    return SCHEDULED


@dataclass(frozen=True)
class StatusFacts:
    pipeline_status: str
    rmg_screening: str | None = None
    withdrawn_from: str | None = None
    #: round key (manual_l1 · manual_l2 · customer_l1 · customer_l2) → state
    rounds: dict[str, str] = field(default_factory=dict)
    #: round keys RMG asked TA to arrange
    requested: frozenset[str] = frozenset()
    ai_l1: str | None = None
    #: `candidate_profiles.budget_status` — "TA_Hold" parks a fresh applicant.
    budget_status: str | None = None
    #: "ta" when TA closed the candidacy at sourcing (`TA_CLOSE_ACTIONS`).
    ta_closed: str | None = None
    #: Sales Head's last word on the terms was "send back" (not yet resubmitted).
    terms_sent_back: bool = False
    #: `Opportunity.pipeline_stage` of the deal — a hold parks a live candidacy.
    opportunity_stage: str | None = None


@dataclass(frozen=True)
class CandidateStatus:
    key: str
    label: str
    tone: str
    group: str
    hint: str
    #: The PHASE the candidate is in (Sourcing … Onboarding) — `STAGES`.
    stage_key: str = ""
    stage_label: str = ""
    #: What is happening IN that phase: the round (Technical L1 Interview …)
    #: and its state (Scheduled / Passed …). `round_key` names the row fields
    #: that carry the round's date (`ROUND_WHEN_FIELDS` on the client).
    round_key: str | None = None
    round_label: str = ""
    round_state: str | None = None

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "tone": self.tone,
                "group": self.group, "hint": self.hint,
                "stage": {"key": self.stage_key, "label": self.stage_label},
                "round": {"key": self.round_key, "label": self.round_label,
                          "state": self.round_state}}


def _status(key: str, *, label: str | None = None, hint: str | None = None) -> CandidateStatus:
    d = STATUS_BY_KEY[key]
    return CandidateStatus(d.key, label or d.label, d.tone, d.group, hint or d.hint)


def _round_status(prefix: str, state: str) -> CandidateStatus:
    """A round state → its catalogue entry. `held` reads as Scheduled until the
    verdict lands (the sheet has no separate step); the hint says so."""
    if state == HELD:
        return _status(f"{prefix}_scheduled",
                       hint="Interview held — waiting for the verdict to be recorded.")
    suffix = {PENDING: "scheduled", SCHEDULED: "scheduled", PASSED: "passed",
              FAILED: "failed", REVIEW: "review"}[state]
    return _status(f"{prefix}_{suffix}")


def _internal(f: StatusFacts) -> CandidateStatus:
    """Sourcing · Technical Interviewing · RMG Review: the latest internal round wins."""
    m1, m2 = f.rounds.get("manual_l1"), f.rounds.get("manual_l2")
    if m2:
        return _round_status("manual_l2", m2)
    if "manual_l2" in f.requested:
        return _status("manual_l2_pending")
    if m1:
        return _round_status("manual_l1", m1)
    if "manual_l1" in f.requested:
        return _status("manual_l1_pending")
    if f.ai_l1:
        return _round_status("ai_l1", f.ai_l1)
    if "ai_l1" in f.requested:
        return _status("ai_l1_pending")
    screening = f.rmg_screening or ""
    if f.pipeline_status in _PRE_REVIEW and f.budget_status == TA_HOLD:
        return _status("ta_hold")
    if screening == "Rejected":
        return _status("rmg_rejected", hint="RMG / GM rejected the candidate at CV screening.")
    # The front of the flow (user rule, 28 Sep 2026): Sourcing with TA →
    # Technical Screening with RMG / GM → Technical Interview once shortlisted,
    # until an L1 is asked for / booked (handled above).
    if f.pipeline_status in _PRE_REVIEW:
        if screening == "Pending":
            return _status("technical_screening")
        if screening == "Shortlisted":
            return _status("technical_interview")
    if f.pipeline_status == PS.SOURCING.value:
        return _status("sourcing")
    return _status("manual_l1_pending")


def _customer(f: StatusFacts) -> CandidateStatus:
    """The customer ladder: the latest customer round wins, else the stage."""
    c1, c2 = f.rounds.get("customer_l1"), f.rounds.get("customer_l2")
    if c2:
        return _round_status("customer_l2", c2)
    if f.pipeline_status == PS.L2_FEEDBACK.value:
        # Sales moved the candidate on to the customer's L2 (29 Sep 2026) and
        # nothing is booked yet — TA's move, never "Scheduled".
        return _status("customer_l2_pending")
    if c1:
        if c1 == PENDING:
            return _status("customer_l1_shortlisted")
        return _round_status("customer_l1", c1)
    stage = f.pipeline_status
    if stage == PS.CUSTOMER_SCREENING.value:
        return _status("submitted_to_customer")
    if stage == PS.CUSTOMER_INTERVIEW.value:
        return _status("customer_interviewing")
    return _round_status("customer_l1", HELD)


#: Stages whose status is the stage itself.
_FIXED: dict[str, str] = {
    PS.SALES_SCREENING.value: "sales_review",
    PS.SHORTLISTED.value: "candidate_selected",
    PS.CUSTOMER_APPROVAL.value: "sales_head_approval",
    PS.HR_SCREENING.value: "hr_discussion",
    PS.HR_INTERVIEWING.value: "hr_round",
    PS.PREBOARDING.value: "pre_onboarding",
    PS.JOINED.value: "joined",
    PS.SALES_REJECTED.value: "sales_rejected",
    PS.CUSTOMER_L1_REJECTED.value: "customer_l1_failed",
    PS.CUSTOMER_L2_REJECTED.value: "customer_l2_failed",
}


def _derive(f: StatusFacts) -> CandidateStatus:
    stage = f.pipeline_status
    # A deal on hold parks every live candidacy on it (1 Oct 2026, user report:
    # "the opportunity is on Customer Hold, why does the candidate still read
    # Submitted to Customer?"). The stored stage is untouched — Reactivate
    # resumes exactly there — and a settled candidacy keeps its own word.
    if f.opportunity_stage in DEAL_HOLD_STATUS_KEY and stage not in _SETTLED:
        return _status(DEAL_HOLD_STATUS_KEY[f.opportunity_stage])
    if stage == PS.SHORTLISTED.value and f.terms_sent_back:
        return _status("terms_sent_back")
    if stage in _BUDGET_STAGES and f.budget_status in _BUDGET_STATUS_KEY:
        return _status(_BUDGET_STATUS_KEY[f.budget_status])
    if stage in _FIXED:
        return _status(_FIXED[stage])
    if stage in _INTERNAL:
        return _internal(f)
    if stage in _CUSTOMER:
        return _customer(f)
    if stage == PS.RMG_REJECTED.value:
        for prefix in ("manual_l2", "manual_l1"):
            if f.rounds.get(prefix) == FAILED:
                return _status(f"{prefix}_failed")
        if f.ai_l1 == FAILED:
            return _status("ai_l1_failed")
        return _status("rmg_rejected")
    if stage == PS.CUSTOMER_REJECTED.value:
        for prefix in ("customer_l2", "customer_l1"):
            if f.rounds.get(prefix) == FAILED:
                return _status(f"{prefix}_failed")
        return _status("customer_rejected")
    if stage == PS.REJECTED.value:
        return _status("ta_rejected" if f.ta_closed == "ta" else "rejected")
    if stage == PS.CUSTOMER_SCREEN_REJECTED.value:
        return _status("customer_rejected", hint="The customer rejected the profile at CV screening.")
    if stage == PS.SELF_WITHDRAWN.value:
        if f.withdrawn_from:
            where = stage_label(f.withdrawn_from)
            return _status("self_withdrawn", label=f"Self Withdrawn ({where})",
                           hint=f"The candidate withdrew at {where}.")
        return _status("self_withdrawn")
    # An unknown stage must still render something honest, never crash a list.
    return CandidateStatus(stage or "unknown", stage_label(stage), NEUTRAL, "closed", "")


# ---------------------------------------------------------------------------
# Stage (the phase) and round (what is happening in it) — 28 Sep 2026
# ---------------------------------------------------------------------------

#: The phases a candidate moves through, in order — the Applied Candidates
#: "Stage" column and filter chips speak this vocabulary.
STAGES: list[tuple[str, str]] = [
    ("sourcing", "Sourcing"),
    ("technical_screening", "Technical Screening"),
    ("technical_interview", "Technical Interview"),
    ("sales_screening", "Sales Screening"),
    ("customer_screening", "Customer Screening"),
    ("customer_interviewing", "Customer Interviewing"),
    ("selection", "Customer Shortlisted"),   # renamed 28 Sep 2026 (user) — was "Candidate Selected"
    ("hr_screening", "HR Screening"),
    ("hr_interviewing", "HR Interviewing"),   # 1 Oct 2026 (user): its own chip after HR Screening
    ("onboarding", "Onboarding"),
    ("joined", "Joined"),                     # 1 Oct 2026 (user): its own chip after Onboarding
    ("closed", "Closed"),
]
STAGE_LABEL: dict[str, str] = dict(STAGES)
#: The phase TA owns: Technical Screening / Hold / Reject are offered only here.
SOURCING_STAGE = "sourcing"

_STAGE_BY_PIPELINE: dict[str, str] = {
    PS.RMG_REVIEW.value: "technical_interview",
    PS.SALES_SCREENING.value: "sales_screening",
    PS.SALES_REJECTED.value: "sales_screening",
    PS.CUSTOMER_SCREENING.value: "customer_screening",
    PS.CUSTOMER_SCREEN_REJECTED.value: "customer_screening",
    PS.CUSTOMER_INTERVIEW.value: "customer_interviewing",
    PS.L1_FEEDBACK.value: "customer_interviewing",
    PS.L2_FEEDBACK.value: "customer_interviewing",
    PS.CUSTOMER_L1_REJECTED.value: "customer_interviewing",
    PS.CUSTOMER_L2_REJECTED.value: "customer_interviewing",
    PS.CUSTOMER_REJECTED.value: "customer_interviewing",
    PS.SHORTLISTED.value: "selection",
    PS.CUSTOMER_APPROVAL.value: "selection",
    PS.HR_SCREENING.value: "hr_screening",
    PS.HR_INTERVIEWING.value: "hr_interviewing",
    PS.PREBOARDING.value: "onboarding",
    PS.JOINED.value: "joined",
}


def _pre_review_stage(f: StatusFacts) -> str:
    """Before RMG Review the facts, not the stored stage, say where they are."""
    if f.rounds.get("manual_l1") or f.rounds.get("manual_l2") or f.ai_l1 or f.requested:
        return "technical_interview"
    screening = f.rmg_screening or ""
    if f.budget_status == TA_HOLD:
        return "sourcing"
    if screening in ("Pending", "Rejected"):
        return "technical_screening"
    if screening == "Shortlisted":
        return "technical_interview"
    return "sourcing"


def stage_for(f: StatusFacts) -> str:
    """The phase key (`STAGES`) for one candidate. PURE."""
    stage = f.pipeline_status
    if stage in _PRE_REVIEW:
        return _pre_review_stage(f)
    if stage in _STAGE_BY_PIPELINE:
        return _STAGE_BY_PIPELINE[stage]
    if stage == PS.RMG_REJECTED.value:
        return "technical_interview" if (f.rounds or f.ai_l1) else "technical_screening"
    if stage == PS.SELF_WITHDRAWN.value and f.withdrawn_from:
        return stage_for(StatusFacts(
            pipeline_status=f.withdrawn_from, rmg_screening=f.rmg_screening,
            rounds=f.rounds, requested=f.requested, ai_l1=f.ai_l1,
            budget_status=f.budget_status))
    if stage == PS.REJECTED.value and f.ta_closed:
        return "sourcing"
    return "closed"


#: Status-key prefix → (round key, round name). The round key is what the
#: client uses to find the round's date on the row.
_ROUNDS: dict[str, tuple[str, str]] = {
    "ai_l1": ("ai_l1", "Technical L1 Interview (AI)"),
    "manual_l1": ("manual_l1", "Technical L1 Interview"),
    "manual_l2": ("manual_l2", "Technical L2 Interview"),
    "customer_l1": ("customer_l1", "Customer L1 Interview"),
    "customer_l2": ("customer_l2", "Customer L2 Interview"),
}
_ROUND_STATES = {"pending": "Yet to Schedule", "scheduled": "Scheduled", "passed": "Passed",
                 "failed": "Failed", "review": "Under Review", "shortlisted": "Yet to Schedule"}
#: Non-round statuses → (round key, name, state) for the Status column.
_STATUS_ROUND: dict[str, tuple[str | None, str, str | None]] = {
    "sourcing": (None, "New Applicant", None),
    "ta_hold": (None, "On Hold", None),
    "technical_screening": (None, "CV Screening", "With RMG / GM"),
    "technical_interview": (None, "Technical L1 Interview", "Yet to Schedule"),
    "hr_discussion": ("hr", "HR Discussion", None),
    "hr_round": ("hr", "HR Round", "Scheduled"),
    "pre_onboarding": (None, "Preboarding", None),
    "terms_sent_back": (None, "Terms", "Sent back"),
    "budget_concern": (None, "Pre-Onboarding", "Budget concern"),
    "budget_flagged": (None, "Budget", "With Sales"),
    "budget_replied": (None, "Budget", "With HR"),
    "deal_customer_hold": (None, "Opportunity", "Customer Hold"),
    "deal_sales_hold": (None, "Opportunity", "Sales Hold"),
}


def round_for(status: CandidateStatus) -> tuple[str | None, str, str | None]:
    """(round key, round name, state) the Status column prints. PURE."""
    if status.key in _STATUS_ROUND:
        return _STATUS_ROUND[status.key]
    for prefix, (key, name) in _ROUNDS.items():
        if status.key.startswith(prefix + "_"):
            return key, name, _ROUND_STATES.get(status.key[len(prefix) + 1:])
    return None, status.label, None


def derive_status(f: StatusFacts) -> CandidateStatus:
    """The status a person should read for one candidate profile — with the
    phase it sits in and the round it is waiting on."""
    base = _derive(f)
    stage = stage_for(f)
    rkey, rlabel, rstate = round_for(base)
    return replace(base, stage_key=stage, stage_label=STAGE_LABEL[stage],
                               round_key=rkey, round_label=rlabel, round_state=rstate)


# ---------------------------------------------------------------------------
# Loaders (batched)
# ---------------------------------------------------------------------------

#: interview_events.kind → round key. Customer_Interview rows tagged stage "L2"
#: are legacy customer L2 rows (see candidate_profiles._upsert_customer_round).
_KIND_TO_ROUND = {
    "L1_Interview": "manual_l1",
    "L2_F2F": "manual_l2",
    "Customer_Interview": "customer_l1",
    "Customer_L2": "customer_l2",
}
_REQUEST_TO_ROUND = {"L1_REQUESTED": "manual_l1", "L2_REQUESTED": "manual_l2",
                     "AI_L1_REQUESTED": "ai_l1"}
_CHUNK = 1000


def _value(v) -> str | None:
    return getattr(v, "value", v)


def _chunks(ids: list[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield ids[i:i + _CHUNK]


def load_facts(db, profiles) -> dict[int, StatusFacts]:
    """StatusFacts for every profile, in three queries per 1,000 profiles.

    `profiles` may be ORM rows or any objects/Rows with `id`, `pipeline_status`,
    `rmg_screening_status` and `withdrawn_from_status`.
    """
    from sqlalchemy import select

    from models import (AiInterviewLink, CandidateProfile, CandidateProfileActivityLog,
                        InterviewEvent, Opportunity)

    base = {int(p.id): p for p in profiles if getattr(p, "id", None)}
    ids = sorted(base)
    rounds: dict[int, dict[str, str]] = {pid: {} for pid in ids}
    requested: dict[int, set[str]] = {pid: set() for pid in ids}
    ai: dict[int, str] = {}
    ta_closed: dict[int, str] = {}
    sent_back: dict[int, bool] = {}
    deal_stage: dict[int, str] = {}
    for chunk in _chunks(ids):
        # The deal's stage — a hold parks the candidacy (one query, never per row).
        for pid, opp_stage in db.execute(
            select(CandidateProfile.id, Opportunity.pipeline_stage)
            .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            .where(CandidateProfile.id.in_(chunk))
        ).all():
            deal_stage[pid] = _value(opp_stage)
        for pid, kind, stage, status, result, when in db.execute(
            select(InterviewEvent.profile_id, InterviewEvent.kind, InterviewEvent.stage,
                   InterviewEvent.status, InterviewEvent.result, InterviewEvent.scheduled_at)
            .where(InterviewEvent.profile_id.in_(chunk),
                   InterviewEvent.kind.in_(tuple(_KIND_TO_ROUND)))
            .order_by(InterviewEvent.id)
        ).all():
            key = _KIND_TO_ROUND[kind]
            if kind == "Customer_Interview" and (stage or "").strip().upper() == "L2":
                key = "customer_l2"
            state = round_state(status, result, when is not None)
            if state is not None:          # ascending id → the latest real round wins
                rounds[pid][key] = state
        for pid, action in db.execute(
            select(CandidateProfileActivityLog.profile_id, CandidateProfileActivityLog.action_type)
            .where(CandidateProfileActivityLog.profile_id.in_(chunk),
                   CandidateProfileActivityLog.action_type.in_(
                       tuple(_REQUEST_TO_ROUND) + tuple(TA_CLOSE_ACTIONS)
                       + (TERMS_SENT_BACK, TERMS_SUBMITTED)))
            .order_by(CandidateProfileActivityLog.id)
        ).all():
            if action in (TERMS_SENT_BACK, TERMS_SUBMITTED):
                sent_back[pid] = action == TERMS_SENT_BACK          # the latest wins
            elif action in TA_CLOSE_ACTIONS:
                ta_closed[pid] = TA_CLOSE_ACTIONS[action]   # the latest wins
            else:
                requested[pid].add(_REQUEST_TO_ROUND[action])
        # Same precedence as latest_ai_interviews: completed first, then newest.
        for link in db.execute(
            select(AiInterviewLink)
            .where(AiInterviewLink.profile_id.in_(chunk))
            .order_by(AiInterviewLink.profile_id,
                      AiInterviewLink.completed_at.desc().nullslast(),
                      AiInterviewLink.id.desc())
        ).scalars().all():
            ai.setdefault(link.profile_id, ai_state(link.effective_result))
    return {
        pid: StatusFacts(
            pipeline_status=_value(base[pid].pipeline_status) or "",
            rmg_screening=getattr(base[pid], "rmg_screening_status", None),
            withdrawn_from=getattr(base[pid], "withdrawn_from_status", None),
            rounds=rounds[pid],
            requested=frozenset(requested[pid]),
            ai_l1=ai.get(pid),
            budget_status=getattr(base[pid], "budget_status", None),
            ta_closed=ta_closed.get(pid),
            terms_sent_back=sent_back.get(pid, False),
            opportunity_stage=deal_stage.get(pid),
        )
        for pid in ids
    }


def statuses_for(db, profiles) -> dict[int, dict]:
    """profile id → the status dict every payload carries as `candidate_status`.

    Never raises: a lookup failure degrades to the stage-only status, because
    a list must not 500 over a label.
    """
    try:
        facts = load_facts(db, profiles)
    except Exception:  # pragma: no cover — degrade, never break a list
        import logging
        logging.getLogger("karnex.crm.candidate_status").warning(
            "candidate status facts failed; falling back to stage only", exc_info=True)
        facts = {int(p.id): StatusFacts(
            pipeline_status=_value(p.pipeline_status) or "",
            rmg_screening=getattr(p, "rmg_screening_status", None),
            withdrawn_from=getattr(p, "withdrawn_from_status", None),
            budget_status=getattr(p, "budget_status", None))
            for p in profiles if getattr(p, "id", None)}
    return {pid: derive_status(f).as_dict() for pid, f in facts.items()}


def attach_to_rows(db, rows: list[dict], *, id_key: str = "profile_id",
                   out_key: str = "profile_status") -> None:
    """Add the status of each row's profile (rows keyed by a profile id) in place.

    For lists whose rows are not profiles themselves — Applied Candidates
    (resume rows), the Screening Desk. One profile query + the three fact
    queries for the whole page; a row with no profile gets None.
    """
    from sqlalchemy import select

    from models import CandidateProfile

    ids = {int(r[id_key]) for r in rows if r.get(id_key)}
    statuses: dict[int, dict] = {}
    if ids:
        profiles = db.execute(
            select(CandidateProfile.id, CandidateProfile.pipeline_status,
                   CandidateProfile.rmg_screening_status, CandidateProfile.withdrawn_from_status,
                   CandidateProfile.budget_status)
            .where(CandidateProfile.id.in_(ids))
        ).all()
        statuses = statuses_for(db, profiles)
    for row in rows:
        pid = row.get(id_key)
        row[out_key] = statuses.get(int(pid)) if pid else None


def parse_status_keys(raw: str | None) -> list[str]:
    """"manual_l1_scheduled,joined" → validated keys (ValueError on an unknown one)."""
    keys = [k.strip() for k in (raw or "").split(",") if k.strip()]
    unknown = [k for k in keys if k not in STATUS_BY_KEY]
    if unknown:
        raise ValueError(f"Unknown status {', '.join(repr(k) for k in unknown)}")
    return keys


def stages_for_keys(keys: Iterable[str]) -> set[str]:
    """Every pipeline stage that can produce one of these statuses."""
    out: set[str] = set()
    for k in keys:
        out |= STATUS_BY_KEY[k].stages
    return out


def profile_ids_with_status(db, stmt, keys: list[str]) -> list[int]:
    """Ids of the profiles `stmt` selects whose derived status is one of `keys`.

    The derived status has no column, so the filter runs in two steps: SQL
    narrows to the stages that can produce the wanted statuses, then the
    status is derived for just those rows. The caller adds
    `CandidateProfile.id.in_(...)` so pagination, counts and sorting stay
    exact — filtering a fetched page would page through a lie.
    """
    from models import CandidateProfile

    stages = stages_for_keys(keys)
    rows = db.execute(
        stmt.with_only_columns(
            CandidateProfile.id, CandidateProfile.pipeline_status,
            CandidateProfile.rmg_screening_status, CandidateProfile.withdrawn_from_status,
            CandidateProfile.budget_status,
        ).where(CandidateProfile.pipeline_status.in_([PS(s) for s in sorted(stages)]))
        .order_by(None)
    ).all()
    wanted = set(keys)
    found = statuses_for(db, rows)
    return [pid for pid, status in found.items() if status["key"] in wanted]


def parse_phases(raw: str | None) -> list[str]:
    """"sourcing,technical_screening" → validated phase keys (`STAGES`)."""
    phases = [k.strip() for k in (raw or "").split(",") if k.strip()]
    unknown = [k for k in phases if k not in STAGE_LABEL]
    if unknown:
        raise ValueError(f"Unknown stage {', '.join(repr(k) for k in unknown)}")
    return phases


def _pipelines_for_phase(phase: str, closed: set[str]) -> set[str]:
    """Stored pipeline stages a LIVE candidate in `phase` can sit in."""
    out = {v for v, k in _STAGE_BY_PIPELINE.items() if k == phase}
    if phase in ("sourcing", "technical_screening", "technical_interview"):
        out |= _PRE_REVIEW
    return out - closed


def profile_ids_in_phase(db, stmt, phases: list[str]) -> list[int]:
    """Ids of the profiles `stmt` selects that sit in one of `phases`.

    A live phase is the derived stage of a candidate still in the pipeline
    (the three front phases share the Sourcing stage, so the facts decide);
    "closed" is every rejected / withdrawn candidacy. SQL narrows first, the
    derivation runs only on that slice — same two-step as the status filter.
    """
    from services.candidate_profiles import REJECTED_BUCKET  # lazy: that module imports this one
    from models import CandidateProfile

    cols = (CandidateProfile.id, CandidateProfile.pipeline_status,
            CandidateProfile.rmg_screening_status, CandidateProfile.withdrawn_from_status,
            CandidateProfile.budget_status)
    found: set[int] = set()
    if "closed" in phases:
        found |= {r[0] for r in db.execute(
            stmt.with_only_columns(CandidateProfile.id)
            .where(CandidateProfile.pipeline_status.in_([PS(s) for s in sorted(REJECTED_BUCKET)]))
            .order_by(None)).all()}
    live = [p for p in phases if p != "closed"]
    pipelines = set().union(*(_pipelines_for_phase(p, REJECTED_BUCKET) for p in live)) if live else set()
    if pipelines:
        rows = db.execute(
            stmt.with_only_columns(*cols)
            .where(CandidateProfile.pipeline_status.in_([PS(s) for s in sorted(pipelines)]))
            .order_by(None)
        ).all()
        wanted = set(live)
        found |= {pid for pid, f in load_facts(db, rows).items() if stage_for(f) in wanted}
    return sorted(found)


def phase_counts(db, stmt) -> dict[str, int]:
    """How many of the profiles `stmt` selects sit in each phase (+ "all").

    The stage chips print these (28 Sep 2026: "the filters must line up with
    the Stage column") — the SAME `stage_for` the column prints, and "closed"
    is the same `REJECTED_BUCKET` rule as `profile_ids_in_phase`, so a chip's
    count is exactly what clicking it lists. One query + one facts load.
    """
    from services.candidate_profiles import REJECTED_BUCKET  # lazy: that module imports this one
    from models import CandidateProfile

    rows = db.execute(stmt.with_only_columns(
        CandidateProfile.id, CandidateProfile.pipeline_status,
        CandidateProfile.rmg_screening_status, CandidateProfile.withdrawn_from_status,
        CandidateProfile.budget_status).order_by(None)).all()
    counts = {key: 0 for key, _ in STAGES}
    closed = {_value(r[1]) for r in rows if _value(r[1]) in REJECTED_BUCKET}
    facts = load_facts(db, rows)
    for r in rows:
        f = facts.get(r[0])
        if f is None:
            continue
        key = "closed" if _value(r[1]) in closed else stage_for(f)
        counts[key] = counts.get(key, 0) + 1
    counts["all"] = len(rows)
    return counts


#: Applied Candidates / Candidate Profiles buckets. A candidacy sits in Archive
#: when someone archived it by hand, OR (5 Oct 2026) its opportunity is on
#: Customer / Sales Hold — see `archive_clause`.
LIVE_BUCKET, ARCHIVE_BUCKET = "live", "archive"


#: Activity actions that move a candidacy in and out of Archive by hand (RMG /
#: GM / Sales, any stage since 5 Oct 2026). The latest of the two wins.
ARCHIVED_ACTION, RESTORED_ACTION = "APPLIED_ARCHIVED", "APPLIED_RESTORED"

#: Opportunity stages that PARK their live candidacies in Archive (5 Oct 2026,
#: user report: a held position's candidates still filled every Candidate
#: Profiles list). Reactivating the deal brings them back — nothing is written.
HOLD_STAGES = tuple(DEAL_HOLD_STATUS_KEY)


def archive_clause():
    """SQL: the profile is in Archive — archived by hand (latest archive action
    is ARCHIVED) OR its deal is on hold while the candidacy is still live
    (Joined / closed candidacies keep their own place). ONE definition for the
    Candidate Profiles directory, its export and Applied Candidates, so a hold
    and a manual archive read the same everywhere. Correlated on CandidateProfile.
    """
    from sqlalchemy import and_, func, or_, select
    from models import (
        CandidateProfile as CP, CandidateProfileActivityLog as Log, Opportunity, PipelineStage,
    )

    # ⚠️ Every branch must be TRUE or FALSE, never NULL: the live list uses
    # NOT(archive_clause()), and NOT(NULL) is NULL — a profile with no archive
    # row (the subquery is NULL) would vanish from EVERY list (6 Oct 2026: the
    # Candidate Profiles page read 0 after deploy). Hence the COALESCE and the
    # explicit IS NOT NULL on the opportunity.
    latest = (select(Log.action_type)
              .where(Log.profile_id == CP.id, Log.action_type.in_((ARCHIVED_ACTION, RESTORED_ACTION)))
              .order_by(Log.id.desc()).limit(1)
              .correlate(CP).scalar_subquery())
    held = and_(
        CP.opportunity_id.is_not(None),
        CP.opportunity_id.in_(select(Opportunity.id).where(
            Opportunity.pipeline_stage.in_([PipelineStage(s) for s in HOLD_STAGES]))),
        CP.pipeline_status.not_in([PS(s) for s in sorted(_SETTLED)]),
    )
    return or_(func.coalesce(latest, "") == ARCHIVED_ACTION, held)


def archive_reasons(db, profile_ids) -> dict[int, str]:
    """`{profile_id: "manual" | "hold"}` for the archived ones among `profile_ids`.
    A hand archive wins the label (it survives a reactivation). Two queries."""
    from sqlalchemy import select
    from models import CandidateProfile as CP

    ids = list({int(i) for i in profile_ids if i is not None})
    if not ids:
        return {}
    out = {pid: "manual" for pid in archived_profile_ids(db, ids)}
    held = db.execute(select(CP.id).where(CP.id.in_(ids), archive_clause())).scalars()
    for pid in held:
        out.setdefault(int(pid), "hold")
    return out


def archived_profile_ids(db, profile_ids) -> set[int]:
    """Profiles whose latest archive action is ARCHIVED. One query."""
    from models import CandidateProfileActivityLog as Log
    from sqlalchemy import select

    ids = list({int(i) for i in profile_ids if i is not None})
    if not ids:
        return set()
    rows = db.execute(select(Log.profile_id, Log.action_type, Log.id).where(
        Log.profile_id.in_(ids), Log.action_type.in_((ARCHIVED_ACTION, RESTORED_ACTION)))
        .order_by(Log.profile_id, Log.id)).all()
    latest: dict[int, str] = {}
    for pid, action, _ in rows:
        latest[pid] = action
    return {pid for pid, action in latest.items() if action == ARCHIVED_ACTION}


_STATUS_CHANGE_RE = re.compile(r"^\s*(\w+)\s*->\s*(\w+)\s*:?\s*(.*)$", re.S)


def closing_notes(db, profile_ids) -> dict[int, dict]:
    """Who closed each candidacy, when, and WHY (1 Oct 2026, user ask: "whoever
    rejected the candidate — show the note why, in the Rejected filter").

    Every rejection / withdrawal already writes ONE `STATUS_CHANGE` row shaped
    "<from> -> <to>: <reason>" (`perform_transition` requires the reason), so
    this reads the LATEST such row whose target is in `REJECTED_BUCKET` per
    profile — one query + one names lookup. Returns
    `{profile_id: {status, reason, by_id, by, at}}`; a profile with no closing
    row is absent.
    """
    from services.candidate_profiles import REJECTED_BUCKET  # lazy: that module imports this one
    from services.revenue_report import _user_names
    from models import CandidateProfileActivityLog as Log
    from sqlalchemy import select

    ids = list({int(i) for i in profile_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(select(Log.profile_id, Log.user_id, Log.comment, Log.timestamp).where(
        Log.profile_id.in_(ids), Log.action_type == "STATUS_CHANGE")
        .order_by(Log.profile_id, Log.id)).all()
    latest: dict[int, dict] = {}
    for pid, uid, comment, at in rows:
        m = _STATUS_CHANGE_RE.match(comment or "")
        if not m:
            continue
        if m.group(2) not in REJECTED_BUCKET:
            latest.pop(int(pid), None)   # reopened (re-applied) — the old note no longer applies
            continue
        latest[int(pid)] = {
            "status": m.group(2),
            "reason": (m.group(3) or "").strip() or None,
            "by_id": uid,
            "at": at.isoformat() if at else None,
        }
    names = _user_names(db, {d["by_id"] for d in latest.values() if d["by_id"]})
    for d in latest.values():
        d["by"] = names.get(d["by_id"]) if d["by_id"] else None
    return latest


def applied_buckets(db, stmt) -> tuple[list[int], list[int]]:
    """(live profile ids, archived profile ids) of the profiles `stmt` selects,
    split by `archive_clause` (hand archive, or the deal on hold). Two queries.
    """
    from models import CandidateProfile

    base = stmt.with_only_columns(CandidateProfile.id).order_by(None)
    every = [r[0] for r in db.execute(base).all()]
    archived_set = {r[0] for r in db.execute(base.where(archive_clause())).all()}
    return [p for p in every if p not in archived_set], [p for p in every if p in archived_set]


def status_counts(db, stmt) -> dict:
    """Counts for the Applied Candidates status chips (30 Sep 2026: the chips
    filter by STATUS, not stage — the Stage column is hidden there).

    `{"live": {status key: n}, "archive": {status key: n}, "live_total": n,
    "archive_total": n, "phases": {"live": {stage key: n}, "archive": {…}}}`
    over the profiles `stmt` selects — the SAME `derive_status` every row
    prints, so a chip's count is what clicking it lists. `phases` (1 Oct 2026:
    the Applied Candidates chips are STAGES again, per bucket) follows the
    `phase_counts` rule — a closed candidacy counts under "closed", whatever
    phase it closed in. One query + one facts load.
    """
    from services.candidate_profiles import REJECTED_BUCKET  # lazy: that module imports this one
    from models import CandidateProfile

    rows = db.execute(stmt.with_only_columns(
        CandidateProfile.id, CandidateProfile.pipeline_status,
        CandidateProfile.rmg_screening_status, CandidateProfile.withdrawn_from_status,
        CandidateProfile.budget_status).order_by(None)).all()
    statuses = statuses_for(db, rows)
    flagged = set(applied_buckets(db, stmt)[1])
    out = {LIVE_BUCKET: {}, ARCHIVE_BUCKET: {}, "live_total": 0, "archive_total": 0,
           "phases": {LIVE_BUCKET: {}, ARCHIVE_BUCKET: {}}}
    for r in rows:
        status = statuses.get(r[0])
        if status is None:
            continue
        bucket = ARCHIVE_BUCKET if r[0] in flagged else LIVE_BUCKET
        out[bucket][status["key"]] = out[bucket].get(status["key"], 0) + 1
        out[f"{bucket}_total"] += 1
        phase = "closed" if _value(r[1]) in REJECTED_BUCKET else status["stage"]["key"]
        out["phases"][bucket][phase] = out["phases"][bucket].get(phase, 0) + 1
    return out


def catalogue() -> dict:
    """The filter / legend payload: groups in order, statuses in flow order.

    `active` / `closed` say which bucket of the directory can hold a status
    (the list's Active / Closed toggle is a stage filter), so each dropdown
    offers only statuses that can actually appear under it.
    """
    from services.candidate_profiles import REJECTED_BUCKET  # lazy: that module imports this one

    return {
        "groups": [{"key": k, "label": label} for k, label in GROUPS],
        "stages": [{"key": k, "label": label} for k, label in STAGES],
        "statuses": [
            {**d.as_dict(),
             "closed": bool(d.stages & REJECTED_BUCKET),
             "active": bool(d.stages - REJECTED_BUCKET)}
            for d in STATUS_DEFS
        ],
    }
