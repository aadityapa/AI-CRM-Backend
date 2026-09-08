from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


MAX_PROMPT_CHARS = 12000


def _norm_csv_or_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [x.strip() for x in str(value).split(",")]
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        s = " ".join(str(raw or "").split()).strip()
        if not s:
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(s)
    return out[:30]


def sanitize_prompt_input(prompt: str, *, max_chars: int = MAX_PROMPT_CHARS) -> str:
    text = str(prompt or "").replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def estimate_tokens(prompt: str) -> int:
    # Fast heuristic suitable for UI limits/preview.
    return max(0, int(round(len(str(prompt or "")) / 4.0)))


def build_template_prompt_context(
    *,
    role: str = "",
    experience: str = "",
    required_skills: Any = None,
    optional_skills: Any = None,
    difficulty: str = "medium",
    interview_type: str = "technical",
    customer_name: str = "",
    opportunity_id: str = "",
    template_instructions: str = "",
    technology_stack: str = "",
    interview_mode: str = "technical",
) -> dict[str, str]:
    req = _norm_csv_or_list(required_skills)
    opt = _norm_csv_or_list(optional_skills)
    skills = req + [s for s in opt if s.lower() not in {x.lower() for x in req}]
    experience_label = str(experience or "").strip()
    if not experience_label:
        experience_label = "Not specified"
    return {
        "role": str(role or "").strip() or "Not specified",
        "experience": experience_label,
        "skills": ", ".join(skills) if skills else "Not specified",
        "difficulty": str(difficulty or "medium").strip().lower() or "medium",
        "interview_type": str(interview_type or "").strip() or "Not specified",
        "customer_name": str(customer_name or "").strip() or "Not specified",
        "opportunity_id": str(opportunity_id or "").strip() or "Not specified",
        "template_instructions": str(template_instructions or "").strip() or "None",
        "technology_stack": str(technology_stack or "").strip() or "Not specified",
        "interview_mode": str(interview_mode or "").strip() or "technical",
    }


