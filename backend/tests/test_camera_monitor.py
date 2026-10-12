"""Camera monitor (13 Oct 2026): a second face is a strike, looking away is a
SOFT warning counted on its own, and the 10th look-away ends the interview
(user decision). The page's CSP lets the monitor's WebAssembly run."""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest

from services import interview_integrity as ii

main = importlib.import_module("main")


def test_gaze_is_soft_and_counted_on_its_own():
    assert "gaze_away" in ii.EVENT_TYPES and "gaze_away" not in ii.STRIKE_TYPES
    assert "multiple_faces" in ii.STRIKE_TYPES
    assert "camera_monitor_unavailable" not in ii.STRIKE_TYPES
    events = [{"type": "gaze_away", "timestamp": f"t{i}"} for i in range(4)] + [{"type": "tab_switch", "timestamp": "t9"}]
    assert ii.count_gaze(events) == 4 and ii.count_strikes(events) == 1
    summary = ii.summarise(events)
    assert summary["by_family"]["gaze"] == 4 and summary["gaze_warnings"] == 4
    assert "looked_away" in ii.CSV_COLUMNS


def test_gaze_limit_defaults_to_ten_and_can_be_turned_off(monkeypatch):
    monkeypatch.delenv("INTERVIEW_GAZE_WARNING_LIMIT", raising=False)
    assert ii.gaze_limit() == 10
    monkeypatch.setenv("INTERVIEW_GAZE_WARNING_LIMIT", "0")
    assert ii.gaze_limit() == 0
    assert not ii.gaze_limit_reached([{"type": "gaze_away"}] * 50)
    monkeypatch.setenv("INTERVIEW_GAZE_WARNING_LIMIT", "nonsense")
    assert ii.gaze_limit() == 10


@pytest.fixture
def violation_session(monkeypatch):
    s = {"meta": {"invite_token": "gaze-tok", "violations": []}, "questions": ["q"], "answers": [], "current": 0}
    main.sessions["inv:gaze-tok"] = s
    monkeypatch.setattr(main, "_require_user",
                        lambda request, roles: ({"invite_token": "gaze-tok", "sub": "invite-gaze", "role": "candidate"}, None))
    monkeypatch.setattr(main, "_live_pause_payload", lambda *a, **k: None)
    monkeypatch.setattr(main, "update_schedule_field", lambda *a, **k: None)
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "_notify_ta_of_integrity", lambda *a, **k: None)
    monkeypatch.delenv("INTERVIEW_GAZE_WARNING_LIMIT", raising=False)
    yield s
    main.sessions.pop("inv:gaze-tok", None)


def _post(vtype: str) -> dict:
    req = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    return asyncio.run(main.interview_violation(req, violation_type=vtype, details="looking down for 3 s",
                                                current_question="", fullscreen_status="",
                                                browser_visibility="", window_focus=""))


def test_nine_look_aways_warn_and_the_tenth_ends_the_interview(violation_session):
    for i in range(1, 10):
        out = _post("gaze_away")
        assert out["auto_terminated"] is False
        assert (out["gaze_count"], out["gaze_limit"]) == (i, 10)
        assert out["violation_count"] == 0  # never a strike
    out = _post("gaze_away")
    assert out["auto_terminated"] is True
    meta = violation_session["meta"]
    assert meta["termination_reason"] == "Looked away from the screen too often"
    assert meta["violations"][-1]["type"] == "termination"


def test_a_second_face_is_still_a_strike(violation_session):
    for _ in range(3):
        assert _post("multiple_faces")["auto_terminated"] is False
    out = _post("multiple_faces")
    assert out["auto_terminated"] is True
    assert violation_session["meta"]["termination_reason"] == "Repeated interview policy violations"


def test_the_csp_lets_webassembly_and_the_model_load():
    """MediaPipe's WebAssembly cannot compile without 'wasm-unsafe-eval' — the
    reason a second face was never detected before 13 Oct 2026."""
    from pathlib import Path
    src = Path(main.__file__).read_text(encoding="utf-8")
    assert "'wasm-unsafe-eval'" in src
    assert "https://storage.googleapis.com" in src
    import mimetypes
    assert mimetypes.guess_type("x.wasm")[0] == "application/wasm"
    assert mimetypes.guess_type("x.mjs")[0] == "text/javascript"


def test_rules_page_names_the_look_away_limit():
    fmt = main._interview_format({"timing_mode": "count", "num_q": 5}, {"timingMode": "count", "numQ": 5})
    assert fmt["gaze_limit"] == ii.gaze_limit()
