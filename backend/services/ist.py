"""One definition of "India time" for every interview-scheduling path.

Written 14 Sep 2026 after a deployed build mailed a candidate a different
interview time from the one the TA picked. Every path that touches an
interview time must agree on three things, and they live here so they cannot
drift again:

* `IST` — Asia/Kolkata, or a FIXED +05:30 when the host has no tzdata. Two
  modules used to fall back to `timezone.utc` instead, which silently turned
  every entered time into UTC on a bare Windows box (a 5h30 shift).
* `now_ist()` — the wall-clock "now" the team means. `datetime.now()` is the
  SERVER's clock: on a UTC-hosted deploy (AWS, Render, Docker) that is 5h30
  behind the recruiter's watch.
* `to_ist()` — render any stored datetime (naive = already IST wall time,
  aware = whatever zone the DB session handed back) as IST wall time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:  # pragma: no cover - tzdata missing on a bare Windows install
    IST = timezone(timedelta(hours=5, minutes=30))

IST_LABEL = "IST"


def now_ist() -> datetime:
    """Aware "now" in IST — independent of the server's OS timezone."""
    return datetime.now(IST)


def now_ist_stamp() -> str:
    """"YYYY-MM-DD HH:MM" in IST — the legacy `scheduled_at_local` format."""
    return now_ist().strftime("%Y-%m-%d %H:%M")


def to_ist(dt: datetime | None) -> datetime | None:
    """IST wall time as an aware datetime. Naive input is taken as IST already."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def ist_naive(dt: datetime | None) -> datetime | None:
    """IST wall time with the zone stripped (for strftime / floating formats)."""
    out = to_ist(dt)
    return out.replace(tzinfo=None) if out is not None else None


def read_as_ist(dt: datetime | None) -> datetime | None:
    """A NAIVE datetime typed by the team is IST; return it as UTC for storage.
    Aware input passes through untouched."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=IST).astimezone(timezone.utc)


def local_stamp_to_iso(stamp: str | None) -> str | None:
    """"2026-09-15 16:00" (IST wall clock, the legacy scheduled_at_local format)
    → "2026-09-15T16:00:00+05:30", so a browser renders the same clock time
    the recruiter picked. Unparseable input passes through unchanged."""
    raw = (stamp or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw[:19], fmt).replace(tzinfo=IST).isoformat()
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)).isoformat()
    except ValueError:
        return raw


def human_when(stamp: str | None) -> str:
    """"2026-09-15 16:00" → "Tuesday, 15 September 2026 at 4:00 PM IST" —
    12-hour clock with the zone spelled out (15 Sep 2026, user request: every
    candidate-facing time in 12-hour format). Unparseable input is returned
    as typed rather than dropped."""
    raw = (stamp or "").strip()
    if not raw:
        return ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw[:19], fmt)
            break
        except ValueError:
            continue
    else:
        return raw
    return f"{dt.strftime('%A, %d %B %Y')} at {dt.strftime('%I:%M %p').lstrip('0')} IST"
