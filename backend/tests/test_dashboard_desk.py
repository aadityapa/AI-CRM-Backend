"""Role desk endpoints behind the redesigned Dashboard tab (14 Sep 2026).

Run:  cd backend && python -m pytest tests/test_dashboard_desk.py -q
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
    "support",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
import crm_deps  # noqa: E402
from services import dashboard_desk as desk  # noqa: E402
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


def _seed(db):
    from models import (
        Customer, Employee, Invoice, PaymentStatus, POStatus, Project, ProjectEmployee,
        PurchaseOrder, Timesheet, TimesheetStatus,
    )

    cust = Customer(name="Uno Minda")
    db.add(cust)
    db.flush()
    project = Project(name="Uno Minda Delivery", customer_id=cust.id)
    emp = Employee(first_name="Ankit", last_name="Bhimani", email="ankit@karnex.in")
    db.add_all([project, emp])
    db.flush()
    pe = ProjectEmployee(project_id=project.id, employee_id=emp.id,
                         onboarding_date=date(2026, 1, 1), billing_rate=1000,
                         is_active=True, is_exit=False,
                         exit_date=date.today() + timedelta(days=3))
    db.add(pe)
    db.flush()
    db.add(Timesheet(project_id=project.id, employee_id=emp.id, project_employee_id=pe.id,
                     month=8, year=2026, status=TimesheetStatus.APPROVED))
    db.add(Timesheet(project_id=project.id, employee_id=emp.id, project_employee_id=pe.id,
                     month=7, year=2026, status=TimesheetStatus.SUBMITTED))
    db.add(PurchaseOrder(po_number="PO-1", customer_id=cust.id, total_value=100, consumed_value=0,
                         balance_value=100, status=POStatus.ACTIVE,
                         end_date=date.today() + timedelta(days=5)))
    db.add(Invoice(invoice_number="INV-1", project_id=project.id, invoice_date=date.today() - timedelta(days=40),
                   due_date=date.today() - timedelta(days=10), sub_total=1000, tax_amount=180,
                   grand_total=1180, paid_amount=0, balance_amount=1180,
                   payment_status=PaymentStatus.UNPAID))
    db.commit()


def test_finance_tiles_show_billing_state(db):
    _seed(db)
    out = desk.today_tiles(db, _user("Finance"))
    by = {t["key"]: t for t in out["tiles"]}
    assert by["fin_ready_to_invoice"]["value"] == 1          # Approved sheet, no invoice
    assert by["fin_overdue"]["value"] == 1180.0 and by["fin_overdue"]["state"] == "bad"
    assert by["fin_overdue"]["format"] == "money"
    assert by["po_expiring"]["value"] == 1
    assert all(t["path"] for t in out["tiles"])


def test_tiles_follow_roles_and_are_capped(db):
    _seed(db)
    assert desk.today_tiles(db, _user("TA"))["tiles"][0]["key"] == "ta_sourcing_sla"
    keys = {t["key"] for t in desk.today_tiles(db, _user("HR"))["tiles"]}
    assert "hr_leave_pending" in keys and "fin_overdue" not in keys
    many = desk.today_tiles(db, _user("TA", "RMG", "Sales", "Finance", "HR"))["tiles"]
    assert len(many) <= desk.MAX_TILES
    assert len({t["key"] for t in many}) == len(many)


def test_admin_gets_the_company_desk(db):
    _seed(db)
    keys = [t["key"] for t in desk.today_tiles(db, _user("Admin"))["tiles"]]
    assert keys[:4] == ["co_open_positions", "co_deployed", "co_cash_at_risk", "co_approvals"]


def test_upcoming_lists_dated_items_the_caller_can_open(db):
    _seed(db)
    fin = desk.upcoming(db, _user("Finance"), days=7)["items"]
    kinds = {i["kind"] for i in fin}
    assert "po_expiry" in kinds and "rolloff" not in kinds     # Finance cannot open project employees
    hr = desk.upcoming(db, _user("HR"), days=7)["items"]
    assert {i["kind"] for i in hr} == {"rolloff"}
    assert all(i["when"] and i["path"] for i in fin + hr)
    whens = [i["when"] for i in desk.upcoming(db, _user("Admin"), days=7)["items"]]
    assert whens == sorted(whens)


def test_team_overview_names_the_stuck_points(db):
    _seed(db)
    out = desk.team_overview(db, _user("Sales_Head"))
    areas = {s["area"] for s in out["stuck"]}
    assert "Finance" in areas                      # overdue invoice + expiring PO
    assert out["stuck"][0]["state"] == "bad"       # worst first
    assert isinstance(out["ta"], list) and isinstance(out["sales"], list)


def test_my_work_gained_the_new_role_items(db):
    _seed(db)
    fin = {i["key"] for i in my_work(db, _user("Finance"))["items"]}
    assert "timesheets_to_invoice" in fin
    assert "customer_feedback_to_chase" not in fin
