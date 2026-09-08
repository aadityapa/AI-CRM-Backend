"""Ask AI package — read-only in-app help assistant KB + assist service."""
from __future__ import annotations

from ai_help.assist import run_assist
from ai_help.loader import all_entries, build_help_context, get_entry, normalize_tab_key

__all__ = [
    "all_entries",
    "build_help_context",
    "get_entry",
    "normalize_tab_key",
    "run_assist",
]
