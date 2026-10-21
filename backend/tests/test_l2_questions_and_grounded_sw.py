"""21 Oct 2026 — the AI report reads against the POSITION's skills, and a
reviewer can ask for ten L2 questions.

Run:  cd backend && python -m pytest tests/test_l2_questions_and_grounded_sw.py -q
"""
from __future__ import annotations

import json
import pathlib

from services import l2_questions as l2
from utils.strengths_weaknesses_analysis import attach_strengths_weaknesses_analysis

QUESTIONS = [
    "Explain how AUTOSAR BSW layers talk to the RTE.",
    "How do you debug a CAN bus-off condition?",
    "What is a Kubernetes operator?",
]
ANSWERS = [
    "The BSW exposes services through the RTE using ports and runnables.",
    "skip",
    "An operator manages custom resources with a control loop.",
]
REPORT = {
    "strengths": ["Strong Kubernetes knowledge", "Good cloud skills"],   # not the position's skills
    "weaknesses": ["Could be more concise"],
    "skill_scores": [{"skill": "Kubernetes", "score": 90}],
    "per_question": [
        {"score": 8, "strengths": ["Named ports and runnables correctly"], "weaknesses": ["No example"]},
        {},
        {"score": 9, "strengths": ["Clear control-loop explanation"], "weaknesses": ["—"]},
    ],
}
SKILLS = ["AUTOSAR", "CAN", "UDS"]


def test_the_summary_names_only_the_position_skills():
    out = attach_strengths_weaknesses_analysis(dict(REPORT), QUESTIONS, ANSWERS, position_skills=SKILLS)
    a = out["strengths_weaknesses_analysis"]
    joined = " ".join(a["overall_strengths"] + a["overall_weaknesses"])
    assert "Kubernetes" not in joined and "cloud" not in joined.lower()
    assert any(s.startswith("AUTOSAR") for s in a["overall_strengths"])
    assert any(w.startswith("UDS") and "not tested" in w for w in a["overall_weaknesses"])
    # The AI's own lists are kept as notes, never thrown away.
    assert a["ai_notes_strengths"] == ["Strong Kubernetes knowledge", "Good cloud skills"]
    assert {r["skill"] for r in a["position_skills"]} == set(SKILLS)


def test_without_position_skills_the_old_lists_stay():
    out = attach_strengths_weaknesses_analysis(dict(REPORT), QUESTIONS, ANSWERS)
    assert out["strengths_weaknesses_analysis"]["overall_strengths"][0] == "Strong Kubernetes knowledge"


def test_parse_keeps_ten_distinct_questions_on_position_skills():
    raw = json.dumps({"questions": [
        {"skill": "AUTOSAR", "question": "1. How would you configure the COM stack for a new signal", "why": "x"},
        {"skill": "AUTOSAR", "question": "How would you configure the COM stack for a new signal?"},  # duplicate
        {"skill": "Cooking", "question": "Walk me through a CAN error frame on the bus?"},
        {"skill": "UDS", "question": "short"},
    ] + [{"skill": "UDS", "question": f"Explain UDS service 0x{i:02X} in a real flashing sequence?"}
         for i in range(20)]})
    qs = l2.parse_questions(raw, SKILLS)
    assert len(qs) == l2.L2_COUNT
    assert qs[0]["question"].startswith("How would you configure") and qs[0]["question"].endswith("?")
    assert qs[1]["skill"] == "CAN"          # off-list skill mapped by the text, never invented
    assert [q["number"] for q in qs] == list(range(1, 11))


def test_the_prompt_points_at_the_weak_and_untested_skills():
    rec = {"job_title": "AUTOSAR Developer", "skills": SKILLS, "report": REPORT,
           "questions": QUESTIONS, "answers": ANSWERS}
    _, user, ctx = l2.build_prompt(rec)
    assert "AUTOSAR Developer" in user and "never a skill outside the list" in user
    statuses = {s["skill"]: s["status"] for s in ctx["skills"]}
    assert statuses["UDS"] == "not_tested"
    assert any(w["q"] == 2 for w in ctx["weak_answers"])          # the skipped CAN answer


def test_the_routes_are_behind_the_report_reader_gate():
    src = (pathlib.Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    for fn in ("def hr_l2_questions(", "def hr_generate_l2_questions("):
        body = src.split(fn, 1)[1].split("\n@app.", 1)[0]
        assert "_require_interview_report_reader(request)" in body, fn
