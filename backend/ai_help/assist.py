"""Ask AI assist service — READ-ONLY help grounded in the per-tab KB.

Reuses existing OpenAI client + prompt_logger. No write APIs / mutations.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from openai_client import OpenAIPurpose, get_openai_client, openai_key_configured
from prompt_logger import tracked_chat_completion

from ai_help.loader import (
    build_help_context,
    format_entry_block,
    get_entry,
    related_entries,
    resolve_navigate_to,
)

logger = logging.getLogger("karnex.ai_help")

SYSTEM_PROMPT = (
    "You are Ask AI — a friendly, conversational in-app guide for Karnex AI HR Suite.\n"
    "Reply like a helpful chatbot: answer the user's specific question first, in plain "
    "language. Do NOT dump a generic page overview or the whole help article unless they "
    "ask for an overview.\n"
    "Ground every fact in the HELP CONTEXT below. If something is not in the context, say "
    "you are not sure and suggest where they can look — never invent fields, buttons, or "
    "business rules.\n"
    "If they ask you to change data or perform actions, explain the steps they can take "
    "themselves (you are read-only) and offer a page to open when useful.\n"
    "Keep answers short and practical. Use light Markdown (short paragraphs, bullets, "
    "bold) when it helps readability.\n\n"
    "When query tools are available, use them for questions about the user's actual "
    "records — counts, lists, statuses, values, names, dates. Look the answer up "
    "instead of guessing, and instead of telling them where to click when they asked "
    "you what the number is. Call several tools if a question needs them. Report what "
    "comes back plainly; if a tool returns an error, say what you could not find and "
    "suggest a narrower question. Tools read data and nothing else — you still cannot "
    "change anything.\n\n"
    "At the end of your reply, if opening another page would help, add a single line "
    "exactly in this form (or omit if none):\n"
    "NAVIGATE_TO: <route_key>\n"
    "Valid route keys: dashboard, customers, opportunities, candidates, "
    "template-requests, profiles, projects, project-employees, my-leave, "
    "leave-applications, holidays, timesheets, pos, invoices, tds, employees, reports.\n"
    "Do not invent other keys. Do not claim you changed any data."
)


def _assist_llm_purpose() -> OpenAIPurpose | None:
    """Pick any configured chat-capable OpenAI key (env may only set purpose-specific keys)."""
    for purpose in ("question", "eval", "default", "tts", "transcribe"):
        if openai_key_configured(purpose):  # type: ignore[arg-type]
            return purpose  # type: ignore[return-value]
    return None

_MAX_HISTORY = 8
_MAX_MSG_CHARS = 2000
_DEFAULT_MAX_TOKENS = 700


def _model() -> str:
    return (os.getenv("INTERVIEW_OPENAI_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip() or "gpt-4o-mini"


def _max_tokens() -> int:
    try:
        return max(128, min(2000, int(os.getenv("AI_ASSIST_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS)))))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS


def _anonymize_question(text: str) -> str:
    """Strip obvious emails/phones for anonymized assist logs."""
    s = text or ""
    s = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[email]", s)
    s = re.sub(r"\b(?:\+?\d[\d\s\-()]{7,}\d)\b", "[phone]", s)
    return s[:500]


def _parse_reply(raw: str) -> tuple[str, str | None]:
    navigate_hint: str | None = None
    text = (raw or "").strip()
    m = re.search(r"(?im)^\s*NAVIGATE_TO:\s*([a-z0-9_\- ]+)\s*$", text)
    if m:
        navigate_hint = m.group(1).strip()
        text = (text[: m.start()] + text[m.end() :]).strip()
    # Also accept trailing JSON blob phase-2 style
    jm = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if jm:
        try:
            obj = json.loads(jm.group(1))
            if isinstance(obj, dict) and obj.get("navigate_to") and not navigate_hint:
                navigate_hint = str(obj["navigate_to"])
            text = (text[: jm.start()] + text[jm.end() :]).strip()
        except json.JSONDecodeError:
            pass
    return text, navigate_hint


def build_messages(
    *,
    message: str,
    tab_key: str | None,
    history: list[dict[str, str]] | None,
    data_context: str | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    entry = get_entry(tab_key)
    related = related_entries(entry)
    ctx = build_help_context(tab_key)

    blocks = [format_entry_block(entry)]
    for rel in related:
        blocks.append(format_entry_block(rel, include_rules=False))

    grounded = (
        f"Current page: {entry['title']} (tab_key={entry['key']})\n\n"
        + "\n\n---\n\n".join(blocks)
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"HELP CONTEXT:\n{grounded}"},
    ]
    if data_context:
        messages.append({
            "role": "system",
            "content": (
                "LIVE CRM DATA (read-only snapshot for THIS user — answer data "
                "questions from it; numbers not present here are unknown, say so "
                "rather than guessing):\n" + data_context
            ),
        })

    for turn in (history or [])[-_MAX_HISTORY:]:
        role = (turn.get("role") or "").strip().lower()
        content = (turn.get("content") or "").strip()[:_MAX_MSG_CHARS]
        if role in {"user", "assistant"} and content:
            messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": (message or "").strip()[:_MAX_MSG_CHARS]})
    return messages, ctx


#: How many times the model may look something up before it has to answer.
#: Three is enough for "find the requirement, then its candidates, then the
#: customer" and low enough that a confused model cannot bill a fortune.
_MAX_TOOL_ROUNDS = 3


def _run_tool_rounds(
    client,
    *,
    messages: list[dict],
    tools: list[dict],
    ctx: dict,
    anon_q: str,
    execute: Any,
) -> tuple[Any, list[str], list[dict]]:
    """Let the model query, then answer. Returns (final_response, names, results).

    Each round appends the assistant's tool calls and their results to the
    conversation, so by the last call the model is answering with the data in
    front of it rather than from the single guessed snapshot it used to get.
    """
    tools_used: list[str] = []
    tool_results: list[dict] = []
    response = None

    for round_no in range(_MAX_TOOL_ROUNDS + 1):
        # On the final round drop the tools entirely: the model must answer
        # with what it has rather than requesting a fourth lookup it will not
        # be allowed to make.
        offer = tools if round_no < _MAX_TOOL_ROUNDS else None
        response = tracked_chat_completion(
            client,
            model=_model(),
            messages=messages,
            temperature=0.45,
            max_tokens=_max_tokens(),
            tools=offer,
            tool_choice="auto" if offer else None,
            call_type="ai_assist",
            difficulty=f"route:{ctx['tab_key']}",
            template_name="ask_ai_help",
            selected_skills=[anon_q[:120]],
        )
        choice = response.choices[0].message
        calls = list(getattr(choice, "tool_calls", None) or [])
        if not calls:
            break

        messages.append({
            "role": "assistant",
            "content": choice.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })
        for call in calls:
            name = call.function.name
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute(name, args)
            tools_used.append(name)
            tool_results.append({"tool": name, "arguments": args, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, default=str),
            })

    return response, tools_used, tool_results


def run_assist(
    *,
    message: str,
    tab_key: str | None,
    history: list[dict[str, str]] | None = None,
    data_context: str | None = None,
    tools: list[dict] | None = None,
    run_tool: Any = None,
) -> dict[str, Any]:
    """Call LLM and return structured assist payload (no DB writes of CRM data).

    ``tools`` / ``run_tool`` are supplied by the router when the caller enabled
    them. They arrive as parameters rather than being imported here so this
    module keeps no database dependency and stays unit-testable without one —
    and so permission filtering happens where the current user is known.
    """
    msg = (message or "").strip()
    if not msg:
        raise ValueError("message is required")

    messages, ctx = build_messages(message=msg, tab_key=tab_key, history=history,
                                   data_context=data_context)

    # Anonymized improvement log (route + question only) — via prompt_logger call_type
    anon_q = _anonymize_question(msg)
    logger.info(
        "ai_assist.request",
        extra={"tab_key": ctx["tab_key"], "question": anon_q},
    )

    purpose = _assist_llm_purpose()
    if purpose is None:
        # Offline/dev fallback: answer from KB without inventing
        entry = get_entry(tab_key)
        fallback = (
            f"**{entry['title']}** — {entry['purpose']}\n\n"
            f"**Common tasks:**\n"
            + "\n".join(f"- {t}" for t in (entry.get("common_tasks") or [])[:3])
            + "\n\nI can explain more once the AI provider key is configured. "
            "I cannot change any data — only guide you."
        )
        nav = None
        low = msg.lower()
        for label, path in (
            ("project", "projects"),
            ("timesheet", "timesheets"),
            ("customer", "customers"),
            ("leave application", "leave-applications"),
            ("holiday", "holidays"),
        ):
            if f"go to {label}" in low or f"open {label}" in low:
                nav = path if path != "dashboard" else ""
                break
        return _response(fallback, navigate_to=nav, ctx=ctx)

    client = get_openai_client(purpose)
    tools_used: list[str] = []
    tool_results: list[dict] = []

    if tools and run_tool is not None:
        res, tools_used, tool_results = _run_tool_rounds(
            client, messages=messages, tools=tools, ctx=ctx, anon_q=anon_q, execute=run_tool,
        )
    else:
        res = tracked_chat_completion(
            client,
            model=_model(),
            messages=messages,
            temperature=0.45,
            max_tokens=_max_tokens(),
            call_type="ai_assist",
            # Anonymized: no candidate PII; route in difficulty field for filterability
            difficulty=f"route:{ctx['tab_key']}",
            template_name="ask_ai_help",
            selected_skills=[anon_q[:120]],
        )

    raw = (res.choices[0].message.content or "").strip()
    reply, hint = _parse_reply(raw)
    navigate_to = resolve_navigate_to(hint)
    return _response(
        reply or "I could not generate a reply. Please try again.",
        navigate_to=navigate_to, ctx=ctx,
        tools_used=tools_used, tool_results=tool_results,
    )


def _response(reply: str, *, navigate_to: str | None, ctx: dict,
              tools_used: list[str] | None = None,
              tool_results: list[dict] | None = None) -> dict[str, Any]:
    """Phase-1 response + Phase-2 seams (actions/tools empty / navigate only via button)."""
    from ai_help.loader import NAVIGABLE, all_entries

    actions: list[dict[str, Any]] = []
    if navigate_to is not None:
        # Seam only: frontend executes navigate after user clicks, not automatically.
        title = None
        for key, path in NAVIGABLE.items():
            if path == navigate_to and key in all_entries():
                title = all_entries()[key]["title"]
                break
        actions.append(
            {
                "type": "navigate",
                "path": navigate_to,
                "label": f"Go to {title}" if title else "Go to page",
                "requires_confirmation": True,  # phase-2: always user-initiated for now
                "enabled": True,
            }
        )

    return {
        "reply": reply,
        "navigate_to": navigate_to,
        "tab_key": ctx["tab_key"],
        "tab_title": ctx["title"],
        "suggested_prompts": ctx["suggested_prompts"],
        "actions": actions,
        # Which whitelisted reads answered this question. Surfaced so the user
        # can see the answer came from their data, not from the model's memory.
        "tools_used": tools_used or [],
        "tool_results": tool_results or [],
        # Still true, and still enforced server-side: every tool is a SELECT.
        "read_only": True,
    }
