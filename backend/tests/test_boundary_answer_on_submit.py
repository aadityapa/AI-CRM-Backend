"""Boundary answer capture when interview ends on timer."""

from main import _append_pending_answer_on_submit, _record_boundary_question_meta


def _open_session():
    return {
        "completed": False,
        "current": 2,
        "questions": ["Q1", "Q2", "Q3"],
        "answers": ["Answer one is long enough.", "Answer two is long enough."],
        "meta": {},
    }


def test_append_pending_answer_on_submit_adds_last_answer():
    s = _open_session()
    appended = _append_pending_answer_on_submit(s, "Linux kernel is the core component of the operating system.")
    assert appended is True
    assert len(s["answers"]) == 3
    assert "Linux kernel" in s["answers"][-1]


def test_boundary_meta_tags_timeout_auto_save():
    s = _open_session()
    _append_pending_answer_on_submit(s, "Linux kernel is the core component of the operating system.")
    _record_boundary_question_meta(
        s,
        time_expired=True,
        finalize_via="timer",
        auto_saved=True,
        pending_appended=True,
    )
    meta = s["meta"]
    assert meta.get("auto_submitted_on_timeout") is True
    assert meta.get("boundary_auto_saved") is True
    assert meta.get("boundary_saved_via") == "timer"


def test_append_pending_skips_empty():
    s = _open_session()
    assert _append_pending_answer_on_submit(s, "") is False
    assert len(s["answers"]) == 2
