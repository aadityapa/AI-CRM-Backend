"""18 Nov 2026 — the Integrity tab's termination reason and advice.

Reported: an interview read "Completed", "6 of 3 strikes", termination reason
"Repeated interview policy violations", and "the candidate ended the interview
early" — three answers that could not all be true. Every warning was counted
twice (the proctor channel's copy), the reason was guessed from that count, and
nothing said what to tell the candidate.
"""
from services.interview_diagnosis import diagnose, warning_causes, warning_list
from services.interview_integrity import count_strikes, proctor_echo_indexes, summarise


def _ev(t, ts, **kw):
    return {"type": t, "timestamp": f"2026-11-18T11:{ts}+05:30", **kw}


THREE_REAL_SIX_LOGGED = [
    _ev("window_blur", "08:53", question="2"),
    _ev("proctor_tabSwitch", "08:53"),
    _ev("fullscreen_exit", "09:10", question="2"),
    _ev("proctor_tabSwitch", "09:11"),
    _ev("fullscreen_exit", "12:40", question="3"),
    _ev("proctor_tabSwitch", "12:41"),
]


def test_a_proctor_copy_of_a_warning_is_the_same_incident():
    assert proctor_echo_indexes(THREE_REAL_SIX_LOGGED) == {1, 3, 5}
    assert count_strikes(THREE_REAL_SIX_LOGGED) == 3
    s = summarise(THREE_REAL_SIX_LOGGED, session_status="completed")
    assert s["strikes"] == 3
    assert len(s["events"]) == 6  # the timeline still shows every event
    assert sum(1 for e in s["events"] if e.get("echo")) == 3


def test_a_proctor_event_alone_still_counts():
    # The direct report failed (network) — the proctor copy is the only record.
    events = [_ev("window_blur", "00:00"), _ev("proctor_tabSwitch", "05:00")]
    assert count_strikes(events) == 2


def test_a_completed_interview_with_warnings_is_not_called_terminated():
    s = summarise(THREE_REAL_SIX_LOGGED, session_status="completed")
    d = diagnose(
        {"session_status": "completed", "interview_started_at": "2026-11-18T11:04:00+05:30",
         "interview_completed_at": "2026-11-18T11:15:00+05:30", "recording_status": ""},
        {"answers_count": 2, "questions_count": 8,
         "meta": {"finalization_reason": "candidate_ended_early", "boundary_saved_via": "unload"}},
        events=s["events"] + [{"type": "recruiter_joined", "timestamp": "2026-11-18T11:07:00+05:30"}],
        recording_available=True,
    )
    assert d["terminated"] is False
    assert "closed or reloaded" in d["headline"]
    assert len(d["warnings"]) == 3
    assert {c["family"] for c in d["causes"]} == {"focus", "fullscreen"}
    assert not any("no recording" in f for f in d["flags"])
    assert any("recruiter joined" in f for f in d["flags"])


def test_a_terminated_interview_names_the_warning_that_ended_it_and_the_advice():
    events = [_ev("window_blur", "05:00", question="1"), _ev("tab_switch", "06:00", question="2"),
              _ev("fullscreen_exit", "07:00", question="2"), _ev("window_blur", "08:00", question="3"),
              {"type": "termination", "reason": "Repeated interview policy violations",
               "timestamp": "2026-11-18T11:08:00+05:30"}]
    d = diagnose({"session_status": "terminated", "interview_started_at": "2026-11-18T11:04:00+05:30"},
                 {"answers_count": 3, "questions_count": 8, "meta": {}}, events=events)
    assert d["terminated"] is True
    assert d["headline"].startswith("Terminated — 4 warnings, 3 allowed")
    assert "4th warning ended the interview" in d["explanation"][0]
    assert any("Tell the candidate" in s for s in d["next_steps"])


def test_a_schedule_that_lost_the_termination_still_reads_terminated():
    # The session said terminated; the schedule row was overwritten "completed".
    d = diagnose({"session_status": "completed", "interview_started_at": "2026-11-18T11:04:00+05:30"},
                 {"answers_count": 1, "meta": {"final_status": "terminated",
                                               "termination_reason": "Looked away from the screen too often"}},
                 events=[{"type": "gaze_away", "timestamp": f"2026-11-18T11:0{i}:00+05:30"} for i in range(10)])
    assert d["terminated"] is True
    assert "looked away" in d["headline"].lower()


def test_warning_helpers_are_pure():
    w = warning_list(summarise(THREE_REAL_SIX_LOGGED)["events"])
    assert [x["n"] for x in w] == [1, 2, 3]
    assert warning_causes(w)[0]["advice"]
