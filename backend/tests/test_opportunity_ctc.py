"""Candidate CTC Slab formula and validation tests."""
import pytest
from pydantic import ValidationError

from schemas.opportunities import OpportunityCtcSlabIn
from services.opportunity_ctc import (
    calculate_billing_bases,
    derive_ctc_row,
    normalize_tm_billing_details,
)


BASE = {
    "hours_per_day": 8,
    "holidays": 10,
    "weekoff": 104,
    "leave": 24,
    "holidays_billable": False,
    "weekoff_billable": False,
    "leave_billable": False,
}


def test_billing_bases_user_confirmed_scenarios():
    """BILLABLE = customer pays that day = stays IN the base (user-confirmed
    18 Aug 2026, four exact scenarios). Paid leaves add back capped at what
    leave deducted."""
    # Nothing billable, no paid leaves: 365 − 10 − 104 − 24 = 227.
    days, hours = calculate_billing_bases(BASE)
    assert float(days) == 227
    assert float(hours) == 1816

    # Nothing billable + 12 paid: 227 + 12 = 239.
    days, _ = calculate_billing_bases({**BASE, "paid_leaves": 12})
    assert float(days) == 239

    # Holidays + Weekoff billable, 24 leave, 12 paid: 365 − 24 + 12 = 353.
    days, _ = calculate_billing_bases({
        **BASE, "holidays_billable": True, "weekoff_billable": True,
        "paid_leaves": 12,
    })
    assert float(days) == 353

    # Everything billable: full 365 (and no paid add-back — leave deducted 0).
    days, _ = calculate_billing_bases({
        **BASE, "holidays_billable": True, "weekoff_billable": True,
        "leave_billable": True, "paid_leaves": 12,
    })
    assert float(days) == 365

    # Add-back cap: paid can never exceed what leave deducted.
    days, _ = calculate_billing_bases({**BASE, "paid_leaves": 99})
    assert float(days) == 251


@pytest.mark.parametrize(
    ("billing_type", "rate", "annual", "monthly"),
    [
        ("Per Hour", 100, 181600, 15133.33),
        ("Per Day", 100, 22700, 1891.67),
        ("Per Month", 10000, 120000, 10000),
        ("Per Year", 120000, 120000, 10000),
    ],
)
def test_all_billing_type_revenue_branches(billing_type, rate, annual, monthly):
    row = derive_ctc_row(
        {"rate": rate, "management_cost_pct": 30, "hike_pct": 10},
        opportunity_type="T&M",
        details={**BASE, "billing_type": billing_type},
    )
    assert row["revenue_annual"] == annual
    assert row["revenue_monthly"] == monthly
    assert row["engineering_budget"] == round(annual * 0.70, 2)
    assert row["approved_ctc_lac"] == round(annual * 0.70 / 1.10, 2)


def test_per_month_is_not_prorated_by_billing_days():
    row = derive_ctc_row(
        {"rate": 15000},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Month", "holidays": 300},
    )
    assert row["revenue_annual"] == 180000
    assert row["revenue_monthly"] == 15000


def test_blank_and_zero_edges_do_not_produce_nan_or_divide_by_zero():
    missing = derive_ctc_row(
        {}, opportunity_type="T&M", details={**BASE, "billing_type": "Per Hour"},
    )
    assert missing["revenue_annual"] is None
    assert missing["approved_ctc_lac"] is None

    no_hours = derive_ctc_row(
        {"rate": 100},
        opportunity_type="T&M",
        details={**BASE, "hours_per_day": "", "billing_type": "Per Hour"},
    )
    assert no_hours["revenue_annual"] is None

    zero = derive_ctc_row(
        {"rate": 0, "management_cost_pct": "", "hike_pct": ""},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Year"},
    )
    assert zero["revenue_annual"] == 0
    assert zero["approved_ctc_lac"] == 0

    guarded = derive_ctc_row(
        {"rate": 100000, "hike_pct": -100},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Year"},
    )
    assert guarded["approved_ctc_lac"] is None


@pytest.mark.parametrize(
    ("opportunity_type", "rate", "details", "annual"),
    [
        ("Work_Package", 120000, {}, 120000),
        ("Fixed_Price", 60000, {"project_duration_months": 6}, 120000),
        ("Retainer", 10000, {}, 120000),
    ],
)
def test_non_tm_types_ignore_hidden_billing_hours(opportunity_type, rate, details, annual):
    row = derive_ctc_row(
        {"rate": rate},
        opportunity_type=opportunity_type,
        details={
            **details,
            "billing_type": "Per Hour",
            "hours_per_day": 999,
            "actual_billing_hours": 999999,
        },
    )
    assert row["revenue_annual"] == annual
    assert row["revenue_monthly"] == 10000


