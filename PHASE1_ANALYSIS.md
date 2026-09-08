# Karnex CRM — Phase 1: Codebase Analysis

Date: 2026-07-08
Repos analyzed: `AI-Interview-Model-B-V2` (backend) and `AI-Interview-Model-F-V2` (frontend)

---

## 0. CRITICAL BLOCKER — backend source is deleted from disk

The entire `backend/` source tree (122 files: `main.py`, `auth_db.py`, `ai.py`, `ats.py`, routers, services, tests…) is **deleted from the working tree**. Only `backend/certs/` remains. All files still exist in git `HEAD` (branch `main`) — `git status` shows 129 `D` entries.

**Before Phase 2 can start, run:** `git restore backend/` (from the Model-B-V2 root).

Note: git writes are blocked from this sandbox (`.git/index.lock` permission error), so you must run the restore yourself, or explicitly ask me to copy files out of git objects another way.

---

## 1. What exists — Backend (Model-B-V2)

### Architecture
- **Monolith FastAPI app**: `backend/main.py` (7,337 lines — nearly all routes declared with `@app.*` directly). Only one real `APIRouter`: `backend/routers/admin.py` (prompt-log/AI-usage admin).
- The `/services/*` microservices (api-gateway, auth-service, candidate-service, template-service) are **strangler-proxy stubs** that forward everything to the monolith. They contain zero domain logic. The monolith is what runs (`start_app.bat`, `scripts/run_backend.cmd`, root `Dockerfile`, `render.yaml`).
- Runs on port 2020 (HTTPS local) / `PORT` in Docker; deployed to Render.

### Database
- **No ORM.** Raw SQL via `sqlite3` and `psycopg2` (ThreadedConnectionPool), with dual-dialect DDL duplicated for both engines in `backend/auth_db.py::init_auth_db()`.
- **Engine:** SQLite at `data/karnex_db.db` by default; **PostgreSQL when `AUTH_DB_URL` is set** (docker-compose uses postgres:16; production target is Supabase Postgres).
- **No Alembic / no migration framework.** Schema managed via idempotent `CREATE TABLE IF NOT EXISTS` + hand-rolled `_ensure_*_columns` additive migrations at startup.

### Existing tables
| Table | Purpose |
|---|---|
| `registration_data` | Users. `role` is `TEXT CHECK IN ('hr','candidate')` — only 2 roles |
| `login_data` | Login audit |
| `interview_schedule` | HR-scheduled interviews, invite tokens, lockout columns |
| `interview_records` | Completed interviews (JSON payload) |
| `interview_progress` | Live/resumable interview state (JSON) |
| `job_templates` | Job/interview templates incl. `opportunity_id`, `customer_name`, skills JSON, prompt versions |
| `hr_candidate_decisions` | HR pass/fail decisions |
| `opportunity_master` | Minimal opportunity lookup (id, opportunity_id, created_by) |
| `customer_master` | Minimal customer lookup (id, customer_name, created_by) |
| `prompt_log` | AI prompt/usage logging |

Plus JSON/file stores: `data/hr_records.json`, `interview_learning.jsonl`, `proctor_reports.json`, `ats_cache.json`, `job_configs.json`.

### Authentication
- **JWT (PyJWT), HS256 Bearer.** Claims: `sub` (username), `role`, `full_name`, `email`, `iat`, `exp`. TTL default 480 min. Secret from `AUTH_SECRET` env.
- Auth check is a helper called inside each handler (not a FastAPI `Depends`):
  `user, auth_err = _require_user(request, {"hr", "candidate"})` → returns 401/403 JSONResponse.
- Passwords: PBKDF2-HMAC-SHA256, 120k iterations, per-user salt.
- HR access gated by a shared **HR access code** (`data/hr_access_code.txt`, rotatable via `/admin/hr-code`).
- **No RBAC beyond `hr` / `candidate`.** No roles/user_roles tables.

