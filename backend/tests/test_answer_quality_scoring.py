"""Strict scoring guards for repeated, incomplete, and keyword-only answers."""

from ai import (
    answer_echoes_question,
    apply_quality_caps_to_per_question_row,
    evaluate_per_question_interview_batch,
    evaluate_turn_with_model,
    preflight_per_question_evaluation,
)


def test_can_fd_verbatim_repeat_scores_zero():
    row = preflight_per_question_evaluation("What is CAN FD?", "What is CAN FD?", 1)
    assert row is not None
    assert float(row["score"]) == 0.0
    assert "No significant technical strengths identified." in row["strengths"]


def test_can_fd_keyword_only_scores_low():
    row = preflight_per_question_evaluation("What is CAN FD?", "CAN FD", 1)
    assert row is not None
    assert float(row["score"]) <= 0.5


def test_can_fd_good_answer_passes_preflight():
    q = "What is CAN FD?"
    a = (
        "CAN FD is an extension of Classical CAN that supports payloads up to 64 bytes "
        "and allows higher data rates during the data phase."
    )
    assert preflight_per_question_evaluation(q, a, 1) is None
    row = apply_quality_caps_to_per_question_row(
        {"question_index": 1, "score": 8.5, "strengths": ["Strong technical detail."], "weaknesses": []},
        q,
        a,
    )
    assert float(row["score"]) >= 7.0


def test_boot_sequence_repeat_with_leadin_scores_zero():
    q = "Explain the boot sequence of system_server and how system services are registered."
    a = "Yeah, explain the boot sequence of system server and how system service are registered."
    echoed, _ = answer_echoes_question(q, a)
    assert echoed is True
    row = preflight_per_question_evaluation(q, a, 3)
    assert row is not None
    assert float(row["score"]) == 0.0


def test_incomplete_fragment_scores_near_zero():
    q = "When would you use shared memory instead of Binder IPC?"
    a = "when would you use memory instead of"
    row = preflight_per_question_evaluation(q, a, 4)
    assert row is not None
    assert float(row["score"]) <= 0.5


def test_off_topic_answer_capped_by_relevance(monkeypatch):
    monkeypatch.setattr("openai_client.openai_key_configured", lambda _p: False)
    qs = ["Explain the difference between Binderized HAL and Passthrough HAL."]
    ans = ["When would you use memory instead of binder IPC?"]
    rows = evaluate_per_question_interview_batch(qs, ans, model="gpt-4o-mini")
    assert float(rows[0]["score"]) <= 2.0


def test_evaluate_turn_echo_scores_zero_without_openai(monkeypatch):
    def _fail_openai(*_a, **_k):
        raise AssertionError("OpenAI should not be called for echo answers")

    monkeypatch.setattr("ai.tracked_chat_completion", _fail_openai)
    out = evaluate_turn_with_model("What is CAN FD?", "What is CAN FD?", "can", "medium")
    assert int(out.get("score", 10)) == 0
