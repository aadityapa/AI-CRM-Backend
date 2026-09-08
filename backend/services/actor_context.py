"""Who is acting in the current request — for outgoing mail.

Every email the app sends on a user's action should come FROM that user
(8 Sep 2026, user request): From shows their name, and — when the SMTP
provider accepts it — their address. ~75 notify/queue call sites never passed
`actor=`, so instead of touching each one the request's user is published
here once (by `crm_deps.get_current_user`) and the mail layer falls back to it
whenever a caller gave no explicit actor.

Mechanics: the middleware in main.py puts a fresh mutable HOLDER dict into a
ContextVar at the start of every request. FastAPI runs sync dependencies and
sync endpoints in threadpool threads with COPIES of the request context — a
value set inside one copy would be invisible to the others — but every copy
points at the SAME holder object, so a mutation made by the dependency is
seen by the endpoint and by anything the endpoint calls. Scheduler jobs and
CLI tools never pass through the middleware and keep the shared sender.
"""
from __future__ import annotations

from contextvars import ContextVar

_HOLDER: ContextVar[dict | None] = ContextVar("karnex_request_actor", default=None)


def begin_request() -> None:
    """Called by the HTTP middleware — one holder per request."""
    _HOLDER.set({})


def set_current_actor(user) -> None:
    holder = _HOLDER.get()
    if holder is not None:
        holder["user"] = user


def current_actor():
    """The CurrentUser of the request in flight, or None outside a request."""
    holder = _HOLDER.get()
    return holder.get("user") if holder else None
