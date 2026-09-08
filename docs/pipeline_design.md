# KARNEX Opportunity → Hire Pipeline — Design (Phase 0)

Status: **design / pre-build.** This maps the requested pipeline onto the
**existing** codebase (audited 2026-07-09) and defines the state machines, schema
deltas, endpoints, and async boundaries. It deliberately **reuses** what exists
rather than rebuilding it. Items marked **[NEW]** don't exist yet; **[HAVE]** do.

---

## 1. Stack (detected)

- Backend: **FastAPI + SQLAlchemy 2.0 + Alembic + Postgres** (`AI-Interview-Model-B-V2/backend`). Legacy interview data in `auth_db.py` (raw SQL, SQLite/Postgres).
- Frontend: **React + TypeScript + Vite + Tailwind + Framer Motion** (`AI-Interview-Model-F-V2/frontend/admin-dashboard`) + a vanilla-JS interview portal.
- Auth/roles: JWT; CRM RBAC in `user_roles`/`roles` (Admin, CEO, Sales, Sales_Head, RMG, TA, HR, Finance). Enforced server-side in `crm_deps` (`role_required`, `get_current_user`).
- Passwords: bcrypt (migrated this session). File/token hashing: SHA-256. No task queue; optional Redis for rate-limit/sessions only.

## 2. How the requested state machines map to existing entities

The spec's three state machines already have homes — with gaps:

| Spec entity | Existing home | Verdict |
|---|---|---|
| **Opportunity** DRAFT→PENDING_SALES_HEAD→PENDING_RMG→APPROVED→CLOSED | `Opportunity.approval_status` (`Pending_Sales_Head_Approval, Approved, Rejected`) + `pipeline_stage` | **[HAVE]** Sales-Head gate + central transition map. **[NEW]** the RMG gate currently lives on the spawned **Requirement**, not the Opportunity. **[NEW]** `CHANGES_REQUESTED`. |
| **Position** OPEN→JOB_POST_CREATED→PUBLISHED→SOURCING→FILLED/CANCELLED | `Requirement.status` (`Open_For_Sourcing, Posted_On_Portals, In_Progress, Fulfilled, Closed, Cancelled`) | **[HAVE]** Requirement = Position. **[NEW]** a real `JobPost` child (versioned) distinct from the free-text `RequirementJobPosting` URL record. |
| **Application** RECEIVED→PARSED→SCORED→INVITED→…→SELECTED/REJECTED (+failures) | split across `Resume.ats_status`, `Resume.ai_interview_status`, `CandidateProfile.pipeline_status` | **[NEW]** unify into ONE `Application.stage` enum with a single transition function; keep the existing tables as backing data. |

**Decision — do not fork the data model.** Extend the existing entities. Introduce
one **unified `Application` stage machine** that supersedes the split resume/profile
statuses, and add the RMG gate + `CHANGES_REQUESTED` to the Opportunity so the spec's
Opportunity machine is literally the columns.

## 3. Target state machines (explicit, enum-backed, one transition fn each)

All transitions go through `services/state_machine.py::transition(entity, to, actor, reason)`
which: (1) looks up `LEGAL[from] → {to}`; (2) checks `AUTHORITY[to] ∩ actor.roles`;
(3) writes an **immutable audit row**; (4) raises `IllegalTransition`/`Forbidden`
otherwise. **No endpoint sets a state column directly.**

```
Opportunity:  DRAFT → PENDING_SALES_HEAD → PENDING_RMG → APPROVED → CLOSED
              {PENDING_*} → REJECTED | CHANGES_REQUESTED(→DRAFT)
Position/Req: OPEN → JOB_POST_CREATED → PUBLISHED → SOURCING → FILLED | CANCELLED
Application:  RECEIVED → PARSED → SCORED → BELOW_THRESHOLD(→ARCHIVED)
                                        → INVITED → SLOT_CONFIRMED → INTERVIEW_SCHEDULED
                                        → INTERVIEW_COMPLETED → REPORT_READY → SELECTED | REJECTED
              failures: PARSE_FAILED, SCORING_FAILED, INVITE_FAILED, NO_SHOW, EXPIRED
```

Role authority (server-side): Sales creates/edits DRAFT; Sales_Head acts on
PENDING_SALES_HEAD; RMG on PENDING_RMG and the final SELECTED/REJECTED; TA on
Position→JobPost→Publish and the human-review invite gate.

## 4. Audit — generic, append-only [NEW]

Add `audit_log(id, entity_type, entity_id, actor_id, action, from_state, to_state,
reason, correlation_id, created_at)`. Written only by the transition fn and outreach
jobs. Never updated/deleted (enforced by app discipline + a DB trigger blocking
UPDATE/DELETE in prod). Existing per-entity activity logs stay for free-text comments.

