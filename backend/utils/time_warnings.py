"""Interview timer warning thresholds (candidate UX, Jun 2026)."""

from __future__ import annotations

from typing import Any

DEFAULT_THRESHOLDS_SEC = {
    "5min": 300,
    "2min": 120,
    "1min": 60,
    "30sec": 30,
}

AUDIT_FIELD_BY_KEY = {
    "5min": "warning_shown_5min",
    "2min": "warning_shown_2min",
    "1min": "warning_shown_1min",
    "30sec": "warning_shown_30sec",
}


def _as_bool(val: Any, default: bool = True) -> bool:
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


def _clamp_threshold_sec(raw: Any, fallback: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = fallback
    return max(10, min(3600, n))


def resolve_time_warning_settings(weights: dict | None = None, meta: dict | None = None) -> dict:
    """
    Resolve enable flag + threshold seconds from template weights or session meta.
    Defaults: enabled ON, 300/120/60/30 seconds.
    """
    w = weights if isinstance(weights, dict) else {}
    m = meta if isinstance(meta, dict) else {}

    enable = m.get("enable_time_warnings")
    if enable is None:
        enable = w.get("enableTimeWarnings", w.get("enable_time_warnings"))
    enabled = _as_bool(enable, True)

    custom = w.get("timeWarningSec") or w.get("time_warning_sec") or m.get("time_warning_thresholds_sec") or {}
    if not isinstance(custom, dict):
        custom = {}

    thresholds = {
        key: _clamp_threshold_sec(custom.get(key), DEFAULT_THRESHOLDS_SEC[key])
        for key in DEFAULT_THRESHOLDS_SEC
    }

    tts = m.get("time_warnings_tts")
    if tts is None:
        tts = w.get("timeWarningsTts", w.get("time_warnings_tts"))
    tts_on = _as_bool(tts, True)

    return {
        "enabled": enabled,
        "thresholds_sec": thresholds,
        "tts_announcements": tts_on,
    }


def stamp_time_warning_settings(meta: dict, weights: dict | None = None) -> None:
    """Persist resolved settings on session meta for /next + audit."""
    cfg = resolve_time_warning_settings(weights, meta)
    meta["enable_time_warnings"] = cfg["enabled"]
    meta["time_warning_thresholds_sec"] = cfg["thresholds_sec"]
    meta["time_warnings_tts"] = cfg["tts_announcements"]
    meta.setdefault("time_warning_audit", {})


def time_warnings_api_payload(meta: dict | None) -> dict:
    cfg = resolve_time_warning_settings(meta=meta)
    return {
        "enabled": cfg["enabled"],
        "thresholds_sec": cfg["thresholds_sec"],
        "tts_announcements": cfg["tts_announcements"],
    }
