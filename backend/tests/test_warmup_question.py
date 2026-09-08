"""
Tests for the communication warmup question pipeline (Issue 2, May 2026).

Covers:
  * `inject_warmup` injection logic + env flag respect
  * `is_warmup_index` / `filter_out_warmups` index math
  * `next_question_payload` flags + index/total math hides the warmup
  * Warmup turns do NOT score, do NOT enter the learning ledger, do NOT count
    toward analytics

The tests stay 100% backward-compatible: legacy sessions without a
`warmup_indices` key behave exactly as before.
"""

from __future__ import annotations


from candidate.service import next_question_payload
from utils.warmup import (
    WARMUP_QUESTION_TEXT,
    filter_out_warmups,
    inject_warmup,
    is_warmup_index,
    warmup_enabled,
)


def _restore_env(monkeypatch, key):
    """Pytest helper — does not actually undo, but documents intent."""
    monkeypatch.delenv(key, raising=False)


def test_warmup_enabled_default_is_true(monkeypatch):
    monkeypatch.delenv("INTERVIEW_WARMUP_QUESTION_ENABLED", raising=False)
    assert warmup_enabled() is True


def test_warmup_enabled_respects_off_flag(monkeypatch):
    monkeypatch.setenv("INTERVIEW_WARMUP_QUESTION_ENABLED", "false")
    assert warmup_enabled() is False
    monkeypatch.setenv("INTERVIEW_WARMUP_QUESTION_ENABLED", "0")
    assert warmup_enabled() is False
    monkeypatch.setenv("INTERVIEW_WARMUP_QUESTION_ENABLED", "off")
    assert warmup_enabled() is False


def test_inject_warmup_prepends_question(monkeypatch):
    monkeypatch.delenv("INTERVIEW_WARMUP_QUESTION_ENABLED", raising=False)
    qs = ["What is REST?", "Explain CAP theorem."]
    new_qs, idx = inject_warmup(qs)
    assert new_qs[0] == WARMUP_QUESTION_TEXT
    assert new_qs[1:] == qs
    assert idx == [0]


def test_inject_warmup_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("INTERVIEW_WARMUP_QUESTION_ENABLED", "false")
    qs = ["What is REST?"]
    new_qs, idx = inject_warmup(qs)
    assert new_qs == qs
    assert idx == []


def test_is_warmup_index_handles_missing_meta():
    assert is_warmup_index({}, 0) is False
    assert is_warmup_index({"warmup_indices": [0]}, 0) is True
    assert is_warmup_index({"warmup_indices": [0]}, 1) is False
    # Bad values must not crash.
    assert is_warmup_index({"warmup_indices": ["x", None, 0]}, 0) is True


def test_filter_out_warmups_strips_aligned_qa():
    meta = {"warmup_indices": [0]}
    qs = ["intro?", "Q1", "Q2"]
    ans = ["hi I'm A", "A1", "A2"]
    q_out, a_out = filter_out_warmups(qs, ans, meta)
    assert q_out == ["Q1", "Q2"]
    assert a_out == ["A1", "A2"]


def test_filter_out_warmups_noop_for_legacy_session():
    """Sessions saved BEFORE the warmup feature have no warmup_indices key."""
    qs = ["Q1", "Q2"]
    ans = ["A1", "A2"]
    assert filter_out_warmups(qs, ans, {}) == (qs, ans)
    assert filter_out_warmups(qs, ans, None) == (qs, ans)


def test_next_question_payload_flags_warmup():
    session = {
        "current": 0,
        "questions": [WARMUP_QUESTION_TEXT, "Q1", "Q2"],
        "answers": [],
        "completed": False,
        "meta": {
            "jd_skills": ["python"],
            "show_spoken_text": True,
            "warmup_indices": [0],
        },
    }
    out = next_question_payload(session)
    assert out["is_warmup"] is True
    assert out.get("question_type") == "INTRODUCTION"
    assert out["question"] == WARMUP_QUESTION_TEXT
    # Warmup index displayed as 0 so the frontend can hide the progress pill.
    assert out["index"] == 0
    # Total excludes the warmup turn (2 real questions in this fixture).
    assert out["total"] == 2
    assert out.get("warmup_label")
    assert out.get("warmup_note")


def test_next_question_payload_first_real_question_excludes_warmup_from_count():
    session = {
        "current": 1,  # past the warmup
        "questions": [WARMUP_QUESTION_TEXT, "Q1", "Q2"],
        "answers": ["small talk"],
        "completed": False,
        "meta": {
            "jd_skills": ["python"],
            "show_spoken_text": True,
            "warmup_indices": [0],
        },
    }
    out = next_question_payload(session)
    assert out["is_warmup"] is False
    assert out["question"] == "Q1"
    # 1-based and warmup-excluded → first scored question reads "1/2".
    assert out["index"] == 1
    assert out["total"] == 2


def test_next_question_payload_completion_uses_evaluated_total():
    session = {
        "current": 3,
        "questions": [WARMUP_QUESTION_TEXT, "Q1", "Q2"],
        "answers": ["intro", "A1", "A2"],
        "completed": False,
        "meta": {
            "jd_skills": ["python"],
            "show_spoken_text": True,
            "warmup_indices": [0],
        },
    }
    out = next_question_payload(session)
    assert out["message"] == "Interview completed"
    assert out["index"] == 2
    assert out["total"] == 2


def test_legacy_session_without_warmup_still_paints_progress():
    """Regression: legacy session shape (no warmup_indices) still works."""
    session = {
        "current": 0,
        "questions": ["Q1", "Q2"],
        "answers": [],
        "completed": False,
        "meta": {"jd_skills": ["python"], "show_spoken_text": True},
    }
    out = next_question_payload(session)
    assert out.get("is_warmup") is False
    assert out["index"] == 1
    assert out["total"] == 2
