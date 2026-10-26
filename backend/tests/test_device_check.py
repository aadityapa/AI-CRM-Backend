"""24 Oct 2026 — the Device Check reports why a screen share failed, and the
Integrity timeline shows it (services/device_check.py)."""
from __future__ import annotations

from services import device_check as dc
from services import interview_integrity as ii


def test_clean_event_keeps_known_checks_and_short_codes_only():
    ev = dc.clean_event({"check": "screen_share", "ok": False, "reason": "denied<script>", "attempt": 2},
                        user_agent="UA", ip="1.2.3.4", at="2026-10-24T10:00:00+05:30")
    assert ev["reason"] == "deniedscript"  # markup stripped
    assert ev["attempt"] == 2 and ev["ok"] is False and ev["ip"] == "1.2.3.4"
    assert dc.clean_event({"check": "anything"}) is None
    assert dc.clean_event("nope") is None  # type: ignore[arg-type]
    ok = dc.clean_event({"check": "screen_share", "ok": True, "reason": "x", "surface": "monitor"})
    assert ok["reason"] == "" and ok["surface"] == "monitor"


def test_append_keeps_only_the_newest():
    log: list = []
    for i in range(dc.MAX_EVENTS + 5):
        log = dc.append(log, {"check": "screen_share", "attempt": i})
    assert len(log) == dc.MAX_EVENTS and log[-1]["attempt"] == dc.MAX_EVENTS + 4


def test_timeline_events_say_why_in_words():
    log = [
        {"check": "screen_share", "ok": False, "reason": "denied", "attempt": 1, "timestamp": "t1"},
        {"check": "screen_share", "ok": False, "reason": "not_entire_screen", "surface": "browser", "attempt": 2},
        {"check": "screen_share", "ok": True, "surface": "monitor", "attempt": 3},
    ]
    events = dc.timeline_events(log)
    assert [e["type"] for e in events] == ["screen_share_failed", "screen_share_failed", "screen_share_ok"]
    assert "cancelled" in events[0]["details"] and "attempt 1" in events[0]["details"]
    assert "a browser tab" in events[1]["details"]
    assert "entire screen" in events[2]["details"]
    assert dc.failure_count(log) == 2


def test_the_events_are_informational():
    for t in ("screen_share_failed", "screen_share_ok"):
        assert t in ii.EVENT_TYPES
        assert t not in ii.STRIKE_TYPES
        assert ii.EVENT_TYPES[t][2] == 0  # no penalty


def test_the_log_round_trips_on_sqlite(tmp_path):
    import sqlite3
    import auth_db

    db = tmp_path / "a.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE interview_schedule (id INTEGER PRIMARY KEY, invite_token TEXT UNIQUE)")
        auth_db._ensure_schedule_security_columns_sqlite(conn)
        conn.execute("INSERT INTO interview_schedule (invite_token) VALUES ('tok')")
        conn.commit()
    assert auth_db.get_device_check_log(db, "tok") == []
    auth_db.set_device_check_log(db, "tok", [{"check": "screen_share", "ok": False}])
    assert auth_db.get_device_check_log(db, "tok") == [{"check": "screen_share", "ok": False}]
    assert auth_db.get_device_check_log(db, "missing") == []
