# KARNEX — Full Application Feature Inventory & Cleanup Plan

Scanned: backend (271 endpoints, ~52 DB tables) + React admin dashboard (28 pages) +
vanilla candidate app (9 screens). **Review Part 1–3, mark anything you DON'T want to
keep, then confirm — Part 4 cleanup executes after your confirmation.**

---

## PART 1 — AI INTERVIEW PLATFORM (legacy core)

**Auth & access**
- [ ] User register / login (JWT) / me / logout
- [ ] HR access-code gate with rotate (admin)

**Candidate interview experience** (vanilla app)
- [ ] Invite link + access key flow (device-bound, OTP verify, login lockout)
- [ ] Device test gate (mic 3s level check, camera, speaker, network)
- [ ] Mandatory fullscreen live interview; AI-dynamic or manual question sources
- [ ] TTS question playback; mic recording; live transcript toggle
- [ ] Silero VAD speech detection + Whisper STT on speech segments only
- [ ] GPT smart auto-advance after silence (timer fallback + banners)
- [ ] Time-warning banners; confirmation before final submit
- [ ] Proctoring: 3-strike termination — tab switch, fullscreen exit, Esc/F11/Alt-Tab/Win-key,
      window blur, multiple-face detection (FaceDetector/MediaPipe)
- [ ] Results screen: report progress, Excel / QA PDF / TXT downloads, print report
- [ ] 3D particle background (three.js)

**HR / recruiter tooling** (React dashboard)
- [ ] HR Dashboard: KPIs, recent candidates, schedules, quick actions [TA/HR/RMG]
- [ ] HR Setup (Interview Schedule): template pick, candidate suggest, week/day calendar +
      time chips + 9 timezones, schedule → invite link + access key + email status + copy-all
- [ ] Upcoming interviews list (search, delete, copy invite)
- [ ] Reports: candidate/session master-detail dashboard [TA/HR/RMG]
- [ ] Full AI candidate report: score, ATS match, radar, per-question transcript,
      strengths/weaknesses, verdict, shortlist/on-hold/reject, score exclusions, PDF/JSON export
- [ ] Candidate interview timeline view
- [ ] Standalone ATS: upload CV + JD → score/grade/hire-probability [TA/HR/RMG]
- [ ] Integrity dashboard: violations, warnings, terminations, login attempts [TA/HR]
- [ ] Templates (RMG): job template list + huge builder (skills, AI prompt config with
      editable prompt, manual vs dynamic questions, question-bank filters, durations)
- [ ] Question Bank (super-admin): CRUD, CSV import/export, stats
- [ ] AI Logs (Admin): LLM prompt/response logs, token cost, latency, export, cleanup
- [ ] Email (SMTP) interview invites; Excel/PDF exports; health/ops endpoints

---

## PART 2 — KARNEX CRM

**Platform**
- [ ] 7-role RBAC (Admin/Sales/Sales_Head/RMG/TA/HR/Finance) + CEO super-admin tier
- [ ] Per-user tab-access overrides + field-level access (opportunities/customers/candidates)
      with editor in Users page
- [ ] My Profile: avatar upload, editable identity, change password (sidebar user block)
- [ ] Notifications bell: 60s polling, unread badge, mark read/all
- [ ] Role-adaptive dashboards: Executive, RMG, TA, Finance, Requirements funnel
- [ ] Reports with CSV export: opportunities, candidate profiles, recruiter productivity
- [ ] Settings: master data (departments, designations, skills, locations, doc types,
      leave types) + AI pass threshold
- [ ] Users admin: create users, roles, activate/deactivate, portal access, delete

**Sales → Delivery pipeline**
- [ ] Customers: branches, billing policy (drives invoicing), contacts, documents
- [ ] Opportunities: OPP-ids, stage machine, skills, attachments, guided full-screen
      New Opportunity form (autosave, dependency hints), linked profiles, activity log
- [ ] Requirements workflow: Sales create/submit → Sales Head approve/reject →
      RMG approve/reject → TA sourcing visibility rules; job postings; close/cancel; audit log
