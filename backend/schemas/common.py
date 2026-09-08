"""Shared response envelope + pagination schemas.

Every CRM endpoint returns: {"success": bool, "data": ..., "message": str, "errors": [...]}
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


def envelope(data: Any = None, message: str = "", success: bool = True,
             errors: list | None = None, meta: dict | None = None) -> dict:
    body: dict = {"success": success, "data": data, "message": message, "errors": errors or []}
    if meta is not None:
        body["meta"] = meta
    return body


class CommentIn(BaseModel):
    comment: str | None = None


class RejectIn(BaseModel):
    reason: str

    def validated_reason(self) -> str:
        reason = (self.reason or "").strip()
        if len(reason) < 10:
            raise ValueError("Rejection reason is mandatory (minimum 10 characters)")
        return reason


class StatusTransitionIn(BaseModel):
    new_status: str
    comment: str | None = None
