"""The RMG screening gate (25 Aug 2026): TA applies → RMG clears → AI L1 unlocks.

Pins the pure gate logic. The endpoints are exercised indirectly through the
existing profile suites; what must never regress silently is the gate's
answer for each status — a wrong None here quietly reopens the AI L1 for
unscreened candidates.

Run:  cd backend && python -m pytest tests/test_rmg_screening_gate.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

from services.candidate_profiles import (
    RMG_SCREENING_PENDING,
    RMG_SCREENING_REJECTED,
    RMG_SCREENING_SHORTLISTED,
    RMG_SCREENING_VALUES,
    rmg_screening_blocks_l1,
)


def _profile(status, note=None):
    return SimpleNamespace(rmg_screening_status=status, rmg_screening_note=note)


def test_pending_blocks_the_ai_l1():
    reason = rmg_screening_blocks_l1(_profile(RMG_SCREENING_PENDING))
    assert reason is not None and "RMG" in reason


def test_rejected_blocks_and_carries_the_note():
    reason = rmg_screening_blocks_l1(_profile(RMG_SCREENING_REJECTED, "wrong tech stack"))
    assert reason is not None
    assert "wrong tech stack" in reason


def test_shortlisted_unlocks():
    assert rmg_screening_blocks_l1(_profile(RMG_SCREENING_SHORTLISTED)) is None


def test_legacy_null_is_not_gated():
    """Profiles created before the gate existed must keep working — the gate
    cannot retroactively freeze candidates already mid-pipeline."""
    assert rmg_screening_blocks_l1(_profile(None)) is None


def test_the_three_values_are_stable():
    """The frontend badge and the API pattern both match on these literals."""
    assert RMG_SCREENING_VALUES == ("Pending", "Shortlisted", "Rejected")


def test_an_unknown_status_fails_open_not_500():
    """Garbage in the column (bad import) must not lock the candidate forever
    with an unexplained error — unknown reads as ungated."""
    assert rmg_screening_blocks_l1(_profile("Weird_Value")) is None


def test_rmg_notifications_deep_link_to_applied_candidates_row():
    """8 Sep 2026 (user report): the 'RMG screening needed' mail opened the
    profile page; RMG works from the requirement's Applied Candidates tab, so
    the link now lands there with the candidate's email prefilled in search.
    No requirement yet → the profile page stays the fallback."""
    from types import SimpleNamespace
    from services.candidate_profiles import applied_candidates_link

    class _DB:
        def __init__(self, req_id, cand):
            self._req, self._cand = req_id, cand
        def execute(self, stmt):
            return SimpleNamespace(scalar=lambda: self._req)
        def get(self, model, pk):
            return self._cand

    prof = SimpleNamespace(id=19212, opportunity_id=5, candidate_id=7)
    cand = SimpleNamespace(first_name="Dipesh", last_name="D", email="dipeshad007@gmail.com")
    assert applied_candidates_link(_DB(41, cand), prof) == \
        "/admin?view=crm&p=requirements/41&tab=resumes&q=dipeshad007%40gmail.com"
    # import placeholder → search by name instead
    ph = SimpleNamespace(first_name="Dipesh", last_name="D", email="dipesh.d.abc123@import.karnex.in")
    assert applied_candidates_link(_DB(41, ph), prof) == \
        "/admin?view=crm&p=requirements/41&tab=resumes&q=Dipesh%20D"
    assert applied_candidates_link(_DB(None, cand), prof) == "/admin?view=crm&p=profiles/19212"
