"""Interview integrity — one taxonomy, one score, one summary (15 Sep 2026).

Before this module the server counted only ``tab_switch`` and
``multiple_faces`` while the candidate page reported ``visibility_hidden``,
``window_blur``, ``fullscreen_exit``, ``alt_tab`` … — so a real tab switch was
written to the log but never counted, never shown, and never moved the
server's termination counter. Everything about an integrity event now lives
here, pure and testable:

* `EVENT_TYPES`   — every type the runtime can send, with a label, a penalty
                    weight and whether it is a STRIKE (counts toward the
                    three-warning termination rule).
* `count_strikes` — the server-side strike count (the authority; the client's
                    own counter is only for the on-screen warning).
* `summarise`     — per-family counts for a row (tab switches, focus loss,
                    fullscreen exits, keyboard escapes, face events, clipboard,
                    devtools) plus the integrity score and a "needs review" flag.
* `integrity_score` — 100 minus weighted penalties, floored at 0; a terminated
                    interview is capped at `TERMINATED_CAP`.
* `dedupe_events` — the proctor channel used to re-append its whole event
                    list on every call; identical (type, timestamp) pairs are
                    collapsed.
* `shared_device_flags` — the same device id / IP across DIFFERENT candidate
                    emails (one laptop taking three people's interviews).
* `rows_to_csv`   — the export.
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Iterable

#: type -> (label, family, penalty weight, counts as a strike)
EVENT_TYPES: dict[str, tuple[str, str, int, bool]] = {
    # tab / window
    "tab_switch": ("Tab switch", "tab", 8, True),
    "visibility_hidden": ("Tab hidden", "tab", 8, True),
    "proctor_tabSwitch": ("Tab switch (proctor)", "tab", 8, True),
    "window_blur": ("Window lost focus", "focus", 5, True),
    "focus_lost": ("Focus lost", "focus", 5, True),
    "alt_tab": ("Alt+Tab", "focus", 6, True),
    "windows_key": ("Windows key", "focus", 6, True),
    # fullscreen / keys
    "fullscreen_exit": ("Fullscreen exit", "fullscreen", 5, True),
    "key_escape": ("Escape key", "keys", 2, False),
    "key_f11": ("F11 key", "keys", 2, False),
    "ctrl_esc": ("Ctrl+Esc", "keys", 3, False),
    # camera
    "multiple_faces": ("Extra face on camera", "face", 15, True),
    "proctor_extraFace": ("Extra face (proctor)", "face", 15, True),
    "no_face": ("No face on camera", "face", 4, False),
    # new (15 Sep 2026)
    "clipboard": ("Copy / paste attempt", "clipboard", 6, True),
    "context_menu": ("Right-click menu", "clipboard", 2, False),
    "devtools": ("Developer tools attempt", "devtools", 12, True),
    # bookkeeping
    "termination": ("Terminated", "system", 0, False),
}

FAMILIES: dict[str, str] = {
    "tab": "Tab switches", "focus": "Focus loss", "fullscreen": "Fullscreen exits",
    "keys": "Key escapes", "face": "Camera", "clipboard": "Clipboard", "devtools": "Dev tools",
}

#: Types that count toward the three-warning termination rule.
STRIKE_TYPES: frozenset[str] = frozenset(t for t, (_, _, _, strike) in EVENT_TYPES.items() if strike)

#: A terminated interview can never score above this.
TERMINATED_CAP = 20
#: At or below this the row is flagged "needs review".
REVIEW_THRESHOLD = 70


def event_type(ev) -> str:
    return str((ev or {}).get("type") or "").strip() if isinstance(ev, dict) else ""


def label_for(t: str) -> str:
    return EVENT_TYPES.get(t, (t.replace("_", " ").title(), "other", 0, False))[0]


def count_strikes(events: Iterable) -> int:
    return sum(1 for ev in events if event_type(ev) in STRIKE_TYPES)


def dedupe_events(events: Iterable) -> list[dict]:
    """Collapse identical (type, timestamp) pairs — keeps order, drops non-dicts."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        key = (event_type(ev), str(ev.get("timestamp") or ev.get("at_ist") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


