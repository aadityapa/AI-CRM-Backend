"""Echo / repeat-question answers must score very low."""

from ai import answer_echoes_question, evaluate_turn_with_model


def test_kotlin_coroutines_echo_detected():
    echoed, reason = answer_echoes_question(
        "What is Kotlin Coroutines?",
        "Kotlin Coroutines",
    )
    assert echoed is True
    assert "repeat" in reason.lower() or "explanation" in reason.lower()


def test_mvvm_one_word_echo_detected():
    echoed, _ = answer_echoes_question("What is MVVM?", "MVVM")
    assert echoed is True


def test_proper_answer_not_echo():
    echoed, _ = answer_echoes_question(
        "What is MVVM?",
        "MVVM stands for Model View ViewModel and separates UI from business logic.",
    )
    assert echoed is False


def test_evaluate_turn_echo_scores_low_without_openai(monkeypatch):
    def _fail_openai(*_a, **_k):
        raise AssertionError("OpenAI should not be called for echo answers")

    monkeypatch.setattr("ai.tracked_chat_completion", _fail_openai)
    out = evaluate_turn_with_model("What is Kotlin Coroutines?", "Kotlin Coroutines", "kotlin", "medium")
    assert int(out.get("score", 10)) == 0
    assert "repeat" in str(out.get("reason", "")).lower() or "explanation" in str(out.get("reason", "")).lower()
