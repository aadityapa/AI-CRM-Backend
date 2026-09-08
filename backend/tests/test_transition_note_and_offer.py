"""When a status change needs a written note, and carrying the offer with it.

Two changes with the same motive: stop making people do bookkeeping that adds
nothing, so the bookkeeping that matters gets taken seriously.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pydantic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from schemas.candidate_profiles import ProfileStatusTransitionIn  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    ENTRY_REQUIREMENTS, MIN_COMMENT_LENGTH, comment_required_for,
)


# ------------------------------------------- routine progress needs no note

@pytest.mark.parametrize("frm,to", [
    (PS.SOURCING, PS.TECHNICAL_SCREENING),
    (PS.TECHNICAL_SCREENING, PS.RMG_REVIEW),
    (PS.RMG_REVIEW, PS.SALES_SCREENING),
    (PS.SALES_SCREENING, PS.CUSTOMER_SCREENING),
    (PS.CUSTOMER_SCREENING, PS.CUSTOMER_INTERVIEW),
    (PS.SHORTLISTED, PS.CUSTOMER_APPROVAL),
    (PS.CUSTOMER_APPROVAL, PS.PREBOARDING),
    (PS.PREBOARDING, PS.JOINED),
])
def test_moving_forward_normally_needs_no_note(frm, to):
    """"Moving to Customer Screening" adds nothing the status does not say.
    Demanding a note here just trains people to type "ok"."""
    assert comment_required_for(frm.value, to.value) is False


# ------------------------------------------------- where a note IS required

@pytest.mark.parametrize("to", [
    PS.RMG_REJECTED, PS.SALES_REJECTED, PS.CUSTOMER_REJECTED,
    PS.SELF_WITHDRAWN, PS.REJECTED,
])
def test_dropping_a_candidate_always_needs_a_reason(to):
    """Why someone was dropped is not recoverable from the status alone."""
    assert comment_required_for(PS.SALES_SCREENING.value, to.value) is True


@pytest.mark.parametrize("to", [PS.L1_FEEDBACK, PS.L2_FEEDBACK])
def test_customer_feedback_stages_need_the_feedback(to):
    """The note IS the feedback — it becomes that round's interview record."""
    assert comment_required_for(PS.CUSTOMER_INTERVIEW.value, to.value) is True


@pytest.mark.parametrize("frm,to", [
    (PS.CUSTOMER_SCREENING, PS.SALES_SCREENING),
    (PS.CUSTOMER_INTERVIEW, PS.CUSTOMER_SCREENING),
    (PS.L1_FEEDBACK, PS.CUSTOMER_INTERVIEW),
    (PS.L2_FEEDBACK, PS.L1_FEEDBACK),
])
def test_going_backwards_needs_explaining(frm, to):
    assert comment_required_for(frm.value, to.value) is True


@pytest.mark.parametrize("frm", [PS.CUSTOMER_INTERVIEW, PS.L1_FEEDBACK, PS.L2_FEEDBACK])
def test_closing_the_customer_ladder_needs_the_verdict(frm):
    assert comment_required_for(frm.value, PS.SHORTLISTED.value) is True


def test_the_minimum_is_short_enough_to_be_reasonable():
    assert MIN_COMMENT_LENGTH == 5


# ------------------------------------------ the offer travels with the move

def test_the_transition_can_carry_an_offer():
    """Customer Approved requires an offer, so it can be supplied with the
    move rather than forcing a detour to the Offers tab and back."""
    payload = ProfileStatusTransitionIn(
        new_status=PS.CUSTOMER_APPROVAL.value,
        offer={"offer_date": "2026-08-10", "ctc": 1250000, "joining_date": "2026-09-01"},
    )
    assert payload.offer is not None
    assert payload.offer.ctc == 1250000
    assert str(payload.offer.joining_date) == "2026-09-01"


def test_the_offer_is_optional():
    payload = ProfileStatusTransitionIn(new_status=PS.PREBOARDING.value, comment="Approved")
    assert payload.offer is None


def test_the_comment_is_optional_on_the_payload():
    """Optionality is decided per transition by comment_required_for, not by
    the schema — so the schema must allow its absence."""
    assert ProfileStatusTransitionIn(new_status=PS.SALES_SCREENING.value).comment is None


def test_an_offer_without_a_date_is_rejected():
    with pytest.raises(pydantic.ValidationError):
        ProfileStatusTransitionIn(new_status=PS.CUSTOMER_APPROVAL.value, offer={"ctc": 100})


def test_customer_approval_still_requires_an_offer_to_exist():
    """The inline form is a convenience, not a way round the precondition."""
    assert PS.CUSTOMER_APPROVAL.value in ENTRY_REQUIREMENTS
