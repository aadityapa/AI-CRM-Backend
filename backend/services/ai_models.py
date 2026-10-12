"""Which OpenAI model runs what — the ONE place that knows model names (9 Oct 2026).

⚠️ 13 Oct 2026 — user rule: "remove gpt-4o-mini from everywhere; use GPT-5.5 and
GPT-6 (Astra) for everything — forcefully". So every TEXT / VISION call runs on
one of `TEXT_MODELS`:

* `interview_model()` — questions, per-answer evaluation, the report, re-score:
  Settings `ai.interview_model` → env `INTERVIEW_OPENAI_MODEL` → **gpt-6-astra**.
* `fast_interview_model()` — the calls the candidate WAITS on between answers
  (follow-up plan, lead-in, clarify, closing answer, skill-bank top-up):
  Settings `ai.interview_fast_model` → env → **gpt-5.5** (sent with
  `reasoning_effort=low`, `prompt_logger.chat_params`).
* `workhorse_model()` — everything else that writes text (resume parsing, ATS
  review, strengths / weaknesses, Ask AI, the support bot …) → **gpt-5.5**.
* `ocr_model()` — reading an uploaded image → **gpt-5.5** (it takes image input).
* `assist_model()` — Ask AI uses function tools; GPT-6 Astra refuses tools on
  Chat Completions, so Ask AI is always GPT-5.5.

Any other text model named in a setting, an env line, a stored schedule or a
caller's default (gpt-4o-mini, gpt-4o, gpt-4.1 …) is REPLACED by
`force_text_model()` — `prompt_logger.tracked_chat_completion` runs every call
through it, so an old env line can never bring gpt-4o-mini back.

Audio cannot run on a text model — OpenAI ships no GPT-5.5 / GPT-6 voice:
* transcription → **gpt-transcribe** (`transcribe_model()`; any *-mini* / whisper
  value is upgraded);
* live voice → **gpt-realtime-2** (GPT-5-class reasoning; its streaming
  transcription is gpt-realtime-whisper);
* spoken questions → **gpt-4o-mini-tts**, OpenAI's only voice model that takes
  speaking instructions — the ONE "mini" left, because there is no alternative.

Pure: no DB work at import. Settings reads go through `org_settings.setting`,
which is cached 60 s and never raises.
"""
from __future__ import annotations

import os
import re

#: The interview's own model (questions, scoring, report).
INTERVIEW_DEFAULT = "gpt-6-astra"
#: Every other text / vision call.
WORKHORSE_MODEL = "gpt-5.5"
#: Kept for older imports: "the default text model".
DEFAULT_MODEL = WORKHORSE_MODEL
#: The ONLY text models the platform runs (13 Oct 2026 user rule).
TEXT_MODELS: tuple[str, ...] = (INTERVIEW_DEFAULT, WORKHORSE_MODEL)
#: Audio models (no GPT-5.5 / GPT-6 voice exists).
TRANSCRIBE_MODEL = "gpt-transcribe"
TTS_MODEL = "gpt-4o-mini-tts"
REALTIME_MODEL = "gpt-realtime-2"
REALTIME_TRANSCRIBE_MODEL = "gpt-realtime-whisper"
PROVIDER = "OpenAI"

#: Models an admin may pick in Settings ▸ AI engine (and `validation_error`
#: refuses anything else). Order = the order of the picker.
SUPPORTED_INTERVIEW_MODELS: tuple[str, ...] = TEXT_MODELS

#: Settings keys (registered in `org_settings.KEYS`).
INTERVIEW_MODEL_KEY = "ai.interview_model"
FAST_MODEL_KEY = "ai.interview_fast_model"
SHOW_TO_CANDIDATES_KEY = "ui.show_ai_model_to_candidates"
MODEL_KEYS = (INTERVIEW_MODEL_KEY, FAST_MODEL_KEY)

