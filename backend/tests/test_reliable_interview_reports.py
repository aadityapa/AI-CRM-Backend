from __future__ import annotations

from pathlib import Path

from auth_db import (
    get_interview_progress_by_invite,
    init_auth_db,
    list_recoverable_interview_progress,
    mark_interview_progress_report_status,
    upsert_interview_progress,
)


def test_interview_progress_round_trip_and_report_status(tmp_path: Path):
    db_file = tmp_path / "auth_progress.db"
    init_auth_db(db_file)

    upsert_interview_progress(
        db_file,
        {
            "interview_id": "iv-progress-1",
            "invite_token": "tok-progress",
            "status": "in_progress",
            "current_index": 1,
            "questions": ["Q1"],
            "answers": ["A1"],
            "meta": {"interview_id": "iv-progress-1", "invite_token": "tok-progress"},
            "last_activity_at": "2026-05-26T16:00:00+05:30",
        },
    )

    row = get_interview_progress_by_invite(db_file, "tok-progress")
    assert row is not None
    assert row["interview_id"] == "iv-progress-1"
    assert row["questions"] == ["Q1"]
    assert row["answers"] == ["A1"]

    recoverable = list_recoverable_interview_progress(db_file)
    assert any(r["interview_id"] == "iv-progress-1" for r in recoverable)

    mark_interview_progress_report_status(
        db_file,
        "iv-progress-1",
        report_status="ready",
        status="completed",
        finalized_at="2026-05-26T16:01:00+05:30",
    )
    row = get_interview_progress_by_invite(db_file, "tok-progress")
    assert row["report_status"] == "ready"
    assert row["status"] == "completed"


def test_finalize_fallback_persists_report_when_evaluation_fails(monkeypatch):
    import main

    persisted = {}

    def fail_eval(_session):
        raise RuntimeError("model unavailable")

    def capture_record(_target, record):
        persisted["record"] = record

    monkeypatch.setattr(main, "get_interview_record_payload", lambda *a, **k: None)
    monkeypatch.setattr(main, "_evaluate_and_store_report", fail_eval)
    monkeypatch.setattr(main, "_persist_hr_record_mirror", lambda *a, **k: None)
    monkeypatch.setattr(main, "upsert_interview_record_snapshot", capture_record)
    monkeypatch.setattr(main, "append_from_evaluation", lambda *a, **k: None)
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "invalidate_hr_dashboard_cache", lambda: None)

    session = {
        "meta": {
            "interview_id": "iv-fallback-1",
            "candidate_profile": {"name": "Candidate", "email": "c@example.com"},
            "jd_skills": ["python"],
        },
        "questions": ["How do you debug flaky tests?"],
        "answers": ["I inspect logs and isolate nondeterminism."],
        "current": 1,
    }

    out = main._finalize_interview_snapshot(
        session,
        reason="candidate_ended_early",
        final_status="partially_completed",
    )

    assert out["report_ready"] is True
    assert persisted["record"]["report"]
    assert persisted["record"]["report_status"] == "fallback"
    assert persisted["record"]["final_status"] == "partially_completed"


def test_fast_finalize_persists_report_without_model_call(monkeypatch):
    import main

    persisted = {}

    def fail_if_called(_session):
        raise AssertionError("full evaluation should not block fast finalize")

    def capture_record(_target, record):
        persisted["record"] = record

    monkeypatch.setattr(main, "get_interview_record_payload", lambda *a, **k: None)
    monkeypatch.setattr(main, "_evaluate_and_store_report", fail_if_called)
    monkeypatch.setattr(main, "_persist_hr_record_mirror", lambda *a, **k: None)
    monkeypatch.setattr(main, "upsert_interview_record_snapshot", capture_record)
    monkeypatch.setattr(main, "_persist_interview_progress", lambda *a, **k: None)
    monkeypatch.setattr(main, "invalidate_hr_dashboard_cache", lambda: None)

    session = {
        "meta": {
            "interview_id": "iv-fast-1",
            "candidate_profile": {"name": "Candidate", "email": "c@example.com"},
            "jd_skills": ["python"],
        },
        "questions": ["Q1?"],
        "answers": ["skip"],
        "current": 1,
    }

    out = main._persist_fast_final_report(
        session,
        reason="candidate_ended_early",
        final_status="partially_completed",
    )

    assert out["report_ready"] is False
    assert out["report_status"] == "generating"
    assert out["fast_finalize"] is True
    assert persisted["record"]["report"]
    assert persisted["record"]["report_status"] == "ready_pending_ai"
