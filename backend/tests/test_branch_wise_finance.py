"""Branch-wise Customer ledger, receivables and Revenue report (2 Nov 2026).

Run:  cd backend && python -m pytest tests/test_branch_wise_finance.py -q
"""
from __future__ import annotations

import importlib
from datetime import date
from decimal import Decimal

import pytest
from fastapi import HTTPException
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
    return "CHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(45)"


for _m in ("base", "rbac", "customers", "opportunities", "requirements", "candidates", "profiles",
           "projects", "timesheets", "finance", "hr", "leave", "masters", "scheduling",
           "resumes", "ai_links", "email_outbox", "notify_routes", "template_requests",
           "user_profiles", "access_templates"):
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import Customer, CustomerBranch, Invoice, InvoicePayment, Project  # noqa: E402
from models.finance import CustomerReceipt  # noqa: E402
from crm_deps import CurrentUser  # noqa: E402
from routers.crm.finance_reports import (  # noqa: E402
    branch_matches, customer_ledger, ledger_branch_summary, receivables,
)
from services.revenue_report import (  # noqa: E402
    BranchProjects, project_clause, project_in, receipt_branch_scope, revenue_report, scope_projects,
)

FIN = CurrentUser(id=1, username="fin", full_name="Fin", roles={"Finance"})
TODAY = date(2026, 9, 30)


@pytest.fixture
def book():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        from models.base import users_table_stub
        s.execute(users_table_stub.insert().values(id=1))
        cust = Customer(name="VISTEON")
        s.add(cust); s.flush()
        pune = CustomerBranch(customer_id=cust.id, branch_name="Pune")
        blr = CustomerBranch(customer_id=cust.id, branch_name="Bengaluru")
        s.add_all([pune, blr]); s.flush()
        p1 = Project(name="P-Pune", customer_id=cust.id, branch_id=pune.id, status="Active", billing_frequency="Monthly")
        p2 = Project(name="P-Blr", customer_id=cust.id, branch_id=blr.id, status="Active", billing_frequency="Monthly")
        p3 = Project(name="P-None", customer_id=cust.id, status="Active", billing_frequency="Monthly")
        s.add_all([p1, p2, p3]); s.flush()

        def inv(num, proj, amt, day):
            i = Invoice(invoice_number=num, project_id=proj.id, invoice_date=date(2026, 9, day),
                        sub_total=amt, tax_amount=0, grand_total=amt, paid_amount=0, balance_amount=amt,
                        payment_status="Unpaid")
            s.add(i); s.flush()
            return i
        a = inv("INV-P", p1, 100_000, 1)
        inv("INV-B", p2, 50_000, 2)
        inv("INV-N", p3, 20_000, 3)
        s.add(InvoicePayment(invoice_id=a.id, payment_date=date(2026, 9, 20), amount=Decimal("40000")))
        a.paid_amount = 40_000; a.balance_amount = 60_000
        s.add(CustomerReceipt(customer_id=cust.id, branch_id=blr.id, received_date=date(2026, 9, 25),
                              amount=Decimal("5000"), unallocated_amount=Decimal("5000"),
                              tds_total=Decimal("0"), payment_mode="NEFT"))
        s.commit()
        yield s, cust, pune, blr


def test_pure_rules():
    assert branch_matches(3, None) and branch_matches(3, 3) and not branch_matches(4, 3)
    assert branch_matches(None, 0) and not branch_matches(3, 0)
    rows = ledger_branch_summary([
        {"branch_id": 1, "type": "Invoice", "debit": Decimal(10), "credit": Decimal(0)},
        {"branch_id": None, "type": "Payment", "debit": Decimal(0), "credit": Decimal(4)},
    ], {1: "Pune", 2: "Bengaluru"})
    assert [r["branch_name"] for r in rows] == ["Bengaluru", "Pune", "No branch on record"]
    assert rows[1]["balance"] == 10 and rows[2]["balance"] == -4
    scoped = BranchProjects([5, 6]); scoped.branch_id = 2
    assert project_in(5, scoped) and not project_in(7, scoped) and project_in(7, None)
    assert receipt_branch_scope(scoped) == 2 and receipt_branch_scope(5) is None
    assert project_clause is not None


