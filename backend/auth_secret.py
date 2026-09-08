"""The one place the JWT signing secret is resolved.

Previously four modules each carried their own copy of

    os.getenv("AUTH_SECRET") or os.getenv("REPORT_CODE") or "change-me-auth-secret"

That literal is in the public source, so any deployment without AUTH_SECRET set
was signing every token with a value an attacker can compute — enough to forge
an Admin session. The startup guard that was meant to catch this only fired when
KARNEX_ENV was literally "production", so a deployment that had not set that
variable ran wide open.

There is no fallback here. If AUTH_SECRET is missing or too short the process
refuses to start, in every environment. A loud failure on day one is far cheaper
than a silent forgery hole.

REPORT_CODE is no longer accepted: it is a short HR unlock code shared with
staff, never a signing key.
"""
from __future__ import annotations

import os
import secrets

#: 32 bytes is the floor for HS256 to be meaningful.
MIN_SECRET_BYTES = 32

_ENV_VAR = "AUTH_SECRET"
#: Values seen in old configs and docs — refuse them explicitly rather than
#: letting a copy-pasted placeholder look like a real secret.
_KNOWN_WEAK = {
    "change-me-auth-secret",
    "changeme",
    "secret",
    "your-secret-key",
    "please-change-me",
}


class AuthSecretError(RuntimeError):
    """Raised at import time when the signing secret is unusable."""


def _fail(reason: str) -> "AuthSecretError":
    suggestion = secrets.token_urlsafe(48)
    return AuthSecretError(
        f"{reason}\n\n"
        f"Set a strong {_ENV_VAR} in the environment (.env is fine locally):\n\n"
        f"    {_ENV_VAR}={suggestion}\n\n"
        f"It must be at least {MIN_SECRET_BYTES} bytes. Changing it signs out every\n"
        f"existing session, which is the intended effect when rotating."
    )


def auth_secret() -> str:
    """The HS256 signing key. Raises rather than falling back to a default."""
    raw = (os.getenv(_ENV_VAR) or "").strip()
    if not raw:
        raise _fail(f"{_ENV_VAR} is not set.")
    if raw.lower() in _KNOWN_WEAK:
        raise _fail(f"{_ENV_VAR} is set to a known placeholder value.")
    if len(raw.encode("utf-8")) < MIN_SECRET_BYTES:
        raise _fail(
            f"{_ENV_VAR} is only {len(raw.encode('utf-8'))} bytes; "
            f"at least {MIN_SECRET_BYTES} are required."
        )
    return raw


def auth_secret_or_none() -> str | None:
    """Non-raising variant, for health checks that want to report rather than crash."""
    try:
        return auth_secret()
    except AuthSecretError:
        return None
