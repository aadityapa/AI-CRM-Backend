"""Adversarial + unit tests for the honest ATS scorer (services/ats_scoring.py).

Pure functions, no DB — run with:  python -m pytest tests/test_ats_scoring.py -q
"""
import pytest

from services.ats_scoring import (
    AtsConfigError,
    detect_experience_years,
    looks_like_resume,
    score_resume_against_requirement,
)

MANDATORY = ["Python", "FastAPI", "PostgreSQL"]
OPTIONAL = ["AWS", "Docker"]

PERFECT_RESUME = """
Jane Smith
jane.smith@example.com | +1 555 987 6543
Summary: Backend engineer with 6 years of experience building APIs.
Skills: Python, FastAPI, PostgreSQL, AWS, Docker, Redis
Experience: 6 years developing services in Python and FastAPI on PostgreSQL and AWS.
Education: B.Tech in Computer Science, Example University
"""

PARTIAL_RESUME = """
John Doe
john.doe@example.com | +1 555 123 4567
Summary: Backend developer, 5 years of experience.
Skills: Python, FastAPI, PostgreSQL, AWS
Experience: Built REST services in Python and FastAPI backed by PostgreSQL.
Education: B.Sc Computer Science
"""

UNRELATED_RESUME = """
Maria Garcia
maria.garcia@example.com | +1 555 222 3333
Summary: Registered nurse with 8 years of clinical experience.
Skills: Patient care, Phlebotomy, Triage, EHR charting
Experience: 8 years in emergency nursing.
Education: B.Sc Nursing, Example University
"""

RESTAURANT_MENU = """
STARTERS
Garlic bread 4.50
Bruschetta 5.00
MAINS
Margherita pizza 9.00
Spaghetti carbonara 11.50
DESSERTS
Tiramisu 6.00
"""

BLANK = "   \n  \n  "
KEYWORD_STUFF = "Python FastAPI PostgreSQL AWS Docker Kubernetes React Node"


# --------------------------------------------------------------- gate tests
def test_blank_document_is_not_a_resume():
    ok, _ = looks_like_resume(BLANK)
    assert ok is False


def test_restaurant_menu_is_not_a_resume():
    ok, sig = looks_like_resume(RESTAURANT_MENU)
    assert ok is False  # no email/phone, no résumé sections


def test_real_resume_is_a_resume():
    ok, sig = looks_like_resume(PERFECT_RESUME)
    assert ok is True
    assert sig["has_email"] is True


# --------------------------------------------------- configuration / 0-0 guard
def test_empty_requirement_is_a_config_error_not_100():
    """No skills AND no JD = nothing to score against → config error."""
    with pytest.raises(AtsConfigError):
        score_resume_against_requirement(PERFECT_RESUME, mandatory_skills=[])


# ------------------------------------------- skills optional when a JD exists
JD_TEXT = """
We are hiring a backend engineer. Must have strong Python and FastAPI
experience, comfortable with PostgreSQL, Docker and AWS deployment.
Responsibilities include building REST APIs and CI/CD pipelines.
"""


def test_no_skills_but_jd_scores_without_error():
    """Skills are optional (Aug 2026): TA can scan the moment RMG attaches a
    JD, even before any Skill Evaluation Details are entered."""
    r = score_resume_against_requirement(
        PERFECT_RESUME, mandatory_skills=[], jd_text=JD_TEXT,
    )
    assert r["ats_score"] > 0
    assert r["breakdown"]["score_details"]["jd_applied"] is True
    assert r["breakdown"]["score_details"]["mandatory_total"] == 0


def test_no_skills_score_is_capped_below_100():
    """Without required skills there is no 'full required match', so even a
    perfect JD overlap must not read as a perfect 100."""
    r = score_resume_against_requirement(
        PERFECT_RESUME, mandatory_skills=[], jd_text=JD_TEXT,
    )
    assert r["ats_score"] <= 99.0
    assert r["breakdown"]["score_details"]["all_required_matched"] is False


def test_optional_skills_alone_also_score():
    r = score_resume_against_requirement(
        PERFECT_RESUME, mandatory_skills=[], optional_skills=["Python", "Docker"],
    )
    assert 0 < r["ats_score"] <= 99.0


def test_no_zero_division_ever():
    # even a totally empty document against real skills scores a number, not a crash
    r = score_resume_against_requirement("nothing relevant here at all", MANDATORY, OPTIONAL, 4, 8)
    assert 0 <= r["ats_score"] <= 100


# ------------------------------------------------------------- scoring tests
def test_perfect_resume_scores_high_with_evidence():
    r = score_resume_against_requirement(PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8)
    assert r["ats_score"] >= 95
    assert r["breakdown"]["score_details"]["all_required_matched"] is True
    # every matched skill must carry evidence
    for s in r["breakdown"]["skills_matched"]:
        assert r["breakdown"]["evidence"].get(s), f"no evidence snippet for {s}"


def test_partial_resume_is_below_100():
    r = score_resume_against_requirement(PARTIAL_RESUME, MANDATORY, OPTIONAL, 4, 8)
    # Docker missing (optional) -> cannot be a perfect 100
    assert r["ats_score"] < 100
    assert "Docker" in r["breakdown"]["skills_missing"]


