"""Fast /submit path returns before evaluation runs (May 2026)."""

from __future__ import annotations

import copy


from main import _append_pending_answer_on_submit, _background_finalize_report


def test_append_pending_answer_on_submit_appends_once():
    session = {
        "completed": False,
        "current": 0,
        "questions": ["What is REST?"],
        "answers": [],
        "meta": {"asked_questions": []},
    }
    ok = _append_pending_answer_on_submit(session, "REST is representational.")
    assert ok is True
    assert len(session["answers"]) == 1
    ok2 = _append_pending_answer_on_submit(session, "duplicate")
    assert ok2 is False
    assert len(session["answers"]) == 1


def test_background_finalize_calls_evaluate(monkeypatch):
    called = {"n": 0}

    def _fake_eval(session):
        called["n"] += 1
        assert session.get("submitted") is True
        return {"overall_score": 7.5}, {"ist_iso": "2026-05-20T12:00:00+05:30"}, {"id": "iv-1"}

    monkeypatch.setattr("main._evaluate_and_store_report", _fake_eval)
    monkeypatch.setattr("main.append_from_evaluation", lambda *a, **k: None)
    monkeypatch.setattr("main.get_interview_record_payload", lambda *a, **k: None)
    monkeypatch.setattr("main._persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr("main._persist_hr_record_mirror", lambda *a, **k: None)
    monkeypatch.setattr("main.upsert_interview_record_snapshot", lambda *a, **k: None)
    monkeypatch.setattr("main.invalidate_hr_dashboard_cache", lambda *a, **k: None)

    snap = {
        "submitted": True,
        "completed": True,
        "questions": ["Q1?"],
        "answers": ["A1"],
        "meta": {"jd_skills": ["python"], "interview_id": "iv-1"},
    }
    _background_finalize_report(copy.deepcopy(snap))
    assert called["n"] == 1
