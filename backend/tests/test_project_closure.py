"""Project close → team on the bench (25 Sep 2026) — `services.project_closure`.

A project can now be closed with a LAST WORKING DAY and a reason. A future day
is scheduled (assignments capped, team keeps working), a past day closes at
once, and the daily job exits the team the day after. Everything runs on
in-memory SQLite with the usual Postgres-type shims.
"""
from __future__ import annotations

import importlib
import inspect
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Customer, Employee, EmployeeProjectHistory, Project, ProjectEmployee, ProjectStatus,
    Timesheet, TimesheetStatus,
)
from services import project_closure as pc  # noqa: E402

TODAY = date(2026, 9, 25)
REASON = "Customer ended the engagement"


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    # Mail is exercised elsewhere; here we only record that it was asked for.
    sent: list[str] = []
    monkeypatch.setattr(pc, "_notify", lambda db, project, **kw: sent.append(kw["event"]))
    s.info["sent"] = sent
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def team(db):
    cust = Customer(name="HARMAN"); db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Infotainment"); db.add(proj); db.flush()
    people = []
    for i, (name, onboard, exit_on) in enumerate([
        ("Asha", date(2026, 1, 5), None),
        ("Ravi", date(2026, 3, 1), date(2026, 10, 10)),   # planned exit AFTER the close
        ("Meena", date(2026, 2, 1), date(2026, 9, 20)),   # planned exit BEFORE the close
    ]):
        emp = Employee(first_name=name, email=f"{name.lower()}@karnex.in"); db.add(emp); db.flush()
        pe = ProjectEmployee(project_id=proj.id, employee_id=emp.id, billing_rate=100000,
                             onboarding_date=onboard, exit_date=exit_on)
        db.add(pe); db.flush()
        db.add(EmployeeProjectHistory(employee_id=emp.id, project_id=proj.id, start_date=onboard))
        people.append((emp, pe))
    db.commit()
    return proj, people


def test_a_future_last_day_is_scheduled_and_caps_only_later_exits(db, team):
    proj, people = team
    out = pc.schedule_project_close(db, proj, end_date=date(2026, 9, 30), reason=REASON,
                                    user_id=1, today=TODAY)
    db.commit()
    assert out["state"] == pc.STATE_SCHEDULED and out["released"] == []
    (asha, pa), (ravi, pr), (meena, pm) = people
    assert pa.exit_date == date(2026, 9, 30)         # open-ended → capped
    assert pr.exit_date == date(2026, 9, 30)         # later plan → capped
    assert pm.exit_date == date(2026, 9, 20)         # earlier plan → kept
    # Nobody has left yet — they keep working until the last day.
    assert all(pe.is_active and not pe.is_exit for _, pe in people)
    assert proj.closed_at is None and pc.closure_state(proj) == pc.STATE_SCHEDULED
    assert pc.closure_out(proj, TODAY)["days_left"] == 6   # 25..30 Sep inclusive


def test_a_past_last_day_closes_at_once_and_moves_the_team_to_the_bench(db, team):
    proj, people = team
    out = pc.schedule_project_close(db, proj, end_date=date(2026, 9, 22), reason=REASON,
                                    user_id=1, today=TODAY)
    db.commit()
    assert out["state"] == pc.STATE_CLOSED
    assert {r["name"]: r["exit_date"] for r in out["released"]} == {
        "Asha": "2026-09-22", "Ravi": "2026-09-22", "Meena": "2026-09-20"}
    assert all(pe.is_exit and not pe.is_active for _, pe in people)
    assert proj.status == ProjectStatus.COMPLETED and proj.closed_at is not None
    hist = db.query(EmployeeProjectHistory).all()
    assert {h.end_date for h in hist} == {date(2026, 9, 22), date(2026, 9, 20)}
    dep = pc.deployment_by_employee(db, [e.id for e, _ in people], TODAY)
    assert {v["status"] for v in dep.values()} == {pc.BENCH}


def test_the_last_working_day_itself_is_still_deployed(db, team):
    proj, people = team
    pc.schedule_project_close(db, proj, end_date=TODAY, reason=REASON, user_id=1, today=TODAY)
    db.commit()
    asha = people[0][0]
    assert pc.deployment_by_employee(db, [asha.id], TODAY)[asha.id]["status"] == pc.DEPLOYED
    # ...and the day after, the job has moved them.
    assert pc.run_project_closures(db, date(2026, 9, 26)) == {"closed": 1, "released": 3}
    assert pc.deployment_by_employee(db, [asha.id], date(2026, 9, 26))[asha.id]["status"] == pc.BENCH


