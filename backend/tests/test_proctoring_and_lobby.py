"""Anti-cheating checks per template + the lobby, silent watch and recruiter
terminate on the Integrity tab (13 Nov 2026)."""
from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace

import pytest

from services import device_check as dc
from services import interview_integrity as ii
from services import interview_live as live
from services.interview import proctoring as pr

main = importlib.import_module("main")

SDP = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n"


# ---------------------------------------------------------------------------
# Template settings
# ---------------------------------------------------------------------------

def test_settings_default_off_and_are_cleaned():
    off = pr.proctoring_settings({})
    assert off["enabled"] is False and off["headphones"] == "off" and off["gesture_prompts"] == 0

    raw = {"headphones": "require", "lighting": "warn", "gestureCheck": True, "gesturePrompts": 99,
           "gazeCalibration": "yes", "readingDetection": 1, "roomScan": "true", "junk": "x"}
    cfg = pr.clean_config(raw)
    assert cfg == {"headphones": "require", "lighting": "warn", "gestureCheck": True,
                   "gesturePrompts": pr.MAX_GESTURE_PROMPTS, "gazeCalibration": True,
                   "readingDetection": True, "roomScan": True}
    assert pr.clean_config({"headphones": "loud", "gesturePrompts": "x"})["headphones"] == "off"

    meta: dict = {}
    pr.stamp(meta, {"proctoring": raw})
    back = pr.settings_of(meta)
    assert back["enabled"] and back["headphones"] == "require" and back["room_scan"] is True
    assert pr.settings_of({})["enabled"] is False  # a session from before the feature


def test_report_summary_counts_and_flags():
    settings = pr.proctoring_settings({"proctoring": {"headphones": "require", "gesturePrompts": 3}})
    device_log = [
        {"check": "headphones", "ok": False, "reason": "speaker_leak"},
        {"check": "headphones", "ok": True, "detail": "tone heard 2 dB"},
        {"check": "lighting", "ok": False, "reason": "too_dark"},
        {"check": "screen_share", "ok": True},
    ]
    events = [
        {"type": "gesture_prompt_ok"}, {"type": "gesture_prompt_missed"}, {"type": "gesture_prompt_missed"},
        {"type": "reading_pattern", "question": "3"}, {"type": "headphones_disconnected"},
        {"type": "room_scan_started", "timestamp": "t1"}, {"type": "room_scan_done", "timestamp": "t2"},
        {"type": "tab_switch"},
    ]
    out = pr.report_summary(settings, device_log, events)
    assert out["gesture_prompts"] == {"asked": 3, "passed": 1, "missed": 2}
    assert out["reading_questions"] == ["3"] and out["headphones_disconnected"] == 1
    assert out["room_scan"] == {"started_at": "t1", "done_at": "t2"}
    heads = next(c for c in out["pre_checks"] if c["check"] == "headphones")
    assert heads["attempts"] == 2 and heads["passed"] is True
    assert any("Lighting not passed" in f for f in out["flags"])
    assert any("Missed 2 of 3" in f for f in out["flags"])
    assert pr.report_summary(pr.proctoring_settings({}), [], [{"type": "tab_switch"}]) is None


# ---------------------------------------------------------------------------
# Device check + integrity taxonomy
# ---------------------------------------------------------------------------

def test_device_check_accepts_the_new_checks_and_explains_them():
    ev = dc.clean_event({"check": "headphones", "ok": False, "reason": "speaker_leak",
                         "detail": "tone heard 18 dB <script>", "attempt": 2}, at="2026-11-13T10:00:00")
    assert ev["check"] == "headphones" and "<" not in ev["detail"]
    assert dc.clean_event({"check": "teleport", "ok": True}) is None
    timeline = dc.timeline_events([ev, {"check": "gesture", "ok": True, "attempt": 1},
                                   {"check": "gaze_calibration", "ok": True}])
    assert [t["type"] for t in timeline] == ["headphones_failed", "gesture_check_ok", "gaze_calibration_ok"]
    assert "speakers" in timeline[0]["details"]
    assert dc.passed([ev], "headphones") is False
    assert dc.failure_count([ev], "headphones") == 1
    for t in timeline:
        assert t["type"] in ii.EVENT_TYPES  # every timeline type has a label


