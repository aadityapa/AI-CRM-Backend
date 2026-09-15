"""Interview times never drift from what the TA typed (14 Sep 2026).

A deployed build mailed a candidate "3 PM" for an interview the TA had agreed
at 11 AM. Three things could shift a time between the form and the email —
the server clock, a missing tzdata fallback, and the floating-time .ics — and
these tests pin each of them to IST wall clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import pytest

from services import ist
from services.candidate_comms import build_ics_invite
from services.interview_invite_email import format_when
from services.interview_rounds import read_as_ist


def test_ist_is_never_plain_utc():
    assert ist.IST.utcoffset(datetime(2026, 9, 15)) == timedelta(hours=5, minutes=30)


def test_now_ist_stamp_ignores_the_server_clock():
    # Whatever zone the host runs in, the stamp is India's wall clock.
    stamp = ist.now_ist_stamp()
    expected = datetime.now(timezone.utc).astimezone(ist.IST).strftime("%Y-%m-%d %H:%M")
    assert stamp[:13] == expected[:13]  # same hour; the minute may tick over


def test_read_as_ist_turns_typed_wall_clock_into_utc():
    typed = datetime(2026, 9, 15, 11, 0)  # TA typed 11:00 IST
    stored = read_as_ist(typed)
    assert stored == datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)
    assert ist.to_ist(stored).strftime("%H:%M") == "11:00"


def test_ics_pins_asia_kolkata_and_prints_ist_wall_clock():
    # The DB hands back UTC; the calendar entry must still say 11:00 IST.
    ics = build_ics_invite("L1 — Test", datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc), duration_minutes=45)
    assert "TZID:Asia/Kolkata" in ics
    assert "DTSTART;TZID=Asia/Kolkata:20260915T110000" in ics
    assert "DTEND;TZID=Asia/Kolkata:20260915T114500" in ics
    assert "DTSTAMP:" in ics and "Z\r\n" in ics
    # A floating DTSTART on the EVENT (the old bug) must not come back — the
    # only bare DTSTART allowed is the VTIMEZONE's 1970 anchor.
    event = ics[ics.index("BEGIN:VEVENT"):]
    assert "\r\nDTSTART:" not in event


def test_ics_takes_a_naive_value_as_ist_already():
    ics = build_ics_invite("x", datetime(2026, 9, 15, 11, 0))
    assert "DTSTART;TZID=Asia/Kolkata:20260915T110000" in ics


def test_email_when_text_is_the_typed_string_verbatim():
    # The AI invite email formats the recruiter's string; no zone maths at all.
    assert format_when("2026-09-15 11:00") == "Tuesday, 15 September 2026 at 11:00 AM IST"
    assert format_when("2026-09-15T16:00") == "Tuesday, 15 September 2026 at 4:00 PM IST"
    assert format_when("garbage") == "garbage"


def test_scheduled_stamp_becomes_an_ist_iso_instant_for_the_ui():
    from services.ist import local_stamp_to_iso
    assert local_stamp_to_iso("2026-09-15 16:00") == "2026-09-15T16:00:00+05:30"
    assert local_stamp_to_iso("") is None


def test_bridge_defaults_to_ist_now_not_server_now():
    """`schedule_l1_interview` with no time must stamp IST 'now', not `datetime.now()`."""
    import inspect
    import services.ai_interview_bridge as bridge
    src = inspect.getsource(bridge.schedule_l1_interview)
    assert "now_ist_stamp()" in src
    assert 'datetime.now().strftime("%Y-%m-%d %H:%M")' not in src


@pytest.mark.parametrize("raw,ok", [("2026-09-15 11:00", True), ("2026-09-15T11:00", True),
                                    ("15/09/2026 11:00", False)])
def test_schedule_endpoint_time_validation(raw, ok):
    from fastapi import HTTPException
    from routers.crm.resumes import ScheduleAiInterviewIn
    body = ScheduleAiInterviewIn(scheduled_at=raw)
    if ok:
        assert body.stamp() == "2026-09-15 11:00"
    else:
        with pytest.raises(HTTPException):
            body.stamp()


def test_manual_round_when_is_spelled_out_in_ist():
    from routers.crm.candidate_profiles import _fmt_slot_ist
    assert _fmt_slot_ist("2026-09-15T11:00") == "15 Sep 2026, 11:00 AM IST"
    assert _fmt_slot_ist("garbage") == "garbage"


def test_interview_round_raw_when_is_ist_wall_clock():
    """The round form posts a UTC instant; raw_when must read as IST."""
    from services.ist import ist_naive
    posted = datetime(2026, 9, 15, 5, 30, tzinfo=timezone.utc)  # 11:00 IST in the browser
    assert ist_naive(posted).strftime("%Y-%m-%d %H:%M") == "2026-09-15 11:00"
