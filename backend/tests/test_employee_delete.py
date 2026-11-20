"""Admin / CEO delete an employee in one step (20 Nov 2026).

User report: Admin could not remove a test employee — "employee has 15 leave
ledger event(s)". Run: cd backend && python -m pytest tests/test_employee_delete.py -q
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
    Customer, Employee, EmployeeLeaveBalance, EmployeeProjectHistory, InterviewEvent, Invoice,
    LeaveAccrualEvent, LeaveApplication, LeavePolicyType, Project, ProjectEmployee,
    ProjectEmployeeRate, Timesheet, TimesheetEntry, TimesheetStatus,
)
from models.finance import InvoiceKind  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from routers.crm import employees as r  # noqa: E402


ADMIN = CurrentUser(id=1, username="admin", roles={"Admin"})
HR = CurrentUser(id=1, username="hr", roles={"HR"})


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


@pytest.fixture()
def person(db):
    cust = Customer(name="HARMAN"); db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Infotainment"); db.add(proj); db.flush()
    lt = LeavePolicyType(name="Casual Leave"); db.add(lt); db.flush()
    emp = Employee(first_name="Test", last_name="Test", email="test@karnex.in"); db.add(emp); db.flush()
    boss = Employee(first_name="Peer", email="peer@karnex.in", reporting_manager_id=emp.id)
    db.add(boss)
    pe = ProjectEmployee(project_id=proj.id, employee_id=emp.id, billing_rate=100000,
                         onboarding_date=date(2026, 7, 1))
    db.add(pe); db.flush()
    db.add(ProjectEmployeeRate(project_employee_id=pe.id, rate=100000,
                               effective_from=date(2026, 7, 1)))
    db.add(EmployeeProjectHistory(employee_id=emp.id, project_id=proj.id, start_date=date(2026, 7, 1)))
    db.add(EmployeeLeaveBalance(employee_id=emp.id, leave_type_id=lt.id, year=2026))
    for i in range(15):
        db.add(LeaveAccrualEvent(employee_id=emp.id, leave_type_id=lt.id, event_type="Accrual",
                                 amount=1, source=f"pe_credit:{i}"))
    db.add(LeaveApplication(employee_id=emp.id, project_employee_id=pe.id, leave_type_id=lt.id,
                            leave_period_type="Full_Day", from_date=date(2026, 8, 3),
                            to_date=date(2026, 8, 3), days=1))
    ts = Timesheet(project_id=proj.id, employee_id=emp.id, project_employee_id=pe.id,
                   month=8, year=2026, status=TimesheetStatus.APPROVED)
    db.add(ts); db.flush()
    db.add(TimesheetEntry(timesheet_id=ts.id, entry_date=date(2026, 8, 3)))
    db.commit()
    return emp, proj, ts


def test_hr_is_told_to_deactivate_in_plain_words(db, person):
    emp, _, _ = person
    with pytest.raises(HTTPException) as e:
        r.delete_employee(emp.id, db=db, user=HR)
    assert e.value.status_code == 409
    msg = e.value.detail
    assert "15 leave ledger event(s)" in msg and "Admin / CEO" in msg
    assert "is_active" not in msg


def test_admin_deletes_the_employee_with_everything_only_theirs(db, person):
    emp, proj, ts = person
    db.add(Invoice(invoice_number="PI-2026-001", project_id=proj.id, timesheet_id=ts.id,
                   kind=InvoiceKind.PROFORMA.value, invoice_date=date(2026, 9, 1),
                   sub_total=100, grand_total=118))
    db.commit()
    out = r.delete_employee(emp.id, db=db, user=ADMIN)
    assert out["success"] and "Test Test deleted" in out["message"]
    assert out["data"]["removed"]["leave_events"] == 15
    assert out["data"]["removed"]["proformas"] == 1
    db.expire_all()
    assert db.get(Employee, emp.id) is None
    for model in (LeaveAccrualEvent, LeaveApplication, Timesheet, TimesheetEntry, ProjectEmployee,
                  ProjectEmployeeRate, EmployeeLeaveBalance, EmployeeProjectHistory, Invoice):
        assert db.query(model).count() == 0, model.__name__
    peer = db.query(Employee).one()
    assert peer.reporting_manager_id is None          # pointer cleared, peer kept


def test_a_tax_invoice_still_blocks_even_for_admin(db, person):
    emp, proj, ts = person
    db.add(Invoice(invoice_number="KRNX26-27-09", project_id=proj.id, timesheet_id=ts.id,
                   kind=InvoiceKind.TAX.value, invoice_date=date(2026, 9, 1),
                   sub_total=100, grand_total=118))
    db.commit()
    with pytest.raises(HTTPException) as e:
        r.delete_employee(emp.id, db=db, user=ADMIN)
    assert e.value.status_code == 409 and "KRNX26-27-09" in e.value.detail
    db.rollback()
    assert db.get(Employee, emp.id) is not None
