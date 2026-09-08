"""
Evaluation must use answered turns only (May 2026).

Covers the bug where a large prefetched question pool (timed interviews)
was stored alongside fewer answers, skewing scores and HR reports.
"""

from __future__ import annotations

from ai import (
    align_qa_to_answered_turns,
    format_decimal_score,
    format_percent_from_ten_scale,
    merge_per_question_eval_into_report,
    slice_qa_for_final_evaluation,
)
from utils.question_uniqueness import build_question_avoid_history


def test_align_qa_trims_unasked_pool_slots():
    """40 generated questions but only 15 answers → evaluation scope is 15."""
    pool = [f"Generated question {i}?" for i in range(40)]
    answers = [f"Answer {i}" for i in range(15)]
    q, a, meta = align_qa_to_answered_turns(pool, answers)
    assert len(q) == 15
    assert len(a) == 15
    assert meta["pool_generated"] == 40
    assert meta["unused_pool_slots"] == 25
    assert meta["evaluation_scope"] == "answered_turns_only"


def test_slice_qa_after_align_excludes_empty_slots():
    pool = ["Q1?", "Q2?", "Q3?"]
    answers = ["Good answer here with enough substance.", "", "skip"]
    q, a, _ = align_qa_to_answered_turns(pool, answers)
    q_eval, a_eval, meta = slice_qa_for_final_evaluation(q, a)
    assert len(q_eval) == 1
    assert meta["evaluated_questions"] == 1


def test_merge_per_question_mean_uses_answered_only(monkeypatch):
    """Mean score ignores trailing empty pool slots."""

    def _fake_batch(questions, answers, model="gpt-4o-mini", *, meta=None, **_kw):
        rows = []
        for i in range(len(questions)):
            rows.append(
                {
                    "question_index": i + 1,
                    "score": 8.0,
                    "strengths": ["Clear"],
                    "weaknesses": [],
                    "feedback": "Relevant answer.",
                }
            )
        return rows

    monkeypatch.setattr("ai.evaluate_per_question_interview_batch", _fake_batch)

    questions = [f"Q{i}?" for i in range(10)]
    answers = ["Strong detailed answer about system design."] * 3 + [""] * 7
    q, a, _ = align_qa_to_answered_turns(questions, answers)
    base = {"overall_score": 9.0, "skill_scores": []}
    out = merge_per_question_eval_into_report(base, q, a, model="gpt-4o-mini")
    summ = out.get("scoring_summary") or {}
    assert summ.get("attempted_questions") == 3
    assert summ.get("generated_questions") == 3  # aligned list length, not prefetched pool
    assert summ.get("evaluated_questions") == 3
    assert out.get("attempted_questions_only") is True
    assert len(out.get("per_question") or []) == 3


def test_decimal_score_and_percent_helpers():
    assert format_decimal_score(7.456) == 7.46
    assert format_percent_from_ten_scale(8.26) == 82.6


def test_build_question_avoid_history_dedupes_sources():
    hist = build_question_avoid_history(
        global_recent=["What is REST?", "What is REST?"],
        job_recent=["Explain Docker?"],
        manual_questions=["Explain Docker?"],
        template_preview=["How do you test APIs?"],
        limit=10,
    )
    assert len(hist) == 3
    assert "What is REST?" in hist
