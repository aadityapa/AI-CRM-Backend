"""The TA -> RMG -> Sales -> Customer handoff.

Checks the guards that make the flow a flow rather than a set of free-form
status edits, plus the two things that were missing: Sales never being told a
candidate had arrived, and Sales seeing every profile at every stage.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    PROFILE_VISIBILITY, STAGE_AUTHORITY, TERMINAL_STATUSES, _ARRIVAL_NOTIFY_ROLE,
    allowed_next_statuses, visible_statuses_for,
)


class FakeUser:
    """Stand-in for CurrentUser — only roles/is_admin matter to these helpers."""

    def __init__(self, roles, is_admin=False):
        self.id = 1
        self.roles = list(roles)
        self.is_admin = is_admin


# ------------------------------------------------------- the happy path exists

def test_the_intended_route_is_walkable():
    """AI L1 done -> RMG -> Sales -> customer, each step allowed by the map."""
    assert PS.RMG_REVIEW.value in allowed_next_statuses(PS.TECHNICAL_SCREENING.value)
    assert PS.SALES_SCREENING.value in allowed_next_statuses(PS.RMG_REVIEW.value)
    assert PS.CUSTOMER_SCREENING.value in allowed_next_statuses(PS.SALES_SCREENING.value)


def test_stages_cannot_be_skipped():
    """RMG cannot push a candidate straight to the customer, bypassing Sales."""
    assert PS.CUSTOMER_SCREENING.value not in allowed_next_statuses(PS.RMG_REVIEW.value)
    assert PS.JOINED.value not in allowed_next_statuses(PS.RMG_REVIEW.value)


def test_each_stage_is_owned_by_exactly_the_right_role():
    assert STAGE_AUTHORITY[PS.TECHNICAL_SCREENING.value] == {"TA"}
    assert STAGE_AUTHORITY[PS.RMG_REVIEW.value] == {"RMG"}
    assert STAGE_AUTHORITY[PS.SALES_SCREENING.value] == {"Sales"}


def test_rejection_is_available_at_each_stage():
    assert PS.RMG_REJECTED.value in allowed_next_statuses(PS.RMG_REVIEW.value)
    assert PS.SALES_REJECTED.value in allowed_next_statuses(PS.SALES_SCREENING.value)


def test_terminal_statuses_go_nowhere():
    for status in TERMINAL_STATUSES:
        assert allowed_next_statuses(status) == [], status


# --------------------------------------------------- the next role gets told

def test_every_handoff_stage_notifies_someone():
    """The gap that broke the flow: nothing told Sales a candidate had arrived."""
    assert _ARRIVAL_NOTIFY_ROLE[PS.RMG_REVIEW.value] == "RMG"
    assert _ARRIVAL_NOTIFY_ROLE[PS.SALES_SCREENING.value] == "Sales"


def test_the_notified_role_owns_the_stage_it_is_notified_about():
    """A notification to a role that cannot act on the stage is noise."""
    for status, role in _ARRIVAL_NOTIFY_ROLE.items():
        owners = STAGE_AUTHORITY.get(status)
        if owners:
            assert role in owners, f"{role} notified about {status} but cannot act on it"


# ------------------------------------------------------------ Sales visibility

def test_every_role_sees_the_whole_pipeline():
    """18 Aug 2026 (user decision): the profile list is no longer scoped.

    Sales used to see only Sales_Screening onward, so a candidate TA had just
    applied (Sourcing) was invisible to them — it read as the record being
    lost. _SALES_VISIBLE stays as the documented definition of the stages
    Sales OWNS (the filter chips use it); it just no longer hides rows.
    """
    for role in ("Sales", "Sales_Head", "TA", "RMG", "HR", "Finance"):
        assert visible_statuses_for(FakeUser([role])) is None, role


def test_early_stages_are_visible_to_sales():
    """The regression that prompted the change: a TA-applied candidate."""
    assert visible_statuses_for(FakeUser(["Sales"])) is None
    for stage in (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value, PS.RMG_REVIEW.value):
        assert stage not in (visible_statuses_for(FakeUser(["Sales"])) or set())


def test_sales_stage_set_still_documents_what_sales_owns():
    """Kept for the filter chips — and so restoring the scope stays one line."""
    from services.candidate_profiles import _SALES_VISIBLE

    for shown in (PS.SALES_SCREENING.value, PS.CUSTOMER_SCREENING.value,
                  PS.CUSTOMER_INTERVIEW.value, PS.SHORTLISTED.value,
                  PS.CUSTOMER_APPROVAL.value, PS.JOINED.value,
                  PS.SALES_REJECTED.value, PS.CUSTOMER_REJECTED.value):
        assert shown in _SALES_VISIBLE, shown


def test_ta_and_rmg_are_unrestricted():
    """They work across the early stages and need the whole board."""
    assert visible_statuses_for(FakeUser(["TA"])) is None
    assert visible_statuses_for(FakeUser(["RMG"])) is None


def test_admin_is_unrestricted():
    assert visible_statuses_for(FakeUser([], is_admin=True)) is None


def test_a_second_unscoped_role_lifts_the_restriction():
    """Someone who is both Sales and RMG must not lose their RMG view."""
    assert visible_statuses_for(FakeUser(["Sales", "RMG"])) is None


def test_no_role_is_scoped_any_more():
    assert PROFILE_VISIBILITY == {}


def test_no_roles_at_all_is_not_treated_as_a_scope():
    """Absence of roles must not silently become an empty allow-list."""
    assert visible_statuses_for(FakeUser([])) is None
