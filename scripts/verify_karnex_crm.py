#!/usr/bin/env python3
"""End-to-end verification script for the Karnex CRM (FastAPI backend).

Exercises the full CRM flow against a LIVE server using only the Python
standard library (urllib): admin login, user provisioning per CRM role,
masters, customer -> opportunity -> requirement approval workflow,
TA sourcing (job posting, resume upload, ATS scan, shortlist, AI L1
scheduling), candidate-profile pipeline transitions with role authority,
projects + employees + timesheets, finance (PO / GST split / invoice /
payments / TDS / PDF), leave balances, dashboards, reports, and the
untouched legacy interview-platform endpoints (/version, /healthz).

Every check prints one line:  PASS/FAIL - description
A summary (passed/failed counts) is printed at the end; the process exits
with code 1 if any check failed.

PREREQUISITES
-------------
1. The backend server is running (default http://127.0.0.1:2020) with the
   CRM PostgreSQL configured (CRM_DATABASE_URL / AUTH_DB_URL).
2. CRM master data is seeded (skills/locations/leave types), e.g.:
       cd backend && python seed_crm.py
   (If skills/locations are missing the script creates its own as Admin,
   but the "seeded masters" checks will be reported as FAIL.)
3. An admin user ALREADY EXISTS and holds the CRM 'Admin' role, assigned
   via the bootstrap CLI (this cannot be done over the API from scratch):
       cd backend && python assign_crm_role.py <admin-username> Admin
   The user itself is a normal /auth/register (hr-role) or pre-existing
   registration_data user.

The script is RE-RUNNABLE: every created entity (users, customer,
opportunity, requirement, candidate, employee, project, PO, invoice)
carries a unique 6-char suffix, so no unique-constraint collisions occur.

HOW TO RUN
----------
    python scripts/verify_karnex_crm.py \
        --base-url http://127.0.0.1:2020 \
        --admin-user pavan --admin-pass <password>

Optional flags:
    --test-pass  Password for the 6 generated role users (default Karnex@123)
    --insecure   Skip TLS certificate verification (local self-signed HTTPS)
"""
from __future__ import annotations

import argparse
import calendar
import json
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime

CRM_ROLES = ["Sales", "Sales_Head", "RMG", "TA", "HR", "Finance"]


# ---------------------------------------------------------------------------
# Low-level HTTP helpers (stdlib only)
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


