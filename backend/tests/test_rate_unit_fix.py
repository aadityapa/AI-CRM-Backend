"""A rate typed against the wrong billing unit (reported 25 Sep 2026).

Kaluvoi Reddy, Aug 2026: 168 billable hours, invoice ₹1,414.77. The Project
Employee had been mapped at ₹1,414.77 with the form's DEFAULT unit "Monthly",
so the engine correctly billed one month at ₹1,414.77 — and nothing anywhere
let anyone change the unit afterwards. Pinned here:

* the preview names the likely typo (`rate_unit_warning`) before money moves;
* `PUT /api/projects/employees/{pe}/billing-unit` relabels the WHOLE rate
  history (never mints a new row) and re-freezes approved, uninvoiced sheets;
* the older PE edit routes route a unit change through the same helper.

Run:  cd backend && python -m pytest tests/test_rate_unit_fix.py -q
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.test_midmonth_timesheet import _seed_sheet, db  # noqa: F401 - fixture


HOURLY_RATE = 1414.77


@pytest.fixture()
def client(db, monkeypatch):  # noqa: F811
    """Projects + timesheets routers as Admin. The PE detail serializer uses a
    Postgres-only concat() for its leave history; the unit fix is not about it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.projects as projects_router
    import routers.crm.timesheets as ts_router
    monkeypatch.setattr(projects_router, "project_employee_detail_out",
                        lambda _db, pe: {"id": pe.id})
    app = FastAPI()
    app.include_router(projects_router.router)
    app.include_router(ts_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="admin", roles={"Admin"})
    return TestClient(app)


def _seed_wrong_unit(db):  # noqa: F811
    """Feb 2026, all 20 working days Present at 8 h → 160 billable hours."""
    from models import ProjectEmployee
    from services.project_employees import ensure_initial_rate
    from services.timesheets import freeze_invoice_figures

    ts = _seed_sheet(db, onboarding=date(2026, 1, 1), unit="Monthly", rate=HOURLY_RATE)
    pe = db.execute(select(ProjectEmployee)).scalars().one()
    ensure_initial_rate(db, pe)
    freeze_invoice_figures(db, ts, _entries(db, ts))
    db.commit()
    return ts, pe


def _entries(db, ts):  # noqa: F811
    from models import TimesheetEntry
    return db.execute(select(TimesheetEntry).where(TimesheetEntry.timesheet_id == ts.id)
                      .order_by(TimesheetEntry.entry_date)).scalars().all()


def test_the_warning_names_an_hourly_rate_saved_per_month():
    from services.project_employee_billing import rate_unit_warning

    msg = rate_unit_warning("Monthly", Decimal("1414.77"), Decimal("8.42"))
    assert msg and "Per Hour" in msg and "1,414.77" in msg
    # A real monthly rate, a real hourly rate: silent.
    assert rate_unit_warning("Monthly", Decimal("300000"), Decimal("1785.71")) is None
    assert rate_unit_warning("Hourly", Decimal("1414.77"), Decimal("1414.77")) is None
    # A monthly figure saved per HOUR is the opposite typo.
    assert rate_unit_warning("Hourly", Decimal("300000"), Decimal("300000"))
    # Nothing to judge → nothing said.
    assert rate_unit_warning("Monthly", None, Decimal("1")) is None
    assert rate_unit_warning(None, Decimal("1"), Decimal("1")) is None


def test_the_preview_carries_the_warning_even_on_frozen_figures(db, client):  # noqa: F811
    ts, _pe = _seed_wrong_unit(db)
    r = client.get(f"/api/timesheets/{ts.id}/invoice-preview")
    assert r.status_code == 200, r.text
    li = r.json()["data"]["line_items"][0]
    assert li["billing_unit"] == "Monthly"
    assert li["amount"] == pytest.approx(HOURLY_RATE)       # one month, as saved
    assert li["rate_unit_warning"] and "Per Hour" in li["rate_unit_warning"]


def test_changing_the_unit_relabels_history_and_rebills_the_approved_sheet(db, client):  # noqa: F811
    from models import ProjectEmployeeRate

    ts, pe = _seed_wrong_unit(db)
    rows_before = db.execute(select(ProjectEmployeeRate)).scalars().all()
    assert len(rows_before) == 1

    r = client.put(f"/api/projects/employees/{pe.id}/billing-unit",
                        json={"billing_unit": "Hourly"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["recalculated"][0]["sub_total_after"] == pytest.approx(160 * HOURLY_RATE)

    rows_after = db.execute(select(ProjectEmployeeRate)).scalars().all()
    assert len(rows_after) == 1                                   # no new row minted
    assert all(getattr(r.billing_unit, "value", r.billing_unit) == "Hourly" for r in rows_after)

    db.refresh(ts)
    assert ts.approved_figures["totals"]["sub_total"] == pytest.approx(160 * HOURLY_RATE)
    li = client.get(f"/api/timesheets/{ts.id}/invoice-preview").json()["data"]["line_items"][0]
    assert li["billing_unit"] == "Hourly"
    assert li["rate_unit_warning"] is None


def test_the_pe_edit_route_changes_the_unit_the_same_way(db, client):  # noqa: F811
    from models import ProjectEmployeeRate

    ts, pe = _seed_wrong_unit(db)
    r = client.put(f"/api/projects/employees/{pe.id}", json={"billing_unit": "Hourly"})
    assert r.status_code == 200, r.text
    rows = db.execute(select(ProjectEmployeeRate)).scalars().all()
    assert len(rows) == 1 and getattr(rows[0].billing_unit, "value", rows[0].billing_unit) == "Hourly"
    db.refresh(ts)
    assert ts.approved_figures["totals"]["sub_total"] == pytest.approx(160 * HOURLY_RATE)


def test_the_same_unit_again_changes_nothing(db, client):  # noqa: F811
    ts, pe = _seed_wrong_unit(db)
    frozen_at = ts.approved_figures["frozen_at"]
    r = client.put(f"/api/projects/employees/{pe.id}/billing-unit",
                        json={"billing_unit": "Monthly"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["recalculated"] == []
    db.refresh(ts)
    assert ts.approved_figures["frozen_at"] == frozen_at
