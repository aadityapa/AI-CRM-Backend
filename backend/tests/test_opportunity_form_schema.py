"""Server-side schema-parity tests for the type-driven Opportunity form.

Mirrors the mission's "TESTS THAT MUST PASS":
  - each type allows exactly its own fields,
  - every non-T&M type has Project Scope,
  - the server rejects a payload with fields not valid for the chosen type,
  - hidden fields are stripped, not persisted.

Run:  python -m pytest tests/test_opportunity_form_schema.py -q
"""
import pytest

from services import opportunity_form_schema as ofs


def test_project_scope_for_every_non_tm_type():
    assert ofs.has_project_scope("Work_Package") is True
    assert ofs.has_project_scope("Fixed_Price") is True
    assert ofs.has_project_scope("Retainer") is True
    assert ofs.has_project_scope("T&M") is False


def test_time_and_material_block_only_for_tm():
    assert ofs.has_time_and_material("T&M") is True
    for t in ("Work_Package", "Fixed_Price", "Retainer"):
        assert ofs.has_time_and_material(t) is False


def test_retainer_rejects_time_and_material_fields():
    with pytest.raises(ofs.OpportunitySchemaError) as exc:
        ofs.validate_details({"tm_position_title": "Backend Dev"}, "Retainer")
    assert "tm_position_title" in str(exc.value)


def test_tm_rejects_project_scope():
    # Time & Material has no Project Scope.
    with pytest.raises(ofs.OpportunitySchemaError):
        ofs.validate_details({"project_scope": "<p>scope</p>"}, "T&M")


def test_work_package_accepts_project_scope_but_not_tm_fields():
    ofs.validate_details({"project_scope": "<p>scope</p>"}, "Work_Package")  # ok
    with pytest.raises(ofs.OpportunitySchemaError):
        ofs.validate_details({"holidays": 10.0}, "Work_Package")  # T&M-only field


def test_fixed_price_accepts_duration_but_other_types_reject_it():
    ofs.validate_details({"project_duration_months": 6}, "Fixed_Price")
    for opportunity_type in ("T&M", "Work_Package", "Retainer"):
        with pytest.raises(ofs.OpportunitySchemaError):
            ofs.validate_details({"project_duration_months": 6}, opportunity_type)


def test_tm_accepts_its_full_block():
    details = {
        "tm_position_title": "SRE", "tm_positions_count": 2, "tm_work_location": "Pune",
        "holidays_billable": True, "holidays": 10.0, "weekoff": 104.0, "leave": 24.0,
        "billing_type": "Per Hour", "actual_billing_days": 227.0,
        "customer_type": "Direct", "contact_email": "a@b.com",
    }
    ofs.validate_details(details, "T&M")  # must not raise


def test_auto_filled_mirrors_allowed_for_all_types():
    mirrors = {
        "customer_type": "Direct",
        "contact_email": "x@y.com",
        "contact_phone": "+918767998766",
        "hiring_manager_email": "hm@y.com",
        "hiring_manager_contact": "+918767897654",
    }
    for t in ofs.OPPORTUNITY_TYPES:
        ofs.validate_details(mirrors, t)  # never rejected


def test_strip_details_drops_invalid_keys():
    payload = {"project_scope": "<p>s</p>", "tm_position_title": "X", "customer_type": "Direct"}
    kept = ofs.strip_details(payload, "Work_Package")
    assert "project_scope" in kept and "customer_type" in kept
    assert "tm_position_title" not in kept  # T&M-only field stripped for Work Package


def test_unknown_type_raises():
    with pytest.raises(ofs.OpportunitySchemaError):
        ofs.allowed_detail_keys("Consulting")


def test_human_labels_are_accepted():
    # Accept "Time & Material" / "Work Package" aliases as well as the enum values.
    assert ofs.has_time_and_material("Time & Material") is True
    assert ofs.has_project_scope("Work Package") is True


def test_imported_opportunity_details_survive_an_edit():
    """8 Sep 2026 (user report): editing a Zoho-imported T&M opportunity 422'd
    because the importer's bookkeeping keys sat on `details` and the update
    path validates the MERGED stored+payload object. Those keys are valid for
    every type and must survive strip_details (they carry the Zoho ids)."""
    from services.opportunity_form_schema import strip_details, validate_details
    details = {
        "tm_position_title": "Dev", "zoho_opportunity_id": "123", "zoho_position_id": "456",
        "imported_from": "zoho-active-2026-09", "in_sales_active": True, "in_ta_active": False,
        "ta_status": "Open", "onboarded_count": 0, "customer_jd_attachment_name": "jd.docx",
    }
    for t in ("T&M", "Work_Package", "Fixed_Price", "Retainer"):
        validate_details({k: v for k, v in details.items() if k != "tm_position_title"}, t)
    validate_details(details, "T&M")
    kept = strip_details(details, "Retainer")
    assert kept["zoho_opportunity_id"] == "123" and "tm_position_title" not in kept
