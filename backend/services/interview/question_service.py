"""
Mode-aware interview question generation (OpenAI + fallback).

Primary path now uses a single template-driven user prompt so question
generation behavior is controlled by the saved template prompt text.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional

from services.openai.chat import chat_completion_with_retry
from utils.interview_mode_mapper import normalize_interview_mode
from validators.interview.question_response import parse_questions_from_json_response

logger = logging.getLogger(__name__)


def _db_target() -> str:
    direct = (os.getenv("AUTH_DB_URL") or "").strip()
    if direct:
        return direct
    host = (os.getenv("DB_HOST") or "").strip()
    port = (os.getenv("DB_PORT") or "5432").strip()
    name = (os.getenv("DB_NAME") or "").strip()
    user = (os.getenv("DB_USER") or "").strip()
    pw = (os.getenv("DB_PASSWORD") or "").strip()
    if host and name and user:
        return f"postgresql://{user}:{pw}@{host}:{port}/{name}"
    from paths import KARNEX_DB_FILE

    return str(KARNEX_DB_FILE)


def _mode_aware_enabled() -> bool:
    """Feature flag: mode-aware generation path."""
    return str(os.getenv("INTERVIEW_MODE_AWARE_GENERATION", "true")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _single_template_prompt(
    *,
    template_prompt: str,
    question_count: int,
) -> str:
    """
    Build one user prompt payload for OpenAI.
    Uses the template prompt as the authoritative instruction block.
    """
    n = max(1, min(int(question_count or 1), 50))
    base = (template_prompt or "").strip()
    if not base:
        base = (
            "You are a senior technical interviewer.\n\n"
            "Generate interview questions based on the configured role, experience, skills, "
            "difficulty, interview type, and interview mode."
        )
    return (
        f"{base}\n\n"
        "Output format:\n"
        "- Return only a JSON array.\n"
        '- Each array item must have keys: "question", "category", "difficulty", "type".\n'
        f"- Generate exactly {n} questions.\n"
        "- Do not include explanations, markdown, or additional text outside JSON."
    )


def generate_mode_aware_questions(
    *,
    interview_mode: str,
    skills: List[str],
    experience: str = "",
    role: str = "",
    difficulty: str = "medium",
    question_count: int,
    tech_stack: str = "",
    resume_summary: str = "",
    jd_text: str = "",
    cv_text: str = "",
    model: str = "gpt-4o-mini",
    temperature: float = 0.45,
    coach_hints: str = "",
    avoid_history: Optional[List[str]] = None,
    domain_categories: Optional[list] = None,
    raw_passthrough: bool = False,
    variety_seed: str = "",
    template_custom: bool = False,
    validation_skills: Optional[List[str]] = None,
) -> List[str]:
    """
    Generate questions with one template-driven prompt.

    Falls back to generate_questions_fallback on failure.
    """
    if not _mode_aware_enabled():
        val_skills = list(validation_skills) if validation_skills is not None else list(skills or [])
        return _legacy_generate(
            jd_text=jd_text,
            cv_text=cv_text,
            difficulty=difficulty,
            question_count=question_count,
            model=model,
            skills=skills,
            coach_hints=coach_hints,
            experience=experience,
            avoid_history=avoid_history,
            domain_categories=domain_categories,
            raw_passthrough=raw_passthrough,
            temperature=temperature,
            variety_seed=variety_seed,
            template_custom=template_custom,
            validation_skills=val_skills,
            strict_validate=not template_custom,
        )

    canonical = normalize_interview_mode(interview_mode)
    n = max(1, min(int(question_count or 1), 50))
    val_skills = list(validation_skills) if validation_skills is not None else list(skills or [])
    strict_validate = not template_custom
    user_prompt = _single_template_prompt(
        template_prompt=coach_hints,
        question_count=n,
    )
    messages = [{"role": "user", "content": user_prompt}]

    def _accept_questions(parsed: List[str]) -> List[str]:
        if not parsed:
            return []
        try:
            from prompt_builder import validate_questions

            accepted, _rejected = validate_questions(
                parsed, val_skills, difficulty, strict=strict_validate
            )
        except Exception:
            accepted = parsed
        if template_custom and parsed and len(accepted) < max(1, n // 2):
            return parsed[:n]
        return accepted[:n]

    try:
        text = chat_completion_with_retry(
            messages=messages,
            model=model,
            temperature=temperature,
            call_type="generate_questions_mode",
            db_target=_db_target(),
            difficulty=difficulty,
            selected_skills=skills,
        )
        parsed = parse_questions_from_json_response(text, expected_count=n)
        accepted = _accept_questions(parsed)
        if len(accepted) >= max(1, n // 2):
            return accepted[:n]
        logger.warning(
            "interview.questions.json_parse_insufficient",
            extra={"got": len(parsed), "expected": n, "mode": canonical},
        )
    except Exception as exc:
        logger.warning(
            "interview.questions.openai_failed",
            extra={"mode": canonical, "error": str(exc)[:200]},
        )

    if template_custom:
        return []

    # Fallback: legacy pipeline (skill-tuned, still stable)
    return _legacy_generate(
        jd_text=jd_text,
        cv_text=cv_text,
        difficulty=difficulty,
        question_count=n,
        model=model,
        skills=skills,
        coach_hints=coach_hints,
        experience=experience,
        avoid_history=avoid_history,
        domain_categories=domain_categories,
        raw_passthrough=raw_passthrough,
        temperature=temperature,
        variety_seed=variety_seed,
        interview_mode=canonical,
        template_custom=template_custom,
        validation_skills=val_skills,
        strict_validate=strict_validate,
    )


def _legacy_generate(
    *,
    jd_text: str,
    cv_text: str,
    difficulty: str,
    question_count: int,
    model: str,
    skills: List[str],
    coach_hints: str,
    experience: str,
    avoid_history: Optional[List[str]],
    domain_categories: Optional[list],
    raw_passthrough: bool,
    temperature: float,
    variety_seed: str,
    interview_mode: str = "technical",
    template_custom: bool = False,
    validation_skills: Optional[List[str]] = None,
    strict_validate: bool = True,
) -> List[str]:
    """Single-template OpenAI generation; fallback is non-OpenAI question templating."""
    from ai import generate_questions_fallback

    n = max(1, min(int(question_count or 1), 50))
    jd = jd_text or ""
    val_skills = list(validation_skills) if validation_skills is not None else list(skills or [])

    # Prefer OpenAI with only the template prompt (coach_hints) to satisfy
    # "single prompt / no layered prompts" requirement.
    from openai_client import openai_key_configured

    has_ai = openai_key_configured("question")
    safe = str(os.getenv("INTERVIEW_SAFE_MODE", "false")).lower() in {"1", "true", "yes", "on"}
    if has_ai and not safe:
        user_prompt = _single_template_prompt(template_prompt=coach_hints, question_count=n)
        messages = [{"role": "user", "content": user_prompt}]
        try:
            text = chat_completion_with_retry(
                messages=messages,
                model=model,
                temperature=temperature,
                call_type="generate_questions_template_only",
                db_target=_db_target(),
                difficulty=difficulty,
                selected_skills=skills,
            )
            parsed = parse_questions_from_json_response(text, expected_count=n)
            try:
                from prompt_builder import validate_questions

                accepted, _rejected = validate_questions(
                    parsed, val_skills, difficulty, strict=strict_validate
                )
            except Exception:
                accepted = parsed
            if template_custom and parsed and len(accepted) < max(1, n // 2):
                return parsed[:n]
            if len(accepted) >= max(1, n // 2):
                return accepted[:n]
        except Exception:
            pass

    if template_custom:
        return []

    # Non-OpenAI fallback to keep interviews operational even if the model
    # call fails or is disabled.
    return generate_questions_fallback(jd, cv_text, difficulty, n, required_skills=skills)
