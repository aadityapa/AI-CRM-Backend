"""F — the live voice interview (9 Oct 2026).

The standard interview speaks each question with text-to-speech and listens
with speech-to-text: a turn-by-turn exchange. Live voice runs the whole
interview over OpenAI's realtime voice model instead, over WebRTC straight from
the candidate's browser — the candidate can interrupt, ask "sorry, what do you
mean?", and hear a natural reply.

What does NOT change, deliberately:

  * the SERVER owns the question list. The realtime model is told to ask only
    the questions the app hands it through the `get_next_question` /
    `submit_answer` tools, and the browser feeds each answer (the realtime
    transcript of the candidate) to the ordinary `/answer` route — so
    follow-ups, the question cap, the clock, scoring and the report are the
    same code as every other interview;
  * the recording, proctoring and integrity strikes run exactly as before;
  * our API key never reaches the browser: the server mints a short-lived
    client secret (`/v1/realtime/client_secrets`) scoped to one session.

If minting fails, the browser falls back to the standard voice for the rest of
the interview — nobody is stuck. `INTERVIEW_REALTIME_ENABLED=false` switches the
mode off platform-wide (templates fall back to the standard voice).

Cost: the browser reports each response's `usage` and the server logs it on the
AI Costs ledger (`call_type="realtime_voice"`, priced by `services/ai_pricing`).
"""
from __future__ import annotations

import os
from typing import Any

from services.interview import conversation as conv

DEFAULT_MODEL = "gpt-realtime-mini"
DEFAULT_VOICE = "marin"
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
#: How long the minted secret may be used to OPEN a call. The call itself lives
#: on after the secret expires; a reconnect mints a fresh one.
SECRET_TTL_S = 600
JD_CHARS = 2500
#: Per-response usage numbers above this are not believed (a tampered report).
MAX_TOKENS_PER_REPORT = 200_000

TOOLS: list[dict] = [
    {
        "type": "function",
        "name": "get_next_question",
        "description": "Get the question to ask now. Call this once at the very start, before greeting.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "type": "function",
        "name": "submit_answer",
        "description": (
            "Call when the candidate has clearly finished answering the current question, or has said they "
            "want to skip it. Returns the next question to ask, or that the interview is complete."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skipped": {"type": "boolean",
                            "description": "True only if the candidate chose not to answer."},
            },
            "required": ["skipped"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "end_interview",
        "description": "Call after you have said goodbye, once the interview is complete.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


class RealtimeUnavailable(RuntimeError):
    """Minting failed — the caller answers 503 and the browser uses the standard voice."""


def realtime_enabled() -> bool:
    return str(os.getenv("INTERVIEW_REALTIME_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def realtime_model() -> str:
    return (os.getenv("INTERVIEW_REALTIME_MODEL") or DEFAULT_MODEL).strip() or DEFAULT_MODEL


def realtime_voice() -> str:
    return (os.getenv("INTERVIEW_REALTIME_VOICE") or DEFAULT_VOICE).strip() or DEFAULT_VOICE


def _api_base() -> str:
    return ((os.getenv("OPENAI_BASE_URL") or "").strip() or "https://api.openai.com/v1").rstrip("/")


def instructions(meta: dict) -> str:
    """The realtime model's brief. PURE. The role description is included only
    so it can answer the candidate's closing questions (when the template
    allows them); the customer's name is scrubbed out of it."""
    cfg = conv.conversation_of(meta)
    names = cfg["hide_names"]
    title = str(meta.get("job_title") or "the open role").strip()
    profile = meta.get("candidate_profile") if isinstance(meta.get("candidate_profile"), dict) else {}
    first = str((profile or {}).get("name") or "").strip().split(" ")[0] or "the candidate"
    jd = conv.scrub_names(str(meta.get("jd_text_plain") or meta.get("jd_text") or ""), names)[:JD_CHARS]
    if cfg["closing_qa"]:
        closing = (
            f"When submit_answer says the interview is complete, say: \"{conv.CLOSING_PROMPT}\" "
            f"Answer at most {conv.MAX_CLOSING_QUESTIONS} questions about the role, ONLY from the role "
            "description below; anything it does not cover, say the recruitment team will help. Never discuss "
            "salary, their performance or the selection decision. Then thank them, say goodbye and call "
            "end_interview."
        )
    else:
        closing = ("When submit_answer says the interview is complete, thank the candidate, say goodbye and "
                   "call end_interview.")
    lines = [
        f"You are the AI interviewer of Karnex Orbit, conducting a recorded voice interview for the role "
        f"\"{title}\" with {first}.",
        "Speak clear Indian-English, warm and professional, in short sentences. Keep each of your turns brief.",
        "THE APP SUPPLIES EVERY QUESTION. Start by calling get_next_question, then greet the candidate in one "
        "sentence and ask that question. Ask ONLY questions the tools give you, one at a time, close to their "
        "wording. Never invent your own interview questions.",
        "After asking, listen and let the candidate finish. If the answer is very short or vague you may ask "
        "ONE brief request for an example or more detail before moving on.",
        "If the candidate asks you to repeat or explain the question, do so in plainer words WITHOUT hinting at "
        "the answer or narrowing it.",
        "When the candidate has finished answering, or clearly says they want to skip or move on, call "
        "submit_answer (skipped=true only if they chose not to answer). It returns the next question: give a "
        "very short neutral acknowledgement and ask it. A question marked as a follow-up should be asked as a "
        "natural follow-up to what they just said.",
        "NEVER evaluate: no praise, never say an answer is right or wrong, never coach, never answer the "
        "technical question yourself, never reveal scores. Never name the client company.",
        "If the candidate is silent for a long time, gently check once that they can hear you.",
        closing,
    ]
    if cfg["closing_qa"] and jd:
        lines.append(f"Role description (for the candidate's closing questions only):\n{jd}")
    return "\n".join(lines)


def session_config(meta: dict) -> dict:
    """The realtime session the minted secret is bound to. PURE."""
    return {
        "type": "realtime",
        "model": realtime_model(),
        "instructions": instructions(meta),
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                "transcription": {
                    "model": (os.getenv("INTERVIEW_REALTIME_TRANSCRIBE_MODEL") or DEFAULT_TRANSCRIBE_MODEL).strip(),
                    "language": "en",
                },
                "turn_detection": {"type": "semantic_vad", "eagerness": "low",
                                   "create_response": True, "interrupt_response": True},
                "noise_reduction": {"type": "near_field"},
            },
            "output": {"voice": realtime_voice()},
        },
        "tools": TOOLS,
        "tool_choice": "auto",
    }


def mint_client_secret(meta: dict, *, http_post=None) -> dict:
    """Mint a short-lived client secret for one realtime session. Raises
    RealtimeUnavailable on any failure (no key, disabled, OpenAI said no)."""
    if not realtime_enabled():
        raise RealtimeUnavailable("Live voice is switched off on this server.")
    from openai_client import _resolve_api_key

    key = _resolve_api_key("realtime")
    if not key:
        raise RealtimeUnavailable("No OpenAI key is configured for live voice.")
    body = {"expires_after": {"anchor": "created_at", "seconds": SECRET_TTL_S}, "session": session_config(meta)}
    url = f"{_api_base()}/realtime/client_secrets"
    if http_post is None:
        import httpx
        http_post = httpx.post
    try:
        res = http_post(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json=body, timeout=15.0)
    except Exception as exc:  # network
        raise RealtimeUnavailable(f"Could not reach the voice service: {type(exc).__name__}") from exc
    status = int(getattr(res, "status_code", 0) or 0)
    if status >= 400 or status == 0:
        detail = ""
        try:
            detail = str(res.text)[:200]
        except Exception:
            pass
        raise RealtimeUnavailable(f"The voice service refused the session ({status}). {detail}".strip())
    try:
        data = res.json()
    except Exception as exc:
        raise RealtimeUnavailable("The voice service sent an unreadable reply.") from exc
    secret = data.get("value") or ((data.get("client_secret") or {}) if isinstance(data.get("client_secret"), dict) else {}).get("value")
    if not secret:
        raise RealtimeUnavailable("The voice service sent no client secret.")
    return {
        "client_secret": secret,
        "expires_at": data.get("expires_at"),
        "model": realtime_model(),
        "voice": realtime_voice(),
        "calls_url": f"{_api_base()}/realtime/calls",
    }


def _int(v: Any) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_TOKENS_PER_REPORT, n))


