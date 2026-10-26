"""The Dashboard's billing tracker (20 Oct 2026, user report: "invoice approved
but not showing there, same for timesheet").

Pinned: every approved timesheet and every invoice is a row — no time window,
no page cut — the stage follows the chain, the month is the WORK month, the
customer filter finds an invoice through its PO too, and the filter choices
never shrink to the current filter.

Run:  cd backend && python -m pytest tests/test_billing_tracker.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone

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
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from services.billing_tracker import (  # noqa: E402
    TrackerFilters, billing_tracker, period_label, stage_of,
)


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


def test_stage_and_period_rules_are_plain():
    assert stage_of(kind=None, returned=False, customer_approved=False, irn=False,
                    payment_status=None) == "awaiting_proforma"
    assert stage_of(kind="Proforma", returned=True, customer_approved=False, irn=False,
                    payment_status="Unpaid") == "returned"
    assert stage_of(kind="Tax", returned=False, customer_approved=False, irn=False,
                    payment_status="Unpaid") == "awaiting_customer"
    assert stage_of(kind="Tax", returned=False, customer_approved=True, irn=False,
                    payment_status="Unpaid") == "irn_pending"
    assert stage_of(kind="Tax", returned=False, customer_approved=True, irn=True,
                    payment_status="Partially_Paid") == "unpaid"
    assert stage_of(kind="Tax", returned=False, customer_approved=False, irn=False,
                    payment_status="Paid") == "paid"
    assert period_label(2026, 5, date(2026, 5, 1), date(2026, 5, 31)) is None
    assert period_label(2026, 5, date(2026, 4, 21), date(2026, 5, 30)) == "21 Apr – 30 May 2026"
    assert period_label(2027, 1, date(2026, 12, 21), date(2027, 1, 20)) == "21 Dec 2026 – 20 Jan 2027"


def _seed(db):
    from models import (
        Customer, Employee, Invoice, Project, PurchaseOrder, Timesheet, TimesheetStatus,
    )
    uno, other = Customer(name="Uno Minda"), Customer(name="Harman")
    db.add_all([uno, other])
    db.flush()
    p_uno = Project(name="CREAT T&M", customer_id=uno.id)
    p_other = Project(name="Harman ADAS", customer_id=other.id)
    a = Employee(first_name="Aakash", last_name="Malwade", email="a@karnex.in")
    b = Employee(first_name="Bina", last_name="Rao", email="b@karnex.in")
    db.add_all([p_uno, p_other, a, b])
    db.flush()
    old = datetime.now(timezone.utc) - timedelta(days=400)   # far outside any desk window
    s1 = Timesheet(project_id=p_uno.id, employee_id=a.id, year=2025, month=6,
                   status=TimesheetStatus.APPROVED, approved_at=old)
    s2 = Timesheet(project_id=p_uno.id, employee_id=a.id, year=2026, month=5,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc),
                   period_start_date=date(2026, 4, 21), period_end_date=date(2026, 5, 30))
    s3 = Timesheet(project_id=p_other.id, employee_id=b.id, year=2026, month=5,
                   status=TimesheetStatus.APPROVED, approved_at=datetime.now(timezone.utc))
    draft = Timesheet(project_id=p_uno.id, employee_id=b.id, year=2026, month=6,
                      status=TimesheetStatus.DRAFT)
    db.add_all([s1, s2, s3, draft])
    db.flush()
    # s2 → a Proforma with Finance; s3 → a customer-approved tax invoice (IRN pending).
    db.add_all([
        Invoice(invoice_number="PI-2026-005", proforma_number="PI-2026-005", kind="Proforma",
                project_id=p_uno.id, timesheet_id=s2.id, invoice_date=date(2026, 10, 19),
                sub_total=273340, tax_amount=49201.2, grand_total=322541),
        Invoice(invoice_number="INV-2026-001", kind="Tax", project_id=p_other.id, timesheet_id=s3.id,
                invoice_date=date(2026, 6, 3), sub_total=100, tax_amount=18, grand_total=118,
                customer_approved_at=datetime.now(timezone.utc)),
    ])
    # A manual invoice whose PROJECT belongs to Harman but whose PO is Uno Minda's.
    po = PurchaseOrder(po_number="4300472106", customer_id=uno.id, total_value=1000,
                       balance_value=1000, consumed_value=0)
    db.add(po)
    db.flush()
    db.add(Invoice(invoice_number="INV-2026-002", kind="Tax", project_id=p_other.id, po_id=po.id,
                   invoice_date=date(2026, 7, 9), sub_total=10, tax_amount=1.8, grand_total=11.8))
    db.commit()
    return uno, other


def test_every_approved_sheet_and_invoice_is_a_row(db):
    uno, _ = _seed(db)
    out = billing_tracker(db, TrackerFilters(limit=100))
    rows = out["rows"]
    by_key = {r["key"]: r for r in rows}
    assert out["meta"]["total"] == 4                       # 3 approved sheets + 1 manual invoice; no draft
    stages = sorted(r["stage"] for r in rows)
    assert stages == ["awaiting_customer", "awaiting_proforma", "irn_pending", "proforma"]
    # The 400-day-old approved sheet is still there (the desk's window hid it).
    assert any(r["month"] == "2025-06" and r["stage"] == "awaiting_proforma" for r in rows)
    # The cross-month sheet says its period; the Proforma row says its work month.
    pi = next(r for r in rows if r["invoice_number"] == "PI-2026-005")
    assert (pi["month_label"], pi["period_label"]) == ("May 2026", "21 Apr – 30 May 2026")
    assert pi["customer"] == "Uno Minda"
    # Counts per stage + "open".
    assert out["meta"]["counts"]["all"] == 4 and out["meta"]["counts"]["open"] == 4
    assert by_key  # keys are unique


def test_filters_narrow_rows_but_never_the_choices(db):
    uno, other = _seed(db)
    may = billing_tracker(db, TrackerFilters(month="2026-05"))
    assert {r["employee"] for r in may["rows"]} == {"Aakash Malwade", "Bina Rao"}
    # A chosen month does not shrink the month dropdown.
    assert {m["key"] for m in may["meta"]["options"]["months"]} >= {"2025-06", "2026-05", "2026-07"}
    # 27 Oct 2026 (filter audit): ONE customer per invoice — the PO's, else the
    # project's (the Invoices list / desk rule). The manual invoice on a Harman
    # project drawn on Uno Minda's PO belongs to Uno Minda, in ONE customer only.
    harman = billing_tracker(db, TrackerFilters(customer_id=other.id))
    assert {r["invoice_number"] for r in harman["rows"]} == {"INV-2026-001"}
    unos = billing_tracker(db, TrackerFilters(customer_id=uno.id))
    assert "INV-2026-002" in {r["invoice_number"] for r in unos["rows"]}
    # Stage filter narrows the rows, not the counts.
    irn = billing_tracker(db, TrackerFilters(stage="irn_pending"))
    assert [r["invoice_number"] for r in irn["rows"]] == ["INV-2026-001"]
    assert irn["meta"]["counts"]["all"] == 4
    # Search finds the employee and the PI number.
    assert billing_tracker(db, TrackerFilters(search="pi-2026-005"))["meta"]["total"] == 1
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(month="May"))
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(stage="nope"))


def test_invoice_list_customer_filter_finds_an_invoice_by_its_po(db):
    """`GET /api/invoices?customer_id=` used to join the PROJECT's customer only,
    while the row printed the PO's — an invoice could be shown as Uno Minda's
    and still be missing from Uno Minda's list."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.finance as fin

    uno, _ = _seed(db)
    app = FastAPI()
    app.include_router(fin.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(
        id=1, username="fin", roles={"Finance"})
    r = TestClient(app).get(f"/api/invoices?customer_id={uno.id}")
    assert r.status_code == 200, r.text
    assert "INV-2026-002" in {x["invoice_number"] for x in r.json()["data"]}


def test_the_route_is_finance_sales_manager_head_admin_only():
    """26 Oct 2026: Finance, the Sales Manager and the Sales Head (by role) +
    Admin / CEO. Plain Sales, RMG, TA, HR are refused; no template widens it."""
    import inspect

    from fastapi import HTTPException

    import crm_deps
    import routers.crm.dashboards as dash
    assert "Depends(billing_reader)" in inspect.getsource(dash.billing_tracker)

    def u(*roles):
        return crm_deps.CurrentUser(id=1, username="x", roles=set(roles))

    for ok in (u("Finance"), u("Sales_Head"), u("Sales", "Sales Manager"), u("Admin"), u("CEO")):
        assert dash.billing_reader(ok) is ok
    for no in (u("Sales"), u("RMG"), u("TA"), u("HR"), u("GM")):
        with pytest.raises(HTTPException):
            dash.billing_reader(no)


def test_every_generated_invoice_and_the_csv(db):
    """26 Oct 2026 — `stage=invoiced` is every issued tax invoice, newest first;
    the CSV holds every filtered row (not one page)."""
    from services.billing_tracker import CSV_COLUMNS, to_csv

    _seed(db)
    out = billing_tracker(db, TrackerFilters(stage="invoiced", limit=1))
    assert out["meta"]["counts"]["invoiced"] == 2
    assert out["meta"]["total"] == 2 and len(out["rows"]) == 1        # paged …
    full = billing_tracker(db, TrackerFilters(stage="invoiced", limit=1), export=True)
    assert [r["invoice_number"] for r in full["rows"]] == ["INV-2026-002", "INV-2026-001"]  # … export is not
    csv = to_csv(full["rows"] + [{"customer": "=HYPERLINK(1)"}])
    lines = csv.strip().splitlines()
    assert lines[0].split(",")[0] == CSV_COLUMNS[0][1] and len(lines) == 4
    assert "'=HYPERLINK" in csv                                            # formula neutralised


def test_returned_proformas_board_and_reopen(db, monkeypatch):
    """26 Oct 2026 — a Proforma Finance returned shows on the board with its
    month, reason and whose move it is; Sales reopens the sheet to correct it."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import crm_deps
    import routers.crm.dashboards as dash
    import routers.crm.timesheets as tsr
    from models import Invoice, Timesheet, TimesheetStatus
    from services.returned_proformas import board, next_step

    # PURE whose-move rule.
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert next_step("Approved", t0, t0 + timedelta(days=1)) == "to_correct"
    assert next_step("Approved", t0 + timedelta(days=2), t0) == "reissue"
    assert next_step("Rejected", None, t0) == "correcting"
    assert next_step("Submitted", None, t0) == "resubmitted"
    assert next_step(None, None, t0) == "manual"

    _seed(db)
    pi = db.query(Invoice).filter_by(invoice_number="PI-2026-005").one()
    pi.returned_at = datetime.now(timezone.utc)
    pi.returned_reason = "Leave on 12 May is missing"
    db.commit()

    sales = crm_deps.CurrentUser(id=1, username="sanjana", roles={"Sales"})
    out = board(db, sales)
    assert out["meta"]["counts"]["to_correct"] == 1 and out["meta"]["can_reopen"]
    row = out["rows"][0]
    assert (row["month_label"], row["returned_reason"], row["step"]) == ("May 2026", "Leave on 12 May is missing", "to_correct")
    assert board(db, sales, month="2026-06")["rows"] == []

    app = FastAPI()
    app.include_router(dash.router)
    app.include_router(tsr.router)
    app.dependency_overrides[crm_deps.get_crm_db] = lambda: db
    app.dependency_overrides[crm_deps.get_current_user] = lambda: sales
    c = TestClient(app)
    r = c.get("/api/dashboard/returned-proformas")
    assert r.status_code == 200 and r.json()["data"][0]["proforma_number"] == "PI-2026-005"
    monkeypatch.setattr(tsr, "_require_owner_or", lambda *a, **k: None)   # template check not under test
    r = c.post(f"/api/timesheets/{row['timesheet_id']}/reopen-for-correction", json={"note": "fixing leave"})
    assert r.status_code == 200, r.text
    ts = db.get(Timesheet, row["timesheet_id"])
    assert ts.status == TimesheetStatus.REJECTED and "Leave on 12 May" in ts.rejection_reason
    assert board(db, sales)["rows"][0]["step"] == "correcting"
    # Twice → refused (no longer approved).
    assert c.post(f"/api/timesheets/{row['timesheet_id']}/reopen-for-correction", json={}).status_code == 409
    # Someone with no reason to see it is refused.
    app.dependency_overrides[crm_deps.get_current_user] = lambda: crm_deps.CurrentUser(id=9, username="ta", roles={"TA"})
    assert c.get("/api/dashboard/returned-proformas").status_code == 403


def test_finance_filters_24_oct(db):
    """24 Oct 2026 — branch · project · employee · PO · days late · date range ·
    amount · waiting · quick switches, all narrowing the counts too."""
    from models import CustomerBranch, Invoice, Project, TdsRecord
    from services.billing_tracker import days_late, late_bucket, parse_flags

    uno, other = _seed(db)
    br = CustomerBranch(customer_id=other.id, branch_name="Pune")
    db.add(br)
    db.flush()
    p_other = db.query(Project).filter_by(customer_id=other.id).one()
    p_other.branch_id = br.id
    inv1 = db.query(Invoice).filter_by(invoice_number="INV-2026-001").one()
    inv1.due_date = date.today() - timedelta(days=45)
    inv1.balance_amount = 118
    db.add(TdsRecord(invoice_id=inv1.id, tds_amount=10, tds_balance=10, tds_paid=0))
    db.commit()

    # PURE pieces.
    assert late_bucket(0) == "not_due" and late_bucket(45) == "late_31_60" and late_bucket(200) == "late_90_plus"
    assert days_late(None, date.today() - timedelta(days=40), date.today()) == 10
    assert parse_flags("split, tds_pending") == ("split", "tds_pending")
    with pytest.raises(ValueError):
        parse_flags("nope")

    base = billing_tracker(db, TrackerFilters(limit=100))
    row = next(r for r in base["rows"] if r["invoice_number"] == "INV-2026-001")
    assert (row["branch"], row["days_late"], row["late_bucket"], row["tds_pending"]) == ("Pune", 45, "late_31_60", 10.0)
    opts = base["meta"]["options"]
    assert {b["name"] for b in opts["branches"]} == {"Pune"}
    assert {p["name"] for p in opts["pos"]} == {"4300472106"}

    def nums(**kw):
        out = billing_tracker(db, TrackerFilters(limit=100, **kw))
        return sorted(r["invoice_number"] or f"ts{r['timesheet_id']}" for r in out["rows"]), out["meta"]["counts"]["all"]

    assert nums(branch_id=br.id)[0] == ["INV-2026-001", "INV-2026-002"]      # the Harman project's branch
    assert len(nums(branch_id=0)[0]) == 2                                      # Uno Minda rows have none
    assert nums(late="late_31_60") == (["INV-2026-001"], 1)                    # counts narrow too
    assert "INV-2026-001" in nums(late="late")[0]
    assert nums(flags=("tds_pending",))[0] == ["INV-2026-001"]
    assert nums(po_id=0)[0] != nums()[0]
    assert "INV-2026-002" in nums(date_from=date(2026, 7, 1), date_to=date(2026, 7, 31))[0]
    assert nums(amount_min=200)[0] == ["PI-2026-005"]
    assert nums(waiting_min=300)[0] == [f"ts{next(r['timesheet_id'] for r in base['rows'] if r['month'] == '2025-06')}"]
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(late="soon"))
    with pytest.raises(ValueError):
        billing_tracker(db, TrackerFilters(date_from=date(2026, 7, 2), date_to=date(2026, 7, 1)))
