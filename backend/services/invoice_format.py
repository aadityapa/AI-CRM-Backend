"""Client-specific invoice format (23 Sep 2026) — pure, no I/O.

Some customers do not want every column of the service table. The GM
confirms, before a Proforma is raised, which OPTIONAL columns this
customer's invoice prints; the choice is frozen on the document
(`invoices.invoice_format`) and remembered on the customer's billing policy
(`customer_billing_policies.invoice_format`) so next month is pre-filled.

The three optional columns are the only ones a customer has ever asked to
drop. S.No, Description, the cost basis, Qty and Amount are the invoice —
they are not switchable. Every renderer (HTML/PDF, reportlab, Word, the
on-screen sheet) reads the same normalized dict, so a column can never be
hidden on one output and printed on another.
"""
from __future__ import annotations

#: key → label shown in the GM's "Invoice format" dialog.
OPTIONAL_INVOICE_COLUMNS: dict[str, str] = {
    "sac": "SAC Code",
    "leave": "Leave (Days)",
    "per_day": "Rate Per Day (Rate/Hour × Hours/Day)",
}

DEFAULT_INVOICE_FORMAT: dict[str, bool] = {key: True for key in OPTIONAL_INVOICE_COLUMNS}


def normalize_invoice_format(raw) -> dict[str, bool]:
    """Coerce whatever was stored/posted into the full {key: bool} map.

    Unknown keys are dropped, missing keys default to True — so a format
    saved before a fourth optional column exists keeps printing that column,
    which is the safe direction (nothing silently disappears from an invoice).
    """
    out = dict(DEFAULT_INVOICE_FORMAT)
    if isinstance(raw, dict):
        for key in OPTIONAL_INVOICE_COLUMNS:
            if key in raw:
                out[key] = bool(raw[key])
    return out


def is_default_format(fmt: dict[str, bool] | None) -> bool:
    return normalize_invoice_format(fmt) == DEFAULT_INVOICE_FORMAT


def format_summary(fmt: dict[str, bool] | None) -> str:
    """Human line for activity logs: 'Hides: Rate Per Day' / 'All columns'."""
    norm = normalize_invoice_format(fmt)
    hidden = [OPTIONAL_INVOICE_COLUMNS[k] for k, on in norm.items() if not on]
    return f"Hides: {', '.join(hidden)}" if hidden else "All columns"