## 5. Schema deltas (Alembic 0011+)

- `opportunities`: add `PENDING_RMG`, `CHANGES_REQUESTED` to the approval enum; add `rmg_approved_by/_at`, `changes_requested_comment`.
- **`job_posts`** [NEW]: `id, requirement_id, version, title, description, required_skills, preferred_skills, exp_min/max, education, location, salary_min/max, openings, status(draft/preview/published/archived), score_threshold (default 50), auto_send (bool, default FALSE), created_by, published_at`. Edits after publish → new `version` row.
- `resumes`: add `file_sha256 (indexed)`, `file_bytes`, `stored_path`; unique-ish dedupe on `(requirement_id, file_sha256)`.
- **`applications`** [NEW] (or a `stage` column consolidating resume/profile): `stage` enum + `stage_reason`, `job_post_id`, `score_total`, `score_breakdown (JSONB)`, `evidence (JSONB)`.
- **`interview_slots`** [NEW]: `id, requirement_id/interviewer, start_utc, end_utc, status(OPEN/HELD/BOOKED/CANCELLED), held_until, application_id`. Unique partial index `(id) where status='BOOKED'`; booking via `SELECT … FOR UPDATE`.
- **`outreach_messages`** [NEW]: `id, application_id, channel(email/whatsapp/sms), template, status(queued/sent/failed/delivered), idempotency_key (unique), attempts, last_error`.
- **`booking_tokens`/`interview_tokens`** [NEW]: store **SHA-256 hash** only, `expires_at`, `used_at`, single-use; constant-time compare.
- `whatsapp_consent(candidate_id, consented, opted_out_at)` [NEW].

## 6. Endpoints (all server-side role-gated; IDOR-checked)

| Method/Path | Role | Notes |
|---|---|---|
| POST `/api/opportunities/{id}/request-changes` | Sales_Head, RMG | [NEW] reason required → DRAFT |
| POST `/api/opportunities/{id}/rmg-approve` \| `/rmg-reject` | RMG | [NEW] second gate |
| POST `/api/requirements/{id}/job-posts` (+ `/{v}/publish`,`/preview`) | TA | [NEW] versioned JobPost |
| POST `/api/job-posts/{id}/publish-portals` | TA | [NEW] adapter fan-out; **mock by default** |
| POST `/api/resumes` (upload) | TA | [HAVE]+ checksum/dedupe [NEW] |
| POST `/api/applications/{id}/score` | system/TA | [HAVE] scorer; queue [NEW] |
| POST `/api/applications/{id}/approve-invite` | TA | [NEW] human-review gate |
| GET  `/book/{token}` · POST `/api/book/{token}` | public | [NEW] slot booking, concurrency-safe |
| POST `/api/applications/{id}/decision` | RMG | [NEW] Select/Reject/Request-L2 |

## 7. Async boundaries

Scoring (OpenAI), outreach (email/WhatsApp), portal publish, reminders, no-show
sweeps **must be queued** with retries + backoff + dead-letter + idempotency key +
correlation id. The repo has **no queue today** → this is a required infra decision
(see §9). Synchronous: state transitions, slot booking (must be transactional), auth.

## 8. External-service failure handling

OpenAI: strict-JSON schema validate → on mismatch/429/timeout → retry ≤N → SCORING_FAILED
+ TA notify (never a default score). Email: primary channel. WhatsApp: enhancement;
on failure the flow completes over email. Portals: per-portal try/catch, partial
success recorded, exponential backoff, idempotent (never double-post). All secrets
in env; grep the client bundle to prove none leak.

## 9. Blocking decisions & required credentials (see chat)

1. **Scoring engine** — keep the deterministic keyword scorer (fixed this session), or add **OpenAI LLM** scoring (needs `OPENAI_API_KEY`, cost, nondeterminism)?
2. **Queue infra** — Celery+Redis (proper) vs. APScheduler/DB-polling (no Redis) vs. defer async?
3. **Job portals** — which (LinkedIn/Naukri/Indeed) + do you have API creds? Else mock adapter + doc.
4. **WhatsApp** — provider (Meta Cloud API / Twilio) + credentials; templates need Meta pre-approval (days). Email-first regardless.

## 10. Build order (after decisions)

P1 gaps (Opportunity RMG gate + request-changes + generic audit) → P3b (resume
SHA-256 + dedupe, no external dep — can start now) → unified Application stage →
P2 JobPost + mock portal adapter → P3c scoring behind chosen engine → P5 slots +
concurrency → P4 outreach (mock senders) → P6 L1 report + RMG decision → P7 cross-cutting →
P8 tests → P9 UI → P10 build. **Human-review mode ON, mock senders ON, no live portal/message without sign-off.**
```
