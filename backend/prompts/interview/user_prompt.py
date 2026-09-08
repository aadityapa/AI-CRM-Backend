"""
Dynamic user prompts for mode-aware interview question generation.
"""

from __future__ import annotations

from utils.interview_mode_mapper import normalize_interview_mode, to_display_label


def _mode_instructions(canonical_mode: str) -> str:
    """Mode-specific generation guidance appended to the user prompt."""
    if canonical_mode == "hr":
        return (
            "Focus on: communication, behavioral, teamwork, leadership, conflict handling, "
            "time management, career goals, culture fit, and situational questions. "
            "Questions should feel human-like and professional; avoid repetitive generic prompts. "
            "Mix leadership, scenario, trade-off, mentoring, and pressure-handling styles."
        )
    return (
        "Focus on: deep technical, real-world scenarios, problem-solving, architecture, "
        "debugging, coding logic, and best practices. "
        "Increase difficulty gradually across the set; avoid repetition. "
        "Mix implementation, debugging, production incident, architecture, optimization, and trade-off styles."
    )


def build_interview_user_prompt(
    *,
    interview_mode: str,
    skills: list[str],
    experience: str,
    role: str,
    difficulty: str,
    tech_stack: str,
    resume_summary: str,
    question_count: int,
) -> str:
    """
    Build the dynamic user prompt from template fields.

    interview_mode: canonical technical | hr (legacy values are normalized).
    """
    mode = normalize_interview_mode(interview_mode)
    mode_label = to_display_label(mode)
    skills_str = ", ".join(s.strip() for s in skills[:15] if s and str(s).strip()) or "general skills"
    exp = (experience or "").strip() or "Not specified"
    job_role = (role or "").strip() or "Not specified"
    diff = (difficulty or "medium").strip() or "medium"
    stack = (tech_stack or "").strip() or "Not specified"
    resume = (resume_summary or "").strip() or "Not provided"
    n = max(1, min(int(question_count or 1), 50))

    return (
        f"Interview Mode: {mode_label}\n"
        f"Candidate Skills: {skills_str}\n"
        f"Experience Level: {exp}\n"
        f"Job Role: {job_role}\n"
        f"Difficulty: {diff}\n"
        f"Tech Stack: {stack}\n"
        f"Resume Summary: {resume[:4000]}\n"
        f"Question Count: {n}\n\n"
        f"{_mode_instructions(mode)}\n\n"
        "Difficulty progression hint:\n"
        "- Early: fundamentals / implementation\n"
        "- Middle: debugging / optimization / scenarios\n"
        "- Late: architecture / reliability / leadership / trade-offs\n\n"
        "Generate professional interview questions.\n"
        "Return a JSON array only. Each item must have keys: "
        '"question", "category", "difficulty", "type".\n'
        f"Generate exactly {n} questions."
    )
