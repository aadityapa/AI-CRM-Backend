"""Tests for candidate /next payload when the interview pool is exhausted."""

from candidate.service import next_question_payload


def test_next_question_marks_completed_when_no_questions_left():
    """Backward compatibility: last index still returns a stable completion shape."""
    session = {
        "current": 2,
        "questions": ["Q1", "Q2"],
        "answers": ["A1"],
        "completed": False,
        "meta": {"jd_skills": ["python"], "show_spoken_text": True, "mic_always_on": False},
    }
    out = next_question_payload(session)
    assert out.get("message") == "Interview completed"
    assert session.get("completed") is True


def test_next_question_default_mic_always_on_is_false():
    """Feature 6 (May 2026): default `mic_always_on` is always False; the
    payload key is kept for legacy clients but the new candidate UI ignores it.
    """
    session = {
        "current": 0,
        "questions": ["Q1"],
        "answers": [],
        "completed": False,
        "meta": {"jd_skills": ["python"], "show_spoken_text": True},  # no explicit mic_always_on
    }
    out = next_question_payload(session)
    assert out.get("question") == "Q1"
    assert out.get("mic_always_on") is False


def test_next_question_preserves_legacy_micalwayson_flag():
    """If an older DB record still has mic_always_on=True the API returns it
    untouched (backward compatibility); the new candidate.js does not act on it.
    """
    session = {
        "current": 0,
        "questions": ["Q1"],
        "answers": [],
        "completed": False,
        "meta": {"jd_skills": ["python"], "show_spoken_text": True, "mic_always_on": True},
    }
    out = next_question_payload(session)
    assert out.get("mic_always_on") is True
