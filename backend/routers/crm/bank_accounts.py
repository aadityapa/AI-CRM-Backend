"""Karnex receivable bank accounts (11 Sep 2026, user request).

Admin/CEO manage the list under Settings ▸ Invoice. Every CRM role can READ
it (Sales picks one for a customer on the Leave & Holiday Billing step) —
the printed account is public on every invoice anyway.

  GET    /api/bank-accounts            list (?include_inactive=1)
  POST   /api/bank-accounts            create (Admin/CEO)
  PUT    /api/bank-accounts/{id}       update (Admin/CEO)
  POST   /api/bank-accounts/{id}/default   make default (Admin/CEO)
  DELETE /api/bank-accounts/{id}       deactivate; hard delete only when unused
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db, role_required
from models import CompanyBankAccount, CustomerBillingPolicy
from schemas.common import envelope

router = APIRouter(prefix="/api/bank-accounts", tags=["CRM: Bank accounts"])

admin_only = role_required()  # Admin / CEO

ACCOUNT_TYPES = ["Current", "Savings", "OD", "CC", "Other"]


class BankAccountIn(BaseModel):
    label: str = Field(min_length=2, max_length=120)
    bank_name: str = Field(min_length=2, max_length=120)
    account_name: str = Field(min_length=2, max_length=255)
    account_number: str = Field(min_length=4, max_length=40)
    ifsc: str = Field(min_length=4, max_length=20)
    branch: str | None = Field(default=None, max_length=160)
    account_type: str | None = Field(default=None, max_length=40)
    swift_code: str | None = Field(default=None, max_length=20)
    micr_code: str | None = Field(default=None, max_length=20)
    upi_id: str | None = Field(default=None, max_length=120)
    bank_address: str | None = Field(default=None, max_length=255)
    is_default: bool = False
    is_active: bool = True


def serialize_bank_account(row: CompanyBankAccount) -> dict:
    return {
        "id": row.id,
        "label": row.label,
        "bank_name": row.bank_name,
        "account_name": row.account_name,
        "account_number": row.account_number,
        "ifsc": row.ifsc,
        "branch": row.branch,
        "account_type": row.account_type,
        "swift_code": row.swift_code,
        "micr_code": row.micr_code,
        "upi_id": row.upi_id,
        "bank_address": row.bank_address,
        "is_default": bool(row.is_default),
        "is_active": bool(row.is_active),
    }


def _clean(payload: BankAccountIn) -> dict:
    data = payload.model_dump()
    for k in ("account_number", "ifsc", "swift_code", "micr_code"):
        if data.get(k):
            data[k] = str(data[k]).replace(" ", "").upper()
    return data


def _get_or_404(db: Session, account_id: int) -> CompanyBankAccount:
    row = db.get(CompanyBankAccount, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Bank account not found")
    return row


def _clear_default(db: Session, keep_id: int | None) -> None:
    for other in db.execute(select(CompanyBankAccount).where(CompanyBankAccount.is_default.is_(True))).scalars():
        if other.id != keep_id:
            other.is_default = False


@router.get("")
def list_bank_accounts(include_inactive: bool = False,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(any_crm_role)):
    stmt = select(CompanyBankAccount).order_by(CompanyBankAccount.is_default.desc(), CompanyBankAccount.label)
    if not include_inactive:
        stmt = stmt.where(CompanyBankAccount.is_active.is_(True))
    rows = db.execute(stmt).scalars().all()
    return envelope(data=[serialize_bank_account(r) for r in rows],
                    meta={"account_types": ACCOUNT_TYPES})


@router.post("")
def create_bank_account(payload: BankAccountIn, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    data = _clean(payload)
    # The first account is the default whether or not the form said so.
    has_any = db.execute(select(func.count(CompanyBankAccount.id))).scalar() or 0
    if not has_any:
        data["is_default"] = True
    row = CompanyBankAccount(**data)
    db.add(row)
    db.flush()
    if row.is_default:
        _clear_default(db, row.id)
    db.commit()
    db.refresh(row)
    return envelope(data=serialize_bank_account(row), message="Bank account added")


@router.put("/{account_id}")
def update_bank_account(account_id: int, payload: BankAccountIn,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    row = _get_or_404(db, account_id)
    for k, v in _clean(payload).items():
        setattr(row, k, v)
    if row.is_default:
        _clear_default(db, row.id)
        row.is_active = True
    db.commit()
    db.refresh(row)
    return envelope(data=serialize_bank_account(row), message="Bank account updated")


@router.post("/{account_id}/default")
def make_default(account_id: int, db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(admin_only)):
    row = _get_or_404(db, account_id)
    row.is_default = True
    row.is_active = True
    _clear_default(db, row.id)
    db.commit()
    return envelope(data=serialize_bank_account(row), message=f"{row.label} is now the default account")


@router.delete("/{account_id}")
def delete_bank_account(account_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    row = _get_or_404(db, account_id)
    used = db.execute(select(func.count(CustomerBillingPolicy.id))
                      .where(CustomerBillingPolicy.bank_account_id == row.id)).scalar() or 0
    if used or row.is_default:
        # Referenced by customers (or the default) → deactivate, never delete:
        # already-generated invoices keep printing a real account.
        row.is_active = False
        db.commit()
        return envelope(data=serialize_bank_account(row),
                        message=f"Deactivated — {used} customer(s) still point at it" if used
                        else "Deactivated (default account is never deleted)")
    db.delete(row)
    db.commit()
    return envelope(message="Bank account deleted")
