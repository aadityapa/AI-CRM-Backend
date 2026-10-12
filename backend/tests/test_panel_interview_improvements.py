"""Interviewer login follow-ups (10 Oct 2026).

1. Feedback opens 10 minutes after the round starts (it waited for the booked
   length to run out — start + 60 min by default).
2. The panel gets the position's JD and an evidence report of the AI interview.
3. The evidence report never calls a skill "strong" on the model's word alone.
4. CTC and notice period are not sent to the panel.
5. The panel can report that the candidate did not join.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from tests.test_ta_decision import db, _profile, _actions  # noqa: F401
from tests.test_panel_interviews import ASHA, VIK, _employee, quiet  # noqa: F401
from models import InterviewEvent, PipelineStatus as PS
from services import panel_interviews as pi
from services.ai_interview_summary import evidence_report, question_covers_skill


def _round_at(db, p, emp, minutes_from_now, **kw):
    ev = InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, kind="L1_Interview", employee_id=emp.id,
                        interviewer="Asha Rao", scheduled_at=datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now),
                        duration_minutes=60, status=kw.get("status", "Scheduled"), result=kw.get("result"), created_by=1)
    db.add(ev); db.flush()
    return ev


def test_feedback_opens_ten_minutes_after_the_start(db, quiet):
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db)
    now = datetime.now(timezone.utc)
    started_5 = _round_at(db, p, asha, -5)
    started_11 = _round_at(db, _profile(db), asha, -11)
    assert pi.phase_of(started_5, now) == "upcoming"
    assert pi.phase_of(started_11, now) == "pending"          # was: only after start + 60 min
    row = pi.my_rounds(db, ASHA, "all")["rows"]
    opens = {r["id"]: r["feedback_opens_at"] for r in row}
    assert opens[started_5.id] and opens[started_5.id] > now.isoformat()


def test_the_panel_never_receives_ctc_or_notice(db, quiet):
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    _round_at(db, _profile(db), asha, -30)
    cand = pi.my_rounds(db, ASHA, "all")["rows"][0]["candidate"]
    for key in ("current_ctc", "expected_ctc", "notice_period"):
        assert key not in cand


def test_the_detail_carries_the_position_jd(db, quiet):
    from models import Requirement
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db)
    req = db.query(Requirement).filter_by(opportunity_id=p.opportunity_id).order_by(Requirement.id.desc()).first()
    if req is None:
        pytest.skip("fixture has no requirement")
    req.rmg_jd_text = "Embedded C, RTOS, CAN."
    db.flush()
    ev = _round_at(db, p, asha, -30)
    d = pi.round_detail(db, ASHA, ev.id)
    assert d["jd"]["rmg_jd_text"] == "Embedded C, RTOS, CAN."
    assert d["no_show_reasons"] == list(pi.NO_SHOW_REASONS)


def test_a_no_show_is_reported_listed_under_done_and_rebookable(db, quiet, monkeypatch):
    told = []
    monkeypatch.setattr(pi, "_tell_schedulers_no_show", lambda *a, **k: told.append(a[3].id) or 1)
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    future = _round_at(db, p, asha, 30)
    with pytest.raises(HTTPException) as e:
        pi.mark_no_show(db, ASHA, future.id, None, "Waited 15 minutes")
    assert e.value.status_code == 409                       # not started yet
    ev = _round_at(db, _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted"), asha, -15)
    with pytest.raises(HTTPException) as e:
        pi.mark_no_show(db, ASHA, ev.id, None, "")
    assert e.value.status_code == 400                       # a note is required
    with pytest.raises(HTTPException) as e:
        pi.mark_no_show(db, VIK, ev.id, None, "Waited 15 minutes")
    assert e.value.status_code == 404                       # someone else's round
    row = pi.mark_no_show(db, ASHA, ev.id, "Did not join the call", "Waited 15 minutes, called twice.")
    assert row["phase"] == "no_show" and ev.status == pi.NO_SHOW_STATUS
    assert "Did not join the call" in ev.feedback and told == [ev.id]
    from services.interview_rounds import NOT_HELD_STATUSES
    assert ev.status in NOT_HELD_STATUSES                   # TA's desk offers it for booking again
    data = pi.my_rounds(db, ASHA, "done")
    assert [r["id"] for r in data["rows"]] == [ev.id] and data["counts"]["no_show"] == 1
    # The candidate joined after all — a verdict overwrites the report.
    row, _ = pi.record_panel_feedback(db, ASHA, ev.id, "Hire", "Joined late; solid on RTOS.")
    assert row["phase"] == "done" and ev.status == "Completed"


def test_the_no_show_event_is_routable():
    from routers.crm.email_flows import EVENTS
    assert any(e["event"] == pi.NO_SHOW_EVENT for e in EVENTS)


def test_question_covers_skill_matches_names_not_substrings():
    assert question_covers_skill("How do you debug a crash in C?", "C")
    assert question_covers_skill("Explain priority inversion in an RTOS.", "RTOS (Embedded)")
    assert not question_covers_skill("Explain the CAN bus arbitration.", "C")
    assert question_covers_skill("Write a C++ class with RAII.", "C++")
    assert not question_covers_skill("Explain RAII.", "Linux Device Drivers")
    assert question_covers_skill("How do you load linux device drivers at boot?", "Linux Device Drivers")


def _record(rows, qs, ans, skill_scores):
    return {"questions": qs, "answers": ans,
            "report": {"per_question": rows, "skill_scores": skill_scores, "overall_score": 6}}


def test_evidence_report_does_not_take_the_models_skill_score_on_trust():
    qs = ["Please introduce yourself.", "Explain priority inversion in an RTOS.",
          "What is a mutex in an RTOS?", "How does CAN arbitration work?"]
    ans = ["I am Ravi.", "When a low priority task holds a lock…", "A lock that…", "skip"]
    rows = [{}, {"score": 8, "correct_concepts": ["Priority inheritance"], "weaknesses": ["No ceiling protocol"]},
            {"score": 7.5}, {"score": 0}]
    skills = [{"skill_name": "RTOS", "is_mandatory": True}, {"skill_name": "CAN", "is_mandatory": True},
              {"skill_name": "Linux Device Drivers", "is_mandatory": False}]
    rep = evidence_report(_record(rows, qs, ans, [{"skill": "Linux Device Drivers", "score": 8.5},
                                                  {"skill": "CAN", "score": 8}]), skills)
    assert rep["asked"] == 3 and rep["answered"] == 2               # warm-up dropped; skip not answered
    q1 = rep["questions"][0]
    assert q1["band"] == "strong" and q1["got_right"] == ["Priority inheritance"] and q1["missed"] == ["No ceiling protocol"]
    assert rep["questions"][2]["band"] == "skipped"
    by = {s["skill"]: s for s in rep["skills"]}
    assert by["RTOS"]["status"] == "shown" and by["RTOS"]["questions"] == [1, 2]
    assert by["CAN"]["status"] == "not_tested" and by["CAN"]["estimate_overstated"]   # AI said 80%, never answered
    assert by["Linux Device Drivers"]["status"] == "not_tested" and by["Linux Device Drivers"]["estimate_overstated"]
    assert [p["skill"] for p in rep["probe"]][:1] == ["CAN"]          # mandatory, untested first
    assert "RTOS" not in [p["skill"] for p in rep["probe"]]
    assert any("higher than its own answers" in c for c in rep["caveats"])
    assert any("too few" in c for c in rep["caveats"])


def test_evidence_report_one_good_answer_is_thin_not_strong():
    rep = evidence_report(_record([{"score": 9}], ["Explain RTOS scheduling."], ["Round robin…"], []),
                          [{"skill_name": "RTOS", "is_mandatory": True}])
    assert rep["skills"][0]["status"] == "thin"
    assert evidence_report(None)["available"] is False
