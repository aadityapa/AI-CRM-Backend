"""My Work — the role-and-template-aware to-do list on the CRM dashboard.

Run:  cd backend && python -m pytest tests/test_my_work.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, timedelta

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
import crm_deps  # noqa: E402
from services.dashboards import my_work  # noqa: E402


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


def _user(*roles: str) -> crm_deps.CurrentUser:
    return crm_deps.CurrentUser(id=1, username="u", roles=set(roles))


def _seed_pending_work(db):
    from models import (
        Customer, Employee, POStatus, Project, ProjectEmployee, PurchaseOrder,
        Timesheet, TimesheetStatus,
    )

    cust = Customer(name="Aptiv")
    db.add(cust)
    db.flush()
    project = Project(name="Aptiv Delivery", customer_id=cust.id)
    emp = Employee(first_name="Neha", email="neha@karnex.in")
    db.add_all([project, emp])
    db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id,
                         onboarding_date=date(2026, 1, 1), billing_rate=1000,
                         is_active=True, is_exit=False)
    db.add(pe)
    db.flush()
    db.add(Timesheet(project_id=project.id, employee_id=emp.id,
                     project_employee_id=pe.id, month=7, year=2026,
                     status=TimesheetStatus.SUBMITTED))
    db.add(PurchaseOrder(po_number="PO-1", customer_id=cust.id,
                         total_value=100, consumed_value=0, balance_value=100,
                         status=POStatus.ACTIVE,
                         end_date=date.today() + timedelta(days=10)))
    db.commit()


def test_items_match_the_callers_roles(db):
    _seed_pending_work(db)

    rmg = my_work(db, _user("RMG"))
    assert any(i["key"] == "timesheets_to_approve" for i in rmg["items"])
    assert not any(i["key"] == "pos_expiring" for i in rmg["items"]), \
        "PO expiry is Finance/Sales_Head work, not RMG's"

    finance = my_work(db, _user("Finance"))
    assert any(i["key"] == "pos_expiring" for i in finance["items"])
    assert not any(i["key"] == "timesheets_to_approve" for i in finance["items"])


def test_zero_counts_are_dropped_and_all_clear_reported(db):
    out = my_work(db, _user("TA"))
    assert out["items"] == []
    assert out["all_clear"] is True


def test_template_hides_items_for_hidden_tabs(db):
    from models import AccessTemplate, UserProfile

    _seed_pending_work(db)
    # RMG user whose template shows only candidates — timesheets tab hidden.
    t = AccessTemplate(name="Narrow", is_active=True, tab_access={"candidates": "edit"})
    db.add(t)
    db.flush()
    db.add(UserProfile(user_id=1, access_template_id=t.id))
    db.commit()

    out = my_work(db, _user("RMG"))
    assert not any(i["key"] == "timesheets_to_approve" for i in out["items"]), \
        "a to-do pointing at a tab the user cannot open is an irritation, not a task"


def test_admin_sees_everything_pending(db):
    _seed_pending_work(db)
    out = my_work(db, _user("Admin"))
    keys = {i["key"] for i in out["items"]}
    assert {"timesheets_to_approve", "pos_expiring"} <= keys
    # Danger items sort before warnings.
    urgencies = [i["urgency"] for i in out["items"]]
    assert urgencies == sorted(urgencies, key=lambda u: {"danger": 0, "warning": 1}.get(u, 2))
