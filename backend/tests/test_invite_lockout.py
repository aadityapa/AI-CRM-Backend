"""Invite verification must use post-increment attempt counts (no stale lockout)."""

import importlib
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse


@pytest.fixture()
def verify_client(monkeypatch):
    main = importlib.import_module("main")
    state = {"attempts": 0}

    def _increment(_db, token):
        state["attempts"] += 1
        return state["attempts"]

    monkeypatch.setattr(main, "increment_schedule_login_attempts", _increment)
    monkeypatch.setattr(
        main,
        "get_schedule_by_token",
        lambda _db, token: {
            "candidate_email": "cand@example.com",
            "access_key": "ABCD",
            "session_status": "pending",
            "login_attempts": 0,
        },
    )
    monkeypatch.setattr(main, "_invite_access_state", lambda record: {"reason": "ok"})
    monkeypatch.setattr(main, "update_schedule_field", lambda *a, **k: None)
    monkeypatch.setattr(main, "_maybe_prewarm_invite_session", lambda *a, **k: None)
    monkeypatch.setattr(main, "_invite_prewarm_snapshot", lambda *a, **k: {})
    return main, state


def test_lockout_uses_incremented_attempt_count(verify_client):
    main, state = verify_client
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    for _ in range(10):
        out = main.candidate_invite_verify("tok", req, email="cand@example.com", access_key="WRONG")
        assert isinstance(out, JSONResponse)
        assert out.status_code == 403

    blocked = main.candidate_invite_verify("tok", req, email="cand@example.com", access_key="WRONG")
    assert isinstance(blocked, JSONResponse)
    assert blocked.status_code == 403
    assert state["attempts"] == 11
