"""Boundary question save + scoring exclusions (Jun 2026)."""

from __future__ import annotations

from ai import (
    answer_turn_is_valid_for_scoring,
    answer_turn_was_attempted,
    is_time_limit_system_message,
)
from main import (
    _append_pending_answer_on_submit,
    _attach_boundary_question_to_report,
    _record_boundary_question_meta,
)


def test_time_limit_placeholder_not_scored():
    msg = "[Interview time limit reached — no answer was submitted for the current question.]"
    assert is_time_limit_system_message(msg) is True
    assert answer_turn_was_attempted(msg) is False
    assert answer_turn_is_valid_for_scoring(msg) is False


def test_append_pending_rejects_placeholder():
    session = {
        "completed": False,
        "current": 0,
        "questions": ["Q1?"],
        "answers": [],
        "meta": {"asked_questions": []},
    }
    ok = _append_pending_answer_on_submit(
        session,
        "[Interview time limit reached — no answer was submitted for the current question.]",
    )
    assert ok is False
    assert session["answers"] == []


def test_boundary_metadata_on_timer_auto_save():
    session = {
        "completed": False,
        "current": 1,
        "questions": ["Q1?", "Q2?"],
        "answers": ["first answer"],
        "meta": {"asked_questions": []},
    }
    appended = _append_pending_answer_on_submit(session, "boundary partial answer text here")
    assert appended is True
    _record_boundary_question_meta(
        session,
        time_expired=True,
        finalize_via="timer",
        auto_saved=True,
        pending_appended=appended,
    )
    meta = session["meta"]
    assert meta["boundary_question_index"] == 1
    # The label NAMES the reason (main.py:3989). A timer expiry is not the same
    # event as a manual finalize, and the report shows this string to a human —
    # "Boundary Question Evaluated" (the old assertion) said neither.
    assert meta["boundary_label"] == "Auto-submitted on timeout"
    assert meta["auto_submitted_on_timeout"] is True
    assert meta["boundary_auto_saved"] is True

    result = _attach_boundary_question_to_report(session, {"per_question": [{}, {}]})
    bq = result.get("boundary_question") or {}
    assert bq.get("label") == "Auto-submitted on timeout"
    assert bq.get("report_turn") == 2
    # The same label is stamped onto the matching per-question row.
    assert result["per_question"][1].get("boundary_label") == "Auto-submitted on timeout"


def test_boundary_label_falls_back_when_meta_has_none():
    """`_attach_boundary_question_to_report` still has the generic default —
    a session recorded before the labels existed must not render an empty tag."""
    session = {"meta": {"boundary_question_index": 0}, "questions": ["Q1?"], "answers": ["a"]}
    result = _attach_boundary_question_to_report(session, {"per_question": [{}]})
    assert (result.get("boundary_question") or {}).get("label") == "Boundary Question Evaluated"
