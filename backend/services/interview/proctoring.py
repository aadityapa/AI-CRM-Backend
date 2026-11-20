"""Anti-cheating checks chosen per interview template (13 Nov 2026).

The business saw candidates cheating in AI interviews and asked for:

* a **gesture check** after the Device Check (both palms, then a random
  gesture we name) and a few random **gesture prompts** during the interview;
* closer **eye monitoring** — a short gaze calibration and a "reading" flag;
* **headphones** connected before the interview can start;
* **good lighting** — no interview in the dark or against a bright window;
* an optional recorded **room scan** before the first question.

Each check is a switch on the template (``weights.proctoring``), so a template
that does not turn one on behaves exactly as before. The candidate's browser
does the measuring (it has the camera and the microphone); the server keeps the
settings, refuses nothing it cannot verify itself, and records what the page
reported so a reviewer sees it on the Integrity timeline and in the report.

Failure rules (user decision 13 Nov 2026):

* before the interview, a check set to ``require`` BLOCKS the start until it
  passes; ``warn`` lets the candidate continue and records the failure;
* during the interview nothing ends the interview — misses are recorded and
  flagged for review (the gaze / strike rules that already end interviews are
  unchanged).

Everything here is PURE (no I/O).
"""
from __future__ import annotations

from typing import Any, Iterable

WEIGHTS_KEY = "proctoring"
META_KEY = "proctoring"

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_REQUIRE = "require"
MODES = (MODE_OFF, MODE_WARN, MODE_REQUIRE)

#: At most this many random gesture prompts during one interview.
MAX_GESTURE_PROMPTS = 5


def _mode(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value in MODES:
        return value
    if value in ("true", "1", "yes", "on"):
        return MODE_REQUIRE
    return MODE_OFF


def _flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw or "").strip().lower() in ("true", "1", "yes", "on")


def _count(raw: Any, lo: int, hi: int) -> int:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, value))


def _raw(weights: Any) -> dict:
    if not isinstance(weights, dict):
        return {}
    raw = weights.get(WEIGHTS_KEY)
    return raw if isinstance(raw, dict) else {}


def clean_config(raw: Any) -> dict:
    """The stored shape (camelCase, like the rest of the template weights).
    Lenient: anything unknown is dropped, a bad value falls back to off."""
    src = raw if isinstance(raw, dict) else {}
    return {
        "headphones": _mode(src.get("headphones")),
        "lighting": _mode(src.get("lighting")),
        "gestureCheck": _flag(src.get("gestureCheck")),
        "gesturePrompts": _count(src.get("gesturePrompts"), 0, MAX_GESTURE_PROMPTS),
        "gazeCalibration": _flag(src.get("gazeCalibration")),
        "readingDetection": _flag(src.get("readingDetection")),
        "roomScan": _flag(src.get("roomScan")),
    }


def proctoring_settings(weights: Any) -> dict:
    """The settings the session and the candidate page use (snake_case)."""
    cfg = clean_config(_raw(weights))
    out = {
        "headphones": cfg["headphones"],
        "lighting": cfg["lighting"],
        "gesture_check": cfg["gestureCheck"],
        "gesture_prompts": cfg["gesturePrompts"],
        "gaze_calibration": cfg["gazeCalibration"],
        "reading_detection": cfg["readingDetection"],
        "room_scan": cfg["roomScan"],
    }
    out["enabled"] = any((
        out["headphones"] != MODE_OFF, out["lighting"] != MODE_OFF, out["gesture_check"],
        out["gesture_prompts"] > 0, out["gaze_calibration"], out["reading_detection"], out["room_scan"],
    ))
    return out


def public_config(weights: Any) -> dict:
    """What the candidate's page may read before sign-in (the rules page and the
    Device Check) — the same as the settings, nothing secret in them."""
    return proctoring_settings(weights)


def stamp(meta: dict, weights: Any) -> dict:
    """Copy the settings onto the session meta at bootstrap."""
    if isinstance(meta, dict):
        meta[META_KEY] = proctoring_settings(weights)
    return meta


