"""Verify HR report records exclude the warmup Q/A pair (Issue 2, May 2026)."""

from __future__ import annotations

from hr.service import build_report_record, build_submitted_record
from utils.warmup import WARMUP_QUESTION_TEXT


def _make_session():
    return {
        "meta": {
            "interview_id": "iv-test-1",
            "created_at": "2026-05-19T10:00:00+00:00",
            "created_at_ist": "2026-05-19T15:30:00+05:30",
            "created_date_ist": "2026-05-19",
            "created_time_ist": "15:30:00",
            "candidate_profile": {"name": "Alice", "email": "alice@example.com"},
            "difficulty": "medium",
            "model": "gpt-4o-mini",
            "jd_skills": ["python", "fastapi"],
            "job_id": "job-1",
            "job_title": "Backend Engineer",
            "warmup_indices": [0],
        },
        "questions": [WARMUP_QUESTION_TEXT, "Explain async/await.", "What is FastAPI?"],
        "answers": ["Hi, I'm Alice.", "async/await ...", "FastAPI is ..."],
        "submitted": True,
    }


def _ist_stub():
    return {"ist_iso": "2026-05-19T15:35:00+05:30", "ist_date": "2026-05-19", "ist_time": "15:35:00"}


def test_build_submitted_record_strips_warmup():
    rec = build_submitted_record(_make_session(), _ist_stub())
    assert rec["questions"] == ["Explain async/await.", "What is FastAPI?"]
    assert rec["answers"] == ["async/await ...", "FastAPI is ..."]
    # Warmup text must not appear anywhere in the saved record body.
    assert WARMUP_QUESTION_TEXT not in rec["questions"]


def test_build_report_record_strips_warmup():
    rec = build_report_record(_make_session(), {"summary": "fine"}, _ist_stub())
    assert rec["questions"] == ["Explain async/await.", "What is FastAPI?"]
    assert rec["answers"] == ["async/await ...", "FastAPI is ..."]
    # The report body is carried through unchanged, plus the communication flag
    # the record now stamps (so a reader can tell "scored 0 for communication"
    # apart from "communication was never assessed"). Assert on what the caller
    # supplied rather than exact equality, so the record can keep growing.
    assert rec["report"]["summary"] == "fine"
    assert rec["report"]["communication_required"] is True


def test_legacy_session_without_warmup_unchanged():
    """Records produced before the warmup feature must still round-trip."""
    legacy = _make_session()
    legacy["meta"].pop("warmup_indices")
    legacy["questions"] = ["Q1", "Q2"]
    legacy["answers"] = ["A1", "A2"]
    rec = build_report_record(legacy, {"summary": "fine"}, _ist_stub())
    assert rec["questions"] == ["Q1", "Q2"]
    assert rec["answers"] == ["A1", "A2"]
