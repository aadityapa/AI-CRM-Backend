"""Profile admin API latency (local). Usage: python scripts/profile_admin_apis.py [base_url] [username] [password]"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import main  # noqa: E402
from main import app  # noqa: E402
from fastapi.testclient import TestClient


def _size_kb(data) -> float:
    try:
        return len(json.dumps(data, default=str).encode("utf-8")) / 1024.0
    except Exception:
        return 0.0


def profile_with_testclient() -> list[dict]:
    client = TestClient(app)
    # Bootstrap HR user if needed
    from auth_db import init_auth_db
    from main import AUTH_DB_TARGET

    init_auth_db(AUTH_DB_TARGET)
    username = "perf_hr_user"
    password = "perf_hr_pass_123"
    try:
        client.post("/auth/register", data={"username": username, "password": password, "role": "hr"})
    except Exception:
        pass
    login = client.post("/auth/login", data={"username": username, "password": password})
    token = (login.json() or {}).get("access_token", "")
    headers = {"Authorization": f"Bearer {token}"}

    endpoints = [
        ("GET", "/hr/dashboard?limit=200", None),
        ("GET", "/hr/schedules", None),
        ("GET", "/job/configs", None),
        ("GET", "/interview/integrity-logs", None),
        ("GET", "/api/prompt-logs?limit=25&offset=0", None),
        ("GET", "/api/prompt-logs/filters", None),
        ("GET", "/hr-records", None),
    ]
    results = []
    for method, path, body in endpoints:
        t0 = time.perf_counter()
        if method == "GET":
            r = client.get(path, headers=headers)
        else:
            r = client.post(path, headers=headers, data=body or {})
        ms = (time.perf_counter() - t0) * 1000.0
        payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        results.append(
            {
                "endpoint": path,
                "status": r.status_code,
                "ms": round(ms, 1),
                "payload_kb": round(_size_kb(payload), 1),
            }
        )
    return results


if __name__ == "__main__":
    rows = profile_with_testclient()
    print(f"{'Endpoint':<45} {'Status':>6} {'ms':>8} {'KB':>8}")
    print("-" * 72)
    for row in sorted(rows, key=lambda x: x["ms"], reverse=True):
        print(f"{row['endpoint']:<45} {row['status']:>6} {row['ms']:>8.1f} {row['payload_kb']:>8.1f}")
