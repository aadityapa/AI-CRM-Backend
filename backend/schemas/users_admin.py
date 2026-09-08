"""Pydantic schemas for Admin user management."""
from __future__ import annotations

from pydantic import BaseModel, field_validator


class UserCreateIn(BaseModel):
    full_name: str
    email: str
    username: str
    password: str
    # Legacy interview-platform role column (kept 'hr' so CRM users can log in).
    legacy_role: str = "hr"
    # CRM role names to assign (Admin, Sales, Sales_Head, RMG, TA, HR, Finance).
    roles: list[str] = []

    @field_validator("full_name", "email", "username", "password")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            raise ValueError("must not be empty")
        return s

    @field_validator("legacy_role")
    @classmethod
    def _legacy_role(cls, v: str) -> str:
        s = (v or "hr").strip().lower()
        if s not in {"hr", "candidate"}:
            raise ValueError("legacy_role must be 'hr' or 'candidate'")
        return s


class RolesIn(BaseModel):
    roles: list[str]


class TabAccessIn(BaseModel):
    # Tab keys the user may see (e.g. ["crm:dashboard", "iv:ats"]).
    # null clears the override -> role-based defaults.
    tabs: list[str] | None = None
    # Optional per-tab field access: { "<tab_key>": ["field_a", ...] }.
    # A tab absent here = all its fields allowed.
    field_access: dict | None = None


class UserOut(BaseModel):
    id: int
    full_name: str
    email: str
    username: str
    legacy_role: str
    is_active: bool
    roles: list[str] = []