def test_new_events_are_soft_never_strikes():
    soft = ["gesture_prompt_missed", "headphones_disconnected", "low_light", "reading_pattern",
            "room_scan_skipped", "recruiter_terminated", "candidate_in_lobby"]
    for t in soft:
        assert t not in ii.STRIKE_TYPES
    s = ii.summarise([{"type": "gesture_prompt_missed", "timestamp": "a"}, {"type": "reading_pattern", "timestamp": "b"}])
    assert s["strikes"] == 0 and s["by_family"]["gesture"] == 1 and s["by_family"]["reading"] == 1
    assert s["integrity_score"] < 100


# ---------------------------------------------------------------------------
# Lobby, watch, terminate — pure rules
# ---------------------------------------------------------------------------

def test_lobby_records_the_stage_and_says_first_once():
    reply = live.empty_reply()
    assert live.set_lobby(reply, "opened", 100.0) is True
    assert live.set_lobby(reply, "device_check", 105.0) is False
    assert live.set_lobby(reply, "nonsense", 106.0) is False  # unknown → opened
    view = live.lobby_view(reply, 107.0)
    assert view["stage"] == "opened" and view["online"] is True and view["first_at"] == 100.0
    assert [s["stage"] for s in view["stages"]] == ["opened", "device_check", "opened"]
    assert live.lobby_view(live.empty_reply(), 1.0) is None
    assert live.normalize_reply({"lobby": {"stage": ""}})["lobby"] is None


def test_watch_is_limited_pruned_and_answered():
    ctrl = live.empty_control()
    reply = live.empty_reply()
    now = 1000.0
    for uid in ("a", "b", "c"):
        live.join(ctrl, uid, f"TA {uid}", now)
    with pytest.raises(live.LiveError):
        live.start_watch(ctrl, "a", "nope", "TA a", now)
    live.start_watch(ctrl, "a", SDP, "TA a", now)
    live.start_watch(ctrl, "b", SDP, "TA b", now)
    with pytest.raises(live.LiveError):
        live.start_watch(ctrl, "c", SDP, "TA c", now)
    live.start_watch(ctrl, "a", SDP, "TA a", now)  # replacing your own is fine
    view = live.candidate_view(ctrl, reply, now)
    assert view["watched"] is True and len(view["watch"]) == 2 and view["poll_ms"] == live.FAST_POLL_MS
    wid = ctrl["watch"]["a"]["id"]
    live.set_watch_answer(reply, ctrl, wid, SDP, now)
    assert len(live.candidate_view(ctrl, reply, now)["watch"]) == 1  # answered offers are not resent
    staff = live.staff_view(ctrl, reply, now, viewer_id="a")
    assert staff["watch"]["answered"] is True and staff["watch"]["answer"] == SDP
    with pytest.raises(live.LiveError):
        live.set_watch_answer(reply, ctrl, "gone", SDP, now)
    # "b" closed the panel: no presence for longer than the window.
    live.presence(ctrl, "a", "TA a", now + live.PRESENCE_WINDOW_SEC + 5)
    assert live.prune_watchers(ctrl, now + live.PRESENCE_WINDOW_SEC + 5) is True
    assert list(ctrl["watch"]) == ["a"]
    live.end_watch(ctrl, "a", "TA a", now + 60)
    assert ctrl["watch"] == {} and ctrl["log"][-1]["action"] == "watch_ended"


def test_terminate_needs_a_reason_and_ends_everything():
    ctrl = live.empty_control()
    live.pause(ctrl, "Asha", "", 10.0)
    live.start_call(ctrl, SDP, "Asha", 11.0)
    live.join(ctrl, "a", "Asha", 11.0)
    live.start_watch(ctrl, "a", SDP, "Asha", 11.0)
    with pytest.raises(live.LiveError):
        live.terminate(ctrl, "no", "Asha", 12.0)
    live.terminate(ctrl, "Reading answers from a phone", "Asha Rao", 20.0)
    assert ctrl["call"] is None and ctrl["watch"] == {} and ctrl["paused_at"] is None
    assert ctrl["pause_total_sec"] == pytest.approx(10.0)
    assert "Reading answers from a phone" in live.termination_reason(ctrl)
    view = live.candidate_view(ctrl, live.empty_reply(), 21.0)
    assert view["terminated"] == {"by": "Asha"}  # the reason stays internal
    rev = ctrl["rev"]
    live.terminate(ctrl, "again and again", "Asha", 22.0)  # idempotent
    assert ctrl["rev"] == rev
    assert live.report_summary(ctrl, 30.0)["terminated"]["by"] == "Asha Rao"


