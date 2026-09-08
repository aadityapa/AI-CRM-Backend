"""Date handling for the interview calendar.

The calendar merges two sources whose times are stored very differently:

  * manual rounds     — a real tz-aware timestamp column
  * AI L1 sessions    — a free-form LOCAL string on a legacy table, no timezone

Everything here is about not corrupting a time on the way to the grid. An
interview shown an hour out, or on the wrong day at a week boundary, is worse
than one not shown at all — the recruiter acts on it.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.interview_calendar import (  # noqa: E402
    MAX_RANGE_DAYS, SOURCE_AI, SOURCE_MANUAL, parse_legacy_local, parse_range,
)


# --------------------------------------------------------------- parse_range

def test_explicit_window_is_respected():
    r = parse_range("2026-08-02", "2026-08-09")
    assert r.start == datetime(2026, 8, 2)
    assert r.end == datetime(2026, 8, 9)
    assert r.days == 7


def test_default_window_is_the_current_week():
    r = parse_range(None, None)
    assert r.days == 7
    assert (r.start.hour, r.start.minute, r.start.second) == (0, 0, 0)


def test_end_before_start_is_corrected():
    """A backwards range would otherwise query nothing and look like no data."""
    r = parse_range("2026-08-09", "2026-08-02")
    assert r.end > r.start


def test_absurd_range_is_clamped():
    """Without a cap a client could ask for a decade and scan everything."""
    r = parse_range("2020-01-01", "2030-01-01")
    assert r.days <= MAX_RANGE_DAYS


def test_iso_with_time_is_accepted():
    r = parse_range("2026-08-02T00:00:00", "2026-08-09T00:00:00")
    assert r.start == datetime(2026, 8, 2)


def test_zoned_iso_is_reduced_to_wall_time():
    """A zone must not shift the window — the grid is drawn in local time."""
    r = parse_range("2026-08-02T00:00:00+05:30", "2026-08-09T00:00:00+05:30")
    assert r.start.hour == 0
    assert r.start.tzinfo is None


def test_junk_bounds_fall_back_to_the_default_week():
    r = parse_range("not-a-date", "also-not-a-date")
    assert r.days == 7


# -------------------------------------------------------- parse_legacy_local

def test_the_format_the_scheduler_writes():
    assert parse_legacy_local("2026-08-07 12:30") == datetime(2026, 8, 7, 12, 30)


def test_other_formats_seen_in_the_wild():
    expected = datetime(2026, 8, 7, 12, 30)
    for value in ("2026-08-07 12:30:00", "2026-08-07T12:30", "2026-08-07T12:30:00"):
        assert parse_legacy_local(value) == expected, value


def test_day_first_format():
    assert parse_legacy_local("07/08/2026 12:30") == datetime(2026, 8, 7, 12, 30)


def test_date_only_lands_at_midnight():
    assert parse_legacy_local("2026-08-07") == datetime(2026, 8, 7, 0, 0)


def test_no_timezone_conversion_happens():
    """The single most important property: 12:30 stays 12:30.

    A recruiter and candidate agree on a wall-clock time. Interpreting the
    legacy string as UTC would move every AI interview by the server's offset —
    5.5 hours in India, i.e. onto a different part of the day.
    """
    parsed = parse_legacy_local("2026-08-07 12:30")
    assert parsed.hour == 12 and parsed.minute == 30
    assert parsed.tzinfo is None


def test_unusable_values_return_none_rather_than_raising():
    """An unparseable time must degrade to "no time", never break the week."""
    for value in ("", None, "   ", "not a date", "99/99/9999 99:99", 0, []):
        assert parse_legacy_local(value) is None, value


# ---------------------------------------------------------------- constants

def test_source_markers_are_stable():
    """The frontend colour-codes on these strings."""
    assert SOURCE_AI == "ai_l1"
    assert SOURCE_MANUAL == "manual_round"
