"""Admin dashboard deep links (18 Sep 2026).

Production served `{"detail":"Not Found"}` for every CRM "View report" /
"Full report" button: the links were built as `/admin?view=…` while the
dashboard is a StaticFiles mount at `/admin/`. Two guards:

1. every server-generated dashboard link carries the trailing slash;
2. `/admin?<query>` is redirected to `/admin/?<query>` with the query intact,
   so links already sitting in sent emails and bell notifications still open.
"""
from __future__ import annotations

import importlib
import pathlib
import re

import pytest

main = importlib.import_module("main")

BACKEND = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("routers", "services")


def test_no_generated_link_uses_the_slashless_admin_path():
    offenders = []
    for d in SCAN_DIRS:
        for path in (BACKEND / d).rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"/admin\?", text):
                line = text.count("\n", 0, m.start()) + 1
                offenders.append(f"{path.relative_to(BACKEND)}:{line}")
    assert offenders == [], f"use '/admin/?…' (StaticFiles mount) — found: {offenders}"


def _redirect_route():
    for r in main.app.routes:
        if getattr(r, "path", None) == "/admin" and "GET" in (getattr(r, "methods", None) or ()):
            return r
    return None


@pytest.mark.skipif(_redirect_route() is None, reason="admin dashboard build not present in this checkout")
def test_admin_without_slash_redirects_and_keeps_the_query():
    from fastapi.testclient import TestClient

    client = TestClient(main.app)
    res = client.get("/admin?view=candidateReport&cid=a%40b.in&iid=42", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"] == "/admin/?view=candidateReport&cid=a%40b.in&iid=42"
    res = client.get("/admin", follow_redirects=False)
    assert res.status_code == 307 and res.headers["location"] == "/admin/"


def test_redirect_is_declared_before_the_static_mount():
    """A route declared after the mount would never be reached."""
    paths = [getattr(r, "path", "") for r in main.app.routes]
    if "/admin" not in paths:
        pytest.skip("admin dashboard build not present in this checkout")
    mount_idx = next(i for i, r in enumerate(main.app.routes)
                     if getattr(r, "path", "") == "/admin" and type(r).__name__ == "Mount")
    route_idx = main.app.routes.index(_redirect_route())
    assert route_idx < mount_idx
