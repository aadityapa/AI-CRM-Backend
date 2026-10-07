"""RMG / GM decide what happens AFTER the AI L1 (7 Oct 2026, user ask) —
`POST /api/candidate-profiles/{id}/ai-l1-decision`: proceed (override to Selected +
hand-off to RMG Review), hold, release. A failed AI needs a reason to be overruled;
a pending AI cannot be "proceeded".

Run:  cd backend && python -m pytest tests/test_ai_l1_decision.py -q
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from tests.test_ta_decision import db, _profile, _actions, RMG  # noqa: F401
from models import AiInterviewLink, PipelineStatus as PS
import routers.crm.candidate_profiles as rp


def _link(db, p, result, score=None):
    link = AiInterviewLink(profile_id=p.id, candidate_id=p.candidate_id, invite_token=f"tok{p.id}",
                           opportunity_id=p.opportunity_id, result=result, overall_score_percent=score,
                           completed_at=datetime.now(timezone.utc) if result != "Pending" else None)
    db.add(link); db.flush()
    return link


def test_a_failed_ai_needs_a_reason_then_proceeds_to_rmg_review(db):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    link = _link(db, p, "Failed", 18.2)
    with pytest.raises(HTTPException) as err:
        rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="proceed", note="no"), db, RMG)
    assert err.value.status_code == 400
    rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="proceed", note="Strong CV, transcript was poor audio"), db, RMG)
    assert p.pipeline_status == PS.RMG_REVIEW
    assert link.hr_decision == "selected" and link.result == "Failed"   # the AI verdict is kept
    assert link.effective_result == "Selected"
    assert "AI_INTERVIEW_DECISION" in _actions(db, p)
    with pytest.raises(HTTPException) as err:
        rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="proceed", note="again"), db, RMG)
    assert err.value.status_code == 409   # already past the pre-review stages


def test_hold_and_release_toggle_the_override_without_moving_the_stage(db):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    link = _link(db, p, "Failed", 40.0)
    rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="hold"), db, RMG)
    assert link.hr_decision == "on_hold" and p.pipeline_status == PS.TECHNICAL_SCREENING
    rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="release"), db, RMG)
    assert link.hr_decision is None
    # a passed interview needs no reason to proceed
    link.result = "Passed"; db.flush()
    rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="proceed"), db, RMG)
    assert p.pipeline_status == PS.RMG_REVIEW


def test_a_pending_ai_cannot_be_proceeded_and_no_link_is_409(db):
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    with pytest.raises(HTTPException) as err:
        rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="hold"), db, RMG)
    assert err.value.status_code == 409
    _link(db, p, "Pending")
    with pytest.raises(HTTPException) as err:
        rp.ai_l1_decision(p.id, rp.AiL1DecisionIn(decision="proceed", note="skip the wait"), db, RMG)
    assert err.value.status_code == 409
