"""Two-way AI interview — the conversation layer (9 Oct 2026).

The interview used to be a quiz: a fixed list of questions read out one after
another. This module makes it feel like a conversation, without changing how
the interview is scored, recorded or proctored. Everything is OPT-IN per
template (`weights.conversation`), so a template that never set it behaves
exactly as before — and that includes every interview already scheduled.

  A  follow-ups      after an answer worth exploring, ONE follow-up question on
                     what the candidate just said is INSERTED (never replaces a
                     planned question; at most `max_followups` per interview,
                     never a follow-up on a follow-up)
  B  acknowledgement a short, neutral spoken lead-in before the next question
                     ("Thanks — you mentioned CAN timing; let's go deeper…"),
                     never praise or a verdict
  C  repeat/clarify  the candidate can ask for the question again or for it in
                     other words — without a hint at the answer
  D  probe           a very short answer earns one "can you give an example?"
  E  closing Q&A     at the end the candidate may ask about the role; answered
                     ONLY from the role description, the customer never named,
                     not scored
  F  live voice      the whole interview over OpenAI's realtime voice model
                     (`services/interview/realtime_voice.py`); the server keeps
                     the question list, the answers and the scoring

Follow-ups are real questions: they are inserted into `session["questions"]`,
answered and scored like any other. `meta["followups_inserted"]` raises the
question cap by the same amount (`candidate/service._question_cap`), so a
follow-up never costs the candidate a planned question.

Every model call here has a hard deadline and a deterministic fallback — a slow
or dead model costs the conversation its polish, never the interview its turn.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
from typing import Any

logger = logging.getLogger("karnex.interview.conversation")

#: The settings key inside the template's `weights` bag.
WEIGHTS_KEY = "conversation"
#: Where the resolved settings live on the session meta.
META_KEY = "conversation"

VOICE_STANDARD = "standard"
VOICE_LIVE = "live"
VOICE_MODES = (VOICE_STANDARD, VOICE_LIVE)

DEFAULT_MAX_FOLLOWUPS = 2
MAX_FOLLOWUPS_CAP = 5
#: An answer this short (in words) earns a probe when D is on.
SHORT_ANSWER_WORDS = 15
#: Clarify / repeat requests honoured per question (a loop is not a conversation).
MAX_CLARIFY_PER_QUESTION = 3
#: Questions the candidate may ask at the end.
MAX_CLOSING_QUESTIONS = 3
#: A follow-up is not started when a timed interview has less than this left.
FOLLOWUP_MIN_TIME_LEFT_S = 120
#: Hard deadline for the per-turn model call (lead-in + follow-up decision).
TURN_PLAN_TIMEOUT_S = 6.0
#: Deadline for a clarification / closing answer.
REPLY_TIMEOUT_S = 8.0
MAX_LEAD_IN_WORDS = 28

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}

#: A lead-in is an acknowledgement, never a verdict — these words would tell
#: the candidate how they did (and bias the answers that follow).
_EVALUATIVE = re.compile(
    r"\b(great|excellent|perfect|brilliant|impressive|awesome|fantastic|amazing|"
    r"correct|incorrect|wrong|right answer|good answer|nice answer|well done|exactly|"
    r"spot on|not quite|unfortunately)\b",
    re.I,
)

_FALLBACK_LEAD_INS = (
    "Thank you.",
    "Thanks for explaining that.",
    "Got it, thank you.",
    "Understood, thanks.",
    "Thank you for sharing that.",
    "Okay, thanks.",
)
_FALLBACK_TRANSITIONS = (
    "Let's move on to the next question.",
    "Here is the next one.",
    "Let's continue.",
    "Moving on.",
)
WARMUP_LEAD_IN = "Thanks for the introduction. Let's begin with the first question."
SKIP_LEAD_IN = "No problem, let's move on."
FOLLOWUP_TRANSITION = "Let me follow up on that."
PROBE_FALLBACK = "Could you walk me through a specific example of that from your own work?"
CLOSING_PROMPT = "Before we finish, do you have any questions about the role?"
CLOSING_GOODBYE = "Thank you for your time today. That completes the interview."
CLOSING_UNKNOWN = (
    "That's a good question for the recruitment team — they will be able to answer it "
    "when they get in touch with you."
)

_REPEAT_PATTERNS = (
    r"\b(can|could|would) you (please )?(repeat|say) (that|it|the question)( again)?\b",
    r"\brepeat (the|that|this) question\b",
    r"\b(please )?repeat( it| that)?( please)?\b",
    r"\bsay (that|it) again\b",
    r"\bcome again\b",
    r"\bpardon\b",
    r"\bsorry,? i (didn'?t|did not) (hear|catch)\b",
    r"\bone more time\b",
)
_CLARIFY_PATTERNS = (
    r"\b(can|could|would) you (please )?(clarify|explain|rephrase|elaborate on) (the|that|this) question\b",
    r"\b(can|could|would) you (please )?(rephrase|clarify)( it| that)?\b",
    r"\bwhat do you mean\b",
    r"\bwhat (does|is) (the|that|this) question (mean|asking)\b",
    r"\bi (didn'?t|did not|don'?t|do not) understand( the question| that)?\b",
    r"\bnot (sure|clear) what (you|the question) (mean|means|is asking)\b",
    r"\bin other words\b",
)
#: A request for a repeat is short; a long answer that happens to contain
#: "pardon" is an answer.
_INTENT_MAX_WORDS = 14


# ---------------------------------------------------------------------------
# Settings (PURE)
# ---------------------------------------------------------------------------

def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _as_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def conversation_settings(weights: Any) -> dict:
    """The template's conversation settings, normalised. Every switch is OFF
    unless the template turned it on — `enabled` says whether any is."""
    raw = weights.get(WEIGHTS_KEY) if isinstance(weights, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    voice = str(raw.get("voiceMode") or raw.get("voice_mode") or "").strip().lower()
    out = {
        "followups": _as_bool(raw.get("followups"), False),
        "max_followups": _as_int(raw.get("maxFollowups", raw.get("max_followups")),
                                 DEFAULT_MAX_FOLLOWUPS, 0, MAX_FOLLOWUPS_CAP),
        "probe_short_answers": _as_bool(raw.get("probeShortAnswers", raw.get("probe_short_answers")), False),
        "acknowledge": _as_bool(raw.get("acknowledge"), False),
        "clarify": _as_bool(raw.get("clarify"), False),
        "closing_qa": _as_bool(raw.get("closingQa", raw.get("closing_qa")), False),
        "voice_mode": voice if voice in VOICE_MODES else VOICE_STANDARD,
    }
    out["enabled"] = bool(
        out["followups"] or out["probe_short_answers"] or out["acknowledge"]
        or out["clarify"] or out["closing_qa"] or out["voice_mode"] == VOICE_LIVE
    )
    out["hide_names"] = []
    return out


def stamp_conversation_settings(meta: dict, weights: Any, *, customer_name: str = "") -> dict:
    """Copy the settings onto the session meta at bootstrap (both the invite and
    the HR-setup paths call this, so they can never disagree). The customer's
    name is remembered only so the AI can be stopped from SAYING it."""
    if not isinstance(meta, dict):
        return meta
    cfg = conversation_settings(weights)
    names = [n for n in [str(customer_name or "").strip()] if len(n) >= 3]
    cfg["hide_names"] = names
    meta[META_KEY] = cfg
    meta.setdefault("followup_indices", [])
    meta.setdefault("followups_inserted", 0)
    return meta


def conversation_of(meta: Any) -> dict:
    """The session's settings; a session from before this feature reads all-off."""
    raw = (meta or {}).get(META_KEY) if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return conversation_settings({})
    base = conversation_settings({WEIGHTS_KEY: {
        "followups": raw.get("followups"), "maxFollowups": raw.get("max_followups"),
        "probeShortAnswers": raw.get("probe_short_answers"), "acknowledge": raw.get("acknowledge"),
        "clarify": raw.get("clarify"), "closingQa": raw.get("closing_qa"), "voiceMode": raw.get("voice_mode"),
    }})
    base["hide_names"] = list(raw.get("hide_names") or [])
    return base