#: id prefix -> label. The LONGEST matching prefix wins.
MODEL_LABELS: dict[str, str] = {
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-5.5": "GPT-5.5",
    "gpt-5": "GPT-5",
    "gpt-transcribe": "GPT Transcribe",
    "gpt-realtime-whisper": "GPT Realtime Whisper",
    "gpt-realtime-2": "GPT Realtime 2",
    "gpt-4.1-mini": "GPT-4.1 mini",
    "gpt-4.1": "GPT-4.1",
    "gpt-4o-mini-tts": "GPT-4o mini TTS",
    "gpt-4o-mini-transcribe": "GPT-4o mini Transcribe",
    "gpt-4o-mini": "GPT-4o mini",
    "gpt-4o": "GPT-4o",
    "gpt-realtime-mini": "GPT Realtime mini",
    "gpt-realtime": "GPT Realtime",
}

_AUDIO_HINTS = ("tts", "transcribe", "whisper", "realtime", "audio", "embedding", "speech")


def force_text_model(model: str | None, default: str = WORKHORSE_MODEL) -> str:
    """THE rule (13 Oct 2026): a text / vision call runs on GPT-6 Astra or
    GPT-5.5, never anything else. A blank or any other model id becomes
    `default`. Audio / embedding ids are not text models and pass unchanged."""
    mid = (model or "").strip()
    if not mid:
        return default
    low = mid.lower()
    if any(h in low for h in _AUDIO_HINTS):
        return mid
    return mid if low in TEXT_MODELS else default


def _setting(key: str) -> str:
    try:
        from services.org_settings import setting

        return (setting(key) or "").strip()
    except Exception:  # never let a label or a model lookup fail
        return ""


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def is_reasoning(model: str | None) -> bool:
    from prompt_logger import is_reasoning_model

    return is_reasoning_model(model)


# ------------------------------------------------------------------ resolution


def interview_model() -> str:
    """The model AI interviews run on: Settings → env → GPT-6 Astra (only
    GPT-6 Astra or GPT-5.5 are accepted — anything else means Astra)."""
    raw = _setting(INTERVIEW_MODEL_KEY) or _env("INTERVIEW_OPENAI_MODEL")
    return force_text_model(raw, INTERVIEW_DEFAULT)


def fast_interview_model(session_model: str | None = None) -> str:
    """The model for calls the candidate waits on between answers:
    Settings `ai.interview_fast_model` → env `INTERVIEW_FAST_MODEL` /
    `INTERVIEW_FOLLOWUP_MODEL` → GPT-5.5. `session_model` is accepted for the
    callers that pass it; it no longer decides anything."""
    explicit = (_setting(FAST_MODEL_KEY) or _env("INTERVIEW_FAST_MODEL")
                or _env("INTERVIEW_FOLLOWUP_MODEL"))
    return force_text_model(explicit, WORKHORSE_MODEL)


def workhorse_model(env_name: str | None = None) -> str:
    """Every other text call (resume parsing, ATS review, report text …):
    an optional env override, forced to GPT-5.5 / GPT-6 Astra."""
    return force_text_model(_env(env_name) if env_name else "", WORKHORSE_MODEL)


def ocr_model() -> str:
    """Reading text out of an uploaded image — GPT-5.5 takes image input."""
    return force_text_model(_env("INTERVIEW_OCR_MODEL"), WORKHORSE_MODEL)


def assist_model() -> str:
    """Ask AI. It calls function tools and GPT-6 Astra refuses tools on Chat
    Completions (8 Oct 2026), so it is GPT-5.5 whatever the env says."""
    chosen = force_text_model(_env("AI_ASSIST_MODEL"), WORKHORSE_MODEL)
    return WORKHORSE_MODEL if chosen == INTERVIEW_DEFAULT else chosen


def transcribe_model() -> str:
    """Speech-to-text: env `OPENAI_TRANSCRIBE_MODEL` → gpt-transcribe. A
    *-mini* or whisper value (the old defaults) is upgraded."""
    raw = _env("OPENAI_TRANSCRIBE_MODEL")
    low = raw.lower()
    if not raw or "mini" in low or low.startswith("whisper"):
        return TRANSCRIBE_MODEL
    return raw


def tts_model() -> str:
    """Spoken questions. OpenAI has no GPT-5.5 / GPT-6 voice; gpt-4o-mini-tts is
    its only model that follows speaking instructions (the Indian-English voice)."""
    return _env("OPENAI_TTS_MODEL") or TTS_MODEL


