"""A recruiter joins a live AI interview (11 Oct 2026): pause, resume, a queued
question, the call hand-shake, the clock and the integrity rules."""
from __future__ import annotations

import importlib
import time
from pathlib import Path

import pytest

from services import interview_live as live

main = importlib.import_module("main")


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def test_pause_and_resume_count_the_paused_time():
    ctrl = live.empty_control()
    live.pause(ctrl, "Asha", "", 1000.0)
    assert live.is_paused(ctrl, 1010.0)
    assert live.total_paused_sec(ctrl, 1030.0) == pytest.approx(30.0)
    live.pause(ctrl, "Asha", "", 1040.0)  # already paused — no second start
    live.resume(ctrl, "Asha", 1060.0)
    assert not live.is_paused(ctrl, 1061.0)
    assert ctrl["pause_total_sec"] == pytest.approx(60.0)
    assert ctrl["pause_count"] == 1


def test_a_long_pause_ends_by_itself(monkeypatch):
    monkeypatch.setenv("INTERVIEW_LIVE_MAX_PAUSE_SEC", "120")
    ctrl = live.empty_control()
    live.pause(ctrl, "Asha", "", 1000.0)
    assert live.is_paused(ctrl, 1100.0)
    assert not live.is_paused(ctrl, 1130.0)
    assert live.expire_pause(ctrl, 1130.0) is True
    assert ctrl["paused_at"] is None and ctrl["pause_total_sec"] == pytest.approx(120.0)
    assert ctrl["log"][-1]["action"] == "auto_resumed"


def test_question_rules():
    ctrl = live.empty_control()
    with pytest.raises(live.LiveError):
        live.add_question(ctrl, "hi", "Asha", 1.0)
    for i in range(live.MAX_PENDING_QUESTIONS):
        live.add_question(ctrl, f"Explain topic number {i}", "Asha", 1.0)
    with pytest.raises(live.LiveError):
        live.add_question(ctrl, "One question too many", "Asha", 1.0)
    first = ctrl["questions"][0]["id"]
    live.remove_question(ctrl, first, "Asha", 2.0)
    assert len(ctrl["questions"]) == live.MAX_PENDING_QUESTIONS - 1


def test_call_offer_reaches_the_candidate_until_answered():
    ctrl = live.empty_control()
    reply = live.empty_reply()
    with pytest.raises(live.LiveError):
        live.start_call(ctrl, "not sdp", "Asha", 1.0)
    live.start_call(ctrl, "v=0\r\nfake", "Asha Rao", 1.0)
    view = live.candidate_view(ctrl, reply, 2.0)
    assert view["call"]["offer"].startswith("v=") and view["call"]["by"] == "Asha"
    live.set_answer(reply, ctrl, ctrl["call"]["id"], "v=0\r\nanswer", 3.0)
    assert live.candidate_view(ctrl, reply, 4.0)["call"]["offer"] is None
    staff = live.staff_view(ctrl, reply, 4.0)
    assert staff["call"]["answered"] is True and staff["call"]["answer"].startswith("v=")
    with pytest.raises(live.LiveError):
        live.set_answer(reply, ctrl, "someone-elses-call", "v=0", 5.0)
    live.end_call(ctrl, "Asha", 6.0)
    assert live.candidate_view(ctrl, reply, 7.0)["call"] is None


def test_candidate_view_never_carries_the_log_or_the_team():
    ctrl = live.empty_control()
    live.join(ctrl, "u1", "Asha Rao", 1.0)
    live.pause(ctrl, "Asha Rao", "checking something", 2.0)
    view = live.candidate_view(ctrl, live.empty_reply(), 3.0)
    assert "log" not in view and "joined" not in view and view["by"] == "Asha"


def test_inserting_a_question_moves_everything_after_it():
    session = {
        "current": 2,
        "questions": ["Intro", "Q1", "Q2", "Q3"],
        "answers": ["a0", "a1"],
        "meta": {"warmup_indices": [0], "followup_indices": [3],
                 "question_specs": {"2": {"kind": "normal"}, "3": {"kind": "coding"}}},
    }
    applied = live.insert_questions(session, [{"id": "x1", "text": "Tell me about CAN bus", "by": "Asha"}])
    assert applied == [{"id": "x1", "text": "Tell me about CAN bus", "by": "Asha", "index": 2}]
    assert session["questions"] == ["Intro", "Q1", "Tell me about CAN bus", "Q2", "Q3"]
    meta = session["meta"]
    assert meta["followup_indices"] == [4]
    assert meta["question_specs"]["4"]["kind"] == "coding"
    assert live.is_recruiter_index(meta, 2) and live.questions_inserted(meta) == 1
    # The same queued question is never inserted twice.
    assert live.insert_questions(session, [{"id": "x1", "text": "Tell me about CAN bus"}]) == []


def test_a_question_queued_during_the_warm_up_goes_after_it():
    session = {"current": 0, "questions": ["Intro", "Q1"], "meta": {"warmup_indices": [0]}}
    live.insert_questions(session, [{"id": "w", "text": "What is an ISR?"}])
    assert session["questions"] == ["Intro", "What is an ISR?", "Q1"]


