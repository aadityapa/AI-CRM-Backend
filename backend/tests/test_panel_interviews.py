"""My Interviews — the panel member's own rounds (7 Oct 2026, user flow): eight
Interviewer logins, each sees ONLY the candidates whose technical interview
they take (CV · AI L1 · the round) and records that round's feedback; someone
else's round is a 404; the verdict goes through the same path as RMG's form
(a "No Hire" closes the candidacy); the Interviewer role grants one tab.

Run:  cd backend && python -m pytest tests/test_panel_interviews.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from tests.test_ta_decision import db, _profile, _actions  # noqa: F401
from crm_deps import CurrentUser
from models import Employee, InterviewEvent, PipelineStatus as PS
from services import panel_interviews as pi
from services import work_desk

ASHA = CurrentUser(id=11, username="asha", full_name="Asha Rao", email="asha@karnex.in", roles={"Interviewer"})
VIK = CurrentUser(id=12, username="vik", full_name="Vikram Nair", email="vikram@karnex.in", roles={"Interviewer"})


def _employee(db, first, last, email, user_id=None):
    e = Employee(first_name=first, last_name=last, email=email, user_id=user_id, is_active=True)
    db.add(e); db.flush()
    return e


def _round(db, p, emp, kind="L1_Interview", *, hours_ago=2.0, result=None, status="Scheduled"):
    ev = InterviewEvent(profile_id=p.id, candidate_id=p.candidate_id, kind=kind, employee_id=emp.id if emp else None,
                        interviewer=f"{emp.first_name} {emp.last_name}" if emp else None,
                        scheduled_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
                        duration_minutes=60, status=status, result=result, created_by=1)
    db.add(ev); db.flush()
    return ev


@pytest.fixture()
def quiet(monkeypatch):
    from services import rmg_tasks
    monkeypatch.setattr(rmg_tasks, "_notify_result", lambda *a, **k: None)


def test_a_login_is_its_employee_by_user_link_or_official_mailbox(db):
    by_link = _employee(db, "Asha", "Rao", "other@karnex.in", user_id=11)
    by_mail = _employee(db, "Vikram", "Nair", "VIKRAM@karnex.in")
    _employee(db, "Nobody", "Else", "x@karnex.in")
    assert pi.my_employee_ids(db, ASHA) == [by_link.id]
    assert pi.my_employee_ids(db, VIK) == [by_mail.id]
    assert pi.panel_user_ids(db, by_link.id) == [11]
    assert pi.employee_by_name(db, "  vikram   nair ").id == by_mail.id
    _employee(db, "Vikram", "Nair", "vik2@karnex.in")
    assert pi.employee_by_name(db, "Vikram Nair") is None     # two of them — never a guess


def test_each_panel_member_sees_only_their_own_rounds(db, quiet):
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    vik = _employee(db, "Vikram", "Nair", "vikram@karnex.in", user_id=12)
    p1, p2, p3 = _profile(db), _profile(db), _profile(db)
    mine_due = _round(db, p1, asha)                       # over, no verdict → pending
    mine_soon = _round(db, p2, asha, hours_ago=-30)       # tomorrow → upcoming
    _round(db, p3, vik)                                   # Vikram's
    _round(db, p1, asha, kind="Customer_Interview")       # not a panel round
    _round(db, p2, asha, kind="L2_F2F", status="Cancelled")  # not held

    data = pi.my_rounds(db, ASHA, "all")
    assert data["linked"] and data["counts"] == {"pending": 1, "upcoming": 1, "done": 0}
    assert [r["id"] for r in data["rows"]] == [mine_due.id, mine_soon.id]
    row = data["rows"][0]
    assert row["candidate"]["name"] == "Ravi" and row["position"]["customer_name"].startswith("VISTEON")
    assert row["round_label"] == "L1 - Interview" and row["phase"] == "pending"
    assert [r["id"] for r in pi.my_rounds(db, ASHA, "pending")["rows"]] == [mine_due.id]

    detail = pi.round_detail(db, ASHA, mine_due.id)
    assert detail["results_scale"] == pi.RESULTS and detail["candidate"]["id"] == p1.candidate_id
    with pytest.raises(HTTPException) as e:
        pi.round_detail(db, ASHA, [r.id for r in db.query(InterviewEvent).filter_by(profile_id=p3.id)][0])
    assert e.value.status_code == 404                      # Vikram's round: not found, never forbidden
    assert pi.my_rounds(db, CurrentUser(id=99, username="n", roles={"Interviewer"}), "all")["linked"] is False


def test_the_feedback_goes_through_the_one_verdict_path_and_closes_on_no_hire(db, quiet):
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    ev = _round(db, p, asha)
    with pytest.raises(HTTPException) as e:
        pi.record_panel_feedback(db, ASHA, ev.id, "Maybe", "ok")
    assert e.value.status_code == 400 and "Pick a result" in e.value.detail
    with pytest.raises(HTTPException) as e:
        pi.record_panel_feedback(db, ASHA, ev.id, "Hire", "ok")
    assert e.value.status_code == 400                      # feedback is the point of the page

    row, moved = pi.record_panel_feedback(db, ASHA, ev.id, "no hire", "Could not explain a mutex.")
    assert row["result"] == "No Hire" and row["status"] == "Completed" and row["phase"] == "done"
    assert ev.user_role == pi.PANEL_USER_ROLE
    assert moved == PS.RMG_REJECTED.value and p.pipeline_status == PS.RMG_REJECTED
    acts = _actions(db, p)
    assert {"INTERVIEW_ROUND_ADDED", "RESULT_RECORDED", "STATUS_CHANGE"} <= acts

    # Vikram cannot record on Asha's round.
    p2 = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    ev2 = _round(db, p2, asha)
    with pytest.raises(HTTPException) as e:
        pi.record_panel_feedback(db, VIK, ev2.id, "Hire", "Strong on RTOS, clear answers.")
    assert e.value.status_code == 404


def test_the_desk_gives_a_panel_only_login_its_own_rounds_and_nothing_else(db, quiet):
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    _round(db, p, asha)
    _round(db, _profile(db), asha, hours_ago=-50)
    assert pi.is_panel_only(ASHA) and not pi.is_panel_only(CurrentUser(id=1, username="t", roles={"TA"}))
    desk = work_desk.desk(db, ASHA)
    keys = [t["key"] for t in desk["tabs"]]
    assert keys == ["panel_feedback", "panel_upcoming"]
    due = desk["tabs"][0]
    assert due["count"] == 1 and due["link"] == "my-interviews?scope=pending"
    assert due["items"][0]["path"].startswith("my-interviews?focus=")


def test_the_interviewer_role_is_seeded_with_the_one_tab_and_the_registry_knows_it():
    from services.access_registry import TABS, validate_access
    assert "my-interviews" in TABS
    validate_access(pi.INTERVIEWER_TAB_ACCESS, None)
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0128_interviewer_role_and_round_employee_link.py"
    spec = importlib.util.spec_from_file_location("m0128", path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    assert mig.TAB_ACCESS == pi.INTERVIEWER_TAB_ACCESS and mig.ROLE_NAME == pi.INTERVIEWER_ROLE_NAME
    assert mig.PANEL_KINDS == pi.PANEL_ROUND_KINDS


def test_the_routes_are_registered_and_the_panel_member_is_told_of_a_booking(db, monkeypatch, quiet):
    from routers.crm import _MODULES
    assert "my_interviews" in _MODULES
    told: list = []
    from services import notify
    monkeypatch.setattr(notify, "notify_user", lambda db, uid, title, message="", link="", **k: told.append((uid, title, link)))
    asha = _employee(db, "Asha", "Rao", "asha@karnex.in", user_id=11)
    p = _profile(db, PS.TECHNICAL_SCREENING, screening="Shortlisted")
    ev = _round(db, p, asha, hours_ago=-24)
    booked_by = CurrentUser(id=1, username="ta", roles={"TA"})
    assert pi.notify_panel_member(db, p, ev, booked_by) == 1
    uid, title, link = told[0]
    assert uid == 11 and title.startswith("You are taking the L1 - Interview") and f"focus={ev.id}" in link
    # The person who booked it is never told they booked it.
    assert pi.notify_panel_member(db, p, ev, ASHA) == 0
