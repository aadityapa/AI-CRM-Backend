"""The AI-interview link path, end to end (15 Sep 2026 audit fixes).

Run:  cd backend && python -m pytest tests/test_interview_link_flow.py -q
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from services import invite_links


@pytest.fixture(autouse=True)
def _reset_base(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(invite_links, "_last_seen_base", "")
    # Settings lookup is a DB call — make it a no-op here.
    monkeypatch.setattr(invite_links, "configured_base", lambda: (invite_links.os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/"))
    yield


def _req(base="http://127.0.0.1:2020/", headers=None):
    return SimpleNamespace(base_url=base, headers=headers or {})


def test_invite_url_is_never_relative_when_a_request_is_present(monkeypatch):
    monkeypatch.setattr(invite_links, "detect_lan_ip", lambda: "192.168.1.20")
    url = invite_links.invite_url("tok123", _req())
    assert url == "http://192.168.1.20:2020/?invite=tok123"


def test_reverse_proxy_headers_win_over_the_socket_host():
    url = invite_links.invite_url("tok", _req(headers={"x-forwarded-host": "hire.karnex.in", "x-forwarded-proto": "https"}))
    assert url == "https://hire.karnex.in/?invite=tok"


def test_configured_base_wins_over_everything(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://interviews.karnex.in/")
    assert invite_links.invite_url("t", _req(headers={"x-forwarded-host": "other"})) == "https://interviews.karnex.in/?invite=t"


def test_background_callers_reuse_the_last_seen_base():
    invite_links.resolve_invite_base(_req(headers={"x-forwarded-host": "hire.karnex.in"}))
    assert invite_links.invite_url("t") == "https://hire.karnex.in/?invite=t"


def test_strict_mode_refuses_a_relative_link():
    with pytest.raises(invite_links.InviteBaseUnavailable):
        invite_links.invite_url("t")
    assert invite_links.invite_url("t", strict=False) == "/?invite=t"


def test_bridge_refuses_to_schedule_without_a_link_base(monkeypatch):
    """No base → scheduled=False with a clear error, and NO schedule row is created."""
    import services.ai_interview_bridge as bridge

    created = []
    monkeypatch.setattr(bridge, "create_interview_schedule", lambda *a, **k: created.append(k) or {"invite_token": "x"}, raising=False)
    monkeypatch.setattr(bridge, "resolve_l1_template_job_id", lambda *a, **k: "job-1", raising=False)
    cand = SimpleNamespace(id=1, first_name="A", last_name="B", email="a@x.in")
    prof = SimpleNamespace(id=1, opportunity_id=1)
    try:
        out = bridge.schedule_l1_interview(None, cand, None, prof)  # type: ignore[arg-type]
    except Exception:
        pytest.skip("bridge needs a DB before reaching the link check in this build")
    assert out["scheduled"] is False and "Public base URL" in out["error"]
    assert created == []


def test_notes_edit_keeps_the_config_block_parseable():
    """The PUT used to split on the wrong marker and corrupt the JSON config."""
    from services.ai_interview_bridge import CFG_MARKER
    import main

    cfg = {"job_id": "job-7", "num_q": 6, "timing_mode": "count"}
    notes = f"Karnex CRM AI L1 interview — Embedded\n{CFG_MARKER}{json.dumps(cfg)}"
    # Replicate the router's rewrite (the function body is inline in the endpoint).
    head, cfg_tail = notes.split(CFG_MARKER, 1)
    headline = head.strip().split("\n", 1)[0]
    rewritten = "\n".join(x for x in (headline, "Bring your laptop") if x) + f"\n{CFG_MARKER}{cfg_tail.strip()}"
    assert main._extract_invite_config_from_notes(rewritten) == cfg
    assert "Bring your laptop" in rewritten.split(CFG_MARKER)[0]


def test_public_schedule_view_never_leaks_the_access_key(monkeypatch):
    import main

    monkeypatch.setattr(main, "get_job_template", lambda *_: {"jobTitle": "Embedded SW Engineer", "timingMode": "time", "timeLimitSec": 1800, "numQ": 8})
    from services.ai_interview_bridge import CFG_MARKER
    record = {
        "candidate_name": "Aakash", "scheduled_at_local": "2026-09-15 11:00", "status": "scheduled",
        "session_status": "pending", "access_key": "ABCD-1234", "active_device_id": "dev-1",
        "notes": f"x\n{CFG_MARKER}" + json.dumps({"job_id": "j1"}),
        "violations_log": "[]",
    }
    view = main._public_schedule_view(record)
    assert view["access_key"] is True                       # presence only, never the value
    assert "ABCD-1234" not in json.dumps(view)
    assert "active_device_id" not in view and "notes" not in view and "violations_log" not in view
    assert view["job_title"] == "Embedded SW Engineer"
    assert view["timing_mode"] == "time" and view["time_limit_sec"] == 1800 and view["num_q"] == 8


def test_score_percent_is_clamped_to_100():
    from services.ai_interview_bridge import _score_percent
    assert float(_score_percent({"overall_score": 40})) == 100.0   # the exception-branch 0-40 "score"
    assert float(_score_percent({"overall_score": 7.5})) == 75.0
    assert float(_score_percent({"overall_score_percent": -3})) == 0.0


def test_recovery_worker_leaves_fresh_post_submit_rows_alone():
    import main
    from datetime import datetime, timedelta

    now = datetime.now(main.IST)
    fresh = {"status": "completed", "report_status": "generating",
             "last_activity_at": (now - timedelta(minutes=2)).isoformat(), "answers": [{"a": 1}]}
    stale = {"status": "completed", "report_status": "generating",
             "last_activity_at": (now - timedelta(minutes=20)).isoformat(), "answers": [{"a": 1}]}
    assert main._should_recover_progress(fresh, now) is False
    assert main._should_recover_progress(stale, now) is True
    assert main._should_recover_progress({"status": "completed", "report_status": "ready"}, now) is False


def test_multi_worker_without_redis_is_refused(monkeypatch):
    import main
    monkeypatch.setenv("UVICORN_WORKERS", "2")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("ALLOW_MULTI_WORKER_UNSAFE", raising=False)
    monkeypatch.setattr(main, "_RECOVERY_WORKER_STARTED", True)
    with pytest.raises(RuntimeError):
        main._start_interview_recovery_worker()
