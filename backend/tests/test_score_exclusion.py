"""HR exclude-from-score moderation."""

from utils.score_exclusion import exclude_question_from_score, recompute_report_aggregates


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


def test_exclude_recomputes_mean_without_deleted_rows():
    qs = ["Q1", "Q2", "Q3", "Q4"]
    ans = ["A1 long enough answer.", "A2 long enough answer.", "A3 long enough answer.", "A4 long enough answer."]
    report = _sample_report()
    out = exclude_question_from_score(
        report,
        qs,
        ans,
        question_index=3,
        excluded_by="John HR",
        reason="Not relevant to role",
    )
    row3 = out["per_question"][2]
    assert row3["excluded_from_score"] is True
    assert row3["excluded_by"] == "John HR"
    assert row3["excluded_reason"] == "Not relevant to role"
    assert out["overall_score"] == 8.17
    summ = out["scoring_summary"]
    assert summ["excluded_questions"] == 1
    assert summ["evaluated_questions"] == 3
    assert len(out["question_evaluation_audit"]) == 1


def test_excluded_question_data_preserved():
    qs = ["Q1", "Q2", "Q3", "Q4"]
    ans = ["A1 long enough.", "A2 long enough.", "A3 long enough.", "A4 long enough."]
    report = _sample_report()
    out = exclude_question_from_score(report, qs, ans, question_index=3, excluded_by="HR", reason="Duplicate")
    assert out["per_question"][2]["score"] == 2.0
    assert out["per_question"][2]["feedback"] == "Poor"
    assert len(out["per_question"]) == 4


def test_recompute_respects_existing_exclusions():
    report = _sample_report()
    report["per_question"][2]["excluded_from_score"] = True
    qs = ["Q1", "Q2", "Q3", "Q4"]
    ans = ["A1 long enough.", "A2 long enough.", "A3 long enough.", "A4 long enough."]
    out = recompute_report_aggregates(report, qs, ans)
    assert out["overall_score"] == 8.17
    assert out["scoring_summary"]["excluded_questions"] == 1


def test_recompute_preserves_communication_evaluation():
    report = _sample_report()
    report["communication_evaluation"] = {
        "communication_score": 6.5,
        "presentation_score": 7.0,
        "confidence_score": 6.0,
        "overall_score": 6.5,
    }
    qs = ["Q1", "Q2", "Q3", "Q4"]
    ans = ["A1 long enough.", "A2 long enough.", "A3 long enough.", "A4 long enough."]
    out = recompute_report_aggregates(report, qs, ans)
    comm = out["communication_evaluation"]
    assert comm["communication_score"] == 6.5
    assert comm["presentation_score"] == 7.0
    assert comm["confidence_score"] == 6.0
