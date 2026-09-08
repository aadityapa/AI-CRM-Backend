"""DELETE endpoints for list row-actions — success + dependency blocks.

Covers: projects, project-employees, timesheets, leave-applications,
template-requests, tds.

Run:  python -m pytest tests/test_crm_list_deletes.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, ARRAY, UUID, INET


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


import importlib
for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
           "timesheets", "finance", "hr", "candidates", "masters", "requirements",
           "profiles", "resumes", "ai_links", "scheduling",
           "user_profiles", "template_requests"]:
    importlib.import_module(f"models.{_m}")

from fastapi import FastAPI                                             # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

from models.base import Base                                           # noqa: E402
from models.customers import Customer, CustomerBranch                  # noqa: E402
from models.opportunities import Opportunity, OppType                  # noqa: E402
from models.projects import Project, ProjectEmployee, BillingUnit      # noqa: E402
from models.hr import Employee                                         # noqa: E402
from models.timesheets import Timesheet, TimesheetStatus               # noqa: E402
from models.leave import LeaveApplication                              # noqa: E402
from models.masters import LeavePolicyType                             # noqa: E402
from models.finance import Invoice, PaymentStatus, TdsRecord, TdsStatus  # noqa: E402
from models.template_requests import TemplateRequest, TemplateRequestStatus  # noqa: E402
from models.requirements import Requirement, RequirementStatus         # noqa: E402
import crm_deps                                                        # noqa: E402
import routers.crm.projects as projects_router                         # noqa: E402
import routers.crm.timesheets as timesheets_router                     # noqa: E402
import routers.crm.leave_applications as leave_apps_router             # noqa: E402
import routers.crm.template_requests as tr_router                      # noqa: E402
import routers.crm.finance as finance_router                           # noqa: E402
import routers.crm.opportunities as opportunities_router               # noqa: E402
import routers.crm.candidates as candidates_router                     # noqa: E402
import routers.crm.customers as customers_router                       # noqa: E402

D = Decimal


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine, future=True)
    from models.base import users_table_stub
    session.execute(users_table_stub.insert().values(id=1))
    session.commit()

    app = FastAPI()
    app.include_router(projects_router.router)
    app.include_router(timesheets_router.router)
    app.include_router(leave_apps_router.router)
    app.include_router(tr_router.router)
    app.include_router(finance_router.router)
    app.include_router(opportunities_router.router)
    app.include_router(candidates_router.router)
    app.include_router(customers_router.router)

    def _override_db():
        yield session

    def _override_user():
        return crm_deps.CurrentUser(id=1, username="qa", roles={"Admin"})

    app.dependency_overrides[crm_deps.get_crm_db] = _override_db
    app.dependency_overrides[crm_deps.get_current_user] = _override_user

    c = TestClient(app)
    c._session = session
    try:
        yield c
    finally:
        session.close()


def _seed_project(session: Session, *, with_pe=False, with_ts=False,
                  ts_status=TimesheetStatus.DRAFT):
    cust = Customer(name="Acme")
    session.add(cust)
    session.flush()
    branch = CustomerBranch(customer_id=cust.id, branch_name="Pune")
    session.add(branch)
    session.flush()
    opp = Opportunity(
        opp_id="OPP-T-1", title="Opp", customer_id=cust.id, branch_id=branch.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    session.add(opp)
    session.flush()
    proj = Project(
        opportunity_id=opp.id, customer_id=cust.id, branch_id=branch.id,
        name="Proj A",
    )
    session.add(proj)
    session.flush()
    pe = None
    ts = None
    if with_pe or with_ts:
        emp = Employee(first_name="Pat", last_name="Lee", email="pat@ex.com")
        session.add(emp)
        session.flush()
        pe = ProjectEmployee(
            project_id=proj.id, employee_id=emp.id,
            billing_rate=D("100"), billing_unit=BillingUnit.MONTHLY, is_active=True,
        )
        session.add(pe)
        session.flush()
    if with_ts:
        ts = Timesheet(
            project_id=proj.id, employee_id=pe.employee_id, project_employee_id=pe.id,
            month=1, year=2026, status=ts_status,
        )
        session.add(ts)
        session.flush()
    session.commit()
    return cust, branch, opp, proj, pe, ts


def test_delete_project_success(client):
    s = client._session
    _, _, _, proj, _, _ = _seed_project(s)
    r = client.delete(f"/api/projects/{proj.id}")
    assert r.status_code == 200, r.text
    assert s.get(Project, proj.id) is None


def test_delete_project_cascades_active_pe_and_draft_timesheet(client):
    """Active PE + draft timesheet no longer block — they cascade."""
    s = client._session
    _, _, _, proj, pe, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.DRAFT)
    pe_id, ts_id = pe.id, ts.id
    r = client.delete(f"/api/projects/{proj.id}")
    assert r.status_code == 200, r.text
    assert s.get(Project, proj.id) is None
    assert s.get(ProjectEmployee, pe_id) is None
    assert s.get(Timesheet, ts_id) is None


def test_delete_project_ok_when_pe_inactive(client):
    """Inactive / exited team members must not block project delete."""
    s = client._session
    _, _, _, proj, pe, _ = _seed_project(s, with_pe=True)
    pe.is_active = False
    pe.is_exit = True
    s.commit()
    pe_id = pe.id
    r = client.delete(f"/api/projects/{proj.id}")
    assert r.status_code == 200, r.text
    assert s.get(Project, proj.id) is None
    assert s.get(ProjectEmployee, pe_id) is None


def test_delete_project_ok_with_history(client):
    """employee_project_history must be purged so delete does not 500."""
    from models.hr import EmployeeProjectHistory

    s = client._session
    _, _, _, proj, pe, _ = _seed_project(s, with_pe=True)
    pe.is_active = False
    pe.is_exit = True
    s.add(EmployeeProjectHistory(
        employee_id=pe.employee_id, project_id=proj.id,
        start_date=date(2026, 1, 1), end_date=date(2026, 2, 1), role="Dev",
    ))
    s.commit()
    r = client.delete(f"/api/projects/{proj.id}")
    assert r.status_code == 200, r.text
    assert s.get(Project, proj.id) is None


def test_delete_project_cascades_unpaid_invoice_and_tds(client):
    s = client._session
    _, _, _, proj, _, _ = _seed_project(s)
    inv = Invoice(
        invoice_number="INV-CASCADE-1", project_id=proj.id,
        invoice_date=date(2026, 1, 1),
        sub_total=D("1000"), tax_amount=D("180"), grand_total=D("1180"),
        paid_amount=D("0"), balance_amount=D("1180"),
        payment_status=PaymentStatus.UNPAID,
    )
    s.add(inv)
    s.flush()
    tds = TdsRecord(
        invoice_id=inv.id, tds_amount=D("100"), tds_paid=D("0"),
        tds_balance=D("100"), tds_status=TdsStatus.PENDING,
    )
    s.add(tds)
    s.commit()
    inv_id, tds_id = inv.id, tds.id
    r = client.delete(f"/api/projects/{proj.id}")
    assert r.status_code == 200, r.text
    assert s.get(Project, proj.id) is None
    assert s.get(Invoice, inv_id) is None
    assert s.get(TdsRecord, tds_id) is None


def test_delete_pe_success(client):
    s = client._session
    _, _, _, _, pe, _ = _seed_project(s, with_pe=True)
    r = client.delete(f"/api/projects/employees/{pe.id}")
    assert r.status_code == 200, r.text
    assert s.get(ProjectEmployee, pe.id) is None


def test_delete_pe_cascades_approved_timesheet_without_invoice(client):
    """Approved timesheet with no invoice is purged so PE cleanup can proceed."""
    s = client._session
    _, _, _, _, pe, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.APPROVED)
    ts_id = ts.id
    r = client.delete(f"/api/projects/employees/{pe.id}")
    assert r.status_code == 200, r.text
    assert s.get(ProjectEmployee, pe.id) is None
    assert s.get(Timesheet, ts_id) is None


def test_delete_pe_cascades_invoice_then_approved_timesheet(client):
    s = client._session
    _, _, _, proj, pe, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.APPROVED)
    inv = Invoice(
        invoice_number="INV-PE-1", project_id=proj.id, timesheet_id=ts.id,
        invoice_date=date(2026, 1, 1),
        sub_total=D("500"), tax_amount=D("90"), grand_total=D("590"),
        paid_amount=D("0"), balance_amount=D("590"),
        payment_status=PaymentStatus.UNPAID,
    )
    s.add(inv)
    s.commit()
    pe_id, ts_id, inv_id = pe.id, ts.id, inv.id
    r = client.delete(f"/api/projects/employees/{pe_id}")
    assert r.status_code == 200, r.text
    assert s.get(ProjectEmployee, pe_id) is None
    assert s.get(Timesheet, ts_id) is None
    assert s.get(Invoice, inv_id) is None


def test_delete_timesheet_draft_ok(client):
    s = client._session
    _, _, _, _, _, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.DRAFT)
    r = client.delete(f"/api/timesheets/{ts.id}")
    assert r.status_code == 200, r.text
    assert s.get(Timesheet, ts.id) is None


def test_delete_timesheet_approved_ok(client):
    """Approved (no invoice) may be deleted; ledger reverse runs first."""
    s = client._session
    _, _, _, _, _, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.APPROVED)
    ts_id = ts.id
    r = client.delete(f"/api/timesheets/{ts_id}")
    assert r.status_code == 200, r.text
    assert s.get(Timesheet, ts_id) is None


def test_delete_timesheet_submitted_ok(client):
    s = client._session
    _, _, _, _, _, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.SUBMITTED)
    ts_id = ts.id
    r = client.delete(f"/api/timesheets/{ts_id}")
    assert r.status_code == 200, r.text
    assert s.get(Timesheet, ts_id) is None


def test_delete_timesheet_invoice_linked_blocked(client):
    s = client._session
    _, _, _, proj, _, ts = _seed_project(s, with_pe=True, with_ts=True, ts_status=TimesheetStatus.APPROVED)
    inv = Invoice(
        invoice_number="INV-TS-DEL-1", project_id=proj.id, timesheet_id=ts.id,
        invoice_date=date(2026, 1, 1),
        sub_total=D("500"), tax_amount=D("90"), grand_total=D("590"),
        paid_amount=D("0"), balance_amount=D("590"),
        payment_status=PaymentStatus.UNPAID,
    )
    s.add(inv)
    s.commit()
    r = client.delete(f"/api/timesheets/{ts.id}")
    assert r.status_code == 409
    assert "invoice" in r.json()["detail"].lower()
    assert s.get(Timesheet, ts.id) is not None


def test_delete_leave_application_pending_ok(client):
    s = client._session
    _, _, _, proj, pe, _ = _seed_project(s, with_pe=True)
    lt = LeavePolicyType(name="Casual")
    s.add(lt)
    s.flush()
    app = LeaveApplication(
        employee_id=pe.employee_id, project_id=proj.id, project_employee_id=pe.id,
        leave_type_id=lt.id, leave_period_type="Full_Day",
        from_date=date(2026, 1, 5), to_date=date(2026, 1, 5),
        days=D("1"), status="Pending",
    )
    s.add(app)
    s.commit()
    r = client.delete(f"/api/leave-applications/{app.id}")
    assert r.status_code == 200, r.text
    assert s.get(LeaveApplication, app.id) is None


def test_delete_leave_application_approved_blocked(client):
    s = client._session
    _, _, _, proj, pe, _ = _seed_project(s, with_pe=True)
    lt = LeavePolicyType(name="Casual")
    s.add(lt)
    s.flush()
    app = LeaveApplication(
        employee_id=pe.employee_id, project_id=proj.id, project_employee_id=pe.id,
        leave_type_id=lt.id, leave_period_type="Full_Day",
        from_date=date(2026, 1, 5), to_date=date(2026, 1, 5),
        days=D("1"), status="Approved",
    )
    s.add(app)
    s.commit()
    r = client.delete(f"/api/leave-applications/{app.id}")
    assert r.status_code == 409


def test_delete_template_request_ok(client):
    s = client._session
    cust = Customer(name="C")
    s.add(cust)
    s.flush()
    opp = Opportunity(
        opp_id="OPP-TR", title="T", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    s.add(opp)
    s.flush()
    req = Requirement(
        opportunity_id=opp.id, customer_id=cust.id, req_number="REQ-1", title="Role",
        status=RequirementStatus.OPEN_FOR_SOURCING, created_by=1,
    )
    s.add(req)
    s.flush()
    tr = TemplateRequest(
        tr_number="TR-1", requirement_id=req.id, opportunity_id=opp.id,
        role_title="Dev", status=TemplateRequestStatus.PENDING_RMG, requested_by=1,
    )
    s.add(tr)
    s.commit()
    r = client.delete(f"/api/template-requests/{tr.id}")
    assert r.status_code == 200, r.text
    assert s.get(TemplateRequest, tr.id) is None


def test_delete_template_request_prepared_blocked(client):
    s = client._session
    cust = Customer(name="C2")
    s.add(cust)
    s.flush()
    opp = Opportunity(
        opp_id="OPP-TR2", title="T", customer_id=cust.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    s.add(opp)
    s.flush()
    req = Requirement(
        opportunity_id=opp.id, customer_id=cust.id, req_number="REQ-2", title="Role",
        status=RequirementStatus.OPEN_FOR_SOURCING, created_by=1,
    )
    s.add(req)
    s.flush()
    tr = TemplateRequest(
        tr_number="TR-2", requirement_id=req.id, opportunity_id=opp.id,
        role_title="Dev", status=TemplateRequestStatus.PREPARED, requested_by=1,
        candidate_email="a@b.com",
    )
    s.add(tr)
    s.commit()
    r = client.delete(f"/api/template-requests/{tr.id}")
    assert r.status_code == 409


def test_delete_tds_ok(client):
    s = client._session
    _, _, _, proj, _, _ = _seed_project(s)
    inv = Invoice(
        invoice_number="INV-1", project_id=proj.id,
        invoice_date=date(2026, 1, 1),
        sub_total=D("1000"), tax_amount=D("180"), grand_total=D("1180"),
        paid_amount=D("0"), balance_amount=D("1180"),
        payment_status=PaymentStatus.UNPAID,
    )
    s.add(inv)
    s.flush()
    tds = TdsRecord(
        invoice_id=inv.id, tds_amount=D("100"), tds_paid=D("0"),
        tds_balance=D("100"), tds_status=TdsStatus.PENDING,
    )
    s.add(tds)
    s.commit()
    r = client.delete(f"/api/tds/{tds.id}")
    assert r.status_code == 200, r.text
    assert s.get(TdsRecord, tds.id) is None


def test_delete_invoice_cascades_unpaid_tds(client):
    s = client._session
    _, _, _, proj, _, _ = _seed_project(s)
    inv = Invoice(
        invoice_number="INV-TDS-CASCADE", project_id=proj.id,
        invoice_date=date(2026, 1, 1),
        sub_total=D("1000"), tax_amount=D("180"), grand_total=D("1180"),
        paid_amount=D("0"), balance_amount=D("1180"),
        payment_status=PaymentStatus.UNPAID,
    )
    s.add(inv)
    s.flush()
    tds = TdsRecord(
        invoice_id=inv.id, tds_amount=D("50"), tds_paid=D("0"),
        tds_balance=D("50"), tds_status=TdsStatus.PENDING,
    )
    s.add(tds)
    s.commit()
    inv_id, tds_id = inv.id, tds.id
    r = client.delete(f"/api/invoices/{inv_id}")
    assert r.status_code == 200, r.text
    assert s.get(Invoice, inv_id) is None
    assert s.get(TdsRecord, tds_id) is None


def test_delete_opportunity_cascades_requirement_and_profile(client):
    from models.candidates import Candidate
    from models.profiles import CandidateProfile
    from models.ai_links import AiInterviewLink

    s = client._session
    cust, branch, opp, _, _, _ = _seed_project(s)
    # Fresh opp without a project so delete is allowed.
    opp2 = Opportunity(
        opp_id="OPP-CASCADE", title="Cascade Opp", customer_id=cust.id, branch_id=branch.id,
        opp_type=OppType.T_AND_M, created_by=1,
    )
    s.add(opp2)
    s.flush()
    req = Requirement(
        opportunity_id=opp2.id, customer_id=cust.id, req_number="REQ-C1", title="Role",
        status=RequirementStatus.OPEN_FOR_SOURCING, created_by=1,
    )
    s.add(req)
    s.flush()
    cand = Candidate(first_name="Ada", last_name="Lovelace", email="ada@ex.com")
    s.add(cand)
    s.flush()
    profile = CandidateProfile(
        candidate_id=cand.id, opportunity_id=opp2.id,
    )
    s.add(profile)
    s.flush()
    link = AiInterviewLink(
        invite_token="tok-cascade-1",
        candidate_id=cand.id, opportunity_id=opp2.id, profile_id=profile.id,
    )
    s.add(link)
    s.commit()
    opp_id, req_id, profile_id, link_id = opp2.id, req.id, profile.id, link.id
    r = client.delete(f"/api/opportunities/{opp_id}")
    assert r.status_code == 200, r.text
    assert s.get(Opportunity, opp_id) is None
    assert s.get(Requirement, req_id) is None
    assert s.get(CandidateProfile, profile_id) is None
    assert s.get(AiInterviewLink, link_id) is None
    # Project-backed opp still blocks.
    r2 = client.delete(f"/api/opportunities/{opp.id}")
    assert r2.status_code == 409
    assert "project" in r2.json()["detail"].lower()


def test_delete_candidate_cascades_profiles(client):
    from models.candidates import Candidate
    from models.profiles import CandidateProfile

    s = client._session
    cust, branch, opp, _, _, _ = _seed_project(s)
    cand = Candidate(first_name="Grace", last_name="Hopper", email="grace@ex.com")
    s.add(cand)
    s.flush()
    profile = CandidateProfile(
        candidate_id=cand.id, opportunity_id=opp.id,
    )
    s.add(profile)
    s.commit()
    cand_id, profile_id = cand.id, profile.id
    r = client.delete(f"/api/candidates/{cand_id}")
    assert r.status_code == 200, r.text
    assert s.get(Candidate, cand_id) is None
    assert s.get(CandidateProfile, profile_id) is None


def test_delete_customer_cascades_leave_policy(client):
    from models.leave import CustomerLeavePolicy
    from models.masters import LeavePolicyType

    s = client._session
    cust = Customer(name="SoloCo")
    s.add(cust)
    s.flush()
    lt = LeavePolicyType(name="Casual-Solo")
    s.add(lt)
    s.flush()
    pol = CustomerLeavePolicy(
        customer_id=cust.id, leave_type_id=lt.id,
        leave_credit_type="Monthly", leave_credit_balance=D("1"),
        initial_credit_balance=D("1"),
    )
    s.add(pol)
    s.commit()
    cust_id, pol_id = cust.id, pol.id
    r = client.delete(f"/api/customers/{cust_id}")
    assert r.status_code == 200, r.text
    assert s.get(Customer, cust_id) is None
    assert s.get(CustomerLeavePolicy, pol_id) is None
