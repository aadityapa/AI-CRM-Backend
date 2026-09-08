from utils.interview_limits import MAX_COUNT_MODE_QUESTIONS, clamp_count_mode_questions


def test_clamp_count_mode_questions_allows_50():
    assert clamp_count_mode_questions(50) == 50


def test_clamp_count_mode_questions_caps_at_max():
    assert clamp_count_mode_questions(500) == MAX_COUNT_MODE_QUESTIONS


def test_clamp_count_mode_questions_min_one():
    assert clamp_count_mode_questions(0) == 1
