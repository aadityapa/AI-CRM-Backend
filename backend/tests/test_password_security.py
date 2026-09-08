"""Security tests for password hashing + transparent legacy migration.

Run:  python -m pytest tests/test_password_security.py -q
"""
import hashlib

import password_hashing as pwh
import auth_db


# --------------------------------------------------------------- unit: module
def test_modern_hash_is_bcrypt_and_self_describing():
    h = pwh.hash_password("correct horse battery staple")
    assert h.startswith("bcrypt$12$")
    assert pwh.is_modern_format(h)


def test_same_password_different_users_different_hashes():
    """Per-user salt: identical passwords must not collide in storage."""
    a = pwh.hash_password("Sup3rSecret!")
    b = pwh.hash_password("Sup3rSecret!")
    assert a != b


def test_password_never_stored_in_plaintext():
    pw = "PlaintextLeakCheck123"
    h = pwh.hash_password(pw)
    assert pw not in h


def test_verify_roundtrip_and_rejects_wrong():
    h = pwh.hash_password("rightpassword1")
    ok, needs = pwh.verify_password("rightpassword1", h)
    assert ok is True and needs is False
    bad, _ = pwh.verify_password("wrongpassword1", h)
    assert bad is False


def test_policy_rejects_short_passwords():
    import pytest
    with pytest.raises(pwh.PasswordPolicyError):
        pwh.validate_password("short")


def test_legacy_pbkdf2_row_verifies_and_flags_rehash():
    """A bare-hex pbkdf2(120000) legacy value verifies and asks to be upgraded."""
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    legacy = hashlib.pbkdf2_hmac("sha256", b"legacyPass1", salt, 120000).hex()
    ok, needs = pwh.verify_password("legacyPass1", legacy, salt.hex())
    assert ok is True and needs is True
    bad, _ = pwh.verify_password("nope", legacy, salt.hex())
    assert bad is False


# ------------------------------------------------- integration: real DB login
def _seed_legacy_user(target, username="legacyuser", password="legacyPass1"):
    salt = auth_db.os.urandom(16)
    legacy_hash = auth_db._hash_password(password, salt)
    now = auth_db._now_ist_parts()
    with auth_db._connect_sqlite(auth_db.Path(target)) as conn:
        conn.execute(
            """INSERT INTO registration_data
               (full_name, email, username, role, password_hash, password_salt,
                created_at_ist, created_date_ist, created_time_ist)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            ("Legacy User", f"{username}@x.com", username, "hr", legacy_hash, salt.hex(),
             now["ist_iso"], now["ist_date"], now["ist_time"]),
        )
        conn.commit()


def _stored_hash(target, username):
    with auth_db._connect_sqlite(auth_db.Path(target)) as conn:
        row = conn.execute(
            "SELECT password_hash FROM registration_data WHERE username = ?", (username,)
        ).fetchone()
    return row["password_hash"] if row else None


def test_login_transparently_upgrades_legacy_hash(tmp_path):
    target = str(tmp_path / "auth.db")
    auth_db.init_auth_db(target)
    _seed_legacy_user(target)

    # Before login: stored value is the bare-hex legacy hash.
    before = _stored_hash(target, "legacyuser")
    assert not pwh.is_modern_format(before)

    res = auth_db.verify_login(target, "legacyuser", "legacyPass1")
    assert res["success"] is True

    # After a successful login: the hash was silently upgraded to bcrypt.
    after = _stored_hash(target, "legacyuser")
    assert pwh.is_modern_format(after)
    assert after.startswith("bcrypt$")

    # And the upgraded user can still log in (and wrong passwords still fail).
    assert auth_db.verify_login(target, "legacyuser", "legacyPass1")["success"] is True
    assert auth_db.verify_login(target, "legacyuser", "WRONG")["success"] is False


def test_register_then_login_uses_modern_hash(tmp_path):
    target = str(tmp_path / "auth.db")
    auth_db.init_auth_db(target)
    auth_db.register_user(target, "New User", "new@x.com", "newuser", "brandNewPass1", "hr")

    assert pwh.is_modern_format(_stored_hash(target, "newuser"))
    assert auth_db.verify_login(target, "newuser", "brandNewPass1")["success"] is True
    assert auth_db.verify_login(target, "newuser", "brandNewPass2")["success"] is False


def test_register_rejects_short_password(tmp_path):
    import pytest
    target = str(tmp_path / "auth.db")
    auth_db.init_auth_db(target)
    with pytest.raises(ValueError):
        auth_db.register_user(target, "Short", "s@x.com", "shortpw", "abc", "hr")