def test_ledger_is_branch_wise(book):
    s, cust, pune, blr = book
    all_ = customer_ledger(customer_id=cust.id, db=s, user=FIN)["data"]
    refs = {e["ref"]: e for e in all_["entries"]}
    assert refs["INV-P"]["branch_name"] == "Pune" and refs["INV-N"]["branch_id"] is None
    # Money on account is a credit on the statement, under the branch that paid.
    on_acc = [e for e in all_["entries"] if e["type"] == "On account"]
    assert len(on_acc) == 1 and on_acc[0]["credit"] == 5000 and on_acc[0]["branch_name"] == "Bengaluru"
    assert all_["closing_balance"] == 170_000 - 40_000 - 5_000
    summary = {b["branch_name"]: b for b in all_["branches"]}
    assert summary["Pune"]["balance"] == 60_000 and summary["Bengaluru"]["balance"] == 45_000
    assert summary["No branch on record"]["balance"] == 20_000

    pune_only = customer_ledger(customer_id=cust.id, branch_id=pune.id, db=s, user=FIN)["data"]
    assert [e["type"] for e in pune_only["entries"]] == ["Invoice", "Payment"]
    assert pune_only["closing_balance"] == 60_000 and pune_only["branch_name"] == "Pune"
    none_only = customer_ledger(customer_id=cust.id, branch_id=0, db=s, user=FIN)["data"]
    assert [e["ref"] for e in none_only["entries"]] == ["INV-N"]
    with pytest.raises(HTTPException):
        customer_ledger(customer_id=cust.id, branch_id=9999, db=s, user=FIN)


def test_receivables_split_by_branch(book):
    s, cust, pune, blr = book
    data = receivables(as_of=TODAY, db=s, user=FIN)["data"]
    row = data["customers"][0]
    by = {b["branch_name"]: b["outstanding"] for b in row["branches"]}
    assert by == {"Pune": 60_000, "Bengaluru": 50_000, "No branch on record": 20_000}
    only = receivables(as_of=TODAY, customer_id=cust.id, branch_id=blr.id, db=s, user=FIN)["data"]
    assert only["totals"]["outstanding"] == 50_000
    assert [i["invoice_number"] for i in only["customers"][0]["open_invoices"]] == ["INV-B"]


def test_revenue_report_by_branch(book):
    s, cust, pune, blr = book
    full = revenue_report(s, "2026-09", today=TODAY)
    assert full["headline"]["billed"] == 170_000
    assert {b["name"] for b in full["filters"]["options"]["branches"]} == {"Pune", "Bengaluru"}
    pune_r = revenue_report(s, "2026-09", today=TODAY, customer_id=cust.id, branch_id=pune.id)
    assert pune_r["headline"]["billed"] == 100_000 and pune_r["headline"]["collected"] == 40_000
    assert pune_r["filters"]["branch_id"] == pune.id and pune_r["targets"]["scoped"]
    # The branch's money on account counts as collected for that branch.
    blr_r = revenue_report(s, "2026-09", today=TODAY, branch_id=blr.id)
    assert blr_r["headline"]["billed"] == 50_000 and blr_r["headline"]["collected"] == 5_000
    none_r = revenue_report(s, "2026-09", today=TODAY, branch_id=0)
    assert none_r["headline"]["billed"] == 20_000
    # A branch with no projects reads zero, never "everything".
    empty = CustomerBranch(customer_id=cust.id, branch_name="Chennai"); s.add(empty); s.commit()
    assert scope_projects(s, None, None, empty.id) == (-1,)
    assert revenue_report(s, "2026-09", today=TODAY, branch_id=empty.id)["headline"]["billed"] == 0


def test_finance_tab_and_drill_pages_follow_the_branch(book):
    from services.ceo_drilldown import drilldown
    from services.executive_dashboard import executive_dashboard
    s, cust, pune, blr = book
    tab = executive_dashboard(s, "finance", "2026-09", "month", today=TODAY, branch_id=pune.id)["data"]
    assert tab["headline"]["billed"] == 100_000
    assert sum(m["billed"] for m in tab["monthly"]) == 100_000
    billed = drilldown(s, "billed", "2026-09", "month", today=TODAY, branch_id=blr.id)
    assert [r["invoice"] for r in billed["tables"][0]["rows"]] == ["INV-B"]
    assert billed["filters"]["branch_id"] == blr.id