def is_live_voice(meta: Any) -> bool:
    return conversation_of(meta)["voice_mode"] == VOICE_LIVE


def client_payload(meta: Any) -> dict | None:
    """What the candidate page needs to know, or None when nothing is on — so a
    template without the feature sends the exact payload it always did."""
    cfg = conversation_of(meta)
    if not cfg["enabled"]:
        return None
    return {
        "acknowledge": cfg["acknowledge"],
        "clarify": cfg["clarify"],
        "closing_qa": cfg["closing_qa"],
        "voice_mode": cfg["voice_mode"],
        "max_clarify": MAX_CLARIFY_PER_QUESTION,
        "max_closing_questions": MAX_CLOSING_QUESTIONS,
        "closing_prompt": CLOSING_PROMPT,
        "closing_goodbye": CLOSING_GOODBYE,
    }


# ---------------------------------------------------------------------------
# Turn helpers (PURE)
# ---------------------------------------------------------------------------

def word_count(text: Any) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", str(text or "")))


def is_short_answer(text: Any) -> bool:
    n = word_count(text)
    return 0 < n < SHORT_ANSWER_WORDS


def detect_turn_intent(text: Any) -> str | None:
    """"repeat", "clarify" or None for what the candidate said. Conservative on
    purpose: only a SHORT utterance that is plainly a request counts, so a real
    answer is never swallowed."""
    t = " ".join(str(text or "").lower().split())
    if not t or word_count(t) > _INTENT_MAX_WORDS:
        return None
    if any(re.search(p, t) for p in _CLARIFY_PATTERNS):
        return "clarify"
    if any(re.search(p, t) for p in _REPEAT_PATTERNS):
        return "repeat"
    return None