def integrity_score(events: Iterable, *, terminated: bool = False) -> int:
    penalty = 0
    for ev in events:
        t = event_type(ev)
        penalty += EVENT_TYPES.get(t, ("", "other", 3, False))[2]
    score = max(0, 100 - penalty)
    if terminated:
        score = min(score, TERMINATED_CAP)
    return int(score)


def summarise(events: Iterable, *, session_status: str = "") -> dict:
    """Per-family counts + score + flags for one interview row."""
    evs = dedupe_events(events)
    by_family: dict[str, int] = defaultdict(int)
    by_type: dict[str, int] = defaultdict(int)
    for ev in evs:
        t = event_type(ev)
        if t == "termination" or not t:
            continue
        fam = EVENT_TYPES.get(t, ("", "other", 0, False))[1]
        by_family[fam] += 1
        by_type[t] += 1
    terminated = str(session_status or "").lower() == "terminated"
    score = integrity_score(evs, terminated=terminated)
    return {
        "events": evs,
        "strikes": count_strikes(evs),
        "by_family": {k: by_family.get(k, 0) for k in FAMILIES},
        "by_type": dict(by_type),
        "integrity_score": score,
        "needs_review": score <= REVIEW_THRESHOLD or terminated,
        "event_count": sum(by_type.values()),
    }


def shared_device_flags(rows: list[dict]) -> dict[str, list[str]]:
    """invite_token -> list of other candidate emails seen on the same device/IP."""
    by_device: dict[str, set[str]] = defaultdict(set)
    by_ip: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        email = str(r.get("candidate_email") or "").strip().lower()
        if not email:
            continue
        dev = str(r.get("active_device_id") or "").strip()
        if dev:
            by_device[dev].add(email)
        for ev in r.get("events") or []:
            ip = str((ev or {}).get("ip") or "").strip() if isinstance(ev, dict) else ""
            if ip and ip not in ("127.0.0.1", "::1"):
                by_ip[ip].add(email)
    out: dict[str, list[str]] = {}
    for r in rows:
        email = str(r.get("candidate_email") or "").strip().lower()
        token = str(r.get("invite_token") or "")
        others: set[str] = set()
        dev = str(r.get("active_device_id") or "").strip()
        if dev:
            others |= by_device[dev] - {email}
        for ev in r.get("events") or []:
            ip = str((ev or {}).get("ip") or "").strip() if isinstance(ev, dict) else ""
            if ip and ip in by_ip:
                others |= by_ip[ip] - {email}
        if others and token:
            out[token] = sorted(others)
    return out


CSV_COLUMNS = [
    "candidate_name", "candidate_email", "customer_name", "requirement_title", "scheduled_at",
    "session_status", "integrity_score", "strikes", "tab_switches", "focus_loss", "fullscreen_exits",
    "face_events", "clipboard", "devtools", "reason", "interview_started_at", "interview_completed_at",
]


def _csv_cell(v) -> str:
    s = "" if v is None else str(v)
    return ("'" + s) if s[:1] in ("=", "+", "-", "@") else s


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        fam = r.get("by_family") or {}
        w.writerow([_csv_cell(x) for x in (
            r.get("candidate_name"), r.get("candidate_email"), r.get("customer_name"),
            r.get("requirement_title"), r.get("scheduled_at"), r.get("session_status"),
            r.get("integrity_score"), r.get("strikes"), fam.get("tab", 0), fam.get("focus", 0),
            fam.get("fullscreen", 0), fam.get("face", 0), fam.get("clipboard", 0), fam.get("devtools", 0),
            r.get("reason"), r.get("interview_started_at"), r.get("interview_completed_at"),
        )])
    return buf.getvalue()
