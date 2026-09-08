# Karnex CRM — Setup & Operations Guide

Karnex CRM extends the existing **AI Interview Platform** into a full CRM/ERP for a
technical staffing company: sales opportunities → requirement approvals → candidate
sourcing → ATS scoring → AI L1 interviews → client pipeline → projects → timesheets →
invoicing (GST/TDS). The interview platform keeps working standalone — every CRM
addition is additive and isolated.

- Backend repo: `AI-Interview-Model-B-V2` (FastAPI monolith + CRM routers)
- Frontend repo: `AI-Interview-Model-F-V2` (vanilla interview UI + React admin dashboard;
  the CRM lives inside the admin dashboard under the **CRM** nav item)

---

## 1. Prerequisites

- Python 3.10+ and Node 18+ on PATH
- **PostgreSQL** (Supabase or local). The CRM tables are Postgres-only; the legacy
  interview tables may keep using SQLite, but pointing `AUTH_DB_URL` at Postgres puts
  everything in one database (recommended).

## 2. Environment variables (`.env` at repo root)

| Variable | Purpose |
|---|---|
| `AUTH_DB_URL` | Postgres DSN used by the interview platform **and** (by default) the CRM |
| `CRM_DATABASE_URL` | Optional override — point the CRM at a different Postgres |
| `AUTH_SECRET` | JWT signing secret (set a long value in production) |
| `PUBLIC_BASE_URL` | Public/LAN base URL used in interview invite links |
| `PORT` | Backend port (default `2020`) |
| `TDS_RATE_PERCENT` | Default TDS rate for invoices (default `10`) |
| `CRM_AI_L1_NUM_QUESTIONS` / `CRM_AI_L1_DIFFICULTY` / `CRM_AI_L1_MODEL` | AI L1 interview defaults |
| `CRM_UPLOAD_DIR` | Optional override for uploaded files (default `data/crm_uploads`) |

Never hardcode credentials — everything comes from `.env`.

## 3. First-time setup

```bat
cd AI-Interview-Model-B-V2\backend
pip install -r requirements.txt

REM 1) Boot once so the legacy tables exist in Postgres (Ctrl+C after startup),
REM    or skip if the interview platform already ran against this database.

REM 2) Create all CRM tables + seed the 7 roles (additive; never touches legacy tables)
alembic upgrade head

REM 3) Seed master data (departments, designations, skills, locations, currencies,
REM    document types, leave types, AI pass threshold = 60%)
python seed_crm.py

REM 4) Make yourself Admin (register a user first via the app's Register screen)
python assign_crm_role.py <your-username> Admin
```

Migration commands: `alembic upgrade head` (apply), `alembic downgrade -1` (undo one),
`alembic history`. Alembic only manages CRM tables — legacy tables are excluded.

## 4. Running locally

```bat
cd AI-Interview-Model-B-V2
start_app.bat            REM HTTPS on https://localhost:2020 (self-signed)
start_app.bat --http     REM plain HTTP
```

The backend serves the frontend from the sibling `AI-Interview-Model-F-V2` repo and
auto-builds the React admin dashboard when needed. Open:

- Interview platform: `https://localhost:2020/`
- Admin dashboard + CRM: `https://localhost:2020/admin` → **CRM** in the top nav

## 5. Running on your network (LAN)

The server already binds `0.0.0.0`, so it is reachable from other devices:

1. Start it: `start_app.bat` (or `--http`).
2. Find your LAN IP: the startup banner prints it, or `ipconfig` (IPv4 Address), or
   `GET /network-info`.
3. On other devices open `https://<your-LAN-IP>:2020` (accept the self-signed
   certificate) or `http://<your-LAN-IP>:2020` in HTTP mode.
4. Set `PUBLIC_BASE_URL=https://<your-LAN-IP>:2020` in `.env` so interview invite
   links sent to candidates use the network address.
5. **Windows Firewall:** allow the port once (run as Administrator):

```bat
netsh advfirewall firewall add rule name="Karnex CRM 2020" dir=in action=allow protocol=TCP localport=2020
```

## 6. Roles

