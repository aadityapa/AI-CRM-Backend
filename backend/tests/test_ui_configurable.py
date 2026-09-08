"""The "no more code deploys for this" batch (Aug 2026).

Covers: seller/TDS via org settings, per-event email templates, and the
configurable week-off pattern. Each was previously a code or env change.

Run:  cd backend && python -m pytest tests/test_ui_configurable.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

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
    "notify_routes", "email_outbox",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    try:
        yield s
    finally:
        s.close()


# ------------------------------------------------------------- week-off days

def test_parse_week_off_days():
    from services.timesheets import parse_week_off_days

    assert parse_week_off_days("5,6") == (5, 6)
    assert parse_week_off_days("4, 5") == (4, 5)
    assert parse_week_off_days("6,5,5") == (5, 6)      # dedupe + sort
    assert parse_week_off_days(None) is None
    assert parse_week_off_days("") is None
    # One bad token invalidates the WHOLE value — a typo must fall back to
    # the next policy level, not quietly produce a 7-day work week.
    assert parse_week_off_days("5,banana") is None
    assert parse_week_off_days("7") is None


def test_classify_calendar_day_honours_pattern():
    from services.timesheets import classify_calendar_day
    from models import AttendanceStatus, DayType

    friday = date(2026, 8, 14)     # weekday 4
    saturday = date(2026, 8, 15)   # weekday 5
    sunday = date(2026, 8, 16)     # weekday 6

    # Default Sat+Sun.
    assert classify_calendar_day(friday, set())[0] == DayType.WORKING
    assert classify_calendar_day(saturday, set())[0] == DayType.WEEK_OFF

    # Gulf week: Fri+Sat off, Sunday working.
    gulf = (4, 5)
    assert classify_calendar_day(friday, set(), week_off_days=gulf)[0] == DayType.WEEK_OFF
    assert classify_calendar_day(saturday, set(), week_off_days=gulf)[0] == DayType.WEEK_OFF
    day_type, is_working, att, _ = classify_calendar_day(sunday, set(), week_off_days=gulf)
    assert day_type == DayType.WORKING and is_working and att == AttendanceStatus.PRESENT


def test_effective_policy_resolves_week_off_days(db):
    from models import Customer, CustomerBillingPolicy, Opportunity, OppType, Project
    from services.timesheets import effective_billing_policy

    cust = Customer(name="Gulf Co")
    db.add(cust)
    db.flush()
    db.add(CustomerBillingPolicy(customer_id=cust.id, week_off_days="4,5",
                                 min_hours_full_day=8, min_hours_half_day=4))
    opp = Opportunity(opp_id="OPP-G-1", title="G", customer_id=cust.id,
                      opp_type=OppType.T_AND_M, created_by=1)
    db.add(opp)
    db.flush()
    project = Project(name="Gulf Delivery", customer_id=cust.id, opportunity_id=opp.id)
    db.add(project)
    db.commit()

    pol = effective_billing_policy(db, project)
    assert pol.week_off_days == (4, 5)

    # Project override wins over the customer default.
    project.week_off_days = "6"
    db.commit()
    assert effective_billing_policy(db, project).week_off_days == (6,)

    # An invalid stored value falls back to the customer level, not to chaos.
    project.week_off_days = "nonsense"
    db.commit()
    assert effective_billing_policy(db, project).week_off_days == (4, 5)


# --------------------------------------------------------- email templates

def test_event_template_rewrites_subject_and_body(db):
    from models import NotificationRoute
    from services.email_outbox import _apply_event_template

    db.add(NotificationRoute(
        event="timesheet.submitted", roles=["HR"],
        subject_template="[Karnex] {subject}",
        body_template="Dear {recipient},\n\n{body}\n\nRegards, {company}",
    ))
    db.commit()

    subject, body = _apply_event_template(
        db, "timesheet.submitted",
        subject="Timesheet submitted", body_text="July sheet awaits approval.",
        to_name="Balasaheb",
    )
    assert subject == "[Karnex] Timesheet submitted"
    assert body.startswith("Dear Balasaheb,")
    assert "July sheet awaits approval." in body
    assert "Regards," in body


def test_no_template_means_untouched_text(db):
    from services.email_outbox import _apply_event_template

    subject, body = _apply_event_template(
        db, "leave.submitted", subject="S", body_text="B", to_name="X")
    assert (subject, body) == ("S", "B")


def test_unknown_placeholder_passes_through_visibly(db):
    """An admin typo must never swallow the mail — it renders literally."""
    from models import NotificationRoute
    from services.email_outbox import _apply_event_template

    db.add(NotificationRoute(event="po.expiry_warning", roles=["Finance"],
                             subject_template="{oops} {subject}"))
    db.commit()
    subject, _ = _apply_event_template(
        db, "po.expiry_warning", subject="PO expiring", body_text="", to_name="")
    assert subject == "{oops} PO expiring"


# ------------------------------------------------------- seller / TDS config

def test_seller_details_resolve_through_settings():
    """DB row → env → default chain; here (no DB) the defaults hold."""
    from services.company_invoice_config import get_bank_details, get_seller_details

    seller = get_seller_details()
    assert seller["gstin"], "GSTIN must never be empty on a tax invoice"
    assert seller["state_code"] == "27"
    bank = get_bank_details()
    assert bank["ifsc"] and bank["account_number"]


def test_tds_rate_falls_back_hard_to_ten():
    from services.tax import default_tds_rate, tds_amount

    assert default_tds_rate() == Decimal("10")
    assert tds_amount(100000) == Decimal("10000.00")