# ---------------------------------------------------------------------------
# Wiring in main.py
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(monkeypatch):
    data = {"tok": [{}, {}]}
    writes: list[dict] = []

    def _get(db, token):
        if token not in data:
            return None
        return dict(data[token][0]), dict(data[token][1])

    monkeypatch.setattr(main, "get_live_columns", _get)
    monkeypatch.setattr(main, "set_live_control", lambda db, t, c: data[t].__setitem__(0, c))
    monkeypatch.setattr(main, "set_live_reply", lambda db, t, r: data[t].__setitem__(1, r))
    monkeypatch.setattr(main, "update_schedule_field", lambda db, t, **kw: writes.append(kw))
    main._LIVE_CACHE.clear()
    data["writes"] = writes
    return data


def test_a_recruiter_termination_reaches_the_session(store):
    s = {"current": 1, "questions": ["Intro", "Q1"], "answers": ["a0"],
         "meta": {"invite_token": "tok", "warmup_indices": [0], "interview_started_epoch": time.time()}}
    ctrl = live.empty_control()
    live.terminate(ctrl, "Second person in the room", "Asha Rao", time.time())
    store["tok"][0] = ctrl
    body = main._live_pause_payload(s)
    assert body["terminated"] is True and body["code"] == "recruiter_terminated"
    meta = s["meta"]
    assert meta["terminated_by_recruiter"] and "Second person in the room" in meta["termination_reason"]
    assert meta["violations"][-1]["type"] == "termination"
    assert store["writes"][-1]["session_status"] == "terminated"
    n = len(meta["violations"])
    main._LIVE_CACHE.clear()
    main._live_pause_payload(s)
    assert len(meta["violations"]) == n  # stamped once


def test_violations_are_ignored_after_a_recruiter_ended_it(store):
    s = {"meta": {"invite_token": "tok", "terminated_by_recruiter": True}}
    main.sessions["inv:tok"] = s
    real = main._require_user
    main._require_user = lambda request, roles: ({"invite_token": "tok", "sub": "invite-tok", "role": "candidate"}, None)
    try:
        req = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        out = asyncio.run(main.interview_violation(req, violation_type="tab_switch", details="", current_question="",
                                                   fullscreen_status="", browser_visibility="", window_focus=""))
    finally:
        main._require_user = real
        main.sessions.pop("inv:tok", None)
    assert out["status"] == "ignored" and out["reason"] == "terminated"


def test_lobby_presence_answers_with_the_live_view_and_tells_the_ta_once(store, monkeypatch):
    told: list[str] = []
    monkeypatch.setattr(main, "get_schedule_by_token",
                        lambda db, t: {"invite_token": t, "session_status": "pending", "candidate_name": "Ravi"} if t == "tok" else None)
    monkeypatch.setattr(main, "_notify_ta_candidate_in_lobby", lambda token, rec: told.append(token))

    class _Req:
        def __init__(self, body):
            self._body = body
            self.headers = {}
            self.client = SimpleNamespace(host="127.0.0.1")

        async def json(self):
            return self._body

    fn = getattr(main.candidate_invite_presence, "__wrapped__", main.candidate_invite_presence)
    ctrl = live.empty_control()
    live.join(ctrl, "a", "Asha", time.time())
    live.start_call(ctrl, SDP, "Asha", time.time())
    store["tok"][0] = ctrl
    view = asyncio.run(fn("tok", _Req({"stage": "device_check"})))
    assert view["call"]["offer"] == SDP and view["ice_servers"] is not None
    assert live.normalize_reply(store["tok"][1])["lobby"]["stage"] == "device_check"
    asyncio.run(fn("tok", _Req({"stage": "rules"})))
    assert told == ["tok"]  # the TA hears once
    missing = asyncio.run(fn("nope", _Req({})))
    assert missing.status_code == 404


def test_the_new_routes_are_registered():
    paths = {getattr(r, "path", "") for r in main.app.routes}
    for path in ("/candidate/invite/{token}/presence", "/candidate/invite/{token}/live/answer"):
        assert path in paths


def test_the_rules_page_carries_the_template_switches():
    fmt = main._interview_format({}, {"weights": {"proctoring": {"headphones": "require", "roomScan": True}}})
    assert fmt["proctoring"]["headphones"] == "require" and fmt["proctoring"]["room_scan"] is True
    assert main._interview_format({}, None)["proctoring"]["enabled"] is False