def resolve_session_model(cfg: dict | None) -> str:
    """The model a NEW session runs on. A model stored in a schedule / template
    counts only when the config says `"model_locked": true` (nothing sets it
    today) — and even then only GPT-6 Astra / GPT-5.5."""
    cfg = cfg if isinstance(cfg, dict) else {}
    stored = str(cfg.get("model") or "").strip()
    if stored and cfg.get("model_locked") is True:
        return force_text_model(stored, interview_model())
    return interview_model()


def show_to_candidates() -> bool:
    raw = _setting(SHOW_TO_CANDIDATES_KEY) or _env("SHOW_AI_MODEL_TO_CANDIDATES") or "true"
    return raw.lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------- labels


def _prettify(model_id: str) -> str:
    parts = [p for p in re.split(r"[-_\s]+", model_id) if p]
    if not parts:
        return model_id
    out: list[str] = []
    for i, part in enumerate(parts):
        low = part.lower()
        if i == 0 and low == "gpt":
            out.append("GPT")
        elif i == 1 and out and out[0] == "GPT":
            out[-1] = f"GPT-{part}"
        elif low in ("mini", "nano"):
            out.append(low)
        else:
            out.append(part[:1].upper() + part[1:])
    return " ".join(out)


def model_label(model_id: str | None) -> str:
    """Human name for a model id — never blank ("gpt-7-nova" → "GPT-7 Nova")."""
    mid = (model_id or "").strip()
    if not mid:
        return "OpenAI"
    low = mid.lower()
    best = ""
    for prefix in MODEL_LABELS:
        if low.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return MODEL_LABELS[best] if best else _prettify(mid)


def describe(model_id: str | None) -> dict:
    mid = (model_id or "").strip()
    return {"id": mid, "label": model_label(mid), "provider": PROVIDER,
            "reasoning": is_reasoning(mid)}


def record_models(record: dict | None) -> dict:
    """Which model asked the questions (`model`) and which scored the report
    (`evaluation_model`) of one interview record, with labels. Records written
    before 9 Oct 2026 carry no `evaluation_model` — it then equals `model`."""
    rec = record if isinstance(record, dict) else {}
    report = rec.get("report") if isinstance(rec.get("report"), dict) else {}
    model = str(rec.get("model") or "").strip()
    evaluation = str(report.get("evaluation_model") or rec.get("evaluation_model") or "").strip() or model
    return {
        "model": model,
        "model_label": model_label(model) if model else "",
        "evaluation_model": evaluation,
        "evaluation_model_label": model_label(evaluation) if evaluation else "",
    }


def _assist_model() -> str:
    return assist_model()


def realtime_model() -> str:
    """Live voice: env `INTERVIEW_REALTIME_MODEL` → gpt-realtime-2 (a *-mini*
    value is upgraded)."""
    raw = _env("INTERVIEW_REALTIME_MODEL")
    if not raw or "mini" in raw.lower():
        return REALTIME_MODEL
    return raw


def realtime_transcribe_model() -> str:
    raw = _env("INTERVIEW_REALTIME_TRANSCRIBE_MODEL")
    if not raw or "mini" in raw.lower():
        return REALTIME_TRANSCRIBE_MODEL
    return raw


def engine() -> dict:
    """Every model the platform uses, by role — ids and labels only (never
    keys, base URLs or env names). Served by `GET /interview/ai-engine`."""
    interview = interview_model()
    return {
        "interview": describe(interview),
        "fast": describe(fast_interview_model(interview)),
        "live_voice": describe(realtime_model()),
        "transcription": describe(transcribe_model()),
        "voice": describe(tts_model()),
        "ask_ai": describe(_assist_model()),
        "ocr": describe(ocr_model()),
        "show_to_candidates": show_to_candidates(),
        "supported": [describe(m) for m in SUPPORTED_INTERVIEW_MODELS],
    }


def validation_error(key: str, value: str) -> str | None:
    """Settings save check for the two model keys (blank = fall back)."""
    if key not in MODEL_KEYS or not value:
        return None
    if value in SUPPORTED_INTERVIEW_MODELS:
        return None
    return f"{value} is not a supported interview model (choose one of: {', '.join(SUPPORTED_INTERVIEW_MODELS)})"
