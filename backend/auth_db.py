from __future__ import annotations

import hashlib
import json
import os
import sqlite3

import password_hashing as pwh
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4
import secrets
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import threading
from template_prompt import (
    build_default_template_prompt,
    build_template_prompt_context,
    now_iso_utc,
    render_prompt_preview,
    sanitize_prompt_input,
)
from utils.interview_limits import clamp_count_mode_questions

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30))

DbTarget = str | Path

# Max manual interview lines per job template after trim + case-insensitive dedupe.
MANUAL_QUESTIONS_MAX = 120


def _api_interview_mode(stored: str | None) -> str:
    """Expose canonical technical | hr in API responses (DB keeps mock | standard)."""
    from utils.interview_mode_mapper import api_interview_mode_from_storage

    return api_interview_mode_from_storage(stored)


def _storage_interview_mode(api_value: str | None) -> str:
    """Normalize API / form values to DB storage mock | standard."""
    from utils.interview_mode_mapper import storage_interview_mode_from_api

    return storage_interview_mode_from_api(api_value)


def _bool_from_any(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _now_ist_parts() -> dict[str, str]:
    now = datetime.now(IST)
    return {
        "ist_iso": now.isoformat(),
        "ist_date": now.strftime("%Y-%m-%d"),
        "ist_time": now.strftime("%H:%M:%S"),
    }


def _stable_master_id(prefix: str, value: str) -> str:
    normalized = " ".join(str(value or "").strip().lower().split())
    return f"{prefix}_{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:16]}"


def _master_table_meta(kind: str) -> tuple[str, str, str]:
    raw = str(kind or "").strip().lower()
    if raw in {"opportunity", "opportunities", "opportunity_master"}:
        return ("opportunity_master", "opportunity_id", "opp")
    if raw in {"customer", "customers", "customer_master"}:
        return ("customer_master", "customer_name", "cust")
    raise ValueError("master kind must be opportunity or customer")


def _is_postgres(db_target: DbTarget) -> bool:
    s = str(db_target)
    return s.startswith("postgresql://") or s.startswith("postgres://")


def normalize_postgres_dsn(dsn: str) -> str:
    """Ensure SSL for cloud Postgres (Render, etc.) when not localhost."""
    raw = (dsn or "").strip()
    if not raw or not _is_postgres(raw):
        return raw
    if "sslmode=" in raw:
        return raw
    host = (urlparse(raw).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "postgres"}:
        return raw
    sep = "&" if "?" in raw else "?"
    return f"{raw}{sep}sslmode=require"


def _connect_sqlite(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


_PG_POOLS: dict[str, ThreadedConnectionPool] = {}
_PG_POOL_LOCK = threading.Lock()
_PG_POOL_MIN = max(1, int(os.getenv("PG_POOL_MIN", "1")))
_PG_POOL_MAX = max(_PG_POOL_MIN, int(os.getenv("PG_POOL_MAX", "10")))
_PG_POOL_ENABLED = (os.getenv("PG_POOL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"})


def _get_pg_pool(dsn: str) -> ThreadedConnectionPool | None:
    if not _PG_POOL_ENABLED:
        return None
    pool = _PG_POOLS.get(dsn)
    if pool is not None:
        return pool
    with _PG_POOL_LOCK:
        pool = _PG_POOLS.get(dsn)
        if pool is not None:
            return pool
        try:
            pool = ThreadedConnectionPool(_PG_POOL_MIN, _PG_POOL_MAX, dsn=dsn)
            _PG_POOLS[dsn] = pool
            return pool
        except Exception:
            return None


class _PooledConnectionCtx:
    """Pool-friendly context manager that mimics psycopg2 connection ctx semantics."""

    __slots__ = ("_dsn", "_pool", "_conn")

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = _get_pg_pool(dsn)
        self._conn = None

    def __enter__(self):
        if self._pool is None:
            self._conn = psycopg2.connect(self._dsn)
            return self._conn
        self._conn = self._pool.getconn()
        try:
            if getattr(self._conn, "closed", 0):
                self._pool.putconn(self._conn, close=True)
                self._conn = self._pool.getconn()
            # Defensive: clear any stale transaction state from prior use.
            try:
                self._conn.rollback()
            except Exception:
                pass
        except Exception:
            pass
        return self._conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        conn = self._conn
        self._conn = None
        if conn is None:
            return False
        if self._pool is None:
            try:
                if exc_type is not None:
                    conn.rollback()
                else:
                    conn.commit()
            except Exception:
                pass
            try:
                conn.close()
            except Exception:
                pass
            return False
        try:
            if exc_type is not None:
                conn.rollback()
            else:
                conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            self._pool.putconn(conn, close=bool(exc_type))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
        return False


def _connect_postgres(dsn: str):
    """Return a context manager yielding a Postgres connection (pooled when enabled)."""
    return _PooledConnectionCtx(dsn)


def close_postgres_pools() -> None:
    with _PG_POOL_LOCK:
        for pool in _PG_POOLS.values():
            try:
                pool.closeall()
            except Exception:
                pass
        _PG_POOLS.clear()


def _ensure_postgres_database(dsn: str) -> None:
    parsed = urlparse(dsn)
    host = (parsed.hostname or "").lower()
    if "supabase.com" in host or "pooler.supabase.com" in host:
        return
    db_name = (parsed.path or "").lstrip("/")
    if not db_name:
        return
    admin_dsn = parsed._replace(path="/postgres").geturl()
    # One-shot admin op — bypass the pool to avoid caching a non-target DSN.
    conn = psycopg2.connect(admin_dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            if cur.fetchone():
                return
            cur.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        conn.close()


def _ensure_job_templates_columns_postgres(conn) -> None:
    """
    Backward-compatible schema evolution for existing installations.
    CREATE TABLE IF NOT EXISTS does not add new columns to an existing table.
    """
    desired = {
        "timing_mode": "TEXT NOT NULL DEFAULT 'count'",
        "time_limit_sec": "INTEGER NOT NULL DEFAULT 0",
        "mic_always_on": "BOOLEAN NOT NULL DEFAULT FALSE",
        "show_spoken_text": "BOOLEAN NOT NULL DEFAULT FALSE",
        "difficulty": "TEXT NOT NULL DEFAULT 'medium'",
        "num_q": "INTEGER NOT NULL DEFAULT 5",
        "followup_mode": "BOOLEAN NOT NULL DEFAULT TRUE",
        "interview_mode": "TEXT NOT NULL DEFAULT 'mock'",
        "jd_text": "TEXT",
        "template_instructions": "TEXT",
        "weights": "JSONB",
        "domain": "TEXT",
        "required_skills": "JSONB",
        "optional_skills": "JSONB",
        "exp_min": "INTEGER NOT NULL DEFAULT 0",
        "exp_max": "INTEGER NOT NULL DEFAULT 0",
        "created_at_ist": "TEXT NOT NULL DEFAULT ''",
        "updated_at_ist": "TEXT NOT NULL DEFAULT ''",
        "question_type": "TEXT NOT NULL DEFAULT 'dynamic'",
        "manual_questions": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "opportunity_id": "TEXT NOT NULL DEFAULT ''",
        "customer_name": "TEXT NOT NULL DEFAULT ''",
        "generated_prompt": "TEXT",
        "edited_prompt": "TEXT",
        "prompt_version": "INTEGER NOT NULL DEFAULT 1",
        "prompt_updated_by": "TEXT NOT NULL DEFAULT ''",
        "prompt_updated_at": "TEXT NOT NULL DEFAULT ''",
        "prompt_history": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'job_templates'
            """
        )
        existing = {str(r[0]) for r in (cur.fetchall() or [])}
        for col, ddl in desired.items():
            if col in existing:
                continue
            cur.execute(f"ALTER TABLE job_templates ADD COLUMN {col} {ddl}")


def _ensure_registration_columns_sqlite(conn: sqlite3.Connection) -> None:
    """Add `registration_data.is_active` to databases created before it existed.

    2 Sep 2026: `verify_login` reads `COALESCE(is_active, 1)` but no CREATE
    TABLE ever defined the column, so a database made by `init_auth_db` failed
    EVERY login with "no such column: is_active". Same self-healing idiom as
    the job_templates / interview_schedule helpers below.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(registration_data)")
    existing = {str(r[1]) for r in (cur.fetchall() or [])}
    if "is_active" not in existing:
        cur.execute(
            "ALTER TABLE registration_data ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")


def _ensure_registration_columns_postgres(conn) -> None:
    """Postgres twin of the above — idempotent, safe on every start."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE registration_data "
            "ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")


def _ensure_job_templates_columns_sqlite(conn: sqlite3.Connection) -> None:
    """
    Backward-compatible schema evolution for existing installations.
    SQLite does not support ALTER COLUMN easily; we only ADD missing columns.
    """
    desired = {
        "timing_mode": "TEXT NOT NULL DEFAULT 'count'",
        "time_limit_sec": "INTEGER NOT NULL DEFAULT 0",
        "mic_always_on": "INTEGER NOT NULL DEFAULT 0",
        "show_spoken_text": "INTEGER NOT NULL DEFAULT 0",
        "domain": "TEXT",
        "required_skills": "TEXT",
        "optional_skills": "TEXT",
        "exp_min": "INTEGER NOT NULL DEFAULT 0",
        "exp_max": "INTEGER NOT NULL DEFAULT 0",
        "difficulty": "TEXT NOT NULL DEFAULT 'medium'",
        "num_q": "INTEGER NOT NULL DEFAULT 5",
        "followup_mode": "INTEGER NOT NULL DEFAULT 1",
        "interview_mode": "TEXT NOT NULL DEFAULT 'mock'",
        "jd_text": "TEXT",
        "template_instructions": "TEXT",
        "weights": "TEXT",
        "created_at_ist": "TEXT NOT NULL DEFAULT ''",
        "updated_at_ist": "TEXT NOT NULL DEFAULT ''",
        "question_type": "TEXT NOT NULL DEFAULT 'dynamic'",
        "manual_questions": "TEXT NOT NULL DEFAULT '[]'",
        "opportunity_id": "TEXT NOT NULL DEFAULT ''",
        "customer_name": "TEXT NOT NULL DEFAULT ''",
        "generated_prompt": "TEXT",
        "edited_prompt": "TEXT",
        "prompt_version": "INTEGER NOT NULL DEFAULT 1",
        "prompt_updated_by": "TEXT NOT NULL DEFAULT ''",
        "prompt_updated_at": "TEXT NOT NULL DEFAULT ''",
        "prompt_history": "TEXT NOT NULL DEFAULT '[]'",
    }
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(job_templates)")
    existing = {str(r[1]) for r in (cur.fetchall() or [])}
    for col, ddl in desired.items():
        if col in existing:
            continue
        cur.execute(f"ALTER TABLE job_templates ADD COLUMN {col} {ddl}")


def _ensure_schedule_security_columns_postgres(conn) -> None:
    desired = {
        "access_key": "TEXT",
        "session_status": "TEXT NOT NULL DEFAULT 'pending'",
        "active_device_id": "TEXT",
        "login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "verified_at": "TEXT",
        "interview_started_at": "TEXT",
        "interview_completed_at": "TEXT",
        "violation_count": "INTEGER NOT NULL DEFAULT 0",
        "violations_log": "JSONB",
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'interview_schedule'
            """
        )
        existing = {str(r[0]) for r in (cur.fetchall() or [])}
        for col, ddl in desired.items():
            if col in existing:
                continue
            cur.execute(f"ALTER TABLE interview_schedule ADD COLUMN {col} {ddl}")


def _ensure_schedule_security_columns_sqlite(conn: sqlite3.Connection) -> None:
    desired = {
        "access_key": "TEXT",
        "session_status": "TEXT NOT NULL DEFAULT 'pending'",
        "active_device_id": "TEXT",
        "login_attempts": "INTEGER NOT NULL DEFAULT 0",
        "verified_at": "TEXT",
        "interview_started_at": "TEXT",
        "interview_completed_at": "TEXT",
        "violation_count": "INTEGER NOT NULL DEFAULT 0",
        "violations_log": "TEXT",
    }
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(interview_schedule)")
    existing = {str(r[1]) for r in (cur.fetchall() or [])}
    for col, ddl in desired.items():
        if col in existing:
            continue
        cur.execute(f"ALTER TABLE interview_schedule ADD COLUMN {col} {ddl}")


def _ensure_interview_progress_table_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS interview_progress (
                interview_id TEXT PRIMARY KEY,
                invite_token TEXT UNIQUE,
                candidate_name TEXT,
                candidate_email TEXT,
                status TEXT NOT NULL DEFAULT 'started',
                current_index INTEGER NOT NULL DEFAULT 0,
                questions JSONB NOT NULL DEFAULT '[]'::jsonb,
                answers JSONB NOT NULL DEFAULT '[]'::jsonb,
                meta JSONB NOT NULL DEFAULT '{}'::jsonb,
                violations JSONB NOT NULL DEFAULT '[]'::jsonb,
                payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_activity_at TEXT NOT NULL DEFAULT '',
                finalized_at TEXT NOT NULL DEFAULT '',
                report_status TEXT NOT NULL DEFAULT '',
                report_error TEXT NOT NULL DEFAULT '',
                created_at_ist TEXT NOT NULL DEFAULT '',
                updated_at_ist TEXT NOT NULL DEFAULT ''
            )
            """
        )


def _ensure_interview_progress_table_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS interview_progress (
            interview_id TEXT PRIMARY KEY,
            invite_token TEXT UNIQUE,
            candidate_name TEXT,
            candidate_email TEXT,
            status TEXT NOT NULL DEFAULT 'started',
            current_index INTEGER NOT NULL DEFAULT 0,
            questions TEXT NOT NULL DEFAULT '[]',
            answers TEXT NOT NULL DEFAULT '[]',
            meta TEXT NOT NULL DEFAULT '{}',
            violations TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL DEFAULT '{}',
            last_activity_at TEXT NOT NULL DEFAULT '',
            finalized_at TEXT NOT NULL DEFAULT '',
            report_status TEXT NOT NULL DEFAULT '',
            report_error TEXT NOT NULL DEFAULT '',
            created_at_ist TEXT NOT NULL DEFAULT '',
            updated_at_ist TEXT NOT NULL DEFAULT ''
        )
        """
    )


def _ensure_master_tables_postgres(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_master (
                id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                created_by TEXT,
                created_at_ist TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_master (
                id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                created_by TEXT,
                created_at_ist TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            )
            """
        )


def _ensure_master_tables_sqlite(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS opportunity_master (
            id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL,
            created_by TEXT,
            created_at_ist TEXT NOT NULL,
            updated_at_ist TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS customer_master (
            id TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            created_by TEXT,
            created_at_ist TEXT NOT NULL,
            updated_at_ist TEXT NOT NULL
        )
        """
    )


def _ensure_query_performance_indexes_postgres(conn) -> None:
    """Speed up HR lookups: candidate timeline + schedule lists + template grouping."""
    stmts = [
        "CREATE INDEX IF NOT EXISTS idx_interview_records_email_lower ON interview_records (LOWER(COALESCE(candidate_email, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_name_lower ON interview_records (LOWER(COALESCE(candidate_name, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_created_at_ist ON interview_records (created_at_ist DESC NULLS LAST)",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_submitted_report ON interview_records (submitted, has_report)",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_job_id ON interview_records ((payload->>'job_id'))",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_hr_username ON interview_schedule (hr_username)",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_scheduled ON interview_schedule (scheduled_at_local DESC)",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_email_lower ON interview_schedule (LOWER(COALESCE(candidate_email, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_name_lower ON interview_schedule (LOWER(COALESCE(candidate_name, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_invite ON interview_progress (invite_token)",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_status ON interview_progress (status, report_status)",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_activity ON interview_progress (last_activity_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_job_templates_updated_at_ist ON job_templates (updated_at_ist DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunity_master_value_lower ON opportunity_master (LOWER(opportunity_id))",
        "CREATE INDEX IF NOT EXISTS idx_opportunity_master_lookup ON opportunity_master (LOWER(opportunity_id), updated_at_ist DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_master_value_lower ON customer_master (LOWER(customer_name))",
        "CREATE INDEX IF NOT EXISTS idx_customer_master_lookup ON customer_master (LOWER(customer_name), updated_at_ist DESC)",
    ]
    with conn.cursor() as cur:
        for sql in stmts:
            try:
                cur.execute(sql)
            except Exception:
                # Functional index on JSON requires payload->>'job_id' to be IMMUTABLE; ignore on older PG.
                continue


def _ensure_query_performance_indexes_sqlite(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for sql in (
        "CREATE INDEX IF NOT EXISTS idx_interview_records_email_lower ON interview_records (LOWER(COALESCE(candidate_email, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_name_lower ON interview_records (LOWER(COALESCE(candidate_name, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_created_at_ist ON interview_records (created_at_ist DESC)",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_submitted_report ON interview_records (submitted, has_report)",
        "CREATE INDEX IF NOT EXISTS idx_interview_records_job_id ON interview_records (json_extract(payload, '$.job_id'))",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_hr_username ON interview_schedule (hr_username)",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_scheduled ON interview_schedule (scheduled_at_local DESC)",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_email_lower ON interview_schedule (LOWER(COALESCE(candidate_email, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_schedule_name_lower ON interview_schedule (LOWER(COALESCE(candidate_name, '')))",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_invite ON interview_progress (invite_token)",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_status ON interview_progress (status, report_status)",
        "CREATE INDEX IF NOT EXISTS idx_interview_progress_activity ON interview_progress (last_activity_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_job_templates_updated_at_ist ON job_templates (updated_at_ist DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_opportunity_master_value_lower ON opportunity_master (LOWER(opportunity_id))",
        "CREATE INDEX IF NOT EXISTS idx_opportunity_master_lookup ON opportunity_master (LOWER(opportunity_id), updated_at_ist DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_customer_master_value_lower ON customer_master (LOWER(customer_name))",
        "CREATE INDEX IF NOT EXISTS idx_customer_master_lookup ON customer_master (LOWER(customer_name), updated_at_ist DESC)",
    ):
        try:
            cur.execute(sql)
        except sqlite3.OperationalError:
            # JSON1 not available in some builds; skip the expression index.
            continue


def init_auth_db(db_target: DbTarget) -> None:
    if _is_postgres(db_target):
        _ensure_postgres_database(str(db_target))
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS registration_data (
                        id SERIAL PRIMARY KEY,
                        full_name TEXT NOT NULL,
                        email TEXT NOT NULL UNIQUE,
                        username TEXT NOT NULL UNIQUE,
                        role TEXT NOT NULL CHECK(role IN ('hr','candidate')),
                        password_hash TEXT NOT NULL,
                        password_salt TEXT NOT NULL,
                        created_at_ist TEXT NOT NULL,
                        created_date_ist TEXT NOT NULL,
                        created_time_ist TEXT NOT NULL,
                        -- 2 Sep 2026: verify_login SELECTs is_active, so a table
                        -- created without it made EVERY login fail on a fresh DB
                        -- ("no such column: is_active"). Deactivation is a real
                        -- feature (users_admin), so the column belongs here.
                        is_active BOOLEAN NOT NULL DEFAULT TRUE
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS login_data (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER,
                        username TEXT NOT NULL,
                        role TEXT,
                        success INTEGER NOT NULL,
                        message TEXT,
                        login_at_ist TEXT NOT NULL,
                        login_date_ist TEXT NOT NULL,
                        login_time_ist TEXT NOT NULL,
                        client_ip TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS interview_schedule (
                        id TEXT PRIMARY KEY,
                        hr_username TEXT NOT NULL,
                        candidate_name TEXT NOT NULL,
                        candidate_email TEXT NOT NULL,
                        scheduled_at_local TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        meeting_link TEXT,
                        invite_token TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'scheduled',
                        notes TEXT,
                        created_at_ist TEXT NOT NULL,
                        created_date_ist TEXT NOT NULL,
                        created_time_ist TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS interview_records (
                        id TEXT PRIMARY KEY,
                        candidate_name TEXT,
                        candidate_email TEXT,
                        created_at_ist TEXT,
                        updated_at_ist TEXT,
                        submitted BOOLEAN,
                        has_report BOOLEAN,
                        payload JSONB NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS job_templates (
                        job_id TEXT PRIMARY KEY,
                        job_title TEXT NOT NULL,
                        domain TEXT,
                        required_skills JSONB,
                        optional_skills JSONB,
                        opportunity_id TEXT NOT NULL DEFAULT '',
                        customer_name TEXT NOT NULL DEFAULT '',
                        exp_min INTEGER NOT NULL DEFAULT 0,
                        exp_max INTEGER NOT NULL DEFAULT 0,
                        difficulty TEXT NOT NULL DEFAULT 'medium',
                        num_q INTEGER NOT NULL DEFAULT 5,
                        followup_mode BOOLEAN NOT NULL DEFAULT TRUE,
                        interview_mode TEXT NOT NULL DEFAULT 'mock',
                        jd_text TEXT,
                        weights JSONB,
                        generated_prompt TEXT,
                        edited_prompt TEXT,
                        prompt_version INTEGER NOT NULL DEFAULT 1,
                        prompt_updated_by TEXT NOT NULL DEFAULT '',
                        prompt_updated_at TEXT NOT NULL DEFAULT '',
                        prompt_history JSONB NOT NULL DEFAULT '[]'::jsonb,
                        created_at_ist TEXT NOT NULL,
                        updated_at_ist TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS hr_candidate_decisions (
                        candidate_id TEXT PRIMARY KEY,
                        decision TEXT NOT NULL,
                        updated_at_ist TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        id SERIAL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        token_hash TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        used_at TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens (token_hash)"
                )
            _ensure_registration_columns_postgres(conn)
            _ensure_job_templates_columns_postgres(conn)
            _ensure_schedule_security_columns_postgres(conn)
            _ensure_interview_progress_table_postgres(conn)
            _ensure_master_tables_postgres(conn)
            _ensure_query_performance_indexes_postgres(conn)
            conn.commit()
        return

    db_path = Path(db_target)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect_sqlite(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS registration_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                username TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL CHECK(role IN ('hr','candidate')),
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                created_at_ist TEXT NOT NULL,
                created_date_ist TEXT NOT NULL,
                created_time_ist TEXT NOT NULL,
                -- See the Postgres twin above: verify_login SELECTs is_active.
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS login_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT NOT NULL,
                role TEXT,
                success INTEGER NOT NULL,
                message TEXT,
                login_at_ist TEXT NOT NULL,
                login_date_ist TEXT NOT NULL,
                login_time_ist TEXT NOT NULL,
                client_ip TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES registration_data(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interview_schedule (
                id TEXT PRIMARY KEY,
                hr_username TEXT NOT NULL,
                candidate_name TEXT NOT NULL,
                candidate_email TEXT NOT NULL,
                scheduled_at_local TEXT NOT NULL,
                provider TEXT NOT NULL,
                meeting_link TEXT,
                invite_token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'scheduled',
                notes TEXT,
                created_at_ist TEXT NOT NULL,
                created_date_ist TEXT NOT NULL,
                created_time_ist TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS interview_records (
                id TEXT PRIMARY KEY,
                candidate_name TEXT,
                candidate_email TEXT,
                created_at_ist TEXT,
                updated_at_ist TEXT,
                submitted INTEGER,
                has_report INTEGER,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS job_templates (
                job_id TEXT PRIMARY KEY,
                job_title TEXT NOT NULL,
                domain TEXT,
                required_skills TEXT,
                optional_skills TEXT,
                opportunity_id TEXT NOT NULL DEFAULT '',
                customer_name TEXT NOT NULL DEFAULT '',
                exp_min INTEGER NOT NULL DEFAULT 0,
                exp_max INTEGER NOT NULL DEFAULT 0,
                difficulty TEXT NOT NULL DEFAULT 'medium',
                num_q INTEGER NOT NULL DEFAULT 5,
                followup_mode INTEGER NOT NULL DEFAULT 1,
                interview_mode TEXT NOT NULL DEFAULT 'mock',
                jd_text TEXT,
                weights TEXT,
                generated_prompt TEXT,
                edited_prompt TEXT,
                prompt_version INTEGER NOT NULL DEFAULT 1,
                prompt_updated_by TEXT NOT NULL DEFAULT '',
                prompt_updated_at TEXT NOT NULL DEFAULT '',
                prompt_history TEXT NOT NULL DEFAULT '[]',
                created_at_ist TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS hr_candidate_decisions (
                candidate_id TEXT PRIMARY KEY,
                decision TEXT NOT NULL,
                updated_at_ist TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES registration_data(id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens (token_hash)"
        )
        _ensure_registration_columns_sqlite(conn)
        _ensure_job_templates_columns_sqlite(conn)
        _ensure_schedule_security_columns_sqlite(conn)
        _ensure_interview_progress_table_sqlite(conn)
        _ensure_master_tables_sqlite(conn)
        _ensure_query_performance_indexes_sqlite(conn)
        conn.commit()


def _normalized_manual_questions_for_job(value: Any) -> list[str]:
    """Parse manual interview questions from API/DB; trim, drop empties, dedupe case-insensitively, cap MANUAL_QUESTIONS_MAX."""
    if value is None:
        return []
    items: list[Any] = []
    if isinstance(value, list):
        items = list(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                items = parsed
            else:
                items = [ln.strip() for ln in s.splitlines()]
        except json.JSONDecodeError:
            items = [ln.strip() for ln in s.splitlines()]
    else:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        line = str(raw or "").strip()
        if not line:
            continue
        low = line.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(line)
    return out[:MANUAL_QUESTIONS_MAX]


def _coerce_question_type(raw: Any) -> str:
    s = str(raw or "dynamic").strip().lower()
    return s if s in ("dynamic", "manual") else "dynamic"


def _template_instructions_for_prompt(job: dict | None) -> str:
    """Dedicated template instructions for AI prompts; legacy templates may only have jdText."""
    if not isinstance(job, dict):
        return ""
    ti = str(job.get("templateInstructions") or job.get("template_instructions") or "").strip()
    if ti:
        return ti
    return str(job.get("jdText") or "").strip()


def _apply_prompt_defaults(job_row: dict) -> dict:
    out = dict(job_row or {})
    ctx = build_template_prompt_context(
        role=out.get("jobTitle", ""),
        experience=f"{int(out.get('expMin') or 0)}-{int(out.get('expMax') or 0)} years",
        required_skills=out.get("requiredSkills") or [],
        optional_skills=out.get("optionalSkills") or [],
        difficulty=out.get("difficulty", "medium"),
        interview_type=out.get("interviewMode", "technical"),
        customer_name=out.get("customerName", ""),
        opportunity_id=out.get("opportunityId", ""),
        template_instructions=_template_instructions_for_prompt(out),
        technology_stack=str((out.get("weights") or {}).get("intelligenceTechStack") or ""),
        interview_mode=out.get("interviewMode", "technical"),
    )
    generated = sanitize_prompt_input(str(out.get("generatedPrompt") or ""))
    if not generated:
        generated = build_default_template_prompt(ctx)
    edited = sanitize_prompt_input(str(out.get("editedPrompt") or ""))
    if edited == generated:
        edited = ""
    effective = edited or generated
    out["generatedPrompt"] = generated
    out["editedPrompt"] = edited
    out["effectivePrompt"] = effective
    out["promptPreview"] = render_prompt_preview(effective, ctx)
    out["promptCharCount"] = len(effective)
    return out


def upsert_job_template(db_target: DbTarget, job: dict) -> dict:
    now = _now_ist_parts()
    jid = str(job.get("jobId") or job.get("job_id") or "").strip()
    title = str(job.get("jobTitle") or job.get("job_title") or "").strip()
    if not title:
        raise ValueError("jobTitle is required.")
    if not jid:
        # stable id from title (short + deterministic)
        jid = hashlib.md5(title.encode("utf-8")).hexdigest()[:10]

    def _to_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            items = value
        else:
            items = [s.strip() for s in str(value).split(",")]
        out: list[str] = []
        seen: set[str] = set()
        for raw in items:
            s = str(raw or "").strip()
            if not s:
                continue
            low = s.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(s)
        return out

    payload = {
        "jobId": jid,
        "jobTitle": title,
        "domain": str(job.get("domain") or "").strip(),
        "opportunityId": " ".join(str(job.get("opportunityId") or job.get("opportunity_id") or "").strip().split()),
        "customerName": " ".join(str(job.get("customerName") or job.get("customer_name") or "").strip().split()),
        "requiredSkills": _to_list(job.get("requiredSkills")),
        "optionalSkills": _to_list(job.get("optionalSkills")),
        "expMin": int(job.get("expMin") or 0),
        "expMax": int(job.get("expMax") or 0),
        "difficulty": str(job.get("difficulty") or "medium").strip().lower() or "medium",
        "numQ": int(job.get("numQ") or job.get("num_q") or 5),
        # Adaptive follow-up UI removed; default OFF for new templates (DB column kept for compatibility).
        "followupMode": bool(job.get("followupMode")) if job.get("followupMode") is not None else bool(job.get("followup_mode", False)),
        "interviewMode": _storage_interview_mode(job.get("interviewMode") or job.get("interview_mode")),
        "timingMode": str(job.get("timingMode") or job.get("timing_mode") or "count").strip().lower() or "count",
        "timeLimitSec": int(job.get("timeLimitSec") or job.get("time_limit_sec") or 0),
        "micAlwaysOn": bool(job.get("micAlwaysOn")) if job.get("micAlwaysOn") is not None else bool(job.get("mic_always_on", False)),
        "showSpokenText": _bool_from_any(
            job.get("enableTranscriptInput")
            if job.get("enableTranscriptInput") is not None
            else job.get("enable_transcript_input")
            if job.get("enable_transcript_input") is not None
            else job.get("showSpokenText")
            if job.get("showSpokenText") is not None
            else job.get("show_spoken_text"),
            False,
        ),
        "jdText": str(job.get("jdText") or "").strip(),
        "templateInstructions": _template_instructions_for_prompt(job),
        "weights": job.get("weights") if isinstance(job.get("weights"), dict) else {},
        "updatedAtIst": now["ist_iso"],
    }
    prompt_context = build_template_prompt_context(
        role=payload["jobTitle"],
        experience=f"{int(payload.get('expMin') or 0)}-{int(payload.get('expMax') or 0)} years",
        required_skills=payload["requiredSkills"],
        optional_skills=payload["optionalSkills"],
        difficulty=payload["difficulty"],
        interview_type=payload["interviewMode"],
        customer_name=payload["customerName"],
        opportunity_id=payload["opportunityId"],
        template_instructions=payload["templateInstructions"],
        technology_stack=str((payload.get("weights") or {}).get("intelligenceTechStack") or ""),
        interview_mode=payload["interviewMode"],
    )
    generated_prompt = sanitize_prompt_input(
        str(job.get("generatedPrompt") or job.get("generated_prompt") or "").strip()
    ) or build_default_template_prompt(prompt_context)
    edited_prompt = sanitize_prompt_input(str(job.get("editedPrompt") or job.get("edited_prompt") or "").strip())
    if edited_prompt == generated_prompt:
        edited_prompt = ""
    prompt_version_raw = job.get("promptVersion") if job.get("promptVersion") is not None else job.get("prompt_version")
    try:
        prompt_version = max(1, int(prompt_version_raw or 1))
    except (TypeError, ValueError):
        prompt_version = 1
    prompt_updated_by = str(job.get("promptUpdatedBy") or job.get("prompt_updated_by") or job.get("createdBy") or "").strip()
    prompt_updated_at = str(job.get("promptUpdatedAt") or job.get("prompt_updated_at") or "").strip() or now_iso_utc()
    prompt_history = job.get("promptHistory") if isinstance(job.get("promptHistory"), list) else job.get("prompt_history")
    if not isinstance(prompt_history, list):
        prompt_history = []
    payload["generatedPrompt"] = generated_prompt
    payload["editedPrompt"] = edited_prompt
    payload["promptVersion"] = prompt_version
    payload["promptUpdatedBy"] = prompt_updated_by
    payload["promptUpdatedAt"] = prompt_updated_at
    payload["promptHistory"] = prompt_history[:50]

    qt = str(job.get("questionType") or job.get("question_type") or "dynamic").strip().lower()
    if qt not in ("dynamic", "manual"):
        qt = "dynamic"
    mq = _normalized_manual_questions_for_job(
        job.get("manualQuestions") if job.get("manualQuestions") is not None else job.get("manual_questions")
    )
    timing = str(job.get("timingMode") or job.get("timing_mode") or "count").strip().lower()
    if timing not in ("count", "time"):
        timing = "count"
    n = clamp_count_mode_questions(payload.get("numQ") or 5)
    if qt == "manual" and mq:
        n = min(n, len(mq))
    payload["numQ"] = n
    if payload["difficulty"] not in {"easy", "medium", "hard"}:
        payload["difficulty"] = "medium"
    # Persist mock|standard; API consumers receive canonical technical|hr below.
    if payload["interviewMode"] not in {"mock", "standard"}:
        payload["interviewMode"] = "mock"
    if payload["timingMode"] not in {"count", "time"}:
        payload["timingMode"] = "count"
    payload["timeLimitSec"] = max(0, min(int(payload["timeLimitSec"] or 0), 6 * 60 * 60))

    payload["questionType"] = qt
    payload["manualQuestions"] = mq
    created_by = str(job.get("createdBy") or job.get("created_by") or "").strip().lower()
    if payload["opportunityId"]:
        upsert_master_value(db_target, "opportunity", payload["opportunityId"], created_by)
    if payload["customerName"]:
        upsert_master_value(db_target, "customer", payload["customerName"], created_by)

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_templates
                    (job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history, created_at_ist, updated_at_ist)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO UPDATE SET
                      job_title = EXCLUDED.job_title,
                      domain = EXCLUDED.domain,
                      opportunity_id = EXCLUDED.opportunity_id,
                      customer_name = EXCLUDED.customer_name,
                      required_skills = EXCLUDED.required_skills,
                      optional_skills = EXCLUDED.optional_skills,
                      exp_min = EXCLUDED.exp_min,
                      exp_max = EXCLUDED.exp_max,
                      difficulty = EXCLUDED.difficulty,
                      num_q = EXCLUDED.num_q,
                      followup_mode = EXCLUDED.followup_mode,
                      interview_mode = EXCLUDED.interview_mode,
                      timing_mode = EXCLUDED.timing_mode,
                      time_limit_sec = EXCLUDED.time_limit_sec,
                      mic_always_on = EXCLUDED.mic_always_on,
                      show_spoken_text = EXCLUDED.show_spoken_text,
                      jd_text = EXCLUDED.jd_text,
                      template_instructions = EXCLUDED.template_instructions,
                      weights = EXCLUDED.weights,
                      question_type = EXCLUDED.question_type,
                      manual_questions = EXCLUDED.manual_questions,
                      generated_prompt = EXCLUDED.generated_prompt,
                      edited_prompt = EXCLUDED.edited_prompt,
                      prompt_version = EXCLUDED.prompt_version,
                      prompt_updated_by = EXCLUDED.prompt_updated_by,
                      prompt_updated_at = EXCLUDED.prompt_updated_at,
                      prompt_history = EXCLUDED.prompt_history,
                      updated_at_ist = EXCLUDED.updated_at_ist
                    """,
                    (
                        jid,
                        payload["jobTitle"],
                        payload["domain"],
                        payload["opportunityId"],
                        payload["customerName"],
                        json.dumps(payload["requiredSkills"], ensure_ascii=False),
                        json.dumps(payload["optionalSkills"], ensure_ascii=False),
                        payload["expMin"],
                        payload["expMax"],
                        payload["difficulty"],
                        int(payload["numQ"] or 5),
                        bool(payload["followupMode"]),
                        payload["interviewMode"],
                        payload["timingMode"],
                        int(payload["timeLimitSec"] or 0),
                        bool(payload["micAlwaysOn"]),
                        bool(payload["showSpokenText"]),
                        payload["jdText"],
                        payload["templateInstructions"],
                        json.dumps(payload["weights"], ensure_ascii=False),
                        payload["questionType"],
                        json.dumps(payload["manualQuestions"], ensure_ascii=False),
                        payload["generatedPrompt"],
                        payload["editedPrompt"],
                        int(payload["promptVersion"] or 1),
                        payload["promptUpdatedBy"],
                        payload["promptUpdatedAt"],
                        json.dumps(payload["promptHistory"], ensure_ascii=False),
                        now["ist_iso"],
                        now["ist_iso"],
                    ),
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT job_id, created_at_ist FROM job_templates WHERE job_id = ?", (jid,))
            row = cur.fetchone()
            created = row["created_at_ist"] if row else now["ist_iso"]
            cur.execute(
                """
                INSERT INTO job_templates
                (job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history, created_at_ist, updated_at_ist)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  job_title=excluded.job_title,
                  domain=excluded.domain,
                  opportunity_id=excluded.opportunity_id,
                  customer_name=excluded.customer_name,
                  required_skills=excluded.required_skills,
                  optional_skills=excluded.optional_skills,
                  exp_min=excluded.exp_min,
                  exp_max=excluded.exp_max,
                  difficulty=excluded.difficulty,
                  num_q=excluded.num_q,
                  followup_mode=excluded.followup_mode,
                  interview_mode=excluded.interview_mode,
                  timing_mode=excluded.timing_mode,
                  time_limit_sec=excluded.time_limit_sec,
                  mic_always_on=excluded.mic_always_on,
                  show_spoken_text=excluded.show_spoken_text,
                  jd_text=excluded.jd_text,
                  template_instructions=excluded.template_instructions,
                  weights=excluded.weights,
                  question_type=excluded.question_type,
                  manual_questions=excluded.manual_questions,
                  generated_prompt=excluded.generated_prompt,
                  edited_prompt=excluded.edited_prompt,
                  prompt_version=excluded.prompt_version,
                  prompt_updated_by=excluded.prompt_updated_by,
                  prompt_updated_at=excluded.prompt_updated_at,
                  prompt_history=excluded.prompt_history,
                  updated_at_ist=excluded.updated_at_ist
                """,
                (
                    jid,
                    payload["jobTitle"],
                    payload["domain"],
                    payload["opportunityId"],
                    payload["customerName"],
                    json.dumps(payload["requiredSkills"], ensure_ascii=False),
                    json.dumps(payload["optionalSkills"], ensure_ascii=False),
                    payload["expMin"],
                    payload["expMax"],
                    payload["difficulty"],
                    int(payload["numQ"] or 5),
                    1 if payload["followupMode"] else 0,
                    payload["interviewMode"],
                    payload["timingMode"],
                    int(payload["timeLimitSec"] or 0),
                    1 if payload["micAlwaysOn"] else 0,
                    1 if payload["showSpokenText"] else 0,
                    payload["jdText"],
                    payload["templateInstructions"],
                    json.dumps(payload["weights"], ensure_ascii=False),
                    payload["questionType"],
                    json.dumps(payload["manualQuestions"], ensure_ascii=False),
                    payload["generatedPrompt"],
                    payload["editedPrompt"],
                    int(payload["promptVersion"] or 1),
                    payload["promptUpdatedBy"],
                    payload["promptUpdatedAt"],
                    json.dumps(payload["promptHistory"], ensure_ascii=False),
                    created,
                    now["ist_iso"],
                ),
            )
            conn.commit()

    row = {
        "jobId": jid,
        "jobTitle": payload["jobTitle"],
        "domain": payload["domain"],
        "opportunityId": payload["opportunityId"],
        "customerName": payload["customerName"],
        "requiredSkills": payload["requiredSkills"],
        "optionalSkills": payload["optionalSkills"],
        "expMin": payload["expMin"],
        "expMax": payload["expMax"],
        "difficulty": payload["difficulty"],
        "numQ": int(payload["numQ"] or 5),
        "followupMode": bool(payload["followupMode"]),
        "interviewMode": _api_interview_mode(payload["interviewMode"]),
        "timingMode": payload["timingMode"],
        "timeLimitSec": int(payload["timeLimitSec"] or 0),
        "micAlwaysOn": bool(payload["micAlwaysOn"]),
        "showSpokenText": bool(payload["showSpokenText"]),
        "enableTranscriptInput": bool(payload["showSpokenText"]),
        "jdText": payload["jdText"],
        "weights": payload["weights"],
        "questionType": payload["questionType"],
        "manualQuestions": list(payload["manualQuestions"] or []),
        "generatedPrompt": payload["generatedPrompt"],
        "editedPrompt": payload["editedPrompt"],
        "promptVersion": int(payload["promptVersion"] or 1),
        "promptUpdatedBy": payload["promptUpdatedBy"],
        "promptUpdatedAt": payload["promptUpdatedAt"],
        "promptHistory": list(payload["promptHistory"] or []),
        "effectivePrompt": payload["editedPrompt"] or payload["generatedPrompt"],
        "promptPreview": render_prompt_preview(payload["editedPrompt"] or payload["generatedPrompt"], prompt_context),
    }
    return _apply_prompt_defaults(row)


def list_job_templates(db_target: DbTarget) -> list[dict]:
    out: list[dict] = []
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history
                    FROM job_templates
                    ORDER BY job_title ASC
                    """
                )
                rows = cur.fetchall() or []
        for r in rows:
            out.append(
                _apply_prompt_defaults({
                    "jobId": r.get("job_id", ""),
                    "jobTitle": r.get("job_title", ""),
                    "domain": r.get("domain", "") or "",
                    "opportunityId": r.get("opportunity_id", "") or "",
                    "customerName": r.get("customer_name", "") or "",
                    "requiredSkills": (r.get("required_skills") or []) if isinstance(r.get("required_skills"), list) else _safe_list_json(r.get("required_skills")),
                    "optionalSkills": (r.get("optional_skills") or []) if isinstance(r.get("optional_skills"), list) else _safe_list_json(r.get("optional_skills")),
                    "expMin": int(r.get("exp_min") or 0),
                    "expMax": int(r.get("exp_max") or 0),
                    "difficulty": str(r.get("difficulty") or "medium"),
                    "numQ": int(r.get("num_q") or 5),
                    "followupMode": bool(r.get("followup_mode")) if r.get("followup_mode") is not None else False,
                    "interviewMode": _api_interview_mode(str(r.get("interview_mode") or "mock")),
                    "timingMode": str(r.get("timing_mode") or "count"),
                    "timeLimitSec": int(r.get("time_limit_sec") or 0),
                    "micAlwaysOn": bool(r.get("mic_always_on")) if r.get("mic_always_on") is not None else False,
                    "showSpokenText": bool(r.get("show_spoken_text")) if r.get("show_spoken_text") is not None else False,
                    "enableTranscriptInput": bool(r.get("show_spoken_text")) if r.get("show_spoken_text") is not None else False,
                    "jdText": r.get("jd_text", "") or "",
                    "templateInstructions": str(r.get("template_instructions") or ""),
                    "weights": r.get("weights") if isinstance(r.get("weights"), dict) else _safe_dict_json(r.get("weights")),
                    "questionType": _coerce_question_type(r.get("question_type")),
                    "manualQuestions": _normalized_manual_questions_for_job(r.get("manual_questions")),
                    "generatedPrompt": str(r.get("generated_prompt") or ""),
                    "editedPrompt": str(r.get("edited_prompt") or ""),
                    "promptVersion": int(r.get("prompt_version") or 1),
                    "promptUpdatedBy": str(r.get("prompt_updated_by") or ""),
                    "promptUpdatedAt": str(r.get("prompt_updated_at") or ""),
                    "promptHistory": _safe_list_json(r.get("prompt_history")),
                })
            )
        return out

    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history
            FROM job_templates
            ORDER BY job_title ASC
            """
        )
        rows = cur.fetchall() or []
    for r in rows:
        out.append(
            _apply_prompt_defaults({
                "jobId": r["job_id"],
                "jobTitle": r["job_title"],
                "domain": r["domain"] or "",
                "opportunityId": r["opportunity_id"] if "opportunity_id" in r.keys() else "",
                "customerName": r["customer_name"] if "customer_name" in r.keys() else "",
                "requiredSkills": _safe_list_json(r["required_skills"]),
                "optionalSkills": _safe_list_json(r["optional_skills"]),
                "expMin": int(r["exp_min"] or 0),
                "expMax": int(r["exp_max"] or 0),
                "difficulty": str(r["difficulty"] or "medium") if "difficulty" in r.keys() else "medium",
                "numQ": int(r["num_q"] or 5) if "num_q" in r.keys() else 5,
                "followupMode": bool(int(r["followup_mode"] or 0)) if "followup_mode" in r.keys() else False,
                "interviewMode": _api_interview_mode(str(r["interview_mode"] or "mock") if "interview_mode" in r.keys() else "mock"),
                "timingMode": str(r["timing_mode"] or "count") if "timing_mode" in r.keys() else "count",
                "timeLimitSec": int(r["time_limit_sec"] or 0) if "time_limit_sec" in r.keys() else 0,
                "micAlwaysOn": bool(int(r["mic_always_on"] or 0)) if "mic_always_on" in r.keys() else False,
                "showSpokenText": bool(int(r["show_spoken_text"] or 0)) if "show_spoken_text" in r.keys() else False,
                "enableTranscriptInput": bool(int(r["show_spoken_text"] or 0)) if "show_spoken_text" in r.keys() else False,
                "jdText": r["jd_text"] or "",
                "templateInstructions": str(r["template_instructions"] or "") if "template_instructions" in r.keys() else "",
                "weights": _safe_dict_json(r["weights"]),
                "questionType": _coerce_question_type(r["question_type"] if "question_type" in r.keys() else None),
                "manualQuestions": _normalized_manual_questions_for_job(
                    r["manual_questions"] if "manual_questions" in r.keys() else None
                ),
                "generatedPrompt": str(r["generated_prompt"] or "") if "generated_prompt" in r.keys() else "",
                "editedPrompt": str(r["edited_prompt"] or "") if "edited_prompt" in r.keys() else "",
                "promptVersion": int(r["prompt_version"] or 1) if "prompt_version" in r.keys() else 1,
                "promptUpdatedBy": str(r["prompt_updated_by"] or "") if "prompt_updated_by" in r.keys() else "",
                "promptUpdatedAt": str(r["prompt_updated_at"] or "") if "prompt_updated_at" in r.keys() else "",
                "promptHistory": _safe_list_json(r["prompt_history"]) if "prompt_history" in r.keys() else [],
            })
        )
    return out


def get_job_template(db_target: DbTarget, job_id: str) -> dict | None:
    jid = str(job_id or "").strip()
    if not jid:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history
                    FROM job_templates
                    WHERE job_id = %s
                    """,
                    (jid,),
                )
                r = cur.fetchone()
        if not r:
            return None
        return _apply_prompt_defaults({
            "jobId": r.get("job_id", ""),
            "jobTitle": r.get("job_title", ""),
            "domain": r.get("domain", "") or "",
            "opportunityId": r.get("opportunity_id", "") or "",
            "customerName": r.get("customer_name", "") or "",
            "requiredSkills": (r.get("required_skills") or []) if isinstance(r.get("required_skills"), list) else _safe_list_json(r.get("required_skills")),
            "optionalSkills": (r.get("optional_skills") or []) if isinstance(r.get("optional_skills"), list) else _safe_list_json(r.get("optional_skills")),
            "expMin": int(r.get("exp_min") or 0),
            "expMax": int(r.get("exp_max") or 0),
            "difficulty": str(r.get("difficulty") or "medium"),
            "numQ": int(r.get("num_q") or 5),
            "followupMode": bool(r.get("followup_mode")) if r.get("followup_mode") is not None else False,
            "interviewMode": _api_interview_mode(str(r.get("interview_mode") or "mock")),
            "timingMode": str(r.get("timing_mode") or "count"),
            "timeLimitSec": int(r.get("time_limit_sec") or 0),
            "micAlwaysOn": bool(r.get("mic_always_on")) if r.get("mic_always_on") is not None else False,
            "showSpokenText": bool(r.get("show_spoken_text")) if r.get("show_spoken_text") is not None else False,
            "enableTranscriptInput": bool(r.get("show_spoken_text")) if r.get("show_spoken_text") is not None else False,
            "jdText": r.get("jd_text", "") or "",
            "templateInstructions": str(r.get("template_instructions") or ""),
            "weights": r.get("weights") if isinstance(r.get("weights"), dict) else _safe_dict_json(r.get("weights")),
            "questionType": _coerce_question_type(r.get("question_type")),
            "manualQuestions": _normalized_manual_questions_for_job(r.get("manual_questions")),
            "generatedPrompt": str(r.get("generated_prompt") or ""),
            "editedPrompt": str(r.get("edited_prompt") or ""),
            "promptVersion": int(r.get("prompt_version") or 1),
            "promptUpdatedBy": str(r.get("prompt_updated_by") or ""),
            "promptUpdatedAt": str(r.get("prompt_updated_at") or ""),
            "promptHistory": _safe_list_json(r.get("prompt_history")),
        })

    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT job_id, job_title, domain, opportunity_id, customer_name, required_skills, optional_skills, exp_min, exp_max, difficulty, num_q, followup_mode, interview_mode, timing_mode, time_limit_sec, mic_always_on, show_spoken_text, jd_text, template_instructions, weights, question_type, manual_questions, generated_prompt, edited_prompt, prompt_version, prompt_updated_by, prompt_updated_at, prompt_history
            FROM job_templates
            WHERE job_id = ?
            """,
            (jid,),
        )
        r = cur.fetchone()
    if not r:
        return None
    return _apply_prompt_defaults({
        "jobId": r["job_id"],
        "jobTitle": r["job_title"],
        "domain": r["domain"] or "",
        "opportunityId": r["opportunity_id"] if "opportunity_id" in r.keys() else "",
        "customerName": r["customer_name"] if "customer_name" in r.keys() else "",
        "requiredSkills": _safe_list_json(r["required_skills"]),
        "optionalSkills": _safe_list_json(r["optional_skills"]),
        "expMin": int(r["exp_min"] or 0),
        "expMax": int(r["exp_max"] or 0),
        "difficulty": str(r["difficulty"] or "medium") if "difficulty" in r.keys() else "medium",
        "numQ": int(r["num_q"] or 5) if "num_q" in r.keys() else 5,
        "followupMode": bool(int(r["followup_mode"] or 0)) if "followup_mode" in r.keys() else False,
        "interviewMode": _api_interview_mode(str(r["interview_mode"] or "mock") if "interview_mode" in r.keys() else "mock"),
        "timingMode": str(r["timing_mode"] or "count") if "timing_mode" in r.keys() else "count",
        "timeLimitSec": int(r["time_limit_sec"] or 0) if "time_limit_sec" in r.keys() else 0,
        "micAlwaysOn": bool(int(r["mic_always_on"] or 0)) if "mic_always_on" in r.keys() else False,
        "showSpokenText": bool(int(r["show_spoken_text"] or 0)) if "show_spoken_text" in r.keys() else False,
        "enableTranscriptInput": bool(int(r["show_spoken_text"] or 0)) if "show_spoken_text" in r.keys() else False,
        "jdText": r["jd_text"] or "",
        "templateInstructions": str(r["template_instructions"] or "") if "template_instructions" in r.keys() else "",
        "weights": _safe_dict_json(r["weights"]),
        "questionType": _coerce_question_type(r["question_type"] if "question_type" in r.keys() else None),
        "manualQuestions": _normalized_manual_questions_for_job(
            r["manual_questions"] if "manual_questions" in r.keys() else None
        ),
        "generatedPrompt": str(r["generated_prompt"] or "") if "generated_prompt" in r.keys() else "",
        "editedPrompt": str(r["edited_prompt"] or "") if "edited_prompt" in r.keys() else "",
        "promptVersion": int(r["prompt_version"] or 1) if "prompt_version" in r.keys() else 1,
        "promptUpdatedBy": str(r["prompt_updated_by"] or "") if "prompt_updated_by" in r.keys() else "",
        "promptUpdatedAt": str(r["prompt_updated_at"] or "") if "prompt_updated_at" in r.keys() else "",
        "promptHistory": _safe_list_json(r["prompt_history"]) if "prompt_history" in r.keys() else [],
    })


def delete_job_template(db_target: DbTarget, job_id: str) -> bool:
    jid = str(job_id or "").strip()
    if not jid:
        return False
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM job_templates WHERE job_id = %s", (jid,))
                deleted = cur.rowcount or 0
            conn.commit()
        return deleted > 0
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM job_templates WHERE job_id = ?", (jid,))
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted > 0


def delete_interview_record(db_target: DbTarget, interview_id: str) -> bool:
    rid = str(interview_id or "").strip()
    if not rid:
        return False
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM interview_records WHERE id = %s", (rid,))
                deleted = cur.rowcount or 0
            conn.commit()
        return deleted > 0
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM interview_records WHERE id = ?", (rid,))
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted > 0


def delete_interview_schedule(db_target: DbTarget, schedule_id: str, hr_username: str | None = None) -> bool:
    sid = str(schedule_id or "").strip()
    if not sid:
        return False
    uname = (hr_username or "").strip().lower()
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                if uname:
                    cur.execute("DELETE FROM interview_schedule WHERE id = %s AND hr_username = %s", (sid, uname))
                else:
                    cur.execute("DELETE FROM interview_schedule WHERE id = %s", (sid,))
                deleted = cur.rowcount or 0
            conn.commit()
        return deleted > 0
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        if uname:
            cur.execute("DELETE FROM interview_schedule WHERE id = ? AND hr_username = ?", (sid, uname))
        else:
            cur.execute("DELETE FROM interview_schedule WHERE id = ?", (sid,))
        deleted = cur.rowcount or 0
        conn.commit()
    return deleted > 0


def delete_interview_schedule_by_token(db_target: DbTarget, invite_token: str) -> int:
    token = str(invite_token or "").strip()
    if not token:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM interview_schedule WHERE invite_token = %s", (token,))
                deleted = cur.rowcount or 0
            conn.commit()
        return int(deleted)
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM interview_schedule WHERE invite_token = ?", (token,))
        deleted = cur.rowcount or 0
        conn.commit()
    return int(deleted)


# Valid HR candidate-level decisions (May 2026):
#   shortlist → candidate moves forward
#   reject    → candidate rejected
#   on_hold   → decision deferred — kept in the pipeline but flagged
# Older rows that only ever stored shortlist/reject continue to parse correctly
# because the storage column is a free-form string; we just normalize on read.
HR_CANDIDATE_DECISION_VALUES = ("shortlist", "reject", "on_hold")


def list_hr_candidate_decisions(db_target: DbTarget) -> dict[str, str]:
    """Lower-cased candidate key -> 'shortlist' / 'reject' / 'on_hold'."""
    out: dict[str, str] = {}
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                try:
                    cur.execute("SELECT candidate_id, decision FROM hr_candidate_decisions")
                    for row in cur.fetchall() or []:
                        ck = str(row.get("candidate_id") or "").strip().lower()
                        d = str(row.get("decision") or "").strip().lower()
                        if ck and d in HR_CANDIDATE_DECISION_VALUES:
                            out[ck] = d
                except Exception:
                    pass
        return out
    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT candidate_id, decision FROM hr_candidate_decisions").fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows or []:
            ck = str(row["candidate_id"] or "").strip().lower()
            d = str(row["decision"] or "").strip().lower()
            if ck and d in HR_CANDIDATE_DECISION_VALUES:
                out[ck] = d
    return out


def get_hr_candidate_decision(db_target: DbTarget, candidate_id: str) -> str | None:
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "SELECT decision FROM hr_candidate_decisions WHERE candidate_id = %s LIMIT 1",
                        (cid,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    d = str(row[0] or "").strip().lower()
                    return d if d in HR_CANDIDATE_DECISION_VALUES else None
                except Exception:
                    return None
    with _connect_sqlite(Path(db_target)) as conn:
        try:
            row = conn.execute(
                "SELECT decision FROM hr_candidate_decisions WHERE candidate_id = ? LIMIT 1",
                (cid,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if not row:
            return None
        d = str(row[0] or "").strip().lower()
        return d if d in HR_CANDIDATE_DECISION_VALUES else None


def set_hr_candidate_decision(db_target: DbTarget, candidate_id: str, decision: str | None) -> None:
    """Persist HR shortlist/reject/on_hold for a dashboard candidate key; None clears the mark."""
    cid = (candidate_id or "").strip().lower()
    if not cid:
        raise ValueError("candidate_id is required")
    raw = (decision or "").strip().lower().replace(" ", "_").replace("-", "_")
    if raw in ("", "null", "none", "clear"):
        raw = ""
    # Accept the variant "hold" as a shorthand for on_hold.
    if raw == "hold":
        raw = "on_hold"
    if raw and raw not in HR_CANDIDATE_DECISION_VALUES:
        raise ValueError("decision must be 'shortlist', 'reject', 'on_hold', or null")
    ist = _now_ist_parts()["ist_iso"]
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                if not raw:
                    cur.execute("DELETE FROM hr_candidate_decisions WHERE candidate_id = %s", (cid,))
                else:
                    cur.execute(
                        """
                        INSERT INTO hr_candidate_decisions (candidate_id, decision, updated_at_ist)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (candidate_id) DO UPDATE SET
                            decision = EXCLUDED.decision,
                            updated_at_ist = EXCLUDED.updated_at_ist
                        """,
                        (cid, raw, ist),
                    )
            conn.commit()
        return
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        if not raw:
            cur.execute("DELETE FROM hr_candidate_decisions WHERE candidate_id = ?", (cid,))
        else:
            cur.execute(
                """
                INSERT INTO hr_candidate_decisions (candidate_id, decision, updated_at_ist)
                VALUES (?, ?, ?)
                ON CONFLICT(candidate_id) DO UPDATE SET
                    decision = excluded.decision,
                    updated_at_ist = excluded.updated_at_ist
                """,
                (cid, raw, ist),
            )
        conn.commit()


def recent_questions_for_job_template(
    db_target: DbTarget, job_id: str, *, limit: int = 80
) -> list[str]:
    """
    Collect question text from prior interviews that used the same job template.

    Used to reduce template-level repetition when generating new sessions.
    """
    jid = str(job_id or "").strip()
    if not jid:
        return []
    cap = max(1, min(int(limit or 80), 200))
    out: list[str] = []
    seen: set[str] = set()

    def _add_question(q: object) -> None:
        text = " ".join(str(q or "").strip().split())
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        out.append(text)

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                try:
                    cur.execute(
                        """
                        SELECT payload
                        FROM interview_records
                        WHERE payload->>'job_id' = %s
                        ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC
                        LIMIT %s
                        """,
                        (jid, cap * 2),
                    )
                    rows = cur.fetchall() or []
                except Exception:
                    rows = []
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    """
                    SELECT payload
                    FROM interview_records
                    WHERE json_extract(payload, '$.job_id') = ?
                    ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC
                    LIMIT ?
                    """,
                    (jid, cap * 2),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []

    for row in rows or []:
        payload = row["payload"] if not _is_postgres(db_target) else row.get("payload")
        rec: dict | None = None
        if isinstance(payload, dict):
            rec = payload
        elif isinstance(payload, str):
            try:
                parsed = json.loads(payload or "")
                if isinstance(parsed, dict):
                    rec = parsed
            except Exception:
                rec = None
        if not rec:
            continue
        for q in rec.get("questions") or []:
            _add_question(q)
            if len(out) >= cap:
                return out
    return out


def list_interview_records_for_candidate(
    db_target: DbTarget, candidate_id: str
) -> list[dict]:
    """
    Return every interview record matching a candidate identifier.

    The frontend uses the lower-cased candidate email (or name when email is
    missing) as the candidate id, so we resolve both shapes here. Records are
    returned newest-first so callers can stream the timeline directly.
    """
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return []
    out: list[dict] = []
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, candidate_name, candidate_email, created_at_ist, updated_at_ist, payload
                    FROM interview_records
                    WHERE LOWER(COALESCE(candidate_email, '')) = %s
                       OR LOWER(COALESCE(candidate_name, '')) = %s
                    ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC
                    """,
                    (cid, cid),
                )
                rows = cur.fetchall() or []
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, candidate_name, candidate_email, created_at_ist, updated_at_ist, payload
                FROM interview_records
                WHERE LOWER(COALESCE(candidate_email, '')) = ?
                   OR LOWER(COALESCE(candidate_name, '')) = ?
                ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC
                """,
                (cid, cid),
            ).fetchall()
    for row in rows or []:
        payload = row["payload"] if not _is_postgres(db_target) else row.get("payload")
        rec: dict | None = None
        if isinstance(payload, dict):
            rec = payload
        elif isinstance(payload, str):
            try:
                parsed = json.loads(payload or "")
                if isinstance(parsed, dict):
                    rec = parsed
            except Exception:
                rec = None
        if not rec:
            continue
        if "id" not in rec:
            rec["id"] = row["id"] if not _is_postgres(db_target) else row.get("id", "")
        out.append(rec)
    return out


def delete_interview_records_for_candidate(
    db_target: DbTarget, candidate_id: str
) -> int:
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM interview_records
                    WHERE LOWER(COALESCE(candidate_email, '')) = %s
                       OR LOWER(COALESCE(candidate_name, '')) = %s
                    """,
                    (cid, cid),
                )
                deleted = cur.rowcount or 0
            conn.commit()
        return int(deleted)
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM interview_records
            WHERE LOWER(COALESCE(candidate_email, '')) = ?
               OR LOWER(COALESCE(candidate_name, '')) = ?
            """,
            (cid, cid),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    return int(deleted)


def delete_interview_schedules_for_candidate(
    db_target: DbTarget, candidate_id: str
) -> int:
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM interview_schedule
                    WHERE LOWER(COALESCE(candidate_email, '')) = %s
                       OR LOWER(COALESCE(candidate_name, '')) = %s
                    """,
                    (cid, cid),
                )
                deleted = cur.rowcount or 0
            conn.commit()
        return int(deleted)
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM interview_schedule
            WHERE LOWER(COALESCE(candidate_email, '')) = ?
               OR LOWER(COALESCE(candidate_name, '')) = ?
            """,
            (cid, cid),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    return int(deleted)


def delete_login_data_for_candidate(
    db_target: DbTarget, candidate_id: str
) -> int:
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM login_data
                    WHERE LOWER(COALESCE(username, '')) = %s
                       AND COALESCE(role, 'candidate') = 'candidate'
                    """,
                    (cid,),
                )
                deleted = cur.rowcount or 0
            conn.commit()
        return int(deleted)
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM login_data
            WHERE LOWER(COALESCE(username, '')) = ?
               AND COALESCE(role, 'candidate') = 'candidate'
            """,
            (cid,),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    return int(deleted)


def delete_candidate_registration(db_target: DbTarget, candidate_id: str) -> int:
    """Remove a candidate-role login from registration_data. HR users are protected."""
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM registration_data
                    WHERE role = 'candidate'
                      AND (LOWER(email) = %s OR LOWER(username) = %s)
                    """,
                    (cid, cid),
                )
                deleted = cur.rowcount or 0
            conn.commit()
        return int(deleted)
    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM registration_data
            WHERE role = 'candidate'
              AND (LOWER(email) = ? OR LOWER(username) = ?)
            """,
            (cid, cid),
        )
        deleted = cur.rowcount or 0
        conn.commit()
    return int(deleted)


def cascade_delete_candidate(db_target: DbTarget, candidate_id: str) -> dict:
    """
    Atomic cascading deletion across every candidate-owned table.

    Wraps the whole sweep in a single transaction so partial failure leaves
    the database untouched (transaction rollback requirement).
    """
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return {"interview_records": 0, "interview_schedule": 0, "login_data": 0, "registration_data": 0}

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM interview_records
                    WHERE LOWER(COALESCE(candidate_email, '')) = %s
                       OR LOWER(COALESCE(candidate_name, '')) = %s
                    """,
                    (cid, cid),
                )
                rec_deleted = cur.rowcount or 0
                cur.execute(
                    """
                    DELETE FROM interview_schedule
                    WHERE LOWER(COALESCE(candidate_email, '')) = %s
                       OR LOWER(COALESCE(candidate_name, '')) = %s
                    """,
                    (cid, cid),
                )
                sch_deleted = cur.rowcount or 0
                cur.execute(
                    """
                    DELETE FROM login_data
                    WHERE LOWER(COALESCE(username, '')) = %s
                       AND COALESCE(role, 'candidate') = 'candidate'
                    """,
                    (cid,),
                )
                login_deleted = cur.rowcount or 0
                cur.execute(
                    """
                    DELETE FROM registration_data
                    WHERE role = 'candidate'
                      AND (LOWER(email) = %s OR LOWER(username) = %s)
                    """,
                    (cid, cid),
                )
                reg_deleted = cur.rowcount or 0
                try:
                    cur.execute("DELETE FROM hr_candidate_decisions WHERE candidate_id = %s", (cid,))
                except Exception:
                    pass
            conn.commit()
        return {
            "interview_records": int(rec_deleted),
            "interview_schedule": int(sch_deleted),
            "login_data": int(login_deleted),
            "registration_data": int(reg_deleted),
        }

    conn = _connect_sqlite(Path(db_target))
    try:
        conn.execute("BEGIN")
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM interview_records
            WHERE LOWER(COALESCE(candidate_email, '')) = ?
               OR LOWER(COALESCE(candidate_name, '')) = ?
            """,
            (cid, cid),
        )
        rec_deleted = cur.rowcount or 0
        cur.execute(
            """
            DELETE FROM interview_schedule
            WHERE LOWER(COALESCE(candidate_email, '')) = ?
               OR LOWER(COALESCE(candidate_name, '')) = ?
            """,
            (cid, cid),
        )
        sch_deleted = cur.rowcount or 0
        cur.execute(
            """
            DELETE FROM login_data
            WHERE LOWER(COALESCE(username, '')) = ?
               AND COALESCE(role, 'candidate') = 'candidate'
            """,
            (cid,),
        )
        login_deleted = cur.rowcount or 0
        cur.execute(
            """
            DELETE FROM registration_data
            WHERE role = 'candidate'
              AND (LOWER(email) = ? OR LOWER(username) = ?)
            """,
            (cid, cid),
        )
        reg_deleted = cur.rowcount or 0
        try:
            cur.execute("DELETE FROM hr_candidate_decisions WHERE candidate_id = ?", (cid,))
        except sqlite3.OperationalError:
            pass
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "interview_records": int(rec_deleted),
        "interview_schedule": int(sch_deleted),
        "login_data": int(login_deleted),
        "registration_data": int(reg_deleted),
    }


def _safe_list_json(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    try:
        parsed = json.loads(str(raw) or "[]")
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
    except Exception:
        return []
    return []


def _safe_dict_json(raw: Any) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw) or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _master_api_shape(kind: str, row: dict[str, Any]) -> dict:
    _, value_col, _ = _master_table_meta(kind)
    value = str(row.get(value_col) or row.get("value") or "").strip()
    base = {
        "id": str(row.get("id") or "").strip(),
        "value": value,
        "label": value,
        "createdBy": str(row.get("created_by") or "").strip(),
        "createdAtIst": str(row.get("created_at_ist") or "").strip(),
        "updatedAtIst": str(row.get("updated_at_ist") or "").strip(),
    }
    if value_col == "opportunity_id":
        base["opportunityId"] = value
    else:
        base["customerName"] = value
    return base


def upsert_master_value(db_target: DbTarget, kind: str, value: str, created_by: str = "") -> dict | None:
    clean = " ".join(str(value or "").strip().split())
    if not clean:
        return None
    table, value_col, prefix = _master_table_meta(kind)
    now = _now_ist_parts()["ist_iso"]
    mid = _stable_master_id(prefix, clean)
    creator = str(created_by or "").strip().lower()

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist FROM {table} WHERE LOWER({value_col}) = LOWER(%s) LIMIT 1", (clean,))
                existing = cur.fetchone()
                if existing:
                    cur.execute(f"UPDATE {table} SET updated_at_ist = %s WHERE id = %s", (now, existing["id"]))
                    existing["updated_at_ist"] = now
                    return _master_api_shape(kind, dict(existing))
                cur.execute(
                    f"""
                    INSERT INTO {table} (id, {value_col}, created_by, created_at_ist, updated_at_ist)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (mid, clean, creator, now, now),
                )
                cur.execute(f"SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist FROM {table} WHERE LOWER({value_col}) = LOWER(%s) LIMIT 1", (clean,))
                row = cur.fetchone()
            conn.commit()
        return _master_api_shape(kind, dict(row)) if row else None

    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist FROM {table} WHERE LOWER({value_col}) = LOWER(?) LIMIT 1",
            (clean,),
        ).fetchone()
        if row:
            cur.execute(f"UPDATE {table} SET updated_at_ist = ? WHERE id = ?", (now, row["id"]))
            conn.commit()
            shaped = dict(row)
            shaped["updated_at_ist"] = now
            return _master_api_shape(kind, shaped)
        cur.execute(
            f"""
            INSERT OR IGNORE INTO {table} (id, {value_col}, created_by, created_at_ist, updated_at_ist)
            VALUES (?, ?, ?, ?, ?)
            """,
            (mid, clean, creator, now, now),
        )
        row = cur.execute(
            f"SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist FROM {table} WHERE LOWER({value_col}) = LOWER(?) LIMIT 1",
            (clean,),
        ).fetchone()
        conn.commit()
    return _master_api_shape(kind, dict(row)) if row else None


def search_master_values(db_target: DbTarget, kind: str, query: str = "", limit: int = 20) -> list[dict]:
    table, value_col, _ = _master_table_meta(kind)
    q = " ".join(str(query or "").strip().lower().split())
    cap = max(1, min(int(limit or 20), 50))
    like = f"%{q}%"
    rows: list[dict] = []
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if q:
                    cur.execute(
                        f"""
                        SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist
                        FROM {table}
                        WHERE LOWER({value_col}) LIKE %s
                        ORDER BY CASE WHEN LOWER({value_col}) = %s THEN 0 WHEN LOWER({value_col}) LIKE %s THEN 1 ELSE 2 END,
                                 {value_col} ASC
                        LIMIT %s
                        """,
                        (like, q, f"{q}%", cap),
                    )
                else:
                    cur.execute(
                        f"""
                        SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist
                        FROM {table}
                        ORDER BY updated_at_ist DESC, {value_col} ASC
                        LIMIT %s
                        """,
                        (cap,),
                    )
                rows = [dict(r) for r in (cur.fetchall() or [])]
        return [_master_api_shape(kind, r) for r in rows]

    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        if q:
            fetched = conn.execute(
                f"""
                SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist
                FROM {table}
                WHERE LOWER({value_col}) LIKE ?
                ORDER BY CASE WHEN LOWER({value_col}) = ? THEN 0 WHEN LOWER({value_col}) LIKE ? THEN 1 ELSE 2 END,
                         {value_col} ASC
                LIMIT ?
                """,
                (like, q, f"{q}%", cap),
            ).fetchall()
        else:
            fetched = conn.execute(
                f"""
                SELECT id, {value_col}, created_by, created_at_ist, updated_at_ist
                FROM {table}
                ORDER BY updated_at_ist DESC, {value_col} ASC
                LIMIT ?
                """,
                (cap,),
            ).fetchall()
    return [_master_api_shape(kind, dict(r)) for r in (fetched or [])]


def search_candidate_suggestions(db_target: DbTarget, query: str = "", limit: int = 10) -> list[dict]:
    q = " ".join(str(query or "").strip().lower().split())
    cap = max(1, min(int(limit or 10), 25))
    like = f"%{q}%"
    rows: list[dict[str, Any]] = []

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                params: list[Any] = []
                where = ""
                if q:
                    where = "WHERE LOWER(COALESCE(name, '')) LIKE %s OR LOWER(COALESCE(email, '')) LIKE %s"
                    params.extend([like, like])
                cur.execute(
                    f"""
                    SELECT name, email, MAX(sort_at) AS sort_at
                    FROM (
                        SELECT candidate_name AS name, candidate_email AS email, COALESCE(updated_at_ist, created_at_ist, '') AS sort_at
                        FROM interview_records
                        UNION ALL
                        SELECT candidate_name AS name, candidate_email AS email, COALESCE(created_at_ist, '') AS sort_at
                        FROM interview_schedule
                        UNION ALL
                        SELECT full_name AS name, email AS email, COALESCE(created_at_ist, '') AS sort_at
                        FROM registration_data
                        WHERE role = 'candidate'
                    ) AS candidates
                    {where}
                    GROUP BY LOWER(COALESCE(email, '')), LOWER(COALESCE(name, '')), name, email
                    ORDER BY sort_at DESC NULLS LAST, name ASC
                    LIMIT %s
                    """,
                    (*params, cap * 3),
                )
                rows = [dict(r) for r in (cur.fetchall() or [])]
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.row_factory = sqlite3.Row
            params = []
            where = ""
            if q:
                where = "WHERE LOWER(COALESCE(name, '')) LIKE ? OR LOWER(COALESCE(email, '')) LIKE ?"
                params.extend([like, like])
            fetched = conn.execute(
                f"""
                SELECT name, email, MAX(sort_at) AS sort_at
                FROM (
                    SELECT candidate_name AS name, candidate_email AS email, COALESCE(updated_at_ist, created_at_ist, '') AS sort_at
                    FROM interview_records
                    UNION ALL
                    SELECT candidate_name AS name, candidate_email AS email, COALESCE(created_at_ist, '') AS sort_at
                    FROM interview_schedule
                    UNION ALL
                    SELECT full_name AS name, email AS email, COALESCE(created_at_ist, '') AS sort_at
                    FROM registration_data
                    WHERE role = 'candidate'
                ) AS candidates
                {where}
                GROUP BY LOWER(COALESCE(email, '')), LOWER(COALESCE(name, '')), name, email
                ORDER BY sort_at DESC, name ASC
                LIMIT ?
                """,
                (*params, cap * 3),
            ).fetchall()
            rows = [dict(r) for r in (fetched or [])]

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("name") or "").strip()
        email = str(row.get("email") or "").strip().lower()
        if not name and not email:
            continue
        key = email or name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name or email, "email": email, "label": f"{name} <{email}>" if name and email else (name or email)})
        if len(out) >= cap:
            break
    return out


def create_interview_schedule(
    db_target: DbTarget,
    hr_username: str,
    candidate_name: str,
    candidate_email: str,
    scheduled_at_local: str,
    provider: str = "karnex-link",
    meeting_link: str = "",
    notes: str = "",
) -> dict:
    if not candidate_name.strip():
        raise ValueError("Candidate name is required.")
    if not candidate_email.strip():
        raise ValueError("Candidate email is required.")
    if not scheduled_at_local.strip():
        raise ValueError("Interview date/time is required.")
    now = _now_ist_parts()
    schedule_id = str(uuid4())
    invite_token = secrets.token_urlsafe(18)
    access_key = secrets.token_urlsafe(6).upper()[:8]
    values = (
        schedule_id,
        (hr_username or "hr").strip().lower(),
        candidate_name.strip(),
        candidate_email.strip().lower(),
        scheduled_at_local.strip(),
        (provider or "karnex-link").strip().lower(),
        meeting_link.strip(),
        invite_token,
        "scheduled",
        notes.strip(),
        now["ist_iso"],
        now["ist_date"],
        now["ist_time"],
        access_key,
    )
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO interview_schedule
                    (id, hr_username, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, invite_token, status, notes, created_at_ist, created_date_ist, created_time_ist, access_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                """
                INSERT INTO interview_schedule
                (id, hr_username, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, invite_token, status, notes, created_at_ist, created_date_ist, created_time_ist, access_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()
    return {
        "id": schedule_id,
        "invite_token": invite_token,
        "access_key": access_key,
        "candidate_name": candidate_name.strip(),
        "candidate_email": candidate_email.strip().lower(),
        "scheduled_at_local": scheduled_at_local.strip(),
        "provider": (provider or "karnex-link").strip().lower(),
        "meeting_link": meeting_link.strip(),
        "status": "scheduled",
        "created_date_ist": now["ist_date"],
        "created_time_ist": now["ist_time"],
    }


def list_interview_schedules(db_target: DbTarget, hr_username: str) -> list[dict]:
    uname = (hr_username or "hr").strip().lower()
    cols = "id, hr_username, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, invite_token, status, notes, created_date_ist, created_time_ist, access_key, session_status, violation_count"
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT {cols}
                    FROM interview_schedule
                    WHERE hr_username = %s
                    ORDER BY created_at_ist DESC
                    """,
                    (uname,),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]
    with _connect_sqlite(Path(db_target)) as conn:
        try:
            rows = conn.execute(
                f"SELECT {cols} FROM interview_schedule WHERE hr_username = ? ORDER BY created_at_ist DESC",
                (uname,),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT id, hr_username, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, invite_token, status, notes, created_date_ist, created_time_ist FROM interview_schedule WHERE hr_username = ? ORDER BY created_at_ist DESC",
                (uname,),
            ).fetchall()
    return [dict(row) for row in rows]


def list_interview_integrity_logs(db_target: DbTarget, hr_username: str) -> list[dict]:
    """Single-query integrity payload for admin (avoids N+1 get_schedule_by_token)."""
    uname = (hr_username or "hr").strip().lower()
    cols = (
        "id, invite_token, status, notes, created_at_ist, candidate_name, candidate_email, scheduled_at_local, session_status, "
        "login_attempts, verified_at, interview_started_at, interview_completed_at, "
        "violation_count, violations_log, active_device_id"
    )
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT {cols}
                    FROM interview_schedule
                    WHERE hr_username = %s
                    ORDER BY scheduled_at_local DESC NULLS LAST, created_at_ist DESC
                    """,
                    (uname,),
                )
                rows = cur.fetchall()
        return [dict(row) for row in rows]
    with _connect_sqlite(Path(db_target)) as conn:
        try:
            rows = conn.execute(
                f"""
                SELECT {cols}
                FROM interview_schedule
                WHERE hr_username = ?
                ORDER BY scheduled_at_local DESC, created_at_ist DESC
                """,
                (uname,),
            ).fetchall()
        except Exception:
            rows = conn.execute(
                """
                SELECT id, invite_token, status, notes, created_at_ist, candidate_name, candidate_email, scheduled_at_local, session_status,
                       violation_count, violations_log
                FROM interview_schedule
                WHERE hr_username = ?
                ORDER BY created_at_ist DESC
                """,
                (uname,),
            ).fetchall()
    return [dict(row) for row in rows]


def get_schedule_by_token(db_target: DbTarget, invite_token: str) -> dict | None:
    token = (invite_token or "").strip()
    cols = "id, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, status, notes, access_key, session_status, active_device_id, login_attempts, verified_at, interview_started_at, interview_completed_at, violation_count, violations_log"
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    f"SELECT {cols} FROM interview_schedule WHERE invite_token = %s",
                    (token,),
                )
                row = cur.fetchone()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            try:
                row = conn.execute(
                    f"SELECT {cols} FROM interview_schedule WHERE invite_token = ?",
                    (token,),
                ).fetchone()
            except Exception:
                row = conn.execute(
                    "SELECT id, candidate_name, candidate_email, scheduled_at_local, provider, meeting_link, status, notes FROM interview_schedule WHERE invite_token = ?",
                    (token,),
                ).fetchone()
    if not row:
        return None
    return dict(row)


def get_schedules_by_tokens(db_target: DbTarget, invite_tokens) -> dict[str, dict]:
    """Bulk form of get_schedule_by_token, keyed by invite_token.

    The calendar renders a week of AI interviews at once, and the scheduled time
    for each lives on this legacy row rather than on ai_interview_links. Calling
    the single-token reader per event would be one round trip per interview —
    an easy 30-60 queries to draw one week.

    Never raises: a missing legacy row just means that event has no time, which
    the caller already has to handle.
    """
    tokens = [str(t).strip() for t in (invite_tokens or []) if str(t or "").strip()]
    if not tokens:
        return {}
    cols = ("invite_token, id, candidate_name, candidate_email, scheduled_at_local, provider, "
            "meeting_link, status, notes, access_key, session_status, verified_at, "
            "interview_started_at, interview_completed_at, violation_count, violations_log")
    fallback_cols = ("invite_token, id, candidate_name, candidate_email, scheduled_at_local, "
                     "provider, meeting_link, status, notes")
    rows: list = []
    try:
        if _is_postgres(db_target):
            with _connect_postgres(str(db_target)) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        f"SELECT {cols} FROM interview_schedule WHERE invite_token = ANY(%s)",
                        (tokens,),
                    )
                    rows = cur.fetchall()
        else:
            placeholders = ",".join("?" for _ in tokens)
            with _connect_sqlite(Path(db_target)) as conn:
                try:
                    rows = conn.execute(
                        f"SELECT {cols} FROM interview_schedule "
                        f"WHERE invite_token IN ({placeholders})",
                        tokens,
                    ).fetchall()
                except Exception:
                    # Older deployments predate some of these columns.
                    rows = conn.execute(
                        f"SELECT {fallback_cols} FROM interview_schedule "
                        f"WHERE invite_token IN ({placeholders})",
                        tokens,
                    ).fetchall()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for row in rows:
        data = dict(row)
        token = str(data.get("invite_token") or "").strip()
        if token:
            out[token] = data
    return out


def update_schedule_field(db_target: DbTarget, invite_token: str, **kwargs) -> None:
    """Update one or more fields on an interview_schedule row by invite_token."""
    token = (invite_token or "").strip()
    if not token or not kwargs:
        return
    if _is_postgres(db_target):
        sets = ", ".join(f"{k} = %s" for k in kwargs)
        vals = list(kwargs.values()) + [token]
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE interview_schedule SET {sets} WHERE invite_token = %s", vals)
            conn.commit()
    else:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [token]
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(f"UPDATE interview_schedule SET {sets} WHERE invite_token = ?", vals)
            conn.commit()


def _json_dumps_db(value: Any, fallback: Any) -> str:
    try:
        return json.dumps(value if value is not None else fallback, ensure_ascii=False)
    except Exception:
        return json.dumps(fallback, ensure_ascii=False)


def _json_loads_db(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return fallback
    try:
        parsed = json.loads(str(value))
        return parsed
    except Exception:
        return fallback


def _normalize_interview_progress_row(row: dict | sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    data = dict(row)
    data["questions"] = _json_loads_db(data.get("questions"), [])
    data["answers"] = _json_loads_db(data.get("answers"), [])
    data["meta"] = _json_loads_db(data.get("meta"), {})
    data["violations"] = _json_loads_db(data.get("violations"), [])
    data["payload"] = _json_loads_db(data.get("payload"), {})
    return data


def upsert_interview_progress(db_target: DbTarget, progress: dict) -> None:
    interview_id = str((progress or {}).get("interview_id") or "").strip()
    if not interview_id:
        return
    now = _now_ist_parts()["ist_iso"]
    meta = progress.get("meta") if isinstance(progress.get("meta"), dict) else {}
    candidate_profile = meta.get("candidate_profile") if isinstance(meta.get("candidate_profile"), dict) else {}
    candidate_name = str(progress.get("candidate_name") or candidate_profile.get("name") or "")
    candidate_email = str(progress.get("candidate_email") or candidate_profile.get("email") or "")
    payload = dict(progress.get("payload") or {})
    payload.setdefault("interview_id", interview_id)
    payload.setdefault("questions", progress.get("questions") or [])
    payload.setdefault("answers", progress.get("answers") or [])
    payload.setdefault("meta", meta)
    fields = {
        "interview_id": interview_id,
        "invite_token": str(progress.get("invite_token") or meta.get("invite_token") or "").strip(),
        "candidate_name": candidate_name,
        "candidate_email": candidate_email,
        "status": str(progress.get("status") or "started").strip() or "started",
        "current_index": int(progress.get("current_index") or 0),
        "questions": progress.get("questions") or [],
        "answers": progress.get("answers") or [],
        "meta": meta,
        "violations": progress.get("violations") or meta.get("violations") or [],
        "payload": payload,
        "last_activity_at": str(progress.get("last_activity_at") or now),
        "finalized_at": str(progress.get("finalized_at") or ""),
        "report_status": str(progress.get("report_status") or ""),
        "report_error": str(progress.get("report_error") or "")[:2000],
        "created_at_ist": str(progress.get("created_at_ist") or meta.get("created_at_ist") or now),
        "updated_at_ist": str(progress.get("updated_at_ist") or now),
    }
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO interview_progress
                    (interview_id, invite_token, candidate_name, candidate_email, status, current_index,
                     questions, answers, meta, violations, payload, last_activity_at, finalized_at,
                     report_status, report_error, created_at_ist, updated_at_ist)
                    VALUES (%s, NULLIF(%s, ''), %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (interview_id) DO UPDATE SET
                      invite_token=COALESCE(EXCLUDED.invite_token, interview_progress.invite_token),
                      candidate_name=EXCLUDED.candidate_name,
                      candidate_email=EXCLUDED.candidate_email,
                      status=EXCLUDED.status,
                      current_index=EXCLUDED.current_index,
                      questions=EXCLUDED.questions,
                      answers=EXCLUDED.answers,
                      meta=EXCLUDED.meta,
                      violations=EXCLUDED.violations,
                      payload=EXCLUDED.payload,
                      last_activity_at=EXCLUDED.last_activity_at,
                      finalized_at=EXCLUDED.finalized_at,
                      report_status=EXCLUDED.report_status,
                      report_error=EXCLUDED.report_error,
                      created_at_ist=COALESCE(NULLIF(interview_progress.created_at_ist, ''), EXCLUDED.created_at_ist),
                      updated_at_ist=EXCLUDED.updated_at_ist
                    """,
                    (
                        fields["interview_id"], fields["invite_token"], fields["candidate_name"], fields["candidate_email"],
                        fields["status"], fields["current_index"], _json_dumps_db(fields["questions"], []),
                        _json_dumps_db(fields["answers"], []), _json_dumps_db(fields["meta"], {}),
                        _json_dumps_db(fields["violations"], []), _json_dumps_db(fields["payload"], {}),
                        fields["last_activity_at"], fields["finalized_at"], fields["report_status"],
                        fields["report_error"], fields["created_at_ist"], fields["updated_at_ist"],
                    ),
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                """
                INSERT INTO interview_progress
                (interview_id, invite_token, candidate_name, candidate_email, status, current_index,
                 questions, answers, meta, violations, payload, last_activity_at, finalized_at,
                 report_status, report_error, created_at_ist, updated_at_ist)
                VALUES (?, NULLIF(?, ''), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(interview_id) DO UPDATE SET
                  invite_token=COALESCE(excluded.invite_token, interview_progress.invite_token),
                  candidate_name=excluded.candidate_name,
                  candidate_email=excluded.candidate_email,
                  status=excluded.status,
                  current_index=excluded.current_index,
                  questions=excluded.questions,
                  answers=excluded.answers,
                  meta=excluded.meta,
                  violations=excluded.violations,
                  payload=excluded.payload,
                  last_activity_at=excluded.last_activity_at,
                  finalized_at=excluded.finalized_at,
                  report_status=excluded.report_status,
                  report_error=excluded.report_error,
                  created_at_ist=COALESCE(NULLIF(interview_progress.created_at_ist, ''), excluded.created_at_ist),
                  updated_at_ist=excluded.updated_at_ist
                """,
                (
                    fields["interview_id"], fields["invite_token"], fields["candidate_name"], fields["candidate_email"],
                    fields["status"], fields["current_index"], _json_dumps_db(fields["questions"], []),
                    _json_dumps_db(fields["answers"], []), _json_dumps_db(fields["meta"], {}),
                    _json_dumps_db(fields["violations"], []), _json_dumps_db(fields["payload"], {}),
                    fields["last_activity_at"], fields["finalized_at"], fields["report_status"],
                    fields["report_error"], fields["created_at_ist"], fields["updated_at_ist"],
                ),
            )
            conn.commit()


def get_interview_progress_by_invite(db_target: DbTarget, invite_token: str) -> dict | None:
    token = str(invite_token or "").strip()
    if not token:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM interview_progress WHERE invite_token = %s LIMIT 1", (token,))
                return _normalize_interview_progress_row(cur.fetchone())
    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        return _normalize_interview_progress_row(
            conn.execute("SELECT * FROM interview_progress WHERE invite_token = ? LIMIT 1", (token,)).fetchone()
        )


def get_interview_progress_by_id(db_target: DbTarget, interview_id: str) -> dict | None:
    rid = str(interview_id or "").strip()
    if not rid:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM interview_progress WHERE interview_id = %s LIMIT 1", (rid,))
                return _normalize_interview_progress_row(cur.fetchone())
    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        return _normalize_interview_progress_row(
            conn.execute("SELECT * FROM interview_progress WHERE interview_id = ? LIMIT 1", (rid,)).fetchone()
        )


def list_recoverable_interview_progress(db_target: DbTarget, limit: int = 100) -> list[dict]:
    cap = max(1, min(int(limit or 100), 1000))
    statuses = ("started", "in_progress", "submitting", "terminated", "abandoned", "partially_completed")
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT * FROM interview_progress
                    WHERE status = ANY(%s) OR COALESCE(report_status, '') NOT IN ('ready', 'no_report_needed')
                    ORDER BY COALESCE(NULLIF(last_activity_at, ''), updated_at_ist, created_at_ist) ASC
                    LIMIT %s
                    """,
                    (list(statuses), cap),
                )
                rows = cur.fetchall() or []
    else:
        placeholders = ",".join("?" for _ in statuses)
        with _connect_sqlite(Path(db_target)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT * FROM interview_progress
                WHERE status IN ({placeholders}) OR COALESCE(report_status, '') NOT IN ('ready', 'no_report_needed')
                ORDER BY COALESCE(NULLIF(last_activity_at, ''), updated_at_ist, created_at_ist) ASC
                LIMIT ?
                """,
                (*statuses, cap),
            ).fetchall()
    return [r for r in (_normalize_interview_progress_row(row) for row in rows) if r]


def mark_interview_progress_report_status(
    db_target: DbTarget,
    interview_id: str,
    report_status: str,
    report_error: str = "",
    status: str | None = None,
    finalized_at: str | None = None,
) -> None:
    rid = str(interview_id or "").strip()
    if not rid:
        return
    now = _now_ist_parts()["ist_iso"]
    updates = {
        "report_status": str(report_status or ""),
        "report_error": str(report_error or "")[:2000],
        "updated_at_ist": now,
    }
    if status is not None:
        updates["status"] = str(status or "")
    if finalized_at is not None:
        updates["finalized_at"] = str(finalized_at or "")
    sets = ", ".join(f"{k} = %s" for k in updates)
    vals = list(updates.values()) + [rid]
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE interview_progress SET {sets} WHERE interview_id = %s", vals)
            conn.commit()
    else:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [rid]
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(f"UPDATE interview_progress SET {sets} WHERE interview_id = ?", vals)
            conn.commit()


def increment_schedule_login_attempts(db_target: DbTarget, invite_token: str) -> int:
    token = (invite_token or "").strip()
    if not token:
        return 0
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE interview_schedule
                    SET login_attempts = COALESCE(login_attempts, 0) + 1
                    WHERE invite_token = %s
                    RETURNING login_attempts
                    """,
                    (token,),
                )
                row = cur.fetchone()
            conn.commit()
            if row:
                return int(row[0] or 0)
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                "UPDATE interview_schedule SET login_attempts = COALESCE(login_attempts, 0) + 1 WHERE invite_token = ?",
                (token,),
            )
            row = conn.execute(
                "SELECT login_attempts FROM interview_schedule WHERE invite_token = ?",
                (token,),
            ).fetchone()
            conn.commit()
            if row:
                return int(row[0] or 0)
    return 0


def get_interview_record_payload(db_target: DbTarget, interview_id: str) -> dict | None:
    """
    Fetch the stored interview payload (questions, answers, report, etc.) by interview id.
    Returns the parsed payload dict or None if not found / not parseable.
    """
    rid = (interview_id or "").strip()
    if not rid:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT payload FROM interview_records WHERE id = %s LIMIT 1", (rid,))
                row = cur.fetchone() or {}
                payload = row.get("payload")
                if isinstance(payload, dict):
                    return payload
                if isinstance(payload, str):
                    try:
                        parsed = json.loads(payload)
                        return parsed if isinstance(parsed, dict) else None
                    except Exception:
                        return None
                return None
    with _connect_sqlite(Path(db_target)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT payload FROM interview_records WHERE id = ? LIMIT 1", (rid,)).fetchone()
        if not row:
            return None
        payload = row["payload"]
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                parsed = json.loads(payload)
                return parsed if isinstance(parsed, dict) else None
            except Exception:
                return None
        return None


def update_interview_hr_status(db_target: DbTarget, interview_id: str, status: str) -> dict | None:
    """
    Persist HR-facing interview status on the interview record payload (hr_interview_status).
    Returns the updated payload dict, or None if the record does not exist.
    """
    rid = (interview_id or "").strip()
    if not rid:
        raise ValueError("interview id is required")
    raw = (status or "").strip().lower().replace("_", " ")
    if raw in ("clear", "reset", "inherit", "null", "none"):
        st_norm: str | None = None
    elif raw in ("selected", "select"):
        st_norm = "Selected"
    elif raw in ("rejected", "reject"):
        st_norm = "Rejected"
    elif raw in ("pending review", "pending"):
        st_norm = "Pending Review"
    # May 2026: "On Hold" is a third pipeline outcome alongside Selected /
    # Rejected. Accept several spellings so HR APIs stay forgiving.
    elif raw in ("on hold", "hold", "onhold"):
        st_norm = "On Hold"
    else:
        raise ValueError("status must be selected, rejected, on_hold, pending_review, or clear")

    payload = get_interview_record_payload(db_target, rid)
    if not payload:
        return None
    now = _now_ist_parts()
    payload = dict(payload)
    payload.setdefault("id", rid)
    if st_norm is None:
        payload.pop("hr_interview_status", None)
    else:
        payload["hr_interview_status"] = st_norm
    payload["updated_at_ist"] = now["ist_iso"]

    candidate_name = str(payload.get("candidate_name") or "")
    candidate_email = str(payload.get("candidate_email") or "")
    submitted = bool(payload.get("submitted"))
    has_report = bool(payload.get("report"))

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE interview_records
                    SET payload = %s::jsonb,
                        updated_at_ist = %s,
                        candidate_name = COALESCE(NULLIF(%s, ''), candidate_name),
                        candidate_email = COALESCE(NULLIF(%s, ''), candidate_email),
                        submitted = %s,
                        has_report = %s
                    WHERE id = %s
                    """,
                    (
                        json.dumps(payload),
                        now["ist_iso"],
                        candidate_name,
                        candidate_email,
                        submitted,
                        has_report,
                        rid,
                    ),
                )
                if not cur.rowcount:
                    return None
            conn.commit()
        return payload

    with _connect_sqlite(Path(db_target)) as conn:
        cur = conn.execute(
            """
            UPDATE interview_records
            SET payload = ?,
                updated_at_ist = ?,
                candidate_name = COALESCE(NULLIF(?, ''), candidate_name),
                candidate_email = COALESCE(NULLIF(?, ''), candidate_email),
                submitted = ?,
                has_report = ?
            WHERE id = ?
            """,
            (
                json.dumps(payload),
                now["ist_iso"],
                candidate_name,
                candidate_email,
                1 if submitted else 0,
                1 if has_report else 0,
                rid,
            ),
        )
        if not cur.rowcount:
            conn.rollback()
            return None
        conn.commit()
    return payload


def _hash_password(password: str, salt_bytes: bytes) -> str:
    # Legacy KDF, retained only to verify old rows during migration.
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 120000)
    return digest.hex()


def _update_password_hash(db_target: DbTarget, user_id: int, new_hash: str) -> None:
    """Persist an upgraded password hash and clear the legacy salt column."""
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE registration_data SET password_hash = %s, password_salt = %s WHERE id = %s",
                    (new_hash, "", user_id),
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                "UPDATE registration_data SET password_hash = ?, password_salt = ? WHERE id = ?",
                (new_hash, "", user_id),
            )
            conn.commit()


def register_user(db_target: DbTarget, full_name: str, email: str, username: str, password: str, role: str) -> dict:
    role = (role or "").strip().lower()
    if role not in {"hr", "candidate"}:
        raise ValueError("Role must be HR or Candidate.")
    try:
        pwh.validate_password((password or "").strip())
    except pwh.PasswordPolicyError as exc:
        raise ValueError(str(exc)) from exc
    if not full_name.strip():
        raise ValueError("Full name is required.")
    if not username.strip():
        raise ValueError("Username is required.")
    if not email.strip():
        raise ValueError("Email is required.")

    now = _now_ist_parts()
    # Modern self-describing hash (bcrypt); the legacy salt column is unused now.
    pw_hash = pwh.hash_password(password)
    salt_hex = ""
    try:
        values = (
            full_name.strip(),
            email.strip().lower(),
            username.strip().lower(),
            role,
            pw_hash,
            salt_hex,
            now["ist_iso"],
            now["ist_date"],
            now["ist_time"],
        )
        if _is_postgres(db_target):
            with _connect_postgres(str(db_target)) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO registration_data
                        (full_name, email, username, role, password_hash, password_salt, created_at_ist, created_date_ist, created_time_ist)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        values,
                    )
                    user_id = cur.fetchone()[0]
                conn.commit()
        else:
            with _connect_sqlite(Path(db_target)) as conn:
                cur = conn.execute(
                    """
                    INSERT INTO registration_data
                    (full_name, email, username, role, password_hash, password_salt, created_at_ist, created_date_ist, created_time_ist)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                conn.commit()
                user_id = cur.lastrowid
    except (sqlite3.IntegrityError, psycopg2.Error) as err:
        msg = str(err).lower()
        if "username" in msg:
            raise ValueError("Username already exists.") from err
        if "email" in msg:
            raise ValueError("Email already exists.") from err
        raise ValueError("Registration failed due to duplicate data.") from err

    return {
        "id": user_id,
        "full_name": full_name.strip(),
        "email": email.strip().lower(),
        "username": username.strip().lower(),
        "role": role,
        "created_date_ist": now["ist_date"],
        "created_time_ist": now["ist_time"],
    }


def verify_login(db_target: DbTarget, username: str, password: str, client_ip: str = "unknown") -> dict:
    uname = (username or "").strip().lower()
    now = _now_ist_parts()
    if not uname or not password:
        _insert_login(db_target, None, uname or "unknown", None, 0, "Missing username/password", now, client_ip)
        return {"success": False, "message": "Username and password are required."}

    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, full_name, email, username, role, password_hash, password_salt,
                           COALESCE(is_active, TRUE) AS is_active
                    FROM registration_data
                    WHERE username = %s OR email = %s
                    """,
                    (uname, uname),
                )
                row = cur.fetchone()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            row = conn.execute(
                """
                SELECT id, full_name, email, username, role, password_hash, password_salt,
                       COALESCE(is_active, 1) AS is_active
                FROM registration_data
                WHERE username = ? OR email = ?
                """,
                (uname, uname),
            ).fetchone()
    if not row:
        _insert_login(db_target, None, uname, None, 0, "User not found", now, client_ip)
        return {"success": False, "message": "Invalid username or password."}

    if not bool(row["is_active"]):
        _insert_login(db_target, row["id"], uname, row["role"], 0, "Account deactivated", now, client_ip)
        return {"success": False, "message": "This account has been deactivated. Contact an Admin."}

    expected_hash = row["password_hash"]
    salt_hex = row["password_salt"]
    # Constant-time verify; supports both the modern format and legacy pbkdf2 rows.
    ok, needs_rehash = pwh.verify_password(password, expected_hash, salt_hex)
    if not ok:
        _insert_login(db_target, row["id"], uname, row["role"], 0, "Wrong password", now, client_ip)
        return {"success": False, "message": "Invalid username or password."}

    if needs_rehash:
        # Transparent migration: upgrade the stored hash on this successful login.
        try:
            _update_password_hash(db_target, row["id"], pwh.hash_password(password))
        except Exception:
            pass  # never block a valid login on an upgrade failure

    _insert_login(db_target, row["id"], uname, row["role"], 1, "Login success", now, client_ip)
    return {
        "success": True,
        "user": {
            "id": row["id"],
            "full_name": row["full_name"],
            "email": row["email"],
            "username": row["username"],
            "role": row["role"],
            "login_date_ist": now["ist_date"],
            "login_time_ist": now["ist_time"],
        },
    }


def _insert_login(
    db_target: DbTarget,
    user_id: int | None,
    username: str,
    role: str | None,
    success: int,
    message: str,
    now: dict[str, str],
    client_ip: str,
) -> None:
    values: tuple[Any, ...] = (
        user_id,
        username or "unknown",
        role,
        success,
        message,
        now["ist_iso"],
        now["ist_date"],
        now["ist_time"],
        client_ip or "unknown",
    )
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO login_data
                    (user_id, username, role, success, message, login_at_ist, login_date_ist, login_time_ist, client_ip)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                """
                INSERT INTO login_data
                (user_id, username, role, success, message, login_at_ist, login_date_ist, login_time_ist, client_ip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
            conn.commit()


def get_user_by_email(db_target: DbTarget, email: str) -> dict | None:
    """Look up a registration_data row by email (role-agnostic). Returns None when missing."""
    addr = (email or "").strip().lower()
    if not addr:
        return None
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, full_name, email, username, role
                    FROM registration_data
                    WHERE email = %s
                    """,
                    (addr,),
                )
                row = cur.fetchone()
        return dict(row) if row else None
    with _connect_sqlite(Path(db_target)) as conn:
        row = conn.execute(
            """
            SELECT id, full_name, email, username, role
            FROM registration_data
            WHERE email = ?
            """,
            (addr,),
        ).fetchone()
    return dict(row) if row else None


def create_password_reset(db_target: DbTarget, user_id: int, token_hash: str, expires_at: str) -> None:
    """Store a hashed password-reset token. ``expires_at`` is a UTC ISO-8601 string."""
    created_at = datetime.now(timezone.utc).isoformat()
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_id, token_hash, expires_at, created_at),
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                """
                INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, token_hash, expires_at, created_at),
            )
            conn.commit()


def consume_password_reset(db_target: DbTarget, token_hash: str) -> dict | None:
    """Atomically mark a valid (unused, unexpired) reset token as used.

    Returns ``{"id", "user_id"}`` when the token was valid, else None.
    Timestamps are same-format UTC ISO-8601 strings, so lexicographic
    comparison in SQL is chronologically correct.
    """
    if not (token_hash or "").strip():
        return None
    now_utc = datetime.now(timezone.utc).isoformat()
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    UPDATE password_reset_tokens
                    SET used_at = %s
                    WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s
                    RETURNING id, user_id
                    """,
                    (now_utc, token_hash, now_utc),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row) if row else None
    with _connect_sqlite(Path(db_target)) as conn:
        row = conn.execute(
            """
            SELECT id, user_id FROM password_reset_tokens
            WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?
            """,
            (token_hash, now_utc),
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (now_utc, row["id"]),
        )
        conn.commit()
        if cur.rowcount != 1:
            return None  # lost the race: another request consumed it first
        return {"id": row["id"], "user_id": row["user_id"]}


def update_user_password(db_target: DbTarget, user_id: int, new_hash: str) -> None:
    """Persist a new (modern, self-describing) password hash; clears the legacy salt column."""
    _update_password_hash(db_target, user_id, new_hash)


def upsert_interview_record_snapshot(db_target: DbTarget, record: dict) -> None:
    rid = str(record.get("id", "")).strip()
    if not rid:
        return
    candidate_name = str(record.get("candidate_name", ""))
    candidate_email = str(record.get("candidate_email", ""))
    created_at_ist = str(record.get("created_at_ist", ""))
    updated_at_ist = str(record.get("updated_at_ist", ""))
    submitted = bool(record.get("submitted"))
    has_report = bool(record.get("report"))
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO interview_records
                    (id, candidate_name, candidate_email, created_at_ist, updated_at_ist, submitted, has_report, payload)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id)
                    DO UPDATE SET
                      candidate_name = EXCLUDED.candidate_name,
                      candidate_email = EXCLUDED.candidate_email,
                      created_at_ist = EXCLUDED.created_at_ist,
                      updated_at_ist = EXCLUDED.updated_at_ist,
                      submitted = EXCLUDED.submitted,
                      has_report = EXCLUDED.has_report,
                      payload = EXCLUDED.payload
                    """,
                    (
                        rid,
                        candidate_name,
                        candidate_email,
                        created_at_ist,
                        updated_at_ist,
                        submitted,
                        has_report,
                        json.dumps(record),
                    ),
                )
            conn.commit()
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.execute(
                """
                INSERT INTO interview_records
                (id, candidate_name, candidate_email, created_at_ist, updated_at_ist, submitted, has_report, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  candidate_name=excluded.candidate_name,
                  candidate_email=excluded.candidate_email,
                  created_at_ist=excluded.created_at_ist,
                  updated_at_ist=excluded.updated_at_ist,
                  submitted=excluded.submitted,
                  has_report=excluded.has_report,
                  payload=excluded.payload
                """,
                (
                    rid,
                    candidate_name,
                    candidate_email,
                    created_at_ist,
                    updated_at_ist,
                    1 if submitted else 0,
                    1 if has_report else 0,
                    json.dumps(record),
                ),
            )
            conn.commit()


def bulk_import_interview_records(db_target: DbTarget, records: list[dict]) -> int:
    count = 0
    for item in records or []:
        rid = str((item or {}).get("id", "")).strip()
        if not rid:
            continue
        upsert_interview_record_snapshot(db_target, item)
        count += 1
    return count


#: Columns never returned by the diagnostic snapshot. Password material lets an
#: attacker crack accounts offline; invite_token + access_key together are the
#: full credential pair for entering a candidate's interview.
_SNAPSHOT_REDACTED = {
    "password_hash", "password_salt", "password", "reset_token",
    "invite_token", "access_key", "active_device_id",
}


def _redact_snapshot_row(row: dict) -> dict:
    """Replace secret columns with a marker, keeping the shape for the UI."""
    return {
        key: ("***redacted***" if key in _SNAPSHOT_REDACTED and value not in (None, "") else value)
        for key, value in dict(row).items()
    }


def get_database_snapshot(db_target: DbTarget, limit: int = 200) -> dict[str, Any]:
    safe_limit = max(1, min(int(limit or 200), 1000))
    tables = [
        "registration_data",
        "login_data",
        "interview_schedule",
        "interview_records",
        "interview_progress",
        "opportunity_master",
        "customer_master",
    ]
    snapshot: dict[str, Any] = {"target": str(db_target), "tables": {}}
    if _is_postgres(db_target):
        with _connect_postgres(str(db_target)) as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for table in tables:
                    cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                    count = int((cur.fetchone() or {}).get("c", 0))
                    if table == "interview_records":
                        cur.execute(
                            f"SELECT * FROM {table} ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC NULLS LAST LIMIT %s",
                            (safe_limit,),
                        )
                    elif table == "interview_progress":
                        cur.execute(
                            f"SELECT * FROM {table} ORDER BY COALESCE(NULLIF(last_activity_at, ''), updated_at_ist, created_at_ist) DESC NULLS LAST LIMIT %s",
                            (safe_limit,),
                        )
                    else:
                        cur.execute(f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT %s", (safe_limit,))
                    rows = [_redact_snapshot_row(r) for r in (cur.fetchall() or [])]
                    snapshot["tables"][table] = {"count": count, "rows": rows}
    else:
        with _connect_sqlite(Path(db_target)) as conn:
            conn.row_factory = sqlite3.Row
            for table in tables:
                count = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
                if table == "interview_records":
                    sql = f"SELECT * FROM {table} ORDER BY COALESCE(updated_at_ist, created_at_ist) DESC LIMIT ?"
                elif table == "interview_progress":
                    sql = f"SELECT * FROM {table} ORDER BY COALESCE(NULLIF(last_activity_at, ''), updated_at_ist, created_at_ist) DESC LIMIT ?"
                else:
                    sql = f"SELECT * FROM {table} ORDER BY 1 DESC LIMIT ?"
                rows = [_redact_snapshot_row(row) for row in conn.execute(sql, (safe_limit,)).fetchall()]
                snapshot["tables"][table] = {"count": count, "rows": rows}
    return snapshot
