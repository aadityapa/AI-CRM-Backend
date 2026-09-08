"""Prompt builder tests for mode-aware question generation."""

from prompts.interview.system_prompt import build_interview_system_prompt
from prompts.interview.user_prompt import build_interview_user_prompt


def test_system_prompt_contains_json_rule():
    prompt = build_interview_system_prompt()
    assert "JSON array" in prompt
    assert "interview questions" in prompt.lower()


def test_user_prompt_technical_mode():
    prompt = build_interview_user_prompt(
        interview_mode="technical",
        skills=["react", "node.js"],
        experience="3 years",
        role="Full Stack Developer",
        difficulty="medium",
        tech_stack="React, Node",
        resume_summary="Built dashboards",
        question_count=5,
    )
    assert "Technical Interview" in prompt
    assert "react" in prompt.lower()
    assert "Question Count: 5" in prompt


def test_user_prompt_hr_mode():
    prompt = build_interview_user_prompt(
        interview_mode="hr",
        skills=["communication"],
        experience="Fresher",
        role="Graduate Trainee",
        difficulty="easy",
        tech_stack="",
        resume_summary="",
        question_count=3,
    )
    assert "HR Interview" in prompt
    assert "behavioral" in prompt.lower()
