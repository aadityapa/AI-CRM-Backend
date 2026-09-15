"""Interview integrity taxonomy, scoring and export (15 Sep 2026).

Run:  cd backend && python -m pytest tests/test_interview_integrity.py -q
"""
from __future__ import annotations

from services import interview_integrity as ii


def _ev(t, ts="2026-09-15T11:00:00+05:30", **kw):
    return {"type": t, "timestamp": ts, **kw}


def test_real_tab_switch_events_now_count_as_strikes():
    # The candidate page sends these on a tab change — they used to be ignored.
    evs = [_ev("visibility_hidden", "t1"), _ev("window_blur", "t2"), _ev("fullscreen_exit", "t3")]
    assert ii.count_strikes(evs) == 3
    assert {"visibility_hidden", "window_blur", "alt_tab", "clipboard", "devtools", "multiple_faces"} <= ii.STRIKE_TYPES
    # Informational types never strike.
    assert not ({"key_escape", "no_face", "context_menu", "termination"} & ii.STRIKE_TYPES)


def test_dedupe_collapses_the_proctor_replay_bug():
    evs = [_ev("proctor_tabSwitch", "a"), _ev("proctor_tabSwitch", "a"), _ev("proctor_tabSwitch", "b"), "junk"]
    assert [e["timestamp"] for e in ii.dedupe_events(evs)] == ["a", "b"]


def test_score_and_review_flag():
    clean = ii.summarise([])
    assert clean["integrity_score"] == 100 and clean["needs_review"] is False
    minor = ii.summarise([_ev("key_escape", "1"), _ev("visibility_hidden", "2")])
    assert minor["integrity_score"] == 90 and minor["needs_review"] is False
    bad = ii.summarise([_ev("multiple_faces", "1"), _ev("devtools", "2"), _ev("visibility_hidden", "3")])
    assert bad["integrity_score"] == 65 and bad["needs_review"] is True
    assert bad["by_family"]["face"] == 1 and bad["by_family"]["devtools"] == 1 and bad["by_family"]["tab"] == 1
    term = ii.summarise([_ev("visibility_hidden", "1")], session_status="terminated")
    assert term["integrity_score"] <= ii.TERMINATED_CAP and term["needs_review"] is True


def test_shared_device_flags_cross_candidates():
    rows = [
        {"invite_token": "t1", "candidate_email": "a@x.in", "active_device_id": "dev-1", "events": []},
        {"invite_token": "t2", "candidate_email": "b@x.in", "active_device_id": "dev-1", "events": []},
        {"invite_token": "t3", "candidate_email": "c@x.in", "active_device_id": "dev-9",
         "events": [_ev("window_blur", ip="10.0.0.5")]},
        {"invite_token": "t4", "candidate_email": "d@x.in", "active_device_id": "",
         "events": [_ev("window_blur", ip="10.0.0.5")]},
        {"invite_token": "t5", "candidate_email": "a@x.in", "active_device_id": "dev-1", "events": []},  # same person twice
    ]
    flags = ii.shared_device_flags(rows)
    assert flags["t1"] == ["b@x.in"] and flags["t2"] == ["a@x.in"]
    assert flags["t3"] == ["d@x.in"] and flags["t4"] == ["c@x.in"]
    assert "t5" in flags and flags["t5"] == ["b@x.in"]


def test_csv_export_neutralises_formulas_and_keeps_columns():
    out = ii.rows_to_csv([{
        "candidate_name": "=SUM(1)", "candidate_email": "a@x.in", "customer_name": "Uno Minda",
        "requirement_title": "Embedded", "scheduled_at": "2026-09-15 11:00", "session_status": "completed",
        "integrity_score": 82, "strikes": 1, "by_family": {"tab": 1}, "reason": "",
        "interview_started_at": "", "interview_completed_at": "",
    }])
    lines = out.splitlines()
    assert lines[0].split(",") == ii.CSV_COLUMNS
    assert lines[1].startswith("'=SUM(1)")


def test_export_route_is_declared_before_the_token_route():
    """/interview/integrity-logs/export must not be captured by /{invite_token}."""
    import main
    paths = [getattr(r, "path", "") for r in main.app.routes]
    assert paths.index("/interview/integrity-logs/export") < paths.index("/interview/integrity-logs/{invite_token}")
