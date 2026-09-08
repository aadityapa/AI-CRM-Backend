"""Basic smoke test for /setup -> /answer -> /submit -> /report flow.

Usage:
  python scripts/smoke_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


BASE_URL = os.getenv("SMOKE_BASE_URL", "http://127.0.0.1:2020").rstrip("/")
REPORT_CODE = os.getenv("REPORT_CODE", "apple")


def post_form(path: str, payload: dict[str, str], token: str = "") -> dict:
    body = urlencode(payload).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(
        f"{BASE_URL}{path}",
        data=body,
        method="POST",
        headers=headers,
    )
    with urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def get_json(path: str, token: str = "") -> dict:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{BASE_URL}{path}", method="GET", headers=headers)
    with urlopen(req, timeout=300) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def ensure_ok(name: str, data: dict) -> None:
    if data.get("error"):
        raise RuntimeError(f"{name} failed: {data['error']}")


def auth_hr() -> str:
    uname = f"smoke_hr_{int(time.time())}"
    email = f"{uname}@local.test"
    password = "SmokePass123!"
    try:
        reg = post_form(
            "/auth/register",
            {
                "full_name": "Smoke HR",
                "email": email,
                "username": uname,
                "password": password,
                "role": "hr",
            },
        )
        ensure_ok("auth.register", reg)
    except HTTPError as exc:
        # If registration endpoint rejects due duplicate race, continue to login path.
        if exc.code not in {400, 409}:
            raise
    login = post_form("/auth/login", {"username": uname, "password": password})
    ensure_ok("auth.login", login)
    token = str(login.get("access_token", "")).strip()
    if not token:
        raise RuntimeError("auth.login failed: access_token missing")
    return token


def main() -> int:
    try:
        live = get_json("/health/live")
        ready = get_json("/health/ready")
        print("health.live:", live)
        print("health.ready:", ready)
        token = auth_hr()
        print("auth.hr: ok")

        setup = post_form(
            "/setup",
            {
                "candidate_name": "Smoke Test Candidate",
                "candidate_experience": "5 years",
                "candidate_email": "smoke@test.local",
                "candidate_role": "Backend Engineer",
                "jd": "Need Python, FastAPI, SQL, debugging skills.",
                "cv": "Worked on Python APIs, FastAPI services, SQL tuning.",
                "difficulty": "Medium",
                "num_q": "2",
                "model": "gpt-4o-mini",
                "custom_model": "",
                "safe_mode": "true",
                "followup_mode": "false",
                "final_skills": "python, fastapi, sql, debugging",
            },
            token=token,
        )
        ensure_ok("setup", setup)
        print("setup.ok:", {"question_count": setup.get("question_count"), "interview_id": setup.get("interview_id")})

        answer_bank = [
            "I designed FastAPI services, added SQL indexes, and fixed production bugs.",
            "I monitor latency and add automated tests before each release.",
            "I profile bottlenecks, tune queries, and verify with load tests.",
            "I communicate trade-offs clearly and document incident learnings.",
            "I use structured debugging with logs, traces, and rollback plans.",
        ]
        idx = 0
        while True:
            nxt = get_json("/next", token=token)
            ensure_ok("next", nxt)
            if nxt.get("message") == "Interview completed":
                done = nxt
                break
            qtxt = str(nxt.get("question", "")).strip()
            print(f"next.q{idx + 1}:", qtxt[:100])
            ans = answer_bank[min(idx, len(answer_bank) - 1)]
            ans_res = post_form("/answer", {"ans": ans}, token=token)
            ensure_ok(f"answer{idx + 1}", ans_res)
            print(f"answer.{idx + 1}:", ans_res)
            idx += 1
            if idx > 20:
                raise RuntimeError("safety stop: too many questions without completion")

        print("next.done:", done)

        submit = post_form("/submit", {}, token=token)
        ensure_ok("submit", submit)
        print("submit:", submit)

        report = post_form("/report", {"secret": REPORT_CODE}, token=token)
        ensure_ok("report", report)
        print("report.ok:", {"interview_id": report.get("interview_id"), "answers_count": report.get("answers_count")})

        print("SMOKE TEST PASSED")
        return 0
    except Exception as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
