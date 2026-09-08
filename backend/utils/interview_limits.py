"""Configurable interview question count limits (replaces legacy 15-question cap)."""

from __future__ import annotations

# Manager-defined count-mode interviews (Ask by question count).
MAX_COUNT_MODE_QUESTIONS = 100
MIN_INTERVIEW_QUESTIONS = 1


def clamp_count_mode_questions(raw: int | str | float | None, *, default: int = 5) -> int:
    try:
        n = int(raw if raw is not None else default)
    except (TypeError, ValueError):
        n = default
    return max(MIN_INTERVIEW_QUESTIONS, min(n, MAX_COUNT_MODE_QUESTIONS))


def resolve_template_num_q(
    invite_cfg: dict | None,
    job: dict | None,
    *,
    env_num: str | None = None,
    skills_fallback: int = 5,
) -> int:
    """Prefer template / invite num_q; never inflate beyond clamp."""
    cfg = invite_cfg if isinstance(invite_cfg, dict) else {}
    j = job if isinstance(job, dict) else {}
    raw = (
        cfg.get("num_q")
        or j.get("numQ")
        or j.get("num_q")
        or env_num
        or skills_fallback
        or 5
    )
    return clamp_count_mode_questions(raw)


def pool_questions_for_timing(
    num_q: int,
    timing_mode: str,
    *,
    time_limit_sec: int = 0,
) -> int:
    """
    Count mode: exactly ``num_q`` scored questions in the initial pool.
    Time mode: estimate a larger prefetch pool (timer ends the session).
    """
    n = clamp_count_mode_questions(num_q)
    tm = str(timing_mode or "count").strip().lower() or "count"
    if tm != "time":
        return n
    est = 30
    if time_limit_sec > 0:
        est = max(15, min(50, (time_limit_sec // 90) + 10))
    return min(50, max(n, est))


def trim_questions_for_count_mode(
    questions: list,
    num_q: int,
    timing_mode: str,
    *,
    warmup_count: int = 0,
) -> list:
    """Ensure count-mode sessions never prefetch more than num_q + warmup slots."""
    if str(timing_mode or "count").strip().lower() != "count":
        return list(questions or [])
    cap = clamp_count_mode_questions(num_q) + max(0, int(warmup_count or 0))
    return list(questions or [])[:cap]