def build_default_template_prompt(context: dict[str, str]) -> str:
    c = context or {}
    exp_text = str(c.get("experience", "") or "").strip().lower()
    exp_nums = [int(x) for x in re.findall(r"\d+", exp_text)]
    max_exp = max(exp_nums) if exp_nums else -1
    if max_exp >= 0 and max_exp <= 2:
        tier = "junior"
        seniority_style = "Junior-friendly"
    elif max_exp >= 5:
        tier = "senior"
        seniority_style = "Senior-level"
    else:
        tier = "mid"
        seniority_style = "Mid-level practical"

    requirements_lines: list[str] = [
        "- Avoid repeated wording and repetitive question openings.",
        "- Cover all listed skills at least once across the generated set.",
        "- Do not repeat the same skill in consecutive questions unless follow-up is needed.",
        "- Keep each question concise and one sentence.",
    ]
    style_lines: list[str] = [
        "- Conversational",
        "- Adaptive",
        f"- {seniority_style}",
        "- Production-oriented",
    ]
    guidelines_lines: list[str] = [
        "- Start with fundamentals.",
        "- Gradually increase difficulty across the set.",
        "- Use clear and direct language suitable for the experience level.",
        "- Avoid tricky, multi-layered, or overly theoretical questions.",
    ]

    if tier == "junior":
        # Your requested junior/fresher style defaults.
        requirements_lines = [
            "- Ask beginner-friendly questions.",
            "- Include simple debugging and troubleshooting scenarios.",
            "- Ask about day-to-day development and testing activities.",
            "- Keep architecture questions easy-level and easy to understand.",
            "- Avoid repeated wording and repetitive question openings.",
            "- Evaluate clarity of understanding, communication, and basic problem-solving skills.",
            "- Cover all listed skills at least once across the generated set.",
            "- Do not repeat the same skill in consecutive questions unless a simple follow-up is needed.",
            "- Keep each question concise, clear, and easy to answer.",
            "- Each question should be one sentence with 10-22 words.",
            "- Avoid advanced optimization, complex scalability, or expert-level production scenarios.",
        ]
        style_lines = [
            "- Conversational",
            "- Junior-friendly",
            "- Supportive",
            "- Easy technical discussion",
        ]
        guidelines_lines = [
            "- Start with simple fundamentals.",
            "- Gradually move toward basic debugging and integration scenarios.",
            "- Use clear and direct language suitable for freshers and junior engineers.",
            "- Avoid tricky, multi-layered, or highly theoretical questions.",
        ]
    elif tier == "mid":
        requirements_lines = [
            "- Ask practical real-world engineering questions.",
            "- Include debugging scenarios and incident handling (non-distributed, team-scale).",
            "- Include API/integration thinking and maintainability decisions.",
            "- Avoid repeated wording and repetitive question openings.",
            "- Evaluate depth of understanding with concrete examples and trade-offs.",
            "- Cover all listed skills at least once across the generated set.",
            "- Do not repeat the same skill in consecutive questions unless follow-up is needed.",
            "- Keep each question concise (one sentence, ideally 12-28 words).",
        ]
        style_lines = [
            "- Conversational",
            "- Practical",
            "- Mid-level",
            "- Production-aware",
        ]
        guidelines_lines = [
            "- Start with implementation fundamentals.",
            "- Move into debugging and integration scenarios.",
            "- Include at least a few trade-off questions.",
            "- Avoid purely textbook definition questions.",
        ]
    else:
        requirements_lines = [
            "- Ask senior-level real-world engineering questions.",
            "- Include debugging scenarios, incident response, and reliability thinking.",
            "- Include architecture decisions, trade-offs, and maintainability concerns.",
            "- Avoid repeated wording and repetitive question openings.",
            "- Evaluate depth of understanding, judgment, and communication clarity.",
            "- Cover all listed skills at least once across the generated set.",
            "- Do not repeat the same skill in consecutive questions unless follow-up is needed.",
            "- Keep each question concise (one sentence, ideally 12-28 words).",
        ]
        style_lines = [
            "- Conversational",
            "- Senior-level",
            "- Production-oriented",
            "- Direct and rigorous",
        ]
        guidelines_lines = [
            "- Start with core fundamentals quickly, then escalate.",
            "- Include incident, reliability, and architecture trade-offs.",
            "- Prefer open-ended questions that reveal engineering judgment.",
            "- Avoid junior-level syntax or trivial definitions.",
        ]
    return sanitize_prompt_input(
        f"""You are a senior AI technical interviewer.

Generate adaptive interview questions for the following role:

Role: {c.get("role", "Not specified")}
Experience: {c.get("experience", "Not specified")}
Skills: {c.get("skills", "Not specified")}
Difficulty: {c.get("difficulty", "medium")}
Interview Type: {c.get("interview_type", "Not specified")}
Interview Mode: {c.get("interview_mode", "technical")}
Customer: {c.get("customer_name", "Not specified")}
Opportunity ID: {c.get("opportunity_id", "Not specified")}
Technology Stack: {c.get("technology_stack", "Not specified")}
Template Instructions: {c.get("template_instructions", "None")}

Requirements:
{chr(10).join(requirements_lines)}

Interview Style:
{chr(10).join(style_lines)}

Question Guidelines:
{chr(10).join(guidelines_lines)}

Generate one question at a time.
"""
    )


def is_custom_edited_prompt(edited: str, generated: str) -> bool:
    """True when HR replaced the generated default with their own prompt text."""
    edited_s = sanitize_prompt_input(edited)
    generated_s = sanitize_prompt_input(generated)
    return bool(edited_s) and bool(generated_s) and edited_s != generated_s


def render_prompt_preview(prompt: str, context: dict[str, str]) -> str:
    text = str(prompt or "")
    c = context or {}
    mapping = {
        "{{role}}": c.get("role", ""),
        "{{experience}}": c.get("experience", ""),
        "{{skills}}": c.get("skills", ""),
        "{{difficulty}}": c.get("difficulty", ""),
        "{{interview_type}}": c.get("interview_type", ""),
        "{{customer_name}}": c.get("customer_name", ""),
        "{{opportunity_id}}": c.get("opportunity_id", ""),
        "{{template_instructions}}": c.get("template_instructions", ""),
        "{{technology_stack}}": c.get("technology_stack", ""),
        "{{interview_mode}}": c.get("interview_mode", ""),
    }
    for k, v in mapping.items():
        text = text.replace(k, str(v or ""))
    return sanitize_prompt_input(text)


def now_iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()

