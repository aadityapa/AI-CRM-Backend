"""Smart auto-advance interview settings (template weights → session meta → /next)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


DEFAULTS = {
    "auto_advance_enabled": True,
    "initial_response_wait_sec": 5,
    "silence_detection_sec": 3,
    "no_response_countdown_sec": 3,
    "max_no_response_warnings": 3,
    "auto_skip_enabled": True,
    "voice_commands_enabled": True,
    "confirmation_before_next_sec": 3,
    "minimum_answer_words": 5,
    "minimum_speech_duration_sec": 2,
    "speech_energy_threshold": 0.038,
    "speech_confirm_ms": 400,
}


def _as_bool(val: Any, default: bool) -> bool:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("0", "false", "off", "no", "disabled"):
        return False
    if s in ("1", "true", "on", "yes", "enabled"):
        return True
    return default


def _clamp_sec(raw: Any, fallback: int, lo: int, hi: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = fallback
    return max(lo, min(hi, n))


def _clamp_float(raw: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        n = float(raw)
    except (TypeError, ValueError):
        n = fallback
    return max(lo, min(hi, n))


def resolve_auto_advance_settings(weights: dict | None = None, meta: dict | None = None) -> dict:
    w = weights if isinstance(weights, dict) else {}
    m = meta if isinstance(meta, dict) else {}

    def _pick(snake: str, camel: str, default: Any) -> Any:
        if snake in m and m[snake] is not None:
            return m[snake]
        if camel in w and w[camel] is not None:
            return w[camel]
        if snake in w and w[snake] is not None:
            return w[snake]
        return default

    enabled = _as_bool(_pick("auto_advance_enabled", "autoAdvanceEnabled", DEFAULTS["auto_advance_enabled"]), False)

    return {
        "enabled": enabled,
        "initial_response_wait_sec": _clamp_sec(
            _pick("initial_response_wait_sec", "initialResponseWaitSec", DEFAULTS["initial_response_wait_sec"]),
            DEFAULTS["initial_response_wait_sec"],
            2,
            30,
        ),
        "silence_detection_sec": _clamp_sec(
            _pick("silence_detection_sec", "silenceDetectionSec", DEFAULTS["silence_detection_sec"]),
            DEFAULTS["silence_detection_sec"],
            2,
            15,
        ),
        "no_response_countdown_sec": _clamp_sec(
            _pick("no_response_countdown_sec", "noResponseCountdownSec", DEFAULTS["no_response_countdown_sec"]),
            DEFAULTS["no_response_countdown_sec"],
            2,
            15,
        ),
        "max_no_response_warnings": _clamp_sec(
            _pick("max_no_response_warnings", "maxNoResponseWarnings", DEFAULTS["max_no_response_warnings"]),
            DEFAULTS["max_no_response_warnings"],
            1,
            5,
        ),
        "auto_skip_enabled": _as_bool(
            _pick("auto_skip_enabled", "autoSkipEnabled", DEFAULTS["auto_skip_enabled"]),
            DEFAULTS["auto_skip_enabled"],
        ),
        "voice_commands_enabled": _as_bool(
            _pick("voice_commands_enabled", "voiceCommandsEnabled", DEFAULTS["voice_commands_enabled"]),
            DEFAULTS["voice_commands_enabled"],
        ),
        "confirmation_before_next_sec": _clamp_sec(
            _pick("confirmation_before_next_sec", "confirmationBeforeNextSec", DEFAULTS["confirmation_before_next_sec"]),
            DEFAULTS["confirmation_before_next_sec"],
            0,
            10,
        ),
        "minimum_answer_words": _clamp_sec(
            _pick("minimum_answer_words", "minimumAnswerWords", DEFAULTS["minimum_answer_words"]),
            DEFAULTS["minimum_answer_words"],
            1,
            30,
        ),
        "minimum_speech_duration_sec": _clamp_sec(
            _pick("minimum_speech_duration_sec", "minimumSpeechDurationSec", DEFAULTS["minimum_speech_duration_sec"]),
            DEFAULTS["minimum_speech_duration_sec"],
            1,
            30,
        ),
        "speech_energy_threshold": _clamp_float(
            _pick("speech_energy_threshold", "speechEnergyThreshold", DEFAULTS["speech_energy_threshold"]),
            DEFAULTS["speech_energy_threshold"],
            0.01,
            0.12,
        ),
        "speech_confirm_ms": _clamp_sec(
            _pick("speech_confirm_ms", "speechConfirmMs", DEFAULTS["speech_confirm_ms"]),
            DEFAULTS["speech_confirm_ms"],
            300,
            500,
        ),
    }


def stamp_auto_advance_settings(meta: dict, weights: dict | None = None) -> None:
    cfg = resolve_auto_advance_settings(weights, meta)
    meta["auto_advance_enabled"] = cfg["enabled"]
    meta["initial_response_wait_sec"] = cfg["initial_response_wait_sec"]
    meta["silence_detection_sec"] = cfg["silence_detection_sec"]
    meta["no_response_countdown_sec"] = cfg["no_response_countdown_sec"]
    meta["max_no_response_warnings"] = cfg["max_no_response_warnings"]
    meta["auto_skip_enabled"] = cfg["auto_skip_enabled"]
    meta["voice_commands_enabled"] = cfg["voice_commands_enabled"]
    meta["confirmation_before_next_sec"] = cfg["confirmation_before_next_sec"]
    meta["minimum_answer_words"] = cfg["minimum_answer_words"]
    meta["minimum_speech_duration_sec"] = cfg["minimum_speech_duration_sec"]
    meta["speech_energy_threshold"] = cfg["speech_energy_threshold"]
    meta["speech_confirm_ms"] = cfg["speech_confirm_ms"]
    meta.setdefault("auto_advance_turn_events", [])


def auto_advance_api_payload(meta: dict | None) -> dict:
    cfg = resolve_auto_advance_settings(meta=meta)
    return {
        "enabled": cfg["enabled"],
        "initial_response_wait_sec": cfg["initial_response_wait_sec"],
        "silence_detection_sec": cfg["silence_detection_sec"],
        "no_response_countdown_sec": cfg["no_response_countdown_sec"],
        "max_no_response_warnings": cfg["max_no_response_warnings"],
        "auto_skip_enabled": cfg["auto_skip_enabled"],
        "voice_commands_enabled": cfg["voice_commands_enabled"],
        "confirmation_before_next_sec": cfg["confirmation_before_next_sec"],
        "minimum_answer_words": cfg["minimum_answer_words"],
        "minimum_speech_duration_sec": cfg["minimum_speech_duration_sec"],
        "speech_energy_threshold": cfg["speech_energy_threshold"],
        "speech_confirm_ms": cfg["speech_confirm_ms"],
    }


def record_auto_advance_turn_event(
    session: dict,
    *,
    question_index: int,
    question_text: str,
    answer_transcript: str = "",
    event: dict | None = None,
) -> None:
    """Persist auto-advance telemetry on the session for reports and audits."""
    meta = session.setdefault("meta", {})
    events = meta.setdefault("auto_advance_turn_events", [])
    if not isinstance(events, list):
        events = []
        meta["auto_advance_turn_events"] = events
    ev = dict(event or {})
    now = datetime.now(timezone.utc).isoformat()
    events.append(
        {
            "question_index": int(question_index) + 1,
            "question_number": int(question_index) + 1,
            "question_text": str(question_text or "")[:900],
            "answer_transcript": str(answer_transcript or "")[:4000],
            "start_speaking_at": ev.get("start_speaking_at"),
            "end_speaking_at": ev.get("end_speaking_at") or now,
            "silence_duration_ms": ev.get("silence_duration_ms"),
            "auto_submitted": bool(ev.get("auto_submitted")),
            "skipped": bool(ev.get("skipped")),
            "skipped_flag": bool(ev.get("skipped")),
            "trigger": str(ev.get("trigger") or "")[:64],
            "evaluation_started": bool(ev.get("evaluation_started")),
            "recorded_at_utc": now,
        }
    )


def parse_auto_advance_meta(raw: str) -> dict:
    if not raw or not str(raw).strip():
        return {}
    try:
        data = json.loads(str(raw))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