def test_the_daily_job_is_idempotent_and_announces_once(db, team):
    proj, _ = team
    pc.schedule_project_close(db, proj, end_date=date(2026, 9, 30), reason=REASON, user_id=1, today=TODAY)
    db.commit()
    assert pc.run_project_closures(db, date(2026, 9, 30)) == {"closed": 0, "released": 0}  # not yet
    assert pc.run_project_closures(db, date(2026, 10, 1)) == {"closed": 1, "released": 3}
    assert pc.run_project_closures(db, date(2026, 10, 2)) == {"closed": 0, "released": 0}
    assert db.info["sent"] == ["project.closed"]


def test_the_reason_is_required(db, team):
    proj, _ = team
    with pytest.raises(pc.ClosureError):
        pc.schedule_project_close(db, proj, end_date=TODAY, reason="done", user_id=1, today=TODAY)


def test_someone_onboarding_after_the_last_day_blocks_the_close_by_name(db, team):
    proj, people = team
    people[1][1].onboarding_date = date(2026, 10, 5)
    db.commit()
    with pytest.raises(pc.ClosureError, match="Ravi"):
        pc.schedule_project_close(db, proj, end_date=date(2026, 9, 30), reason=REASON, user_id=1, today=TODAY)
    assert proj.end_date is None


def test_a_closed_project_cannot_be_closed_or_cancelled_again(db, team):
    proj, _ = team
    pc.schedule_project_close(db, proj, end_date=date(2026, 9, 1), reason=REASON, user_id=1, today=TODAY)
    db.commit()
    with pytest.raises(pc.ClosureConflict):
        pc.schedule_project_close(db, proj, end_date=date(2026, 9, 2), reason=REASON, user_id=1, today=TODAY)
    with pytest.raises(pc.ClosureConflict):
        pc.cancel_project_close(db, proj)


def test_cancelling_a_scheduled_close_restores_every_exit_it_capped(db, team):
    proj, people = team
    pc.schedule_project_close(db, proj, end_date=date(2026, 9, 30), reason=REASON, user_id=1, today=TODAY)
    db.commit()
    assert pc.cancel_project_close(db, proj) == 2
    db.commit()
    (_, pa), (_, pr), (_, pm) = people
    assert pa.exit_date is None                    # open-ended again
    assert pr.exit_date == date(2026, 10, 10)      # his own later plan, not lost
    assert pm.exit_date == date(2026, 9, 20)       # never touched
    assert proj.end_date is None and proj.closed_reason is None and proj.closure_capped_exits is None
    assert pc.closure_state(proj) == pc.STATE_OPEN


def test_closing_flags_open_timesheets_for_settlement(db, team):
    proj, people = team
    asha, _ = people[0]
    db.add(Timesheet(project_id=proj.id, employee_id=asha.id, month=9, year=2026,
                     status=TimesheetStatus.DRAFT))
    db.commit()
    pc.schedule_project_close(db, proj, end_date=date(2026, 9, 22), reason=REASON, user_id=1, today=TODAY)
    db.commit()
    ts = db.query(Timesheet).one()
    assert "flagged for settlement" in (ts.rejection_reason or "")


def test_inactive_employees_are_neither_deployed_nor_bench_in_the_list():
    """The Employees list only calls someone "Bench" while they are active."""
    from routers.crm import employees
    src = inspect.getsource(employees.list_employees)
    assert 'deployment_status' in src and "e.is_active" in src


def test_close_routes_are_gated_by_the_close_action():
    from routers.crm import projects
    src = inspect.getsource(projects)
    assert 'gated_write_action("project.close", "projects")' in src
    from services.action_permissions import ACTIONS, is_approval
    assert "project.close" in ACTIONS and not is_approval("project.close")


def test_a_status_edit_cannot_skip_the_close_flow():
    from routers.crm import projects
    src = inspect.getsource(projects._guard_status_change)
    assert "Use Close project" in src and "reopen_fields" in src


def test_both_close_events_are_admin_routable():
    from routers.crm.email_flows import EVENTS
    names = {e["event"] for e in EVENTS}
    assert {"project.close_scheduled", "project.closed"} <= names


def test_the_job_is_registered_with_its_setting():
    from services.scheduler import DEFAULTS, JOBS
    assert JOBS["project_closures"][0] == "scheduler.project_closures"
    assert DEFAULTS["scheduler.project_closures"] == "true"
