"""Phones and tablets cannot sit the AI interview (12 Oct 2026, user decision).

The interview needs a laptop or desktop with a camera: it is recorded (camera +
shared screen), proctored, and the coding questions need a keyboard. The
candidate page refuses a handheld device before the welcome screen; this module
is the SERVER's twin, checked again by the invite verify / login routes so a
link opened on a phone can never start a session.

Detection is by User-Agent (pure, no I/O). It catches every phone browser's
default; a candidate who forces "desktop site" on a phone is caught by the page
instead (touch-only pointer, no screen sharing). ``INTERVIEW_ALLOW_MOBILE=1``
switches the rule off without a deploy.
"""
from __future__ import annotations

import os
import re

DESKTOP_REQUIRED_CODE = "desktop_required"
DESKTOP_REQUIRED_MESSAGE = (
    "This interview must be taken on a laptop or desktop computer with a camera. "
    "Please open the same link on your laptop or PC."
)

# Tokens that only phone / tablet browsers send. "Mobile" covers Android phones,
# iOS Safari and most in-app browsers; iPad / tablets are named explicitly.
_HANDHELD_RE = re.compile(
    r"Android|iPhone|iPad|iPod|Mobile|Windows Phone|BlackBerry|BB10|Opera Mini|"
    r"IEMobile|webOS|Silk/|Kindle|KFAPWI|PlayBook|Tablet",
    re.IGNORECASE,
)


def is_handheld_user_agent(user_agent: str | None) -> bool:
    """True for a phone or tablet browser's User-Agent. A blank UA is not
    judged handheld (some proxies strip it — the page still checks)."""
    ua = str(user_agent or "").strip()
    if not ua:
        return False
    return bool(_HANDHELD_RE.search(ua))


def desktop_required() -> bool:
    """The rule is on unless ``INTERVIEW_ALLOW_MOBILE`` is truthy."""
    raw = str(os.getenv("INTERVIEW_ALLOW_MOBILE", "") or "").strip().lower()
    return raw not in {"1", "true", "yes", "on"}


def handheld_block(request_headers) -> dict | None:
    """The 403 body to send when this request comes from a handheld device and
    the rule is on; None otherwise. ``request_headers`` is any mapping with
    ``.get`` (Starlette headers, a dict)."""
    if not desktop_required():
        return None
    ua = request_headers.get("user-agent") if request_headers is not None else None
    hint = str((request_headers.get("sec-ch-ua-mobile") if request_headers is not None else "") or "").strip()
    if is_handheld_user_agent(ua) or hint == "?1":
        return {"error": DESKTOP_REQUIRED_MESSAGE, "code": DESKTOP_REQUIRED_CODE}
    return None