### Existing API surface (grouped)
- Auth: `/auth/register`, `/auth/login`, `/auth/me`, `/auth/logout`
- Interview lifecycle: `/setup`, `/next`, `/answer`, `/submit`, `/report`, `/session-status`, `/extract-skills`
- Candidate invite flow: `/candidate/invite/{token}` (+ `/verify`, `/login`), media (`/candidate/transcribe`, `/tts`)
- HR: `/hr/schedule-interview`, `/hr/schedules`, `/hr/dashboard`, `/hr/database`, `/hr/candidates/*` (~15 routes), `/hr-records`, `/hr-record/{id}`
- Masters: `GET/POST /masters/opportunities`, `GET/POST /masters/customers`
- Templates: `/job/config`, `/job/configs`, `/job/template/*`
- **ATS (already exists!):** `POST /ats/score`, `POST /ats/score/upload`, `GET /candidates/ranked` (`backend/ats.py`, 582 lines)
- Proctoring: `/proctor/*`; Integrity: `/interview/violation`, `/interview/integrity-logs`
- Admin: `/api/prompt-logs*`, `/admin/ai/usage`, `/admin/hr-code*`
- Ops: `/version`, `/health/*`, `/healthz`, `/readyz`

### Tests
~60 pytest files in `backend/tests/`. No conftest/pytest.ini. Run: `cd backend && python -m pytest tests/`.

---

## 2. What exists — Frontend (Model-F-V2)

**Hybrid, NOT a single React app:**

1. **Main HR + candidate app = vanilla HTML/JS.** `frontend/index.html` (6,637 lines, all CSS inlined as design tokens) + `frontend/js/` ES modules (9,181 lines, no bundler). Screens toggled via `showScreen` in `js/app.js`: auth, HR portal, device test, live interview, results, invite states.
2. **Admin dashboard = React 18 + TypeScript + Vite + Tailwind**, served at `/admin` (`frontend/admin-dashboard/`). **No react-router** — custom `View` union + `pushState` routing in `App.tsx`. No Redux/Zustand — local state + hooks + module-level GET cache in `src/api/client.ts`. Icons: lucide-react. Charts: recharts. Animations: framer-motion. PDF: jspdf.
   Pages: HrDashboard, Dashboard/Candidates (reports), CandidateReportPage, CandidateInterviews, UpcomingInterviews, Templates, TemplateForm, ATS, PromptLogs, IntegrityLogs, QuestionBank (super-admin).

### API wiring
- All API calls are **same-origin relative paths** (`API = ""`); locally the FastAPI monolith serves the static frontend; on Vercel, `vercel.json` rewrites proxy each API prefix to the Render backend. New API prefixes must be added to `scripts/vercel-build.mjs` `apiPrefixes` + `vite.config.ts` proxy list.
- **JWT in localStorage** (`authToken`, `authUser`, `authTokenExpiryIst`), attached as `Authorization: Bearer` by `js/core.js::apiFetch` (vanilla) and `src/api/client.ts::authFetch` (React). 401 → auto-clear + reload.

---

## 3. What needs to be ADDED

- **All CRM tables** (~45 new tables per the spec): master data, customers (full), opportunities (full), requirements workflow, resumes/ATS pipeline, candidates, candidate profiles, projects, timesheets, POs/invoices/TDS, employees/HR, roles/user_roles/notifications.
- **7-role RBAC** (Admin, Sales, Sales_Head, RMG, TA, HR, Finance) — roles + user_roles tables, JWT role claims, and a reusable FastAPI `Depends` (`role_required([...])`).
- **All CRM API modules** (routers/schemas/services per module) — the spec's Phase 3 list.
- **Requirement approval workflow engine** with server-side status-transition validation + activity logs.
- **Requirement-scoped ATS pipeline** — extend the existing `ats.py` scorer to score against `requirement_skills` and persist `resumes` rows with score breakdown (currently it scores resume-vs-JD ad hoc with a JSON cache).
- **CRM frontend** — role-based sidebar and ~15 page groups. Natural home: extend the **React admin dashboard** (Tailwind/Vite patterns already established); the vanilla app stays as the candidate interview experience.
- **Migration tooling decision** — Alembic is not present; either introduce it or follow the existing idempotent-DDL startup pattern.
- **Notification system** (table + triggers + bell UI).
- **Invoice PDF generation** (WeasyPrint/ReportLab — neither currently installed).

