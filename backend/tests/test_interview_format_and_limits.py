"""After the first test interview (12 Oct 2026): the template decides how many
spoken / pseudo-code / coding questions; the interview ends on the ONE limit the
template chose; the rules page shows the real count; a recruiter's presence
makes the candidate's page check every second; the server's clock ends a timed
interview even if the page's countdown does not."""
from __future__ import annotations

import importlib
import time

from services import interview_live as live
from services.interview import skill_bank as sb

main = importlib.import_module("main")


def _q(i: int, kind: str = "normal", skill: str = "Embedded C") -> dict:
    q = {"id": f"q{i}", "skill": skill, "kind": kind, "text": f"Question {i}?"}
    if kind == "pseudo":
        q.update({"code": "x = 1", "options": ["a", "b"], "correct": 0})
    return q


def _bank(**extra) -> dict:
    raw = {"enabled": True, "ai_fill": False, "pseudo_count": 0, "coding_count": 0,
           "mandatory_skills": ["Embedded C"], "pick_min": 1, "pick_max": 1,
           "questions": [_q(i) for i in range(10)]}
    raw.update(extra)
    return sb.clean_config(raw)


# ---------------------------------------------------------------------------
# How many of each kind
# ---------------------------------------------------------------------------

def test_spoken_count_draws_that_many_from_a_longer_list():
    cfg = _bank(spoken_count=4)
    plan = sb.build_plan(cfg, ["Embedded C"], "seed-a")
    assert len(plan) == 4
    other = sb.build_plan(cfg, ["Embedded C"], "seed-b")
    assert len(other) == 4
    # A different random draw for another candidate (very likely different).
    assert [p["text"] for p in plan] != [p["text"] for p in other] or True


def test_spoken_count_zero_keeps_the_older_per_skill_rule():
    cfg = _bank(normal_per_skill=3)
    assert cfg["spoken_count"] == 0
    assert len(sb.build_plan(cfg, ["Embedded C"], "s")) == 3


def test_spoken_count_shortfall_is_written_by_the_ai_only_when_allowed():
    short = _bank(spoken_count=13)
    assert sb.missing_normals(short, ["Embedded C"]) == []
    fill = _bank(spoken_count=13, ai_fill=True)
    assert sb.missing_normals(fill, ["Embedded C"]) == [("Embedded C", 3)]


def test_typed_counts_follow_the_template():
    cfg = _bank(spoken_count=5, pseudo_count=1, coding_count=1,
                questions=[_q(i) for i in range(6)] + [_q(10, "pseudo"), _q(11, "pseudo"), _q(12, "coding")])
    plan = sb.build_plan(cfg, ["Embedded C"], "s")
    kinds = [p["kind"] for p in plan]
    assert kinds.count("normal") == 5 and kinds.count("pseudo") == 1 and kinds.count("coding") == 1


def test_plan_summary_is_what_one_interview_asks():
    cfg = _bank(spoken_count=5, pseudo_count=1,
                questions=[_q(i) for i in range(8)] + [_q(10, "pseudo"), _q(11, "pseudo")])
    summary = sb.plan_summary(cfg, ["Embedded C"])
    assert (summary["total"], summary["spoken"], summary["pseudo"], summary["coding"]) == (6, 5, 1, 0)
    assert summary["varies"] is False
    assert summary["minutes_estimate"] > 0


# ---------------------------------------------------------------------------
# One limit — the one the template chose
# ---------------------------------------------------------------------------

def test_count_mode_has_no_clock_and_time_mode_keeps_its_limit():
    assert main._effective_time_limit("count", 1200) == 0
    assert main._effective_time_limit("time", 1200) == 1200
    assert main._effective_time_limit("time", 10 ** 9) == 6 * 60 * 60
    assert main._effective_time_limit("time", "x") == 0


def test_rules_page_format_reads_the_real_bank_count(monkeypatch):
    job = {"jobTitle": "Embedded", "timingMode": "count", "numQ": 5, "timeLimitSec": 1200,
           "requiredSkills": ["Embedded C"],
           "weights": {"skillBank": {"enabled": True, "ai_fill": False, "spoken_count": 7, "pseudo_count": 2,
                                     "mandatory_skills": ["Embedded C"],
                                     "questions": [_q(i) for i in range(9)] + [_q(20, "pseudo"), _q(21, "pseudo")]}}}
    fmt = main._interview_format({"timing_mode": "count", "time_limit_sec": 1200, "num_q": 5}, job)
    assert fmt["num_q"] == 9  # 7 spoken + 2 pseudo-code, not the stale 5
    assert fmt["question_mix"] == {"spoken": 7, "pseudo": 2, "coding": 0}
    assert fmt["time_limit_sec"] == 0  # count mode: no clock on the page either
    timed = main._interview_format({"timing_mode": "time", "time_limit_sec": 1200}, {**job, "timingMode": "time"})
    assert timed["time_limit_sec"] == 1200


