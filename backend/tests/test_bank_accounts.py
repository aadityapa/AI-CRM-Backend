"""Company bank accounts (11 Sep 2026): the ONE account printed on an invoice
is the customer's pick → default account → invoice.bank_* settings."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


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


from models import Base, CompanyBankAccount, Customer, CustomerBillingPolicy  # noqa: E402
from services.company_invoice_config import resolve_bank_details  # noqa: E402


def _session():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_bank_resolution_precedence():
    s = _session()
    c1 = Customer(name="Uno Minda"); c2 = Customer(name="Harman")
    s.add_all([c1, c2]); s.flush()
    hdfc = CompanyBankAccount(label="HDFC Baner", bank_name="HDFC Bank", account_name="KARNEX",
                              account_number="50200075368143", ifsc="HDFC0001784", is_default=True)
    icici = CompanyBankAccount(label="ICICI", bank_name="ICICI Bank", account_name="KARNEX",
                               account_number="000405001234", ifsc="ICIC0000004")
    s.add_all([hdfc, icici]); s.flush()
    s.add(CustomerBillingPolicy(customer_id=c1.id, bank_account_id=icici.id)); s.flush()

    picked = resolve_bank_details(s, c1.id)
    assert picked["account_number"] == "000405001234" and picked["source"] == "customer"
    default = resolve_bank_details(s, c2.id)
    assert default["account_number"] == "50200075368143" and default["source"] == "default"
    assert resolve_bank_details(s, None)["source"] == "default"


def test_settings_fallback_when_no_accounts():
    s = _session()
    d = resolve_bank_details(s, None)
    assert d["source"] == "settings" and d["account_number"]
