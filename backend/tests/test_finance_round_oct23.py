"""23 Oct 2026 — the finance round.

1. "Awaiting customer approval reads 101 on Invoices but 25 on the Sales
   Manager's dashboard": both now use `billing_rules.awaiting_customer_clause`.
2. Data start month (FY 2026-27 → March 2026): lists and queues start there.
3. "Project closed on 30 Apr 2026 — why a May timesheet?": no sheet past the
   last working day; untouched ones are removed.
4. Finance desk: Pending e-invoices, Pending record payment, TDS, receipts.
5. Received amount: the branch is inferred or asked for; 0147 backfills it.

Run:  cd backend && python -m pytest tests/test_finance_round_oct23.py -q
"""
from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
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


for _m in ["base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
           "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
           "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates"]:
    importlib.import_module(f"models.{_m}")

from crm_deps import CurrentUser  # noqa: E402
from models.base import Base  # noqa: E402
from models import (  # noqa: E402
    Customer, CustomerBranch, Employee, Invoice, Project, ProjectEmployee, Timesheet, TimesheetStatus,
)
from models.finance import CustomerReceipt  # noqa: E402
from services import billing_rules as br  # noqa: E402
import services.work_desk as wd  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
FINANCE = CurrentUser(id=3, username="fin", full_name="Fin", roles={"Finance"})


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    from models.base import users_table_stub
    s.execute(users_table_stub.insert().values(id=1))
    s.commit()
    import services.dashboard_desk as dd
    import services.dashboards as dsh
    monkeypatch.setattr(dd, "upcoming", lambda _db, _u, days=7: {"days": days, "items": []})
    monkeypatch.setattr(dsh, "my_work", lambda _db, _u: {"items": [], "all_clear": True})
    try:
        yield s
    finally:
        s.close()


def _book(db):
    cust = Customer(name="HARMAN")
    db.add(cust); db.flush()
    proj = Project(customer_id=cust.id, name="Cluster")
    emp = Employee(first_name="Asha", last_name="Rao", email="asha@k.in")
    db.add_all([proj, emp]); db.flush()
    return cust, proj, emp


def _sheet(db, proj, emp, year, month, status=TimesheetStatus.APPROVED):
    ts = Timesheet(project_id=proj.id, employee_id=emp.id, year=year, month=month, status=status)
    db.add(ts); db.flush()
    return ts


def _tax(db, proj, number, *, ts=None, day=None, **kw):
    inv = Invoice(invoice_number=number, project_id=proj.id, timesheet_id=getattr(ts, "id", None),
                  invoice_date=day or date.today(), sub_total=100000, grand_total=118000,
                  balance_amount=kw.pop("balance_amount", 118000), kind="Tax", **kw)
    db.add(inv); db.flush()
    return inv


# ------------------------------------------------------------- 1 + 2

def test_awaiting_customer_is_one_rule_with_no_own_window(db, monkeypatch):
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2026-03")
    _, proj, emp = _book(db)
    long_ago = date.today() - timedelta(days=200)          # the old 90-day tile window hid these
    for i, month in enumerate((3, 4, 5, 6)):
        _tax(db, proj, f"INV-{i}", ts=_sheet(db, proj, emp, 2026, month), day=long_ago)
    _tax(db, proj, "INV-FEB", ts=_sheet(db, proj, emp, 2026, 2), day=long_ago)   # before the data start
    _tax(db, proj, "INV-OK", ts=_sheet(db, proj, emp, 2026, 7),
         customer_approved_at=datetime.now(timezone.utc))
    listed = db.execute(select(func.count()).select_from(Invoice).where(br.awaiting_customer_clause())).scalar()
    tab = wd._customer_confirm_tab(db)
    assert listed == tab["count"] == 4
    assert {i["title"].split(" ")[0] for i in tab["items"]} == {"INV-0", "INV-1", "INV-2", "INV-3"}
    # The list route uses the same clause.
    src = (BACKEND / "routers" / "crm" / "finance.py").read_text(encoding="utf-8")
    assert "billing_rules.invoice_window_clause()" in src
    assert "CONFIRM_LOOKBACK_DAYS" not in (BACKEND / "services" / "work_desk.py").read_text(encoding="utf-8")


def test_data_start_month_setting_and_window(monkeypatch):
    from services.org_settings import validation_error
    assert validation_error("billing.data_start_month", "2026-03") is None
    assert validation_error("billing.data_start_month", "March 2026")
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2026-03")
    assert br.data_start() == (2026, 3) and br.data_start_label() == "March 2026"
    assert not br.month_in_window(2026, 2) and br.month_in_window(2026, 3)
    assert br.start_refusal(2026, 2) and br.start_refusal(2026, 4) is None
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "garbage")
    assert br.data_start() == br.DEFAULT_DATA_START


# ------------------------------------------------------------- 3

