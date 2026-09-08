"""Regression tests for the ATS accuracy fixes.

Written from a real case: a hardware engineer scored 24/100 here while ChatGPT
scored the same resume+JD at 84. Each test below pins one of the causes so it
cannot come back.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ats_scoring import (  # noqa: E402
    _experience_points, _skill_present, _stem, _word_match,
    extract_jd_keywords, looks_like_job_description, looks_like_resume,
    score_resume_against_requirement,
)

JD = """Job Title: Embedded Electrical Hardware Engineer
Location: Bengaluru, India. Client: Aptiv / TCI
Qualification: B.E/B.Tech minimum degree. Experience: 5-7 years
- Design and review high-speed hardware for Automotive camera Products
- Work independently on Micro-Controller design tasks and lead development
- Own schematic and PCB layout, DDR4 and LPDDR5 memory interfaces
- Validate designs with oscilloscopes and function generators
- Ensure Functional Safety compliance to ISO 26262
- SOC bring-up, high speed SDIO, PCIe, USB3.0, MIPI"""

RESUME = """Bibin P S - Embedded Hardware Design Engineer. 4.9 years of experience.
B.Tech in Electronics. Based in Bengaluru.
High-speed digital interfaces: DDR4, LPDDR5, PCIe, USB3.0, MIPI, SDIO.
SoC based board design with Qualcomm and Ambarella processors.
Micro-controller and processor based product development.
Schematic design, OrCAD, Allegro, multilayer PCB design and Gerber review.
Board bring-up, debugging, DVT and environmental testing.
Lab equipment: Oscilloscope, Logic Analyzer, Signal Generator, Bench Power Supply.
Power supply design with LDOs and switching regulators."""

MANDATORY = [
    "Functional Safety", "Embedded", "Micro-Controller design",
    "oscilloscopes function generators", "power supplies",
]


# --------------------------------------------------------------- word matching

def test_skill_at_end_of_sentence_matches():
    """`.` is a skill char (Node.js), which made sentence-final skills invisible."""
    assert _word_match("Python", "Strong experience in Python.")
    assert _word_match("Generator", "Lab: Oscilloscope, Signal Generator.")


def test_dotted_tokens_still_match_whole():
    assert _word_match("Node.js", "Built services with Node.js and Express")
    assert _word_match("ASP.NET", "ASP.NET Core background")


def test_dotted_token_is_not_matched_by_its_prefix():
    """"Node" must not match inside "Node.js" — that would over-credit."""
    assert not _word_match("Node", "Built services with Node.js")


def test_hyphen_is_a_word_boundary():
    """"high-speed interfaces" does contain "speed"."""
    assert _word_match("speed", "High-Speed Interfaces: DDR4, LPDDR5")
    assert _word_match("high", "High-Speed Interfaces")
    assert _word_match("functional", "cross-functional collaboration")


def test_hyphenated_term_still_needs_its_hyphen():
    """A term written with a hyphen must still match literally."""
    assert _word_match("Micro-Controller", "Micro-Controller design work")
    assert not _word_match("Micro-Controller", "Microprocessors: CV28, QCS5430")


def test_plus_suffixed_tokens_are_not_split():
    assert not _word_match("C", "Strong C++ and embedded skills")


# --------------------------------------------------------------------- stemmer

def test_stemmer_does_not_over_strip():
    assert _stem("oscilloscopes") == "oscilloscope"   # was "oscilloscop"
    assert _stem("generators") == "generator"
    assert _stem("supplies") == "supply"
    assert _stem("batteries") == "battery"
    assert _stem("boxes") == "box"


def test_stemmer_leaves_non_plurals_alone():
    assert _stem("process") == "process"
    assert _stem("class") == "class"


# -------------------------------------------------------------- skill matching

def test_plural_skill_matches_singular_resume():
    assert _skill_present("power supplies", RESUME)[0]
    assert _skill_present("oscilloscopes function generators", RESUME)[0]


def test_hyphenated_skill_is_not_split_apart():
    assert _skill_present("Micro-Controller design", RESUME)[0]


def test_genuinely_absent_skill_still_reported_missing():
    """The fix must not turn into "match everything"."""
    assert not _skill_present("Functional Safety", RESUME)[0]
    assert not _skill_present("Kubernetes", RESUME)[0]


# ---------------------------------------------------------------- JD keywords

def test_jd_boilerplate_is_not_scored():
    kw = {k.lower() for k in extract_jd_keywords(JD)}
    for noise in ("title", "qualification", "location", "degree", "minimum",
                  "independently", "tasks", "lead", "customer", "development",
                  "products", "bengaluru"):
        assert noise not in kw, f"{noise!r} is JD boilerplate, not a skill"


def test_jd_keywords_keep_the_technical_terms():
    kw = {k.lower() for k in extract_jd_keywords(JD)}
    for real in ("ddr4", "lpddr5", "usb3.0", "pcie", "mipi"):
        assert real in kw, f"{real!r} is a genuine requirement and must be scored"


def test_jd_tokens_have_punctuation_stripped():
    kw = extract_jd_keywords("Interface with customer on specifications.")
    assert not any(k.endswith(".") for k in kw)


# ----------------------------------------------------------------- experience

def test_experience_just_below_band_is_not_zero():
    """4.9 against a 5-7 band was scoring 0/15 — the biggest single distortion."""
    assert _experience_points(4.9, 5, 7, 15) > 13
    assert _experience_points(5.0, 5, 7, 15) == 15


def test_experience_far_below_band_still_zero():
    assert _experience_points(2.0, 5, 7, 15) == 0.0


def test_overqualified_penalised_less_than_underqualified():
    over = _experience_points(9.0, 5, 7, 15)     # 2 years over
    under = _experience_points(3.0, 5, 7, 15)    # 2 years under
    assert over > under


def test_unknown_experience_earns_nothing():
    assert _experience_points(None, 5, 7, 15) == 0.0


# ------------------------------------------------------------------ end to end

def test_real_case_scores_realistically():
    """The whole point: this candidate is a good match and must not read as 24."""
    result = score_resume_against_requirement(
        RESUME, MANDATORY, [], 5, 7, "Bengaluru", jd_text=JD,
    )
    score = result["ats_score"]
    assert score > 65, f"expected a realistic score, got {score}"
    details = result["breakdown"]["score_details"]
    assert details["mandatory_matched"] == 4      # all but Functional Safety
    assert "Functional Safety" in result["breakdown"]["skills_missing"]


# -------------------------------------------------- the JD-as-resume trap
# A JD scored against its own requirement matches every skill and every JD
# keyword and comes back near 100 — which reads as an outstanding candidate.
# This is how a 4.9-year consumer-camera engineer showed as 91/100 with
# "7 yrs" experience and "Functional Safety" matched, none of which is true.

def test_a_job_description_is_rejected_as_a_resume():
    accepted, signals = looks_like_resume(JD)
    assert not accepted, "a job description must never be accepted as a resume"
    assert signals["looks_like_job_description"]
    assert signals["jd_markers"]


def test_a_real_resume_is_still_accepted():
    accepted, signals = looks_like_resume(
        "Bibin P S | Hardware Design Engineer\nKannur, Kerala\n"
        "Phone: 7909109365\nEmail: bibinps12345@gmail.com\n" + RESUME
    )
    assert accepted
    assert not signals["looks_like_job_description"]


def test_resume_without_contact_details_still_accepted():
    """Some CVs are anonymised by the agency — sections alone must still pass."""
    accepted, _ = looks_like_resume(
        "PROFESSIONAL SUMMARY\nHardware engineer, 4.9 years.\n"
        "WORK EXPERIENCE\nVVDN Technologies, board bring-up and PCB review, "
        "schematic design, DDR4 and LPDDR5 interfaces.\n"
        "EDUCATION\nB.Tech Electronics, Vimal Jyothi Engineering College."
    )
    assert accepted


def test_jd_detector_needs_more_than_one_stray_word():
    """"experience" appears in every CV — one weak hit must not flag a JD."""
    is_jd, _ = looks_like_job_description(
        "Senior engineer with 6 years of experience in embedded systems."
    )
    assert not is_jd


def test_jd_scored_against_itself_would_be_near_perfect():
    """Documents WHY the gate matters — this is the score the gate prevents."""
    result = score_resume_against_requirement(
        JD, MANDATORY, [], 5, 7, "Bengaluru", jd_text=JD,
    )
    assert result["ats_score"] > 85          # real case measured 95 keyword-only
    # The tell-tale in the real report: experience read off the JD's own
    # "5-7 years" requirement, so the candidate appeared to have 7.
    assert result["breakdown"]["score_details"]["detected_experience_years"] == 7.0


def test_unrelated_resume_still_scores_low():
    """Guard against the fixes simply inflating everything."""
    unrelated = (
        "Priya R - Chartered Accountant with 6 years in statutory audit, "
        "GST filing, Tally ERP and financial reporting. M.Com."
    )
    result = score_resume_against_requirement(
        unrelated, MANDATORY, [], 5, 7, "Bengaluru", jd_text=JD,
    )
    assert result["ats_score"] < 40, (
        f"an unrelated resume must not score well, got {result['ats_score']}"
    )
