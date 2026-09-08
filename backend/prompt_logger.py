"""
Centralized AI Prompt Logging System.

Captures every OpenAI API request/response with full prompt text, payload,
token usage, and timing data. Writes to both file-based logs and database.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


from paths import ROOT_DIR

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger("karnex.prompt_logger")

PROMPT_LOGS_DIR = ROOT_DIR / "logs" / "openai-prompts"

_SENSITIVE_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9]{20,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+[A-Za-z0-9\-._~+/]+=*)", re.IGNORECASE),
]

MAX_LOG_AGE_DAYS = int(os.getenv("PROMPT_LOG_RETENTION_DAYS", "30"))
MAX_RESPONSE_LOG_CHARS = 8000


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


PROMPT_LOG_ENABLED = _bool_env("PROMPT_LOG_ENABLED", True)
PROMPT_LOG_FILE_ENABLED = _bool_env("PROMPT_LOG_FILE_ENABLED", True)
PROMPT_LOG_DB_ENABLED = _bool_env("PROMPT_LOG_DB_ENABLED", True)
PROMPT_LOG_QUEUE_MAX = int(os.getenv("PROMPT_LOG_QUEUE_MAX", "1000"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ist() -> datetime:
    return datetime.now(IST)


def _mask_secrets(text: str) -> str:
    """Replace API keys and bearer tokens with masked versions."""
    masked = text
    for pat in _SENSITIVE_PATTERNS:
        masked = pat.sub(lambda m: m.group(0)[:8] + "****" + m.group(0)[-4:], masked)
    return masked


def _safe_json_serialize(obj: Any, max_chars: int = 0) -> str:
    try:
        raw = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        raw = str(obj)
    if max_chars and len(raw) > max_chars:
        raw = raw[:max_chars] + "...[truncated]"
    return _mask_secrets(raw)


def _truncate_for_db(text: str | None, limit: int = 50000) -> str:
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit] + "...[truncated]"
    return text


def _is_postgres(dsn: str) -> bool:
    return dsn.startswith("postgresql://") or dsn.startswith("postgres://")


# ---------------------------------------------------------------------------
# DB schema bootstrap
# ---------------------------------------------------------------------------

_POSTGRES_CREATE = """
CREATE TABLE IF NOT EXISTS ai_prompt_logs (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    template_name TEXT,
    candidate_id TEXT,
    candidate_name TEXT,
    candidate_role TEXT,
    interview_id TEXT,
    selected_skills TEXT,
    difficulty TEXT,
    call_type TEXT NOT NULL,
    model TEXT,
    system_prompt TEXT,
    user_prompt TEXT,
    final_prompt TEXT,
    request_payload TEXT,
    response_payload TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    temperature REAL,
    max_tokens INTEGER,
    response_time_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    error_log TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at_ist TEXT NOT NULL,
    created_date_ist TEXT NOT NULL,
    created_time_ist TEXT NOT NULL
)
"""

_SQLITE_CREATE = """
CREATE TABLE IF NOT EXISTS ai_prompt_logs (
    id TEXT PRIMARY KEY,
    template_id TEXT,
    template_name TEXT,
    candidate_id TEXT,
    candidate_name TEXT,
    candidate_role TEXT,
    interview_id TEXT,
    selected_skills TEXT,
    difficulty TEXT,
    call_type TEXT NOT NULL,
    model TEXT,
    system_prompt TEXT,
    user_prompt TEXT,
    final_prompt TEXT,
    request_payload TEXT,
    response_payload TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    temperature REAL,
    max_tokens INTEGER,
    response_time_ms INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    error_log TEXT,
    created_at TEXT NOT NULL,
    created_at_ist TEXT NOT NULL,
    created_date_ist TEXT NOT NULL,
    created_time_ist TEXT NOT NULL
)
"""

_INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_apl_call_type ON ai_prompt_logs (call_type)",
    "CREATE INDEX IF NOT EXISTS idx_apl_interview ON ai_prompt_logs (interview_id)",
    "CREATE INDEX IF NOT EXISTS idx_apl_candidate ON ai_prompt_logs (candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_apl_status ON ai_prompt_logs (status)",
    "CREATE INDEX IF NOT EXISTS idx_apl_created ON ai_prompt_logs (created_date_ist)",
    "CREATE INDEX IF NOT EXISTS idx_apl_model ON ai_prompt_logs (model)",
]


def init_prompt_log_table(db_target: str) -> None:
    """Create the ai_prompt_logs table if it doesn't exist."""
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(_POSTGRES_CREATE)
                    for stmt in _INDEX_STMTS:
                        cur.execute(stmt)
                conn.commit()
        else:
            db_path = Path(db_target)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(_SQLITE_CREATE)
                for stmt in _INDEX_STMTS:
                    conn.execute(stmt)
                conn.commit()
    except Exception as exc:
        logger.warning("Failed to init ai_prompt_logs table: %s", exc)