def test_fixed_price_blank_and_zero_duration_edges():
    blank = derive_ctc_row(
        {"rate": 120000},
        opportunity_type="Fixed_Price",
        details={"project_duration_months": ""},
    )
    assert blank["revenue_annual"] == 120000

    zero = derive_ctc_row(
        {"rate": 120000},
        opportunity_type="Fixed_Price",
        details={"project_duration_months": 0},
    )
    assert zero["revenue_annual"] is None
    assert zero["approved_ctc_lac"] is None


def test_opportunity_type_switch_recalculates_instead_of_reusing_stale_annual():
    tm = derive_ctc_row(
        {"rate": 100},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    switched = derive_ctc_row(
        tm,
        opportunity_type="Work_Package",
        details={"actual_billing_hours": 1816},
    )
    assert tm["revenue_annual"] == 181600
    assert switched["revenue_annual"] == 100


def test_normalize_tm_details_applies_form_defaults():
    details = normalize_tm_billing_details({"billing_type": "Per Hour"})
    # No phantom holidays/weekoff/leave — blank deducts as 0 → 365 days × 8h.
    assert "holidays" not in details
    assert "weekoff" not in details
    assert "leave" not in details
    assert details["hours_per_day"] == 8
    assert details["actual_billing_days"] == 365
    assert details["actual_billing_hours"] == 2920


def test_exp_max_is_one_year_above_exp_min():
    """NEXUS parity (4 Sep 2026): each slab row is one year — 7→8, 8→9, 9→10
    regardless of the target."""
    row = derive_ctc_row(
        {"exp_min": 7, "target_exp": 10, "rate": 1407.5},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    assert row["exp_max"] == 8

    non_tm = derive_ctc_row(
        {"exp_min": 2, "target_exp": 6, "rate": 120000},
        opportunity_type="Work_Package",
        details={},
    )
    assert non_tm["exp_max"] == 3

    # Client-sent Exp Max is never trusted; a blank Exp Min leaves it blank.
    blank = derive_ctc_row(
        {"exp_min": "", "target_exp": 5, "exp_max": 9, "rate": 400},
        opportunity_type="T&M",
        details={**BASE, "billing_type": "Per Hour"},
    )
    assert blank["exp_max"] is None


def test_exp_min_cannot_exceed_exp_max():
    with pytest.raises(ValidationError, match="Exp Min"):
        OpportunityCtcSlabIn(exp_min=5, exp_max=3)


def test_appraisal_cycles_power_rule():
    """NEXUS-parity screenshot (14 Aug 2026): budget 143010 at hike 10% —
    5→7 = 1 cycle → 130009.09; 6→7 = 0 → full budget; 7→10 = 2 → 118190.08.
    Approved CTC = Budget / 1.1^cycles, cycles = Target − Exp Min − 1."""
    base = {"billing_type": "Per Year"}
    for exp_min, target, cycles, approved in [
        (5, 7, "1", 130009.09),
        (6, 7, "0", 143010.0),
        (7, 10, "2", 118190.08),
    ]:
        row = derive_ctc_row(
            {"rate": 204300, "management_cost_pct": 30, "hike_pct": 10,
             "exp_min": exp_min, "target_exp": target},
            opportunity_type="T&M", details=base,
        )
        assert row["appraisal_cycle"] == cycles
        assert row["engineering_budget"] == 143010.0
        assert row["approved_ctc_lac"] == approved
        # Exp Max is one year above Exp Min (4 Sep 2026), whatever the target.
        assert row["exp_max"] == exp_min + 1


def test_zoho_parity_56_pct_management_cost():
    """The user's side-by-side test (14 Aug 2026): annual 25,69,222.32 at
    Mgmt 56% → budget 11,30,457.82; band 3–5 gives targets 5 (cycles 1 →
    ÷1.1 = 10,27,688.93 ≈ Zoho's 1027689) and 5 (cycles 0 → full budget).
    Same for the 5–7 band at annual 37,40,796.56 → budget 16,45,950.49."""
    for exp_min, target, annual, budget, approved in [
        (3, 5, 2569222.32, 1130457.82, 1027688.93),
        (4, 5, 2569222.32, 1130457.82, 1130457.82),
        (5, 7, 3740796.56, 1645950.49, 1496318.63),
        (6, 7, 3740796.56, 1645950.49, 1645950.49),
    ]:
        row = derive_ctc_row(
            {"rate": annual, "management_cost_pct": 56, "hike_pct": 10,
             "exp_min": exp_min, "target_exp": target},
            opportunity_type="T&M", details={"billing_type": "Per Year"},
        )
        assert row["engineering_budget"] == budget
        assert row["approved_ctc_lac"] == approved
        assert row["appraisal_cycle"] == str(max(0, target - exp_min - 1))
