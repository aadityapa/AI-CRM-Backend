"""HR's edit window on a candidate profile (2 Sep 2026, user request).

HR confirms the onboarding paperwork with the candidate at Pre Onboarding —
the CTCs, the references, the two onboarding dates, the verified experience,
the relocation answer. So:

  * an HR-ONLY user may edit those fields while the profile is at Preboarding;
  * at any other stage the profile is another team's and HR is read-only;
  * the approval amount is Sales Head's at every stage — HR never touches it;
  * a user who is ALSO TA/Sales/RMG keeps that role's full rights (the window
    constrains HR-only users, it does not shrink anyone else).

Run:  cd backend && python -m pytest tests/test_hr_edit_window.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import PipelineStatus as PS  # noqa: E402
from routers.crm.candidate_profiles import (  # noqa: E402
    HR_EDITABLE_FIELDS, enforce_hr_edit_window, enforce_onboarding_record_owner,
)


def _user(*roles, admin=False):
    return SimpleNamespace(id=1, roles=set(roles), is_admin=admin)


def _profile(status):
    return SimpleNamespace(id=42, pipeline_status=status)


HR_FIELDS = {"current_ctc": 820000, "expected_ctc": 1500000,
             "karnex_onboarding_date": "2026-09-07", "relocation_applicable": True}


def test_hr_may_edit_their_fields_at_preboarding():
    enforce_hr_edit_window(_profile(PS.PREBOARDING), _user("HR"), dict(HR_FIELDS))


@pytest.mark.parametrize("stage", [PS.SOURCING, PS.RMG_REVIEW, PS.CUSTOMER_APPROVAL, PS.JOINED])
def test_hr_is_read_only_at_every_other_stage(stage):
    with pytest.raises(HTTPException) as err:
        enforce_hr_edit_window(_profile(stage), _user("HR"), {"current_ctc": 1})
    assert err.value.status_code == 403
    assert "Pre-Onboarding" in err.value.detail


def test_hr_never_touches_the_approval_amount():
    with pytest.raises(HTTPException) as err:
        enforce_hr_edit_window(_profile(PS.PREBOARDING), _user("HR"),
                               {"ctc_approval_amount": 1560000})
    assert err.value.status_code == 403
    assert "ctc_approval_amount" in err.value.detail


def test_the_window_only_constrains_hr_only_users():
    """TA at Sourcing, or HR+Sales anywhere, are untouched by the rule."""
    enforce_hr_edit_window(_profile(PS.SOURCING), _user("TA"), {"current_ctc": 1})
    enforce_hr_edit_window(_profile(PS.SOURCING), _user("HR", "Sales"),
                           {"ctc_approval_amount": 1})
    enforce_hr_edit_window(_profile(PS.SOURCING), _user("HR", admin=True),
                           {"ctc_approval_amount": 1})


def _stored(status, **values):
    base = dict(employee_ref=None, offer_letter_reference=None, official_email=None,
                total_experience_years=None, department_id=None, designation_id=None,
                karnex_onboarding_date=None, customer_onboarding_date=None)
    base.update(values)
    return SimpleNamespace(id=42, pipeline_status=status, **base)


def test_onboarding_record_is_hr_or_admin_at_preboarding_only():
    """6 Oct 2026: filled after the Sales Head approval, at Pre-Onboarding, by HR / Admin / CEO."""
    enforce_onboarding_record_owner(_stored(PS.PREBOARDING), _user("HR"), {"employee_ref": "310"})
    enforce_onboarding_record_owner(_stored(PS.PREBOARDING), _user("Sales", admin=True), {"official_email": "a@karnex.in"})
    for user in (_user("Sales"), _user("TA"), _user("RMG"), _user("GM")):
        with pytest.raises(HTTPException) as err:
            enforce_onboarding_record_owner(_stored(PS.PREBOARDING), user, {"employee_ref": "310"})
        assert err.value.status_code == 403
    for stage in (PS.SHORTLISTED, PS.HR_SCREENING, PS.JOINED):
        with pytest.raises(HTTPException) as err:
            enforce_onboarding_record_owner(_stored(stage), _user("HR"), {"employee_ref": "310"})
        assert "Pre-Onboarding" in err.value.detail


def test_an_unchanged_onboarding_field_is_not_a_change():
    """An older client re-posting the form with the stored values must not fail."""
    enforce_onboarding_record_owner(_stored(PS.SHORTLISTED, employee_ref="310", total_experience_years=3),
                                    _user("Sales"), {"employee_ref": "310", "total_experience_years": 3.0,
                                                     "current_ctc": 1})


def test_the_field_list_is_the_onboarding_paperwork_and_nothing_else():
    assert "ctc_approval_amount" not in HR_EDITABLE_FIELDS
    assert "commercial_approved" not in HR_EDITABLE_FIELDS
    for f in ("current_ctc", "expected_ctc", "karnex_onboarding_date",
              "customer_onboarding_date", "total_experience_years", "relocation_applicable"):
        assert f in HR_EDITABLE_FIELDS
