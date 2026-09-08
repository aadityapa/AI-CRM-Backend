"""CRM invoice GST — reuses tax_invoice engine (KARNEX CGST/SGST/IGST)."""
from __future__ import annotations

from services.finance import (
    GST_MISSING_NOTE,
    buyer_dict_from_branch,
    compute_karnex_gst,
    resolve_buyer_state_code,
)
from services import tax_invoice as ti


ITEMS_176x650 = [{"billing_hours": 176, "rate_per_hour": 650}]


def _round2(v: float) -> float:
    return round(float(v), 2)


def test_intra_state_27_cgst_sgst():
    gst = compute_karnex_gst(
        buyer={"state_code": "27", "gstn": "27ABCDE1234F1Z5"},
        items=ITEMS_176x650,
    )
    assert gst["subtotal"] == 114400.0
    assert gst["intra"] is True
    assert gst["buyer_state_code"] == "27"
    assert gst["cgst"] == 10296.0
    assert gst["sgst"] == 10296.0
    assert gst["igst"] == 0.0
    assert gst["total_gst"] == 20592.0
    assert gst["grand_total"] == 134992.0
    assert "note" not in gst or gst.get("note") in (None, "")


def test_inter_state_36_igst():
    gst = compute_karnex_gst(
        buyer={"state_code": "36", "gstn": ""},
        items=ITEMS_176x650,
    )
    assert gst["subtotal"] == 114400.0
    assert gst["intra"] is False
    assert gst["buyer_state_code"] == "36"
    assert gst["cgst"] == 0.0
    assert gst["sgst"] == 0.0
    assert gst["igst"] == 20592.0
    assert gst["total_gst"] == 20592.0
    assert gst["grand_total"] == 134992.0


def test_blank_state_gstn_prefix_drives_igst():
    buyer = {"state_code": "", "gstn": "36ABCDE1234F1Z5"}
    assert ti.buyer_state_code(buyer) == "36"
    gst = compute_karnex_gst(buyer=buyer, items=ITEMS_176x650)
    assert gst["buyer_state_code"] == "36"
    assert gst["intra"] is False
    assert gst["igst"] == 20592.0
    assert gst["cgst"] == 0.0
    assert gst["sgst"] == 0.0
    assert gst["total_gst"] == 20592.0
    assert gst["grand_total"] == 134992.0


def test_missing_state_and_gstin_zero_gst_with_note():
    gst = compute_karnex_gst(
        buyer={"state_code": "", "gstn": "", "gstin": ""},
        items=ITEMS_176x650,
    )
    assert gst["subtotal"] == 114400.0
    assert gst["intra"] is False
    assert gst["buyer_state_code"] == ""
    assert gst["cgst"] == 0.0
    assert gst["sgst"] == 0.0
    assert gst["igst"] == 0.0
    assert gst["total_gst"] == 0.0
    assert gst["grand_total"] == 114400.0
    assert gst["note"] == GST_MISSING_NOTE


def test_engine_functions_reused_not_duplicated():
    """Sanity: finance wrapper delegates state + totals to tax_invoice."""
    buyer = {"state_code": "27", "gstin": "27ABCDE1234F1Z5"}
    assert ti.buyer_state_code(buyer) == "27"
    assert ti.is_intra_state(buyer) is True
    totals = ti.compute_totals({"buyer": buyer, "items": ITEMS_176x650})
    gst = compute_karnex_gst(buyer=buyer, items=ITEMS_176x650)
    assert gst["cgst"] == totals.cgst
    assert gst["sgst"] == totals.sgst
    assert gst["igst"] == totals.igst
    assert gst["total_gst"] == totals.total_gst
    assert gst["grand_total"] == totals.total


def test_stored_total_mismatch_note():
    gst = compute_karnex_gst(
        buyer={"state_code": "27"},
        items=ITEMS_176x650,
        stored_tax=0.0,
        stored_grand=114400.0,
    )
    assert gst["total_gst"] == 20592.0
    assert "stored total differs" in (gst.get("note") or "")


def test_buyer_dict_from_branch_uses_gstin_field():
    class _Branch:
        state = "Maharashtra"
        gstin = "27ABCDE1234F1Z5"

    d = buyer_dict_from_branch(_Branch())  # type: ignore[arg-type]
    assert d["gstn"] == "27ABCDE1234F1Z5"
    assert d["gstin"] == "27ABCDE1234F1Z5"
    assert ti.buyer_state_code(d) == "27"


def test_subtotal_181_intra_27_display_rounded():
    gst = compute_karnex_gst(buyer={"state_code": "27"}, subtotal=181.0)
    assert gst["intra"] is True
    assert _round2(gst["cgst"]) == 16.29
    assert _round2(gst["sgst"]) == 16.29
    assert gst["igst"] == 0.0
    assert _round2(gst["total_gst"]) == 32.58
    assert _round2(gst["grand_total"]) == 213.58


def test_subtotal_181_inter_36_igst():
    gst = compute_karnex_gst(buyer={"state_code": "36"}, subtotal=181.0)
    assert gst["intra"] is False
    assert gst["cgst"] == 0.0
    assert gst["sgst"] == 0.0
    assert _round2(gst["igst"]) == 32.58
    assert _round2(gst["total_gst"]) == 32.58
    assert _round2(gst["grand_total"]) == 213.58


def test_invoice_override_beats_branch():
    class _Branch:
        state = "27"
        gstin = "27ABCDE1234F1Z5"

    branch = _Branch()
    code, src = resolve_buyer_state_code(override="36", branch=branch)  # type: ignore[arg-type]
    assert code == "36"
    assert src == "override"
    gst = compute_karnex_gst(
        branch=branch,  # type: ignore[arg-type]
        state_code_override="36",
        subtotal=181.0,
    )
    assert gst["buyer_state_code"] == "36"
    assert gst["buyer_state_source"] == "override"
    assert gst["intra"] is False
    assert _round2(gst["igst"]) == 32.58


def test_clearing_override_falls_back_to_branch():
    class _Branch:
        state = "27"
        gstin = "27ABCDE1234F1Z5"

    branch = _Branch()
    # Explicit blank override → branch state / GSTIN
    code, src = resolve_buyer_state_code(override=None, branch=branch)  # type: ignore[arg-type]
    assert code == "27"
    assert src == "branch"
    gst = compute_karnex_gst(
        branch=branch,  # type: ignore[arg-type]
        state_code_override=None,
        subtotal=181.0,
    )
    assert gst["buyer_state_code"] == "27"
    assert gst["buyer_state_source"] == "branch"
    assert gst["intra"] is True
    assert _round2(gst["total_gst"]) == 32.58


def test_branch_gstin_prefix_when_state_name_only():
    class _Branch:
        state = "Telangana"
        gstin = "36ABCDE1234F1Z5"

    code, src = resolve_buyer_state_code(branch=_Branch())  # type: ignore[arg-type]
    assert code == "36"
    assert src == "branch"
