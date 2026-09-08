"""Seed Karnex CRM master data with sample values. Idempotent — safe to re-run.

Usage (from backend/):  python seed_crm.py
Requires CRM_DATABASE_URL or AUTH_DB_URL pointing at Postgres (run
`alembic upgrade head` first).
"""
from __future__ import annotations

import os
from datetime import date

from sqlalchemy import select

from crm_db import get_session_factory
from models import (
    AppSetting, CalendarYear, ContactRole, Currency, Department, Designation, DocumentType,
    FinancialYear, LeavePolicyType, Location, Skill, TaxRate,
)

DEPARTMENTS = ["Engineering", "Sales", "Talent Acquisition", "RMG", "HR", "Finance", "Delivery"]

DESIGNATIONS = {
    "Engineering": ["Embedded Software Engineer", "Senior Embedded Engineer", "QA Engineer",
                    "Automation Test Engineer", "DevOps Engineer", "Technical Architect"],
    "Sales": ["Sales Executive", "Account Manager", "Sales Head"],
    "Talent Acquisition": ["Recruiter", "Senior Recruiter", "TA Lead"],
    "RMG": ["Resource Manager", "RMG Head"],
    "HR": ["HR Executive", "HR Manager"],
    "Finance": ["Accounts Executive", "Finance Manager"],
    "Delivery": ["Project Manager", "Delivery Head"],
}

SKILLS = {
    "Embedded": ["Embedded C", "C++", "RTOS", "AUTOSAR", "CAN", "LIN", "UDS", "MISRA",
                 "Microcontrollers", "Device Drivers", "Bootloader"],
    "Automotive": ["Model Based Development", "MATLAB/Simulink", "ISO 26262", "ASPICE",
                   "Vector CANoe", "CAPL"],
    "QA": ["Manual Testing", "Selenium", "Pytest", "API Testing", "JIRA", "Test Automation"],
    "DevOps": ["Docker", "Kubernetes", "Jenkins", "AWS", "Azure", "CI/CD", "Terraform", "Linux"],
    "Software": ["Python", "Java", "JavaScript", "React", "FastAPI", "PostgreSQL", "Git"],
}

LOCATIONS = [
    ("Pune", "Maharashtra"), ("Bengaluru", "Karnataka"), ("Hyderabad", "Telangana"),
    ("Chennai", "Tamil Nadu"), ("Mumbai", "Maharashtra"), ("Delhi NCR", "Delhi"),
    ("Coimbatore", "Tamil Nadu"), ("Ahmedabad", "Gujarat"),
]

CURRENCIES = [("INR", "Indian Rupee", "₹"), ("USD", "US Dollar", "$"), ("EUR", "Euro", "€")]

DOCUMENT_TYPES = ["MSA", "NDA", "SOW", "Rate Card", "GST Certificate", "PAN Card", "Agreement"]

CONTACT_ROLES = ["Finance", "Operational", "Procurement", "HR"]

# Canonical names (must match migration 0038 / 0041). Short aliases like
# "Casual" / "Sick" were retired — they duplicated "Casual Leave" / "Sick Leave".
LEAVE_TYPES = [
    ("Casual Leave", "1 per month", "No carry forward"),
    ("Sick Leave", "0.5 per month", "No carry forward"),
    ("Earned Leave", "1.25 per month", "Carry forward up to 30 days"),
    ("Comp-Off", "Earned on approved weekend/holiday work", "Expires in 90 days"),
    ("Maternity Leave", "26 weeks as per Maternity Benefit Act", "Not applicable"),
    ("Paternity Leave", "5 days per child", "Not applicable"),
    ("Loss of Pay", "Unpaid — deducted from salary", "Not applicable"),
    ("Paid Leave", "As per client / company policy", "As per policy"),
]

FINANCIAL_YEARS = [
    ("FY 2025-26", date(2025, 4, 1), date(2026, 3, 31)),
    ("FY 2026-27", date(2026, 4, 1), date(2027, 3, 31)),
]

CALENDAR_YEARS = [2025, 2026]

# Reference masters only — services/tax.py computation stays env-driven.
GST_SLABS = ["5", "12", "18", "28"]
TDS_DEFAULT_RATE = os.getenv("TDS_RATE_PERCENT", "10")

SETTINGS = [
    ("ai_interview_pass_threshold", "60", "AI L1 interview pass threshold (%) — Admin editable"),
    ("ats_auto_threshold", "50", "Auto-shortlist + slot-invite when ATS score ≥ this % (Admin editable)"),
    ("ats_auto_invite", "true", "Enable automatic slot-booking invite at the ATS threshold"),
]


def _get_or_create(session, model, defaults=None, **filters):
    obj = session.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if obj:
        return obj, False
    obj = model(**filters, **(defaults or {}))
    session.add(obj)
    session.flush()
    return obj, True


def seed() -> None:
    session = get_session_factory()()
    created = 0
    try:
        for name in DEPARTMENTS:
            dept, new = _get_or_create(session, Department, name=name)
            created += new
            for desig in DESIGNATIONS.get(name, []):
                _, new = _get_or_create(session, Designation, name=desig, department_id=dept.id)
                created += new

        for category, names in SKILLS.items():
            for name in names:
                _, new = _get_or_create(session, Skill, name=name, defaults={"category": category})
                created += new

        for city, state in LOCATIONS:
            _, new = _get_or_create(session, Location, city=city, state=state, country="India")
            created += new

        for code, name, symbol in CURRENCIES:
            _, new = _get_or_create(session, Currency, code=code,
                                    defaults={"name": name, "symbol": symbol})
            created += new

        for name in DOCUMENT_TYPES:
            _, new = _get_or_create(session, DocumentType, name=name)
            created += new

        for name in CONTACT_ROLES:
            _, new = _get_or_create(session, ContactRole, name=name)
            created += new

        for name, accrual, carry in LEAVE_TYPES:
            _, new = _get_or_create(session, LeavePolicyType, name=name,
                                    defaults={"accrual_rule": accrual, "carry_forward_rule": carry})
            created += new

        for name, start, end in FINANCIAL_YEARS:
            _, new = _get_or_create(session, FinancialYear, name=name,
                                    defaults={"start_date": start, "end_date": end,
                                              "is_active": True})
            created += new

        for year in CALENDAR_YEARS:
            _, new = _get_or_create(session, CalendarYear, year=year,
                                    defaults={"is_active": True})
            created += new

        for slab in GST_SLABS:
            _, new = _get_or_create(session, TaxRate, name=f"GST {slab}%",
                                    defaults={"tax_type": "GST", "rate": slab,
                                              "is_active": True})
            created += new
        _, new = _get_or_create(session, TaxRate, name="TDS 194J",
                                defaults={"tax_type": "TDS", "rate": TDS_DEFAULT_RATE,
                                          "is_active": True})
        created += new

        for key, value, desc in SETTINGS:
            _, new = _get_or_create(session, AppSetting, key=key,
                                    defaults={"value": value, "description": desc})
            created += new

        session.commit()
        print(f"Seed complete. {created} new rows inserted (existing rows left untouched).")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    seed()
