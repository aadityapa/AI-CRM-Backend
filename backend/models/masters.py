"""Master data tables — admin-configurable, never hardcoded."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base


class Department(Base):
    __tablename__ = "departments"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class Designation(Base):
    __tablename__ = "designations"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False)
    department_id = sa.Column(sa.Integer, sa.ForeignKey("departments.id"), nullable=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    __table_args__ = (sa.UniqueConstraint("name", "department_id", name="uq_designation_name_dept"),)


class Skill(Base):
    __tablename__ = "skills"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    category = sa.Column(sa.String(120), nullable=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class Currency(Base):
    __tablename__ = "currencies"
    id = sa.Column(sa.Integer, primary_key=True)
    code = sa.Column(sa.String(8), nullable=False, unique=True)
    name = sa.Column(sa.String(64), nullable=False)
    symbol = sa.Column(sa.String(8), nullable=False)


class Location(Base):
    __tablename__ = "locations"
    id = sa.Column(sa.Integer, primary_key=True)
    city = sa.Column(sa.String(120), nullable=False)
    state = sa.Column(sa.String(120), nullable=True)
    country = sa.Column(sa.String(120), nullable=False, server_default="India")
    __table_args__ = (sa.UniqueConstraint("city", "state", "country", name="uq_location"),)


class DocumentType(Base):
    __tablename__ = "document_types"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class LeavePolicyType(Base):
    __tablename__ = "leave_policy_types"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    accrual_rule = sa.Column(sa.String(255), nullable=True)
    carry_forward_rule = sa.Column(sa.String(255), nullable=True)


class ContactRole(Base):
    """Branch / customer contact person role master (Finance, HR, …)."""

    __tablename__ = "contact_roles"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class FinancialYear(Base):
    """Indian financial year master, e.g. "FY 2026-27" (Apr 1 → Mar 31)."""

    __tablename__ = "financial_years"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(64), nullable=False, unique=True)
    start_date = sa.Column(sa.Date, nullable=False)
    end_date = sa.Column(sa.Date, nullable=False)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class CalendarYear(Base):
    __tablename__ = "calendar_years"
    id = sa.Column(sa.Integer, primary_key=True)
    year = sa.Column(sa.Integer, nullable=False, unique=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class TaxRate(Base):
    """Reference tax-rate master (GST slabs, TDS sections).

    NOTE: services/tax.py computation is deliberately NOT driven by this table
    (compliance layer stays decoupled) — this is a lookup master for the UI.
    """

    __tablename__ = "tax_rates"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)  # e.g. "GST 18%", "TDS 194J"
    tax_type = sa.Column(sa.String(8), nullable=False)             # "GST" | "TDS"
    rate = sa.Column(sa.Numeric(5, 2), nullable=False)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())


class AppSetting(Base):
    """Key/value app settings (e.g. ai_interview_pass_threshold, editable by Admin)."""

    __tablename__ = "app_settings"
    key = sa.Column(sa.String(120), primary_key=True)
    value = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.String(255), nullable=True)


class UserTablePreference(Base):
    """A user's saved layout for one list screen.

    `config` holds the whole layout so adding a customisable table needs no
    migration:

        {"columns": [{"key": "candidate_name", "visible": true}, ...],
         "sort":    [{"by": "customer", "dir": "asc"}, ...]}

    Column ORDER is the array order. Sort is a list because the UI offers
    Excel-style multi-level sort (first key wins, later keys break ties).
    """

    __tablename__ = "user_table_preferences"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer,
                        sa.ForeignKey("registration_data.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    table_key = sa.Column(sa.String(64), nullable=False)
    # Plain '{}' literal, no ::jsonb cast: Postgres casts it implicitly for a
    # jsonb column, and the ::-cast syntax broke EVERY SQLite-backed test at
    # CREATE TABLE time (154 collection-level errors across the suite).
    config = sa.Column(JSONB, nullable=False, server_default=sa.text("'{}'"))
    created_at = sa.Column(sa.DateTime(timezone=True),
                           server_default=sa.func.now(), nullable=False)
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now(), nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("user_id", "table_key", name="uq_user_table_preference"),
    )
