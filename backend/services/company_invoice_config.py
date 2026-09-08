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
        "tagline": setting("invoice.seller_tagline"),
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
