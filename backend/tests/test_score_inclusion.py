"""HR include-in-score moderation (reverse of exclude)."""

from utils.score_exclusion import exclude_question_from_score, include_question_in_score


def _sample_report():
    return {
        "overall_score": 6.25,
        "recommendation": "Consider",
        "per_question": [
            {"question_index": 1, "score": 8.0, "strengths": ["Clear"], "weaknesses": [], "feedback": "Good"},
            {"question_index": 2, "score": 7.5, "strengths": ["Solid"], "weaknesses": [], "feedback": "Good"},
            {"question_index": 3, "score": 2.0, "strengths": [], "weaknesses": ["Weak"], "feedback": "Poor"},
            {"question_index": 4, "score": 9.0, "strengths": ["Strong"], "weaknesses": [], "feedback": "Excellent"},
        ],
    }


def test_include_after_exclude_restores_aggregate_score():
    qs = ["Q1", "Q2", "Q3", "Q4"]
    ans = ["A1 long enough answer.", "A2 long enough answer.", "A3 long enough answer.", "A4 long enough answer."]
    report = _sample_report()
    excluded = exclude_question_from_score(
        report, qs, ans, question_index=3, excluded_by="HR", reason="Not relevant"
    )
    assert excluded["overall_score"] == 8.17

    restored = include_question_in_score(excluded, qs, ans, question_index=3, included_by="HR")
    row3 = restored["per_question"][2]
    assert row3.get("excluded_from_score") is not True
    assert float(restored["overall_score"]) == 6.62
    assert restored["scoring_summary"]["excluded_questions"] == 0
    actions = [a.get("action") for a in restored.get("question_evaluation_audit") or []]
    assert "include_in_score" in actions
