"""Tests for professional per-question assessment JSON parsing and backward compatibility."""

from ai import (
    _normalize_professional_assessment_row,
    _zero_score_evaluation_row,
    apply_quality_caps_to_per_question_row,
    preflight_per_question_evaluation,
)


def test_normalize_professional_assessment_row_parses_rich_json():
    raw = {
        "i": 1,
        "score": 7.5,
        "overall_rating": 7.5,
        "summary": "Solid CAN FD overview with practical framing.",
        "correct_concepts": [
            {"topic": "Payload size", "explanation": "Mentioned 64-byte payload support."},
        ],
        "improvement_areas": [
            {
                "topic": "Bit timing",
                "explanation": "Did not explain data-phase bit rate switching.",
                "correction": "Describe how BRS enables faster data phase.",
            }
        ],
        "expected_answer": "CAN FD extends Classical CAN with up to 64-byte payloads and optional faster data phase.",
        "interview_feedback": "The candidate demonstrated working knowledge of CAN FD fundamentals.",
        "follow_up_questions": ["How do you configure CAN FD on your target ECU?"],
        "dimension_scores": {
            "technical_accuracy": 78,
            "concept_coverage": 72,
            "depth": 65,
            "communication": 80,
            "confidence": 70,
        },
        "strengths": ["Clear payload explanation."],
        "weaknesses": ["Missing bit-rate detail."],
    }
    row = _normalize_professional_assessment_row(raw)
    assert row["overall_rating"] == 7.5
    assert row["summary"].startswith("Solid CAN FD")
    assert len(row["correct_concepts"]) == 1
    assert row["correct_concepts"][0]["topic"] == "Payload size"
    assert len(row["improvement_areas"]) == 1
    assert row["improvement_areas"][0]["correction"]
    assert row["expected_answer"]
    assert row["interview_feedback"]
    assert row["follow_up_questions"] == ["How do you configure CAN FD on your target ECU?"]
    assert row["dimension_scores"]["technical_accuracy"] == 78
    assert row["ideal_answer"] == row["expected_answer"]


def test_zero_score_row_includes_professional_fields():
    row = _zero_score_evaluation_row(
        2,
        feedback="Repeated the question.",
        weaknesses=["Repeated question instead of answering."],
        reason="question_repetition",
        question="What is CAN FD?",
    )
    assert float(row["score"]) == 0.0
    assert row["correct_concepts"] == []
    assert row["strengths"] == ["No significant technical strengths identified."]
    assert len(row["improvement_areas"]) >= 1
    assert row["dimension_scores"]["technical_accuracy"] == 0
    assert row["follow_up_questions"]


def test_preflight_echo_uses_professional_zero_score():
    row = preflight_per_question_evaluation("What is CAN FD?", "What is CAN FD?", 1)
    assert row is not None
    assert float(row["score"]) == 0.0
    assert "No significant technical strengths identified." in row["strengths"]
    assert row["correct_concepts"] == []
    assert row["summary"]


def test_legacy_row_without_new_fields_still_normalizes():
    legacy = {
        "question_index": 1,
        "score": 6.0,
        "strengths": ["Good basics."],
        "weaknesses": ["Needs more depth."],
        "feedback": "Adequate answer.",
    }
    row = _normalize_professional_assessment_row(legacy)
    assert row["score"] == 6.0
    assert row["strengths"] == ["Good basics."]
    assert row["weaknesses"] == ["Needs more depth."]
    assert row["summary"] == "Adequate answer."
    assert row["improvement_areas"]
    assert row["dimension_scores"]["technical_accuracy"] == 60


def test_apply_quality_caps_preserves_professional_fields():
    q = "What is CAN FD?"
    a = (
        "CAN FD is an extension of Classical CAN that supports payloads up to 64 bytes "
        "and allows higher data rates during the data phase."
    )
    row = apply_quality_caps_to_per_question_row(
        {
            "question_index": 1,
            "score": 8.5,
            "overall_rating": 8.5,
            "summary": "Strong answer.",
            "correct_concepts": [{"topic": "Payload", "explanation": "64 bytes."}],
            "improvement_areas": [],
            "expected_answer": "Model answer text.",
            "interview_feedback": "Well explained.",
            "follow_up_questions": ["Follow up?"],
            "dimension_scores": {
                "technical_accuracy": 85,
                "concept_coverage": 80,
                "depth": 75,
                "communication": 82,
                "confidence": 78,
            },
            "strengths": ["Strong technical detail."],
            "weaknesses": [],
        },
        q,
        a,
    )
    assert float(row["score"]) >= 7.0
    assert row["correct_concepts"]
    assert row["expected_answer"] == "Model answer text."
