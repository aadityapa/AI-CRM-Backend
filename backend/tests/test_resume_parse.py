"""Resume field extraction + duplicate detection (services/resume_parse.py).

The AI path is not tested here (no API key in tests) — parse_resume_text falls
back to the regex extractor, which is exactly the degraded mode we must pin:
a missing key must mean "less complete", never "broken".

Run:  cd backend && python -m pytest tests/test_resume_parse.py -q
"""
from __future__ import annotations

import importlib
import zipfile
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@compiles(JSONB, "sqlite")
def _j(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(ARRAY, "sqlite")
def _a(e, c, **k):  # noqa: ANN001
    return "JSON"


@compiles(UUID, "sqlite")
def _u(e, c, **k):  # noqa: ANN001
    return "VARCHAR(36)"


@compiles(INET, "sqlite")
def _i(e, c, **k):  # noqa: ANN001
    return "VARCHAR(64)"


for _m in [
    "base", "rbac", "customers", "opportunities", "projects", "leave", "timesheets",
    "finance", "hr", "candidates", "masters", "requirements", "profiles", "resumes",
    "ai_links", "scheduling", "user_profiles", "template_requests", "access_templates",
]:
    importlib.import_module(f"models.{_m}")

from models.base import Base  # noqa: E402
from models import Candidate  # noqa: E402
from services.resume_parse import (  # noqa: E402
    _regex_parse, extract_text_from_bytes, find_duplicate_candidate,
)

SAMPLE = (
    "Sivakumar Sayeeram\n"
    "siva8848@gmail.com\n"
    "+91-8838522496\n"
    "5 years experience in Embedded Hardware Design\n"
    "Bengaluru, Karnataka"
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    s = Session(bind=engine, future=True)
    try:
        yield s
    finally:
        s.close()


# --- regex fallback ----------------------------------------------------------

def test_regex_finds_the_contact_details():
    r = _regex_parse(SAMPLE)
    assert r["name"] == "Sivakumar Sayeeram"
    assert r["email"] == "siva8848@gmail.com"
    assert r["experience"] == "5.0"


def test_phone_does_not_swallow_the_next_line():
    """The digit class must not include newlines — "…496\\n5 years" once
    yielded a phone ending in an extra 5."""
    r = _regex_parse(SAMPLE)
    digits = "".join(ch for ch in r["phone"] if ch.isdigit())
    assert digits.endswith("8838522496")
    assert len(digits) == 12  # 91 + the 10-digit number, nothing more


def test_empty_text_gives_empty_fields_not_a_crash():
    r = _regex_parse("")
    assert r["email"] == "" and r["skills"] == []


def test_txt_bytes_roundtrip():
    assert "Sivakumar" in extract_text_from_bytes(SAMPLE.encode(), ".txt")


def test_unsupported_extension_returns_empty():
    assert extract_text_from_bytes(b"anything", ".exe") == ""


# --- duplicate rule ----------------------------------------------------------

def _cand(db, email="siva8848@gmail.com", phone="+91 8838522496"):
    c = Candidate(first_name="Sivakumar", last_name="Sayeeram", email=email, phone=phone)
    db.add(c)
    db.flush()
    return c


def test_email_match_is_case_insensitive(db):
    c = _cand(db)
    assert find_duplicate_candidate(db, "SIVA8848@GMAIL.COM", None).id == c.id


def test_phone_matches_on_last_ten_digits(db):
    c = _cand(db)
    assert find_duplicate_candidate(db, None, "08838522496").id == c.id
    assert find_duplicate_candidate(db, None, "+91-88385-22496").id == c.id


def test_import_placeholder_email_never_matches(db):
    _cand(db, email="priya.9f3c@import.karnex.in", phone=None)
    assert find_duplicate_candidate(db, "priya.9f3c@import.karnex.in", None) is None


def test_no_match_returns_none(db):
    _cand(db)
    assert find_duplicate_candidate(db, "someone.else@example.com", "9999999999") is None


def test_short_phone_fragments_do_not_match(db):
    """Fewer than 10 digits is too weak a signal to hold a resume on."""
    _cand(db)
    assert find_duplicate_candidate(db, None, "2496") is None


# --- soft name match ---------------------------------------------------------

def test_name_match_is_found_on_normalised_equality(db):
    from services.resume_parse import find_name_match
    c = _cand(db)
    m = find_name_match(db, "sivakumar   SAYEERAM")
    assert m is not None and m.id == c.id


def test_short_names_never_match(db):
    """<6 letters would flag half the database — 'A Ku' matches nobody."""
    from models import Candidate as _C
    from services.resume_parse import find_name_match
    db.add(_C(first_name="A", last_name="Ku", email="aku@x.com")); db.flush()
    assert find_name_match(db, "A Ku") is None


def test_different_names_do_not_match(db):
    from services.resume_parse import find_name_match
    _cand(db)
    assert find_name_match(db, "Sivakumar Ramanathan") is None


# --- parse cache -------------------------------------------------------------

def test_cache_hit_skips_extraction_entirely(db):
    """A cached sha must return the stored parse without touching the file —
    that is the whole point: repeat files never re-pay the model call."""
    from models import ResumeParseCache
    from services.resume_parse import parse_resume_bytes

    from services.resume_parse import PARSE_FIELDS
    # A cache entry must carry the FULL current schema — entries from before
    # PARSE_FIELDS grew are deliberately treated as misses (28 Aug 2026).
    payload = {**{k: "" for k in PARSE_FIELDS},
               "name": "Cached Person", "email": "c@x.com", "skills": []}
    db.add(ResumeParseCache(file_sha256="a" * 64, parsed=payload))
    db.flush()
    # Garbage bytes + unsupported ext would normally yield nothing — the cache
    # answers first.
    out = parse_resume_bytes(b"\x00garbage", ".pdf", db=db, sha256="a" * 64)
    assert out["name"] == "Cached Person"
    assert out["from_cache"] is True


def test_regex_fallback_is_never_cached(db):
    """No API key in tests → _ai_parse returns from_ai=False → nothing may be
    written to the cache, or a transient outage would freeze thin data."""
    from models import ResumeParseCache
    from services.resume_parse import parse_resume_bytes
    from sqlalchemy import select as _select

    out = parse_resume_bytes(SAMPLE.encode(), ".txt", db=db, sha256="b" * 64)
    assert out["email"] == "siva8848@gmail.com"  # regex still worked
    assert db.execute(_select(ResumeParseCache)).first() is None


# --- zip sanity (the endpoint's member filter, mirrored) ---------------------

def test_zip_member_filtering_mirrors_the_endpoint():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cv1.pdf", b"x")
        zf.writestr("notes/cv2.docx", b"x")
        zf.writestr("__MACOSX/._cv1.pdf", b"x")
        zf.writestr("readme.md", b"x")
        zf.writestr("folder/", b"")
    zf = zipfile.ZipFile(io.BytesIO(buf.getvalue()))
    members = [m for m in zf.infolist()
               if not m.is_dir()
               and not m.filename.startswith("__MACOSX")
               and m.filename.rsplit(".", 1)[-1].lower() in ("pdf", "docx", "txt")]
    assert sorted(m.filename for m in members) == ["cv1.pdf", "notes/cv2.docx"]


def test_stale_cache_schema_is_treated_as_miss(db):
    """A cache entry missing newer PARSE_FIELDS keys (written before the
    schema grew) must NOT be served — the file gets a fresh extraction."""
    from models import ResumeParseCache
    from services.resume_parse import parse_resume_bytes

    db.add(ResumeParseCache(file_sha256="b" * 64,
                            parsed={"name": "Old Thin Person", "email": "o@x.com", "skills": []}))
    db.flush()
    out = parse_resume_bytes(b"\x00garbage", ".pdf", db=db, sha256="b" * 64)
    # Garbage bytes yield no extraction — the point is the stale cache did
    # NOT answer with "Old Thin Person".
    assert out.get("name") != "Old Thin Person"
    assert out.get("from_cache") is not True
