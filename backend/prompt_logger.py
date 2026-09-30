"""
Centralized AI Prompt Logging System.

Captures every OpenAI API request/response with full prompt text, payload,
token usage, and timing data. Writes to both file-based logs and database.
"""
from __future__ import annotations

import atexit
import contextvars
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
# Interview attribution (28 Sep 2026)
# ---------------------------------------------------------------------------
# Every AI call an interview makes — question generation, follow-ups, per-turn
# and final evaluation, the spoken question (TTS), the candidate's speech
# (transcription), the report upgrade that runs after submit — is logged from
# somewhere deep in ai.py that has no idea which interview it serves. The
# Candidate column on AI Logs read "-" for every row, and per-interview cost was
# impossible. Rather than thread five extra parameters through fourteen call
# sites, the handlers that DO know the session set a context here and
# `log_openai_call` fills its blanks from it.
#
# A ContextVar follows the request through `run_in_threadpool` (anyio copies
# the context) but NOT into a bare `threading.Thread` — those callers capture
# `current_interview_context()` and re-enter it (see `tts_prewarm.prewarm_tts`).

#: Keys a context may carry — the prompt-log columns they land in.
INTERVIEW_CONTEXT_KEYS = ("interview_id", "candidate_id", "candidate_name",
                          "candidate_role", "template_id", "template_name")

_interview_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "karnex_interview_log_context", default=None)


def current_interview_context() -> dict:
    """The context in force (a copy), or {}."""
    return dict(_interview_ctx.get() or {})


def set_interview_context(**fields) -> contextvars.Token:
    """Set (or replace) the context for the rest of this task/thread.
    Blank values are dropped so a partial context never erases a fuller one."""
    clean = {k: str(v) for k, v in fields.items()
             if k in INTERVIEW_CONTEXT_KEYS and v not in (None, "")}
    return _interview_ctx.set(clean or None)


