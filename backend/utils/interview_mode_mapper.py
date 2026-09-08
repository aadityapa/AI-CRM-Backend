"""
Backward-compatible interview mode normalization.

Storage (DB / legacy sessions): mock | standard
API / UI canonical values: technical | hr

Legacy label mapping:
  mock / "Mock Technical Interview" -> technical / "Technical Interview"
  standard / "Standard Interview" -> hr / "HR Interview"
"""

from __future__ import annotations

from typing import Literal

CanonicalMode = Literal["technical", "hr"]
StorageMode = Literal["mock", "standard"]

CANONICAL_MODES: frozenset[str] = frozenset({"technical", "hr"})
STORAGE_MODES: frozenset[str] = frozenset({"mock", "standard"})

# Legacy DB + form values -> canonical API values
_LEGACY_TO_CANONICAL: dict[str, CanonicalMode] = {
    "mock": "technical",
    "standard": "hr",
    "technical": "technical",
    "hr": "hr",
    "technical interview": "technical",
    "hr interview": "hr",
    "mock technical interview": "technical",
    "standard interview": "hr",
}

_CANONICAL_TO_STORAGE: dict[str, StorageMode] = {
    "technical": "mock",
    "hr": "standard",
}

_DISPLAY_LABELS: dict[str, str] = {
    "technical": "Technical Interview",
    "hr": "HR Interview",
    "mock": "Technical Interview",
    "standard": "HR Interview",
}


def normalize_interview_mode(raw: str | None, *, default: CanonicalMode = "technical") -> CanonicalMode:
    """Map any stored or legacy value to canonical technical | hr."""
    key = str(raw or "").strip().lower()
    if not key:
        return default
    mapped = _LEGACY_TO_CANONICAL.get(key)
    if mapped:
        return mapped
    if key in CANONICAL_MODES:
        return key  # type: ignore[return-value]
    return default


def to_storage_mode(canonical_or_legacy: str | None) -> StorageMode:
    """Persist as mock | standard for schema backward compatibility."""
    canonical = normalize_interview_mode(canonical_or_legacy)
    return _CANONICAL_TO_STORAGE[canonical]


def to_display_label(mode: str | None) -> str:
    """Human-readable label for UI, reports, and analytics."""
    canonical = normalize_interview_mode(mode)
    return _DISPLAY_LABELS.get(canonical, "Technical Interview")


def api_interview_mode_from_storage(stored: str | None) -> CanonicalMode:
    """When reading job templates / sessions from DB."""
    return normalize_interview_mode(stored)


def storage_interview_mode_from_api(api_value: str | None) -> StorageMode:
    """When writing job templates from API forms."""
    return to_storage_mode(api_value)
