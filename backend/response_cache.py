"""Lightweight response cache for deterministic OpenAI calls (e.g. sample-questions).

- Keyed by a stable hash of the request inputs (skills, role, seniority, domains,
  difficulty, jd prefix, numQ). Variety seed and other freshness nonces are
  deliberately excluded from the key.
- Backed by SQLite or Postgres; same target as auth_db.
- TTL configurable via OPENAI_RESPONSE_CACHE_TTL_S (default 24h).
- Gated by OPENAI_RESPONSE_CACHE_ENABLED (default off in dev, opt-in in prod).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("karnex.response_cache")


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except (TypeError, ValueError):
        return default


CACHE_ENABLED = _bool_env("OPENAI_RESPONSE_CACHE_ENABLED", False)
CACHE_TTL_S = _int_env("OPENAI_RESPONSE_CACHE_TTL_S", 24 * 60 * 60)


def _is_postgres(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


_CREATE_PG = """
CREATE TABLE IF NOT EXISTS openai_response_cache (
    key TEXT PRIMARY KEY,
    call_type TEXT NOT NULL,
    output JSONB NOT NULL,
    created_at_epoch BIGINT NOT NULL
)
"""

_CREATE_SQLITE = """
CREATE TABLE IF NOT EXISTS openai_response_cache (
    key TEXT PRIMARY KEY,
    call_type TEXT NOT NULL,
    output TEXT NOT NULL,
    created_at_epoch INTEGER NOT NULL
)
"""

_INDEX = "CREATE INDEX IF NOT EXISTS idx_orc_created ON openai_response_cache (created_at_epoch DESC)"

_initialized: dict[str, bool] = {}


def _ensure_schema(db_target: str) -> None:
    if not db_target or _initialized.get(db_target):
        return
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(_CREATE_PG)
                    cur.execute(_INDEX)
                conn.commit()
        else:
            Path(db_target).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(db_target) as conn:
                conn.execute(_CREATE_SQLITE)
                conn.execute(_INDEX)
                conn.commit()
        _initialized[db_target] = True
    except Exception as exc:
        logger.warning("response_cache schema init failed: %s", exc)


def make_key(call_type: str, payload: dict[str, Any]) -> str:
    """Stable cache key for the given call type and payload."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{call_type}|{canonical}".encode("utf-8")).hexdigest()


def get(db_target: str, key: str) -> Any | None:
    if not CACHE_ENABLED or not db_target or not key:
        return None
    _ensure_schema(db_target)
    cutoff = int(time.time()) - max(60, CACHE_TTL_S)
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            from psycopg2.extras import RealDictCursor
            with pg.connect(db_target) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "SELECT output FROM openai_response_cache WHERE key = %s AND created_at_epoch >= %s",
                        (key, cutoff),
                    )
                    row = cur.fetchone()
            if not row:
                return None
            out = row.get("output")
            if isinstance(out, (dict, list)):
                return out
            return json.loads(out) if isinstance(out, str) else None
        else:
            with sqlite3.connect(db_target) as conn:
                cur = conn.execute(
                    "SELECT output FROM openai_response_cache WHERE key = ? AND created_at_epoch >= ?",
                    (key, cutoff),
                )
                row = cur.fetchone()
            if not row:
                return None
            return json.loads(row[0]) if row[0] else None
    except Exception as exc:
        logger.warning("response_cache get failed: %s", exc)
        return None


def set(db_target: str, key: str, call_type: str, output: Any) -> None:
    if not CACHE_ENABLED or not db_target or not key:
        return
    _ensure_schema(db_target)
    payload = json.dumps(output, ensure_ascii=False, default=str)
    now = int(time.time())
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO openai_response_cache (key, call_type, output, created_at_epoch)
                        VALUES (%s, %s, %s::jsonb, %s)
                        ON CONFLICT (key) DO UPDATE SET output = EXCLUDED.output, created_at_epoch = EXCLUDED.created_at_epoch
                        """,
                        (key, call_type, payload, now),
                    )
                conn.commit()
        else:
            with sqlite3.connect(db_target) as conn:
                conn.execute(
                    """
                    INSERT INTO openai_response_cache (key, call_type, output, created_at_epoch)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET output = excluded.output, created_at_epoch = excluded.created_at_epoch
                    """,
                    (key, call_type, payload, now),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("response_cache set failed: %s", exc)


def purge_expired(db_target: str) -> int:
    if not db_target:
        return 0
    _ensure_schema(db_target)
    cutoff = int(time.time()) - max(60, CACHE_TTL_S)
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM openai_response_cache WHERE created_at_epoch < %s", (cutoff,))
                    n = cur.rowcount
                conn.commit()
                return int(n or 0)
        else:
            with sqlite3.connect(db_target) as conn:
                cur = conn.execute("DELETE FROM openai_response_cache WHERE created_at_epoch < ?", (cutoff,))
                conn.commit()
                return int(cur.rowcount or 0)
    except Exception:
        return 0
