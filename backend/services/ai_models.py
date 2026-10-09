"""Which OpenAI model runs what — the ONE place that knows model names (9 Oct 2026).

Since 8 Oct 2026 production runs AI interviews on `gpt-6-astra`. Before this
module four places decided the interview model (the HR form's hidden select,
the schedule notes, the CRM bridge's `CRM_AI_L1_MODEL`, `INTERVIEW_OPENAI_MODEL`)
and they disagreed. Now:

* `interview_model()` — Settings `ai.interview_model` → env
  `INTERVIEW_OPENAI_MODEL` → `gpt-4o-mini`. Decided by the SERVER when a session
  starts; a model stored in a schedule counts only with `"model_locked": true`
  (`resolve_session_model`).
* `fast_interview_model(session_model)` — the calls the candidate WAITS on
  between answers (follow-up plan, clarify, closing answer, inline follow-up).
  A reasoning model takes 3–5 s per call, so they default to gpt-4o-mini while the
  interview runs on one; quality-deciding calls (questions, evaluation, report,
  re-score) stay on the interview model.
* `ocr_model()` — reading an uploaded image; never the interview model.
* `model_label(id)` / `describe(id)` / `engine()` — what the UI prints.

Pure: no DB work at import. Settings reads go through `org_settings.setting`,
which is cached 60 s and never raises.
"""
from __future__ import annotations

import os
import re

DEFAULT_MODEL = "gpt-4o-mini"
PROVIDER = "OpenAI"

#: Models an admin may pick in Settings ▸ AI engine (and `validation_error`
#: refuses anything else). Order = the order of the picker.
SUPPORTED_INTERVIEW_MODELS: tuple[str, ...] = (
    "gpt-6-astra",
    "gpt-6.1-sol",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
)

#: Settings keys (registered in `org_settings.KEYS`).
INTERVIEW_MODEL_KEY = "ai.interview_model"
FAST_MODEL_KEY = "ai.interview_fast_model"
SHOW_TO_CANDIDATES_KEY = "ui.show_ai_model_to_candidates"
MODEL_KEYS = (INTERVIEW_MODEL_KEY, FAST_MODEL_KEY)

#: id prefix -> label. The LONGEST matching prefix wins.
MODEL_LABELS: dict[str, str] = {
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-5": "GPT-5",
    "gpt-4.1-mini": "GPT-4.1 mini",
    "gpt-4.1": "GPT-4.1",
    "gpt-4o-mini-tts": "GPT-4o mini TTS",
    "gpt-4o-mini-transcribe": "GPT-4o mini Transcribe",
    "gpt-4o-mini": "GPT-4o mini",
    "gpt-4o": "GPT-4o",
    "gpt-realtime-mini": "GPT Realtime mini",
    "gpt-realtime": "GPT Realtime",
}


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
    """The model AI interviews run on: Settings → env → gpt-4o-mini."""
    return _setting(INTERVIEW_MODEL_KEY) or _env("INTERVIEW_OPENAI_MODEL") or DEFAULT_MODEL


def fast_interview_model(session_model: str | None = None) -> str:
    """The model for calls the candidate waits on between answers.

    Settings `ai.interview_fast_model` → env `INTERVIEW_FAST_MODEL` → env
    `INTERVIEW_FOLLOWUP_MODEL` (the older name, still honoured) → gpt-4o-mini
    when the session runs on a reasoning model → the session model."""
    explicit = (_setting(FAST_MODEL_KEY) or _env("INTERVIEW_FAST_MODEL")
                or _env("INTERVIEW_FOLLOWUP_MODEL"))
    if explicit:
        return explicit
    session = (session_model or "").strip() or interview_model()
    return DEFAULT_MODEL if is_reasoning(session) else session


def ocr_model() -> str:
    """Model for reading text out of an uploaded image — a vision call that
    must keep its own model (the interview model may refuse its parameters)."""
    return _env("INTERVIEW_OCR_MODEL") or DEFAULT_MODEL


def resolve_session_model(cfg: dict | None) -> str:
    """The model a NEW session runs on. A model stored in a schedule / template
    counts only when the config says `"model_locked": true` (nothing sets it
    today — it is the hook for a future per-template choice); otherwise the
    server decides, so one switch moves every interview."""
    cfg = cfg if isinstance(cfg, dict) else {}
    stored = str(cfg.get("model") or "").strip()
    if stored and cfg.get("model_locked") is True:
        return stored
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
    try:
        from ai_help.assist import _model

        return _model()
    except Exception:
        return DEFAULT_MODEL


def realtime_model() -> str:
    try:
        from services.interview import realtime_voice

        return realtime_voice.realtime_model()
    except Exception:
        return "gpt-realtime-mini"


def engine() -> dict:
    """Every model the platform uses, by role — ids and labels only (never
    keys, base URLs or env names). Served by `GET /interview/ai-engine`."""
    interview = interview_model()
    return {
        "interview": describe(interview),
        "fast": describe(fast_interview_model(interview)),
        "live_voice": describe(realtime_model()),
        "transcription": describe(_env("OPENAI_TRANSCRIBE_MODEL") or "gpt-4o-mini-transcribe"),
        "voice": describe(_env("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"),
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
