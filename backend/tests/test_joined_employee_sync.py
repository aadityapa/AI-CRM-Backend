"""HR's Workflow section -> the Employees record, at Joined (22 Sep 2026).

Reported: an internal employee (Emp ID 155, with Karnex since 2022) clears the
customer rounds; HR fills the Workflow section and flips the profile to Joined.
The existing employee IS updated today — but only for the fields that had a
column, so Offer Letter Reference, Customer Onboarding Date, Relocation and the
Resignation Certificate stayed stranded on the candidate profile. Migration 0105
gave them somewhere to live.

What is pinned here: the declarative field map, the "only what HR filled" rule
(a blank must never erase live employee data), that `False` survives it, the
UNIQUE clash guards on Emp ID and official email, and that a second employee
record is never created for someone who already exists.
"""
from __future__ import annotations

import importlib
from datetime import date

import pytest
from sqlalchemy import create_engine, select
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
    Candidate, CandidateProfile, Customer, Employee, OppType, Opportunity,
    PipelineStatus, Project, ProfileType,
)
from services.candidate_profiles import (  # noqa: E402
    _KEEP_FALSE, _PROFILE_TO_EMPLOYEE, ensure_employee_for_joined_profile,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    for uid in (1, 2):
        s.execute(users_table_stub.insert().values(id=uid))
    s.commit()
    try:
        yield s
    finally:
        s.close()


def _internal_employee(db, *, code="155", email="ganesh.old@karnex.in") -> Employee:
    """The internal trainee who has been with Karnex since 2022."""
    emp = Employee(first_name="Ganesh", last_name="Kumar.T", email=email,
                   employee_code=code, date_of_joining=date(2022, 12, 19),
                   profile_type=ProfileType.INTERNAL, is_active=False)
    db.add(emp); db.commit()
    return emp


def _joined_profile(db, **fields) -> CandidateProfile:
    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00087", title="Senior non-AUTOSAR engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1,
                      details={"tm_work_location": "Bangalore"})
    db.add(opp); db.flush()
    cand = Candidate(first_name="Ganesh", last_name="Kumar.T",
                     email="ganeshtiruveedhula2020@gmail.com", cv_url="/cv/ganesh.pdf")
    db.add(cand); db.flush()
    profile = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                               pipeline_status=PipelineStatus.JOINED, **fields)
    db.add(profile); db.commit()
    return profile


# ------------------------------------------------------------------- the map


def test_every_mapped_field_exists_on_both_sides():
    """A typo in the map would fail silently — `getattr` returns None and the
    field is quietly skipped, which is exactly the class of bug this replaced."""
    for src, dest in _PROFILE_TO_EMPLOYEE:
        assert hasattr(CandidateProfile, src), f"CandidateProfile has no {src}"
        assert hasattr(Employee, dest), f"Employee has no {dest}"


def test_the_fields_hr_fills_are_all_mapped():
    """The four that 0105 added — the whole reason for this change."""
    mapped = {dest for _src, dest in _PROFILE_TO_EMPLOYEE}
    for column in ("offer_letter_reference", "resignation_certificate_url",
                   "customer_onboarding_date", "relocation_applicable"):
        assert column in mapped


def test_karnex_joining_date_is_the_one_that_maps_to_date_of_joining():
    """⚠️ NOT the customer's onboarding date. `date_of_joining` drives payroll
    and leave accrual — taking the customer's value would rewrite years of
    service for an internal employee placed today."""
    pairs = dict(_PROFILE_TO_EMPLOYEE)
    assert pairs["karnex_onboarding_date"] == "date_of_joining"
    assert pairs["customer_onboarding_date"] == "customer_onboarding_date"


# --------------------------------------------------------------- the sync


