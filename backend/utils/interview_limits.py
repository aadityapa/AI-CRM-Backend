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
    """THE TEMPLATE WINS (13 Oct 2026); the schedule's copy is the fallback.

    A schedule stores a snapshot of the scheduling form, and the dashboard's
    HR Setup form never sends num_q — so the snapshot said 5 whatever the
    template said. Never inflate beyond the clamp."""
    cfg = invite_cfg if isinstance(invite_cfg, dict) else {}
    j = job if isinstance(job, dict) else {}
    raw = (
        j.get("numQ")
        or j.get("num_q")
        or cfg.get("num_q")
        or env_num
        or skills_fallback
        or 5
    )
    return clamp_count_mode_questions(raw)


TIMING_MODES = ("count", "time")


def _positive_int(raw: object) -> int:
    try:
        n = int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def template_timing(invite_cfg: dict | None, job: dict | None) -> tuple[str, int]:
    """How the interview ends: ``(timing_mode, raw time limit in seconds)``.

    THE TEMPLATE WINS (13 Oct 2026). A schedule keeps a copy of the scheduling
    form, and that copy used to decide: the dashboard's HR Setup page posts
    only the template and the skills, so every schedule it made said
    "count, no time limit" — a template set to "Time limit: 10 min" ran with no
    clock, no time warnings, and ended when the questions ran out. The
    schedule's copy now counts only when there is no template, or the template
    names no mode. A time-mode template with no minutes falls back to the
    schedule's number; the caller still applies its own clamp.
    """
    cfg = invite_cfg if isinstance(invite_cfg, dict) else {}
    j = job if isinstance(job, dict) else {}
    tpl_mode = str(j.get("timingMode") or j.get("timing_mode") or "").strip().lower()
    if tpl_mode in TIMING_MODES:
        if tpl_mode == "count":
            return "count", 0
        limit = _positive_int(j.get("timeLimitSec") or j.get("time_limit_sec"))
        return "time", limit or _positive_int(cfg.get("time_limit_sec"))
    mode = str(cfg.get("timing_mode") or "count").strip().lower()
    if mode not in TIMING_MODES:
        mode = "count"
    return mode, (_positive_int(cfg.get("time_limit_sec")) if mode == "time" else 0)


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
