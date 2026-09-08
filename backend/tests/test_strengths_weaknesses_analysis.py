from utils.strengths_weaknesses_analysis import (
    _ensure_question_sw,
    attach_strengths_weaknesses_analysis,
    build_strengths_weaknesses_analysis,
)


def test_build_from_existing_per_question():
    report = {
        "strengths": ["Strong CAN knowledge"],
        "gaps": ["Weak CAPL depth"],
        "per_question": [
            {
                "score": 7,
                "strengths": ["Correct definition"],
                "weaknesses": ["Missed multiplexing"],
            }
        ],
    }
    analysis = build_strengths_weaknesses_analysis(report, ["What is CAN?"], ["Controller Area Network."])
    assert analysis["overall_strengths"] == ["Strong CAN knowledge"]
    assert analysis["overall_weaknesses"] == ["Weak CAPL depth"]
    assert len(analysis["questions"]) == 1
    assert analysis["questions"][0]["question_strengths"] == ["Correct definition"]
    assert analysis["questions"][0]["score_display"] == "7/10"


def test_attach_persists_without_changing_overall_score(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    report = {
        "overall_score": 6.5,
        "per_question": [{"score": 6.5, "strengths": ["Good"], "weaknesses": ["Gap"]}],
        "strengths": ["Overall good"],
        "gaps": ["Overall gap"],
    }
    out = attach_strengths_weaknesses_analysis(report, ["Q1"], ["A1"], model="gpt-4o-mini")
    assert out["overall_score"] == 6.5
    sw = out.get("strengths_weaknesses_analysis") or {}
    assert sw.get("complete") is True
    assert sw["questions"][0]["question_strengths"] == ["Good"]


def test_ensure_question_sw_fills_high_score_weaknesses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    item = {
        "question_index": 1,
        "question": "What is CAN?",
        "answer": "Controller Area Network for ECUs.",
        "question_strengths": ["Clear CAN definition"],
        "question_weaknesses": [],
        "score": 8.5,
        "score_display": "8.5/10",
    }
    out = _ensure_question_sw(item)
    assert len(out["question_strengths"]) >= 1
    assert len(out["question_weaknesses"]) >= 1
    assert "production" in out["question_weaknesses"][0].lower() or "performance" in out["question_weaknesses"][0].lower()


def test_ensure_question_sw_skipped_template(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    out = attach_strengths_weaknesses_analysis(
        {"per_question": [{"score": 0, "strengths": [], "weaknesses": []}]},
        ["UDS?"],
        ["skip"],
        model="gpt-4o-mini",
    )
    sw = out["strengths_weaknesses_analysis"]["questions"][0]
    assert sw["question_strengths"] == ["None identified"]
    assert "skipped" in sw["question_weaknesses"][0].lower()
    assert out["strengths_weaknesses_analysis"].get("discussion_points")


def test_attach_adds_discussion_points_for_low_score(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "")
    out = attach_strengths_weaknesses_analysis(
        {
            "per_question": [
                {"score": 4, "strengths": ["Partial"], "weaknesses": ["Missing depth", "Incomplete"]},
            ]
        },
        ["Q1"],
        ["short"],
        model="gpt-4o-mini",
    )
    pts = out["strengths_weaknesses_analysis"].get("discussion_points") or []
    assert any(p.get("question_index") == 1 for p in pts)
