"""The Karnex JD built from the customer's JD (5 Nov 2026) — draft, parse,
PDF / Word download. Pure functions; no database."""
from __future__ import annotations

from services import karnex_jd as kj

CUSTOMER_JD = """HARMAN International — Android Auto / CarPlay Validation Engineer

About the Role
Harman is looking for a validation engineer for its infotainment programme.

Roles and Responsibilities:
• Validate Android Auto and Apple CarPlay projection on head units
• Write and run test cases; log defects in JIRA
1. Analyse logs (logcat, iAP2) and report root cause

Must Have Skills
- Android Auto, CarPlay
- Bluetooth / Wi-Fi connectivity testing

Good to have
- Python automation

Qualification: B.E / B.Tech in ECE or CS
"""


def facts():
    return kj.JdFacts(title="Android Auto CarPlay Validation", experience_min=3, experience_max=6,
                      location="Bangalore, Karnataka", work_mode="Onsite", openings=2,
                      must_skills=["Android Auto", "CarPlay"], nice_skills=["Python"])


def test_the_customer_jd_is_split_by_its_own_headings():
    s = kj.split_sections(CUSTOMER_JD)
    assert "Validate Android Auto and Apple CarPlay projection on head units" in s["responsibilities"]
    assert "Analyse logs (logcat, iAP2) and report root cause" in s["responsibilities"]   # "1." bullet stripped
    assert "Bluetooth / Wi-Fi connectivity testing" in s["must"]
    assert s["nice"] == ["Python automation"]
    assert s["education"] == ["B.E / B.Tech in ECE or CS"]                                 # heading with its value


def test_the_draft_is_the_karnex_layout_and_never_names_the_customer():
    text = kj.draft_karnex_jd(facts(), CUSTOMER_JD, ["Harman", "HARMAN International"])
    assert text.startswith("Job title: Android Auto CarPlay Validation\nExperience: 3–6 years")
    assert "Location: Bangalore, Karnataka" in text and "Openings: 2" in text
    order = [text.index(h) for h in ("About the role", "Key responsibilities", "Must-have skills",
                                     "Good-to-have skills", "Education")]
    assert order == sorted(order)
    assert "harman" not in text.lower() and "our client" in text
    # The position's skills lead, and a repeat of the customer's line is not printed twice.
    must = text.split("Must-have skills")[1].split("Good-to-have skills")[0]
    assert must.strip().startswith("- Android Auto\n- CarPlay")
    assert must.splitlines().count("- Android Auto") == 1


def test_a_customer_jd_with_no_headings_becomes_the_responsibilities():
    text = kj.draft_karnex_jd(kj.JdFacts(title="Tester"), "Test the head unit\nLog defects", [])
    assert "Key responsibilities\n- Test the head unit\n- Log defects" in text
    assert "About the role\nKarnex is hiring a Tester." in text


def test_parse_reads_facts_sections_and_bullets():
    doc = kj.parse_jd(kj.draft_karnex_jd(facts(), CUSTOMER_JD, ["Harman"]))
    assert ("Experience", "3–6 years") in doc["facts"]
    titles = [s["title"] for s in doc["sections"]]
    assert titles[:2] == ["About the role", "Key responsibilities"]
    resp = doc["sections"][1]
    assert len(resp["items"]) == 3 and resp["paras"] == []


def test_pdf_and_word_downloads_render():
    text = kj.draft_karnex_jd(facts(), CUSTOMER_JD, ["Harman"])
    pdf = kj.render_pdf(text, title="Android Auto CarPlay Validation", ref="C-2026-00095 · REQ-95")
    assert pdf[:4] == b"%PDF" and len(pdf) > 1500
    docx = kj.render_docx(text, title="Android Auto CarPlay Validation", ref="C-2026-00095")
    assert docx[:2] == b"PK"
    import io
    from docx import Document
    words = "\n".join(p.text for p in Document(io.BytesIO(docx)).paragraphs)
    assert "Key responsibilities" in words and "Validate Android Auto" in words
    assert kj.jd_filename("Android Auto / CarPlay", "REQ-1", "pdf") == "Karnex JD - Android Auto CarPlay.pdf"


def test_the_routes_are_gated():
    import inspect
    import routers.crm.requirements as rr
    src = inspect.getsource(rr)
    assert '"/{requirement_id}/karnex-jd/draft"' in src
    assert "user: CurrentUser = Depends(JD_EDIT_GATE)):\n    \"\"\"A Karnex JD drafted" in src
    assert '"/{requirement_id}/rmg-jd.pdf"' in src and '"/{requirement_id}/rmg-jd.docx"' in src