def followup_indices(meta: Any) -> list[int]:
    out: list[int] = []
    for v in ((meta or {}).get("followup_indices") or []) if isinstance(meta, dict) else []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def is_followup_index(meta: Any, idx: int) -> bool:
    try:
        return int(idx) in set(followup_indices(meta))
    except (TypeError, ValueError):
        return False


def followups_inserted(meta: Any) -> int:
    try:
        return max(0, int((meta or {}).get("followups_inserted") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def followup_kind_wanted(meta: Any, *, answered_index: int, answer: str, is_warmup: bool,
                         is_skipped: bool, time_left_s: float | None) -> str | None:
    """"probe", "followup" or None — whether this answer may earn ONE inserted
    follow-up. Never after the warm-up, a skip, or another follow-up; never past
    the template's budget; never with under two minutes of a timed interview left."""
    cfg = conversation_of(meta)
    if not (cfg["followups"] or cfg["probe_short_answers"]):
        return None
    if is_warmup or is_skipped or is_followup_index(meta, answered_index):
        return None
    if followups_inserted(meta) >= cfg["max_followups"]:
        return None
    if time_left_s is not None and time_left_s < FOLLOWUP_MIN_TIME_LEFT_S:
        return None
    if cfg["probe_short_answers"] and is_short_answer(answer):
        return "probe"
    if cfg["followups"]:
        return "followup"
    return None


def scrub_names(text: str, names: list[str] | None) -> str:
    """The customer is never named to a candidate (first-touch rule, the same as
    the opening mail)."""
    out = str(text or "")
    for n in names or []:
        if n and len(n) >= 3:
            out = re.sub(re.escape(n), "the client", out, flags=re.I)
    return out


def sanitize_lead_in(text: Any, fallback_seed: int = 0) -> str:
    """One or two short sentences, no question, no verdict. Anything else falls
    back to a neutral line."""
    t = " ".join(str(text or "").split()).strip().strip('"').strip()
    if not t or "?" in t or _EVALUATIVE.search(t) or word_count(t) > MAX_LEAD_IN_WORDS:
        return fallback_lead_in(fallback_seed)
    if t[-1] not in ".!":
        t += "."
    return t


def fallback_lead_in(seed: int = 0) -> str:
    s = abs(int(seed or 0))
    return f"{_FALLBACK_LEAD_INS[s % len(_FALLBACK_LEAD_INS)]} {_FALLBACK_TRANSITIONS[s % len(_FALLBACK_TRANSITIONS)]}"


def insert_followup(session: dict, index: int, question: str) -> bool:
    """Insert a follow-up at `index` (the next turn). Indices recorded for later
    turns would shift; nothing here records any (warm-up is index 0, every
    other list is about turns already taken)."""
    q = " ".join(str(question or "").split()).strip()
    if not q:
        return False
    questions = session.setdefault("questions", [])
    idx = max(0, min(int(index), len(questions)))
    questions.insert(idx, q)
    meta = session.setdefault("meta", {})
    meta["followup_indices"] = sorted({*(i + 1 if i >= idx else i for i in followup_indices(meta)), idx})
    meta["followups_inserted"] = followups_inserted(meta) + 1
    return True


def set_lead_in(meta: dict, index: int, text: str) -> None:
    if text:
        meta["lead_in"] = {"index": int(index), "text": str(text)}
    else:
        meta.pop("lead_in", None)


def lead_in_for_index(meta: Any, index: int) -> str | None:
    raw = (meta or {}).get("lead_in") if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        if int(raw.get("index")) != int(index):
            return None
    except (TypeError, ValueError):
        return None
    text = str(raw.get("text") or "").strip()
    return text or None


# ---------------------------------------------------------------------------
# Model calls (bounded, never raise)
# ---------------------------------------------------------------------------

def _model_for(meta: Any) -> str:
    """The candidate waits on every call here — the FAST model (gpt-4o-mini
    while the interview runs on a reasoning model; `INTERVIEW_FAST_MODEL` /
    `INTERVIEW_FOLLOWUP_MODEL` / Settings override). 9 Oct 2026."""
    from services.ai_models import fast_interview_model

    return fast_interview_model(str((meta or {}).get("model") or ""))


def _chat_json(meta: Any, *, system: str, user: str, call_type: str, timeout_s: float,
               max_tokens: int = 220) -> dict | None:
    """One JSON chat call under a hard deadline. None on timeout / error / no key /
    safe mode — the caller always has a fallback."""
    if (meta or {}).get("safe_mode"):
        return None
    try:
        from openai_client import openai_key_configured
        if not openai_key_configured("question"):
            return None
    except Exception:
        return None

    def _call() -> dict | None:
        from ai import _client, _db_target
        from prompt_logger import tracked_chat_completion
        res = tracked_chat_completion(
            _client("question"),
            model=_model_for(meta),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.5,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            call_type=call_type,
            db_target=_db_target(),
        )
        text = (res.choices[0].message.content or "").strip()
        data = json.loads(text) if text else None
        return data if isinstance(data, dict) else None

    # The prompt logger reads the interview context from a ContextVar; a pool
    # thread does not inherit it, so it is copied across explicitly.
    import contextvars
    ctx = contextvars.copy_context()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(ctx.run, _call)
        return fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        logger.warning("conversation.%s timed out after %.1fs", call_type, timeout_s)
        return None
    except Exception as exc:
        logger.warning("conversation.%s failed: %s", call_type, str(exc)[:200])
        return None
    finally:
        pool.shutdown(wait=False)


_TURN_SYSTEM = (
    "You are the voice of a professional technical interviewer in a recorded job interview. "
    "Speak naturally, warmly and briefly in Indian-English business style. "
    "You NEVER judge the answer aloud: no praise, no 'correct'/'wrong', no hints. "
    "Reply ONLY with a JSON object."
)


def plan_turn(meta: dict, *, question: str, answer: str, recent_transcript: str,
              want_lead_in: bool, followup_kind: str | None, avoid: list[str] | None = None,
              seed: int = 0) -> dict:
    """The conversation's half of one turn: {"lead_in": str|None, "follow_up": str|None}.
    One model call covers both; on any failure the lead-in falls back to a neutral
    line and a probe falls back to a generic example request."""
    if not want_lead_in and not followup_kind:
        return {"lead_in": None, "follow_up": None}
    names = conversation_of(meta)["hide_names"]
    asks = []
    if want_lead_in:
        asks.append(
            '"lead_in": one short spoken sentence (max 20 words) acknowledging what the candidate said, '
            "neutral, may mention a topic they raised, no question, no judgement"
        )
    if followup_kind == "probe":
        asks.append(
            '"follow_up": the answer was very short — ONE question asking for a concrete example or more '
            "detail on exactly what they said (max 30 words)"
        )
    elif followup_kind == "followup":
        asks.append(
            '"follow_up": ONLY if the answer has something specific worth probing deeper (a decision, a '
            "trade-off, a tool, a claim), ONE natural follow-up question on it (max 30 words); otherwise "
            'an empty string. Never repeat an earlier question.'
        )
    user = (
        f"Role: {str(meta.get('job_title') or '').strip() or 'the open role'}\n"
        f"Question just asked: {question}\n"
        f"Candidate's answer (speech-to-text, may contain transcription errors): {answer[:1800]}\n"
        + (f"Recent conversation:\n{recent_transcript[:2000]}\n" if recent_transcript else "")
        + (f"Do not repeat any of these questions: {json.dumps((avoid or [])[-12:])}\n" if avoid else "")
        + "Return JSON with: " + "; ".join(asks) + "."
    )
    data = _chat_json(meta, system=_TURN_SYSTEM, user=user, call_type="conversation_turn",
                      timeout_s=TURN_PLAN_TIMEOUT_S) or {}
    lead_in = None
    if want_lead_in:
        lead_in = sanitize_lead_in(scrub_names(str(data.get("lead_in") or ""), names), seed)
    follow_up = None
    if followup_kind:
        fu = " ".join(scrub_names(str(data.get("follow_up") or ""), names).split()).strip()
        if fu and not fu.endswith("?"):
            fu = fu.rstrip(".") + "?"
        if fu and word_count(fu) > 45:
            fu = ""
        if not fu and followup_kind == "probe":
            fu = PROBE_FALLBACK
        follow_up = fu or None
    return {"lead_in": lead_in, "follow_up": follow_up}


def clarify_text(meta: dict, question: str, mode: str) -> str:
    """The spoken reply to "repeat that" / "what do you mean". A repeat is the
    question itself; a clarification says the same thing in plainer words and
    never narrows it towards the answer."""
    q = " ".join(str(question or "").split()).strip()
    if mode != "clarify" or not q:
        return f"Sure. {q}" if q else "Sure."
    data = _chat_json(
        meta,
        system=("You rephrase interview questions so a candidate understands what is being asked. "
                "Never add hints, examples of a correct answer, or extra scope. Reply ONLY with JSON."),
        user=(f"Original question: {q}\n"
              'Return {"text": "one or two plain sentences (max 45 words) that ask exactly the same thing in '
              'simpler words, starting with \'Let me put it another way:\'"}'),
        call_type="conversation_clarify", timeout_s=REPLY_TIMEOUT_S,
    ) or {}
    text = " ".join(scrub_names(str(data.get("text") or ""), conversation_of(meta)["hide_names"]).split())
    if not text or word_count(text) > 60:
        return f"Let me put it another way: {q}"
    return text


def answer_closing_question(meta: dict, candidate_question: str) -> str:
    """Answer the candidate's question about the role from the role description
    ONLY. Anything not covered there goes to the recruitment team. The customer
    is never named; salary, selection odds and feedback on the interview are
    never discussed."""
    cq = " ".join(str(candidate_question or "").split()).strip()[:600]
    if not cq:
        return CLOSING_UNKNOWN
    names = conversation_of(meta)["hide_names"]
    jd = scrub_names(str(meta.get("jd_text_plain") or meta.get("jd_text") or ""), names)[:4000]
    data = _chat_json(
        meta,
        system=(
            "You are a polite interviewer answering a candidate's question at the end of an interview. "
            "Answer ONLY from the role description given. Never name the client company. Never discuss salary, "
            "the candidate's performance, their chances, or the selection decision. If the role description does "
            "not answer it, say the recruitment team will help with that. Reply ONLY with JSON."
        ),
        user=(f"Role: {meta.get('job_title') or 'the open role'}\nRole description:\n{jd}\n\n"
              f"Candidate's question: {cq}\n"
              'Return {"answer": "max 60 words, spoken style"}'),
        call_type="conversation_closing", timeout_s=REPLY_TIMEOUT_S, max_tokens=180,
    ) or {}
    text = " ".join(scrub_names(str(data.get("answer") or ""), names).split())
    if not text or word_count(text) > 90:
        return CLOSING_UNKNOWN
    return text


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report_summary(session: dict) -> dict | None:
    """What the conversation added, for the report page: which questions were
    follow-ups, how often the candidate asked for a repeat, and what they asked
    about the role at the end. None for an interview without the feature."""
    meta = (session or {}).get("meta") or {}
    cfg = conversation_of(meta)
    if not cfg["enabled"]:
        return None
    questions = list((session or {}).get("questions") or [])
    followups = [
        {"index": i, "question": str(questions[i])}
        for i in followup_indices(meta) if 0 <= i < len(questions)
    ]
    clar = [x for x in (meta.get("clarifications") or []) if isinstance(x, dict)]
    closing = [
        {"question": str(x.get("question") or ""), "answer": str(x.get("answer") or "")}
        for x in (meta.get("closing_qa") or []) if isinstance(x, dict)
    ]
    return {
        "voice_mode": cfg["voice_mode"],
        "followups": followups,
        "repeats": sum(1 for x in clar if x.get("mode") == "repeat"),
        "clarifications": sum(1 for x in clar if x.get("mode") == "clarify"),
        "closing_qa": closing,
    }