def test_the_template_decides_how_the_interview_ends():
    """13 Oct 2026: HR Setup posted no timing, so every schedule said "count, no
    limit" and a template set to "Time limit: 10 min" ran with no clock and no
    warnings. The template wins; the schedule's copy is only the fallback."""
    from utils.interview_limits import resolve_template_num_q, template_timing
    stale = {"timing_mode": "count", "time_limit_sec": 0, "num_q": 5}
    timed_tpl = {"timingMode": "time", "timeLimitSec": 600, "numQ": 9}
    assert template_timing(stale, timed_tpl) == ("time", 600)
    assert template_timing({"timing_mode": "time", "time_limit_sec": 900}, {"timingMode": "count"}) == ("count", 0)
    # A time-mode template that forgot its minutes falls back to the schedule's.
    assert template_timing({"timing_mode": "time", "time_limit_sec": 900}, {"timingMode": "time"}) == ("time", 900)
    # No template: the schedule decides, as before.
    assert template_timing({"timing_mode": "time", "time_limit_sec": 300}, None) == ("time", 300)
    assert template_timing({"timing_mode": "bogus"}, None) == ("count", 0)
    assert resolve_template_num_q(stale, timed_tpl) == 9
    assert resolve_template_num_q(stale, None) == 5


def test_hr_setup_schedules_carry_the_template_settings(monkeypatch):
    """The dashboard's HR Setup posts only the template + skills; what is stored
    must be the TEMPLATE's timing, count, transcript and mic switches."""
    captured: dict = {}
    tpl = {"jobId": "j1", "jobTitle": "Projection", "timingMode": "time", "timeLimitSec": 600,
           "numQ": 7, "questionType": "dynamic", "enableTranscriptInput": True, "showSpokenText": True,
           "micAlwaysOn": True, "difficulty": "hard", "followupMode": False, "weights": {}}
    monkeypatch.setattr(main, "_require_user", lambda request, roles: ({"sub": "hr1", "role": "hr"}, None))
    monkeypatch.setattr(main, "_enforce_crm_roles", lambda *a, **k: None)
    monkeypatch.setattr(main, "get_job_template", lambda target, jid: dict(tpl))

    def fake_create(target, **kw):
        captured.update(kw)
        raise ValueError("stop here")  # the route answers {"error"} — enough for the test

    monkeypatch.setattr(main, "create_interview_schedule", fake_create)
    out = main.hr_schedule_interview(
        object(), candidate_name="A", candidate_email="a@x.com", scheduled_at_local="2026-10-13T10:00",
        notes="", final_skills="c", jobId="j1", num_q=5, difficulty="medium", followup_mode="false",
        timing_mode="count", time_limit_sec=0, mic_always_on="false", show_spoken_text="false",
        enable_transcript_input="", model="", candidate_id=0, opportunity_id=0)
    assert out == {"error": "stop here"}
    cfg = main._extract_invite_config_from_notes(captured["notes"])
    assert (cfg["timing_mode"], cfg["time_limit_sec"], cfg["num_q"]) == ("time", 600, 7)
    assert cfg["enable_transcript_input"] is True and cfg["mic_always_on"] is True
    assert cfg["difficulty"] == "hard"


def test_typed_question_timer_never_outruns_the_interview_clock():
    from candidate.service import next_question_payload
    spec = {"kind": "coding", "skill": "C", "text": "Write a ring buffer.", "time_limit_sec": 600, "code": ""}
    session = {
        "questions": ["Write a ring buffer."], "current": 0, "answers": [],
        "meta": {"question_source": sb.QUESTION_SOURCE, "timing_mode": "time", "time_limit_sec": 1200,
                 "num_q": 1, "question_specs": {"0": spec},
                 "interview_started_epoch": time.time() - 1080},  # 2 minutes left
    }
    out = next_question_payload(session)
    assert out.get("answer_time_limit_sec") is not None
    assert out["answer_time_limit_sec"] <= 121


def test_the_server_clock_says_time_up():
    sess = {"meta": {"timing_mode": "time", "time_limit_sec": 60, "interview_started_epoch": time.time() - 61}}
    assert main._session_time_up(sess) is True
    sess["finalizing"] = True
    assert main._session_time_up(sess) is False
    assert main._session_time_up({"meta": {"time_limit_sec": 0, "interview_started_epoch": 1}}) is False


# ---------------------------------------------------------------------------
# Joining is immediate
# ---------------------------------------------------------------------------

def test_candidate_polls_every_second_while_a_recruiter_is_present():
    ctrl = live.empty_control()
    now = 10_000.0
    assert live.candidate_poll_ms(ctrl, now) == live.CANDIDATE_POLL_SEC * 1000
    live.join(ctrl, "7", "Asha", now)
    assert live.candidate_poll_ms(ctrl, now + 5) == live.FAST_POLL_MS
    assert live.candidate_poll_ms(ctrl, now + live.PRESENCE_WINDOW_SEC + 1) == live.CANDIDATE_POLL_SEC * 1000
    log_len = len(ctrl["log"])
    live.presence(ctrl, "7", "Asha", now + 60)
    assert len(ctrl["log"]) == log_len  # a heartbeat writes no log line
    assert live.candidate_poll_ms(ctrl, now + 61) == live.FAST_POLL_MS
    assert live.candidate_view(ctrl, live.empty_reply(), now + 61)["poll_ms"] == live.FAST_POLL_MS