- [ ] Template Requests workflow: TA raises → RMG fulfils (with jump-to-Templates) →
      TA prepares L1
- [ ] Resumes/ATS: upload per requirement, scan + scan-all with score breakdown,
      shortlist/reject, Schedule AI L1 (auto-creates candidate + profile + real interview
      session with invite link)
- [ ] **Public apply flow: TA generates a public apply link per requirement; candidates
      apply via public landing page** (found in code — confirm this is wanted on UI)
- [ ] Candidates master: CV, education, experience + certificates, skills, linked profiles
- [ ] Candidate Profiles: 15-state pipeline with per-role transition authority + mandatory
      comments, skill-evaluation grid, offers, activity log, AI Interview tab (trigger,
      sessions, scores, report deep-links, pending badge)
- [ ] AI interview ↔ CRM sync: completed interviews auto-set Passed/Failed vs threshold,
      fill reviewer skill ratings, notify TA

**Operations & finance**
- [ ] Projects: team assignment with billing rates, communication matrix, timesheets tab,
      POs & invoices tab, one-click Create PO
- [ ] Timesheets: calendar day grid, server-computed billables from customer policy,
      rollups, submit/approve/reject, attachments
- [ ] Employees: directory, leave balances (auto-recomputed), project history
- [ ] Finance: POs (GST auto-split, allocations, cancel guard), invoices (PO balance
      enforcement, PDF generation, payments, TDS + TDS payments), TDS register

---

## PART 3 — DUPLICATIONS FOUND (decide which to keep)

1. **Two ATS engines**: legacy `/ats/score` (standalone ATS page) vs CRM requirement-scoped
   scanner. Both work; different code. *Recommend: keep both (different jobs) — or retire the
   standalone page if unused.*
2. **Two customer/opportunity stores**: legacy `/masters/*` (used by old template UI) vs full
   CRM modules. *Recommend: keep aliases for now (templates still read them).*
3. **Two role systems**: legacy hr/candidate + CRM 7-role (by design — interview platform
   unaffected by CRM).
4. **Two schema mechanisms**: legacy raw SQL + Alembic for CRM (by design).

---

## PART 4 — CLEANUP CANDIDATES (executed after your confirmation)

**Safe to remove (verified orphans)**
- `src/components/StrengthsWeaknessesPanel.tsx` (11.6 KB) — imported by nothing; live copy
  is in `candidate-report/`.
- `admin-dashboard/vite.config.ts.timestamp-*.mjs` (2 files) — Vite temp files committed by accident.
- `js/avatar.js` no-op stub in vanilla app (verify nothing imports it at runtime).

**Repo hygiene (big wins, zero runtime risk)**
- `admin-dashboard/dist/` (2.4 MB) committed build output — should be gitignored (rebuilt on boot).
- `frontend/admin/` — a second committed copy of the built app. Confirm which one the backend
  serves; gitignore the other.
- `node_modules/` in tree — gitignore if committed.

**Performance opportunities (measured from build chunks)**
- `vendor-pdf` 583 KB (jspdf + html2canvas) — lazy-load only when exporting a PDF.
- `vendor-charts` 399 KB (recharts) — already chunked; ensure loaded only on dashboard pages.
- `index.es` 150 KB — investigate source.
- 82 backdrop-blur layers → token system already caps blur on mobile; migrate remaining pages
  to `.glass` to activate it everywhere.

**Flagged for verification (not removal)**
- `routers/crm/__init__.py` — confirm every CRM router (incl. finance, dashboards, employees,
  reports, apply, template_requests) is in the registration tuple.
- `opportunity_attachments` persistence — confirm table/storage exists.
- `lib/tabAllowedByOverride()` marked deprecated — remove after migration completes.

---

**HOW TO REVIEW:** tick features you confirm should stay visible in the UI; strike anything
you want removed or hidden. Reply with the numbers/names — the optimization pass then removes
the confirmed-dead code, gitignores build artifacts, and lazy-loads the heavy chunks.
