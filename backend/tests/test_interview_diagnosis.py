""""What happened" for one AI interview + the recovery worker never closes a
session that was only prepared (5 Nov 2026).

Reported: an interview read COMPLETED with "Started —", finished at 01:54 pm
for a 03:00 pm slot, "Failed 0 %", no recording. The candidate had opened the
link at 12:54 pm; the lookup pre-built the session; 60 minutes later the
recovery worker closed it as "abandoned" and the link went dead.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import main
from auth_db import (
    IST,
    get_interview_progress_by_id,
    init_auth_db,
    interview_progress_summary,
    upsert_interview_progress,
)
from services.interview_diagnosis import diagnose, parse_when, session_never_started

NOW = datetime(2026, 11, 4, 14, 30, tzinfo=IST)


def _sched(**kw):
    base = {"session_status": "completed", "scheduled_at_local": "2026-11-04 15:00",
            "verified_at": "", "interview_started_at": "", "interview_completed_at": "2026-11-04T13:54:10+05:30",
            "login_attempts": 0, "active_device_id": "", "recording_status": ""}
    base.update(kw)
    return base


def _prog(**kw):
    base = {"interview_id": "iv1", "status": "abandoned", "answers_count": 0, "questions_count": 8,
            "created_at_ist": "2026-11-04T12:53:40+05:30", "finalized_at": "2026-11-04T13:54:10+05:30",
            "last_activity_at": "2026-11-04T12:53:40+05:30", "report_status": "ready", "recovery_attempts": 1,
            "meta": {"finalization_reason": "abandoned_without_answers", "final_status": "abandoned"}}
    base.update(kw)
    return base


DEVICE_OK = [{"check": "screen_share", "ok": True, "surface": "monitor", "attempt": 1,
              "timestamp": "2026-11-04T12:54:30+05:30"}]


def test_the_reported_case_reads_closed_before_it_started():
    d = diagnose(_sched(), _prog(), device_log=DEVICE_OK,
                 link={"ai_result": "Failed", "ai_score": 0, "not_attempted": True}, now=NOW)
    assert d["headline"] == "Closed by the system before the candidate started"
    assert d["tone"] == "bad"
    text = " ".join(d["explanation"])
    assert "12:53 PM" in text and "before the slot" in text and "recovery job" in text
    assert any("Reschedule AI L1" in s for s in d["next_steps"])
    assert any("BEFORE the scheduled time" in f for f in d["flags"])
    steps = {s["key"]: s for s in d["steps"]}
    assert steps["device"]["state"] == "done"
    assert steps["signed_in"]["state"] == "missing" and steps["first_question"]["state"] == "missing"
    assert steps["report"]["detail"].startswith("Not attempted")


def test_a_normal_interview_reads_completed():
    d = diagnose(_sched(interview_started_at="2026-11-04T15:02:00+05:30", recording_status="ready",
                        interview_completed_at="2026-11-04T15:31:00+05:30"),
                 _prog(status="completed", answers_count=8, meta={"finalization_reason": "candidate_submitted",
                                                                  "interview_started_epoch": 1793784720}),
                 link={"ai_result": "Passed", "ai_score": 72}, now=NOW + timedelta(hours=2))
    assert d["headline"] == "Completed" and d["tone"] == "ok"
    assert {s["key"]: s["state"] for s in d["steps"]}["recording"] == "done"


def test_device_check_failures_before_sign_in():
    log = [{"check": "screen_share", "ok": False, "reason": "not_entire_screen", "surface": "browser", "attempt": 1,
            "timestamp": "2026-11-04T14:10:00+05:30"}]
    d = diagnose(_sched(session_status="pending", interview_completed_at=""), _prog(status="started", meta={},
                 finalized_at=""), device_log=log, now=NOW)
    assert d["headline"] == "Stuck at the Device Check"
    assert "a browser tab" in {s["key"]: s for s in d["steps"]}["device"]["detail"]


def test_parse_when_reads_every_shape():
    assert parse_when("2026-11-04 15:00").hour == 15
    assert parse_when("2026-11-04T09:30:00Z").hour == 15
    assert parse_when(1793784720) is not None
    assert parse_when("") is None and parse_when("junk") is None


def test_session_never_started_rule():
    sess = {"answers": [], "meta": {}}
    assert session_never_started(sess, {"session_status": "pending"}) is True
    assert session_never_started(sess, {"session_status": "verified"}) is True
    assert session_never_started(sess, {"session_status": "active"}) is False
    assert session_never_started(sess, {"session_status": "pending", "interview_started_at": "x"}) is False
    assert session_never_started({"answers": ["a"], "meta": {}}, {"session_status": "pending"}) is False
    assert session_never_started({"answers": [], "meta": {"interview_started_epoch": 1}}, {"session_status": "pending"}) is False
    assert session_never_started(sess, None) is False  # no schedule → keep the old behaviour


@pytest.fixture
def db(tmp_path: Path) -> Path:
    f = tmp_path / "auth.db"
    init_auth_db(f)
    return f


def test_the_recovery_worker_leaves_a_prepared_session_open(db: Path, monkeypatch):
    now = datetime.now(IST)
    upsert_interview_progress(db, {
        "interview_id": "prep", "invite_token": "tok-prep", "status": "started",
        "questions": ["Q1"], "answers": [],
        "meta": {"interview_id": "prep", "invite_token": "tok-prep"},
        "last_activity_at": (now - timedelta(minutes=70)).isoformat(),
    })
    monkeypatch.setattr(main, "AUTH_DB_TARGET", db)
    monkeypatch.setattr(main, "get_schedule_by_token",
                        lambda target, tok: {"session_status": "pending", "interview_started_at": ""})
    finalized: list[str] = []
    monkeypatch.setattr(main, "_finalize_interview_snapshot",
                        lambda sess, reason, final_status: finalized.append(reason) or {"report_ready": True})
    assert main._recover_interviews_once(limit=10) == 0
    assert finalized == []
    full = get_interview_progress_by_id(db, "prep")
    assert full["report_status"] == "no_report_needed" and full["status"] == "started"
    # the next pass does not pick it up again
    assert main._recover_interviews_once(limit=10) == 0 and finalized == []


def test_a_signed_in_session_that_went_silent_is_still_closed(db: Path, monkeypatch):
    now = datetime.now(IST)
    upsert_interview_progress(db, {
        "interview_id": "gone", "invite_token": "tok-gone", "status": "in_progress",
        "questions": ["Q1"], "answers": [],
        "meta": {"interview_id": "gone", "invite_token": "tok-gone", "interview_started_epoch": 1},
        "last_activity_at": (now - timedelta(minutes=70)).isoformat(),
    })
    monkeypatch.setattr(main, "AUTH_DB_TARGET", db)
    monkeypatch.setattr(main, "get_schedule_by_token",
                        lambda target, tok: {"session_status": "active", "interview_started_at": "x"})
    finalized: list[str] = []
    monkeypatch.setattr(main, "_finalize_interview_snapshot",
                        lambda sess, reason, final_status: finalized.append(reason) or {"report_ready": True})
    assert main._recover_interviews_once(limit=10) == 1
    assert finalized == ["abandoned_without_answers"]


def test_the_slim_summary_carries_counts_and_meta_without_json(db: Path):
    upsert_interview_progress(db, {
        "interview_id": "s1", "invite_token": "tok-s1", "status": "completed",
        "questions": ["Q1", "Q2", "Q3"], "answers": ["A1"],
        "meta": {"interview_id": "s1", "invite_token": "tok-s1", "finalization_reason": "candidate_submitted",
                 "big": ["x"] * 50},
    })
    s = interview_progress_summary(db, "tok-s1")
    assert s["answers_count"] == 1 and s["questions_count"] == 3
    assert s["meta"] == {"finalization_reason": "candidate_submitted"}
    for heavy in ("questions", "answers", "payload", "violations"):
        assert heavy not in s
    assert interview_progress_summary(db, "nope") is None


def test_the_detail_route_returns_the_diagnosis():
    import inspect
    src = inspect.getsource(main.interview_integrity_detail)
    assert '"diagnosis": diagnosis' in src and "interview_progress_summary(AUTH_DB_TARGET, token)" in src