def test_unrelated_field_resume_scores_low_and_lists_missing():
    r = score_resume_against_requirement(UNRELATED_RESUME, MANDATORY, OPTIONAL, 4, 8)
    assert r["ats_score"] <= 40
    assert set(MANDATORY).issubset(set(r["breakdown"]["skills_missing"]))
    assert r["breakdown"]["score_details"]["mandatory_matched"] == 0


def test_100_only_when_all_required_matched():
    # requirement with only mandatory skills, all present -> 100 is legitimate
    r = score_resume_against_requirement("Python FastAPI PostgreSQL B.Tech", MANDATORY)
    assert r["breakdown"]["score_details"]["all_required_matched"] is True
    # requirement with a missing mandatory skill -> capped below 100
    r2 = score_resume_against_requirement("Python FastAPI B.Tech", MANDATORY)
    assert r2["ats_score"] < 100


def test_keyword_stuffing_flagged():
    r = score_resume_against_requirement(KEYWORD_STUFF, MANDATORY, OPTIONAL)
    assert r["breakdown"]["keyword_stuffing_suspected"] is True


def test_determinism():
    a = score_resume_against_requirement(PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8)
    b = score_resume_against_requirement(PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8)
    assert a["ats_score"] == b["ats_score"]


def test_empty_criteria_do_not_inflate():
    """The old bug: no optional/experience/location => free points => ~100.
    Now those criteria are simply excluded from the denominator."""
    only_mandatory_half = score_resume_against_requirement("Python FastAPI B.Tech", MANDATORY)
    # PostgreSQL missing -> 2/3 mandatory. earned=50*2/3 + edu 10 = 43.33; possible=60 -> 72.2
    assert only_mandatory_half["ats_score"] < 80
    assert only_mandatory_half["ats_score"] > 0


def test_no_jd_falls_back_to_skills_only():
    r = score_resume_against_requirement(PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8)
    assert r["breakdown"]["score_details"].get("jd_applied") is False
    assert "jd_keywords_matched" not in r["breakdown"]


def test_jd_match_boosts_resume_aligned_with_jd():
    jd = (
        "We need a backend engineer skilled in Python FastAPI PostgreSQL AWS Docker Redis "
        "and microservices architecture for API development."
    )
    without = score_resume_against_requirement(PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8)
    with_jd = score_resume_against_requirement(
        PERFECT_RESUME, MANDATORY, OPTIONAL, 4, 8, jd_text=jd,
    )
    assert with_jd["breakdown"]["score_details"]["jd_applied"] is True
    assert with_jd["breakdown"]["score_details"]["jd_keywords_matched"] > 0
    assert with_jd["breakdown"]["score_details"]["earned_points"] >= without["breakdown"]["score_details"]["earned_points"]
    nurse = score_resume_against_requirement(UNRELATED_RESUME, MANDATORY, OPTIONAL, 4, 8, jd_text=jd)
    assert with_jd["ats_score"] > nurse["ats_score"]


# ------------------------------------------- symbol-ending skills (C++, C#, .NET)
SYMBOL_RESUME = """
Alex Lee
alex.lee@example.com | +1 555 111 2222
Skills: C++, C#, .NET, STL
Experience: 5 years building systems in C++ and C#.
Education: B.Tech
"""


def test_symbol_skills_match():
    r = score_resume_against_requirement(SYMBOL_RESUME, ["C++"])
    assert "C++" in r["breakdown"]["skills_matched"]
    assert "C++" not in r["breakdown"]["skills_missing"]


# --------------------------------------------------------- skill alias matching
def test_skill_alias_react_matches_reactjs():
    resume = (
        "Sam Roy\nsam.roy@example.com | +1 555 000 1111\n"
        "Skills: ReactJS, JavaScript, K8s\nExperience: 4 years.\nEducation: B.E"
    )
    r = score_resume_against_requirement(resume, ["React"])
    assert "React" in r["breakdown"]["skills_matched"]


# --------------------------------------------------- improved experience parsing
def test_experience_from_date_range():
    assert detect_experience_years("Software Engineer, Acme 2018 - 2023.") == 5.0


def test_experience_prefers_stated_total():
    assert detect_experience_years("Backend engineer with 7 years of experience.") == 7.0


# ----------------------------------------------------- per-requirement weighting
def test_custom_weights_apply_and_change_score():
    base = score_resume_against_requirement(PARTIAL_RESUME, MANDATORY, OPTIONAL, 4, 8)
    heavy = score_resume_against_requirement(
        PARTIAL_RESUME, MANDATORY, OPTIONAL, 4, 8, weights={"experience": 40},
    )
    assert heavy["breakdown"]["score_details"]["weights"]["experience"] == 40
    assert heavy["breakdown"]["score_details"]["weights_customized"] is True
    assert heavy["ats_score"] != base["ats_score"]


def test_bad_weights_are_ignored_not_fatal():
    r = score_resume_against_requirement(
        PARTIAL_RESUME, MANDATORY, weights={"experience": -5, "bogus": 99},
    )
    # negative/unknown keys ignored → experience stays at its default 15
    assert r["breakdown"]["score_details"]["weights"]["experience"] == 15
