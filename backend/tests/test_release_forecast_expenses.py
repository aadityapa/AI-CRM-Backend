"""Planned release / extension, predicted revenue, expenses (4 Nov 2026). PURE."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import importlib  # noqa: E402



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
from services.expenses import by_category, by_month, clean, net_figures
from services.pe_release import planned_end, release_problem, reminder_due
from services.revenue_forecast import month_prediction, predicted_trend
from services.revenue_report import Month

TODAY = date(2026, 11, 4)


def test_planned_end_is_the_earlier_of_exit_and_release():
    assert planned_end(None, None) is None
    assert planned_end(date(2026, 12, 31), None) == date(2026, 12, 31)
    assert planned_end(date(2026, 12, 31), date(2026, 11, 30)) == date(2026, 11, 30)
    assert planned_end(date(2026, 11, 15), date(2026, 11, 30)) == date(2026, 11, 15)


@pytest.mark.parametrize("days,expected", [(30, 30), (15, 15), (5, 5), (2, 2), (1, 1),
                                           (29, None), (0, None), (10, None)])
def test_reminders_go_30_15_5_2_1_days_before(days, expected):
    from datetime import timedelta
    assert reminder_due(TODAY + timedelta(days=days), TODAY) == expected


def test_release_and_extension_rules():
    assert release_problem("release", date(2026, 12, 31), None, date(2026, 1, 1), TODAY) is None
    assert "before the onboarding" in release_problem("release", date(2025, 12, 1), None,
                                                      date(2026, 1, 1), TODAY)
    assert "set the release date first" in release_problem("extension", date(2027, 1, 31), None,
                                                            None, TODAY)
    assert "must be after" in release_problem("extension", date(2026, 12, 1), date(2026, 12, 31),
                                              None, TODAY)
    assert release_problem("extension", date(2027, 3, 31), date(2026, 12, 31), None, TODAY) is None
    assert "three years" in release_problem("release", date(2031, 1, 1), None, None, TODAY)


def _row(pe, rate, start, exit=None, release=None, customer=1, project=10):
    return {"pe_id": pe, "rate": rate, "unit": "Monthly", "start": start, "exit": exit,
            "release": release, "customer_id": customer, "customer": f"C{customer}",
            "project_id": project, "project": f"P{project}", "name": f"E{pe}"}


def test_a_release_cuts_the_prediction_from_that_day():
    rows = [_row(1, 300000, date(2026, 1, 1), release=date(2026, 11, 15)),
            _row(2, 100000, date(2026, 1, 1), customer=2, project=20)]
    nov = month_prediction(rows, Month(2026, 11))
    assert round(nov["amount"]) == 100000 + 150000      # half of November for E1
    assert len(nov["releasing"]) == 1
    dec = month_prediction(rows, Month(2026, 12))
    assert round(dec["amount"]) == 100000 and dec["customers"] == {2}


def test_trend_counts_customers_and_projects_per_bucket():
    rows = [_row(1, 300000, date(2026, 1, 1), release=date(2026, 12, 31)),
            _row(2, 100000, date(2026, 1, 1), customer=2, project=20)]
    t = predicted_trend(rows, TODAY, {"2026-11": 50000})
    assert len(t["month"]) == 12 and len(t["quarter"]) == 4 and len(t["fy"]) == 2
    first = t["month"][0]
    assert first["key"] == "2026-11" and first["current"] and first["billed_so_far"] == 50000
    assert first["customers"] == 2 and first["projects"] == 2
    jan = t["month"][2]
    assert jan["customers"] == 1 and round(jan["lost_to_releases"]) == 300000
    assert t["releases"][0]["release_date"] == "2026-12-31"


def test_net_revenue_is_billed_minus_expenses():
    n = net_figures(1000000, 600000, 150000)
    assert n["net_revenue"] == 850000 and n["net_profit"] == 250000
    assert n["net_margin_pct"] == 25.0 and n["expense_ratio_pct"] == 15.0
    assert net_figures(0, 0, 10)["net_margin_pct"] is None


def test_expense_grouping_and_validation():
    rows = [{"date": date(2026, 4, 3), "category": "Rent & office", "amount": 100.0},
            {"date": date(2026, 4, 9), "category": "Travel", "amount": 50.0},
            {"date": date(2026, 5, 1), "category": "Rent & office", "amount": 100.0}]
    assert by_month(rows) == {"2026-04": 150.0, "2026-05": 100.0}
    cats = by_category(rows)
    assert cats[0]["category"] == "Rent & office" and cats[0]["share_pct"] == 80.0
    ok = clean({"expense_date": "2026-04-03", "category": "Travel", "description": "Cab",
                "amount": "1200.5", "tax_amount": None})
    assert ok["amount"] == Decimal("1200.50") and ok["tax_amount"] == Decimal("0.00")
    for bad in ({"amount": 0}, {"amount": -1}, {"description": ""}, {"expense_date": "2099-01-01"}):
        body = {"expense_date": "2026-04-03", "category": "Travel", "description": "Cab",
                "amount": 10, **bad}
        with pytest.raises(HTTPException):
            clean(body)
    assert clean({"amount": 5}, partial=True) == {"amount": Decimal("5.00")}


# ------------------------------------------------------------------ database


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    told: list[dict] = []
    import services.notify as notify
    monkeypatch.setattr(notify, "notify_roles", lambda db, roles, title="", message="", link="", **kw:
                        told.append({"title": title, **kw}) or 1)
    s.info["told"] = told
    try:
        yield s
    finally:
        s.close()


def _pe(db):
    from models import Customer, Employee, Project, ProjectEmployee
    cust = Customer(name="HARMAN"); db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Infotainment"); db.add(proj); db.flush()
    emp = Employee(first_name="Asha", email="asha@karnex.in"); db.add(emp); db.flush()
    pe = ProjectEmployee(project_id=proj.id, employee_id=emp.id, billing_rate=300000,
                         onboarding_date=date(2026, 1, 5))
    db.add(pe); db.commit()
    return pe


def test_release_then_extension_keeps_history_and_reminds_on_the_day(db):
    from types import SimpleNamespace
    from services.pe_release import clear_release, run_release_reminders, set_release
    pe = _pe(db)
    user = SimpleNamespace(id=1, full_name="Bala")
    out = set_release(db, pe, date(2026, 12, 4), "release", None, "Budget cut", user, today=TODAY)
    db.commit()
    assert out["release_date"] == "2026-12-04" and len(out["history"]) == 1
    with pytest.raises(HTTPException):
        set_release(db, pe, date(2026, 11, 30), "extension", None, "", user, today=TODAY)
    out = set_release(db, pe, date(2027, 1, 31), "extension", None, "Customer extended", user, today=TODAY)
    db.commit()
    assert out["extensions"] == 1 and [h["kind"] for h in out["history"]] == ["extension", "release"]
    db.info["told"].clear()
    assert run_release_reminders(db, date(2027, 1, 1))["reminded"] == 1     # 30 days before
    assert run_release_reminders(db, date(2027, 1, 2))["reminded"] == 0
    assert "in 30 days" in db.info["told"][0]["title"]
    out = clear_release(db, pe, "Withdrawn", user)
    db.commit()
    assert out["release_date"] is None and out["history"][0]["kind"] == "cleared"


def test_team_release_saves_each_person_and_names_who_was_skipped(db):
    """Project page ▸ Team release date (4 Nov 2026, later): one date for several people;
    a person the date does not fit is skipped and named, the rest are saved."""
    from types import SimpleNamespace
    from models import Employee, ProjectEmployee
    from routers.crm.projects import TeamReleaseIn, set_team_release
    pe = _pe(db)
    late = Employee(first_name="Ravi", email="ravi@karnex.in"); db.add(late); db.flush()
    pe2 = ProjectEmployee(project_id=pe.project_id, employee_id=late.id, billing_rate=200000,
                          onboarding_date=date(2027, 2, 1))       # joins after the release date
    db.add(pe2); db.commit()
    user = SimpleNamespace(id=1, full_name="Bala")
    end = date(date.today().year + 1, 1, 15)
    res = set_team_release(pe.project_id, TeamReleaseIn(release_date=end, pe_ids=[pe.id, pe2.id, 999]),
                           db=db, user=user)
    body = res if isinstance(res, dict) else res.body
    data = body["data"] if isinstance(body, dict) else __import__("json").loads(body)["data"]
    assert data["saved"] == [pe.id]
    assert {s["pe_id"] for s in data["skipped"]} == {pe2.id, 999}
    db.refresh(pe)
    assert pe.release_date == end
    # Nobody fits → 400, nothing saved.
    with pytest.raises(HTTPException):
        set_team_release(pe.project_id, TeamReleaseIn(kind="extension", release_date=end, pe_ids=[pe2.id]),
                         db=db, user=user)
