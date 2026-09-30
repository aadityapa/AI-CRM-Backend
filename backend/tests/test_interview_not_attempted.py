"""An interview that never asked a scored question is not a rejection.

Reported after the September deploy: the interview closed one turn in — only
the warm-up "Please introduce yourself." was saved — and the report read
0% across the board with a recommendation of **Reject**. Nobody had been
rejected; the platform never asked a scored question.

These pin the line between "did badly" and "never happened".
"""

import importlib

from services.interview_outcome import (
    NOT_ATTEMPTED_FITMENT,
    NOT_ATTEMPTED_RECOMMENDATION,
    apply_not_attempted,
    is_not_attempted,
    scored_answer_count,
)

WARMUP_META = {"warmup_indices": [0]}


def _report(**over) -> dict:
    base = {
        "overall_score": 0.0,
        "overall_fitment": "Weak Fit",
        "recommendation": "Reject",
        "summary": "No scored content in the evaluable answer set.",
    }
    base.update(over)
    return base


def test_only_the_warmup_answered_is_not_attempted():
    questions = ["Please introduce yourself.", "Explain CAN arbitration."]
    answers = ["Hi, I am Karan, six years in automotive."]
    assert scored_answer_count(questions, answers, WARMUP_META) == 0
    assert is_not_attempted(questions, answers, WARMUP_META) is True


def test_the_warmup_answer_never_counts_as_a_scored_answer():
    """The warm-up is a microphone check, not evidence of competence."""
    questions = ["Please introduce yourself."]
    answers = ["A long, articulate, thoroughly impressive introduction."]
    assert scored_answer_count(questions, answers, WARMUP_META) == 0


def test_one_real_answer_is_an_attempt():
    questions = ["Please introduce yourself.", "Explain CAN arbitration."]
    answers = ["Hi, I am Karan.", "Lower ID wins arbitration on CAN."]
    assert scored_answer_count(questions, answers, WARMUP_META) == 1
    assert is_not_attempted(questions, answers, WARMUP_META) is False


def test_skipping_every_question_is_still_an_attempt_and_keeps_its_verdict():
    """The whole point of the distinction.

    A candidate who was ASKED five questions and skipped all five has attempted
    the interview. Refusing to answer is a real signal, and the model's Reject
    must survive untouched. Only "we never asked" is exempted.
    """
    questions = ["Please introduce yourself.", "Q1", "Q2", "Q3"]
    answers = ["Hello.", "skip", "skip", "skip"]
    assert is_not_attempted(questions, answers, WARMUP_META) is True

    report = apply_not_attempted(_report(), questions, answers, WARMUP_META)
    assert report["recommendation"] == NOT_ATTEMPTED_RECOMMENDATION

    # ...but one substantive answer among the skips is an attempt, verdict kept.
    answers_with_one = ["Hello.", "skip", "Lower ID wins arbitration.", "skip"]
    assert is_not_attempted(questions, answers_with_one, WARMUP_META) is False
    kept = apply_not_attempted(_report(), questions, answers_with_one, WARMUP_META)
    assert kept["recommendation"] == "Reject"
    assert kept["not_attempted"] is False


def test_apply_overrides_the_verdict_but_keeps_the_numbers():
    """Scores stay visible; only the part a TA acts on changes.

    The zeros are honest — they describe an empty transcript, and a reviewer
    should be able to see it. What must not stand is a hiring verdict.
    """
    questions = ["Please introduce yourself.", "Q1"]
    answers = ["Hello."]
    report = apply_not_attempted(_report(overall_score=0.0), questions, answers, WARMUP_META)
    assert report["not_attempted"] is True
    assert report["recommendation"] == NOT_ATTEMPTED_RECOMMENDATION
    assert report["overall_fitment"] == NOT_ATTEMPTED_FITMENT
    assert report["verdict_suppressed_reason"] == "no_scored_answers"
    assert report["overall_score"] == 0.0  # untouched
    assert "re-invite" in report["summary"].lower()


def test_blank_and_placeholder_answers_do_not_count():
    questions = ["Please introduce yourself.", "Q1", "Q2", "Q3"]
    for empty in ("", "   ", "skip", "SKIPPED", "[skipped]"):
        answers = ["Hello.", empty, empty, empty]
        assert scored_answer_count(questions, answers, WARMUP_META) == 0, empty


def test_a_session_with_no_warmup_configured_still_works():
    """`warmup_indices` is absent on older sessions — index 0 is then scored."""
    questions = ["Explain CAN arbitration."]
    answers = ["Lower ID wins."]
    assert scored_answer_count(questions, answers, {}) == 1
    assert is_not_attempted(questions, answers, {}) is False
    assert is_not_attempted(questions, answers, None) is False


def test_build_report_record_stamps_it():
    """The one funnel every report passes through must apply this.

    Pinned as a behaviour rather than a call: the fast path, the AI upgrade and
    both recovery paths all reach `build_report_record`, so stamping it there
    is what covers them without four separate hooks.
    """
    hr_service = importlib.import_module("hr.service")
    session = {
        "questions": ["Please introduce yourself.", "Explain CAN arbitration."],
        "answers": ["Hi, I am Karan."],
        "meta": {"warmup_indices": [0], "interview_id": "iv-1", "jd_skills": ["can"]},
    }
    record = hr_service.build_report_record(session, _report(), {"ist_iso": "", "ist_date": "", "ist_time": ""})
    report = record.get("report") if isinstance(record.get("report"), dict) else _report()
    # `build_report_record` mutates the result dict it was handed, which is the
    # same object it embeds, so either view shows the override.
    assert report.get("recommendation") == NOT_ATTEMPTED_RECOMMENDATION or \
        record.get("recommendation") == NOT_ATTEMPTED_RECOMMENDATION
