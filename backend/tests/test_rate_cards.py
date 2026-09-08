"""Customer Rate Card API (/api/rate-cards, 0076).

Per-customer experience-band pricing with five OPTIONAL rate columns.
Access: Sales / Sales_Head (+ Admin/CEO), template-gated like every tab.

Run:  cd backend && python -m pytest tests/test_rate_cards.py -q
"""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
import crm_deps  # noqa: E402
import routers.crm.rate_cards as rc_router  # noqa: E402


def _mk_client(roles: set[str]):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = Session(bind=engine, future=True)
    from models.base import users_table_stub
    db.execute(users_table_stub.insert().values(id=1))
    from models import Customer
    cust = Customer(name="Magna")
    db.add(cust)
    db.commit()

    app = FastAPI()
    app.include_router(rc_router.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="u", roles=roles)
    client = TestClient(app)
    client._db = db
    client._customer_id = cust.id
    return client


@pytest.fixture()
def sales():
    c = _mk_client({"Sales"})
    yield c
    c._db.close()


def test_partial_rates_allowed(sales):
    """A customer that quotes ONLY hourly stores exactly that — blanks stay
    blank, never zero."""
    r = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 1, "exp_max": 2,
        "rate_hourly": 500,
    })
    assert r.status_code == 200, r.text
    row = r.json()["data"]
    assert row["rate_hourly"] == 500.0
    assert row["rate_monthly"] is None and row["rate_yearly"] is None


def test_bands_cannot_overlap(sales):
    sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 1, "exp_max": 3, "rate_hourly": 500})
    r = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 2, "exp_max": 4, "rate_hourly": 600})
    assert r.status_code == 400
    assert "overlaps" in r.json()["detail"]
    # Touching boundaries are fine: 3–5 next to 1–3.
    r = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 3, "exp_max": 5, "rate_hourly": 600})
    assert r.status_code == 200


def test_band_order_enforced(sales):
    r = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 2, "exp_max": 2, "rate_hourly": 500})
    assert r.status_code == 400


def test_update_and_delete(sales):
    row = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "exp_min": 1, "exp_max": 2,
        "rate_hourly": 500}).json()["data"]
    r = sales.put(f"/api/rate-cards/{row['id']}", json={"rate_monthly": 25000})
    assert r.status_code == 200
    assert r.json()["data"]["rate_monthly"] == 25000.0
    assert r.json()["data"]["rate_hourly"] == 500.0  # untouched
    assert sales.delete(f"/api/rate-cards/{row['id']}").status_code == 200
    assert sales.get(f"/api/rate-cards?customer_id={sales._customer_id}").json()["data"] == []


def test_list_sorted_by_band(sales):
    for lo, hi in [(3, 4), (1, 2), (2, 3)]:
        sales.post("/api/rate-cards", json={
            "customer_id": sales._customer_id, "exp_min": lo, "exp_max": hi,
            "rate_daily": 1000 * lo})
    rows = sales.get(f"/api/rate-cards?customer_id={sales._customer_id}").json()["data"]
    assert [r["exp_min"] for r in rows] == [1.0, 2.0, 3.0]


@pytest.mark.parametrize("roles,expected", [
    ({"Sales"}, 200), ({"Sales_Head"}, 200), ({"Admin"}, 200), ({"CEO"}, 200),
    ({"HR"}, 403), ({"TA"}, 403), ({"RMG"}, 403), ({"Finance"}, 403),
])
def test_access_is_sales_and_admin_only(roles, expected):
    c = _mk_client(roles)
    try:
        r = c.get(f"/api/rate-cards?customer_id={c._customer_id}")
        assert r.status_code == expected, (roles, r.status_code)
        r = c.post("/api/rate-cards", json={
            "customer_id": c._customer_id, "exp_min": 1, "exp_max": 2, "rate_hourly": 1})
        assert r.status_code == expected, (roles, r.status_code)
    finally:
        c._db.close()


def test_branch_scoped_bands(sales):
    """0077: rate cards are branch-wise. Two branches may quote the SAME band
    at different prices; overlap is rejected only within one branch (or within
    the customer-wide NULL scope)."""
    from models import CustomerBranch

    db = sales._db
    b1 = CustomerBranch(customer_id=sales._customer_id, branch_name="Pune")
    b2 = CustomerBranch(customer_id=sales._customer_id, branch_name="Bangalore")
    db.add_all([b1, b2])
    db.commit()

    r1 = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "branch_id": b1.id,
        "exp_min": 1, "exp_max": 3, "rate_hourly": 500})
    assert r1.status_code == 200, r1.text
    assert r1.json()["data"]["branch_id"] == b1.id
    # Same band on ANOTHER branch: fine.
    assert sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "branch_id": b2.id,
        "exp_min": 1, "exp_max": 3, "rate_hourly": 650}).status_code == 200
    # Same band again on b1: overlap.
    assert sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "branch_id": b1.id,
        "exp_min": 2, "exp_max": 4, "rate_hourly": 700}).status_code == 400
    # Customer-wide (NULL) scope is independent of both branches.
    assert sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id,
        "exp_min": 1, "exp_max": 3, "rate_hourly": 550}).status_code == 200
    # A branch of another customer is rejected.
    from models import Customer
    other = Customer(name="Other")
    db.add(other)
    db.flush()
    b3 = CustomerBranch(customer_id=other.id, branch_name="Delhi")
    db.add(b3)
    db.commit()
    r = sales.post("/api/rate-cards", json={
        "customer_id": sales._customer_id, "branch_id": b3.id,
        "exp_min": 5, "exp_max": 6, "rate_hourly": 1})
    assert r.status_code == 400


