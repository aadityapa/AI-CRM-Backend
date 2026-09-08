"""Resume file integrity: SHA-256 checksum + dedupe key + verify-on-read.

Run:  python -m pytest tests/test_resume_checksum.py -q
"""
import hashlib
import io

import services.crm_common as cc


class _FakeUpload:
    """Minimal stand-in for Starlette's UploadFile (.filename + .file)."""
    def __init__(self, data: bytes, filename: str = "resume.pdf"):
        self.filename = filename
        self.file = io.BytesIO(data)


def _isolate(tmp_path):
    cc.CRM_UPLOAD_DIR = tmp_path


def test_checksum_matches_sha256_of_bytes(tmp_path):
    _isolate(tmp_path)
    data = b"%PDF-1.4 fake resume bytes"
    url, sha, size = cc.save_upload_hashed(_FakeUpload(data), "resumes")
    assert sha == hashlib.sha256(data).hexdigest()
    assert size == len(data)
    assert url.startswith("/api/crm-files/resumes/")


def test_identical_bytes_same_hash_different_random_name(tmp_path):
    """Dedupe key is stable across uploads; stored filename is not reused."""
    _isolate(tmp_path)
    data = b"identical bytes"
    u1, s1, _ = cc.save_upload_hashed(_FakeUpload(data), "resumes")
    u2, s2, _ = cc.save_upload_hashed(_FakeUpload(data), "resumes")
    assert s1 == s2          # same content -> same dedupe key
    assert u1 != u2          # but different randomized storage names


def test_different_bytes_different_hash(tmp_path):
    _isolate(tmp_path)
    _, a, _ = cc.save_upload_hashed(_FakeUpload(b"one"), "resumes")
    _, b, _ = cc.save_upload_hashed(_FakeUpload(b"two"), "resumes")
    assert a != b


def test_verify_checksum_roundtrip(tmp_path):
    _isolate(tmp_path)
    data = b"verify me on read"
    url, sha, _ = cc.save_upload_hashed(_FakeUpload(data), "resumes")
    rel = url.split("/api/crm-files/", 1)[1]
    assert cc.verify_crm_file_checksum(rel, sha) is True
    # tampered / wrong digest fails, constant-time compare
    assert cc.verify_crm_file_checksum(rel, "0" * 64) is False


def test_filename_is_not_trusted(tmp_path):
    """A path-traversal filename must not escape the upload dir.

    Production got STRICTER than this test's original expectation: rather than
    sanitising an unknown extension, `safe_upload_extension` now refuses it
    (400), so a bad name never reaches disk at all. "../../etc/passwd" has no
    allowed suffix, so it is rejected outright — a stronger guarantee than
    "stored under a uuid".
    """
    import pytest
    from fastapi import HTTPException

    _isolate(tmp_path)
    with pytest.raises(HTTPException) as exc:
        cc.save_upload_hashed(_FakeUpload(b"x", filename="../../etc/passwd"), "resumes")
    assert exc.value.status_code == 400
    assert "not accepted" in str(exc.value.detail)


def test_traversal_in_an_allowed_extension_is_still_neutralised(tmp_path):
    """The other half of the rule: a traversal path that DOES end in an allowed
    extension is stored under a uuid, never at the attacker's path."""
    _isolate(tmp_path)
    url, _sha, _ = cc.save_upload_hashed(
        _FakeUpload(b"x", filename="../../etc/passwd.pdf"), "resumes")
    assert "etc/passwd" not in url
    assert ".." not in url
    assert "/api/crm-files/resumes/" in url
