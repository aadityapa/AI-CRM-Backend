"""Pydantic schemas for CRM master data + app settings."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, field_validator


def _required_str(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise ValueError("must not be empty")
    return s


# ---------------------------------------------------------------- departments
class DepartmentCreate(BaseModel):
    name: str
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class DepartmentUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class DepartmentOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    is_active: bool


# ---------------------------------------------------------------- designations
class DesignationCreate(BaseModel):
    name: str
    department_id: int | None = None
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class DesignationUpdate(BaseModel):
    name: str | None = None
    department_id: int | None = None
    is_active: bool | None = None


class DesignationOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    department_id: int | None = None
    is_active: bool


# ---------------------------------------------------------------- skills
class SkillCreate(BaseModel):
    name: str
    category: str | None = None
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class SkillUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    is_active: bool | None = None


class SkillOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    category: str | None = None
    is_active: bool


# ---------------------------------------------------------------- locations
class LocationCreate(BaseModel):
    city: str
    state: str | None = None
    country: str = "India"

    _city = field_validator("city")(_required_str)
    _country = field_validator("country")(_required_str)


class LocationUpdate(BaseModel):
    city: str | None = None
    state: str | None = None
    country: str | None = None


class LocationOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    city: str
    state: str | None = None
    country: str


# ---------------------------------------------------------------- currencies
class CurrencyCreate(BaseModel):
    code: str
    name: str
    symbol: str

    _code = field_validator("code")(_required_str)
    _name = field_validator("name")(_required_str)
    _symbol = field_validator("symbol")(_required_str)


class CurrencyUpdate(BaseModel):
    code: str | None = None
    name: str | None = None
    symbol: str | None = None


class CurrencyOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    code: str
    name: str
    symbol: str


# ---------------------------------------------------------------- document types
class DocumentTypeCreate(BaseModel):
    name: str
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class DocumentTypeUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class DocumentTypeOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    is_active: bool


# ---------------------------------------------------------------- contact roles
class ContactRoleCreate(BaseModel):
    name: str
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class ContactRoleUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None


class ContactRoleOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    is_active: bool


# ---------------------------------------------------------------- leave policy types
class LeavePolicyTypeCreate(BaseModel):
    name: str
    accrual_rule: str | None = None
    carry_forward_rule: str | None = None

    _name = field_validator("name")(_required_str)


class LeavePolicyTypeUpdate(BaseModel):
    name: str | None = None
    accrual_rule: str | None = None
    carry_forward_rule: str | None = None


class LeavePolicyTypeOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    accrual_rule: str | None = None
    carry_forward_rule: str | None = None


# ---------------------------------------------------------------- financial years
class FinancialYearCreate(BaseModel):
    name: str
    start_date: date
    end_date: date
    is_active: bool = True

    _name = field_validator("name")(_required_str)


class FinancialYearUpdate(BaseModel):
    name: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_active: bool | None = None


class FinancialYearOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    start_date: date
    end_date: date
    is_active: bool


# ---------------------------------------------------------------- calendar years
class CalendarYearCreate(BaseModel):
    year: int
    is_active: bool = True


class CalendarYearUpdate(BaseModel):
    year: int | None = None
    is_active: bool | None = None


class CalendarYearOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    year: int
    is_active: bool


# ---------------------------------------------------------------- tax rates
def _valid_tax_type(v: str) -> str:
    s = (v or "").strip().upper()
    if s not in ("GST", "TDS"):
        raise ValueError("tax_type must be 'GST' or 'TDS'")
    return s


class TaxRateCreate(BaseModel):
    name: str
    tax_type: str
    rate: Decimal
    is_active: bool = True

    _name = field_validator("name")(_required_str)
    _tax_type = field_validator("tax_type")(_valid_tax_type)


class TaxRateUpdate(BaseModel):
    name: str | None = None
    tax_type: str | None = None
    rate: Decimal | None = None
    is_active: bool | None = None

    @field_validator("tax_type")
    @classmethod
    def _tax_type_opt(cls, v):
        return None if v is None else _valid_tax_type(v)


class TaxRateOut(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    name: str
    tax_type: str
    rate: float
    is_active: bool


# ---------------------------------------------------------------- app settings
class SettingValueIn(BaseModel):
    value: str
    description: str | None = None


class SettingOut(BaseModel):
    model_config = {"from_attributes": True}
    key: str
    value: str
    description: str | None = None