def close_to(a, b, tol=0.01) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return False


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
        # tokens per role name + "Admin"
        self.tok: dict[str, str | None] = {}
        # shared state created along the flow
        self.skills: list[dict] = []       # two picked skills [{id,name},...]
        self.location: dict | None = None  # {id, city}
        self.customer_id = None
        self.branch_id = None
        self.contact_id = None
        self.opp_id = None                 # numeric PK
        self.req_id = None                 # numeric PK
        self.req_number = None
        self.resume_id = None
        self.candidate_id = None
        self.profile_id = None
        self.project_id = None
        self.employee_id = None
        self.timesheet_id = None
        self.po_id = None
        self.po_before = None              # serialized PO dict at creation
        self.invoice = None                # serialized invoice dict
        self.leave_type_id = None

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

    # ---------------- convenience wrappers -------------------------------

    def get(self, path, role, **kw):
        return self.api.request("GET", path, token=self.tok.get(role), **kw)

    def post(self, path, role, **kw):
        return self.api.request("POST", path, token=self.tok.get(role), **kw)

    def put(self, path, role, **kw):
        return self.api.request("PUT", path, token=self.tok.get(role), **kw)

    def login(self, username: str, password: str, label: str) -> str | None:
        st, _, js, raw = self.api.request(
            "POST", "/auth/login", form={"username": username, "password": password})
        token = (js or {}).get("access_token") if isinstance(js, dict) else None
        self.check(st == 200 and token,
                   f"login as {label} ({username})",
                   f"status={st} {brief(js, raw)}")
        return token

    # =====================================================================
    # 1. Admin login + /api/me
    # =====================================================================

    def sec_admin(self):
        self.tok["Admin"] = self.login(self.args.admin_user, self.args.admin_pass, "Admin")
        self.need(self.tok["Admin"], "admin token")
        st, _, js, raw = self.get("/api/me", "Admin")
        data = unwrap(js) or {}
        roles = data.get("roles") or []
        self.check(st == 200 and "Admin" in roles,
                   "GET /api/me returns roles containing 'Admin'",
                   f"status={st} roles={roles} {brief(js, raw)}")

    # =====================================================================
    # 2. Create one user per CRM role and log each in
    # =====================================================================

    def sec_users(self):
        self.need(self.tok.get("Admin"), "admin token")
        for role in CRM_ROLES:
            uname = f"{role.lower()}_{self.sfx}"
            payload = {
                "full_name": f"{role} Verifier {self.sfx}",
                "email": f"{role.lower()}.{self.sfx}@karnex.test",
                "username": uname,
                "password": self.args.test_pass,
                "legacy_role": "hr",
                "roles": [role],
            }
            st, _, js, raw = self.post("/api/users", "Admin", json_body=payload)
            data = unwrap(js) or {}
            ok = st == 200 and role in (data.get("roles") or [])
            self.check(ok, f"POST /api/users creates {role} user '{uname}'",
                       f"status={st} {brief(js, raw)}")
            self.tok[role] = self.login(uname, self.args.test_pass, role) if ok else None

    # =====================================================================
    # 3. Masters: skills + locations (+ leave types looked up later)
    # =====================================================================

    def sec_masters(self):
        self.need(self.tok.get("Admin"), "admin token")
        st, _, js, raw = self.get("/api/skills", "Admin", query={"limit": 100, "is_active": "true"})
        items = unwrap(js) or []
        self.check(st == 200 and isinstance(items, list) and len(items) > 0,
                   "GET /api/skills returns a non-empty seeded list",
                   f"status={st} count={len(items) if isinstance(items, list) else 'n/a'}")
        # Prefer plain alphanumeric names so the ATS word-boundary regex matches.
        simple = [s for s in items if re.fullmatch(r"[A-Za-z][A-Za-z0-9 ]*", str(s.get("name", "")))]
        pool = simple if len(simple) >= 2 else items
        if len(pool) >= 2:
            self.skills = [{"id": pool[0]["id"], "name": pool[0]["name"]},
                           {"id": pool[1]["id"], "name": pool[1]["name"]}]
        else:
            # fallback: create two skills as Admin so the flow can continue
            for name in (f"VerifySkillA {self.sfx}", f"VerifySkillB {self.sfx}"):
                st2, _, js2, _ = self.post("/api/skills", "Admin",
                                           json_body={"name": name, "category": "Verify"})
                d2 = unwrap(js2) or {}
                if st2 == 200 and d2.get("id"):
                    self.skills.append({"id": d2["id"], "name": name})
        self.check(len(self.skills) == 2, "picked 2 skill ids for the flow",
                   f"skills={self.skills}")

        st, _, js, raw = self.get("/api/locations", "Admin", query={"limit": 50})
        locs = unwrap(js) or []
        if isinstance(locs, list) and locs:
            self.location = {"id": locs[0]["id"], "city": locs[0].get("city", "")}
        else:
            st2, _, js2, _ = self.post("/api/locations", "Admin",
                                       json_body={"city": f"VerifyCity{self.sfx}",
                                                  "state": "VerifyState", "country": "India"})
            d2 = unwrap(js2) or {}
            if st2 == 200 and d2.get("id"):
                self.location = {"id": d2["id"], "city": d2.get("city", "")}
        self.check(self.location is not None,
                   "GET /api/locations yields a location to use",
                   f"status={st}")

    # =====================================================================
    # 4. Customer + branch + billing policy + contact (Sales)
    # =====================================================================

    def sec_customer(self):
        self.need(self.tok.get("Sales"), "Sales token")
        st, _, js, raw = self.post("/api/customers", "Sales", json_body={
            "name": f"Acme Verification {self.sfx}",
            "legal_entity_name": f"Acme Verification Pvt Ltd {self.sfx}",
            "status": "Active",
        })
        data = unwrap(js) or {}
        self.customer_id = data.get("id")
        self.check(st == 200 and self.customer_id,
                   "POST /api/customers creates a customer (Sales)",
                   f"status={st} {brief(js, raw)}")
        self.need(self.customer_id, "customer id")

        st, _, js, raw = self.post(f"/api/customers/{self.customer_id}/branches", "Sales",
                                   json_body={
                                       "branch_name": "HQ",
                                       "billing_address": "1 Verification Street",
                                       "city": (self.location or {}).get("city") or "Pune",
                                       "state": "Maharashtra",
                                       "is_primary": True,
                                   })
        data = unwrap(js) or {}
        self.branch_id = data.get("id")
        self.check(st == 200 and self.branch_id,
                   "POST /api/customers/{id}/branches creates a primary branch",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.put(f"/api/customers/{self.customer_id}/billing-policy", "Sales",
                                  json_body={
                                      "week_off_billable": False,
                                      "leave_billable": False,
                                      "holidays_billable": False,
                                      "min_hours_full_day": 8.0,
                                      "min_hours_half_day": 4.0,
                                  })
        data = unwrap(js) or {}
        self.check(st == 200 and close_to(data.get("min_hours_full_day"), 8.0),
                   "PUT /api/customers/{id}/billing-policy upserts the policy",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/customers/{self.customer_id}/contacts", "Sales",
                                   json_body={
                                       "name": f"Contact {self.sfx}",
                                       "branch_id": self.branch_id,
                                       "email": f"contact.{self.sfx}@acme.test",
                                       "phone": "9999999999",
                                       "is_hiring_manager": True,
                                   })
        data = unwrap(js) or {}
        self.contact_id = data.get("id")
        self.check(st == 200 and self.contact_id,
                   "POST /api/customers/{id}/contacts creates a contact person",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 5. Opportunity (Sales)
    # =====================================================================

    def sec_opportunity(self):
        self.need(self.tok.get("Sales"), "Sales token")
        self.need(self.customer_id, "customer id")
        st, _, js, raw = self.post("/api/opportunities", "Sales", json_body={
            "title": f"Verification Opportunity {self.sfx}",
            "customer_id": self.customer_id,
            "branch_id": self.branch_id,
            "contact_person_id": self.contact_id,
            "opp_type": "T&M",
            "rfi_value": 500000,
        })
        data = unwrap(js) or {}
        self.opp_id = data.get("id")
        opp_code = data.get("opp_id") or ""
        self.check(st == 200 and self.opp_id,
                   "POST /api/opportunities creates an opportunity (opp_type T&M)",
                   f"status={st} {brief(js, raw)}")
        self.check(re.fullmatch(r"OPP-\d{4}-\d{3,}", opp_code) is not None,
                   f"opp_id '{opp_code}' matches OPP-YYYY-NNN",
                   f"opp_id={opp_code!r}")
        self.need(self.opp_id, "opportunity id")

        st, _, js, raw = self.post(f"/api/opportunities/{self.opp_id}/skills", "Sales",
                                   json_body=[
                                       {"skill_id": self.skills[0]["id"], "is_mandatory": True},
                                       {"skill_id": self.skills[1]["id"], "is_mandatory": False},
                                   ])
        data = unwrap(js)
        self.check(st == 200 and isinstance(data, list) and len(data) == 2,
                   "POST /api/opportunities/{id}/skills sets 2 skills",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 6. Requirement (Sales) + TA visibility negatives
    # =====================================================================

    def sec_requirement(self):
        self.need(self.tok.get("Sales"), "Sales token")
        self.need(self.opp_id, "opportunity id")
        st, _, js, raw = self.post("/api/requirements", "Sales", json_body={
            "opportunity_id": self.opp_id,
            "title": f"Verification Requirement {self.sfx}",
            "description": "Created by verify_karnex_crm.py",
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
        self.req_number = data.get("req_number") or ""
        self.check(st == 200 and self.req_id and data.get("status") == "Draft",
                   "POST /api/requirements creates a Draft requirement with skills",
                   f"status={st} {brief(js, raw)}")
        self.check(re.fullmatch(r"REQ-\d{4}-\d{3,}", self.req_number) is not None,
                   f"req_number '{self.req_number}' matches REQ-YYYY-NNN",
                   f"req_number={self.req_number!r}")
        self.need(self.req_id, "requirement id")
        self.need(self.tok.get("TA"), "TA token")

        st, _, js, raw = self.get(f"/api/requirements/{self.req_id}", "TA")
        self.check(st == 404,
                   "NEGATIVE: TA GET /api/requirements/{id} on a Draft returns 404",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get("/api/requirements", "TA",
                                  query={"search": self.req_number, "limit": 100})
        items = unwrap(js) or []
        ids = [r.get("id") for r in items if isinstance(r, dict)]
        self.check(st == 200 and self.req_id not in ids,
                   "NEGATIVE: TA requirement list does not contain the Draft requirement",
                   f"status={st} ids={ids}")

        st, _, js, raw = self.post("/api/requirements", "TA", json_body={
            "opportunity_id": self.opp_id,
            "title": "TA must not be able to create requirements",
        })
        self.check(st == 403,
                   "NEGATIVE: TA POST /api/requirements returns 403",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 7. Approval workflow + notifications
    # =====================================================================

    def sec_workflow(self):
        self.need(self.req_id, "requirement id")
        for role in ("Sales", "Sales_Head", "RMG", "TA"):
            self.need(self.tok.get(role), f"{role} token")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/submit", "Sales")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Pending_Sales_Head_Approval",
                   "Sales submit -> Pending_Sales_Head_Approval",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/sales-head-reject",
                                   "Sales_Head", json_body={"reason": "nope!"})
        self.check(st == 400,
                   "NEGATIVE: sales-head-reject with a 5-char reason returns 400",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/sales-head-approve", "RMG")
        self.check(st == 403,
                   "NEGATIVE: RMG POST sales-head-approve returns 403",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/sales-head-approve",
                                   "Sales_Head", json_body={"comment": "Approved by verifier"})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Pending_Engineering_Review",
                   "Sales_Head approve -> Pending_Engineering_Review",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/engineering-approve",
                                   "RMG", json_body={"comment": "Engineering OK"})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Open_For_Sourcing",
                   "RMG engineering-approve -> Open_For_Sourcing",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get("/api/requirements", "TA",
                                  query={"search": self.req_number, "limit": 100})
        items = unwrap(js) or []
        ids = [r.get("id") for r in items if isinstance(r, dict)]
        self.check(st == 200 and self.req_id in ids,
                   "TA now sees the requirement in the list (Open_For_Sourcing)",
                   f"status={st} ids={ids}")

        for role in ("Sales_Head", "RMG"):
            st, _, js, raw = self.get("/api/notifications", role, query={"limit": 100})
            items = unwrap(js) or []
            hit = any(self.req_number in f"{n.get('title', '')} {n.get('message', '')}"
                      for n in items if isinstance(n, dict))
            self.check(st == 200 and (hit or len(items) > 0),
                       f"{role} received notifications (mentioning {self.req_number})",
                       f"status={st} count={len(items) if isinstance(items, list) else 'n/a'}")

    # =====================================================================
    # 8. TA sourcing: job posting, resume, ATS scan, shortlist, AI L1
    # =====================================================================

    def sec_sourcing(self):
        self.need(self.tok.get("TA"), "TA token")
        self.need(self.req_id, "requirement id")

        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/job-postings", "TA",
                                   json_body={
                                       "portal_name": "LinkedIn",
                                       "job_post_url": f"https://www.linkedin.com/jobs/view/verify-{self.sfx}",
                                   })
        self.check(st == 200, "TA POST job-postings (LinkedIn) succeeds",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get(f"/api/requirements/{self.req_id}", "TA")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Posted_On_Portals",
                   "requirement auto-moved to Posted_On_Portals after first job posting",
                   f"status={st} req_status={data.get('status')}")

        # Resume text engineered to hit the ATS scorer: both skills,
        # "5 years experience" (within 3..8), an education keyword, the city.
        resume_text = (
            f"Resume of Verify Candidate {self.sfx}\n"
            f"Skills: {self.skills[0]['name']}, {self.skills[1]['name']}\n"
            f"Total 5 years experience in software delivery.\n"
            f"Education: B.Tech in Computer Science.\n"
            f"Location: {(self.location or {}).get('city') or 'Pune'}\n"
        )
        body, ctype = encode_multipart(
            fields={
                "candidate_name": f"Verify Candidate {self.sfx}",
                "email": f"verify.candidate.{self.sfx}@example.test",
                "phone": "9876543210",
                "source_portal": "LinkedIn",
            },
            file_field="file",
            filename=f"resume_{self.sfx}.txt",
            file_content=resume_text.encode("utf-8"),
            file_content_type="text/plain",
        )
        st, _, js, raw = self.post(f"/api/requirements/{self.req_id}/resumes", "TA",
                                   raw_body=body, content_type=ctype)
        data = unwrap(js) or {}
        self.resume_id = data.get("id")
        self.check(st == 200 and self.resume_id,
                   "TA uploads a resume (multipart .txt)",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get(f"/api/requirements/{self.req_id}", "TA")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "In_Progress",
                   "requirement auto-moved to In_Progress after first resume",
                   f"status={st} req_status={data.get('status')}")
        self.need(self.resume_id, "resume id")

        st, _, js, raw = self.post(f"/api/resumes/{self.resume_id}/ats-scan", "TA")
        data = unwrap(js) or {}
        breakdown = data.get("ats_score_breakdown") or {}
        matched = breakdown.get("skills_matched") or []
        self.check(st == 200 and data.get("ats_score") is not None,
                   "ATS scan sets a non-null ats_score",
                   f"status={st} score={data.get('ats_score')} {brief(js, raw)}")
        self.check(len(matched) > 0,
                   "ATS breakdown has non-empty skills_matched",
                   f"skills_matched={matched}")

        st, _, js, raw = self.post(f"/api/resumes/{self.resume_id}/shortlist", "TA")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("ats_status") == "Shortlisted",
                   "resume shortlisted (ats_status Shortlisted)",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/resumes/{self.resume_id}/schedule-ai-interview", "TA")
        data = unwrap(js) or {}
        self.candidate_id = data.get("candidate_id")
        self.profile_id = data.get("profile_id")
        resume_out = data.get("resume") or {}
        self.check(st == 200 and self.candidate_id and self.profile_id
                   and data.get("scheduled") is True,
                   "schedule-ai-interview returns candidate_id, profile_id, scheduled=true",
                   f"status={st} {brief(js, raw)}")
        self.check((data.get("ai_interview_status") == "Scheduled"
                    or resume_out.get("ai_interview_status") == "Scheduled"),
                   "resume ai_interview_status is 'Scheduled'",
                   f"got={data.get('ai_interview_status')!r}")
        self.need(self.profile_id, "candidate profile id")

        st, _, js, raw = self.get(f"/api/candidate-profiles/{self.profile_id}/ai-interviews", "TA")
        items = unwrap(js) or []
        meta = js.get("meta", {}) if isinstance(js, dict) else {}
        pending = meta.get("pending_count")
        if pending is None:
            pending = sum(1 for l in items if isinstance(l, dict) and l.get("result") == "Pending")
        self.check(st == 200 and isinstance(items, list) and len(items) >= 1 and pending >= 1,
                   "profile has 1 AI interview session with result Pending",
                   f"status={st} sessions={len(items) if isinstance(items, list) else 'n/a'} pending={pending}")

    # =====================================================================
    # 9. Candidate profile: skill evaluation + pipeline authority
    # =====================================================================

    def sec_profile(self):
        self.need(self.profile_id, "candidate profile id")
        for role in ("TA", "RMG"):
            self.need(self.tok.get(role), f"{role} token")

        st, _, js, raw = self.post(f"/api/candidate-profiles/{self.profile_id}/skill-evaluation",
                                   "TA", json_body=[
                                       {"skill_id": self.skills[0]["id"],
                                        "required_level": 3, "reviewer_rated": 4},
                                   ])
        data = unwrap(js)
        self.check(st == 200 and isinstance(data, list) and len(data) >= 1,
                   "TA upserts a skill evaluation on the profile",
                   f"status={st} {brief(js, raw)}")

        # Profile was created in Technical_Screening by the AI-L1 scheduler.
        # Authority map: Technical_Screening moves belong to TA.
        st, _, js, raw = self.post(f"/api/candidate-profiles/{self.profile_id}/status-transition",
                                   "TA", json_body={"new_status": "RMG_Review",
                                                    "comment": "AI L1 cleared - verifier"})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("pipeline_status") == "RMG_Review",
                   "TA moves profile Technical_Screening -> RMG_Review",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/candidate-profiles/{self.profile_id}/status-transition",
                                   "RMG", json_body={"new_status": "Sales_Screening",
                                                     "comment": "RMG review OK - verifier"})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("pipeline_status") == "Sales_Screening",
                   "RMG moves profile RMG_Review -> Sales_Screening",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/candidate-profiles/{self.profile_id}/status-transition",
                                   "TA", json_body={"new_status": "Customer_Screening",
                                                    "comment": "TA should be denied here"})
        self.check(st == 403,
                   "NEGATIVE: TA cannot move profile out of Sales_Screening (403)",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 10. Project (Sales_Head) + employee (HR) + timesheet cycle
    # =====================================================================

    def sec_project_timesheet(self):
        for role in ("Sales_Head", "HR"):
            self.need(self.tok.get(role), f"{role} token")
        self.need(self.opp_id, "opportunity id")
        self.need(self.customer_id, "customer id")

        st, _, js, raw = self.post("/api/projects", "Sales_Head", json_body={
            "opportunity_id": self.opp_id,
            "customer_id": self.customer_id,
            "name": f"Verification Project {self.sfx}",
        })
        data = unwrap(js) or {}
        self.project_id = data.get("id")
        self.check(st == 200 and self.project_id,
                   "Sales_Head POST /api/projects creates a project",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post("/api/employees", "HR", json_body={
            "first_name": "Verify",
            "last_name": f"Employee {self.sfx}",
            "email": f"verify.emp.{self.sfx}@karnex.test",
            "profile_type": "Internal",
            "date_of_joining": date.today().isoformat(),
        })
        data = unwrap(js) or {}
        self.employee_id = data.get("id")
        self.check(st == 200 and self.employee_id,
                   "HR POST /api/employees creates an employee",
                   f"status={st} {brief(js, raw)}")
        self.need(self.project_id, "project id")
        self.need(self.employee_id, "employee id")

        st, _, js, raw = self.post(f"/api/projects/{self.project_id}/employees", "HR",
                                   json_body={
                                       "employee_id": self.employee_id,
                                       "onboarding_date": date.today().isoformat(),
                                       "billing_rate": 1200,
                                       "billing_unit": "Daily",
                                       "work_mode": "Remote",
                                   })
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("id"),
                   "employee assigned to project with a billing_rate",
                   f"status={st} {brief(js, raw)}")

        today = datetime.now()
        month, year = today.month, today.year
        st, _, js, raw = self.post("/api/timesheets", "HR", json_body={
            "project_id": self.project_id,
            "employee_id": self.employee_id,
            "month": month,
            "year": year,
            "generate_days": True,
        })
        data = unwrap(js) or {}
        self.timesheet_id = data.get("id")
        self.check(st == 200 and self.timesheet_id and data.get("status") == "Draft",
                   "timesheet created with generate_days=true (Draft)",
                   f"status={st} {brief(js, raw)}")
        self.need(self.timesheet_id, "timesheet id")

        # first 3 weekdays of the month -> Present, 8h each
        last_day = calendar.monthrange(year, month)[1]
        weekdays = [date(year, month, d) for d in range(1, last_day + 1)
                    if date(year, month, d).weekday() < 5][:3]
        entries = [{
            "entry_date": d.isoformat(),
            "is_working": True,
            "hours_worked": 8,
            "attendance_status": "Present",
            "location": "Remote",
        } for d in weekdays]
        st, _, js, raw = self.post(f"/api/timesheets/{self.timesheet_id}/entries", "HR",
                                   json_body=entries)
        self.check(st == 200,
                   f"upserted {len(entries)} Present/8h timesheet entries",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/timesheets/{self.timesheet_id}/submit", "HR")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Submitted",
                   "timesheet submitted", f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/timesheets/{self.timesheet_id}/approve", "HR")
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("status") == "Approved",
                   "timesheet approved by HR", f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get(f"/api/timesheets/{self.timesheet_id}/summary", "HR")
        data = unwrap(js) or {}
        bh = data.get("billable_hours")
        self.check(st == 200 and isinstance(bh, (int, float)) and bh > 0,
                   "timesheet summary shows billable_hours > 0",
                   f"status={st} billable_hours={bh}")

    # =====================================================================
    # 11. Finance: PO, GST split, invoice, payments, TDS, PDF
    # =====================================================================

    def sec_finance(self):
        self.need(self.tok.get("Finance"), "Finance token")
        self.need(self.customer_id, "customer id")
        self.need(self.project_id, "project id")

        st, _, js, raw = self.post("/api/purchase-orders", "Finance", json_body={
            "customer_id": self.customer_id,
            "billing_branch_id": self.branch_id,
            "delivery_branch_id": self.branch_id,
            "contact_person_id": self.contact_id,
            "received_date": date.today().isoformat(),
            "po_type": "Standard",
            "payment_terms": "Net 30",
            "tax_slab": 18,
            "inter_state": False,
            "total_value": 100000,
        })
        po = unwrap(js) or {}
        self.po_id = po.get("id")
        self.po_before = po
        self.check(st == 200 and self.po_id,
                   "Finance creates a PO (tax_slab 18, intra-state)",
                   f"status={st} {brief(js, raw)}")
        self.check(close_to(po.get("sgst"), 9) and close_to(po.get("cgst"), 9),
                   "PO GST split: sgst == 9 and cgst == 9",
                   f"sgst={po.get('sgst')} cgst={po.get('cgst')} igst={po.get('igst')}")
        self.need(self.po_id, "purchase order id")

        st, _, js, raw = self.post("/api/invoices", "Finance", json_body={
            "po_id": self.po_id,
            "project_id": self.project_id,
            "invoice_date": date.today().isoformat(),
            "sub_total": 10000,
        })
        inv = unwrap(js) or {}
        self.invoice = inv
        grand = inv.get("grand_total")
        self.check(st == 200 and inv.get("id") and close_to(grand, 11800),
                   "invoice created against PO: sub_total 10000 -> grand_total 11800 (18% GST)",
                   f"status={st} grand_total={grand} {brief(js, raw)}")
        self.need(inv.get("id"), "invoice id")

        st, _, js, raw = self.get(f"/api/purchase-orders/{self.po_id}", "Finance")
        po_now = unwrap(js) or {}
        consumed_delta = (po_now.get("consumed_value") or 0) - (self.po_before.get("consumed_value") or 0)
        balance_delta = (self.po_before.get("balance_value") or 0) - (po_now.get("balance_value") or 0)
        self.check(st == 200 and close_to(consumed_delta, grand or 0)
                   and close_to(balance_delta, grand or 0),
                   "PO consumed_value increased and balance_value decreased by the invoice grand_total",
                   f"consumed_delta={consumed_delta} balance_delta={balance_delta} grand={grand}")

        half = round(float(grand or 0) / 2, 2)
        st, _, js, raw = self.post(f"/api/invoices/{inv['id']}/record-payment", "Finance",
                                   json_body={
                                       "payment_date": date.today().isoformat(),
                                       "amount": half,
                                       "payment_mode": "NEFT",
                                       "reference_number": f"VER-{self.sfx}",
                                   })
        data = unwrap(js) or {}
        inv_after = data.get("invoice") or {}
        self.check(st == 200 and inv_after.get("payment_status") == "Partially_Paid"
                   and close_to(inv_after.get("balance_amount"), half),
                   f"half payment ({half}) -> payment_status Partially_Paid, balance {half}",
                   f"status={st} inv={ {k: inv_after.get(k) for k in ('payment_status', 'paid_amount', 'balance_amount')} }")

        st, _, js, raw = self.post(f"/api/invoices/{inv['id']}/record-tds", "Finance",
                                   json_body={})
        tds = unwrap(js) or {}
        self.check(st == 200 and tds.get("id") and tds.get("tds_status") == "Pending"
                   and (tds.get("tds_amount") or 0) > 0,
                   "TDS record created (status Pending)",
                   f"status={st} {brief(js, raw)}")

        tds_half = round(float(tds.get("tds_amount") or 0) / 2, 2) or 1
        st, _, js, raw = self.post(f"/api/invoices/{inv['id']}/tds-payment", "Finance",
                                   json_body={"amount": tds_half})
        data = unwrap(js) or {}
        self.check(st == 200 and data.get("tds_status") == "Partially_Paid",
                   "partial TDS payment -> tds_status Partially_Paid",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.post(f"/api/invoices/{inv['id']}/generate-pdf", "Finance")
        data = unwrap(js) or {}
        pdf_url = data.get("invoice_pdf_url")
        self.check(st == 200 and pdf_url,
                   "generate-pdf sets invoice_pdf_url",
                   f"status={st} {brief(js, raw)}")
        if pdf_url:
            st, ctype, _, body = self.get(pdf_url, "Finance")
            self.check(st == 200 and ("pdf" in ctype.lower() or len(body) > 0),
                       "invoice PDF downloadable with auth (200, pdf/non-empty)",
                       f"status={st} content-type={ctype} bytes={len(body)}")

    # =====================================================================
    # 12. Employee leave balances
    # =====================================================================

    def sec_leave(self):
        self.need(self.tok.get("HR"), "HR token")
        self.need(self.employee_id, "employee id")

        st, _, js, raw = self.get("/api/leave-policy-types", "HR", query={"limit": 50})
        types = unwrap(js) or []
        if isinstance(types, list) and types:
            self.leave_type_id = types[0].get("id")
        else:
            st2, _, js2, _ = self.post("/api/leave-policy-types", "Admin",
                                       json_body={"name": f"VerifyLeave {self.sfx}"})
            d2 = unwrap(js2) or {}
            self.leave_type_id = d2.get("id")
        self.check(self.leave_type_id is not None,
                   "leave policy type available",
                   f"status={st}")
        self.need(self.leave_type_id, "leave type id")

        year = datetime.now().year
        st, _, js, raw = self.put(f"/api/employees/{self.employee_id}/leave-balances", "HR",
                                  json_body=[{
                                      "leave_type_id": self.leave_type_id,
                                      "year": year,
                                      "accrued": 12,
                                      "consumed": 2,
                                      "carry_forward": 1,
                                  }])
        self.check(st == 200,
                   "PUT leave-balances (accrued 12, consumed 2, carry_forward 1)",
                   f"status={st} {brief(js, raw)}")

        st, _, js, raw = self.get(f"/api/employees/{self.employee_id}/leave-balances", "HR",
                                  query={"year": year})
        rows = unwrap(js) or []
        row = next((r for r in rows if isinstance(r, dict)
                    and r.get("leave_type_id") == self.leave_type_id), {})
        self.check(st == 200 and close_to(row.get("balance"), 11),
                   "GET leave-balances shows server-computed balance == 11",
                   f"status={st} balance={row.get('balance')}")

    # =====================================================================
    # 13. Dashboards (positive per role + one negative)
    # =====================================================================

    def sec_dashboards(self):
        dash = [
            ("Sales_Head", "/api/dashboard/executive"),
            ("RMG", "/api/dashboard/rmg"),
            ("TA", "/api/dashboard/ta"),
            ("Finance", "/api/dashboard/finance"),
            ("Sales", "/api/dashboard/requirements"),
        ]
        for role, path in dash:
            if not self.tok.get(role):
                self.check(False, f"{role} GET {path}", "no token for role")
                continue
            st, _, js, raw = self.get(path, role)
            self.check(st == 200, f"{role} GET {path} returns 200",
                       f"status={st} {brief(js, raw)}")
        if self.tok.get("TA"):
            st, _, js, raw = self.get("/api/dashboard/finance", "TA")
            self.check(st == 403,
                       "NEGATIVE: TA GET /api/dashboard/finance returns 403",
                       f"status={st} {brief(js, raw)}")

    # =====================================================================
    # 14. Reports (JSON + CSV)
    # =====================================================================

    def sec_reports(self):
        self.need(self.tok.get("Sales"), "Sales token")
        st, _, js, raw = self.get("/api/reports/opportunities", "Sales")
        self.check(st == 200 and isinstance(js, dict) and js.get("success") is True,
                   "GET /api/reports/opportunities returns a 200 envelope",
                   f"status={st} {brief(js, raw)}")
        st, ctype, _, body = self.get("/api/reports/opportunities", "Sales",
                                      query={"format": "csv"})
        self.check(st == 200 and "text/csv" in ctype.lower(),
                   "GET /api/reports/opportunities?format=csv returns text/csv",
                   f"status={st} content-type={ctype} bytes={len(body)}")

    # =====================================================================
    # 15. Legacy interview platform untouched
    # =====================================================================

    def sec_platform(self):
        st, _, js, raw = self.api.request("GET", "/version")
        self.check(st == 200, "GET /version returns 200",
                   f"status={st} {brief(js, raw)}")
        st, _, js, raw = self.api.request("GET", "/healthz")
        self.check(st == 200, "GET /healthz returns 200",
                   f"status={st} {brief(js, raw)}")

    # =====================================================================

    def run(self) -> bool:
        print(f"Karnex CRM end-to-end verification  |  base={self.api.base_url}  "
              f"suffix={self.sfx}  ts={datetime.now().isoformat(timespec='seconds')}")
        sections = [
            ("1. Admin login & /api/me", self.sec_admin),
            ("2. Create role users via POST /api/users", self.sec_users),
            ("3. Masters (skills / locations)", self.sec_masters),
            ("4. Customer, branch, billing policy, contact", self.sec_customer),
            ("5. Opportunity + skills", self.sec_opportunity),
            ("6. Requirement + TA visibility negatives", self.sec_requirement),
            ("7. Approval workflow + notifications", self.sec_workflow),
            ("8. TA sourcing: posting / resume / ATS / AI L1", self.sec_sourcing),
            ("9. Candidate profile pipeline & authority", self.sec_profile),
            ("10. Project, employee, timesheet cycle", self.sec_project_timesheet),
            ("11. Finance: PO / invoice / payments / TDS / PDF", self.sec_finance),
            ("12. Employee leave balances", self.sec_leave),
            ("13. Dashboards", self.sec_dashboards),
            ("14. Reports", self.sec_reports),
            ("15. Legacy platform untouched", self.sec_platform),
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
        description="End-to-end verification of the Karnex CRM API (stdlib only).")
    parser.add_argument("--base-url", default="http://127.0.0.1:2020",
                        help="Backend base URL (default: %(default)s)")
    parser.add_argument("--admin-user", required=True,
                        help="Existing user holding the CRM Admin role "
                             "(assigned via backend/assign_crm_role.py)")
    parser.add_argument("--admin-pass", required=True, help="Admin password")
    parser.add_argument("--test-pass", default="Karnex@123",
                        help="Password for the generated role users (default: %(default)s)")
    parser.add_argument("--insecure", action="store_true",
                        help="Skip TLS certificate verification (self-signed HTTPS)")
    args = parser.parse_args()

    verifier = Verifier(args)
    ok = verifier.run()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
