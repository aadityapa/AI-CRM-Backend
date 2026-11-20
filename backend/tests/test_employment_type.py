"""Employment type: Full-Time · Part-Time · Contract · Intern (20 Nov 2026).
Run: cd backend && python -m pytest tests/test_employment_type.py -q"""
from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from schemas.employees import EmployeeCreate, EmployeeUpdate
from services.employees import EMPLOYMENT_TYPES, normalize_employment_type


def test_the_four_types_and_what_people_type():
    assert EMPLOYMENT_TYPES == ("Full_Time", "Part_Time", "Contract", "Intern")
    assert normalize_employment_type("Full-Time") == "Full_Time"
    assert normalize_employment_type("part time") == "Part_Time"
    assert normalize_employment_type("CONTRACT") == "Contract"
    assert normalize_employment_type("Internship") == "Intern"
    assert normalize_employment_type("Contract / Consultant") == "Contract"
    assert normalize_employment_type("Consultant") == "Contract"
    assert normalize_employment_type("Intern") == "Intern"
    assert normalize_employment_type("  ") is None
    with pytest.raises(ValueError):
        normalize_employment_type("Freelance")


def test_the_forms_accept_intern_and_refuse_anything_else():
    assert EmployeeCreate(first_name="A", email="a@karnex.in", employment_type="Intern").employment_type == "Intern"
    assert EmployeeUpdate(employment_type="Full-Time").employment_type == "Full_Time"
    assert EmployeeUpdate(employment_type="").employment_type is None
    with pytest.raises(ValidationError):
        EmployeeUpdate(employment_type="Freelance")


def test_the_list_filters_and_carries_it():
    import routers.crm.employees as r
    from services import employees as svc
    assert "employment_type" in inspect.signature(r.list_employees).parameters
    assert '"employment_type": emp.employment_type' in inspect.getsource(svc.serialize_employee)