def test_the_clock_stops_while_paused():
    from candidate.service import interview_elapsed_seconds

    now = time.time()
    meta = {"interview_started_epoch": now - 600}
    ctrl = live.empty_control()
    ctrl["pause_total_sec"] = 120.0
    ctrl["paused_at"] = now - 60
    live.mirror_clock(meta, ctrl, now)
    assert interview_elapsed_seconds({"meta": meta}) == pytest.approx(600 - 120 - 60, abs=2)


# ---------------------------------------------------------------------------
# Wiring in main.py, with an in-memory stand-in for the two columns
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(monkeypatch):
    data = {"tok": [{}, {}]}

    def _get(db, token):
        if token not in data:
            return None
        return dict(data[token][0]), dict(data[token][1])

    monkeypatch.setattr(main, "get_live_columns", _get)
    monkeypatch.setattr(main, "set_live_control", lambda db, t, c: data[t].__setitem__(0, c))
    monkeypatch.setattr(main, "set_live_reply", lambda db, t, r: data[t].__setitem__(1, r))
    main._LIVE_CACHE.clear()
    return data


def _session():
    return {"current": 1, "questions": ["Intro", "Q1", "Q2"], "answers": ["a0"],
            "meta": {"invite_token": "tok", "warmup_indices": [0], "num_q": 2,
                     "interview_started_epoch": time.time() - 30}}


def test_paused_interview_refuses_and_then_asks_the_recruiters_question(store):
    s = _session()
    ctrl = live.empty_control()
    live.pause(ctrl, "Asha Rao", "", time.time())
    live.add_question(ctrl, "How would you debug a hard fault?", "Asha Rao", time.time())
    store["tok"][0] = ctrl
    body = main._live_pause_payload(s)
    assert body and body["code"] == "live_paused" and body["live"]["paused"] is True
    assert s["questions"] == ["Intro", "Q1", "Q2"]  # nothing inserted while paused

    live.resume(store["tok"][0], "Asha Rao", time.time())
    main._LIVE_CACHE.clear()
    assert main._live_pause_payload(s) is None
    assert s["questions"][1] == "How would you debug a hard fault?"
    saved = live.normalize_control(store["tok"][0])
    assert saved["questions"] == [] and saved["asked"][0]["index"] == 1
    # The interview's question budget grew by one, so Q2 is still asked.
    from candidate.service import _question_cap
    assert _question_cap(s) == 2 + 1 + 1
    assert s["meta"]["live_summary"]["pause_count"] == 1


def test_an_answer_never_inserts_mid_turn(store):
    s = _session()
    ctrl = live.empty_control()
    live.add_question(ctrl, "What does volatile mean in C?", "Asha", time.time())
    store["tok"][0] = ctrl
    assert main._live_pause_payload(s, apply_questions=False) is None
    assert len(s["questions"]) == 3


def test_violations_are_not_counted_while_paused(store):
    import asyncio
    from types import SimpleNamespace

    s = _session()
    main.sessions["inv:tok"] = s
    try:
        ctrl = live.empty_control()
        live.pause(ctrl, "Asha", "", time.time())
        store["tok"][0] = ctrl
        req = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        main_require = main._require_user
        main._require_user = lambda request, roles: ({"invite_token": "tok", "sub": "invite-tok", "role": "candidate"}, None)
        try:
            out = asyncio.run(main.interview_violation(req, violation_type="tab_switch", details="",
                                                       current_question="", fullscreen_status="",
                                                       browser_visibility="", window_focus=""))
        finally:
            main._require_user = main_require
        assert out["status"] == "ignored" and out["reason"] == "paused"
        assert not s["meta"].get("violations")
    finally:
        main.sessions.pop("inv:tok", None)


def test_columns_round_trip_on_sqlite(tmp_path: Path):
    import auth_db

    db = tmp_path / "auth.db"
    auth_db.init_auth_db(db)
    sched = auth_db.create_interview_schedule(db, "hr", "Cand", "c@x.in", "2026-10-11 10:00")
    token = sched["invite_token"]
    assert auth_db.get_live_columns(db, token) == ({}, {})
    auth_db.set_live_control(db, token, {"paused_at": 5.0})
    auth_db.set_live_reply(db, token, {"call_id": "c1"})
    assert auth_db.get_live_columns(db, token) == ({"paused_at": 5.0}, {"call_id": "c1"})
    assert auth_db.get_live_columns(db, "missing") is None


def test_routes_are_registered_and_staff_only():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    assert client.get("/interview/live/tok").status_code in (401, 403)
    assert client.post("/interview/live/tok/pause", json={}).status_code in (401, 403)
    assert client.get("/candidate/live/state").status_code in (401, 403)


def test_next_and_submit_hold_the_session_lock():
    import ast

    src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name: ast.get_source_segment(src, n) for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "with session_lock(sk):" in funcs["next_question"]
    assert "with session_lock(sk):" in funcs["submit"]
