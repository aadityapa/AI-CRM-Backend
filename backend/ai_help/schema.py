"""Typed shape for Ask AI per-tab help KB entries."""
from __future__ import annotations

try:  # NotRequired landed in the stdlib in 3.11
    from typing import NotRequired, TypedDict
except ImportError:  # pragma: no cover - depends on the running interpreter
    # On 3.10 the bare stdlib import raises, and because this module is pulled
    # in by routers.crm.ai_assist that single failure aborts the whole CRM
    # router registration — all of it, logged as one line while the app still
    # reports healthy. Not worth risking over one type annotation.
    from typing_extensions import NotRequired, TypedDict


class HelpEntry(TypedDict):
    """One tab/route help article. Keys match CRM `p=` paths where possible."""

    key: str
    title: str
    purpose: str
    key_fields: list[str]
    common_tasks: list[str]
    gotchas: list[str]
    related_routes: list[str]
    suggested_prompts: list[str]
    # Optional deep-rule block IDs from business_rules.py
    rule_ids: NotRequired[list[str]]
