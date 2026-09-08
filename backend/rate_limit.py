"""Optional rate-limiting wrapper around slowapi.

Activate explicitly with ``RATE_LIMIT_ENABLED=true``, or rely on production
defaults (enabled when ``KARNEX_ENV=production`` unless opted out).

When disabled or the dep is missing, ``setup_rate_limit(app)`` is a no-op
and ``limit(spec)`` returns a passthrough decorator.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger("karnex.rate_limit")


def _is_production_env() -> bool:
    env = str(os.getenv("KARNEX_ENV") or os.getenv("ENV") or os.getenv("NODE_ENV") or "").strip().lower()
    return env in {"production", "prod"}


def _enabled() -> bool:
    raw = str(os.getenv("RATE_LIMIT_ENABLED", "")).strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return _is_production_env()


_limiter: Any | None = None


def setup_rate_limit(app) -> bool:
    """Attach slowapi to the FastAPI app if available and enabled. Returns True on success."""
    global _limiter
    if not _enabled():
        return False
    try:
        from slowapi import Limiter
        from slowapi.errors import RateLimitExceeded
        from slowapi.middleware import SlowAPIMiddleware
        from slowapi.util import get_remote_address
    except Exception:
        logger.info("slowapi not installed; rate-limit disabled.")
        return False

    redis_url = (os.getenv("REDIS_URL") or "").strip()
    storage_uri = redis_url if redis_url else "memory://"
    _limiter = Limiter(key_func=get_remote_address, storage_uri=storage_uri, default_limits=[])
    app.state.limiter = _limiter

    @app.exception_handler(RateLimitExceeded)
    async def _ratelimit_handler(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(
            {"error": "Too many requests. Please slow down."},
            status_code=429,
            headers={"Retry-After": str(int(getattr(exc, "retry_after", 1) or 1))},
        )

    app.add_middleware(SlowAPIMiddleware)
    logger.info("Rate limit enabled; storage=%s", "redis" if redis_url else "memory")
    return True


def limit(spec: str) -> Callable:
    """Decorator factory; returns slowapi limiter when active, no-op otherwise.

    IMPORTANT: this reads ``_limiter`` at *decoration* time, so
    ``setup_rate_limit(app)`` must run before any decorated route is defined.
    main.py calls it immediately after ``app = FastAPI(...)`` for that reason.
    A silent no-op here previously left every per-route limit inert in
    production, so the miss is now logged loudly instead of passing quietly.
    """
    if _limiter is None:
        if _enabled():
            logger.error(
                "rate-limit: @limit(%r) evaluated before setup_rate_limit(app) "
                "(or slowapi missing) - this route is NOT rate limited.",
                spec,
            )
        def _noop(fn: Callable) -> Callable:
            return fn
        return _noop
    return _limiter.limit(spec)
