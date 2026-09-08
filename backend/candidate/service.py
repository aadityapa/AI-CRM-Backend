from __future__ import annotations

import time

from utils.auto_advance import auto_advance_api_payload
from utils.question_uniqueness import ensure_unique_served_question, record_question_registry, remember_asked_question
from utils.time_warnings import time_warnings_api_payload
from utils.warmup import (
    QUESTION_TYPE_INTRODUCTION,
    WARMUP_LABEL,
    WARMUP_NOTE,
    is_warmup_index,
    question_type_for_index,
)

def _evaluated_total(session: dict) -> int:
    """Total questions excluding the (optional) warmup, for UI progress display.

    Count-mode interviews use template ``meta.num_q`` so progress shows 10/10
    even when a legacy session prefetched a larger time-mode pool.
    """
    meta = session.get("meta", {}) or {}
    timing = str(meta.get("timing_mode") or "count").strip().lower() or "count"
    if timing == "count":
        try:
            nq = int(meta.get("num_q") or 0)
        except (TypeError, ValueError):
            nq = 0
        if nq > 0:
            return nq
    warm = meta.get("warmup_indices") or []
    return max(0, len(session.get("questions") or []) - len(warm))


def _evaluated_index(session: dict) -> int:
    """1-based index over evaluated questions (skips warmup positions)."""
    meta = session.get("meta", {}) or {}
    warm = {int(i) for i in (meta.get("warmup_indices") or []) if isinstance(i, (int, float, str)) and str(i).lstrip("-").isdigit()}
    cur = int(session.get("current") or 0)
    seen = 0
    for i in range(cur + 1):
        if i in warm:
            continue
        seen += 1
    return seen


def mark_interview_started(session: dict) -> float:
    """Stamp (once) when the candidate actually received their first question.

    The template's time limit has to be measured from somewhere, and it must be
    the server's clock: the browser timer can be paused, throttled in a
    background tab, or simply edited, and a candidate who reloads must not get a
    fresh 15 minutes.
    """
    meta = session.setdefault("meta", {})
    started = meta.get("interview_started_epoch")
    if not started:
        started = time.time()
        meta["interview_started_epoch"] = started
    return float(started)


def interview_elapsed_seconds(session: dict) -> float:
    meta = session.get("meta", {}) or {}
    started = meta.get("interview_started_epoch")
    if not started:
        return 0.0
    try:
        return max(0.0, time.time() - float(started))
    except (TypeError, ValueError):
        return 0.0


def interview_time_limit_seconds(session: dict) -> int:
    """The template's time limit, or 0 when it did not set one."""
    meta = session.get("meta", {}) or {}
    try:
        return max(0, int(meta.get("time_limit_sec") or 0))
    except (TypeError, ValueError):
        return 0


def time_limit_reached(session: dict) -> bool:
    limit = interview_time_limit_seconds(session)
    return bool(limit) and interview_elapsed_seconds(session) >= limit


def _question_cap(session: dict) -> int | None:
    """Max session index (warmup + scored) implied by the template's question count.

    Previously this applied ONLY to count-mode templates, and the time limit was
    enforced ONLY by the browser timer. That left both modes half-guarded: a
    time-mode interview had no question ceiling at all, and a count-mode one had
    no clock. A template that says "15 minutes or 10 questions" should end on
    whichever arrives first, so the cap is now read whenever the template
    supplies a question count, regardless of mode.
    """
    meta = session.get("meta", {}) or {}
    try:
        nq = int(meta.get("num_q") or 0)
    except (TypeError, ValueError):
        return None
    if nq <= 0:
        return None
    warm = meta.get("warmup_indices") or []
    return nq + len(warm)


#: Previous name, kept so existing callers and tests keep working. The behaviour
#: changed deliberately (it is no longer count-mode-only) — see _question_cap.
_count_mode_question_cap = _question_cap