def test_an_existing_employee_is_updated_never_duplicated(db):
    emp = _internal_employee(db)
    profile = _joined_profile(
        db,
        employee_ref="155",
        karnex_onboarding_date=date(2022, 12, 19),
        customer_onboarding_date=date(2026, 9, 21),
        offer_letter_reference="KRX/OL/2026/0142",
        relocation_applicable=False,
        total_experience_years=4.1,
        official_email="ganesh.tiruveedhula@karnex.in",
    )
    out = ensure_employee_for_joined_profile(db, profile)
    db.commit()

    assert out is not None and out.id == emp.id            # the SAME record
    assert db.execute(select(Employee)).scalars().all() == [emp]

    db.refresh(emp)
    assert emp.is_active is True                            # re-activated
    assert emp.candidate_profile_id == profile.id
    assert emp.customer_onboarding_date == date(2026, 9, 21)
    assert emp.offer_letter_reference == "KRX/OL/2026/0142"
    assert float(emp.experience_years) == 4.1
    # Their old address is preserved as the personal one, never overwritten.
    assert emp.email == "ganesh.tiruveedhula@karnex.in"
    assert emp.personal_email == "ganesh.old@karnex.in"
    # Work location comes from the OPPORTUNITY, not the profile.
    assert emp.work_location == "Bangalore"


def test_a_blank_never_erases_what_the_employee_already_has(db):
    """This runs against a LIVE employee who may hold better data than the
    profile. A field HR left empty means "not captured", not "delete it"."""
    emp = _internal_employee(db)
    emp.offer_letter_reference = "KRX/OL/2024/0001"
    emp.work_location = "Pune"
    db.commit()

    profile = _joined_profile(db, employee_ref="155")     # HR filled nothing else
    ensure_employee_for_joined_profile(db, profile)
    db.commit(); db.refresh(emp)

    assert emp.offer_letter_reference == "KRX/OL/2024/0001"
    assert emp.date_of_joining == date(2022, 12, 19)      # untouched
    # The opportunity HAS a location, so that one is allowed to win.
    assert emp.work_location == "Bangalore"


def test_an_explicit_no_to_relocation_is_not_dropped(db):
    """`False` is a real answer. A plain truthiness check would discard it and
    leave the field looking un-asked."""
    assert "relocation_applicable" in _KEEP_FALSE
    emp = _internal_employee(db)
    profile = _joined_profile(db, employee_ref="155", relocation_applicable=False)
    ensure_employee_for_joined_profile(db, profile)
    db.commit(); db.refresh(emp)
    assert emp.relocation_applicable is False


def test_a_clashing_emp_id_or_email_is_refused_not_crashed(db):
    """Both columns are UNIQUE — writing a duplicate would 500 the whole join,
    and a join must never fail because of bookkeeping."""
    _internal_employee(db)                                  # code 155
    other = Employee(first_name="Someone", last_name="Else", email="taken@karnex.in",
                     employee_code="999", profile_type=ProfileType.INTERNAL)
    db.add(other); db.commit()

    profile = _joined_profile(db, employee_ref="155",
                              official_email="taken@karnex.in")
    out = ensure_employee_for_joined_profile(db, profile)
    db.commit()
    assert out is not None
    db.refresh(out)
    assert out.email == "ganesh.old@karnex.in"      # refused, kept its own
    db.refresh(other)
    assert other.email == "taken@karnex.in"         # and the other is intact


def test_a_brand_new_candidate_still_gets_an_employee(db):
    """The original path must keep working — no Emp ID means a new record."""
    profile = _joined_profile(db, karnex_onboarding_date=date(2026, 9, 21))
    out = ensure_employee_for_joined_profile(db, profile)
    db.commit()
    assert out is not None
    # INTERNAL, not External (2 Sep 2026 decision): a joined candidate is on
    # Karnex payroll, deployed to the customer.
    assert out.profile_type == ProfileType.INTERNAL
    assert out.date_of_joining == date(2026, 9, 21)


def test_an_internal_candidate_found_by_personal_email_is_updated(db):
    """29 Sep 2026: an internal candidate applied with their own address — the
    Employees record keeps it as the personal email, and HR typed no Emp ID.
    The join updates THAT record; a namesake-less match is never merged."""
    emp = _internal_employee(db, code="201")
    emp.personal_email = "ganeshtiruveedhula2020@gmail.com"
    db.commit()
    out = ensure_employee_for_joined_profile(db, _joined_profile(db))
    db.commit()
    assert out is not None and out.id == emp.id
    assert db.execute(select(Employee)).scalars().all() == [emp]


