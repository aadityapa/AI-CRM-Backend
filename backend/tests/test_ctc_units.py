"""CTC units: one salary, five spellings, one stored scale (2 Sep 2026).

Bug report: a candidate showed Current CTC ₹1,00,00,00,000 on the Applicants
tab. CTC is stored in RUPEES but several inputs are labelled "(Lac)" and
multiply by 100,000 — a rupee figure reaching one of those is 10^5 too big,
and nothing rejected the result.

Run:  cd backend && python -m pytest tests/test_ctc_units.py -q
"""
from __future__ import annotations

import pytest

from services.ctc import MAX_CTC_RUPEES, ctc_looks_wrong, parse_ctc_to_rupees

LAKH = 100_000


@pytest.mark.parametrize("text", [
    "12 LPA", "12lpa", "12 lakh", "12 Lakhs", "12L", "₹12L", "12",
    "12,00,000", "1200000", "1,200,000", "12.0 LPA",
])
def test_every_spelling_of_twelve_lakh_lands_on_the_same_rupees(text):
    assert parse_ctc_to_rupees(text) == 12 * LAKH


def test_crore_scales():
    assert parse_ctc_to_rupees("1.2 Cr") == 12_000_000
    assert parse_ctc_to_rupees("1 crore") == 10_000_000


def test_thousands_scale():
    assert parse_ctc_to_rupees("800k") == 800_000


def test_bare_small_number_reads_as_lakhs_not_rupees():
    """A CV writing "8" means 8 LPA. Storing ₹8 is the quiet half of this bug."""
    assert parse_ctc_to_rupees("8") == 8 * LAKH
    assert parse_ctc_to_rupees(8) == 8 * LAKH


def test_bare_large_number_is_already_rupees():
    assert parse_ctc_to_rupees("950000") == 950_000


def test_unreadable_and_empty_are_none():
    for v in (None, "", "   ", "negotiable", "as per company norms", 0, -5):
        assert parse_ctc_to_rupees(v) is None


def test_absurd_values_are_refused_rather_than_stored():
    """The reported row: 100000 typed into a Lac field = ₹1,00,00,00,000."""
    assert parse_ctc_to_rupees(100_000 * LAKH) is None
    assert parse_ctc_to_rupees("10000000000") is None


# --- the write-time guard --------------------------------------------------

def test_guard_flags_the_reported_value_with_a_useful_message():
    reason = ctc_looks_wrong(10_000_000_000)
    assert reason is not None
    assert "not a realistic CTC" in reason
    assert "Lac" in reason           # names the likely unit slip


def test_guard_passes_normal_salaries():
    for ok in (0, 300_000, 1_200_000, 25_00_000, 45_000_000):
        assert ctc_looks_wrong(ok) is None


def test_guard_rejects_negative():
    assert ctc_looks_wrong(-1) == "cannot be negative"


def test_guard_boundary_is_inclusive():
    assert ctc_looks_wrong(MAX_CTC_RUPEES) is None
    assert ctc_looks_wrong(MAX_CTC_RUPEES + 1) is not None


def test_guard_ignores_none():
    assert ctc_looks_wrong(None) is None
