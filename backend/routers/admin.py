"""Admin / observability routes: prompt logs + AI usage + cost summary.

These routes are read-only and isolated; moving them out of main.py keeps the
core app assembly small. URL paths are unchanged.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

import response_cache
from prompt_logger import (
    cleanup_old_db_logs,
    cleanup_old_file_logs,
    get_distinct_values,
    get_prompt_log_by_id,
    get_token_usage_stats,
    prompt_logger_status,
    query_prompt_logs,
)

logger = logging.getLogger("karnex.routers.admin")

router = APIRouter()

# Filled by configure() from main.py once dependencies are available.
_AUTH_DB_TARGET: str = ""
_REQUIRE_USER: Callable[[Request, set[str] | None], tuple[dict | None, Any]] | None = None

# Default pricing (USD per 1K tokens) for gpt-4o-mini family; override via env.
_AI_USAGE_PRICING = {
    "gpt-4o-mini": {"prompt": 0.00015, "completion": 0.00060},
    "gpt-4o": {"prompt": 0.00250, "completion": 0.01000},
    "gpt-4.1-mini": {"prompt": 0.00040, "completion": 0.00160},
    "gpt-4.1": {"prompt": 0.00300, "completion": 0.01200},
}


def configure(auth_db_target: str, require_user: Callable) -> None:
    """main.py calls this once at startup."""
    global _AUTH_DB_TARGET, _REQUIRE_USER
    _AUTH_DB_TARGET = auth_db_target
    _REQUIRE_USER = require_user


def _auth(request: Request) -> tuple[dict | None, Any]:
    if _REQUIRE_USER is None:
        return None, JSONResponse({"error": "Server not initialized."}, status_code=500)
    return _REQUIRE_USER(request, {"hr"})


def _pricing_table() -> dict[str, dict[str, float]]:
    table = dict(_AI_USAGE_PRICING)
    overrides = (os.getenv("OPENAI_PRICING_USD_PER_1K") or "").strip()
    if not overrides:
        return table
    try:
        data = json.loads(overrides)
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and "prompt" in v and "completion" in v:
                    table[str(k).lower()] = {
                        "prompt": float(v["prompt"]),
                        "completion": float(v["completion"]),
                    }
    except Exception:
        pass
    return table


def _price_for(name: str, table: dict[str, dict[str, float]]) -> dict[str, float]:
    key = (name or "").strip().lower()
    for prefix, prices in table.items():
        if key.startswith(prefix):
            return prices
    return table.get("gpt-4o-mini", _AI_USAGE_PRICING["gpt-4o-mini"])


@router.get("/api/prompt-logs")
def api_prompt_logs(
    request: Request,
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
):
    _, err = _auth(request)
    if err:
        return err
    return query_prompt_logs(
        _AUTH_DB_TARGET,
        call_type=call_type,
        model=model,
        status=status,
        candidate_id=candidate_id,
        interview_id=interview_id,
        template_id=template_id,
        date_from=date_from,
        date_to=date_to,
        search=search,
        limit=max(1, min(int(limit or 50), 200)),
        offset=max(0, int(offset or 0)),
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/api/prompt-logs/stats")
def api_prompt_logs_stats(request: Request, days: int = 30):
    _, err = _auth(request)
    if err:
        return err
    return get_token_usage_stats(_AUTH_DB_TARGET, days=max(1, min(int(days or 30), 365)))


@router.get("/admin/ai/usage")
def admin_ai_usage(request: Request, days: int = 30):
    _, err = _auth(request)
    if err:
        return err
    stats = get_token_usage_stats(_AUTH_DB_TARGET, days=max(1, min(int(days or 30), 365)))
    pricing_table = _pricing_table()

    by_model = stats.get("by_model", []) or []
    total_usd = 0.0
    for row in by_model:
        prices = _price_for(str(row.get("model") or ""), pricing_table)
        tokens = int(row.get("tokens") or 0)
        usd = (tokens / 1000.0) * (prices["prompt"] + prices["completion"]) / 2.0
        row["estimated_usd"] = round(usd, 4)
        row["pricing_prompt_per_1k"] = prices["prompt"]
        row["pricing_completion_per_1k"] = prices["completion"]
        total_usd += usd

    by_date = stats.get("by_date", []) or []
    for row in by_date:
        tokens = int(row.get("tokens") or 0)
        prices = _price_for(
            str((by_model[0] if by_model else {}).get("model") or "gpt-4o-mini"),
            pricing_table,
        )
        row["estimated_usd"] = round(
            (tokens / 1000.0) * (prices["prompt"] + prices["completion"]) / 2.0, 4
        )

    summary = stats.get("total_summary") or {}
    if isinstance(summary, dict):
        summary["estimated_usd"] = round(total_usd, 4)
        stats["total_summary"] = summary

    cache_purged = (
        response_cache.purge_expired(_AUTH_DB_TARGET) if response_cache.CACHE_ENABLED else 0
    )
    return {
        **stats,
        "logger": prompt_logger_status(),
        "response_cache": {
            "enabled": response_cache.CACHE_ENABLED,
            "ttl_s": response_cache.CACHE_TTL_S,
            "purged": cache_purged,
        },
        "pricing_basis": "USD per 1K tokens; configure via OPENAI_PRICING_USD_PER_1K",
    }


@router.get("/api/prompt-logs/filters")
def api_prompt_logs_filters(request: Request):
    _, err = _auth(request)
    if err:
        return err
    return {
        "call_types": get_distinct_values(_AUTH_DB_TARGET, "call_type"),
        "models": get_distinct_values(_AUTH_DB_TARGET, "model"),
        "statuses": get_distinct_values(_AUTH_DB_TARGET, "status"),
        "difficulties": get_distinct_values(_AUTH_DB_TARGET, "difficulty"),
        "templates": get_distinct_values(_AUTH_DB_TARGET, "template_name"),
    }


@router.get("/api/prompt-logs/{log_id}")
def api_prompt_log_detail(log_id: str, request: Request):
    _, err = _auth(request)
    if err:
        return err
    entry = get_prompt_log_by_id(_AUTH_DB_TARGET, log_id)
    if not entry:
        return JSONResponse({"error": "Log not found."}, status_code=404)
    return {"log": entry}


@router.post("/api/prompt-logs/cleanup")
def api_prompt_logs_cleanup(request: Request):
    _, err = _auth(request)
    if err:
        return err
    file_removed = cleanup_old_file_logs()
    db_removed = cleanup_old_db_logs(_AUTH_DB_TARGET)
    return {"status": "ok", "file_dirs_removed": file_removed, "db_rows_removed": db_removed}


@router.get("/api/prompt-logs/export")
def api_prompt_logs_export(
    request: Request,
    call_type: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 1000,
):
    _, err = _auth(request)
    if err:
        return err
    result = query_prompt_logs(
        _AUTH_DB_TARGET,
        call_type=call_type,
        date_from=date_from,
        date_to=date_to,
        limit=max(1, min(int(limit or 1000), 5000)),
        offset=0,
    )
    logs = result.get("logs", [])
    content = json.dumps(logs, ensure_ascii=False, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=prompt_logs_export.json"},
    )
