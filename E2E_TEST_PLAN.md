# KARNEX — End-to-End Flow Test Plan (Opportunity → Hire)

Run these steps on your machine (app built + backend running + Postgres up).
Each step lists the action, expected result, and current status.
Legend: ✅ implemented · ⚠️ partial/manual · ❌ not built yet.

Prereqs
- `cd AI-Interview-Model-B-V2/backend && alembic upgrade head` (applies 0010–0014)
- Backend running; `cd AI-Interview-Model-F-V2/frontend/admin-dashboard && npm run build` then serve.
- Test users with roles: Sales, Sales_Head, RMG, TA (create via Users tab as Admin).

## Step 1 — Sales creates a Customer ✅
- Log in as **Sales** → Customers → **Create User/Customer** → fill name (NN type auto), add a **Branch** with a **Contact Person + phone** and a **Hiring Manager + phone**.
- Expect: customer saved; branch saved; contacts visible in the customer's Contacts tab **with phone numbers** (if phone is blank here, the branch form isn't saving phone — that's the known gap to fix).

## Step 2 — Sales creates an Opportunity ✅
- New Opportunity → select the customer → select the **branch** → Contact Person, Contact Email/Phone, Hiring Manager + email/phone **auto-fill**. Pick a Type, fill required fields, Submit.
- Expect: opportunity created, `opp_id` like `OPP-2026-001`, status **Pending_Sales_Head_Approval**.
- IDOR check: as a *different* Sales user, `GET /api/opportunities/{id}` for someone else's opp → still readable (opps are org-shared) but **POST /approve must 403** (see Step 3).

## Step 3 — Sales Head approves ✅
- Log in as **Sales_Head** → Opportunities → Pending Approval → **Approve**.
- Expect: status → **Approved**; a **Requirement** is auto-created in **Pending_Engineering_Review**; RMG notified.
- Negative: as **Sales** (not head), call `POST /api/opportunities/{id}/approve` directly → **403**.

## Step 4 — RMG approves → Requirement open ✅
- Log in as **RMG** → Requirements → the new requirement → **Engineering Approve**.
- Expect: requirement status → **Open_For_Sourcing** (now TA-visible).
- Negative: as **TA**, confirm you could NOT see it before this step.

## Step 5 — TA sees the requirement ✅
- Log in as **TA** → Requirements → the requirement appears (Open_For_Sourcing).

## Step 6 — TA posts to a job portal ⚠️ (manual URL only)
- TA opens the requirement → **Add job posting** → paste portal name + URL.
- Expect: recorded; requirement auto-moves to **Posted_On_Portals**.
- NOT built: automatic posting to LinkedIn/Naukri/Indeed via their APIs.

## Step 7 — TA reaches out to preexisting candidates ❌ (not built)
- Expected feature: a tool to email/WhatsApp/call known candidates ("are you looking?").
- Status: no outreach tool yet. Workaround: TA uploads their resumes manually (Step 8).

## Step 8 — Resume intake + ATS score ✅
- TA → requirement → upload a resume (or use the public apply link) → **ATS scan**.
- Expect: a match **score with sub-scores + evidence**; a random/irrelevant doc is **rejected (422)**, not scored 100% (the old bug is fixed); duplicate file (same bytes) is de-duplicated by SHA-256.

## Step 9 — Auto-invite if score > 50% ❌ (not built)
- Expected: score ≥ configurable threshold (default 50%) → candidate auto-emailed/WhatsApp'd a slot-booking link; below → Archived.
- Status: no threshold-triggered auto-invite, no WhatsApp, email not wired to this step.
- To build: needs SMTP creds; WhatsApp needs Meta/Twilio + approved templates; a background job runner.

## Step 10 — Candidate books a slot ❌ (not built)
- Expected: candidate picks an open slot; concurrency-safe (no double-book); signed expiring token.
- Status: no slot/booking system exists.

## Step 11 — System sends L1 AI Interview link ⚠️ (generated, manual send)
- Today: HR/TA can generate an interview schedule → an **invite link + access key** are produced.
- Expect: link works, candidate can enter with email + access key, complete the L1 AI interview, report generated.
- NOT built: auto-send of that link by email/WhatsApp on slot confirmation.

## Step 12 — RMG reviews L1 report → decision ⚠️
- The AI-interview bridge writes back pass/fail vs a configurable threshold and updates the candidate.
- NOT built as a single "L1 report + Select/Reject/Request-L2 side-by-side" review screen.

---

## Summary
- **End-to-end works today:** Steps 1–5 and 8 (Sales→customer→opportunity→approvals→requirement→TA→resume→ATS score).
- **Manual today:** Step 6 (portal URL), Step 11 (link generated, shared manually).
- **Not built:** Step 7 (outreach), Step 9 (auto-invite on threshold + WhatsApp), Step 10 (slot booking), Step 12 (unified L1 review).

## To complete the flow you need to provide
- SMTP host/port/user/password (email — code exists, needs config).
- WhatsApp Business API creds (Meta Cloud or Twilio) + pre-approved templates.
- Optional: job-portal API credentials (LinkedIn/Naukri/Indeed) for auto-posting.
- Go-ahead to run a lightweight background scheduler (auto-invites, reminders, slot expiry).
