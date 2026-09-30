"""What one OpenAI call costs — the ONE price list (28 Sep 2026).

Every AI call the platform makes is priced HERE, at log time, and the figure is
stored on the prompt-log row (`ai_prompt_logs.cost_usd`) so a later price change
never rewrites history. Three kinds of call, three units:

  * chat  — text tokens in/out (per 1M tokens, the way OpenAI publishes them);
            cached prompt tokens at the cheaper `cached_input` rate when the
            response reports them
  * tts   — text in (per 1M tokens) + spoken audio OUT: per 1M audio tokens
            when the count is known, else per MEASURED minute (the MP3 length)
  * stt   — audio IN: per 1M audio tokens when the transcription response
            carries `usage` (the gpt-4o-*-transcribe models do), else per minute
            of the recording; plus the transcript's text tokens

Token rates are OpenAI's list prices; the per-minute rates are their own
"estimated $/min" for the same models and are the fallback only. Override any
rate without a deploy: `OPENAI_PRICING_JSON`, a JSON object keyed by model
prefix — `{"gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075}}`.
The older `OPENAI_PRICING_USD_PER_1K` (`{"prompt", "completion"}` per 1K tokens)
is still honoured for chat models.

PURE: no DB, no I/O beyond reading two env vars. `services/org_settings` holds
the USD→INR rate (`ai.usd_inr_rate`) so the CEO page can print rupees.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

#: Call kinds. Anything not tts/stt is priced as chat.
CHAT, TTS, STT = "chat", "tts", "stt"

#: call_type prefixes that are audio, not chat.
AUDIO_CALL_TYPES = {"tts": TTS, "tts_prewarm": TTS, "transcribe": STT}


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens (text in/out, cached in, audio in/out) and, as the
    fallback when a token count is unknown, USD per MINUTE of audio."""
    input: float = 0.0
    output: float = 0.0
    cached_input: float = 0.0
    audio_in_per_1m: float = 0.0
    audio_out_per_1m: float = 0.0
    audio_in_per_min: float = 0.0
    audio_out_per_min: float = 0.0


#: USD per 1M tokens (chat) / per minute (audio). Longest prefix wins, so
#: "gpt-4o-mini-tts" is matched before "gpt-4o-mini" before "gpt-4o".
DEFAULT_PRICES: dict[str, Price] = {
    "gpt-4o-mini-tts": Price(input=0.60, audio_out_per_1m=12.00, audio_out_per_min=0.015),
    "gpt-4o-mini-transcribe": Price(output=5.00, audio_in_per_1m=1.25, audio_in_per_min=0.003),
    "gpt-4o-transcribe": Price(output=10.00, audio_in_per_1m=2.50, audio_in_per_min=0.006),
    "tts-1-hd": Price(input=30.00),            # per 1M characters, billed as text
    "tts-1": Price(input=15.00),
    "whisper-1": Price(audio_in_per_min=0.006),
    "gpt-4o-mini": Price(input=0.15, output=0.60, cached_input=0.075),
    "gpt-4o": Price(input=2.50, output=10.00, cached_input=1.25),
    "gpt-4.1-mini": Price(input=0.40, output=1.60, cached_input=0.10),
    "gpt-4.1-nano": Price(input=0.10, output=0.40, cached_input=0.025),
    "gpt-4.1": Price(input=2.00, output=8.00, cached_input=0.50),
    "text-embedding-3-small": Price(input=0.02),
    "text-embedding-3-large": Price(input=0.13),
}
FALLBACK_MODEL = "gpt-4o-mini"

#: Rough words-per-minute of the OpenAI voices, used to turn a spoken question's
#: text into seconds when the stream's true length is not known.
TTS_CHARS_PER_SECOND = 15.0
#: Opus in a browser MediaRecorder runs at roughly this rate; used only when the
#: client did not send the recording's duration.
STT_BYTES_PER_SECOND = 6000.0