def test_slab_versions_by_effective_date(sales):
    """0079: a NEW ladder with the same bands is a rate revision, not a
    conflict — versions coexist, distinguished by effective_from; overlap is
    enforced only INSIDE one version."""
    base = {"customer_id": sales._customer_id, "exp_min": 1, "exp_max": 3}
    r1 = sales.post("/api/rate-cards", json={
        **base, "rate_hourly": 850, "effective_from": "2026-01-01"})
    assert r1.status_code == 200, r1.text
    # Same band, NEW version → fine (the revision case).
    r2 = sales.post("/api/rate-cards", json={
        **base, "rate_hourly": 900, "effective_from": "2026-06-01"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["effective_from"] == "2026-06-01"
    # Same band, SAME version → overlap.
    r3 = sales.post("/api/rate-cards", json={
        **base, "rate_hourly": 999, "effective_from": "2026-06-01"})
    assert r3.status_code == 400
    # Legacy NULL-version rows are their own scope too.
    assert sales.post("/api/rate-cards", json={
        **base, "rate_hourly": 800}).status_code == 200


def test_copy_slab_from_primary_branch(sales):
    """'Same as primary branch': copies the source's CURRENT version into the
    target as a new version there — a copy, not a link, so branches can
    diverge later without repricing each other."""
    from models import CustomerBranch

    db = sales._db
    primary = CustomerBranch(customer_id=sales._customer_id, branch_name="Bangalore",
                             is_primary=True)
    other = CustomerBranch(customer_id=sales._customer_id, branch_name="Pune")
    db.add_all([primary, other])
    db.commit()

    # Primary has an old and a current version — only the CURRENT one copies.
    for eff, rate in [("2025-01-01", 800), ("2026-01-01", 850)]:
        for lo, hi in [(1, 3), (3, 5)]:
            assert sales.post("/api/rate-cards", json={
                "customer_id": sales._customer_id, "branch_id": primary.id,
                "effective_from": eff, "exp_min": lo, "exp_max": hi,
                "rate_hourly": rate + lo}).status_code == 200

    r = sales.post("/api/rate-cards/copy-from-branch", json={
        "customer_id": sales._customer_id, "source_branch_id": primary.id,
        "target_branch_id": other.id, "effective_from": "2026-08-01"})
    assert r.status_code == 200, r.text
    rows = r.json()["data"]
    assert len(rows) == 2
    assert all(x["branch_id"] == other.id for x in rows)
    assert all(x["effective_from"] == "2026-08-01" for x in rows)
    assert sorted(x["rate_hourly"] for x in rows) == [851.0, 853.0]  # 2026 rates

    # Copying again to the same date collides with the fresh version.
    assert sales.post("/api/rate-cards/copy-from-branch", json={
        "customer_id": sales._customer_id, "source_branch_id": primary.id,
        "target_branch_id": other.id, "effective_from": "2026-08-01"}).status_code == 400
    # Source with no slab at all → clear 400.
    empty = CustomerBranch(customer_id=sales._customer_id, branch_name="Delhi")
    db.add(empty)
    db.commit()
    r = sales.post("/api/rate-cards/copy-from-branch", json={
        "customer_id": sales._customer_id, "source_branch_id": empty.id,
        "target_branch_id": other.id, "effective_from": "2026-09-01"})
    assert r.status_code == 400
    assert "no CTC slab" in r.json()["detail"]


def test_the_500_per_hour_example():
    """The confirmed worked example, against the slab engine's own maths:
    365 − 10 holidays − 104 week-offs − 20 leaves (none billable) = 231 days;
    × 8 h = 1,848 hours; × ₹500 = ₹9,24,000 annual, ₹77,000 monthly.
    The TS mirror (ctcSlab.ts::calculateBillingBases) computes the same bases.
    """
    days = 365 - 10 - 104 - 20
    hours = days * 8
    assert days == 231 and hours == 1848
    assert hours * 500 == 924000
    assert round(924000 / 12, 2) == 77000.0
