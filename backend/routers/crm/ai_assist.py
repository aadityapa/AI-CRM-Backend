"""Ask AI — READ-ONLY in-app help assistant (CRM).

POST /api/ai/assist — grounded reply (+ optional navigate_to hint)
GET  /api/ai/help-context — tab title + suggested prompts (no LLM)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db
from schemas.common import envelope
import rate_limit as _rl

from ai_help.assist import run_assist
from ai_help.loader import build_help_context

logger = logging.getLogger("karnex.ai_assist")

router = APIRouter(prefix="/api/ai", tags=["CRM: Ask AI"])


class AssistHistoryTurn(BaseModel):
    role: str = Field(..., description="user | assistant")
    content: str = Field(..., max_length=4000)


class AssistRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    # Accept either name for the current CRM tab/path
    route: str | None = Field(default=None, max_length=120)
    tab_key: str | None = Field(default=None, max_length=120)
    history: list[AssistHistoryTurn] = Field(default_factory=list, max_length=16)
    #: Let the model run whitelisted READ queries (ai_help/tools.py) instead of
    #: relying only on the keyword-guessed snapshot. Tools are SELECT-only and
    #: filtered by the caller's roles before they are offered to the model.
    enable_tools: bool = True
    #: Restrict this request to a subset of tool names. None = every tool the
    #: caller's roles allow.
    tool_whitelist: list[str] | None = None
    #: Reserved: navigation actions are already user-confirmed (the frontend
    #: renders a button; nothing runs on its own). No write action exists to
    #: confirm, because no write tool exists.
    confirm_actions: bool = True


@router.get("/help-context")
def help_context(
    route: str | None = Query(default=None),
    tab_key: str | None = Query(default=None),
    user: CurrentUser = Depends(any_crm_role),
):
    """Return title + suggested prompts for the current tab (no LLM call)."""
    _ = user
    ctx = build_help_context(tab_key or route)
    return envelope(
        data={
            "tab_key": ctx["tab_key"],
            "title": ctx["title"],
            "purpose": ctx["purpose"],
            "suggested_prompts": ctx["suggested_prompts"],
            "related_routes": ctx["related_routes"],
            "read_only": True,
        },
        message="Help context",
    )


@router.post("/assist")
@_rl.limit("20/minute")
def assist(
    request: Request,  # required by slowapi when limiter active
    payload: AssistRequest,
    user: CurrentUser = Depends(any_crm_role),
    db: Session = Depends(get_crm_db),
):
    """Read-only help reply grounded in the per-tab KB, a live CRM snapshot, and
    whitelisted read queries the model may run itself."""
    _ = request  # slowapi key_func may use request

    tab = payload.tab_key or payload.route
    history = [{"role": t.role, "content": t.content} for t in payload.history]
    # Live CRM copilot: read-only, role-aware snapshot so the assistant can
    # answer questions about the user's actual data ("top candidates for
    # REQ-2026-016", "which POs expire this quarter", ...). Best-effort.
    data_context = None
    try:
        from ai_help.crm_data import build_data_context
        data_context = build_data_context(
            db, set(user.roles), bool(getattr(user, "is_admin", False)), payload.message,
        )
    except Exception:
        logger.warning("ai_assist.data_context_failed", exc_info=True)

    # Whitelisted READ tools. Permission filtering happens HERE, where the
    # current user is known — a tool this person may not call is never offered
    # to the model, so it cannot describe or attempt a capability they lack.
    roles = set(user.roles)
    is_admin = bool(getattr(user, "is_admin", False))
    tools: list[dict] | None = None
    tool_runner = None
    if payload.enable_tools:
        try:
            from ai_help.tools import available_tools, run_tool

            tools = available_tools(roles, is_admin, payload.tool_whitelist) or None
            if tools:
                def tool_runner(name: str, args: dict) -> dict:  # noqa: F811
                    return run_tool(db, name, args, roles=roles, is_admin=is_admin,
                                    whitelist=payload.tool_whitelist)
        except Exception:
            # A broken tool layer must degrade to the old snapshot-only
            # behaviour, never to a failed answer.
            logger.warning("ai_assist.tools_unavailable", exc_info=True)
            tools, tool_runner = None, None

    try:
        data = run_assist(message=payload.message, tab_key=tab, history=history,
                          data_context=data_context, tools=tools, run_tool=tool_runner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("ai_assist.failed")
        raise HTTPException(
            status_code=502,
            detail="Ask AI is temporarily unavailable. Please try again.",
        ) from exc

    return envelope(data=data, message="Assist reply")
