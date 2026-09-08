"""The complete candidate journey, stage by stage, with the right owner at each.

    AI L1 (TA)
      → RMG L2 review (RMG)
      → Sales Screening (Sales)
      → Customer Screening (Sales)
      → Customer Interviewing (Sales)
      → L1 Feedback (Sales)        the customer's first round
      → L2 Feedback (Sales)        the customer's second round
      → Customer Shortlisted (Sales)
      → Customer Approved (Sales Head approves)
      → Pre Onboarding (HR takes over)
      → Joined

Two ladders use the names L1 and L2 and they are NOT the same thing: RMG's
technical rounds happen before we submit the candidate, the customer's happen
after. The tests below pin that distinction because conflating them would let
an engineering verdict be read as the client's.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    _ARRIVAL_NOTIFY_ROLE, _CUSTOMER_LADDER, _FEEDBACK_STAGE_ROUND,
    STAGE_AUTHORITY, allowed_next_statuses, visible_statuses_for,
)


class FakeUser:
    def __init__(self, roles, is_admin=False):
        self.id = 1
        self.roles = list(roles)
        self.is_admin = is_admin


#: The intended happy path, in order.
HAPPY_PATH = [
    PS.TECHNICAL_SCREENING, PS.RMG_REVIEW, PS.SALES_SCREENING, PS.CUSTOMER_SCREENING,
    PS.CUSTOMER_INTERVIEW, PS.L1_FEEDBACK, PS.L2_FEEDBACK, PS.SHORTLISTED,
    PS.CUSTOMER_APPROVAL, PS.PREBOARDING, PS.JOINED,
]


# ------------------------------------------------------------ the chain walks

@pytest.mark.parametrize("frm,to", list(zip(HAPPY_PATH, HAPPY_PATH[1:])))
def test_each_step_of_the_happy_path_is_allowed(frm, to):
    assert to.value in allowed_next_statuses(frm.value)


def test_the_new_feedback_stages_exist():
    assert PS.L1_FEEDBACK.value == "L1_Feedback"
    assert PS.L2_FEEDBACK.value == "L2_Feedback"


# ------------------------------------------------------------- who owns what

@pytest.mark.parametrize("stage,owners", [
    (PS.TECHNICAL_SCREENING, {"TA"}),
    (PS.RMG_REVIEW, {"RMG"}),
    (PS.SALES_SCREENING, {"Sales"}),
    (PS.CUSTOMER_SCREENING, {"Sales"}),
    (PS.L1_FEEDBACK, {"Sales", "Sales_Head"}),
    (PS.L2_FEEDBACK, {"Sales", "Sales_Head"}),
    (PS.CUSTOMER_APPROVAL, {"Sales_Head"}),
])
def test_stage_ownership(stage, owners):
    assert STAGE_AUTHORITY[stage.value] == owners


def test_sales_cannot_sign_off_its_own_offer():
    """Sales proposes the rate + onboarding date; Sales Head approves them."""
    assert "Sales" not in STAGE_AUTHORITY[PS.CUSTOMER_APPROVAL.value]


def test_hr_takes_over_at_pre_onboarding():
    assert "HR" in STAGE_AUTHORITY[PS.PREBOARDING.value]


# --------------------------------------------- the customer's rounds are theirs

def test_both_feedback_stages_map_to_a_customer_round():
    assert _FEEDBACK_STAGE_ROUND[PS.L1_FEEDBACK.value] == "L1"
    assert _FEEDBACK_STAGE_ROUND[PS.L2_FEEDBACK.value] == "L2"


def test_the_customer_ladder_is_exactly_the_customer_stages():
    """RMG's technical rounds must not be inside the customer's ladder."""
    assert _CUSTOMER_LADDER == {
        PS.CUSTOMER_INTERVIEW.value, PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value,
    }
    assert PS.RMG_REVIEW.value not in _CUSTOMER_LADDER
    assert PS.TECHNICAL_SCREENING.value not in _CUSTOMER_LADDER


def test_a_customer_running_only_one_round_can_still_shortlist():
    """Not every customer interviews twice; forcing L2 would make the pipeline lie."""
    assert PS.SHORTLISTED.value in allowed_next_statuses(PS.L1_FEEDBACK.value)


def test_a_feedback_round_can_be_re_run():
    assert PS.CUSTOMER_INTERVIEW.value in allowed_next_statuses(PS.L1_FEEDBACK.value)
    assert PS.L1_FEEDBACK.value in allowed_next_statuses(PS.L2_FEEDBACK.value)


@pytest.mark.parametrize("stage,reject", [
    # Round-specific rejections (Aug 2026): the customer's "no" carries WHICH
    # round said it. The generic Customer_Rejected stays for Shortlisted /
    # Customer_Approval drops and for legacy rows.
    (PS.CUSTOMER_SCREENING, PS.CUSTOMER_SCREEN_REJECTED),
    (PS.CUSTOMER_INTERVIEW, PS.CUSTOMER_L1_REJECTED),
    (PS.L1_FEEDBACK, PS.CUSTOMER_L1_REJECTED),
    (PS.L2_FEEDBACK, PS.CUSTOMER_L2_REJECTED),
    (PS.SHORTLISTED, PS.CUSTOMER_REJECTED),
    (PS.CUSTOMER_APPROVAL, PS.CUSTOMER_REJECTED),
])
def test_the_customer_can_reject_at_each_round_with_its_own_status(stage, reject):
    assert reject.value in allowed_next_statuses(stage.value)


# --------------------------------------------------------------- handoffs land

@pytest.mark.parametrize("stage,role", [
    (PS.RMG_REVIEW, "RMG"),
    (PS.SALES_SCREENING, "Sales"),
    (PS.L1_FEEDBACK, "Sales"),
    (PS.L2_FEEDBACK, "Sales"),
    (PS.CUSTOMER_APPROVAL, "Sales_Head"),
    (PS.PREBOARDING, "HR"),
])
def test_the_next_owner_is_notified(stage, role):
    assert _ARRIVAL_NOTIFY_ROLE[stage.value] == role


def test_every_notified_role_can_act_on_the_stage():
    """Telling someone about a stage they cannot move is noise."""
    for stage, role in _ARRIVAL_NOTIFY_ROLE.items():
        owners = STAGE_AUTHORITY.get(stage)
        if owners:
            assert role in owners, f"{role} notified about {stage} but cannot act"


# ------------------------------------------------------------ Sales visibility

def test_sales_visibility_is_unrestricted():
    """No stage scoping since 18 Aug 2026 — see test_pipeline_handoff.py."""
    assert visible_statuses_for(FakeUser(["Sales"])) is None


# ------------------------------------------------------------------ AI L1 gate

def test_ai_l1_can_only_be_triggered_by_ta():
    """AI L1 is TA's step. The resume path was already TA-only; the profile
    page let RMG and Sales fire one, so the same action had two answers."""
    from routers.crm.ai_interviews import TRIGGER_ROLES
    assert TRIGGER_ROLES == ("TA",)
