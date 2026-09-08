"""Organisation settings: DB-first configuration with env + code fallbacks.

The rule that makes this safe to sprinkle anywhere: **a lookup can never fail
and can never be slow**. Values come from the `app_settings` table when a row
exists, else the historical environment variable, else the code default — so
an empty table, a dead database or an un-migrated install all behave exactly
like before this module existed.

Reads are cached for a short TTL because several consumers (the email From
header, link building) run on hot paths without a session of their own; a
60-second staleness window after an admin edit is an acceptable price for
never opening a session per email header. The admin PUT endpoint calls
`invalidate()` so the editing admin sees their change take effect immediately.
"""
from __future__ import annotations

import logging
import os
import threading
import time

logger = logging.getLogger("karnex.org_settings")

#: setting key -> (env fallback, code default)
KEYS: dict[str, tuple[str, str]] = {
    "org.company_name": ("COMPANY_NAME", "Karnex Software Solutions PVT LTD"),
    "org.company_short_name": ("COMPANY_SHORT_NAME", "Karnex"),
    "org.company_phone": ("COMPANY_PHONE", ""),
    "org.company_email": ("COMPANY_EMAIL", "info@karnex.in"),
    "org.company_website": ("COMPANY_WEBSITE", "https://karnex.in/"),
    "email.public_base_url": ("PUBLIC_BASE_URL", ""),
    "email.sender_domains": ("EMAIL_SENDER_DOMAINS", ""),
    "interview.default_duration": ("INTERVIEW_DEFAULT_DURATION", ""),
    "interview.autosend": ("AI_INTERVIEW_AUTOSEND", "false"),
    # RMG screening gate (25 Aug 2026): TA applies → RMG clears → AI L1 unlocks.
    # Off = no gating, no Pending stamping, no RMG notifications — lets an org
    # roll the workflow out (or back) without a deploy.
    "hiring.rmg_screening_gate": ("RMG_SCREENING_GATE", "true"),
    # Hours a profile may sit in RMG Pending before the daily SLA nudge fires.
    "scheduler.rmg_screening_sla_hours": ("RMG_SCREENING_SLA_HOURS", "48"),
    # Daily background jobs (services/scheduler.py). Each is admin-editable so
    # a noisy job can be turned off without a deploy.
    "scheduler.enabled": ("SCHEDULER_ENABLED", "true"),
    "scheduler.run_hour": ("SCHEDULER_RUN_HOUR", "8"),
    "scheduler.timesheet_reminders": ("", "true"),
    "scheduler.rmg_screening_sla": ("", "true"),
    "scheduler.timesheet_reminder_days": ("", "1,3,5,7"),
    "scheduler.po_expiry": ("", "true"),
    "scheduler.po_expiry_days": ("", "45,30,15,5,1,0"),
    "scheduler.po_expiry_overdue_days": ("", "1,7,14,30,60,90"),
    "scheduler.recurring_invoices": ("", "true"),
    "scheduler.pe_leave_credit": ("", "true"),
    "scheduler.pe_leave_credit_lookback": ("", "12"),
    # Finance — an office move or a bank change must not be a code deploy.
    "finance.tds_rate_percent": ("TDS_RATE_PERCENT", "10"),
    # Seller (company) block on the Tax Invoice. DB row -> INVOICE_* env -> default.
    "invoice.seller_name": ("INVOICE_SELLER_NAME", "KARNEX SOFTWARE SOLUTIONS PRIVATE LIMITED"),
    "invoice.seller_tagline": ("INVOICE_SELLER_TAGLINE", "Excellence In Motion"),
    "invoice.seller_address_line1": (
        "INVOICE_SELLER_ADDRESS_LINE1",
        "103, Pride Purple Accord, Opp- RMZ Icon, Near Nanakbawdi Flyover, Baner"),
    "invoice.seller_address_line2": ("INVOICE_SELLER_ADDRESS_LINE2", "Pune, MH, India - 411045"),
    "invoice.seller_city": ("INVOICE_SELLER_CITY", "Pune"),
    "invoice.seller_state": ("INVOICE_SELLER_STATE", "Maharashtra"),
    "invoice.seller_state_code": ("INVOICE_SELLER_STATE_CODE", "27"),
    "invoice.seller_pincode": ("INVOICE_SELLER_PINCODE", "411045"),
    "invoice.seller_country": ("INVOICE_SELLER_COUNTRY", "India"),
    "invoice.seller_phone": ("INVOICE_SELLER_PHONE", "+91 1234 5678"),
    "invoice.seller_email": ("INVOICE_SELLER_EMAIL", "karnex.singh@karnex.in"),
    "invoice.seller_contact_email": ("INVOICE_SELLER_CONTACT_EMAIL", "info@karnex.in"),
    "invoice.seller_website": ("INVOICE_SELLER_WEBSITE", "www.karnex.in"),
    "invoice.seller_gstin": ("INVOICE_SELLER_GSTIN", "27AAJCK2474BA1ZL"),
    "invoice.seller_pan": ("INVOICE_SELLER_PAN", "AAJCK2474BA"),
    "invoice.seller_cin": ("INVOICE_SELLER_CIN", "U72900RJ2018PTC638288"),
    "invoice.seller_logo_url": ("INVOICE_SELLER_LOGO_URL", "/admin/assets/karnex-logo-invoice.png"),
    "invoice.seller_seal_url": ("INVOICE_SELLER_SEAL_URL", "/admin/assets/karnex-seal-sign.png"),
    "invoice.seller_declaration": (
        "INVOICE_SELLER_DECLARATION",
        "We declare that this invoice shows the actual price of the "
        "goods described and that all particulars are true and correct."),
    # Receivable bank account on the Tax Invoice.
    "invoice.bank_name": ("INVOICE_BANK_NAME", "HDFC Bank"),
    "invoice.bank_account_name": (
        "INVOICE_BANK_ACCOUNT_NAME", "KARNEX SOFTWARE SOLUTIONS PRIVATE LIMITED"),
    "invoice.bank_account_number": ("INVOICE_BANK_ACCOUNT_NUMBER", "50200075368143"),
    "invoice.bank_ifsc": ("INVOICE_BANK_IFSC", "HDFC0001784"),
    "invoice.bank_branch": ("INVOICE_BANK_BRANCH", "Baner, Pune"),
    "invoice.bank_account_type": ("INVOICE_BANK_ACCOUNT_TYPE", "Current"),
    # "Scan to view" QR on the Tax Invoice (3 Sep 2026). Where the QR sends the
    # phone. Blank = this app's own public invoice page. To use the hosted
    # Karnex Invoice Viewer (Vercel) instead, set its URL with placeholders:
    #   {view_url}  this app's public HTML page   {pdf_url}  public PDF
    #   {data_url}  public invoice JSON            {token}    signed token
    #   {invoice_number} {id}
    # e.g. https://karnex-invoice-viewer.vercel.app/?src={data_url}
    "invoice.qr_viewer_url": ("INVOICE_QR_VIEWER_URL", ""),
}

