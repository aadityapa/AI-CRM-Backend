#!/usr/bin/env python3
"""End-to-end verification of the Karnex CRM automated candidate pipeline.

Exercises, against a LIVE server (stdlib urllib only — pattern-copied from
scripts/verify_karnex_crm.py):

  admin login -> create a TA user -> customer -> opportunity -> requirement
  (approval flow to Open_For_Sourcing) -> TA publishes 2 future interview
  slots -> resume upload WITH email -> ATS scan (auto-threshold: expects
  auto_shortlisted=true + a SlotBooking auto-created) -> public GET
  /book/{token} page -> public POST /api/book/{token}/confirm (schedules the
  AI L1 interview) -> assertions on booking/resume/slot state -> a second
  confirm attempt must 4xx.

PREREQUISITES (same as verify_karnex_crm.py):
  * backend running with CRM Postgres configured, migrations at head (0015),
  * seed_crm.py applied (or defaults: ats_auto_invite=true, threshold=50),
  * an existing Admin user (backend/assign_crm_role.py <user> Admin).

HOW TO RUN
    python scripts/verify_auto_pipeline.py \
        --base-url http://127.0.0.1:2020 \
        --admin-user pavan --admin-pass <password>

Every check prints PASS/FAIL; exit code 1 if any check failed.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (stdlib only) — copied from verify_karnex_crm.py
# ---------------------------------------------------------------------------

class ApiClient:
    """Minimal JSON/form/multipart HTTP client over urllib."""

    def __init__(self, base_url: str, insecure: bool = False):
        self.base_url = base_url.rstrip("/")
        self.ctx = None
        if insecure:
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, method: str, path: str, token: str | None = None,
                json_body=None, form: dict | None = None,
                raw_body: bytes | None = None, content_type: str | None = None,
                query: dict | None = None, timeout: int = 120):
        """Return (status, content_type, parsed_json_or_None, raw_bytes)."""
        url = self.base_url + path
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw_body is not None:
            data = raw_body
            headers["Content-Type"] = content_type or "application/octet-stream"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as resp:
                body = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "") or ""
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status = exc.code
            ctype = (exc.headers.get("Content-Type", "") if exc.headers else "") or ""
        except (urllib.error.URLError, OSError) as exc:
            return 0, "", None, str(exc).encode("utf-8")
        js = None
        if body:
            try:
                js = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                js = None
        return status, ctype, js, body


def encode_multipart(fields: dict, file_field: str, filename: str,
                     file_content: bytes, file_content_type: str = "text/plain"):
    """Hand-rolled multipart/form-data body. Returns (body_bytes, content_type)."""
    boundary = "----KarnexVerify" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        if value is None:
            continue
        parts.append(
            (f"--{boundary}\r\n"
             f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
             f"{value}\r\n").encode("utf-8")
        )
    parts.append(
        (f"--{boundary}\r\n"
         f"Content-Disposition: form-data; name=\"{file_field}\"; "
         f"filename=\"{filename}\"\r\n"
         f"Content-Type: {file_content_type}\r\n\r\n").encode("utf-8")
    )
    parts.append(file_content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def unwrap(js):
    """Defensive envelope unwrap: {success,data,message,meta} -> data."""
    if isinstance(js, dict) and "data" in js:
        return js["data"]
    return js


def brief(js, raw: bytes = b"", limit: int = 160) -> str:
    """Short server-message string for FAIL details."""
    if isinstance(js, dict):
        for key in ("detail", "message", "error"):
            if js.get(key):
                return str(js[key])[:limit]
        return json.dumps(js)[:limit]
    return raw.decode("utf-8", "replace")[:limit]


class SkipSection(Exception):
    """Raised when a section's prerequisites were not met (earlier FAIL)."""


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------