def _apply_overrides(table: dict[str, Price]) -> dict[str, Price]:
    raw = (os.getenv("OPENAI_PRICING_JSON") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            for k, v in (data.items() if isinstance(data, dict) else []):
                if not isinstance(v, dict):
                    continue
                base = table.get(str(k).lower(), Price())
                table[str(k).lower()] = Price(**{
                    f: float(v.get(f, getattr(base, f))) for f in Price.__dataclass_fields__
                })
        except Exception:
            pass
    legacy = (os.getenv("OPENAI_PRICING_USD_PER_1K") or "").strip()
    if legacy:
        try:
            data = json.loads(legacy)
            for k, v in (data.items() if isinstance(data, dict) else []):
                if isinstance(v, dict) and "prompt" in v and "completion" in v:
                    table[str(k).lower()] = Price(input=float(v["prompt"]) * 1000.0,
                                                  output=float(v["completion"]) * 1000.0)
        except Exception:
            pass
    return table


def pricing_table() -> dict[str, Price]:
    """Defaults + env overrides. Cheap; called per log write."""
    return _apply_overrides(dict(DEFAULT_PRICES))


def price_for(model: str, table: dict[str, Price] | None = None) -> Price:
    key = (model or "").strip().lower()
    table = table or pricing_table()
    best = ""
    for prefix in table:
        if key.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return table[best] if best else table.get(FALLBACK_MODEL, Price())


def call_kind(call_type: str) -> str:
    ct = (call_type or "").strip().lower()
    for prefix, kind in AUDIO_CALL_TYPES.items():
        if ct == prefix or ct.startswith(prefix + "_"):
            return kind
    return CHAT


def estimate_cost_usd(*, model: str, call_type: str = "", prompt_tokens: int = 0,
                      completion_tokens: int = 0, cached_tokens: int = 0,
                      audio_tokens: int = 0, audio_seconds: float = 0.0,
                      table: dict[str, Price] | None = None) -> float:
    """USD for one call. Text tokens are per 1M (`cached_tokens` — a subset of
    `prompt_tokens` — at the cached rate). Audio in the direction the kind
    implies (tts = out, stt = in) is priced per 1M audio tokens when
    `audio_tokens` is known, else per minute of `audio_seconds`. Never
    negative, never raises."""
    p = price_for(model, table)
    kind = call_kind(call_type)
    prompt = max(0, int(prompt_tokens or 0))
    cached = min(prompt, max(0, int(cached_tokens or 0))) if p.cached_input else 0
    usd = ((prompt - cached) * p.input + cached * p.cached_input
           + max(0, int(completion_tokens or 0)) * p.output) / 1_000_000.0
    a_tokens = max(0, int(audio_tokens or 0))
    minutes = max(0.0, float(audio_seconds or 0.0)) / 60.0
    if kind == TTS:
        usd += a_tokens * p.audio_out_per_1m / 1_000_000.0 if a_tokens and p.audio_out_per_1m \
            else minutes * p.audio_out_per_min
    elif kind == STT:
        usd += a_tokens * p.audio_in_per_1m / 1_000_000.0 if a_tokens and p.audio_in_per_1m \
            else minutes * p.audio_in_per_min
    return round(usd, 6)


def tts_seconds_for_text(text: str) -> float:
    return round(len((text or "").strip()) / TTS_CHARS_PER_SECOND, 2)


def stt_seconds_for_bytes(n_bytes: int) -> float:
    return round(max(0, int(n_bytes or 0)) / STT_BYTES_PER_SECOND, 2)


def text_tokens_estimate(text: str) -> int:
    """~4 characters per token — good enough for the pennies TTS text input costs."""
    return max(0, (len((text or "").strip()) + 3) // 4)


def usd_inr_rate() -> float:
    """Settings ▸ `ai.usd_inr_rate` (DB row → env → default). Never raises."""
    try:
        from services.org_settings import setting
        return max(1.0, float(setting("ai.usd_inr_rate") or 0) or 84.0)
    except Exception:
        return 84.0
