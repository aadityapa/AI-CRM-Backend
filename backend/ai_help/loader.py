"""Load and resolve Ask AI help KB entries by route/tab key."""
from __future__ import annotations

import re
from functools import lru_cache

from ai_help.business_rules import rules_text
from ai_help.entries import ENTRIES
from ai_help.schema import HelpEntry

# Map UI path prefixes / aliases → KB key
_ALIASES: dict[str, str] = {
    "": "dashboard",
    "dashboard": "dashboard",
    "home": "dashboard",
    "customers": "customers",
    "branch": "branch",
    "branch-policy": "branch",
    "opportunities": "opportunities",
    "candidates": "candidates",
    "template-requests": "template-requests",
    "profiles": "profiles",
    "projects": "projects",
    "project-employees": "project-employees",
    "my-leave": "my-leave",
    "leave-applications": "leave-applications",
    "holidays": "holidays",
    "timesheets": "timesheets",
    "pos": "pos",
    "purchase-orders": "pos",
    "invoices": "invoices",
    "tds": "tds",
    "employees": "employees",
    "reports": "reports",
}

# Safe CRM navigate targets (read-only nav). Values are `p=` paths.
NAVIGABLE: dict[str, str] = {
    "dashboard": "",
    "customers": "customers",
    "opportunities": "opportunities",
    "candidates": "candidates",
    "template-requests": "template-requests",
    "profiles": "profiles",
    "projects": "projects",
    "project-employees": "project-employees",
    "my-leave": "my-leave",
    "leave-applications": "leave-applications",
    "holidays": "holidays",
    "timesheets": "timesheets",
    "pos": "pos",
    "purchase orders": "pos",
    "purchase-orders": "pos",
    "invoices": "invoices",
    "tds": "tds",
    "employees": "employees",
    "reports": "reports",
}


@lru_cache(maxsize=1)
def all_entries() -> dict[str, HelpEntry]:
    return {e["key"]: e for e in ENTRIES}


def normalize_tab_key(raw: str | None) -> str:
    """Normalize CRM path / tab label to a KB key."""
    if raw is None:
        return "dashboard"
    s = str(raw).strip().lower()
    if not s or s in {"/", "crm"}:
        return "dashboard"
    # Strip query crumbs and leading slashes
    s = s.split("?", 1)[0].strip("/")
    # Take first segment (customers/12 → customers; branch-policy/3 → branch-policy)
    segment = s.split("/", 1)[0]
    if segment in _ALIASES:
        return _ALIASES[segment]
    # Label-ish: "Project Employees" → project-employees
    slug = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if slug in _ALIASES:
        return _ALIASES[slug]
    if slug in all_entries():
        return slug
    return segment if segment in all_entries() else "dashboard"


def get_entry(tab_key: str | None) -> HelpEntry:
    key = normalize_tab_key(tab_key)
    entries = all_entries()
    return entries.get(key) or entries["dashboard"]


def related_entries(entry: HelpEntry, *, limit: int = 4) -> list[HelpEntry]:
    out: list[HelpEntry] = []
    catalog = all_entries()
    for rel in entry.get("related_routes") or []:
        key = normalize_tab_key(rel)
        if key in catalog and key != entry["key"]:
            out.append(catalog[key])
        if len(out) >= limit:
            break
    return out


def format_entry_block(entry: HelpEntry, *, include_rules: bool = True) -> str:
    lines = [
        f"## {entry['title']} (key: {entry['key']})",
        f"Purpose: {entry['purpose']}",
        "Key fields/concepts:",
        *[f"- {f}" for f in entry.get("key_fields") or []],
        "Common tasks:",
        *[f"- {t}" for t in entry.get("common_tasks") or []],
        "Gotchas:",
        *[f"- {g}" for g in entry.get("gotchas") or []],
    ]
    if include_rules:
        rules = rules_text(entry.get("rule_ids"))
        if rules:
            lines.append("Business rules:")
            lines.append(rules)
    return "\n".join(lines)


def build_help_context(tab_key: str | None) -> dict:
    """Payload for GET help-context and for prompt grounding."""
    entry = get_entry(tab_key)
    related = related_entries(entry)
    return {
        "tab_key": entry["key"],
        "title": entry["title"],
        "purpose": entry["purpose"],
        "suggested_prompts": list(entry.get("suggested_prompts") or [])[:4],
        "related_routes": [
            {"key": r["key"], "title": r["title"], "path": NAVIGABLE.get(r["key"], r["key"])}
            for r in related
        ],
        "navigable_paths": {
            k: v for k, v in NAVIGABLE.items() if k in all_entries() or k in {"purchase orders", "purchase-orders"}
        },
    }


def resolve_navigate_to(hint: str | None) -> str | None:
    """Map a model navigate hint to a CRM `p=` path, or None."""
    if not hint:
        return None
    raw = str(hint).strip().lower()
    if not raw:
        return None
    # Direct NAVIGABLE key / label
    if raw in NAVIGABLE:
        return NAVIGABLE[raw]
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    if slug in NAVIGABLE:
        return NAVIGABLE[slug]
    # First path segment only when it is a known entry (do NOT fall back to dashboard)
    segment = raw.split("?", 1)[0].strip("/").split("/", 1)[0]
    if segment in _ALIASES:
        key = _ALIASES[segment]
        return NAVIGABLE.get(key)
    if segment in all_entries():
        return NAVIGABLE.get(segment, segment if segment != "dashboard" else "")
    # Title match
    for e in all_entries().values():
        if e["title"].lower() == raw:
            return NAVIGABLE.get(e["key"], e["key"] if e["key"] != "dashboard" else "")
    return None
