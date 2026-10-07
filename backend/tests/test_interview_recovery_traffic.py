"""The interview recovery worker must terminate, read little and run once.

7 Oct 2026 — production pulled ~8.7 GB/day from Postgres, ~90 % of it this
worker: `SELECT *` over the 100 oldest `interview_progress` rows (JSON
columns included) every 60 s in BOTH app processes, and the same 100 stuck
rows every time because a terminal row with no final report never left the
set (B2 in CLAUDE.md). These tests pin the new contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import main
from auth_db import (
    IST,
    INTERVIEW_RECOVERY_MAX_ATTEMPTS,
    get_interview_progress_by_id,
    init_auth_db,
    list_recoverable_interview_progress,
    record_interview_recovery_attempt,
    upsert_interview_progress,
)


def _stamp(now: datetime, minutes_ago: int) -> str:
    return (now - timedelta(minutes=minutes_ago)).isoformat()


def _row(db: Path, rid: str, *, status: str, report_status: str, minutes_ago: int, now: datetime, answers=None):
    upsert_interview_progress(
        db,
        {
            "interview_id": rid,
            "invite_token": f"tok-{rid}",
            "status": status,
            "questions": ["Q1", "Q2"],
            "answers": ["A1"] if answers is None else answers,
            "meta": {"interview_id": rid, "invite_token": f"tok-{rid}"},
            "report_status": report_status,
            "last_activity_at": _stamp(now, minutes_ago),
        },
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    f = tmp_path / "auth.db"
    init_auth_db(f)
    return f


def _ids(rows) -> set[str]:
    return {r["interview_id"] for r in rows}


def test_terminal_rows_with_a_final_report_are_never_selected(db: Path):
    now = datetime.now(IST)
    _row(db, "ready", status="completed", report_status="ready", minutes_ago=600, now=now)
    _row(db, "none", status="completed", report_status="no_report_needed", minutes_ago=600, now=now)
    _row(db, "failed", status="completed", report_status="recovery_failed", minutes_ago=600, now=now)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    rows = list_recoverable_interview_progress(db, now=now)
    assert _ids(rows) == {"stuck"}
    # the Python twin agrees
    assert main._should_recover_progress({"status": "completed", "report_status": "recovery_failed"}, now) is False
    assert main._should_recover_progress({"status": "completed", "report_status": "no_report_needed"}, now) is False


def test_the_listing_carries_no_json_columns(db: Path):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    (row,) = list_recoverable_interview_progress(db, now=now)
    for heavy in ("questions", "answers", "meta", "payload", "violations"):
        assert heavy not in row, heavy
    assert row["has_answers"] is True
    assert row["recovery_attempts"] == 0
    assert set(row) >= {"interview_id", "status", "report_status", "last_activity_at", "updated_at_ist", "created_at_ist"}


def test_a_row_is_selected_at_most_max_attempts_times(db: Path):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    seen = 0
    for _ in range(INTERVIEW_RECOVERY_MAX_ATTEMPTS + 3):
        # well past every backoff window, so only the cap can stop it
        later = now + timedelta(days=1 + seen)
        rows = list_recoverable_interview_progress(db, now=later)
        if not rows:
            break
        seen += 1
        record_interview_recovery_attempt(db, "stuck", succeeded=False, error="boom")
    assert seen == INTERVIEW_RECOVERY_MAX_ATTEMPTS
    full = get_interview_progress_by_id(db, "stuck")
    assert full["report_status"] == "recovery_failed"
    assert full["report_error"] == "boom"
    assert full["recovery_attempts"] == INTERVIEW_RECOVERY_MAX_ATTEMPTS
    assert list_recoverable_interview_progress(db, now=now + timedelta(days=30)) == []


def test_a_failed_attempt_waits_out_its_backoff_before_the_next(db: Path):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    record_interview_recovery_attempt(db, "stuck", succeeded=False, error="first")
    real_now = datetime.now(IST)
    assert list_recoverable_interview_progress(db, now=real_now + timedelta(minutes=2)) == []
    assert _ids(list_recoverable_interview_progress(db, now=real_now + timedelta(minutes=15))) == {"stuck"}
    record_interview_recovery_attempt(db, "stuck", succeeded=False, error="second")
    assert list_recoverable_interview_progress(db, now=real_now + timedelta(minutes=15)) == []
    assert _ids(list_recoverable_interview_progress(db, now=real_now + timedelta(minutes=90))) == {"stuck"}


def test_a_successful_attempt_closes_the_row_as_ready(db: Path):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    record_interview_recovery_attempt(db, "stuck", succeeded=True)
    assert get_interview_progress_by_id(db, "stuck")["report_status"] == "ready"
    assert list_recoverable_interview_progress(db, now=now + timedelta(days=1)) == []


def test_the_101st_oldest_row_is_picked_once_the_first_100_settle(db: Path):
    now = datetime.now(IST)
    for i in range(101):
        # i=0 is the oldest; i=100 the newest and therefore the 101st in order
        _row(db, f"r{i:03d}", status="completed", report_status="pending", minutes_ago=5000 - i, now=now)
    first = list_recoverable_interview_progress(db, limit=100, now=now)
    assert len(first) == 100 and "r100" not in _ids(first)
    for r in first:
        record_interview_recovery_attempt(db, r["interview_id"], succeeded=True)
    second = list_recoverable_interview_progress(db, limit=100, now=now)
    assert _ids(second) == {"r100"}


def test_the_sql_filter_mirrors_the_python_rule(db: Path):
    now = datetime.now(IST)
    _row(db, "gen_fresh", status="completed", report_status="generating", minutes_ago=2, now=now)
    _row(db, "gen_old", status="completed", report_status="generating", minutes_ago=20, now=now)
    _row(db, "live_fresh", status="in_progress", report_status="", minutes_ago=10, now=now)
    _row(db, "live_idle_answers", status="in_progress", report_status="", minutes_ago=40, now=now)
    _row(db, "live_idle_silent", status="in_progress", report_status="", minutes_ago=40, now=now, answers=[])
    _row(db, "live_stale_silent", status="started", report_status="", minutes_ago=70, now=now, answers=[])
    rows = list_recoverable_interview_progress(db, now=now)
    assert _ids(rows) == {"gen_old", "live_idle_answers", "live_stale_silent"}
    for r in rows:
        assert main._should_recover_progress(r, now) is True
    assert rows[0]["interview_id"] == "live_stale_silent"  # oldest activity first


def test_the_worker_loads_the_full_row_only_for_what_it_recovers(db: Path, monkeypatch):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    _row(db, "fresh", status="completed", report_status="generating", minutes_ago=1, now=now)
    monkeypatch.setattr(main, "AUTH_DB_TARGET", db)
    loaded: list[str] = []
    real_get = main.get_interview_progress_by_id

    def spy(target, rid):
        loaded.append(rid)
        return real_get(target, rid)

    monkeypatch.setattr(main, "get_interview_progress_by_id", spy)
    finalized: list[str] = []

    def fake_finalize(sess, reason, final_status):
        finalized.append(sess["meta"]["interview_id"])
        return {"report_ready": True}

    monkeypatch.setattr(main, "_finalize_interview_snapshot", fake_finalize)
    assert main._recover_interviews_once(limit=100) == 1
    assert loaded == ["stuck"] and finalized == ["stuck"]
    assert get_interview_progress_by_id(db, "stuck")["report_status"] == "ready"
    # a second pass finds nothing — the row left the set
    assert main._recover_interviews_once(limit=100) == 0
    assert loaded == ["stuck"]


def test_a_failing_finalize_is_counted_and_eventually_closed(db: Path, monkeypatch):
    now = datetime.now(IST)
    _row(db, "stuck", status="completed", report_status="pending", minutes_ago=600, now=now)
    monkeypatch.setattr(main, "AUTH_DB_TARGET", db)

    def boom(sess, reason, final_status):
        raise RuntimeError("evaluator down")

    monkeypatch.setattr(main, "_finalize_interview_snapshot", boom)
    assert main._recover_interviews_once(limit=100) == 0
    full = get_interview_progress_by_id(db, "stuck")
    assert full["recovery_attempts"] == 1 and full["report_status"] == "pending"
    # the backoff keeps the next pass off it
    assert main._recover_interviews_once(limit=100) == 0
    assert get_interview_progress_by_id(db, "stuck")["recovery_attempts"] == 1


def test_env_gate_disables_the_loop(monkeypatch):
    monkeypatch.setattr(main, "_RECOVERY_WORKER_STARTED", False)
    monkeypatch.setattr(main, "_is_production_env", lambda: False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    started: list[str] = []

    class FakeThread:
        def __init__(self, *a, **k):
            started.append(k.get("name") or "")

        def start(self):
            pass

    monkeypatch.setattr(main.threading, "Thread", FakeThread)
    monkeypatch.setenv("INTERVIEW_RECOVERY_WORKER", "false")
    main._start_interview_recovery_worker()
    assert started == [] and main._RECOVERY_WORKER_STARTED is False
    monkeypatch.setenv("INTERVIEW_RECOVERY_WORKER", "true")
    main._start_interview_recovery_worker()
    assert started == ["interview-recovery"] and main._RECOVERY_WORKER_STARTED is True


def test_default_interval_is_five_minutes(monkeypatch):
    monkeypatch.delenv("INTERVIEW_RECOVERY_INTERVAL_SEC", raising=False)
    assert main._recovery_worker_interval_sec() == 300
    monkeypatch.setenv("INTERVIEW_RECOVERY_INTERVAL_SEC", "5")
    assert main._recovery_worker_interval_sec() == 30
