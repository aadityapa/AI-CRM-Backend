"""Password hashing with a self-describing, versioned format and transparent
legacy migration.

Why this exists
---------------
The original scheme stored ``pbkdf2_hmac('sha256', pw, salt, 120000)`` as bare
hex in ``password_hash`` with the salt in a separate ``password_salt`` column,
and verified with a plain ``!=`` (not constant-time). PBKDF2 is an acceptable
KDF, but the project standard is Argon2id/bcrypt, and secret comparison must be
constant-time.

This module:
  * hashes new passwords with **bcrypt (cost 12)** when available, else a
    strengthened **PBKDF2-HMAC-SHA256 (600k iterations)** — both stored in a
    self-describing string so the algorithm is never guessed.
  * verifies **both** the new format and the legacy bare-hex pbkdf2 rows, using
    constant-time comparison (``hmac.compare_digest`` / bcrypt's own compare).
  * reports ``needs_rehash`` so callers can transparently upgrade a user's stored
    hash on their next successful login — no mass password reset required.

Stored formats
--------------
  new bcrypt   :  ``bcrypt$12$<base64 bcrypt hash>``
  new pbkdf2   :  ``pbkdf2_sha256$600000$<salt_hex>$<hash_hex>``
  legacy       :  bare hex digest (salt supplied separately by the DB row)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

try:  # bcrypt is in requirements; degrade gracefully if it is ever missing.
    import bcrypt as _bcrypt
except Exception:  # pragma: no cover
    _bcrypt = None

BCRYPT_COST = 12
PBKDF2_ITERATIONS = 600_000  # OWASP 2023 floor for PBKDF2-HMAC-SHA256
_MIN_LENGTH = 8


class PasswordPolicyError(ValueError):
    """Raised when a password fails policy (e.g. too short)."""


def validate_password(password: str) -> None:
    """Enforce the minimum password policy. Raises PasswordPolicyError."""
    if not isinstance(password, str) or len(password) < _MIN_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {_MIN_LENGTH} characters.")


def _bcrypt_prehash(password: str) -> bytes:
    """bcrypt silently truncates at 72 bytes and chokes on NUL bytes.

    Pre-hashing with SHA-256 and base64-encoding gives a fixed-length, NUL-free
    input so long passwords keep their full entropy. This is the standard
    "bcrypt(base64(sha256(pw)))" construction.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Return a self-describing hash string for a NEW/updated password.

    Does NOT enforce policy — callers that accept user input should call
    ``validate_password`` first; internal re-hashes of an already-accepted
    password skip the policy check on purpose.
    """
    if _bcrypt is not None:
        hashed = _bcrypt.hashpw(_bcrypt_prehash(password), _bcrypt.gensalt(rounds=BCRYPT_COST))
        return f"bcrypt${BCRYPT_COST}${base64.b64encode(hashed).decode('ascii')}"
    # Fallback: strengthened PBKDF2, still self-describing.
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_bcrypt(password: str, stored: str) -> bool:
    if _bcrypt is None:  # pragma: no cover - bcrypt present in this project
        return False
    try:
        _, _cost, b64 = stored.split("$", 2)
        raw = base64.b64decode(b64)
        return _bcrypt.checkpw(_bcrypt_prehash(password), raw)  # constant-time in bcrypt
    except Exception:
        return False


def _verify_pbkdf2_selfdescribing(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$", 3)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False


def _verify_legacy(password: str, legacy_hash: str, legacy_salt_hex: str) -> bool:
    """Verify the original bare-hex pbkdf2(120000) format, constant-time."""
    try:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(legacy_salt_hex), 120_000)
        return hmac.compare_digest(digest.hex(), (legacy_hash or "").strip())
    except Exception:
        return False


def is_modern_format(stored: str) -> bool:
    s = (stored or "").strip()
    return s.startswith("bcrypt$") or s.startswith("pbkdf2_sha256$")


def verify_password(password: str, stored_hash: str, legacy_salt_hex: str | None = None) -> tuple[bool, bool]:
    """Verify ``password`` against ``stored_hash``.

    Returns ``(ok, needs_rehash)``. ``needs_rehash`` is True when the stored value
    is a legacy/weaker format and the caller should re-hash on success.
    Comparison is constant-time in every branch.
    """
    stored = (stored_hash or "").strip()
    if not stored or not password:
        return (False, False)

    if stored.startswith("bcrypt$"):
        return (_verify_bcrypt(password, stored), False)
    if stored.startswith("pbkdf2_sha256$"):
        # Modern only if bcrypt is unavailable; if bcrypt exists, prefer upgrading.
        ok = _verify_pbkdf2_selfdescribing(password, stored)
        return (ok, ok and _bcrypt is not None)
    # Legacy bare-hex row: needs the separate salt column.
    if legacy_salt_hex:
        ok = _verify_legacy(password, stored, legacy_salt_hex)
        return (ok, ok)  # always upgrade legacy on success
    return (False, False)
