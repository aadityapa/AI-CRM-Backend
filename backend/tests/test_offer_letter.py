"""Offer letter renderers (4 Sep 2026, user request).

Pure-function tests: the context is hand-built, so no database is needed.
What is pinned: both formats render, carry the same facts, the Indian
number formatting and the words rule, and the shared wording never diverges
between PDF and Word.

Run:  cd backend && python -m pytest tests/test_offer_letter.py -q
"""
from __future__ import annotations

import io
import zipfile

import pytest

from services.offer_letter import (  # noqa: E402
    _fmt_inr, amount_in_words, clean_overrides, effective_letter, offer_letter_facts,
    offer_letter_filename, offer_letter_paragraphs, render_offer_letter_docx,
    render_offer_letter_pdf,
)


@pytest.fixture
def ctx():
    return {
        "reference": "KRX/OL/2026/0142", "letter_date": "04 September 2026",
        "company_name": "Karnex Software Solutions PVT LTD",
        "company_address": "Baner, Pune, MH, India - 411045",
        "company_phone": "+91 1234 5678", "company_email": "info@karnex.in",
        "company_website": "https://karnex.in/", "company_cin": "U72900RJ2018PTC638288",
        "candidate_name": "Swati Dadasaheb Naik", "candidate_address": "", "candidate_city": "Pune",
        "candidate_email": "swati@example.com", "candidate_phone": "+91 98765 43210",
        "designation": "Engineer-I", "department": "Engineering",
        "role_title": "Functional Safety Manager", "client_name": "APTIV-ASUX",
        "opportunity_ref": "OPP-2026-008", "work_location": "Bangalore",
        "joining_date": "01 June 2026", "ctc_annual": _fmt_inr(1795549.73),
        "ctc_words": amount_in_words(1795549.73), "rate_line": "Rs. 1,49,629 per month",
        "offer_valid_until": "15 September 2026", "official_email": "swati@karnex.in",
        "relocation": True, "total_experience": "6.6",
        "signatory_name": "Vishal Harjani", "signatory_title": "Human Resources",
    }


def test_indian_grouping_and_words():
    assert _fmt_inr(1795549.73) == "Rs. 17,95,549.73"
    assert _fmt_inr(1800000) == "Rs. 18,00,000"
    assert _fmt_inr(999) == "Rs. 999"
    assert amount_in_words(1795549.73) == (
        "Seventeen Lakh Ninety Five Thousand Five Hundred Fifty Rupees Only")  # rounded to the rupee
    assert amount_in_words(12000000) == "One Crore Twenty Lakh Rupees Only"
    assert amount_in_words(0) == "Zero Rupees Only"


def test_wording_carries_every_fact(ctx):
    text = " ".join(offer_letter_paragraphs(ctx))
    for needle in ("Swati Dadasaheb Naik", "Engineer-I", "Engineering", "APTIV-ASUX",
                   "01 June 2026", "Bangalore", "Rs. 17,95,549.73", "Seventeen Lakh",
                   "Relocation", "15 September 2026"):
        assert needle in text, needle
    facts = dict(offer_letter_facts(ctx))
    assert facts["Official email"] == "swati@karnex.in"
    assert facts["Client / Project"] == "APTIV-ASUX"


def test_optional_lines_drop_out_when_blank(ctx):
    ctx.update(relocation=False, offer_valid_until="—", client_name="", official_email="")
    text = " ".join(offer_letter_paragraphs(ctx))
    assert "Relocation" not in text and "valid until" not in text and "engagement with" not in text
    keys = [k for k, _ in offer_letter_facts(ctx)]
    assert "Client / Project" not in keys and "Official email" not in keys


def test_pdf_renders(ctx):
    pdf = render_offer_letter_pdf(ctx)
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000


def test_docx_renders_with_the_same_wording(ctx):
    blob = render_offer_letter_docx(ctx)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    for needle in ("Swati Dadasaheb Naik", "Engineer-I", "Rs. 17,95,549.73", "Vishal Harjani",
                   "Offer of Employment"):
        assert needle in xml, needle


def test_filename_is_safe(ctx):
    ctx["candidate_name"] = "A/B: C"
    assert offer_letter_filename(ctx, "pdf") == "Offer_Letter_A_B__C.pdf"


# ------------------------------------------------------------ HR edits (4 Sep 2026)

def test_overrides_are_whitelisted_and_bounded():
    assert clean_overrides(None) is None
    assert clean_overrides({"fields": {}, "paragraphs": []}) is None
    ov = clean_overrides({"fields": {"designation": " Senior Engineer ", "company_name": "Evil Corp", "ctc_annual": 5},
                          "paragraphs": ["Dear X,", "   ", 42, "Body."]})
    assert ov == {"fields": {"designation": "Senior Engineer"}, "paragraphs": ["Dear X,", "Body."]}


def test_field_edit_reflows_the_default_wording(ctx):
    c, paras = effective_letter(ctx, {"fields": {"designation": "Senior Engineer"}})
    assert c["designation"] == "Senior Engineer"
    assert "Senior Engineer" in paras[1] and "Engineer-I" not in " ".join(paras)
    assert dict(offer_letter_facts(c))["Designation"] == "Senior Engineer"


def test_saved_paragraphs_replace_the_body_and_survive_markup(ctx):
    c, paras = effective_letter(ctx, {"paragraphs": ["Dear <b>Swati</b> & team,", "Custom body."]})
    assert paras == ["Dear <b>Swati</b> & team,", "Custom body."]
    pdf = render_offer_letter_pdf(c, paras)
    assert pdf[:5] == b"%PDF-"
    blob = render_offer_letter_docx(c, paras)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    assert "Custom body." in xml and "With reference to your application" not in xml