_TTL_SECONDS = 60.0
_lock = threading.Lock()
_cache: dict[str, str] = {}
_cache_at: float = 0.0


def invalidate() -> None:
    """Drop the cache — called by the admin PUT so edits apply immediately."""
    global _cache_at
    with _lock:
        _cache_at = 0.0


def _load_rows() -> dict[str, str]:
    """One short-lived session, one SELECT, never raises."""
    try:
        from sqlalchemy import text

        from crm_db import get_session_factory

        session = get_session_factory()()
        try:
            rows = session.execute(
                text("SELECT key, value FROM app_settings WHERE key = ANY(:keys)"),
                {"keys": list(KEYS)},
            ).all()
            return {k: (v if v is not None else "") for k, v in rows}
        finally:
            session.close()
    except Exception as exc:
        logger.debug("org_settings load failed (falling back to env): %s", exc)
        return {}


def setting(key: str, default: str | None = None) -> str:
    """Effective value for one key: DB row -> env var -> code default."""
    global _cache, _cache_at
    env_key, code_default = KEYS.get(key, ("", ""))
    if default is None:
        default = code_default
    now = time.monotonic()
    with _lock:
        stale = now - _cache_at > _TTL_SECONDS
    if stale:
        try:
            rows = _load_rows()
        except Exception:  # belt over _load_rows' own braces: never raise here
            rows = {}
        with _lock:
            _cache = rows
            _cache_at = now
    with _lock:
        if key in _cache and str(_cache[key]).strip():
            return str(_cache[key]).strip()
    if env_key:
        env_val = (os.getenv(env_key) or "").strip()
        if env_val:
            return env_val
    return default


def setting_bool(key: str) -> bool:
    return setting(key).strip().lower() in ("1", "true", "yes", "on")


def effective(db) -> list[dict]:
    """All keys with value + provenance, for the admin screen. Uses the
    caller's session for the DB half so the admin always sees fresh rows."""
    from sqlalchemy import text

    try:
        rows = dict(db.execute(
            text("SELECT key, value FROM app_settings WHERE key = ANY(:keys)"),
            {"keys": list(KEYS)},
        ).all())
    except Exception:
        rows = {}
    out = []
    for key, (env_key, code_default) in KEYS.items():
        db_val = str(rows.get(key) or "").strip()
        env_val = (os.getenv(env_key) or "").strip() if env_key else ""
        value = db_val or env_val or code_default
        source = "settings" if db_val else ("environment" if env_val else "default")
        out.append({"key": key, "value": value, "source": source,
                    "env_key": env_key, "default": code_default})
    return out
