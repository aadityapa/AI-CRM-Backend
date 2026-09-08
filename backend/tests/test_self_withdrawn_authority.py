"""Self Withdrew — who may record it, from where, and what gets stamped.

The Aug 2026 rule: a candidate saying "I'm out" is heard by the TA who sourced
them or by the Sales side — regardless of which stage currently owns the
pipeline. So Self_Withdrawn bypasses STAGE_AUTHORITY and uses its own check:

  * Admin/CEO           — always
  * Sales / Sales_Head  — always
  * TA                  — only their OWN profiles (ta_owner_id), except that
                          unowned profiles (pre-attribution rows) are open to
                          any TA
  * RMG / HR / Finance  — never

And the stage withdrawn FROM is stamped into withdrawn_from_status so the UI
can render "Self Withdrew (RMG Review)".
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from crm_deps import CurrentUser
from models.profiles import PipelineStatus as PS
from services.candidate_profiles import (
    TRANSITION_MAP,
    allowed_next_statuses_for_user,
    may_mark_self_withdrawn,
)


def _user(*roles: str, id: int = 1) -> CurrentUser:
    return CurrentUser(id=id, username=f"u{id}", roles=set(roles))


def _profile(owner: int | None):
    return SimpleNamespace(ta_owner_id=owner)


# ------------------------------------------------------------------ the rule

def test_sales_and_sales_head_may_always_mark_withdrawn():
    for role in ("Sales", "Sales_Head"):
        assert may_mark_self_withdrawn(_profile(owner=99), _user(role, id=1))


def test_admin_and_ceo_pass():
    for role in ("Admin", "CEO"):
        assert may_mark_self_withdrawn(_profile(owner=99), _user(role, id=1))


def test_owning_ta_may_mark_their_own_profile():
    assert may_mark_self_withdrawn(_profile(owner=7), _user("TA", id=7))


def test_other_ta_may_not_touch_an_owned_profile():
    assert not may_mark_self_withdrawn(_profile(owner=7), _user("TA", id=8))


def test_any_ta_may_mark_an_unowned_profile():
    """Pre-attribution rows have no ta_owner_id — any TA may record the withdrawal."""
    assert may_mark_self_withdrawn(_profile(owner=None), _user("TA", id=8))


def test_rmg_hr_finance_may_not():
    for role in ("RMG", "HR", "Finance"):
        assert not may_mark_self_withdrawn(_profile(owner=None), _user(role, id=1))


# ------------------------------------------- the dropdown reflects the rule

def test_dropdown_adds_withdrawn_for_owning_ta_even_off_stage():
    """A TA does not own Customer_Interview, but their candidate withdrawing
    during it must still be recordable — the option appears for them alone."""
    ta = _user("TA", id=7)
    allowed = allowed_next_statuses_for_user(
        PS.CUSTOMER_INTERVIEW.value, ta, _profile(owner=7))
    assert allowed == [PS.SELF_WITHDRAWN.value]


def test_dropdown_hides_withdrawn_from_the_wrong_ta():
    ta = _user("TA", id=8)
    allowed = allowed_next_statuses_for_user(
        PS.CUSTOMER_INTERVIEW.value, ta, _profile(owner=7))
    assert PS.SELF_WITHDRAWN.value not in allowed


def test_dropdown_hides_withdrawn_from_rmg_even_on_their_own_stage():
    rmg = _user("RMG", id=3)
    allowed = allowed_next_statuses_for_user(
        PS.RMG_REVIEW.value, rmg, _profile(owner=7))
    assert PS.SELF_WITHDRAWN.value not in allowed
    # ...but their normal stage moves stay intact.
    assert PS.SALES_SCREENING.value in allowed
    assert PS.RMG_REJECTED.value in allowed


def test_customer_screening_still_suppresses_generic_exits():
    """_NO_GENERIC keeps Self_Withdrawn out of Customer_Screening's dropdown
    for everyone — the rule adds the option only where the map allows it."""
    sales = _user("Sales", id=2)
    allowed = allowed_next_statuses_for_user(
        PS.CUSTOMER_SCREENING.value, sales, _profile(owner=None))
    assert PS.SELF_WITHDRAWN.value not in allowed


# ------------------------------------------------------- round-specific rejects

def test_round_specific_rejects_are_terminal_and_mapped():
    from services.candidate_profiles import TERMINAL_STATUSES
    for v in (PS.CUSTOMER_SCREEN_REJECTED.value, PS.CUSTOMER_L1_REJECTED.value,
              PS.CUSTOMER_L2_REJECTED.value):
        assert v in TERMINAL_STATUSES
        assert v not in TRANSITION_MAP  # terminal: no outgoing moves
