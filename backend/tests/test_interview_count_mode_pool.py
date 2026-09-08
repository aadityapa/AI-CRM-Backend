from candidate.service import _count_mode_question_cap, _evaluated_total, next_question_payload
from utils.interview_limits import pool_questions_for_timing, trim_questions_for_count_mode


def test_count_mode_pool_is_exact_template_num():
    assert pool_questions_for_timing(10, "count") == 10
    assert pool_questions_for_timing(15, "count") == 15
    assert pool_questions_for_timing(20, "count") == 20


def test_time_mode_pool_may_estimate_larger():
    assert pool_questions_for_timing(10, "time", time_limit_sec=3600) >= 10


def test_trim_questions_for_count_mode_caps_warmup_plus_scored():
    qs = [f"Q{i}" for i in range(1, 55)]
    out = trim_questions_for_count_mode(qs, 10, "count", warmup_count=1)
    assert len(out) == 11


def test_evaluated_total_uses_meta_num_q_in_count_mode():
    session = {
        "questions": ["W"] + [f"Q{i}" for i in range(1, 51)],
        "current": 0,
        "meta": {"timing_mode": "count", "num_q": 10, "warmup_indices": [0]},
    }
    assert _evaluated_total(session) == 10


def test_count_mode_cap_stops_serving_after_template_limit():
    session = {
        "questions": ["W"] + [f"Q{i}" for i in range(1, 51)],
        "answers": ["intro"] + ["a"] * 10,
        "current": 11,
        "meta": {"timing_mode": "count", "num_q": 10, "warmup_indices": [0], "jd_skills": []},
    }
    assert _count_mode_question_cap(session) == 11
    payload = next_question_payload(session)
    assert payload.get("message") == "Interview completed"
    assert session.get("completed") is True
