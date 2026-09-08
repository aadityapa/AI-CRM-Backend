"""Leave credit as a scheduler job — gap detection, replay, and the alert rule.

The point of moving this job into the scheduler was that a missed month used to
be invisible. These tests pin the three behaviours that make it visible:
gaps are found, replaying them is safe, and a replay that changes nothing does
not raise an alarm.

Run:  cd backend && python -m pytest tests/test_leave_credit_scheduler.py -q
"""
from __future__ import annotations

import importlib
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
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services import project_employee_leave_credit as credit  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _seed_accruing_pe(db: Session, *, effective: date) -> int:
    """One customer -> project -> employee -> PE with one monthly leave row."""
    from models import (
        Customer, CustomerLeavePolicy, Employee, LeavePolicyType, Opportunity,
        OppType, Project, ProjectEmployee, ProjectEmployeeLeaveDetail,
    )

    customer = Customer(name="Acme Ltd")
    db.add(customer)
    db.flush()

    opportunity = Opportunity(opp_id="OPP-LC-1", title="Acme", customer_id=customer.id,
                              opp_type=OppType.T_AND_M, created_by=1)
    db.add(opportunity)
    db.flush()

    project = Project(name="Acme Delivery", customer_id=customer.id,
                      opportunity_id=opportunity.id)
    leave_type = LeavePolicyType(name="Casual Leave")
    employee = Employee(first_name="Asha", last_name="Rao", email="asha@acme.test")
    db.add_all([project, leave_type, employee])
    db.flush()

    policy = CustomerLeavePolicy(
        customer_id=customer.id,
        leave_type_id=leave_type.id,
        leave_credit_balance=1,
        leave_credit_type="Monthly",
        effective_date=effective,
        is_active=True,
    )
    db.add(policy)
    db.flush()

    pe = ProjectEmployee(project_id=project.id, employee_id=employee.id,
                         onboarding_date=effective, billing_rate=1000,
                         is_active=True, is_exit=False)
    db.add(pe)
    db.flush()

    db.add(ProjectEmployeeLeaveDetail(
        project_employee_id=pe.id,
        leave_type_id=leave_type.id,
        customer_leave_policy_id=policy.id,
        leave_balance=0,
    ))
    db.commit()
    return pe.id


def test_month_end_lands_on_the_last_day():
    assert credit.month_end(2026, 2) == date(2026, 2, 28)
    assert credit.month_end(2024, 2) == date(2024, 2, 29)  # leap year
    # December's month end is 31 Dec, which is the only date year-end carry
    # forward acts on — that is why replay uses month ends.
    assert credit.month_end(2026, 12) == date(2026, 12, 31)


def test_missing_periods_lists_closed_months_only(db):
    _seed_accruing_pe(db, effective=date(2026, 1, 1))
    gaps = credit.missing_credit_periods(db, as_of=date(2026, 4, 15))
    # January to March are closed and never ran. April is this run's own job.
    assert gaps == ["2026-01", "2026-02", "2026-03"]
    assert "2026-04" not in gaps


def test_missing_periods_respects_the_lookback_window(db):
    _seed_accruing_pe(db, effective=date(2020, 1, 1))
    gaps = credit.missing_credit_periods(db, as_of=date(2026, 4, 15), lookback_months=2)
    # A system down for years is repaired deliberately, not by a daily job.
    assert gaps == ["2026-02", "2026-03"]


def test_missing_periods_empty_when_nothing_accrues(db):
    assert credit.missing_credit_periods(db, as_of=date(2026, 4, 15)) == []


def test_backfill_credits_each_month_once(db):
    pe_id = _seed_accruing_pe(db, effective=date(2026, 1, 1))
    from models import ProjectEmployeeLeaveDetail

    gaps = credit.missing_credit_periods(db, as_of=date(2026, 4, 15))
    first = credit.run_pe_leave_credit_backfill(db, periods=gaps)
    assert first["periods"] == ["2026-01", "2026-02", "2026-03"]
    assert first["rows_credited"] == 3

    row = db.query(ProjectEmployeeLeaveDetail).filter_by(project_employee_id=pe_id).one()
    assert float(row.leave_balance) == pytest.approx(3.0)

    # Replaying the same months is a no-op — this is what makes it safe to run
    # the repair on every scheduler pass.
    again = credit.run_pe_leave_credit_backfill(db, periods=gaps)
    assert again["rows_credited"] == 0
    db.refresh(row)
    assert float(row.leave_balance) == pytest.approx(3.0)

    assert credit.missing_credit_periods(db, as_of=date(2026, 4, 15)) == []


def test_scheduler_job_repairs_then_credits_today(db, monkeypatch):
    from services import scheduler

    sent: list[dict] = []
    monkeypatch.setattr(
        "services.notify.notify_roles",
        lambda db, roles, title, message="", link="", **kw: sent.append(
            {"roles": roles, "title": title}),
    )

    _seed_accruing_pe(db, effective=date(2026, 1, 1))
    out = scheduler.run_pe_leave_credit_job(db, today=date(2026, 4, 15))

    assert out["repaired_periods"] == ["2026-01", "2026-02", "2026-03"]
    assert out["repaired_rows"] == 3
    assert out["rows_credited"] == 1          # April, this run's own month
    assert len(sent) == 1, "a repair that moved balances must alert"
    assert "missed month" in sent[0]["title"]


def test_scheduler_job_is_quiet_when_nothing_was_missed(db, monkeypatch):
    from services import scheduler

    sent: list[dict] = []
    monkeypatch.setattr(
        "services.notify.notify_roles",
        lambda db, roles, title, message="", link="", **kw: sent.append({"title": title}),
    )

    _seed_accruing_pe(db, effective=date(2026, 4, 1))
    out = scheduler.run_pe_leave_credit_job(db, today=date(2026, 4, 15))

    assert out["repaired_periods"] == []
    assert out["rows_credited"] == 1
    assert sent == [], "no alert when there was nothing to repair"

    # Second pass the same day credits nothing and still says nothing.
    again = scheduler.run_pe_leave_credit_job(db, today=date(2026, 4, 20))
    assert again["rows_credited"] == 0
    assert sent == []


def test_job_is_registered_with_a_switch():
    from services.scheduler import DEFAULTS, JOBS

    assert "pe_leave_credit" in JOBS
    flag_key, fn = JOBS["pe_leave_credit"]
    assert flag_key == "scheduler.pe_leave_credit"
    assert fn.__name__ == "run_pe_leave_credit_job"
    assert DEFAULTS[flag_key] == "true"
