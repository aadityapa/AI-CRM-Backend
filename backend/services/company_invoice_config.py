"""Seller / company + bank details for Tax Invoice rendering.

Since Aug 2026 these resolve through Org Settings (Settings → Organisation →
Tax Invoice), so an office move, a new GSTIN or a bank change is a form edit,
not a code deploy. Resolution order per field, courtesy of
`services.org_settings.setting`: **DB row → INVOICE_* env var → code default**
— existing env-based deployments keep working unchanged.
"""
from __future__ import annotations

from services.org_settings import setting


def get_seller_details() -> dict:
    """Company (seller) block embedded in GET /api/invoices/{id} detail."""
    return {
        "name": setting("invoice.seller_name"),
        "tagline": "",   # removed from the invoice (11 Sep 2026, user decision)
        "address_line1": setting("invoice.seller_address_line1"),
        "address_line2": setting("invoice.seller_address_line2"),
        "city": setting("invoice.seller_city"),
        "state": setting("invoice.seller_state"),
        "state_code": setting("invoice.seller_state_code"),
        "pincode": setting("invoice.seller_pincode"),
        "country": setting("invoice.seller_country"),
        "phone": setting("invoice.seller_phone"),
        "email": setting("invoice.seller_email"),
        "contact_email": setting("invoice.seller_contact_email"),
        "website": setting("invoice.seller_website"),
        "gstin": setting("invoice.seller_gstin"),
        "pan": setting("invoice.seller_pan"),
        "cin": setting("invoice.seller_cin"),
        "logo_url": setting("invoice.seller_logo_url"),
        "seal_url": setting("invoice.seller_seal_url"),
        "declaration": setting("invoice.seller_declaration"),
        # 11 Sep 2026: service line defaults + footer (website only, clickable).
        "sac_code": setting("invoice.sac_code"),
        "service_description": setting("invoice.service_description"),
        "signatory_line": setting("invoice.signatory_line"),
        "footer_website_url": setting("invoice.footer_website_url"),
        "footer_text": setting("invoice.footer_text"),
    }


def get_bank_details() -> dict:
    """Receivable bank account shown on the Tax Invoice."""
    return {
        "bank_name": setting("invoice.bank_name"),
        "account_name": setting("invoice.bank_account_name"),
        "account_number": setting("invoice.bank_account_number"),
        "ifsc": setting("invoice.bank_ifsc"),
        "branch": setting("invoice.bank_branch"),
        "account_type": setting("invoice.bank_account_type"),
    }


def _account_dict(row) -> dict:
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
        "source": "customer",
    }


def resolve_bank_details(db, customer_id: int | None) -> dict:
    """The ONE bank account printed on a customer's invoice (11 Sep 2026).

    Precedence: the account Sales picked for the customer (Leave & Holiday
    Billing step) → the default company account → the `invoice.bank_*`
    settings. Never raises — an invoice must always carry some account.
    """
    try:
        if db is not None:
            from sqlalchemy import select
            from models import CompanyBankAccount, CustomerBillingPolicy
            if customer_id:
                pol = db.execute(select(CustomerBillingPolicy)
                                 .where(CustomerBillingPolicy.customer_id == customer_id)).scalar_one_or_none()
                acc_id = getattr(pol, "bank_account_id", None) if pol else None
                if acc_id:
                    row = db.get(CompanyBankAccount, acc_id)
                    if row is not None:
                        return _account_dict(row)
            row = db.execute(select(CompanyBankAccount)
                             .where(CompanyBankAccount.is_default.is_(True),
                                    CompanyBankAccount.is_active.is_(True))).scalars().first()
            if row is not None:
                d = _account_dict(row)
                d["source"] = "default"
                return d
    except Exception:
        pass
    d = get_bank_details()
    d["source"] = "settings"
    return d