def next_question_payload(session: dict) -> dict:
    meta = session.get("meta", {})
    skills = meta.get("jd_skills", []) or []
    cap = _question_cap(session)
    cur = int(session.get("current") or 0)

    # The template's two limits, enforced on the SERVER and whichever lands
    # first wins. The browser timer alone was not enough: it can be throttled in
    # a background tab, or reset by reloading the page.
    out_of_questions = cap is not None and cur >= cap
    out_of_time = time_limit_reached(session)
    if out_of_questions or out_of_time:
        session["completed"] = True
        meta["completion_reason"] = "time_limit" if out_of_time and not out_of_questions else "question_limit"
        out = {
            "message": "Interview completed",
            "skills": skills,
            "index": _evaluated_total(session),
            "total": _evaluated_total(session),
            "show_spoken_text": bool(meta.get("show_spoken_text", False)),
            "enable_transcript_input": bool(meta.get("enable_transcript_input", meta.get("show_spoken_text", False))),
            "mic_always_on": bool(meta.get("mic_always_on", False)),
            "timing_mode": str(meta.get("timing_mode") or "count"),
            "time_limit_sec": int(meta.get("time_limit_sec") or 0),
            "time_warnings": time_warnings_api_payload(meta),
            "session_difficulty": str(meta.get("session_difficulty") or meta.get("difficulty") or "medium"),
            "auto_advance": auto_advance_api_payload(meta),
        }
        out["completion_reason"] = str(meta.get("completion_reason") or "question_limit")
        if meta.get("last_turn_score") is not None:
            out["last_turn_score"] = meta.get("last_turn_score")
            out["last_turn_feedback"] = str(meta.get("last_turn_feedback") or "")[:500]
        return out
    if session["current"] >= len(session["questions"]):
        session["completed"] = True
        meta["completion_reason"] = "questions_exhausted"
        out = {
            "message": "Interview completed",
            "skills": skills,
            "index": _evaluated_total(session),
            "total": _evaluated_total(session),
            "show_spoken_text": bool(meta.get("show_spoken_text", False)),
            "enable_transcript_input": bool(meta.get("enable_transcript_input", meta.get("show_spoken_text", False))),
            "mic_always_on": bool(meta.get("mic_always_on", False)),
            "timing_mode": str(meta.get("timing_mode") or "count"),
            "time_limit_sec": int(meta.get("time_limit_sec") or 0),
            "time_warnings": time_warnings_api_payload(meta),
            "session_difficulty": str(meta.get("session_difficulty") or meta.get("difficulty") or "medium"),
            "auto_advance": auto_advance_api_payload(meta),
        }
        if meta.get("last_turn_score") is not None:
            out["last_turn_score"] = meta.get("last_turn_score")
            out["last_turn_feedback"] = str(meta.get("last_turn_feedback") or "")[:500]
        return out
    # A question is about to be served, so the interview is genuinely under way.
    # Start the clock here rather than at login: the candidate may sit on the
    # rules or device-test screen, and that time is not interview time.
    mark_interview_started(session)

    ensure_unique_served_question(session)
    q = session["questions"][session["current"]]
    remember_asked_question(session, q)
    qnum = int(session["current"]) + 1
    record_question_registry(
        session,
        question_number=qnum,
        question_text=q,
        status="asked",
        source=str(meta.get("question_source") or "dynamic"),
    )
    is_warm = is_warmup_index(meta, session["current"])
    # Warmup gets a distinct payload that the candidate UI uses to render a
    # "System Warmup" chip + "This response is not evaluated." subtitle.
    out = {
        "question": q,
        # For the evaluated pool, expose a clean 1-based index/total that hides
        # the warmup from progress UI ("Question 1/5" not "Question 2/6").
        # For the warmup itself we emit index=0 + total=evaluated_total so the
        # frontend can hide/replace the progress pill cleanly.
        "index": 0 if is_warm else _evaluated_index(session),
        "total": _evaluated_total(session),
        "skills": skills,
        "show_spoken_text": bool(meta.get("show_spoken_text", False)),
        "enable_transcript_input": bool(meta.get("enable_transcript_input", meta.get("show_spoken_text", False))),
        "mic_always_on": bool(meta.get("mic_always_on", False)),
        "timing_mode": str(meta.get("timing_mode") or "count"),
        "time_limit_sec": int(meta.get("time_limit_sec") or 0),
        "time_warnings": time_warnings_api_payload(meta),
        "session_difficulty": str(meta.get("session_difficulty") or meta.get("difficulty") or "medium"),
        "auto_advance": auto_advance_api_payload(meta),
        "question_source": str(meta.get("question_source") or "dynamic"),
        "is_warmup": bool(is_warm),
        "question_type": QUESTION_TYPE_INTRODUCTION if is_warm else question_type_for_index(meta, session["current"]),
        # Server-authoritative clock. The client counts down from this instead of
        # its own start time, so a reload or a throttled background tab cannot
        # hand the candidate extra minutes.
        "time_remaining_sec": (
            max(0, interview_time_limit_seconds(session) - int(interview_elapsed_seconds(session)))
            if interview_time_limit_seconds(session) else None
        ),
        "questions_remaining": (
            max(0, _question_cap(session) - int(session.get("current") or 0))
            if _question_cap(session) is not None else None
        ),
    }
    if is_warm:
        out["warmup_label"] = WARMUP_LABEL
        out["warmup_note"] = WARMUP_NOTE
    if meta.get("last_turn_score") is not None:
        out["last_turn_score"] = meta.get("last_turn_score")
        out["last_turn_feedback"] = str(meta.get("last_turn_feedback") or "")[:500]

    # Start synthesising the FOLLOWING question now, while the candidate is
    # still listening to and answering this one. By the time they submit, its
    # audio is already made and /candidate/tts returns it with no OpenAI call on
    # the critical path. Questions are generated in one batch up front, so the
    # next one's text is already known here.
    _prewarm_following_question(session)
    return out


def upcoming_question_text(session: dict) -> str | None:
    """The question that will be asked AFTER the current one, if known.

    Returns None at the end of the pool, or when an adaptive follow-up is due to
    replace the slot — in that case the text would change and warming it would
    be wasted work.
    """
    try:
        meta = session.get("meta", {}) or {}
        if meta.get("followup_mode") or meta.get("adaptive_next_question"):
            return None
        questions = session.get("questions") or []
        nxt = int(session.get("current") or 0) + 1
        cap = _question_cap(session)
        if cap is not None and nxt >= cap:
            return None
        if nxt >= len(questions):
            return None
        # No point synthesising audio the candidate will never reach.
        if time_limit_reached(session):
            return None
        text = str(questions[nxt] or "").strip()
        return text or None
    except Exception:
        return None


def _prewarm_following_question(session: dict) -> None:
    """Fire-and-forget TTS warm for the next question. Never raises."""
    try:
        text = upcoming_question_text(session)
        if not text:
            return
        from services.tts_prewarm import prewarm_tts
        prewarm_tts(text)
    except Exception:
        pass  # a missed prefetch only costs the usual latency