class Verifier:
    def __init__(self, args):
        self.api = ApiClient(args.base_url, insecure=args.insecure)
        self.args = args
        self.sfx = uuid.uuid4().hex[:6]
        self.passed = 0
        self.failed = 0
        self.tok: dict[str, str | None] = {}
        self.skills: list[dict] = []
        self.location: dict | None = None
        self.customer_id = None
        self.opp_id = None
        self.req_id = None
        self.resume_id = None
        self.slot_ids: list[int] = []
        self.booking_token = None
        self.chosen_slot_id = None

    # ---------------- reporting -----------------------------------------

    def check(self, cond, label: str, detail: str = "") -> bool:
        ok = bool(cond)
        if ok:
            self.passed += 1
            print(f"PASS - {label}")
        else:
            self.failed += 1
            extra = f"  [{detail}]" if detail else ""
            print(f"FAIL - {label}{extra}")
        return ok

    @staticmethod
    def header(title: str) -> None:
        print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))

    def need(self, value, what: str):
        if value is None:
            raise SkipSection(f"missing prerequisite: {what}")
        return value

    def get(self, path, role, **kw):
        return self.api.request("GET", path, token=self.tok.get(role), **kw)

    def post(self, path, role, **kw):
        return self.api.request("POST", path, token=self.tok.get(role), **kw)

    def login(self, username: str, password: str, label: str) -> str | None:
        st, _, js, raw = self.api.request(
            "POST", "/auth/login", form={"username": username, "password": password})
        token = (js or {}).get("access_token") if isinstance(js, dict) else None
        self.check(st == 200 and token,
                   f"login as {label} ({username})",
                   f"status={st} {brief(js, raw)}")
        return token

    # =====================================================================
    # 1. Admin login + TA user
    # =====================================================================

    def sec_admin_and_ta(self):
        self.tok["Admin"] = self.login(self.args.admin_user, self.args.admin_pass, "Admin")
        self.need(self.tok["Admin"], "admin token")
        uname = f"ta_auto_{self.sfx}"
        st, _, js, raw = self.post("/api/users", "Admin", json_body={
            "full_name": f"TA AutoPipeline {self.sfx}",
            "email": f"ta.auto.{self.sfx}@karnex.test",
            "username": uname,
            "password": self.args.test_pass,
            "legacy_role": "hr",
            "roles": ["TA"],
        })
        data = unwrap(js) or {}
        ok = st == 200 and "TA" in (data.get("roles") or [])
        self.check(ok, f"POST /api/users creates TA user '{uname}'",
                   f"status={st} {brief(js, raw)}")
        self.tok["TA"] = self.login(uname, self.args.test_pass, "TA") if ok else None

    # =====================================================================
    # 2. Masters + customer + opportunity + requirement (Open_For_Sourcing)
    # =====================================================================

    def sec_requirement(self):
        self.need(self.tok.get("Admin"), "admin token")
        st, _, js, _ = self.get("/api/skills", "Admin", query={"limit": 100, "is_active": "true"})
        items = unwrap(js) or []
        simple = [s for s in items if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]*", str(s.get("name", "")))]
        pool = simple if len(simple) >= 2 else (items if isinstance(items, list) else [])
        if len(pool) >= 2:
            self.skills = [{"id": pool[0]["id"], "name": pool[0]["name"]},
                           {"id": pool[1]["id"], "name": pool[1]["name"]}]
        else:
            for name in (f"AutoSkillA {self.sfx}", f"AutoSkillB {self.sfx}"):
                st2, _, js2, _ = self.post("/api/skills", "Admin",
                                           json_body={"name": name, "category": "Verify"})
                d2 = unwrap(js2) or {}
                if st2 == 200 and d2.get("id"):
                    self.skills.append({"id": d2["id"], "name": name})
        self.check(len(self.skills) == 2, "picked 2 skill ids for the flow", f"skills={self.skills}")

        st, _, js, _ = self.get("/api/locations", "Admin", query={"limit": 50})
        locs = unwrap(js) or []
        if isinstance(locs, list) and locs:
            self.location = {"id": locs[0]["id"], "city": locs[0].get("city", "")}

        st, _, js, raw = self.post("/api/customers", "Admin", json_body={
            "name": f"AutoPipe Customer {self.sfx}",
            "legal_entity_name": f"AutoPipe Customer Pvt Ltd {self.sfx}",
            "status": "Active",
        })
        self.customer_id = (unwrap(js) or {}).get("id")
        self.check(st == 200 and self.customer_id, "customer created",
                   f"status={st} {brief(js, raw)}")
        self.need(self.customer_id, "customer id")

        st, _, js, raw = self.post("/api/opportunities", "Admin", json_body={
            "title": f"AutoPipe Opportunity {self.sfx}",
            "customer_id": self.customer_id,
            "opp_type": "T&M",
            "rfi_value": 500000,
        })
        self.opp_id = (unwrap(js) or {}).get("id")
        self.check(st == 200 and self.opp_id, "opportunity created",
                   f"status={st} {brief(js, raw)}")
        self.need(self.opp_id, "opportunity id")

        st, _, js, raw = self.post("/api/requirements", "Admin", json_body={
            "opportunity_id": self.opp_id,
            "title": f"AutoPipe Requirement {self.sfx}",
            "description": "Created by verify_auto_pipeline.py",
            "no_of_positions": 1,
            "experience_min": 3,
            "experience_max": 8,
            "work_mode": "Remote",
            "location_id": (self.location or {}).get("id"),
            "priority": "High",
            "skills": [
                {"skill_id": self.skills[0]["id"], "is_mandatory": True, "min_rating": 3},
                {"skill_id": self.skills[1]["id"], "is_mandatory": False},
            ],
        })
        data = unwrap(js) or {}
        self.req_id = data.get("id")
        self.check(st == 200 and self.req_id, "requirement created (Draft)",
                   f"status={st} {brief(js, raw)}")
        self.need(self.req_id, "requirement id")

        # Admin satisfies every role gate: submit -> approve -> engineering-approve.
        self.post(f"/api/requirements/{self.req_id}/submit", "Admin")
        self.post(f"/api/requirements/{self.req_id}/sales-head-approve", "Admin",
                  json_body={"comment": "auto-pipeline verifier"})
        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/engineering-approve",
                                   "Admin", json_body={"comment": "auto-pipeline verifier"})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Open_For_Sourcing",
                   "requirement approved to Open_For_Sourcing",
                   f"status={st} req_status={data.get('status')} {brief(js, raw)}")

    # =====================================================================
    # 3. TA publishes 2 future interview slots
    # =====================================================================

    def sec_slots(self):
        self.need(self.tok.get("TA"), "TA token")
        self.need(self.req_id, "requirement id")
        base = datetime.now(timezone.utc).replace(microsecond=0)
        payload = [
            {"slot_at": (base + timedelta(days=2)).isoformat(), "capacity": 1},
            {"slot_at": (base + timedelta(days=3)).isoformat(), "capacity": 2},
        ]
        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/slots", "TA",
                                   json_body=payload)
        data = unwrap(js) or []
        self.check(st == 200 and isinstance(data, list) and len(data) == 2,
                   "TA bulk-creates 2 future interview slots",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get(f"/api/requirements/{self.req_id}/slots", "TA")
        slots = unwrap(js) or []
        self.slot_ids = [s.get("id") for s in slots if isinstance(s, dict)]
        self.check(st == 200 and len(self.slot_ids) == 2,
                   "GET slots lists the 2 upcoming slots",
                   f"status={st} slot_ids={self.slot_ids}")

        # past datetime must be rejected
        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/slots", "TA",
                                   json_body=[{"slot_at": (base - timedelta(days=1)).isoformat()}])
        self.check(st == 400,
                   "NEGATIVE: creating a past slot returns 400",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 4. Resume upload (with email) + ATS scan -> auto-shortlist + booking
    # =====================================================================

    def sec_scan_auto(self):
        self.need(self.tok.get("TA"), "TA token")
        self.need(self.req_id, "requirement id")
        resume_text = (
            f"Resume of Auto Candidate {self.sfx}\n"
            f"Email: auto.candidate.{self.sfx}@example.test\nPhone: 9876543210\n"
            f"Skills: {self.skills[0]['name']}, {self.skills[1]['name']}\n"
            f"Total 5 years experience in software delivery.\n"
            f"Education: B.Tech in Computer Science.\n"
            f"Location: {(self.location or {}).get('city') or 'Pune'}\n"
        )
        body, ctype = encode_multipart(
            fields={
                "candidate_name": f"Auto Candidate {self.sfx}",
                "email": f"auto.candidate.{self.sfx}@example.test",
                "phone": "9876543210",
                "source_portal": "LinkedIn",
            },
            file_field="file",
            filename=f"resume_auto_{self.sfx}.txt",
            file_content=resume_text.encode("utf-8"),
            file_content_type="text/plain",
        )
        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/resumes", "TA",
                                   raw_body=body, content_type=ctype)
        data = unwrap(js) or {}
        self.resume_id = data.get("id")
        self.check(st == 200 and self.resume_id,
                   "TA uploads a resume WITH email",
                   f"status={st} {brief(js, raw)}")
        self.need(self.resume_id, "resume id")

        st, _, js, raw = self.post(f"/api/resumes/{self.resume_id}/ats-scan", "TA")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("ats_score") is not None,
                   "ATS scan returns a score",
                   f"status={st} score={data.get('ats_score')} {brief(js, raw)}")
        self.check(data.get("auto_shortlisted") is True
                   and data.get("ats_status") == "Shortlisted",
                   "auto-threshold: auto_shortlisted=true and ats_status=Shortlisted",
                   f"auto_shortlisted={data.get('auto_shortlisted')} "
                   f"ats_status={data.get('ats_status')} score={data.get('ats_score')}")

        st, _, js, raw = self.get(f"/api/requirements/{self.req_id}/bookings", "TA")
        bookings = unwrap(js) or []
        booking = next((b for b in bookings if isinstance(b, dict)
                        and b.get("resume_id") == self.resume_id), None)
        self.booking_token = (booking or {}).get("token")
        self.check(st == 200 and booking is not None and booking.get("status") == "Pending"
                   and self.booking_token,
                   "SlotBooking auto-created (Pending, has token) — GET bookings",
                   f"status={st} booking={booking}")

    # =====================================================================
    # 5. Public booking page + confirm
    # =====================================================================

    def sec_public_booking(self):
        self.need(self.booking_token, "booking token")
        self.need(self.slot_ids, "slot ids")

        st, ctype, _, body = self.api.request("GET", f"/book/{self.booking_token}")
        html = body.decode("utf-8", "replace")
        self.check(st == 200 and "text/html" in ctype.lower()
                   and f"AutoPipe Requirement {self.sfx}" in html
                   and 'name="slot_id"' in html,
                   "GET /book/{token} returns 200 HTML with role title + slot radios",
                   f"status={st} content-type={ctype} bytes={len(body)}")

        st, _, _, body = self.api.request("GET", "/book/not-a-real-token")
        self.check(st == 404,
                   "NEGATIVE: GET /book/{bad-token} returns 404",
                   f"status={st}")

        self.chosen_slot_id = self.slot_ids[0]
        st, _, js, raw = self.api.request(
            "POST", f"/api/book/{self.booking_token}/confirm",
            json_body={"slot_id": self.chosen_slot_id})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Confirmed"
                   and (data.get("invite_url") or "").strip(),
                   "POST confirm -> Confirmed with a non-empty invite_url",
                   f"status={st} {brief(js, raw)}")
        self.check("access_key" in data,
                   "confirm response includes access_key",
                   f"keys={sorted(data.keys()) if isinstance(data, dict) else 'n/a'}")

    # =====================================================================
    # 6. Post-confirmation state
    # =====================================================================

    def sec_post_state(self):
        self.need(self.tok.get("TA"), "TA token")
        self.need(self.req_id, "requirement id")
        self.need(self.booking_token, "booking token")

        st, _, js, _ = self.get(f"/api/requirements/{self.req_id}/bookings", "TA")
        bookings = unwrap(js) or []
        booking = next((b for b in bookings if isinstance(b, dict)
                        and b.get("resume_id") == self.resume_id), {}) or {}
        self.check(st == 200 and booking.get("status") == "Confirmed"
                   and (booking.get("invite_url") or "").strip()
                   and booking.get("slot_id") == self.chosen_slot_id,
                   "booking is Confirmed with invite_url and the chosen slot",
                   f"status={st} booking={ {k: booking.get(k) for k in ('status', 'slot_id', 'invite_url')} }")

        st, _, js, _ = self.get(f"/api/requirements/{self.req_id}/slots", "TA")
        slots = unwrap(js) or []
        slot = next((s for s in slots if isinstance(s, dict)
                     and s.get("id") == self.chosen_slot_id), {}) or {}
        self.check(st == 200 and slot.get("booked_count") == 1,
                   "chosen slot booked_count == 1",
                   f"status={st} slot={ {k: slot.get(k) for k in ('id', 'booked_count', 'capacity')} }")

        st, _, js, _ = self.get(f"/api/requirements/{self.req_id}/resumes", "TA",
                                query={"limit": 100})
        resumes = unwrap(js) or []
        resume = next((r for r in resumes if isinstance(r, dict)
                       and r.get("id") == self.resume_id), {}) or {}
        self.check(st == 200 and resume.get("ai_interview_status") == "Scheduled",
                   "resume ai_interview_status == Scheduled",
                   f"status={st} ai_interview_status={resume.get('ai_interview_status')}")

        st, _, js, raw = self.api.request(
            "POST", f"/api/book/{self.booking_token}/confirm",
            json_body={"slot_id": self.chosen_slot_id})
        self.check(400 <= st < 500,
                   "NEGATIVE: second confirm attempt returns 4xx",
                   f"status={st} {brief(js, raw)}")

        # confirmed booking page shows the invite link
        st, _, _, body = self.api.request("GET", f"/book/{self.booking_token}")
        html = body.decode("utf-8", "replace")
        self.check(st == 200 and "confirmed" in html.lower(),
                   "GET /book/{token} after confirm shows the confirmed state",
                   f"status={st} bytes={len(body)}")

    # =====================================================================

    def run(self) -> bool:
        print(f"Karnex auto-pipeline verification  |  base={self.api.base_url}  "
              f"suffix={self.sfx}  ts={datetime.now().isoformat(timespec='seconds')}")
        sections = [
            ("1. Admin login + TA user", self.sec_admin_and_ta),
            ("2. Customer -> opportunity -> requirement (approved)", self.sec_requirement),
            ("3. TA publishes interview slots", self.sec_slots),
            ("4. Resume upload + ATS scan auto-threshold", self.sec_scan_auto),
            ("5. Public booking page + confirm", self.sec_public_booking),
            ("6. Post-confirmation state", self.sec_post_state),
        ]
        for title, fn in sections:
            self.header(title)
            try:
                fn()
            except SkipSection as exc:
                print(f"--   section skipped ({exc}); see earlier FAIL for the root cause")
            except Exception as exc:  # defensive: never abort the whole run
                self.check(False, f"section '{title}' ran without unexpected errors",
                           f"{type(exc).__name__}: {exc}")

        total = self.passed + self.failed
        print("\n" + "=" * 66)
        print(f"SUMMARY: {self.passed}/{total} checks passed, {self.failed} failed")
        print("=" * 66)
        return self.failed == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the Karnex CRM automated candidate pipeline (stdlib only).")
    parser.add_argument("--base-url", default="http://127.0.0.1:2020",
                        help="Backend base URL (default: %(default)s)")
    parser.add_argument("--admin-user", required=True,
                        help="Existing user holding the CRM Admin role")
    parser.add_argument("--admin-pass", required=True, help="Admin password")
    parser.add_argument("--test-pass", default="Karnex@123",
                        help="Password for the generated TA user (default: %(default)s)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification (self-signed HTTPS)")
    args = parser.parse_args()

    verifier = Verifier(args)
    ok = verifier.run()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