class interview_context:
    """`with interview_context(interview_id=..., candidate_name=...):` — set for
    the block, restored afterwards. Also usable as `interview_context(ctx_dict)`."""

    def __init__(self, ctx: dict | None = None, **fields):
        self._fields = {**(ctx or {}), **fields}
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = set_interview_context(**self._fields)
        return self

    def __exit__(self, *exc):
        if self._token is not None:
            _interview_ctx.reset(self._token)
        return False


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
    created_time_ist TEXT NOT NULL,
    audio_seconds REAL DEFAULT 0,
    cost_usd REAL
)
"""

#: Columns added after the table first shipped (28 Sep 2026): the audio length
#: behind a TTS / transcription call and the USD cost priced AT LOG TIME
#: (`services/ai_pricing`), so a later price change never rewrites history.
#: Added with ALTER on both dialects, the way `interview_schedule` grows.
_ADDED_COLUMNS: dict[str, str] = {
    "audio_seconds": "REAL DEFAULT 0",
    "cost_usd": "REAL",
}

_INDEX_STMTS = [
    "CREATE INDEX IF NOT EXISTS idx_apl_call_type ON ai_prompt_logs (call_type)",
    "CREATE INDEX IF NOT EXISTS idx_apl_interview ON ai_prompt_logs (interview_id)",
    "CREATE INDEX IF NOT EXISTS idx_apl_candidate ON ai_prompt_logs (candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_apl_status ON ai_prompt_logs (status)",
    "CREATE INDEX IF NOT EXISTS idx_apl_created ON ai_prompt_logs (created_date_ist)",
    "CREATE INDEX IF NOT EXISTS idx_apl_model ON ai_prompt_logs (model)",
]


def _ensure_added_columns_postgres(cur) -> None:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'ai_prompt_logs'"
    )
    existing = {str(r[0]) for r in (cur.fetchall() or [])}
    for col, ddl in _ADDED_COLUMNS.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE ai_prompt_logs ADD COLUMN {col} {ddl}")


def _ensure_added_columns_sqlite(conn: sqlite3.Connection) -> None:
    existing = {str(r[1]) for r in conn.execute("PRAGMA table_info(ai_prompt_logs)").fetchall()}
    for col, ddl in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE ai_prompt_logs ADD COLUMN {col} {ddl}")


def init_prompt_log_table(db_target: str) -> None:
    """Create the ai_prompt_logs table if it doesn't exist, add any newer
    columns, and price the rows written before costs were stored."""
    try:
        if _is_postgres(db_target):
            import psycopg2 as pg
            with pg.connect(db_target) as conn:
                with conn.cursor() as cur:
                    cur.execute(_POSTGRES_CREATE)
                    _ensure_added_columns_postgres(cur)
                    for stmt in _INDEX_STMTS:
                        cur.execute(stmt)
                conn.commit()
        else:
            db_path = Path(db_target)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(db_path)) as conn:
                conn.execute(_SQLITE_CREATE)
                _ensure_added_columns_sqlite(conn)
                for stmt in _INDEX_STMTS:
                    conn.execute(stmt)
                conn.commit()
        backfill_prompt_log_costs(db_target)
    except Exception as exc:
        logger.warning("Failed to init ai_prompt_logs table: %s", exc)


def backfill_prompt_log_costs(db_target: str) -> int:
    """Price every row with `cost_usd IS NULL` from its tokens at TODAY's rates —
    once, for the rows written before costs were stored. Idempotent: a priced
    row is never touched again, so live prices only ever apply to new calls."""
    from services.ai_pricing import estimate_cost_usd, pricing_table

    table = pricing_table()
    is_pg = _is_postgres(db_target)
    ph = "%s" if is_pg else "?"
    updated = 0
    try:
        conn = _connect(db_target)
        try:
            cur = conn.cursor()
            cur.execute("SELECT DISTINCT model, call_type FROM ai_prompt_logs WHERE cost_usd IS NULL")
            pairs = [(str(r[0] or ""), str(r[1] or "")) for r in (cur.fetchall() or [])]
            for model, call_type in pairs:
                unit_in = estimate_cost_usd(model=model, call_type=call_type, prompt_tokens=1_000_000, table=table)
                unit_out = estimate_cost_usd(model=model, call_type=call_type, completion_tokens=1_000_000, table=table)
                unit_audio = estimate_cost_usd(model=model, call_type=call_type, audio_seconds=60.0, table=table)
                cur.execute(
                    "UPDATE ai_prompt_logs SET cost_usd = "
                    f"(COALESCE(prompt_tokens, 0) * {ph} + COALESCE(completion_tokens, 0) * {ph}) / 1000000.0 "
                    f"+ COALESCE(audio_seconds, 0) / 60.0 * {ph} "
                    f"WHERE cost_usd IS NULL AND COALESCE(model, '') = {ph} AND COALESCE(call_type, '') = {ph}",
                    (unit_in, unit_out, unit_audio, model, call_type),
                )
                updated += int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Prompt-log cost backfill failed: %s", exc)
    return updated


def _connect(db_target: str):
    if _is_postgres(db_target):
        import psycopg2 as pg
        return pg.connect(db_target)
    return sqlite3.connect(str(db_target))


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
        "created_date_ist", "created_time_ist", "audio_seconds", "cost_usd",
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
    audio_seconds: float = 0.0,
    audio_tokens: int = 0,
) -> dict:
    """
    Log a single OpenAI API call to both file and database.
    Returns the log entry dict.

    Blank attribution fields (interview / candidate / template) are filled from
    the interview context in force; the USD cost is priced now and stored.
    """
    now = _now_ist()
    log_id = uuid4().hex[:16]
    ctx = current_interview_context()
    interview_id = interview_id or ctx.get("interview_id", "")
    candidate_id = candidate_id or ctx.get("candidate_id", "")
    candidate_name = candidate_name or ctx.get("candidate_name", "")
    candidate_role = candidate_role or ctx.get("candidate_role", "")
    template_id = template_id or ctx.get("template_id", "")
    template_name = template_name or ctx.get("template_name", "")

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

    cached_tokens = 0
    if response is not None and getattr(response, "usage", None):
        usage = response.usage
        if not prompt_tokens:
            prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
            completion_tokens = getattr(usage, "completion_tokens", 0) or 0
            total_tokens = getattr(usage, "total_tokens", 0) or 0
        # Cached prompt tokens are billed at a lower rate (28 Sep 2026): the
        # question template repeats across an interview, so this is not a rounding error.
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0

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
        "audio_seconds": round(float(audio_seconds or 0.0), 2),
        "cost_usd": _cost_now(model, call_type, prompt_tokens, completion_tokens, audio_seconds,
                              cached_tokens=cached_tokens, audio_tokens=audio_tokens),
    }

    if not PROMPT_LOG_ENABLED:
        return entry

    _enqueue_log(entry, db_target)
    return entry


def _cost_now(model: str, call_type: str, prompt_tokens: int, completion_tokens: int,
              audio_seconds: float, *, cached_tokens: int = 0, audio_tokens: int = 0) -> float:
    """Priced from the ONE price list; a failed call that returned no usage costs
    nothing. Never raises — a pricing bug must not lose the log."""
    try:
        from services.ai_pricing import estimate_cost_usd
        return estimate_cost_usd(model=model, call_type=call_type, prompt_tokens=prompt_tokens,
                                 completion_tokens=completion_tokens, cached_tokens=cached_tokens,
                                 audio_tokens=audio_tokens, audio_seconds=audio_seconds)
    except Exception:
        return 0.0


def log_audio_call(
    *,
    db_target: str = "",
    kind: str,
    model: str,
    text: str = "",
    audio_seconds: float = 0.0,
    audio_bytes: int = 0,
    response_time_ms: int = 0,
    status: str = "success",
    error_log: str = "",
    response: Any = None,
    source: str = "",
) -> dict:
    """Log a text-to-speech ("tts") or transcription ("transcribe") call.

    Priced by `services/ai_pricing`: per audio TOKEN when the count is known
    (a transcription response with `usage` — the gpt-4o-*-transcribe models —
    is exact), else per minute of `audio_seconds` — the caller's MEASURED
    length (the MP3 frames of a clip, the browser's recording clock); only
    when neither exists is TTS estimated from the text and transcription from
    the upload size. `source` distinguishes the live stream from a prewarm."""
    from services.ai_pricing import (
        stt_seconds_for_bytes, text_tokens_estimate, tts_seconds_for_text,
    )
    kind = "tts" if kind == "tts" else "transcribe"
    call_type = f"{kind}_{source}" if source else kind
    seconds = float(audio_seconds or 0.0)
    prompt_tokens = completion_tokens = audio_tokens = 0
    if kind == "tts":
        prompt_tokens = text_tokens_estimate(text)
        if seconds <= 0:
            seconds = tts_seconds_for_text(text)
    else:
        if seconds <= 0:
            seconds = stt_seconds_for_bytes(audio_bytes)
        usage = getattr(response, "usage", None) if response is not None else None
        if usage is not None:
            completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            details = getattr(usage, "input_token_details", None)
            audio_tokens = int(getattr(details, "audio_tokens", 0) or 0) if details is not None else 0
            if not audio_tokens:
                audio_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    return log_openai_call(
        db_target=db_target,
        call_type=call_type,
        model=model,
        messages=[{"role": "user", "content": text}] if text else None,
        response_text=text if kind == "transcribe" else "",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        response_time_ms=response_time_ms,
        status=status,
        error_log=error_log,
        audio_seconds=seconds,
        audio_tokens=audio_tokens,
    )


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


def prune_prompt_log_text(db_target: str, max_age_days: int | None = None) -> int:
    """Retention that keeps the MONEY (28 Sep 2026). The old cleanup DELETED rows
    older than `PROMPT_LOG_RETENTION_DAYS`, which would erase the per-interview
    cost history the CEO page reports across years. Only the heavy text —
    prompts, payloads, responses (up to 50 KB each) — is dropped from rows
    past the retention window; tokens, audio seconds, cost and attribution stay
    forever. Idempotent; returns the number of rows pruned this run."""
    days = max_age_days if max_age_days is not None else MAX_LOG_AGE_DAYS
    if days <= 0:
        return 0
    cutoff = (_now_ist() - timedelta(days=days)).strftime("%Y-%m-%d")
    ph = "%s" if _is_postgres(db_target) else "?"
    sql = (
        "UPDATE ai_prompt_logs SET system_prompt = NULL, user_prompt = NULL, final_prompt = NULL, "
        "request_payload = NULL, response_payload = NULL "
        f"WHERE created_date_ist < {ph} AND (final_prompt IS NOT NULL OR response_payload IS NOT NULL)"
    )
    try:
        conn = _connect(db_target)
        try:
            cur = conn.cursor()
            cur.execute(sql, [cutoff])
            pruned = int(cur.rowcount or 0)
            conn.commit()
            return pruned
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("prune_prompt_log_text failed: %s", exc)
        return 0
