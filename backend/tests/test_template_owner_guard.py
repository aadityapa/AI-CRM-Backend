"""13 Oct 2026 — an AI L1 never runs on another opportunity's template, and an
interview that already did says so.

Reported: a Bluetooth Developer candidate (C-2026-00095) was interviewed on the
"AGM - R&D (ADAS & ARAS)" template of C-2026-00097 and the profile read "AI L1
Passed". The 8 Oct fix copies a template at LINK time; a request linked before it
still pointed at the other deal's template, so every new AI L1 for the position
would have repeated the mistake. These pin the schedule-time check and the warning.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services import ai_interview_bridge as bridge

OPP = SimpleNamespace(id=95, opp_id="C-2026-00095", pipeline_stage="Active")
AGM = {"jobId": "agm-rd", "jobTitle": "AGM - Research & Development (ADAS & ARAS)", "opportunityId": "C-2026-00097"}
BT = {"jobId": "bt-dev", "jobTitle": "Bluetooth Developer", "opportunityId": "C-2026-00095"}


@pytest.fixture
def templates(monkeypatch):
    import auth_db
    store = {"agm-rd": AGM, "bt-dev": BT}
    monkeypatch.setattr(auth_db, "get_job_template", lambda target, job_id: store.get(job_id))
    monkeypatch.setattr(bridge, "_legacy_db_target", lambda: "unused")
    monkeypatch.setattr(bridge, "_open_template_request", lambda db, opp, req: None)
    return store


def _status_for(monkeypatch, job: str) -> dict:
    monkeypatch.setattr(bridge, "resolve_l1_template_job_id", lambda db, opp, req: job)
    return bridge.l1_template_status(None, OPP, None)


def test_the_ownership_rule_is_one_pure_function_shared_with_the_link_code():
    from routers.crm.template_requests import template_owner_conflict as router_rule
    assert router_rule is bridge.template_owner_conflict
    assert bridge.template_owner_conflict(AGM, "C-2026-00095") == "C-2026-00097"
    assert bridge.template_owner_conflict(BT, " c-2026-00095 ") == ""
    assert bridge.template_owner_conflict({"opportunityId": ""}, "C-2026-00095") == ""


def test_a_template_owned_by_another_deal_is_refused_with_the_fix_spelled_out(templates, monkeypatch):
    st = _status_for(monkeypatch, "agm-rd")
    assert st["ready"] is False
    assert st["state"] == "other_opportunity"
    assert st["code"] == bridge.TEMPLATE_OTHER_OPPORTUNITY_CODE
    assert st["template_owner"] == "C-2026-00097"
    assert "AGM - Research & Development" in st["template_title"]
    assert "Change template" in st["reason"] and "C-2026-00095" in st["reason"]
    with pytest.raises(HTTPException) as exc:
        monkeypatch.setattr(bridge, "l1_template_status", lambda db, o, r: st)
        bridge.ensure_l1_template_ready(None, OPP, None)
    assert exc.value.status_code == 400 and "belongs to C-2026-00097" in exc.value.detail


def test_the_deals_own_template_and_an_unlinked_one_behave_as_before(templates, monkeypatch):
    assert _status_for(monkeypatch, "bt-dev")["ready"] is True
    assert _status_for(monkeypatch, "")["state"] == "missing"


def test_the_candidates_own_booking_cannot_slip_past_the_check(templates, monkeypatch):
    """The slot-confirm path calls schedule_l1_interview without the route check."""
    import services.invite_links as invite_links
    monkeypatch.setattr(invite_links, "resolve_invite_base", lambda request: "https://karnex.test")
    monkeypatch.setattr(bridge, "resolve_l1_template_job_id", lambda db, opp, req: "agm-rd")
    monkeypatch.setattr(bridge, "_skills_for", lambda db, req, opp_id: ["Bluetooth"])
    db = SimpleNamespace(get=lambda model, pk: OPP)
    profile = SimpleNamespace(id=19492, opportunity_id=95)
    out = bridge.schedule_l1_interview(db, SimpleNamespace(id=1), None, profile)
    assert out["scheduled"] is False and out["link_id"] is None
    assert "C-2026-00097" in out["error"]


def test_a_finished_interview_on_the_wrong_template_is_flagged_and_a_voided_one_is_not(templates, monkeypatch):
    import auth_db
    from routers.crm import ai_interviews as router

    monkeypatch.setattr(router, "_legacy_target", lambda: "unused")
    monkeypatch.setattr(auth_db, "interview_record_job_ids",
                        lambda target, ids: {"r-1": "agm-rd", "r-2": "agm-rd", "r-3": "bt-dev"})
    db = SimpleNamespace(get=lambda model, pk: OPP)
    profile = SimpleNamespace(opportunity_id=95)
    links = [
        SimpleNamespace(id=1, interview_record_id="r-1", voided_at=None),
        SimpleNamespace(id=2, interview_record_id="r-2", voided_at="2026-10-13"),
        SimpleNamespace(id=3, interview_record_id="r-3", voided_at=None),
        SimpleNamespace(id=4, interview_record_id=None, voided_at=None),
    ]
    out = router.wrong_templates_for_links(db, profile, links)
    assert set(out) == {1}
    assert out[1]["owner"] == "C-2026-00097" and out[1]["opportunity"] == "C-2026-00095"
    # The single-link summary path agrees.
    assert router.wrong_template_for(db, profile, links[0], {"job_id": "agm-rd"})["owner"] == "C-2026-00097"
    assert router.wrong_template_for(db, profile, links[1], {"job_id": "agm-rd"}) is None


def test_the_job_id_is_read_without_loading_the_interview(tmp_path):
    import auth_db
    db = tmp_path / "auth.db"
    auth_db.init_auth_db(db)
    auth_db.upsert_interview_record_snapshot(db, {"id": "rec-1", "job_id": "agm-rd", "candidate_name": "A",
                                                  "candidate_email": "a@x.in", "questions": ["q"] * 50})
    assert auth_db.interview_record_job_ids(db, ["rec-1", "missing", ""]) == {"rec-1": "agm-rd"}
    assert auth_db.interview_record_job_ids(db, []) == {}
