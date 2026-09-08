"""Server-side mirror of the Opportunity form's type-driven schema.

The frontend schema lives in
`src/crm/pages/opportunity/opportunitySchema.ts`. This module is the SERVER's
copy of the same visibility rules so the API can enforce Part 3's rule:

    "The server validates against the SAME schema for the SAME type. A client
     that posts Time & Material fields for a Retainer is rejected. Never trust
     the client."

Type-specific fields are stored in the Opportunity's `details` JSONB column; the
CTC slab is a separate relational table. Core relational columns (customer_id,
title, opp_type, rfi_value, ...) are handled by the Pydantic model as before —
this module governs only the `details` object and the CTC slab, which is where
the type-conditional fields live.

Keep this in sync with the TS schema. Both derive the same key sets.
"""
from __future__ import annotations

OPPORTUNITY_TYPES = ("T&M", "Work_Package", "Fixed_Price", "Retainer")

# Auto-filled mirror fields (from the selected customer/contact) — recorded on the
# opportunity for audit; valid for every type.
_ALWAYS_DETAIL: frozenset[str] = frozenset({
    "customer_type", "contact_email", "contact_phone",
    "hiring_manager_email", "hiring_manager_contact",
    "sales_stage",  # Work Page "Stage" (e.g. Sales Validation)
})

# Importer provenance / bookkeeping keys (8 Sep 2026, user report). The Zoho
# importers (`tools/import_zoho_*`) stamp these on `details`; they are not form
# fields for ANY type. `update_opportunity` merges the STORED details with the
# edit payload before validating, so without this set every edit of an imported
# opportunity 422'd with "Fields not valid for opportunity type 'T&M':
# zoho_opportunity_id, …" — and `strip_details` would have thrown the Zoho ids
# away. Valid for every type, preserved on save, never rendered by the form.
_IMPORT_META: frozenset[str] = frozenset({
    "imported_from", "zoho_opportunity_id", "zoho_position_id", "zoho_rmg",
    "in_sales_active", "in_ta_active", "ta_status", "onboarded_count",
    "customer_jd_attachment_name", "industry", "internal_external", "publish_public",
})

# Type-specific detail keys (exactly the fields the TS schema shows for each type).
_TM_DETAIL: frozenset[str] = frozenset({
    # Time & Material Details
    "tm_position_title", "tm_positions_count", "tm_exp_min", "tm_exp_max",
    "tm_notice_period", "tm_closing_date", "tm_position_type", "tm_replacement_engineer",
    "tm_duration_months",
    "tm_jd_attachments", "tm_role", "tm_work_location",
    # legacy (removed from UI; still accepted so old drafts don't 422)
    "tm_wfo_remote",
    # Leave & Holiday Details (full for T&M only)
    "holidays_billable", "weekoff_billable", "leave_billable",
    "credit_leave_monthly", "leave_policy", "holidays", "weekoff", "leave",
    # Paid leaves the customer bills even when leave isn't billable (the
    # "APTIV rule"; frontend twin has carried it since 14 Aug 2026 — its
    # absence HERE made every T&M create with a prefilled branch policy 422).
    "paid_leaves",
    # Commercial Details extras (rfi_value is a core column, handled elsewhere)
    "billing_type", "hours_per_day", "actual_billing_days", "actual_billing_hours",
})

_PROJECT_SCOPE: frozenset[str] = frozenset({"project_scope"})
_FIXED_PRICE_DETAIL: frozenset[str] = _PROJECT_SCOPE | frozenset({"project_duration_months"})

_TYPE_DETAIL: dict[str, frozenset[str]] = {
    "T&M": _TM_DETAIL,
    "Work_Package": _PROJECT_SCOPE,   # header-only Leave/Holiday, RFI-only Commercial, + Project Scope
    "Fixed_Price": _FIXED_PRICE_DETAIL,
    "Retainer": _PROJECT_SCOPE,
}


class OpportunitySchemaError(ValueError):
    """Raised when a payload contains detail fields not valid for its type."""


def _normalize_type(opp_type: str) -> str:
    t = (opp_type or "").strip()
    # Accept the human labels too, just in case.
    alias = {
        "Time & Material": "T&M", "Time and Material": "T&M", "TM": "T&M",
        "Work Package": "Work_Package", "Fixed Price": "Fixed_Price",
    }
    t = alias.get(t, t)
    if t not in OPPORTUNITY_TYPES:
        raise OpportunitySchemaError(f"Unknown opportunity type: {opp_type!r}")
    return t


def allowed_detail_keys(opp_type: str) -> frozenset[str]:
    """Detail-object keys the given type is allowed to carry."""
    t = _normalize_type(opp_type)
    return _ALWAYS_DETAIL | _IMPORT_META | _TYPE_DETAIL[t]


def validate_details(details: dict | None, opp_type: str) -> None:
    """Raise OpportunitySchemaError if `details` has keys invalid for the type."""
    if not details:
        return
    allowed = allowed_detail_keys(opp_type)
    offending = sorted(k for k in details.keys() if k not in allowed)
    if offending:
        raise OpportunitySchemaError(
            f"Fields not valid for opportunity type {opp_type!r}: {', '.join(offending)}"
        )


def strip_details(details: dict | None, opp_type: str) -> dict:
    """Return only the detail keys valid for the type (defensive; never persist hidden fields)."""
    if not details:
        return {}
    allowed = allowed_detail_keys(opp_type)
    return {k: v for k, v in details.items() if k in allowed}


def has_project_scope(opp_type: str) -> bool:
    return "project_scope" in _TYPE_DETAIL[_normalize_type(opp_type)]


def has_time_and_material(opp_type: str) -> bool:
    return _normalize_type(opp_type) == "T&M"
