"""Invite login must not block on OpenAI — fast_only bootstrap + integrity dedupe."""

from __future__ import annotations

import time


from main import (
    _bootstrap_invite_interview_session,
    _dedupe_integrity_schedule_rows,
    _wait_for_invite_prewarm,
    sessions,
)


def test_dedupe_integrity_collapses_duplicate_email_in_flight():
    rows = [
        {
            "invite_token": "tok-a",
            "candidate_email": "a@example.com",
            "session_status": "verified",
            "scheduled_at_local": "2026-06-01 10:00",
        },
        {
            "invite_token": "tok-b",
            "candidate_email": "a@example.com",
            "session_status": "active",
            "scheduled_at_local": "2026-06-02 10:00",
            "interview_started_at": "2026-06-02T10:05:00+05:30",
        },
        {
            "invite_token": "tok-c",
            "candidate_email": "b@example.com",
            "session_status": "completed",
            "scheduled_at_local": "2026-06-01 09:00",
        },
    ]
    out = _dedupe_integrity_schedule_rows(rows)
    emails_active = [
        r["candidate_email"]
        for r in out
        if str(r.get("session_status")).lower() in {"pending", "verified", "active"}
    ]
    assert emails_active.count("a@example.com") == 1
    assert any(r.get("invite_token") == "tok-b" for r in out)
    assert any(r.get("invite_token") == "tok-c" for r in out)


def test_fast_bootstrap_skips_openai(monkeypatch):
    token = "test-invite-fast-bootstrap"
    skey = f"inv:{token}"
    sessions.pop(skey, None)

    schedule = {
        "candidate_name": "Test User",
        "candidate_email": "test@example.com",
        "scheduled_at_local": "2026-06-04 12:00",
        "notes": "",
    }

    def _slow_ai(*_a, **_k):
        raise AssertionError("OpenAI must not be called during fast_only bootstrap")

    monkeypatch.setattr("main._generate_interview_questions", _slow_ai)
    monkeypatch.setattr("main.get_interview_progress_by_invite", lambda *a, **k: None)
    monkeypatch.setattr("main._persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr("main.get_job_template", lambda *a, **k: None)
    monkeypatch.setattr("main.list_job_templates", lambda *a, **k: [])
    monkeypatch.setattr("main.generate_questions_fallback", lambda *a, **k: ["Fallback Q1?", "Fallback Q2?"])

    started = time.time()
    result = _bootstrap_invite_interview_session(token, schedule, fast_only=True)
    elapsed = time.time() - started

    assert result.get("error") is None
    assert result.get("fast_only") is True
    assert sessions.get(skey)
    assert len(sessions[skey].get("questions") or []) >= 1
    assert elapsed < 5.0
    sessions.pop(skey, None)


def test_wait_for_prewarm_returns_when_session_ready(monkeypatch):
    token = "wait-prewarm-token"
    skey = f"inv:{token}"
    sessions[skey] = {"questions": ["Q?"], "meta": {}, "answers": [], "current": 0}
    snap = _wait_for_invite_prewarm(token, timeout_sec=1.0)
    assert snap.get("status") == "ready"
    sessions.pop(skey, None)