## 4. What needs to be MODIFIED

- `backend/auth_db.py` — add new DDL (dual-dialect if SQLite dev support is kept), extend `registration_data` (role_id / employee_id / is_active).
- `backend/main.py` auth — extend JWT claims + convert `_require_user` pattern into a `Depends`-style `role_required` for new routers (existing routes untouched).
- `/masters/opportunities` + `/masters/customers` and `opportunity_master`/`customer_master` — reconcile with the new full `customers`/`opportunities` tables (see conflicts).
- AI interview session creation — accept optional `candidate_id` + `opportunity_id`, write results back to `skill_evaluations` (Phase 5).
- `job_templates` — link to new requirement/opportunity FKs instead of free-text `customer_name`.
- Frontend build config — new API prefixes in `vercel-build.mjs` + `vite.config.ts`; new React pages/nav in `App.tsx`.

## 5. Conflicts & structural issues to resolve BEFORE Phase 2

1. **BLOCKER — restore `backend/` from git** (see §0).
2. **Prompt assumes an ORM; the codebase is raw SQL.** Options: (a) follow existing raw-SQL pattern (consistent but verbose for ~45 tables), or (b) introduce SQLAlchemy + Alembic for new CRM tables only, leaving existing tables raw. **Recommendation: (b)** — 45 tables with workflow logic in raw dual-dialect SQL is a maintenance trap. Your call.
3. **Prompt assumes PostgreSQL; local default is SQLite.** Production (Render/Supabase) is Postgres. Decide: keep dual-dialect support for the new tables (harder, esp. JSONB/enums) or make CRM Postgres-only and require Postgres locally via docker-compose. **Recommendation: Postgres-only for CRM tables.**
4. **`customers` / `opportunities` vs existing `customer_master` / `opportunity_master`.** New tables can be created with the spec's names, then migrate master rows in and repoint `/masters/*` endpoints (kept as thin aliases so the existing template UI doesn't break).
5. **"Candidate" concept is overloaded**: `registration_data.role='candidate'` (auth identity), `interview_records`/`interview_progress` (interview data), vs the new `candidates` ATS entity. Plan: new `candidates` table is the CRM source of truth; interview sessions link via `candidate_id` FK added in Phase 5.
6. **Roles**: `registration_data.role` is CHECK-constrained to `('hr','candidate')`. New 7-role model must either relax that constraint or (cleaner) use `user_roles` join table and keep the legacy column for the interview platform.
7. **Frontend is NOT one React app.** The spec's Phase 4 ("follow existing React patterns") maps to `frontend/admin-dashboard/` (React+Vite+Tailwind). Its custom no-router navigation will strain under ~15 new modules — recommend introducing react-router **inside the admin app only**, or accept extending the `View` union.
8. **Two repos, one deployment.** Backend serves frontend statics locally; Vercel proxies to Render. Every new API prefix (`/api/customers`, etc.) must be registered in the Vercel rewrite generator — easy to forget. The spec's `/api/*` prefix convention helps: one `/api` rewrite already exists.
9. **7,337-line `main.py`.** New CRM modules must go in proper routers (`backend/routers/{module}.py`) per the spec — do not grow main.py.
10. **No test scaffolding conventions** (no conftest). New CRM tests will need a conftest + test-DB fixture.

---

## Proposed decisions needing your confirmation

| # | Decision | Recommendation |
|---|---|---|
| 1 | Restore `backend/` from git | You run `git restore backend/` |
| 2 | ORM for new CRM tables | SQLAlchemy + Alembic (new tables only) |
| 3 | DB engine for CRM | Postgres-only (drop SQLite for CRM tables) |
| 4 | masters reconciliation | Migrate into new tables, keep `/masters/*` as aliases |
| 5 | CRM UI location | Extend React admin dashboard; add react-router inside it |

Confirm (or override) these and I'll proceed to Phase 2.
