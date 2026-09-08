"""The template's time and question limits must both end the interview.

Before this, each mode enforced only half of its contract:
  * count mode had a question cap but no clock
  * time mode had a clock, but only in the browser, and no question cap at all

A template that says "15 minutes or 10 questions" has to stop on whichever
arrives first, and it has to be the SERVER that decides — a browser timer can be
throttled in a background tab or reset by reloading the page.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from candidate.service import (  # noqa: E402
    _question_cap, interview_elapsed_seconds, interview_time_limit_seconds,
    mark_interview_started, next_question_payload, time_limit_reached,
)

FIFTEEN_MINUTES = 15 * 60


def session(*, num_q=10, timing="count", limit=0, pool=30, current=0, started_ago=None):
    s = {
        "current": current,
        "questions": [f"Question {i + 1}?" for i in range(pool)],
        "meta": {
            "num_q": num_q, "timing_mode": timing, "time_limit_sec": limit,
            "warmup_indices": [], "jd_skills": [],
        },
    }
    if started_ago is not None:
        s["meta"]["interview_started_epoch"] = time.time() - started_ago
    return s


def completed(payload) -> bool:
    return payload.get("message") == "Interview completed"


# ------------------------------------------------------------- question limit

def test_question_limit_ends_count_mode():
    s = session(num_q=3, timing="count", current=3)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "question_limit"


def test_question_limit_ends_time_mode_too():
    """Regression: time-mode sessions had NO question cap and ran the whole pool."""
    s = session(num_q=3, timing="time", limit=FIFTEEN_MINUTES, current=3)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "question_limit"


# ----------------------------------------------------------------- time limit

def test_time_limit_ends_time_mode():
    s = session(num_q=50, timing="time", limit=FIFTEEN_MINUTES,
                current=2, started_ago=FIFTEEN_MINUTES + 1)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "time_limit"


def test_time_limit_ends_count_mode_too():
    """Regression: count mode never checked the clock on the server."""
    s = session(num_q=50, timing="count", limit=FIFTEEN_MINUTES,
                current=2, started_ago=FIFTEEN_MINUTES + 1)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "time_limit"


def test_exactly_at_the_limit_ends():
    s = session(num_q=50, limit=FIFTEEN_MINUTES, current=1, started_ago=FIFTEEN_MINUTES)
    assert time_limit_reached(s)


# ------------------------------------------------------- whichever comes first

def test_question_limit_wins_when_it_arrives_first():
    s = session(num_q=3, limit=FIFTEEN_MINUTES, current=3, started_ago=60)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "question_limit"


def test_interview_continues_while_inside_both_limits():
    s = session(num_q=10, limit=FIFTEEN_MINUTES, current=1, started_ago=30)
    payload = next_question_payload(s)
    assert not completed(payload)
    assert payload["question"] == "Question 2?"
    assert payload["questions_remaining"] == 9
    assert 860 <= payload["time_remaining_sec"] <= 870


# ------------------------------------------------------------- no limits set

def test_no_limits_configured_never_force_completes():
    s = session(num_q=0, timing="time", limit=0, current=5)
    assert _question_cap(s) is None
    assert not time_limit_reached(s)
    payload = next_question_payload(s)
    assert not completed(payload)
    assert payload["time_remaining_sec"] is None
    assert payload["questions_remaining"] is None


def test_running_out_of_questions_still_completes():
    s = session(num_q=0, timing="time", limit=0, pool=2, current=2)
    assert completed(next_question_payload(s))
    assert s["meta"]["completion_reason"] == "questions_exhausted"


# ------------------------------------------------------------------- the clock

def test_clock_starts_when_the_first_question_is_served_not_at_login():
    """A candidate reading the rules screen is not yet using interview time."""
    s = session(num_q=10, limit=FIFTEEN_MINUTES)
    assert interview_elapsed_seconds(s) == 0.0     # nothing served yet
    next_question_payload(s)
    assert s["meta"]["interview_started_epoch"] is not None
    assert interview_elapsed_seconds(s) < 1.0


def test_clock_is_not_restarted_by_a_reload():
    """mark_interview_started must be idempotent, or reloading buys extra time."""
    s = session(num_q=10, limit=FIFTEEN_MINUTES, started_ago=300)
    first = mark_interview_started(s)
    second = mark_interview_started(s)
    assert first == second
    assert interview_elapsed_seconds(s) >= 299


def test_time_limit_reads_the_template_value():
    s = session(limit=FIFTEEN_MINUTES)
    assert interview_time_limit_seconds(s) == FIFTEEN_MINUTES
    assert interview_time_limit_seconds(session(limit=0)) == 0