def usage_numbers(usage: Any) -> dict:
    """Flatten a realtime `response.done` usage object into what the ledger
    prices. PURE; unknown shapes count as zero."""
    u = usage if isinstance(usage, dict) else {}
    ind = u.get("input_token_details") if isinstance(u.get("input_token_details"), dict) else {}
    outd = u.get("output_token_details") if isinstance(u.get("output_token_details"), dict) else {}
    in_total = _int(u.get("input_tokens"))
    out_total = _int(u.get("output_tokens"))
    audio_in = _int(ind.get("audio_tokens"))
    audio_out = _int(outd.get("audio_tokens"))
    text_in = _int(ind.get("text_tokens")) or max(0, in_total - audio_in)
    text_out = _int(outd.get("text_tokens")) or max(0, out_total - audio_out)
    cached = _int(ind.get("cached_tokens"))
    return {"text_in": text_in, "text_out": text_out, "audio_in": audio_in,
            "audio_out": audio_out, "cached": min(cached, text_in + audio_in)}


def log_usage(meta: dict, usage: Any, *, db_target: str = "") -> float:
    """Log one realtime response on the AI Costs ledger. Returns the USD priced."""
    nums = usage_numbers(usage)
    if not any(nums.values()):
        return 0.0
    from prompt_logger import log_openai_call

    if not db_target:
        # The same store every other AI call is logged to (the AI Costs report reads it).
        from ai import _db_target
        db_target = _db_target()
    entry = log_openai_call(
        db_target=db_target,
        call_type="realtime_voice",
        model=str((meta or {}).get("realtime_model") or realtime_model()),
        prompt_tokens=nums["text_in"],
        completion_tokens=nums["text_out"],
        total_tokens=sum(nums.values()) - nums["cached"],
        audio_tokens=nums["audio_in"],
        audio_out_tokens=nums["audio_out"],
        cached_tokens=nums["cached"],
        response_text="",
    )
    return float(entry.get("cost_usd") or 0.0)
