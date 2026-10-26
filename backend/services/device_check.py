"""Device-check results the candidate's page reports before the interview (24 Oct 2026).

Why this exists
---------------
A candidate could not get past the Device Check because the screen share kept
failing, and nobody could see why: the page wrote the reason to the browser
console only. The Device Check runs BEFORE the candidate signs in, so it has no
session — it reports with the invite link's token (the same capability the
public lookup uses) and the result is kept on the schedule row
(`interview_schedule.device_check_log`). The Integrity tab reads it into the
interview's timeline as informational events — they never count as strikes or
change the score.

Everything here is PURE; `main.py` does the reading and writing.
"""
from __future__ import annotations

import re
from typing import Any

#: Checks the page may report. Only the screen share today.
CHECKS = frozenset({"screen_share"})

#: Entries kept per interview — a candidate pressing the button 50 times must
#: not grow the row without end. The newest are kept.
MAX_EVENTS = 20

#: What each failure means, in words a recruiter reads on the timeline.
REASON_WORDS: dict[str, str] = {
    "denied": "the share window was cancelled or the browser blocked it (NotAllowedError)",
    "not_entire_screen": "the candidate shared {surface} instead of the entire screen",
    "unsupported": "this browser cannot share the screen",
    "no_track": "the browser returned no video from the share",
    "NotFoundError": "no screen was available to share (NotFoundError)",
    "NotReadableError": "the screen could not be captured — another app or the system blocked it (NotReadableError)",
    "AbortError": "the share was interrupted (AbortError)",
    "InvalidStateError": "the browser refused because the page was not active (InvalidStateError)",
    "TypeError": "the browser rejected the share settings (TypeError)",
    "OverconstrainedError": "the browser could not meet the share settings (OverconstrainedError)",
}

SURFACE_WORDS = {"browser": "a browser tab", "window": "a window", "monitor": "the entire screen"}

_SAFE = re.compile(r"[^A-Za-z0-9_ .:/()\-]")


def _clip(value: Any, limit: int) -> str:
    text = _SAFE.sub("", str(value or "")).strip()
    return text[:limit]


def clean_event(body: dict, *, user_agent: str = "", ip: str = "", at: str = "") -> dict | None:
    """One reported result → the stored entry, or None when the body is not a
    known check. Never trusts the page's own wording: the reason is a short
    code, mapped to words at read time."""
    if not isinstance(body, dict):
        return None
    check = _clip(body.get("check"), 32)
    if check not in CHECKS:
        return None
    ok = bool(body.get("ok"))
    return {
        "check": check,
        "ok": ok,
        "reason": "" if ok else (_clip(body.get("reason"), 40) or "error"),
        "surface": _clip(body.get("surface"), 16),
        "attempt": max(0, min(int(_num(body.get("attempt"))), 999)),
        "user_agent": str(user_agent or "")[:240],
        "ip": str(ip or "")[:64],
        "timestamp": at,
    }


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def append(log: list | None, event: dict, cap: int = MAX_EVENTS) -> list:
    """The stored list with `event` added, newest `cap` kept."""
    items = [e for e in (log or []) if isinstance(e, dict)]
    items.append(event)
    return items[-cap:]


def reason_words(reason: str, surface: str = "") -> str:
    template = REASON_WORDS.get(reason)
    if template:
        return template.format(surface=SURFACE_WORDS.get(surface, surface or "only part of the screen"))
    return f"the share failed ({reason or 'unknown error'})"


def timeline_events(log: list | None) -> list[dict]:
    """The stored entries as Integrity timeline events (informational)."""
    out: list[dict] = []
    for e in log or []:
        if not isinstance(e, dict) or e.get("check") != "screen_share":
            continue
        attempt = int(_num(e.get("attempt")))
        prefix = f"Device check, attempt {attempt}: " if attempt else "Device check: "
        if e.get("ok"):
            what = SURFACE_WORDS.get(str(e.get("surface") or ""), "the screen")
            details = f"{prefix}shared {what}."
            kind = "screen_share_ok"
        else:
            details = prefix + reason_words(str(e.get("reason") or ""), str(e.get("surface") or "")) + "."
            kind = "screen_share_failed"
        out.append({
            "type": kind,
            "details": details,
            "timestamp": str(e.get("timestamp") or ""),
            "ip": str(e.get("ip") or ""),
            "user_agent": str(e.get("user_agent") or ""),
        })
    return out


def failure_count(log: list | None) -> int:
    return sum(1 for e in log or [] if isinstance(e, dict) and e.get("check") == "screen_share" and not e.get("ok"))
