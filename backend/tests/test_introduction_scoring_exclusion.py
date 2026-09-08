"""INTRODUCTION / warmup turns are stored but excluded from scored aggregates."""

from __future__ import annotations

from ai import evaluate_per_question_interview_batch, merge_per_question_eval_into_report
from candidate.service import next_question_payload
from utils.warmup import (
    QUESTION_TYPE_INTRODUCTION,
    WARMUP_QUESTION_TEXT,
    stamp_introduction_question_types,
)


def test_next_question_payload_marks_introduction_type():
    session = {
        "current": 0,
        "questions": [WARMUP_QUESTION_TEXT, "Explain CAN bus timing."],
        "meta": {"warmup_indices": [0], "jd_skills": ["can"], "timing_mode": "count", "num_q": 1},
    }
    out = next_question_payload(session)
    assert out["is_warmup"] is True
    assert out["question_type"] == QUESTION_TYPE_INTRODUCTION


def test_stamp_introduction_question_types():
    meta = {"warmup_indices": [0]}
    stamp_introduction_question_types(meta, [0])
    assert meta["question_types"]["0"] == QUESTION_TYPE_INTRODUCTION


def test_per_question_batch_excludes_warmup_text(monkeypatch):
    monkeypatch.setattr("openai_client.openai_key_configured", lambda *_a, **_k: False)

    qs = [WARMUP_QUESTION_TEXT, "What is UDS?"]
    ans = ["I am Alex, a firmware engineer.", "UDS is a diagnostic protocol."]
    rows = evaluate_per_question_interview_batch(qs, ans, model="gpt-4o-mini")
    assert len(rows) == 2
    assert rows[0]["score"] == 0.0
    weakness_blob = " ".join(rows[0].get("weaknesses") or []).lower()
    assert "warmup" in weakness_blob or "introduction" in weakness_blob
    assert float(rows[1].get("score") or 0.0) > 0.0


def test_merge_mean_ignores_introduction_when_present(monkeypatch):
    def _fake_batch(questions, answers, model="gpt-4o-mini", *, meta=None, **_kw):
        return [
            {"question_index": 1, "score": 0.0, "feedback": "Introduction warmup (not counted toward overall score)."},
            {"question_index": 2, "score": 8.0, "strengths": [], "weaknesses": [], "feedback": "Good"},
        ]

    monkeypatch.setattr("ai.evaluate_per_question_interview_batch", _fake_batch)
    base = {"overall_score": 9.0, "skill_scores": []}
    qs = [WARMUP_QUESTION_TEXT, "What is CAN?"]
    ans = ["Hi, I am Sam.", "Controller area network."]
    out = merge_per_question_eval_into_report(base, qs, ans, model="gpt-4o-mini")
    assert out["technical_score"] == 8.0
    summ = out.get("scoring_summary") or {}
    assert summ.get("evaluated_questions") == 1