def settings_of(meta: Any) -> dict:
    """The session's settings; a session from before this feature reads all-off."""
    raw = (meta or {}).get(META_KEY) if isinstance(meta, dict) else None
    if not isinstance(raw, dict):
        return proctoring_settings({})
    return proctoring_settings({WEIGHTS_KEY: {
        "headphones": raw.get("headphones"), "lighting": raw.get("lighting"),
        "gestureCheck": raw.get("gesture_check"), "gesturePrompts": raw.get("gesture_prompts"),
        "gazeCalibration": raw.get("gaze_calibration"), "readingDetection": raw.get("reading_detection"),
        "roomScan": raw.get("room_scan"),
    }})


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

#: Device-check results (before sign-in) the report summarises, in reading order.
PRE_CHECKS = ("headphones", "lighting", "gesture", "gaze_calibration")

#: During-interview event types the report counts.
DURING_TYPES = (
    "gesture_prompt_ok", "gesture_prompt_missed", "headphones_disconnected",
    "audio_device_changed", "low_light", "reading_pattern", "room_scan_started", "room_scan_done",
)


def _type(ev: Any) -> str:
    return str((ev or {}).get("type") or "").strip() if isinstance(ev, dict) else ""


def report_summary(settings: dict, device_log: Iterable | None, events: Iterable | None) -> dict | None:
    """The report's "Anti-cheating checks" section, or None when the template
    turned nothing on and nothing was recorded."""
    settings = settings if isinstance(settings, dict) else proctoring_settings({})
    pre: dict[str, dict] = {}
    for entry in device_log or []:
        if not isinstance(entry, dict):
            continue
        check = str(entry.get("check") or "")
        if check not in PRE_CHECKS:
            continue
        slot = pre.setdefault(check, {"attempts": 0, "passed": False, "last_reason": "", "detail": ""})
        slot["attempts"] += 1
        if entry.get("ok"):
            slot["passed"] = True
            slot["detail"] = str(entry.get("detail") or "")
        else:
            slot["last_reason"] = str(entry.get("reason") or "")
    counts = {t: 0 for t in DURING_TYPES}
    room_scan: dict[str, str] = {}
    reading_questions: list[str] = []
    for ev in events or []:
        t = _type(ev)
        if t not in counts:
            continue
        counts[t] += 1
        if t == "room_scan_started" and not room_scan.get("started_at"):
            room_scan["started_at"] = str(ev.get("timestamp") or "")
        if t == "room_scan_done":
            room_scan["done_at"] = str(ev.get("timestamp") or "")
        if t == "reading_pattern" and ev.get("question"):
            reading_questions.append(str(ev.get("question")))
    if not settings.get("enabled") and not pre and not any(counts.values()):
        return None
    asked = counts["gesture_prompt_ok"] + counts["gesture_prompt_missed"]
    flags: list[str] = []
    if counts["gesture_prompt_missed"]:
        flags.append(f"Missed {counts['gesture_prompt_missed']} of {asked} gesture prompt(s)")
    if counts["headphones_disconnected"]:
        flags.append(f"Headphones disconnected {counts['headphones_disconnected']} time(s)")
    if counts["reading_pattern"]:
        flags.append(f"Eyes moved as if reading {counts['reading_pattern']} time(s)")
    if counts["low_light"]:
        flags.append(f"Poor lighting {counts['low_light']} time(s)")
    for check, slot in pre.items():
        if not slot["passed"]:
            flags.append(f"{PRE_CHECK_LABELS.get(check, check)} not passed before the interview")
    return {
        "settings": {k: v for k, v in settings.items() if k != "enabled"},
        "pre_checks": [{"check": c, "label": PRE_CHECK_LABELS.get(c, c), **pre[c]} for c in PRE_CHECKS if c in pre],
        "gesture_prompts": {"asked": asked, "passed": counts["gesture_prompt_ok"], "missed": counts["gesture_prompt_missed"]},
        "headphones_disconnected": counts["headphones_disconnected"],
        "audio_device_changed": counts["audio_device_changed"],
        "low_light": counts["low_light"],
        "reading_pattern": counts["reading_pattern"],
        "reading_questions": reading_questions[:20],
        "room_scan": room_scan or None,
        "flags": flags,
    }


PRE_CHECK_LABELS = {
    "headphones": "Headphones",
    "lighting": "Lighting",
    "gesture": "Gesture check",
    "gaze_calibration": "Eye calibration",
}
