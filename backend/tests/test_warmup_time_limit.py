"""The warm-up question carries its own time limit (23 Sep 2026).

"Please introduce yourself." is not scored, so the client counts a visible
limit down on it and moves on at zero. The number comes from the server so it
is one setting, not a constant buried in the candidate page.
"""
import importlib

import pytest

from candidate.service import next_question_payload
from utils.warmup import DEFAULT_WARMUP_TIME_LIMIT_SEC, WARMUP_TIME_LIMIT_ENV, warmup_time_limit_sec


def _session(current=0):
    return {
        "current": current,
        "questions": ["Please introduce yourself.", "What is CAN?", "What is AUTOSAR?"],
        "answers": [] if current == 0 else ["hi"],
        "meta": {"jd_skills": ["can"], "timing_mode": "count", "num_q": 2, "warmup_indices": [0]},
    }


def test_default_is_sixty_seconds(monkeypatch):
    monkeypatch.delenv(WARMUP_TIME_LIMIT_ENV, raising=False)
    assert warmup_time_limit_sec() == DEFAULT_WARMUP_TIME_LIMIT_SEC == 60


@pytest.mark.parametrize("raw,expected", [("90", 90), ("5", 15), ("99999", 600), ("0", 0), ("abc", 60), ("", 60)])
def test_env_override_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv(WARMUP_TIME_LIMIT_ENV, raw)
    assert warmup_time_limit_sec() == expected


def test_warmup_payload_carries_the_limit(monkeypatch):
    monkeypatch.delenv(WARMUP_TIME_LIMIT_ENV, raising=False)
    out = next_question_payload(_session(0))
    assert out["is_warmup"] is True
    assert out["warmup_time_limit_sec"] == 60


def test_scored_question_has_no_warmup_limit():
    out = next_question_payload(_session(1))
    assert out["is_warmup"] is False
    assert "warmup_time_limit_sec" not in out
