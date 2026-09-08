"""In-memory interview sessions (single-worker unless REDIS_URL is configured)."""

from __future__ import annotations

import threading

sessions: dict[str, dict] = {}
_session_locks: dict[str, threading.Lock] = {}
_global_lock = threading.Lock()


def session_lock(session_key: str) -> threading.Lock:
    """Per-session mutex for /answer and background turn evaluation."""
    key = str(session_key or "").strip() or "default"
    with _global_lock:
        lock = _session_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _session_locks[key] = lock
        return lock