# ---------------------------------------------------------------------------
# File logging
# ---------------------------------------------------------------------------

def _write_file_log(log_entry: dict) -> Path | None:
    """Write a single prompt log to a date-partitioned JSON file."""
    try:
        now = _now_ist()
        day_dir = PROMPT_LOGS_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        call_type = log_entry.get("call_type", "unknown")
        interview_id = log_entry.get("interview_id", "")
        log_id = log_entry.get("id", uuid4().hex[:12])
        ts = now.strftime("%H%M%S")

        parts = [call_type]
        if interview_id:
            parts.append(interview_id[:20])
        parts.append(f"{ts}_{log_id[:8]}")
        filename = "_".join(parts) + ".json"

        filepath = day_dir / filename
        safe_entry = json.loads(_mask_secrets(json.dumps(log_entry, ensure_ascii=False, default=str)))
        filepath.write_text(
            json.dumps(safe_entry, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return filepath
    except Exception as exc:
        logger.warning("File prompt log write failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# DB logging
# ---------------------------------------------------------------------------

def _write_db_log(db_target: str, entry: dict) -> bool:
    """Insert a single prompt log row into the database."""
    cols = [
        "id", "template_id", "template_name", "candidate_id", "candidate_name",
        "candidate_role", "interview_id", "selected_skills", "difficulty",
        "call_type", "model", "system_prompt", "user_prompt", "final_prompt",
        "request_payload", "response_payload", "prompt_tokens", "completion_tokens",
        "total_tokens", "temperature", "max_tokens", "response_time_ms",
        "status", "error_log", "created_at", "created_at_ist",
        "created_date_ist", "created_time_ist",
    ]
    vals = [entry.get(c) for c in cols]

    try:
        if _is_postgres(db_target):
            placeholders = ", ".join(["%s"] * len(cols))
            sql = f"INSERT INTO ai_prompt_logs ({', '.join(cols)}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING"
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, vals)
                conn.commit()
        else:
            placeholders = ", ".join(["?"] * len(cols))
            sql = f"INSERT OR IGNORE INTO ai_prompt_logs ({', '.join(cols)}) VALUES ({placeholders})"
            with sqlite3.connect(str(db_target)) as conn:
                conn.execute(sql, vals)
                conn.commit()
        return True
    except Exception as exc:
        logger.warning("DB prompt log write failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API: log an OpenAI call
# ---------------------------------------------------------------------------

def log_openai_call(
    *,
    db_target: str = "",
    call_type: str,
    model: str = "",
    messages: list[dict] | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    request_payload: dict | None = None,
    response: Any = None,
    response_text: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    response_time_ms: int = 0,
    status: str = "success",
    error_log: str = "",
    template_id: str = "",
    template_name: str = "",
    candidate_id: str = "",
    candidate_name: str = "",
    candidate_role: str = "",
    interview_id: str = "",
    selected_skills: list[str] | None = None,
    difficulty: str = "",
) -> dict:
    """
    Log a single OpenAI API call to both file and database.
    Returns the log entry dict.
    """
    now = _now_ist()
    log_id = uuid4().hex[:16]

    system_prompt = ""
    user_prompt = ""
    final_prompt = ""
    if messages:
        sys_parts = []
        user_parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                content = "\n".join(text_parts)
            if role == "system":
                sys_parts.append(str(content))
            elif role == "user":
                user_parts.append(str(content))
        system_prompt = "\n---\n".join(sys_parts)
        user_prompt = "\n---\n".join(user_parts)
        final_prompt = "\n\n".join(
            f"[{m.get('role', 'unknown')}]\n{m.get('content', '')}" for m in messages
        )

    resp_text = response_text
    if not resp_text and response:
        try:
            if hasattr(response, "choices") and response.choices:
                resp_text = str(response.choices[0].message.content or "")
            elif isinstance(response, dict):
                resp_text = json.dumps(response, ensure_ascii=False, default=str)
            else:
                resp_text = str(response)
        except Exception:
            resp_text = str(response)[:2000]

    if not prompt_tokens and response and hasattr(response, "usage") and response.usage:
        prompt_tokens = getattr(response.usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(response.usage, "completion_tokens", 0) or 0
        total_tokens = getattr(response.usage, "total_tokens", 0) or 0

    entry = {
        "id": log_id,
        "template_id": template_id or "",
        "template_name": template_name or "",
        "candidate_id": candidate_id or "",
        "candidate_name": candidate_name or "",
        "candidate_role": candidate_role or "",
        "interview_id": interview_id or "",
        "selected_skills": ", ".join(selected_skills) if selected_skills else "",
        "difficulty": difficulty or "",
        "call_type": call_type,
        "model": model or "",
        "system_prompt": _truncate_for_db(system_prompt),
        "user_prompt": _truncate_for_db(user_prompt),
        "final_prompt": _truncate_for_db(final_prompt),
        "request_payload": _safe_json_serialize(request_payload or {}, MAX_RESPONSE_LOG_CHARS * 2),
        "response_payload": _truncate_for_db(_mask_secrets(resp_text), MAX_RESPONSE_LOG_CHARS),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_time_ms": response_time_ms,
        "status": status,
        "error_log": _truncate_for_db(error_log, 5000),
        "created_at": now.isoformat(),
        "created_at_ist": now.isoformat(),
        "created_date_ist": now.strftime("%Y-%m-%d"),
        "created_time_ist": now.strftime("%H:%M:%S"),
    }

    if not PROMPT_LOG_ENABLED:
        return entry

    _enqueue_log(entry, db_target)
    return entry


# ---------------------------------------------------------------------------
# Background worker (single-thread, bounded queue, drops on full)
# ---------------------------------------------------------------------------

_log_queue: "queue.Queue[tuple[dict, str] | None]" = queue.Queue(maxsize=PROMPT_LOG_QUEUE_MAX)
_log_worker: threading.Thread | None = None
_log_worker_lock = threading.Lock()
_log_dropped = 0


def _ensure_worker_started() -> None:
    global _log_worker
    if _log_worker is not None and _log_worker.is_alive():
        return
    with _log_worker_lock:
        if _log_worker is not None and _log_worker.is_alive():
            return
        t = threading.Thread(target=_worker_loop, name="prompt-log-writer", daemon=True)
        t.start()
        _log_worker = t


def _worker_loop() -> None:
    while True:
        try:
            item = _log_queue.get()
        except Exception:
            break
        if item is None:
            break
        entry, db_target = item
        try:
            if PROMPT_LOG_FILE_ENABLED:
                _write_file_log(entry)
            if PROMPT_LOG_DB_ENABLED and db_target:
                _write_db_log(db_target, entry)
        except Exception as exc:
            logger.warning("Background prompt-log write failed: %s", exc)
        finally:
            try:
                _log_queue.task_done()
            except Exception:
                pass


def _enqueue_log(entry: dict, db_target: str) -> None:
    global _log_dropped
    _ensure_worker_started()
    try:
        _log_queue.put_nowait((entry, db_target))
    except queue.Full:
        _log_dropped += 1
        if _log_dropped % 50 == 1:
            logger.warning("Prompt log queue full; dropped %d entries so far", _log_dropped)


def _shutdown_worker() -> None:
    global _log_worker
    if _log_worker is None:
        return
    try:
        _log_queue.put_nowait(None)
    except queue.Full:
        return
    try:
        _log_worker.join(timeout=2.0)
    except Exception:
        pass


atexit.register(_shutdown_worker)


def prompt_logger_status() -> dict:
    """Snapshot for /admin/ai/usage style endpoints."""
    return {
        "enabled": PROMPT_LOG_ENABLED,
        "file_enabled": PROMPT_LOG_FILE_ENABLED,
        "db_enabled": PROMPT_LOG_DB_ENABLED,
        "queue_size": _log_queue.qsize(),
        "queue_max": PROMPT_LOG_QUEUE_MAX,
        "dropped": _log_dropped,
        "worker_alive": bool(_log_worker and _log_worker.is_alive()),
    }


# ---------------------------------------------------------------------------
# Wrapped OpenAI call helper
# ---------------------------------------------------------------------------

def tracked_chat_completion(
    client,
    *,
    model: str,
    messages: list[dict],
    temperature: float | None = None,
    max_tokens: int | None = None,
    response_format: dict | None = None,
    #: OpenAI tool/function definitions. Passed straight through so callers that
    #: need tool-calling (Ask AI) stay inside the same logging + retry path
    #: instead of reaching for the raw client.
    tools: list[dict] | None = None,
    tool_choice: Any = None,
    call_type: str = "chat_completion",
    db_target: str = "",
    template_id: str = "",
    template_name: str = "",
    candidate_id: str = "",
    candidate_name: str = "",
    candidate_role: str = "",
    interview_id: str = "",
    selected_skills: list[str] | None = None,
    difficulty: str = "",
) -> Any:
    """
    Drop-in replacement for client.chat.completions.create() that adds logging.
    Returns the OpenAI response object (unchanged).
    """
    kwargs: dict[str, Any] = {"model": model, "messages": messages}
    if temperature is not None:
        kwargs["temperature"] = temperature
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if response_format is not None:
        kwargs["response_format"] = response_format
    if tools:
        kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice

    start = time.perf_counter()
    status = "success"
    error_log = ""
    response = None

    try:
        max_retries = max(0, min(4, int(os.getenv("OPENAI_RETRY_MAX", "2"))))
        retryable = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                response = client.chat.completions.create(**kwargs)
                return response
            except Exception as exc:
                last_exc = exc
                if attempt >= max_retries or type(exc).__name__ not in retryable:
                    raise
                delay = min(8.0, 0.4 * (2**attempt))
                time.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError("OpenAI call failed without exception")
    except Exception as exc:
        status = "failed"
        error_log = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
        raise
    finally:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        try:
            log_openai_call(
                db_target=db_target,
                call_type=call_type,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                request_payload=kwargs,
                response=response,
                response_time_ms=elapsed_ms,
                status=status,
                error_log=error_log,
                template_id=template_id,
                template_name=template_name,
                candidate_id=candidate_id,
                candidate_name=candidate_name,
                candidate_role=candidate_role,
                interview_id=interview_id,
                selected_skills=selected_skills,
                difficulty=difficulty,
            )
        except Exception as log_exc:
            logger.warning("Prompt logging failed (non-blocking): %s", log_exc)


# ---------------------------------------------------------------------------
# Query API for admin dashboard
# ---------------------------------------------------------------------------

def query_prompt_logs(
    db_target: str,
    *,
    call_type: str = "",
    model: str = "",
    status: str = "",
    candidate_id: str = "",
    interview_id: str = "",
    template_id: str = "",
    date_from: str = "",
    date_to: str = "",
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at_ist",
    sort_order: str = "desc",
) -> dict:
    """Query prompt logs with filtering, pagination, and sorting."""
    conditions: list[str] = []
    params: list[Any] = []
    is_pg = _is_postgres(db_target)
    ph = "%s" if is_pg else "?"

    if call_type:
        conditions.append(f"call_type = {ph}")
        params.append(call_type)
    if model:
        conditions.append(f"model = {ph}")
        params.append(model)
    if status:
        conditions.append(f"status = {ph}")
        params.append(status)
    if candidate_id:
        conditions.append(f"candidate_id = {ph}")
        params.append(candidate_id)
    if interview_id:
        conditions.append(f"interview_id = {ph}")
        params.append(interview_id)
    if template_id:
        conditions.append(f"template_id = {ph}")
        params.append(template_id)
    if date_from:
        conditions.append(f"created_date_ist >= {ph}")
        params.append(date_from)
    if date_to:
        conditions.append(f"created_date_ist <= {ph}")
        params.append(date_to)
    if search:
        like_val = f"%{search}%"
        conditions.append(
            f"(candidate_name LIKE {ph} OR template_name LIKE {ph} "
            f"OR user_prompt LIKE {ph} OR call_type LIKE {ph})"
        )
        params.extend([like_val, like_val, like_val, like_val])

    where = " AND ".join(conditions) if conditions else "1=1"
    allowed_sorts = {
        "created_at_ist", "response_time_ms", "total_tokens",
        "prompt_tokens", "call_type", "model", "status",
    }
    col = sort_by if sort_by in allowed_sorts else "created_at_ist"
    order = "ASC" if sort_order.lower() == "asc" else "DESC"

    count_sql = f"SELECT COUNT(*) FROM ai_prompt_logs WHERE {where}"
    data_sql = (
        f"SELECT * FROM ai_prompt_logs WHERE {where} "
        f"ORDER BY {col} {order} LIMIT {ph} OFFSET {ph}"
    )
    params_data = params + [limit, offset]

    try:
        if is_pg:
            import psycopg2 as pg
            from psycopg2.extras import RealDictCursor
            with pg.connect(db_target) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(count_sql, params)
                    total = cur.fetchone()["count"]
                    cur.execute(data_sql, params_data)
                    rows = [dict(r) for r in cur.fetchall()]
        else:
            with sqlite3.connect(str(db_target)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(count_sql, params)
                total = cur.fetchone()[0]
                cur.execute(data_sql, params_data)
                rows = [dict(r) for r in cur.fetchall()]

        for row in rows:
            for key in ("created_at",):
                if key in row and row[key] is not None:
                    row[key] = str(row[key])

        return {
            "logs": rows,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
        }
    except Exception as exc:
        logger.warning("query_prompt_logs failed: %s", exc)
        return {"logs": [], "total": 0, "limit": limit, "offset": offset, "has_more": False}


def get_prompt_log_by_id(db_target: str, log_id: str) -> dict | None:
    """Fetch a single prompt log by ID."""
    is_pg = _is_postgres(db_target)
    ph = "%s" if is_pg else "?"
    sql = f"SELECT * FROM ai_prompt_logs WHERE id = {ph}"
    try:
        if is_pg:
            import psycopg2 as pg
            from psycopg2.extras import RealDictCursor
            with pg.connect(db_target) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(sql, [log_id])
                    row = cur.fetchone()
                    return dict(row) if row else None
        else:
            with sqlite3.connect(str(db_target)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(sql, [log_id]).fetchone()
                return dict(row) if row else None
    except Exception as exc:
        logger.warning("get_prompt_log_by_id failed: %s", exc)
        return None


def get_token_usage_stats(db_target: str, days: int = 30) -> dict:
    """Aggregate token usage statistics for the admin dashboard."""
    is_pg = _is_postgres(db_target)
    ph = "%s" if is_pg else "?"
    cutoff = (_now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")

    queries = {
        "total_summary": f"""
            SELECT
                COUNT(*) as total_calls,
                COALESCE(SUM(prompt_tokens), 0) as total_prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as total_completion_tokens,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                COALESCE(AVG(response_time_ms), 0) as avg_response_ms,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_calls
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
        """,
        "by_call_type": f"""
            SELECT
                call_type,
                COUNT(*) as call_count,
                COALESCE(SUM(total_tokens), 0) as tokens,
                COALESCE(AVG(total_tokens), 0) as avg_tokens,
                COALESCE(AVG(response_time_ms), 0) as avg_response_ms
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
            GROUP BY call_type
            ORDER BY tokens DESC
        """,
        "by_model": f"""
            SELECT
                model,
                COUNT(*) as call_count,
                COALESCE(SUM(total_tokens), 0) as tokens
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
            GROUP BY model
            ORDER BY tokens DESC
        """,
        "by_date": f"""
            SELECT
                created_date_ist as date,
                COUNT(*) as call_count,
                COALESCE(SUM(total_tokens), 0) as tokens
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
            GROUP BY created_date_ist
            ORDER BY created_date_ist DESC
            LIMIT 30
        """,
        "most_expensive": f"""
            SELECT id, call_type, model, total_tokens, response_time_ms,
                   candidate_name, interview_id, created_at_ist
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
            ORDER BY total_tokens DESC
            LIMIT 10
        """,
        "slowest_calls": f"""
            SELECT id, call_type, model, total_tokens, response_time_ms,
                   candidate_name, interview_id, created_at_ist
            FROM ai_prompt_logs
            WHERE created_date_ist >= {ph}
            ORDER BY response_time_ms DESC
            LIMIT 10
        """,
    }

    results: dict[str, Any] = {}
    try:
        if is_pg:
            import psycopg2 as pg
            from psycopg2.extras import RealDictCursor
            with pg.connect(db_target) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    for key, sql in queries.items():
                        cur.execute(sql, [cutoff])
                        rows = cur.fetchall()
                        results[key] = [dict(r) for r in rows] if rows else []
        else:
            with sqlite3.connect(str(db_target)) as conn:
                conn.row_factory = sqlite3.Row
                for key, sql in queries.items():
                    rows = conn.execute(sql, [cutoff]).fetchall()
                    results[key] = [dict(r) for r in rows] if rows else []

        summary = results.get("total_summary", [{}])
        if summary:
            s = summary[0] if isinstance(summary, list) else summary
            for k in ("avg_response_ms", "avg_tokens"):
                if k in s and s[k] is not None:
                    s[k] = round(float(s[k]), 1)
            results["total_summary"] = s

        for key in ("by_call_type", "by_model"):
            for row in results.get(key, []):
                for k in ("avg_tokens", "avg_response_ms"):
                    if k in row and row[k] is not None:
                        row[k] = round(float(row[k]), 1)

    except Exception as exc:
        logger.warning("get_token_usage_stats failed: %s", exc)
        results = {"total_summary": {}, "by_call_type": [], "by_model": [], "by_date": [],
                    "most_expensive": [], "slowest_calls": []}

    return results


# ---------------------------------------------------------------------------
# Log cleanup / rotation
# ---------------------------------------------------------------------------

def cleanup_old_file_logs(max_age_days: int | None = None) -> int:
    """Remove file logs older than max_age_days. Returns count of removed files."""
    days = max_age_days if max_age_days is not None else MAX_LOG_AGE_DAYS
    if days <= 0:
        return 0
    cutoff = _now_ist() - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    removed = 0
    try:
        if not PROMPT_LOGS_DIR.exists():
            return 0
        for day_dir in sorted(PROMPT_LOGS_DIR.iterdir()):
            if not day_dir.is_dir():
                continue
            if day_dir.name < cutoff_str:
                import shutil
                shutil.rmtree(day_dir, ignore_errors=True)
                removed += 1
    except Exception as exc:
        logger.warning("cleanup_old_file_logs failed: %s", exc)
    return removed


def cleanup_old_db_logs(db_target: str, max_age_days: int | None = None) -> int:
    """Remove database logs older than max_age_days. Returns count of removed rows."""
    days = max_age_days if max_age_days is not None else MAX_LOG_AGE_DAYS
    if days <= 0:
        return 0
    cutoff = (_now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    is_pg = _is_postgres(db_target)
    ph = "%s" if is_pg else "?"
    sql = f"DELETE FROM ai_prompt_logs WHERE created_date_ist < {ph}"
    try:
        if is_pg:
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql, [cutoff])
                    removed = cur.rowcount
                conn.commit()
                return removed
        else:
            with sqlite3.connect(str(db_target)) as conn:
                cur = conn.execute(sql, [cutoff])
                removed = cur.rowcount
                conn.commit()
                return removed
    except Exception as exc:
        logger.warning("cleanup_old_db_logs failed: %s", exc)
        return 0


def get_distinct_values(db_target: str, column: str) -> list[str]:
    """Get distinct values for a column (for filter dropdowns)."""
    allowed = {"call_type", "model", "status", "difficulty", "template_name"}
    if column not in allowed:
        return []
    is_pg = _is_postgres(db_target)
    sql = f"SELECT DISTINCT {column} FROM ai_prompt_logs WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
    try:
        if is_pg:
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    return [str(r[0]) for r in cur.fetchall()]
        else:
            with sqlite3.connect(str(db_target)) as conn:
                return [str(r[0]) for r in conn.execute(sql).fetchall()]
    except Exception:
        return []
