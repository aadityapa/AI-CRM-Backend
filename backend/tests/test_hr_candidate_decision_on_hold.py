"""
Tests for the HR candidate-decision pipeline (May 2026).

Covers the new third state ``on_hold`` while making sure the legacy
``shortlist`` and ``reject`` decisions still round-trip the SQLite store
untouched (backward compatibility).

Each test runs against a fresh SQLite file inside ``tmp_path`` so the global
auth database is never mutated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auth_db import (
    HR_CANDIDATE_DECISION_VALUES,
    get_hr_candidate_decision,
    init_auth_db,
    list_hr_candidate_decisions,
    set_hr_candidate_decision,
    update_interview_hr_status,
    upsert_interview_record_snapshot,
)


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """A bootstrapped SQLite auth DB scoped to this test."""
    db_file = tmp_path / "auth_on_hold.db"
    init_auth_db(db_file)
    return db_file


def test_hr_decision_values_include_on_hold():
    """The exported tuple drives both runtime validation and admin payload mapping."""
    assert "on_hold" in HR_CANDIDATE_DECISION_VALUES
    # Legacy values must still be valid so existing dashboards/payloads work.
    assert "shortlist" in HR_CANDIDATE_DECISION_VALUES
    assert "reject" in HR_CANDIDATE_DECISION_VALUES


@pytest.mark.parametrize(
    "decision",
    ["shortlist", "reject", "on_hold"],
)
def test_set_get_round_trip(db_path: Path, decision: str):
    """Every supported decision must persist and read back identically."""
    set_hr_candidate_decision(db_path, "alice@example.com", decision)
    assert get_hr_candidate_decision(db_path, "alice@example.com") == decision

    listed = list_hr_candidate_decisions(db_path)
    assert listed["alice@example.com"] == decision


def test_on_hold_accepts_friendly_aliases(db_path: Path):
    """The PUT endpoint already lower-cases, but the model accepts UI/legacy spellings."""
    set_hr_candidate_decision(db_path, "bob@example.com", "On Hold")
    assert get_hr_candidate_decision(db_path, "bob@example.com") == "on_hold"

    set_hr_candidate_decision(db_path, "carol@example.com", "ON-HOLD")
    assert get_hr_candidate_decision(db_path, "carol@example.com") == "on_hold"

    set_hr_candidate_decision(db_path, "dave@example.com", "hold")
    assert get_hr_candidate_decision(db_path, "dave@example.com") == "on_hold"


def test_clear_removes_decision(db_path: Path):
    """Passing None / empty / clear must delete the row so the candidate is un-marked."""
    set_hr_candidate_decision(db_path, "eve@example.com", "on_hold")
    assert get_hr_candidate_decision(db_path, "eve@example.com") == "on_hold"

    set_hr_candidate_decision(db_path, "eve@example.com", None)
    assert get_hr_candidate_decision(db_path, "eve@example.com") is None

    # And a re-clear of an already cleared row must stay idempotent.
    set_hr_candidate_decision(db_path, "eve@example.com", "clear")
    assert get_hr_candidate_decision(db_path, "eve@example.com") is None


def test_invalid_decision_rejected(db_path: Path):
    """Any non-supported decision label must raise so bad payloads fail loudly."""
    with pytest.raises(ValueError):
        set_hr_candidate_decision(db_path, "fred@example.com", "maybe")


def test_legacy_decisions_unchanged(db_path: Path):
    """The May 2026 addition must not regress shortlist/reject round-trips."""
    set_hr_candidate_decision(db_path, "old1@example.com", "shortlist")
    set_hr_candidate_decision(db_path, "old2@example.com", "reject")

    listed = list_hr_candidate_decisions(db_path)
    assert listed == {
        "old1@example.com": "shortlist",
        "old2@example.com": "reject",
    }


def _seed_interview(db_path: Path, rid: str = "iv-on-hold-1") -> str:
    """Seed a minimal interview record so update_interview_hr_status has a row to touch."""
    upsert_interview_record_snapshot(
        db_path,
        {
            "id": rid,
            "candidate_name": "Test Candidate",
            "candidate_email": "test@example.com",
            "created_at_ist": "2026-05-19T10:00:00+05:30",
            "updated_at_ist": "2026-05-19T10:00:00+05:30",
            "submitted": True,
            "report": {"overall_score": 80},
        },
    )
    return rid


def test_update_interview_hr_status_accepts_on_hold(db_path: Path):
    """The status PATCH endpoint must normalize ``on_hold`` to the canonical ``On Hold``."""
    rid = _seed_interview(db_path)

    payload = update_interview_hr_status(db_path, rid, "on_hold")
    assert payload is not None
    assert payload["hr_interview_status"] == "On Hold"

    # Friendly aliases — all collapse to the canonical "On Hold" label.
    for alias in ("On Hold", "onhold", "Hold"):
        payload = update_interview_hr_status(db_path, rid, alias)
        assert payload["hr_interview_status"] == "On Hold", f"alias {alias!r} did not normalize"

    # Legacy labels are still accepted unchanged.
    payload = update_interview_hr_status(db_path, rid, "selected")
    assert payload["hr_interview_status"] == "Selected"

    payload = update_interview_hr_status(db_path, rid, "rejected")
    assert payload["hr_interview_status"] == "Rejected"

    # Clearing the status removes the field from the payload entirely.
    payload = update_interview_hr_status(db_path, rid, "clear")
    assert "hr_interview_status" not in payload


def test_update_interview_hr_status_rejects_garbage(db_path: Path):
    """A nonsense status must raise so the HTTP layer can return 400."""
    rid = _seed_interview(db_path)
    with pytest.raises(ValueError):
        update_interview_hr_status(db_path, rid, "totally-not-a-status")
