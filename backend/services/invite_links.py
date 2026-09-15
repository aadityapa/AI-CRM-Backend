"""The ONE way to build a candidate's interview invite URL (15 Sep 2026).

Every CRM path used to emit ``f"/?invite={token}"`` when ``PUBLIC_BASE_URL``
was unset — a bare relative path that went straight into the candidate's
email and into the string the TA copies. Only the legacy HR screen resolved
the host properly (``main._invite_base_url``). This module is the shared
resolver, in precedence order:

1. Settings ▸ ``email.public_base_url`` (Admin-editable, wins over the env).
2. ``PUBLIC_BASE_URL`` env (unless ``auto``).
3. The current request: ``X-Forwarded-Proto/Host`` from a reverse proxy, else
   the request's own base URL — swapping ``localhost``/``0.0.0.0`` for the
   detected LAN IPv4 so a link built on the server still opens from another
   machine on the same network.
4. The last base a request resolved (background jobs have no request).

``invite_url`` never returns a relative path: with nothing to go on it raises
``InviteBaseUnavailable`` so the caller fails loudly instead of mailing a
link nobody can click.
"""
from __future__ import annotations

import ipaddress
import os
import socket
import threading
from urllib.parse import urlparse

_last_seen_base = ""
_lock = threading.Lock()

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


class InviteBaseUnavailable(RuntimeError):
    """No absolute base URL could be derived for an invite link."""


def configured_base() -> str:
    """Settings value, else env. Empty when neither is set (or set to 'auto')."""
    base = ""
    try:
        from services.org_settings import setting
        base = (setting("email.public_base_url") or "").strip().rstrip("/")
    except Exception:
        base = ""
    if not base:
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base.lower() == "auto":
        return ""
    return base


def detect_lan_ip() -> str:
    """Best-effort private IPv4 of this host (no packets are actually sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        addr = ipaddress.ip_address(ip)
        if addr.version == 4 and not (addr.is_loopback or addr.is_link_local or addr.is_unspecified):
            return ip
    except Exception:
        pass
    return ""


def base_from_request(request) -> str:
    """Absolute origin the candidate can reach, derived from one HTTP request."""
    if request is None:
        return ""
    headers = getattr(request, "headers", {}) or {}
    fwd_host = (headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if fwd_host:
        proto = (headers.get("x-forwarded-proto") or "https").split(",")[0].strip() or "https"
        return f"{proto}://{fwd_host}"
    try:
        raw = str(request.base_url).rstrip("/")
    except Exception:
        return ""
    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port else ""
    if host in _LOCAL_HOSTS or host.startswith("169.254."):
        lan = detect_lan_ip()
        if lan:
            return f"{scheme}://{lan}{port}"
    return raw


def resolve_invite_base(request=None) -> str:
    """Configured base → request-derived base → last seen base → ''."""
    global _last_seen_base
    base = configured_base()
    if not base and request is not None:
        base = base_from_request(request)
    if base:
        with _lock:
            _last_seen_base = base
        return base
    with _lock:
        return _last_seen_base


def remember_base(base: str) -> None:
    """Record a base another resolver (main._invite_base_url) already derived."""
    global _last_seen_base
    base = (base or "").strip().rstrip("/")
    host = (urlparse(base).hostname or "").lower()
    if base and host and host not in _LOCAL_HOSTS:
        with _lock:
            _last_seen_base = base


def invite_url(invite_token: str, request=None, *, strict: bool = True) -> str:
    """Absolute ``<base>/?invite=<token>``. With ``strict`` (default) raises
    InviteBaseUnavailable rather than returning a relative path."""
    base = resolve_invite_base(request)
    if not base:
        if strict:
            raise InviteBaseUnavailable(
                "Cannot build the interview link: set Settings ▸ Email ▸ Public base URL "
                "(or the PUBLIC_BASE_URL environment variable) to the address candidates open.")
        return f"/?invite={invite_token}"
    return f"{base}/?invite={invite_token}"
