"""A JOINED candidate must exist in the Employees tab (31 Aug 2026 bug report).

Without this, the whole post-hire chain is blocked: no employee → no project
mapping → no timesheet → no PO consumption → no invoice.

Run:  cd backend && python -m pytest tests/test_joined_creates_employee.py -q
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
from models import (  # noqa: E402
    Candidate, CandidateProfile, Customer, Employee, OfferHistory, Opportunity,
    OppType, PipelineStatus, ProfileType,
)
from services.candidate_profiles import ensure_employee_for_joined_profile  # noqa: E402


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


def _profile(db, *, email="omkar@karnex.in", with_offer=True):
    cust = Customer(name="Visteon")
    db.add(cust); db.flush()
    opp = Opportunity(opp_id="C-2026-00067", title="Manual Integration Test Engineer",
                      customer_id=cust.id, opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp); db.flush()
    cand = Candidate(first_name="Omkar", last_name="Kamathe", email=email, phone="9145265887")
    db.add(cand); db.flush()
    prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id,
                            pipeline_status=PipelineStatus.JOINED)
    db.add(prof); db.flush()
    if with_offer:
        db.add(OfferHistory(profile_id=prof.id, offer_date=date(2026, 8, 1),
                            ctc=1200000, joining_date=date(2026, 9, 1)))
        db.flush()
    return prof


def test_joined_profile_creates_an_internal_employee(db):
    prof = _profile(db)
    emp = ensure_employee_for_joined_profile(db, prof)
    assert emp is not None
    assert emp.candidate_profile_id == prof.id
    assert emp.first_name == "Omkar" and emp.email == "omkar@karnex.in"
    # On Karnex payroll, deployed to the customer — Internal like every other
    # employee (2 Sep 2026, user decision). External = not on our payroll.
    assert emp.profile_type == ProfileType.INTERNAL
    # Offer terms carry over so payroll/invoicing start from real numbers.
    assert emp.date_of_joining == date(2026, 9, 1)
    assert float(emp.current_ctc) == 1200000


def test_is_idempotent_no_duplicate_employee(db):
    prof = _profile(db)
    first = ensure_employee_for_joined_profile(db, prof)
    again = ensure_employee_for_joined_profile(db, prof)
    assert again.id == first.id
    assert db.execute(select(Employee)).scalars().all().__len__() == 1


def test_existing_employee_with_same_email_is_linked_not_duplicated(db):
    prof = _profile(db)
    db.add(Employee(first_name="Omkar", email="omkar@karnex.in",
                    profile_type=ProfileType.EXTERNAL))
    db.flush()
    emp = ensure_employee_for_joined_profile(db, prof)
    assert emp.candidate_profile_id == prof.id
    assert len(db.execute(select(Employee)).scalars().all()) == 1


def test_placeholder_email_gets_a_local_address(db):
    """employees.email is UNIQUE NOT NULL — an import placeholder must not be
    copied in, or the second such hire collides."""
    prof = _profile(db, email="omkar.abc123@import.karnex.in")
    emp = ensure_employee_for_joined_profile(db, prof)
    assert emp is not None
    assert "@import.karnex.in" not in emp.email
    assert emp.email.endswith("@pending.karnex.local")
