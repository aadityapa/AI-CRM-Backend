"""Skip answer flow: per-question independence and mixed skip/answer sequences."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture()
def answer_client(monkeypatch):
    main = importlib.import_module("main")
    sessions = {}
    eval_calls = []

    def _fake_require_user(request, roles):
        return ({"sub": "cand@test", "role": "candidate"}, None)

    monkeypatch.setattr(main, "_require_user", _fake_require_user)
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-test")
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_interview_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        main,
        "_apply_turn_evaluation",
        lambda s, q, a: eval_calls.append({"question": q, "answer": a}),
    )
    state = {"expand_calls": 0}
    monkeypatch.setattr(main, "_expand_time_mode_pool", lambda s: state.__setitem__("expand_calls", state["expand_calls"] + 1))
    monkeypatch.setattr(main, "remember_asked_question", lambda *a, **k: None)
    monkeypatch.setattr(main, "is_warmup_index", lambda *a, **k: False)
    monkeypatch.setattr(main, "detect_skill_from_question", lambda *a, **k: "can")
    monkeypatch.setattr(main, "generate_questions_fallback", lambda *a, **k: ["What is UDS?"])
    monkeypatch.setattr(main, "sessions", sessions)

    sessions["sk-test"] = {
        "current": 0,
        "questions": ["Q1", "Q2", "Q3", "Q4"],
        "answers": [],
        "meta": {"jd_skills": ["can"], "timing_mode": "count", "num_q": 4, "question_source": "manual"},
    }
    return main, sessions, state, eval_calls


def test_skip_returns_next_and_records_metadata(answer_client):
    main, sessions, state, eval_calls = answer_client
    req = SimpleNamespace()
    out = main.answer(req, ans="", action="skip", skip_reason="Candidate skipped manually")
    assert out["status"] == "ok"
    assert out["skipped"] is True
    assert out.get("next", {}).get("question") == "Q2"
    assert sessions["sk-test"]["answers"] == ["skip"]
    assert eval_calls == []

    meta = sessions["sk-test"]["meta"]
    skipped_turns = meta.get("skipped_turns") or []
    assert len(skipped_turns) == 1
    assert skipped_turns[0]["status"] == "SKIPPED"
    assert skipped_turns[0]["question_text"] == "Q1"
    assert state["expand_calls"] == 0


def test_mixed_skip_and_answer_sequence(answer_client):
    """Q1 skip, Q2 answer, Q3 answer, Q4 skip — each turn independent."""
    main, sessions, _state, eval_calls = answer_client
    req = SimpleNamespace()

    main.answer(req, ans="", action="skip")
    main.answer(req, ans="CAN uses differential signaling.", action="send")
    main.answer(req, ans="UDS runs over CAN transport.", action="send")
    out = main.answer(req, ans="", action="skip")

    assert sessions["sk-test"]["answers"] == [
        "skip",
        "CAN uses differential signaling.",
        "UDS runs over CAN transport.",
        "skip",
    ]
    assert len(eval_calls) == 2
    assert eval_calls[0]["question"] == "Q2"
    assert eval_calls[1]["question"] == "Q3"
    assert out["skipped"] is True
    assert len(sessions["sk-test"]["meta"].get("skipped_turns") or []) == 2


def test_manual_skip_with_transcript_converts_to_answer(answer_client):
    main, sessions, _state, eval_calls = answer_client
    req = SimpleNamespace()
    meta = '{"trigger":"manual_skip_with_answer","capture_text":"Coroutines are async.","word_count":3}'
    out = main.answer(req, ans="skip", action="skip", auto_advance_meta=meta)
    assert out["status"] == "ok"
    assert out["skipped"] is False
    assert sessions["sk-test"]["answers"] == ["Coroutines are async."]
    assert len(eval_calls) == 1


def test_answer_after_skip_is_evaluated(answer_client):
    """Q1 skip then Q2 answer — only Q1 skipped."""
    main, sessions, _state, eval_calls = answer_client
    req = SimpleNamespace()

    main.answer(req, ans="", action="skip")
    main.answer(req, ans="Practical CAN debugging example.", action="send")

    assert sessions["sk-test"]["answers"] == ["skip", "Practical CAN debugging example."]
    assert len(eval_calls) == 1
    assert eval_calls[0]["answer"] == "Practical CAN debugging example."
