"""A fresh AI L1 link after one that was not attempted / not cleared (7 Oct 2026,
user flow): TA sends the first link, the candidate cannot attempt it, the row
reads "Not attempted" (not "Failed"); once the candidate confirms they are ready
TA presses "Reschedule AI L1" — a NEW link, a required note, the old verdict kept
on record, the screeners told. A passed AI L1 is never rescheduled, and the
newest link is what every screen reads.

Run:  cd backend && python -m pytest tests/test_ai_l1_reschedule.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from tests.test_ta_decision import db, _profile, _actions, TA  # noqa: F401
from models import AiInterviewLink, PipelineStatus as PS
from services import candidate_status as cs
from services.candidate_profiles import latest_ai_interviews
import routers.crm.ai_interviews as ai


def _link(db, p, result, score=None, *, not_attempted=False, hr_decision=None, created=None):
    n = db.query(AiInterviewLink).count() + 1
    link = AiInterviewLink(profile_id=p.id, candidate_id=p.candidate_id, invite_token=f"tok{p.id}-{n}",
                           opportunity_id=p.opportunity_id, result=result, overall_score_percent=score,
                           not_attempted=not_attempted, hr_decision=hr_decision,
                           created_at=created or datetime.now(timezone.utc),
                           completed_at=datetime.now(timezone.utc) if result != "Pending" else None)
    db.add(link); db.flush()
    return link


@pytest.fixture()
def bridge(monkeypatch):
    """The legacy interview store is not here: schedule_l1_interview is stubbed to
    mint a link row the way the real bridge does, and the invite mail is recorded."""
    sent: list = []

    def _schedule(db, candidate, requirement, profile, **kw):
        link = AiInterviewLink(profile_id=profile.id, candidate_id=candidate.id, invite_token=f"new-{profile.id}-{len(sent)}",
                               opportunity_id=profile.opportunity_id, result="Pending",
                               scheduled_by=kw.get("scheduled_by"))
        db.add(link); db.flush()
        sent.append(("scheduled", kw.get("scheduled_at_local")))
        return {"scheduled": True, "session_ref": link.invite_token, "invite_url": "https://x/?invite=" + link.invite_token,
                "access_key": "KEY", "link_id": link.id}

    monkeypatch.setattr(ai, "schedule_l1_interview", _schedule)
    monkeypatch.setattr(ai, "ensure_l1_template_ready", lambda *a, **k: None)
    monkeypatch.setattr(ai, "ai_interview_autosend_enabled", lambda: False)
    monkeypatch.setattr(ai, "_record_reschedule",
                        lambda db, profile, candidate, previous, when, note, user: sent.append(("resched", previous.id, note)))
    return sent


def _post(db, p, note=None, when="2026-10-09 11:00"):
    from unittest.mock import MagicMock
    body = ai.AiInterviewCreate(scheduled_at=when, reschedule_note=note)
    return ai.trigger_ai_interview(p.id, MagicMock(), body, db, TA)


def test_not_attempted_is_carried_on_the_row_and_in_the_derived_status(db):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    _link(db, p, "Failed", 0, not_attempted=True)
    row = latest_ai_interviews(db, [p])[p.id]
    assert row["ai_interview_result"] == "Failed" and row["ai_not_attempted"] is True
    assert cs.ai_state("Failed", True) == cs.NOT_ATTEMPTED
    assert cs.ai_state("Selected", True) == cs.PASSED          # a recruiter override still wins
    assert cs.ai_state("Failed", False) == cs.FAILED
    label = cs.statuses_for(db, [p])[p.id]["label"]
    assert label == "AI L1 – Not Attempted"
    assert cs.round_for(cs.STATUS_BY_KEY["ai_l1_not_attempted"])[2] == "Not Attempted"


def test_reschedule_needs_a_note_then_mints_a_new_link_and_keeps_the_old_one(db, bridge):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    old = _link(db, p, "Failed", 0, not_attempted=True,
                created=datetime.now(timezone.utc) - timedelta(days=1))
    with pytest.raises(HTTPException) as err:
        _post(db, p, note="")
    assert err.value.status_code == 400 and "fresh AI L1" in err.value.detail
    out = _post(db, p, note="Candidate confirmed on the phone — ready Thursday 11 AM")
    assert out["success"]
    links = db.query(AiInterviewLink).filter_by(profile_id=p.id).order_by(AiInterviewLink.id).all()
    assert [l.result for l in links] == ["Failed", "Pending"]         # the old verdict stays on record
    assert old.not_attempted is True
    assert ("resched", old.id, "Candidate confirmed on the phone — ready Thursday 11 AM") in bridge
    # Every screen now reads the NEW link, not the old "Failed".
    row = latest_ai_interviews(db, [p])[p.id]
    assert row["ai_interview_result"] == "Pending" and row["ai_not_attempted"] is False
    assert cs.statuses_for(db, [p])[p.id]["label"] == "AI L1 – Scheduled"


def test_a_first_link_needs_no_note_and_a_passed_one_is_never_rescheduled(db, bridge):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    out = _post(db, p)                                   # no previous link → plain schedule
    assert out["success"] and not [s for s in bridge if s[0] == "resched"]
    db.query(AiInterviewLink).filter_by(profile_id=p.id).delete()
    _link(db, p, "Passed", 81.0)
    with pytest.raises(HTTPException) as err:
        _post(db, p, note="trying again anyway")
    assert err.value.status_code == 409
    # a Failed link a recruiter overrode to Selected counts as passed too
    db.query(AiInterviewLink).filter_by(profile_id=p.id).delete()
    _link(db, p, "Failed", 30.0, hr_decision="selected")
    with pytest.raises(HTTPException) as err:
        _post(db, p, note="trying again anyway")
    assert err.value.status_code == 409


def test_a_pending_link_still_blocks_and_the_outcome_words_read_right(db, bridge):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    _link(db, p, "Pending")
    with pytest.raises(HTTPException) as err:
        _post(db, p, note="again")
    assert err.value.status_code == 409
    assert ai.previous_outcome_words(_link(db, p, "Failed", 0, not_attempted=True)) == "Not attempted"
    assert ai.previous_outcome_words(_link(db, p, "Failed", 42.5)) == "Failed (42.5%)"
    assert ai.previous_outcome_words(_link(db, p, "Failed", 42.5, hr_decision="on_hold")) \
        == "On Hold (42.5%) (recruiter override)"


def test_the_reschedule_event_is_routable_from_email_flows():
    from routers.crm.email_flows import EVENTS
    row = next(e for e in EVENTS if e["event"] == ai.RESCHEDULED_EVENT)
    assert set(row["default_roles"]) == {"RMG", "GM"}
