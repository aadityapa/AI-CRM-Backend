"""
Process-wide OpenAI clients keyed by purpose (SDK is thread-safe).

Env vars (each falls back to OPENAI_API_KEY when unset):
  OPENAI_API_KEY              — master / fallback (OCR, embeddings, etc.)
  OPENAI_TTS_API_KEY          — text-to-speech (/candidate/tts)
  OPENAI_QUESTION_API_KEY     — question generation & follow-ups
  OPENAI_EVAL_API_KEY         — interview evaluation & per-question scoring
  OPENAI_TRANSCRIBE_API_KEY   — voice-to-text (/candidate/transcribe)
  OPENAI_ATS_API_KEY          — ATS resume↔JD semantic review

Aliases (same purpose, either name works):
  OPENAI_API_KEY_TTS, OPENAI_API_KEY_QUESTIONS, OPENAI_API_KEY_EVALUATION,
  OPENAI_API_KEY_TRANSCRIBE, OPENAI_API_KEY_ATS

"ats" is deliberately its own purpose rather than sharing "eval": resume scanning
is bursty and high-volume (scan-all over a requirement), so keeping it on a
separate key means its spend and rate limits are visible and cappable on their
own, and an ATS burst cannot exhaust the quota a live interview depends on.
It falls back to "eval" and then to OPENAI_API_KEY, so nothing breaks if unset.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Literal

from openai import OpenAI

OpenAIPurpose = Literal["default", "tts", "question", "eval", "transcribe", "ats"]

_PURPOSE_ENV: dict[OpenAIPurpose, tuple[str, ...]] = {
    "default": ("OPENAI_API_KEY",),
    "tts": ("OPENAI_TTS_API_KEY", "OPENAI_API_KEY_TTS"),
    "question": ("OPENAI_QUESTION_API_KEY", "OPENAI_API_KEY_QUESTIONS"),
    "eval": ("OPENAI_EVAL_API_KEY", "OPENAI_API_KEY_EVALUATION"),
    "transcribe": ("OPENAI_TRANSCRIBE_API_KEY", "OPENAI_API_KEY_TRANSCRIBE"),
    "ats": ("OPENAI_ATS_API_KEY", "OPENAI_API_KEY_ATS"),
}

#: Purposes that borrow another purpose's key before falling back to the master.
#: ATS used to run on the "eval" key, so an install that only sets that keeps
#: working after this change.
_PURPOSE_FALLBACK: dict[OpenAIPurpose, OpenAIPurpose] = {"ats": "eval"}


def _first_env(*names: str) -> str:
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value and value != "your_key_here":
            return value
    return ""


def _resolve_api_key(purpose: OpenAIPurpose) -> str:
    specific = _first_env(*_PURPOSE_ENV[purpose])
    if specific:
        return specific
    borrowed = _PURPOSE_FALLBACK.get(purpose)
    if borrowed:
        inherited = _first_env(*_PURPOSE_ENV[borrowed])
        if inherited:
            return inherited
    return _first_env("OPENAI_API_KEY")


def openai_key_source(purpose: OpenAIPurpose = "default") -> str | None:
    """Which env var is actually supplying this purpose's key.

    Reported by scripts/diagnose_ats.py so "which key is the ATS scan using?"
    has an answer that does not involve printing the key.
    """
    for name in _PURPOSE_ENV[purpose]:
        if _first_env(name):
            return name
    borrowed = _PURPOSE_FALLBACK.get(purpose)
    if borrowed:
        for name in _PURPOSE_ENV[borrowed]:
            if _first_env(name):
                return f"{name} (inherited from '{borrowed}')"
    return "OPENAI_API_KEY (master fallback)" if _first_env("OPENAI_API_KEY") else None


def openai_key_configured(purpose: OpenAIPurpose = "default") -> bool:
    key = _resolve_api_key(purpose)
    return bool(key and key != "your_key_here")


@lru_cache(maxsize=8)
def get_openai_client(purpose: OpenAIPurpose = "default") -> OpenAI:
    api_key = _resolve_api_key(purpose)
    base_url = (os.getenv("OPENAI_BASE_URL") or "").strip()
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)
