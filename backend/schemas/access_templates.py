"""Pydantic schemas for Access Templates (Admin/CEO managed)."""
from __future__ import annotations

from pydantic import BaseModel


class AccessTemplateCreate(BaseModel):
    name: str
    description: str | None = None
    department_id: int | None = None
    role: str | None = None
    is_active: bool = True
    tab_access: dict[str, str] | None = None                 # { tab: "view"|"edit" }
    field_access: dict[str, dict[str, str]] | None = None    # { tab: { field: "view"|"edit" } }


class AccessTemplateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    department_id: int | None = None
    role: str | None = None
    is_active: bool | None = None
    tab_access: dict[str, str] | None = None
    field_access: dict[str, dict[str, str]] | None = None


class AssignTemplateIn(BaseModel):
    user_id: int
    template_id: int | None = None
