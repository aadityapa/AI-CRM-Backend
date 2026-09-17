"""Invite link policy (16 Sep 2026, user decision).

A link cannot be used BEFORE its scheduled slot, can be used ANY time after
it, and works exactly ONCE — completion or termination closes it, not the
clock. `INVITE_LINK_VALID_HOURS` brings an expiry window back when wanted.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

main = importlib.import_module("main")
IST = main.IST


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def test_link_never_expires_by_default(monkeypatch):
    monkeypatch.delenv("INVITE_LINK_VALID_HOURS", raising=False)
    slot = datetime.now(IST) - timedelta(days=30)
    state = main._invite_access_state({"scheduled_at_local": _stamp(slot)})
    assert state["ok"] is True and state["reason"] == ""


def test_window_can_be_configured(monkeypatch):
    monkeypatch.setenv("INVITE_LINK_VALID_HOURS", "24")
    slot = datetime.now(IST) - timedelta(hours=30)
    assert main._invite_access_state({"scheduled_at_local": _stamp(slot)})["reason"] == "expired"
    slot = datetime.now(IST) - timedelta(hours=2)
    assert main._invite_access_state({"scheduled_at_local": _stamp(slot)})["ok"] is True


def test_before_the_slot_is_a_wait(monkeypatch):
    monkeypatch.delenv("INVITE_LINK_VALID_HOURS", raising=False)
    slot = datetime.now(IST) + timedelta(hours=1)
    state = main._invite_access_state({"scheduled_at_local": _stamp(slot)})
    assert state["reason"] == "scheduled_wait" and state["seconds_until_start"] > 3500


def _request(device="dev-1"):
    return SimpleNamespace(headers={"x-device-id": device}, client=SimpleNamespace(host="127.0.0.1"))


@pytest.fixture()
def schedule(monkeypatch):
    row = {
        "invite_token": "tok", "candidate_email": "c@x.in", "access_key": "ABC123",
        "session_status": "pending", "active_device_id": "", "login_attempts": 0,
        "scheduled_at_local": _stamp(datetime.now(IST) + timedelta(hours=2)),
        "candidate_name": "Cand",
    }
    monkeypatch.setattr(main, "get_schedule_by_token", lambda db, token: dict(row))
    calls = []
    monkeypatch.setattr(main, "increment_schedule_login_attempts", lambda db, token: calls.append(1) or 1)
    monkeypatch.setattr(main, "update_schedule_field", lambda *a, **k: None)
    return row, calls


def test_verify_before_the_slot_is_refused_without_spending_an_attempt(schedule):
    row, calls = schedule
    res = main.candidate_invite_verify("tok", _request(), email="c@x.in", access_key="WRONG")
    assert res.status_code == 425
    assert b"scheduled_wait" in res.body
    assert calls == []  # no attempt burned


def test_verify_on_a_closed_link_does_not_burn_attempts(schedule):
    row, calls = schedule
    row["session_status"] = "completed"
    row["scheduled_at_local"] = _stamp(datetime.now(IST) - timedelta(hours=1))
    res = main.candidate_invite_verify("tok", _request(), email="c@x.in", access_key="WRONG")
    assert res.status_code == 403
    assert b'"invite_state":"completed"' in res.body.replace(b" ", b"")
    assert calls == []


def test_resume_info_reports_the_saved_turn():
    assert main._resume_info({"current": 0, "questions": ["a", "b"]}) is None
    assert main._resume_info({"current": 3, "questions": ["a"] * 8}) == {"current": 3, "total": 8}
    assert main._resume_info(None) is None
