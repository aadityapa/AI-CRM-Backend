"""The end of the pipeline: customer interview → offer → approval → preboarding.

Two rules matter here and both are easy to get wrong:

  * The person who proposes offer terms must not be the person who approves
    them. Sales attaches the offer; Sales Head signs it off.
  * Customer Approval means "these are the terms" — entering it without an
    offer on record asks Sales Head to approve nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    ENTRY_REQUIREMENTS, STAGE_AUTHORITY, _ARRIVAL_NOTIFY_ROLE, _CUSTOMER_VERDICT,
    allowed_next_statuses, visible_statuses_for,
)


class FakeUser:
    def __init__(self, roles, is_admin=False):
        self.id = 1
        self.roles = list(roles)
        self.is_admin = is_admin


# ------------------------------------------------------- the approval gate

def test_sales_cannot_approve_its_own_offer():
    """Sales submits the rate + customer onboarding date; only Sales Head
    signs them off (re-confirmed 2 Sep 2026, user decision)."""
    assert STAGE_AUTHORITY[PS.CUSTOMER_APPROVAL.value] == {"Sales_Head"}


def test_sales_head_can_send_the_terms_back_to_sales():
    """A wrong rate is not a rejected candidate — the offer goes back to Sales
    (Shortlisted) to be redone, instead of the only exit being terminal."""
    assert PS.SHORTLISTED.value in allowed_next_statuses(PS.CUSTOMER_APPROVAL.value)
    assert PS.CUSTOMER_REJECTED.value in allowed_next_statuses(PS.CUSTOMER_APPROVAL.value)


def test_sales_still_owns_the_stages_before_approval():
    """Narrowing the approval must not strip Sales of its own pipeline."""
    assert "Sales" in STAGE_AUTHORITY[PS.SALES_SCREENING.value]
    assert "Sales" in STAGE_AUTHORITY[PS.CUSTOMER_SCREENING.value]
    assert "Sales" in STAGE_AUTHORITY[PS.SHORTLISTED.value]


def test_approval_leads_to_preboarding():
    assert PS.PREBOARDING.value in allowed_next_statuses(PS.CUSTOMER_APPROVAL.value)


def test_hr_takes_over_at_preboarding():
    assert "HR" in STAGE_AUTHORITY[PS.PREBOARDING.value]
    assert PS.JOINED.value in allowed_next_statuses(PS.PREBOARDING.value)


def test_each_stage_arrival_has_its_own_routable_event():
    """HR never heard about a Pre Onboarding hand-off (2 Sep 2026): every
    stage shared ONE route key, so a route saved for Sales redirected HR's
    too. Each stage now has its own key with its owner as the default."""
    from routers.crm.email_flows import EVENTS
    from services.candidate_profiles import stage_arrival_event

    by_key = {e["event"]: e for e in EVENTS}
    assert "candidate.stage_arrival" not in by_key, "the catch-all key must be gone"
    assert stage_arrival_event(PS.PREBOARDING.value) == "candidate.stage_arrival.preboarding"
    assert by_key["candidate.stage_arrival.preboarding"]["default_roles"] == ["HR"]
    assert by_key["candidate.stage_arrival.rmg_review"]["default_roles"] == ["RMG"]
    assert len(by_key) == len(EVENTS), "event keys must be unique"


def test_everyone_who_worked_the_candidate_hears_about_the_join():
    """Joined is broadcast to TA, RMG, Sales, Sales Head and leadership
    (2 Sep 2026, user request) — HR is the actor, not a recipient."""
    from routers.crm.email_flows import EVENTS
    from services.candidate_profiles import JOINED_NOTIFY_ROLES

    assert set(JOINED_NOTIFY_ROLES) == {"TA", "RMG", "Sales", "Sales_Head", "Admin", "CEO"}
    joined = next(e for e in EVENTS if e["event"] == "candidate.joined")
    assert set(joined["default_roles"]) == set(JOINED_NOTIFY_ROLES)


def test_sales_head_is_told_an_offer_is_waiting():
    """The approver has to know there is something to approve."""
    assert _ARRIVAL_NOTIFY_ROLE[PS.CUSTOMER_APPROVAL.value] == "Sales_Head"


def test_hr_is_told_when_preboarding_starts():
    assert _ARRIVAL_NOTIFY_ROLE[PS.PREBOARDING.value] == "HR"


# ------------------------------------------------------ the offer precondition

def test_customer_approval_requires_an_offer():
    assert PS.CUSTOMER_APPROVAL.value in ENTRY_REQUIREMENTS


def test_the_precondition_explains_itself():
    """An error that only says "not allowed" makes the user guess."""
    reason = ENTRY_REQUIREMENTS[PS.CUSTOMER_APPROVAL.value]
    assert "offer" in reason.lower()
    assert "Offers tab" in reason


def test_no_other_stage_has_a_hidden_precondition():
    """Preconditions are invisible until they fire; keep the set deliberate."""
    assert set(ENTRY_REQUIREMENTS) == {PS.CUSTOMER_APPROVAL.value}


# ------------------------------------------------ customer feedback as a round

def test_shortlisting_records_a_hire_verdict():
    assert _CUSTOMER_VERDICT[PS.SHORTLISTED.value] == "Hire"


def test_customer_rejection_records_a_no_hire_verdict():
    assert _CUSTOMER_VERDICT[PS.CUSTOMER_REJECTED.value] == "No Hire"


def test_a_bounce_back_is_not_a_verdict():
    """Returning to Customer Screening is a reschedule, not the client's answer."""
    assert PS.CUSTOMER_SCREENING.value not in _CUSTOMER_VERDICT


def test_every_verdict_is_a_real_result_value():
    from services.interview_rounds import RESULTS
    for verdict in _CUSTOMER_VERDICT.values():
        assert verdict in RESULTS, verdict


# ------------------------------------------------------- Sales visibility holds

def test_sales_and_sales_head_see_everything():
    """Including Customer Approval, the gate Sales Head must action."""
    assert visible_statuses_for(FakeUser(["Sales"])) is None
    assert visible_statuses_for(FakeUser(["Sales_Head"])) is None
