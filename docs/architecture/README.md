# KARNEX AI Interview + CRM — Architecture Documentation

Auto-generated, living architecture documentation for the KARNEX platform. All
diagrams use [Mermaid](https://mermaid.js.org/) and render natively on GitHub and
in the Cursor/VS Code Markdown preview.

> **Maintenance policy:** These diagrams are kept in sync with the codebase. Whenever
> a module, model, route, or workflow changes, the corresponding diagram file is
> updated in the same change set. See [`.cursor/rules/architecture-docs.mdc`](../../.cursor/rules/architecture-docs.mdc)
> for the rule that governs this, and [How to regenerate](#how-to-regenerate) below.

## The system in one paragraph

KARNEX is a single **FastAPI** backend (`backend/`) that fuses two systems sharing one
**PostgreSQL** database: a **legacy AI-Interview platform** (raw-SQL, endpoints defined
directly in `main.py`, secured by a `role`-claim JWT) and a modern **Karnex CRM**
(SQLAlchemy 2.0 + Alembic, 20 routers under `/api/*`, secured by a 7-role DB-backed
RBAC model). The backend also serves two frontends (`AI-Interview-Model-F-V2/frontend`):
a vanilla-JS **candidate/HR interview UI** and a **React admin dashboard** (which embeds
the CRM). External integrations are **OpenAI** (TTS, Whisper transcription, question
generation, evaluation) and optional **SMTP** for candidate invite emails. The two halves
are bridged by the `ai_interview_links` table (join key `invite_token`).

## Diagram index

| # | File | Contents |
|---|------|----------|
| 1 | [`01-system-architecture.md`](./01-system-architecture.md) | High-level system architecture, deployment/runtime topology, external integrations |
| 2 | [`02-module-flow.md`](./02-module-flow.md) | Backend module map, frontend module map, request pipeline, per-module flows |
| 3 | [`03-database-er.md`](./03-database-er.md) | ER diagrams per domain (RBAC, Sales, Recruitment, Delivery, Finance) + legacy tables |
| 4 | [`04-api-flow.md`](./04-api-flow.md) | API surface map, auth dependency chains, request/response envelope flows |
| 5 | [`05-user-journeys.md`](./05-user-journeys.md) | User-journey diagrams for each role (Candidate, HR, Admin, Sales, Sales_Head, RMG, TA, Finance) |
| 6 | [`06-sequence-diagrams.md`](./06-sequence-diagrams.md) | Sequence diagrams for the key workflows (auth, invite, interview loop, HR decision, CRM bridge) |

## Key architectural facts (source of truth for the diagrams)

- **Two auth realms, one secret.** Legacy `role`-claim JWT (`hr`/`candidate`/`manager`/`admin`)
  via `_require_user` for `@app.*` interview routes; DB-backed 7-role RBAC
  (`Admin, Sales, Sales_Head, RMG, TA, HR, Finance`) via `CurrentUser`/`role_required`
  for `/api/*` CRM routes. Both trust the same HS256 token.
- **CRM stack:** `routers/crm/*` → `services/*` → SQLAlchemy `models/*` → Postgres (Alembic-managed).
- **Interview stack:** `main.py` handlers → `ai.py` / `services/interview` / `ats.py` →
  OpenAI (5 purpose-keyed clients) + `auth_db.py` (raw SQL) + in-memory `session.py`.
- **Bridge:** `services/ai_interview_bridge.py` links CRM candidate profiles/resumes to
  interview schedules and syncs results back through `ai_interview_links`.

## How to regenerate

The Markdown files are the source of truth and render without any build step.

**Optional SVG/PNG export** (requires Node.js, which the project already uses):

```bash
# from the repo root
node docs/architecture/scripts/render.mjs
```

This extracts every Mermaid block from the `.md` files and writes `.svg` (and `.png`
when supported) into `docs/architecture/assets/` using `@mermaid-js/mermaid-cli`
(invoked via `npx`, no install needed). If the tool cannot download its headless
browser (offline/locked-down env), the Markdown diagrams still render everywhere.

## Conventions

- One concern per diagram; large domains are split so each diagram stays readable.
- Legacy (raw-SQL) tables and soft/logical joins are drawn with dashed edges or noted explicitly.
- Role names match `models/rbac.py::RoleName` exactly.
- Endpoint paths and table/column names match the code exactly so diagrams are greppable.

_Last synced: 2026-08-19 — Opportunity/requirement pipeline + CTC/policy prefill; session keep-alive and CRM dashboard/UI polish._