def test_no_month_is_owed_after_the_last_working_day(monkeypatch):
    monkeypatch.setenv("BILLING_DATA_START_MONTH", "2026-03")
    project = SimpleNamespace(end_date=date(2026, 4, 30))
    pe = SimpleNamespace(onboarding_date=date(2025, 11, 3), billing_date=None, exit_date=date(2026, 4, 30))
    assert br.owed_months(pe, project, date(2026, 10, 23)) == [(2026, 3), (2026, 4)]
    assert br.month_after_end(2026, 5, project, pe) and not br.month_after_end(2026, 4, project, pe)
    assert "30 Apr 2026" in br.end_refusal(2026, 5, project, pe)
    assert br.end_refusal(2026, 4, project, pe) is None
    open_pe = SimpleNamespace(onboarding_date=date(2026, 9, 2), billing_date=None, exit_date=None)
    assert br.owed_months(open_pe, SimpleNamespace(end_date=None), date(2026, 10, 23)) == [(2026, 9), (2026, 10)]


def test_untouched_sheets_after_the_end_are_removed(db):
    _, proj, emp = _book(db)
    proj.end_date = date(2026, 4, 30)
    db.add(ProjectEmployee(project_id=proj.id, employee_id=emp.id, onboarding_date=date(2026, 1, 1),
                           billing_rate=1000))
    april = _sheet(db, proj, emp, 2026, 4, TimesheetStatus.DRAFT)
    may = _sheet(db, proj, emp, 2026, 5, TimesheetStatus.DRAFT)
    june_submitted = _sheet(db, proj, emp, 2026, 6, TimesheetStatus.SUBMITTED)
    july_invoiced = _sheet(db, proj, emp, 2026, 7, TimesheetStatus.REJECTED)
    _tax(db, proj, "INV-JUL", ts=july_invoiced)
    assert br.drop_sheets_after_end(db, proj) == 1
    left = {t.month for t in db.execute(select(Timesheet)).scalars()}
    assert left == {april.month, june_submitted.month, july_invoiced.month} and may.month not in left


def test_create_route_refuses_and_migration_cleans():
    src = (BACKEND / "routers" / "crm" / "timesheets.py").read_text(encoding="utf-8")
    assert "billing_rules.end_refusal(body.year, body.month, project, assignment)" in src
    assert "billing_rules.start_refusal(body.year, body.month)" in src
    mig = next((BACKEND / "alembic" / "versions").glob("0147_*.py")).read_text(encoding="utf-8")
    assert 'down_revision = "0146"' in mig and "Draft', 'Rejected'" in mig
    assert "NOT EXISTS (SELECT 1 FROM invoices" in mig


# ------------------------------------------------------------- 4

def test_finance_payment_tds_and_receipt_tabs(db):
    cust, proj, emp = _book(db)
    late = _tax(db, proj, "INV-LATE", ts=_sheet(db, proj, emp, 2026, 5), irn_number="a" * 64,
                day=date.today() - timedelta(days=60), customer_approved_at=datetime.now(timezone.utc))
    _tax(db, proj, "INV-PAID", ts=_sheet(db, proj, emp, 2026, 6), irn_number="b" * 64,
         payment_status="Paid", balance_amount=0)
    _tax(db, proj, "INV-NOIRN", ts=_sheet(db, proj, emp, 2026, 7))
    db.add(CustomerReceipt(customer_id=cust.id, received_date=date.today(), amount=50000,
                           unallocated_amount=50000))
    db.flush()
    pay = wd._payment_pending_tab(db)
    assert [i["path"] for i in pay["items"]] == [f"invoices/{late.id}"]
    assert pay["items"][0]["chip"] == "30 d late" and pay["items"][0]["section"] == "Overdue"
    rc = wd._receipts_to_map_tab(db)
    assert rc["count"] == 1 and rc["items"][0]["action"] == "Map invoices"
    assert rc["items"][0]["path"].startswith("received-amounts?receipt=")
    assert wd._tab("fin_customer_approved", [])["label"] == "Pending e-invoices"


# ------------------------------------------------------------- 5

def test_receipt_branch_is_inferred(db):
    from routers.crm.customer_receipts import _infer_branch
    cust, proj, _ = _book(db)
    only = CustomerBranch(customer_id=cust.id, branch_name="Pune")
    db.add(only); db.flush()
    assert _infer_branch(db, cust.id, []).id == only.id
    db.add(CustomerBranch(customer_id=cust.id, branch_name="Bengaluru")); db.flush()
    assert _infer_branch(db, cust.id, []) is None                      # two branches: ask
    proj.branch_id = only.id
    inv = _tax(db, proj, "INV-B")
    assert _infer_branch(db, cust.id, [inv]).id == only.id             # the invoices' one branch
    src = (BACKEND / "routers" / "crm" / "customer_receipts.py").read_text(encoding="utf-8")
    assert '@router.post("/assign-branch")' in src
    assert src.index('@router.post("/assign-branch")') < src.index('@router.put("/{receipt_id}")')