# ------------------------------------------------------------- list ordering


def test_the_list_opens_latest_first():
    """DOJ cannot answer "who changed just now": a 2022 internal employee
    placed today still sorts by 2022. Since 29 Sep 2026 (user ask: "the joined
    employee, new or overridden, latest first") the default is recently
    updated; joining date stays one pick away."""
    from routers.crm.employees import DEFAULT_EMPLOYEE_SORT, _EMPLOYEE_SORTS
    assert DEFAULT_EMPLOYEE_SORT == "recently_updated"
    assert "date_of_joining" in _EMPLOYEE_SORTS
    assert "recently_updated" in _EMPLOYEE_SORTS
    for key in _EMPLOYEE_SORTS:
        assert _EMPLOYEE_SORTS[key]()        # every clause builds


def test_employees_now_carries_timestamps():
    """`employees` predated TimestampMixin; 0105 added both halves."""
    assert hasattr(Employee, "updated_at") and hasattr(Employee, "created_at")


# ----------------------------------------------------------- project history


def test_project_history_carries_the_deal_it_came_from(db):
    """A project name and two dates do not answer "what was this placement
    for?" — the customer, the opportunity and the headcount do."""
    from models import ProjectEmployee
    from models.hr import EmployeeProjectHistory
    from models import Requirement, RequirementStatus, BillingUnit
    from services.employees import project_history_context, serialize_project_history

    emp = _internal_employee(db)
    cust = Customer(name="VISTEON")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00091", title="Telematics Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    db.add(Requirement(req_number="REQ-91", opportunity_id=opp.id, customer_id=cust.id,
                       title="Telematics Engineer", no_of_positions=3,
                       status=RequirementStatus.IN_PROGRESS, created_by=1))
    proj = Project(customer_id=cust.id, opportunity_id=opp.id, name="Telematics")
    db.add(proj); db.flush()
    db.add(ProjectEmployee(project_id=proj.id, employee_id=emp.id,
                           billing_rate=120000, billing_unit=BillingUnit.MONTHLY,
                           onboarding_date=date(2026, 9, 21)))
    row = EmployeeProjectHistory(employee_id=emp.id, project_id=proj.id,
                                 start_date=date(2026, 9, 21), role="Senior Engineer")
    db.add(row); db.commit()

    ctx = project_history_context(db, [row], emp.id)
    out = serialize_project_history(row, project_name=proj.name, context=ctx.get(proj.id))

    assert out["project_name"] == "Telematics"
    assert out["customer_name"] == "VISTEON"
    assert out["opportunity_opp_id"] == "C-2026-00091"
    assert out["opportunity_title"] == "Telematics Engineer"
    assert out["positions_total"] == 3          # what the deal was sourcing for
    assert out["role"] == "Senior Engineer"     # the position HELD, not today's
    assert out["billing_rate"] == 120000
    assert out["is_current"] is True            # still open — no end_date


def test_project_history_context_is_batched_not_per_row(db):
    """An employee with a long history must not cost a query per placement."""
    import inspect
    from services import employees as mod
    src = inspect.getsource(mod.project_history_context)
    # Three batched reads: projects, opportunities, project-employees.
    assert src.count("db.execute(") == 3
    assert "in_(project_ids)" in src


def test_project_history_survives_a_project_with_no_opportunity(db):
    """`projects.opportunity_id` is NULLABLE (migration 0069) — a direct
    project placement must render, not crash."""
    from models.hr import EmployeeProjectHistory
    from services.employees import project_history_context

    emp = _internal_employee(db)
    cust = Customer(name="DIRECT CO")
    db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Internal tooling")
    db.add(proj); db.flush()
    row = EmployeeProjectHistory(employee_id=emp.id, project_id=proj.id,
                                 start_date=date(2026, 1, 1))
    db.add(row); db.commit()

    ctx = project_history_context(db, [row], emp.id)[proj.id]
    assert ctx["customer_name"] == "DIRECT CO"
    assert ctx["opportunity_opp_id"] is None
    assert ctx["positions_total"] is None
    assert ctx["billing_rate"] is None          # never assigned as a PE
