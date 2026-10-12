"""Project leave policies reach employees mapped BEFORE them (12 Oct 2026).

User report: leave policies added on the project did not show on the timesheet.
The timesheet reads the assignment's leave rows, which were copied from the
policies once at assignment and never again.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from tests.test_project_leave_override import db, _map  # noqa: F401 — fixture reuse
from tests.test_timesheet_entry_grid import client  # noqa: F401 — fixture reuse

from models.masters import LeavePolicyType
from models.customers import Customer
from models.opportunities import Opportunity, OppType
from models.projects import Project, ProjectEmployeeLeaveDetail, ProjectLeavePolicy
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent
from models.hr import Employee
from services.project_employees import (
    apply_effective_leave_policies, apply_project_leave_policies_to_team,
    seed_leave_details_from_customer_policy,
)

D = Decimal


def _world(db):  # noqa: F811
    cl = LeavePolicyType(name="Casual Leave")
    el = LeavePolicyType(name="Earned Leave")
    cust = Customer(name="NinePointFive")
    db.add_all([cl, el, cust])
    db.flush()
    opp = Opportunity(opp_id="OPP-LV", title="Lv", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    proj = Project(opportunity_id=opp.id, customer_id=cust.id, name="Lv project")
    emp = Employee(first_name="Lea", email="lea@leave.test")
    db.add_all([proj, emp])
    db.add(CustomerLeavePolicy(
        customer_id=cust.id, leave_type_id=cl.id, leave_credit_type="One_Time",
        leave_credit_timing="Start_Of_Period", leave_credit_balance=D("6"),
        initial_credit_balance=D("0"), prorate_balance_credit=False, is_active=True))
    db.commit()
    return cl, el, proj, emp


def _project_policy(db, proj, lt, amount):
    pol = ProjectLeavePolicy(
        project_id=proj.id, leave_type_id=lt.id, name=lt.name,
        leave_credit_type="One_Time", leave_credit_timing="Start_Of_Period",
        leave_credit_balance=D(amount), initial_credit_balance=D("0"),
        is_max_limit=False, is_active=True)
    db.add(pol)
    db.commit()
    return pol


def _rows(db, pe):
    return {r.leave_type_id: r for r in db.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id)).scalars()}


def test_policies_added_after_mapping_reach_the_employee(db):  # noqa: F811
    cl, el, proj, emp = _world(db)
    pe = _map(db, emp, proj, onboarding=date(2026, 1, 1))
    seed_leave_details_from_customer_policy(db, pe, proj, seed_as_of=date(2026, 6, 1))
    db.commit()
    assert set(_rows(db, pe)) == {cl.id}                    # customer CL only
    pcl = _project_policy(db, proj, cl, "12")               # project overrides CL
    _project_policy(db, proj, el, "15")                     # and adds EL

    out = apply_project_leave_policies_to_team(db, proj)
    db.commit()
    assert out == {"employees": 1, "added": 1, "relinked": 1}
    rows = _rows(db, pe)
    assert rows[cl.id].project_leave_policy_id == pcl.id
    assert rows[cl.id].customer_leave_policy_id is None
    assert rows[cl.id].leave_balance == D("12")             # untouched row re-opens
    assert rows[el.id].leave_balance == D("15")
    adj = db.execute(select(LeaveAccrualEvent).where(
        LeaveAccrualEvent.event_type == "Adjustment")).scalars().all()
    assert [a.amount for a in adj] == [D("6")]               # 6 → 12 is on the ledger

    again = apply_effective_leave_policies(db, pe, proj)    # idempotent
    assert again["added"] == 0 and again["relinked"] == 0


def test_a_used_row_keeps_its_balance_when_relinked(db):  # noqa: F811
    cl, _el, proj, emp = _world(db)
    pe = _map(db, emp, proj, onboarding=date(2026, 1, 1))
    seed_leave_details_from_customer_policy(db, pe, proj, seed_as_of=date(2026, 6, 1))
    row = _rows(db, pe)[cl.id]
    row.leave_consumed = D("2")
    row.leave_balance = D("4")
    db.commit()
    pcl = _project_policy(db, proj, cl, "12")
    apply_project_leave_policies_to_team(db, proj)
    db.commit()
    row = _rows(db, pe)[cl.id]
    assert row.project_leave_policy_id == pcl.id
    assert row.leave_balance == D("4") and row.leave_consumed == D("2")


def test_removing_the_project_policy_hands_the_row_back_to_the_customer(db):  # noqa: F811
    cl, _el, proj, emp = _world(db)
    pcl = _project_policy(db, proj, cl, "12")
    pe = _map(db, emp, proj, onboarding=date(2026, 1, 1))
    seed_leave_details_from_customer_policy(db, pe, proj, seed_as_of=date(2026, 6, 1))
    db.commit()
    assert _rows(db, pe)[cl.id].project_leave_policy_id == pcl.id
    pcl.is_active = False
    db.commit()
    apply_project_leave_policies_to_team(db, proj)
    db.commit()
    row = _rows(db, pe)[cl.id]
    assert row.project_leave_policy_id is None and row.customer_leave_policy_id is not None
    assert row.leave_balance == D("6")


def test_timesheet_shows_project_leave_added_after_mapping(client):  # noqa: F811
    """The sheet's own route: a new sheet syncs the assignment, and an existing
    sheet's "Apply leave policies" button does the same."""
    from tests.test_timesheet_entry_grid import _seed
    sdb = client._session
    proj, emp, pe = _seed(sdb)
    lt = LeavePolicyType(name="Privilege Leave")
    sdb.add(lt)
    sdb.commit()
    res = client.post("/api/timesheets", json={
        "project_id": proj.id, "employee_id": emp.id, "project_employee_id": pe.id,
        "month": 6, "year": 2026, "generate_days": True})
    ts_id = res.json()["data"]["id"]
    assert res.json()["data"]["leave_balances_by_type"] == {}
    _project_policy(sdb, proj, lt, "10")                     # added on the project later
    out = client.post(f"/api/timesheets/{ts_id}/leave/sync")
    assert out.status_code == 200, out.text
    assert out.json()["data"]["leave_balances_by_type"] == {"Privilege Leave": 10.0}
    assert "1 type(s) added" in out.json()["message"]
