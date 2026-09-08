"""Duplicate /answer submissions for the same turn must be idempotent."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture()
def answer_client(monkeypatch):
    main = importlib.import_module("main")
    sessions = {}

    monkeypatch.setattr(main, "_require_user", lambda request, roles: ({"sub": "cand@test", "role": "candidate"}, None))
    monkeypatch.setattr(main, "_enforce_invite_device_binding", lambda *a, **k: None)
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-test")
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_interview_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_schedule_turn_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(main, "_expand_time_mode_pool", lambda *a, **k: None)
    monkeypatch.setattr(main, "remember_asked_question", lambda *a, **k: None)
    monkeypatch.setattr(main, "is_warmup_index", lambda *a, **k: False)
    monkeypatch.setattr(main, "detect_skill_from_question", lambda *a, **k: "can")
    monkeypatch.setattr(main, "sessions", sessions)

    sessions["sk-test"] = {
        "current": 0,
        "questions": ["Q1", "Q2"],
        "answers": [],
        "meta": {"jd_skills": ["can"], "timing_mode": "count", "question_source": "manual"},
    }
    return main, sessions


def test_duplicate_answer_returns_same_next_without_double_append(answer_client):
    main, sessions = answer_client
    req = SimpleNamespace()

    first = main.answer(req, ans="First answer text here.", action="send")
    assert first["status"] == "ok"
    assert sessions["sk-test"]["answers"] == ["First answer text here."]
    assert sessions["sk-test"]["current"] == 1

    # Simulate race: answer persisted but current not advanced yet.
    sessions["sk-test"]["current"] = 0
    dup = main.answer(req, ans="Duplicate payload.", action="send")
    assert dup.get("idempotent") is True
    assert sessions["sk-test"]["answers"] == ["First answer text here."]
    assert sessions["sk-test"]["current"] == 0
    assert dup.get("next", {}).get("question") == "Q2"
