"""The Workflow block's dates fill themselves in.

Before this, eight of the eleven fields in that block had no writer anywhere in
the application — they were columns added for a Zoho import that has not been
run, so every profile created in the app showed a permanent row of dashes.
Three of the dates are things the pipeline already knows, so it stamps them.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from services.candidate_profiles import (  # noqa: E402
    _STAGE_DATE_STAMPS, _stamp_workflow_dates,
)


class FakeProfile:
    """Only the columns the stamper touches."""

    def __init__(self, **overrides):
        self.id = 1
        self.technical_submission_date = None
        self.sales_submission_date = None
        self.customer_submission_date = None
        self.customer_onboarding_date = None
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeDB:
    """Stands in for the offer lookup; returns whatever joining date is set."""

    def __init__(self, joining=None):
        self._joining = joining

    def execute(self, _stmt):
        joining = self._joining

        class R:
            @staticmethod
            def scalar_one_or_none():
                return joining

        return R()


# ------------------------------------------- each handover stamps its own date

@pytest.mark.parametrize("status,field", [
    (PS.TECHNICAL_SCREENING.value, "technical_submission_date"),
    (PS.SALES_SCREENING.value, "sales_submission_date"),
    (PS.CUSTOMER_SCREENING.value, "customer_submission_date"),
])
def test_reaching_a_stage_records_the_date(status, field):
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(), profile, status)
    assert getattr(profile, field) == date.today()


def test_only_the_matching_field_is_touched():
    """Reaching Sales Screening must not backfill the technical date."""
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(), profile, PS.SALES_SCREENING.value)
    assert profile.sales_submission_date == date.today()
    assert profile.technical_submission_date is None
    assert profile.customer_submission_date is None


# ----------------------------------------- the first submission date is the one

def test_an_existing_date_is_never_overwritten():
    """A step back and forward again must not reset the clock. That original
    date is what turnaround time is measured from, and quietly moving it
    forward would make every SLA look better than it was."""
    original = date.today() - timedelta(days=12)
    profile = FakeProfile(sales_submission_date=original)
    _stamp_workflow_dates(FakeDB(), profile, PS.SALES_SCREENING.value)
    assert profile.sales_submission_date == original


# ------------------------------------- onboarding is planned, not stamped today

def test_onboarding_date_comes_from_the_offer():
    joining = date.today() + timedelta(days=30)
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(joining=joining), profile, PS.PREBOARDING.value)
    assert profile.customer_onboarding_date == joining


def test_onboarding_date_is_not_todays_date():
    """It is a future planned date. Stamping "today" would be plainly wrong."""
    joining = date.today() + timedelta(days=30)
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(joining=joining), profile, PS.PREBOARDING.value)
    assert profile.customer_onboarding_date != date.today()


def test_no_offer_joining_date_leaves_onboarding_blank():
    """Better empty than invented — HR can type it in."""
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(joining=None), profile, PS.PREBOARDING.value)
    assert profile.customer_onboarding_date is None


def test_a_manually_set_onboarding_date_survives_preboarding():
    chosen = date.today() + timedelta(days=5)
    profile = FakeProfile(customer_onboarding_date=chosen)
    _stamp_workflow_dates(FakeDB(joining=date.today() + timedelta(days=60)),
                          profile, PS.PREBOARDING.value)
    assert profile.customer_onboarding_date == chosen


# ------------------------------------------------------------- unrelated moves

@pytest.mark.parametrize("status", [
    PS.SOURCING.value, PS.RMG_REVIEW.value, PS.CUSTOMER_INTERVIEW.value,
    PS.SHORTLISTED.value, PS.REJECTED.value,
])
def test_other_transitions_stamp_nothing(status):
    profile = FakeProfile()
    _stamp_workflow_dates(FakeDB(), profile, status)
    assert profile.technical_submission_date is None
    assert profile.sales_submission_date is None
    assert profile.customer_submission_date is None
    assert profile.customer_onboarding_date is None


def test_every_stamped_field_exists_on_the_model():
    """Guards against a rename leaving the map pointing at nothing — setattr
    would happily create a stray attribute and the column would stay empty."""
    from models import CandidateProfile
    for field in _STAGE_DATE_STAMPS.values():
        assert hasattr(CandidateProfile, field), field


def test_the_editable_references_are_accepted_by_the_update_schema():
    """These had no writer at all before; the block was display-only."""
    from schemas.candidate_profiles import ProfileUpdate
    payload = ProfileUpdate(offer_letter_reference="KRX/OL/2026/0142",
                            employee_ref="KRX-4821",
                            customer_onboarding_date="2026-09-01")
    assert payload.offer_letter_reference == "KRX/OL/2026/0142"
    assert payload.employee_ref == "KRX-4821"
    assert str(payload.customer_onboarding_date) == "2026-09-01"


def test_a_reference_can_be_cleared():
    """The UI sends null rather than "" so clearing actually clears."""
    from schemas.candidate_profiles import ProfileUpdate
    payload = ProfileUpdate(offer_letter_reference=None)
    assert "offer_letter_reference" in payload.model_dump(exclude_unset=True)
