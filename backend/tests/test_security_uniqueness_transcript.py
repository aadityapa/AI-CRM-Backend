from __future__ import annotations

from candidate.service import next_question_payload
from main import MAX_WARNINGS, _proctor_score
from utils.question_uniqueness import make_question_session_seed, prepare_unique_question_sequence, remember_asked_question


def test_proctor_terminates_on_fourth_violation():
    viol = {"tabSwitch": MAX_WARNINGS, "fullscreenExit": 0, "keyboardBlocked": 0, "cameraOff": 0, "inactivity": 0}
    _, status_three, _, _ = _proctor_score(viol)
    assert status_three != "FAIL"

    viol["tabSwitch"] = MAX_WARNINGS + 1
    _, status_four, _, risk = _proctor_score(viol)
    assert status_four == "FAIL"
    assert risk == "High"


def test_question_seed_and_sequence_are_session_unique():
    questions = ["Explain Python testing.", "Explain Python testing.", "Describe API design.", "Discuss observability."]
    seed_a = make_question_session_seed("candidate@example.com", "session-a", "slot-1", "token")
    seed_b = make_question_session_seed("candidate@example.com", "session-b", "slot-1", "token")

    seq_a = prepare_unique_question_sequence(questions, seed=seed_a)
    seq_b = prepare_unique_question_sequence(questions, seed=seed_b)

    assert len(seq_a) == 3
    assert sorted(seq_a) == sorted(set(seq_a))
    assert seed_a != seed_b
    assert set(seq_a) == set(seq_b)


def test_remember_asked_question_deduplicates_session_meta():
    session = {"meta": {}}
    remember_asked_question(session, " What is REST? ")
    remember_asked_question(session, "What   is REST?")
    assert session["meta"]["asked_questions"] == ["What is REST?"]


def test_transcript_payload_defaults_off_when_template_does_not_enable_it():
    session = {
        "current": 0,
        "questions": ["Q1"],
        "answers": [],
        "completed": False,
        "meta": {"jd_skills": ["python"]},
    }
    out = next_question_payload(session)
    assert out["show_spoken_text"] is False


def test_transcript_payload_respects_explicit_template_enablement():
    session = {
        "current": 0,
        "questions": ["Q1"],
        "answers": [],
        "completed": False,
        "meta": {"jd_skills": ["python"], "show_spoken_text": True},
    }
    out = next_question_payload(session)
    assert out["show_spoken_text"] is True