| Role | Can do |
|---|---|
| `Admin` | Everything: users, settings, master data; implicit access to all modules |
| `Sales` | Customers, opportunities, create/edit/submit requirements (own), candidate profiles (sales stages), projects, reports |
| `Sales_Head` | Requirement approval queue, opportunity archival, close/cancel requirements, projects, executive dashboard |
| `RMG` | Engineering review queue, RMG pipeline stage, RMG dashboard |
| `TA` | Sourcing view (only post-approval requirements), job postings, resumes/ATS, AI L1 scheduling, candidates, TA dashboard |
| `HR` | Employees, leave balances, timesheet approval |
| `Finance` | POs, invoices, payments, TDS, finance dashboard, project billing |

Assign roles in **CRM → Users** (Admin) or `python assign_crm_role.py <username> <Role>`.
RBAC is enforced server-side on every endpoint — the UI only mirrors it.

## 7. Verifying the installation (automated E2E)

With the server running and an Admin user bootstrapped:

```bat
cd AI-Interview-Model-B-V2
python scripts\verify_karnex_crm.py --base-url https://127.0.0.1:2020 --insecure ^
    --admin-user <your-username> --admin-pass <your-password>
```

~60 checks cover: role creation + login for all 7 roles, the full requirement workflow
(Sales → Sales Head → RMG → TA visibility rules, including negatives like TA-cannot-see-drafts
and wrong-role approvals → 403), resume upload → ATS scan → shortlist → AI L1 scheduling,
pipeline transitions with role authority, projects/timesheets with billable rollups,
PO → invoice → payment → TDS balance auto-updates, GST split, invoice PDF generation,
leave-balance recomputation, dashboards + CSV reports, and legacy `/version` + `/healthz`.
It creates uniquely-suffixed test users (password `Karnex@123` by default — these double
as **test credentials for each role**) and is safe to re-run.

## 8. Where things live (backend)

```
backend/
  crm_db.py                  # CRM Postgres engine/session (lazy; 503 when unconfigured)
  crm_deps.py                # JWT auth + role_required RBAC dependency
  models/                    # SQLAlchemy models — 46 CRM tables
  schemas/                   # Pydantic request/response models
  services/                  # Business logic (workflows, ATS, tax.py = GST/TDS layer,
                             #   ai_interview_bridge.py = interview integration)
  routers/crm/               # /api/* routers, registered via register_crm_routers(app)
  alembic/                   # Migrations (0001 = schema, 0002 = AI interview links)
  seed_crm.py                # Master-data seeding (idempotent)
  assign_crm_role.py         # CLI role bootstrap
scripts/verify_karnex_crm.py # E2E verification suite
```

## 9. AI interview integration (how it flows)

TA shortlists a resume → **Schedule AI L1 Interview** creates the candidate + pipeline
profile, then a real `interview_schedule` session (invite link + access key, skills from
the requirement). When the candidate finishes, the platform's completion hook pushes the
result back: resume becomes **Passed/Failed** against the Admin-configurable threshold
(CRM → Settings → `ai_interview_pass_threshold`), per-skill scores land in the profile's
skill-evaluation grid (reviewer column), the activity log gets an entry, and TA is
notified. The profile page's **AI Interview** tab lists sessions, scores, and deep-links
to the full interview report. Standalone interviews (scheduled from the HR portal) are
completely unaffected unless you pass the new optional `candidate_id` + `opportunity_id`
fields to `/hr/schedule-interview`.

## 10. Troubleshooting

- **CRM endpoints return 503** — no Postgres configured: set `AUTH_DB_URL` or
  `CRM_DATABASE_URL` and restart.
- **"No CRM role assigned"** in the CRM UI — run `assign_crm_role.py` or assign via Users.
- **Alembic error about `registration_data` missing** — boot the app once against the
  database first (it creates the legacy tables), then `alembic upgrade head`.
- **Invite links show localhost** — set `PUBLIC_BASE_URL`.
- **AI scheduling returns 502** — the legacy scheduler failed (check `AUTH_DB_URL` and
  server logs `logs/server*.log`).
