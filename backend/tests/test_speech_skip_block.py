"""Auto-skip blocked when client reports speech evidence."""

import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture()
def answer_client(monkeypatch):
    main = importlib.import_module("main")
    sessions = {}

    def _fake_require_user(request, roles):
        return ({"sub": "cand@test", "role": "candidate"}, None)

    monkeypatch.setattr(main, "_require_user", _fake_require_user)
    monkeypatch.setattr(main, "_session_key_from_payload", lambda p: "sk-test")
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "append_interview_turn", lambda *a, **k: None)
    monkeypatch.setattr(main, "_schedule_turn_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(main, "_expand_time_mode_pool", lambda *a, **k: None)
    monkeypatch.setattr(main, "remember_asked_question", lambda *a, **k: None)
    monkeypatch.setattr(main, "is_warmup_index", lambda *a, **k: False)
    monkeypatch.setattr(main, "detect_skill_from_question", lambda *a, **k: "kotlin")
    monkeypatch.setattr(main, "sessions", sessions)

    sessions["sk-test"] = {
        "current": 0,
        "questions": ["Q1", "Q2"],
        "answers": [],
        "meta": {"jd_skills": ["kotlin"], "timing_mode": "count", "num_q": 2, "question_source": "dynamic"},
    }
    return main, sessions


def test_auto_skip_converts_to_answer_with_speech_meta(answer_client):
    main, sessions = answer_client
    req = SimpleNamespace()
    meta = '{"trigger":"no_response","speech_confirmed":true,"word_count":2,"interim_transcript":"hello world"}'
    out = main.answer(req, ans="skip", action="skip", auto_advance_meta=meta)
    assert out["status"] == "ok"
    assert out["skipped"] is False
    assert sessions["sk-test"]["answers"] == ["hello world"]
    assert sessions["sk-test"]["current"] == 1


def test_manual_skip_without_speech_meta_allowed(answer_client):
    main, sessions = answer_client
    req = SimpleNamespace()
    out = main.answer(req, ans="skip", action="skip", skip_reason="Candidate skipped manually")
    assert out["status"] == "ok"
    assert out["skipped"] is True
    assert sessions["sk-test"]["answers"] == ["skip"]
