# Dead Code Cleanup Report

**Branch:** `chore/dead-code-cleanup` (both repos)  
**Date:** 2026-07-21  
**Scope:** `AI-Interview-Model-B-V2/backend`, `AI-Interview-Model-F-V2/frontend/admin-dashboard`

---

## Phase 1 — Detection Summary

Tools run: **knip**, **ts-prune**, **depcheck** (frontend); **ruff F401/F841**, **vulture (≥80%)**, **deptry** (backend).

### Baseline gates (before cleanup)

| Gate | Result |
|------|--------|
| Frontend `typecheck` | ✅ pass |
| Frontend `lint` | ⚠️ 76 errors / 26 warnings (pre-existing a11y + hooks) |
| Frontend `build` | ✅ pass (chunk warnings only) |
| Backend `import main` | ✅ pass |
| Backend `ruff F401,F841` | ❌ 60 issues |
| Backend `pytest -q` | ⚠️ 323 pass / 3 fail (pre-existing: boundary label, password bcrypt) |
| Backend `alembic current` | ✅ `0035 (head)` |
| Backend `compileall` | ✅ pass |

### Frontend — Confirmed unused (removed in Phase 2)

| Item | Evidence | Action |
|------|----------|--------|
| `src/crm/pages/ProjectHistory.tsx` | Not in `routes.ts` lazy map; `ProjectOverviewTab` has zero importers (`Projects.tsx` uses inline `OverviewTab`) | **Deleted** |
| `scripts/_repro_phone_hops.cjs` | One-off phone repro script; zero references | **Deleted** |
| `@testing-library/user-event` | In `package.json` only; no test imports | **Removed from devDependencies** |
| Duplicate `export default` on 6 CRM pages | `routes.ts` lazy-loads named exports only | **Removed default exports** |
| `useCrmPathActive` import in `CrmApp.tsx` | ESLint unused-var | **Removed import** |

### Frontend — Uncertain (kept)

| Item | Reason |
|------|--------|
| `scripts/check-contrast.mjs` | Documented WCAG verifier (`DESIGN-DECISIONS.md`, token READMEs) |
| `autoprefixer`, `postcss`, `tailwindcss` | depcheck false positive — used by PostCSS/Tailwind build chain |
| `@vitest/coverage-v8` | Unlisted but referenced in `vitest.config.ts` — add to devDeps if coverage runs in CI |
| 33 knip “unused exports” (e.g. `crmUrl`, `canEditTab`, API re-exports) | Public module surface / future hooks; ts-prune marks many as “used in module” |
| `src/api/index.ts` re-exports (getSchedules, deleteSchedule, …) | HR dashboard API surface; may be used by lazy pages or future features |
| `Tilt3D`, `PressableScale`, motion/RBAC helpers | Component library exports; safe to trim later with explicit audit |

### Backend — Confirmed unused (removed in Phase 2)

| Item | Evidence | Action |
|------|----------|--------|
| 50× unused imports (F401) | ruff across `main.py`, tests, utils, `auth_db.py`, … | **Auto-removed** |
| 10× unused locals (F841) | `ql`, `jd_lower`, dead `coach` assignments, test fixtures | **Removed** |
| `tests/test_submit_background_finalize.py` | Patched obsolete `main.upsert_hr_record` (no longer imported) | **Updated to patch `_persist_hr_record_mirror`** |

### Backend — Uncertain (kept)

| Item | Reason |
|------|--------|
| Alembic migration unused imports (`0002_*.py`) | User rule: do not modify migration files |
| Pydantic validator `cls` params | Required by Pydantic `@field_validator` signature |
| `alembic/env.py` `compare_to`, `reflected` | Alembic hook signature |
| `auth_db.py` `exc_val`, `exc_tb` | Exception context manager protocol |
| `uvicorn`, `python-multipart`, `tzdata`, `supabase`, `pytest` (deptry DEP002) | Runtime / deploy / test deps not imported in app code |
| `slowapi` (deptry DEP001) | Used in `rate_limit.py` but **missing from requirements.txt** — fix separately (add dep), do not remove |
| `generate_questions_with_model` in `ai.py` | Used by `question_service.py`; only unused re-import in `main.py` (removed) |

---

## Phase 2 — Removals (committed)

### Batch A — Backend unused imports/locals
- ruff `--fix` on all files except `alembic/versions/*`
- Files: `main.py`, `ai.py`, `ats.py`, `auth_db.py`, `prompt_builder.py`, utils, tests

### Batch B — Backend test fix
- `test_submit_background_finalize.py`: monkeypatch `_persist_hr_record_mirror` instead of removed `upsert_hr_record` re-export

### Batch C — Frontend dead files
- Delete `ProjectHistory.tsx`, `_repro_phone_hops.cjs`

### Batch D — Frontend deps & exports
- Remove `@testing-library/user-event`
- Remove duplicate default exports from 6 CRM pages
- Remove unused `useCrmPathActive` import

---

## Phase 3 — Optimization (conservative)

### Frontend bundle (after cleanup)

| Chunk | Size (minified) |
|-------|-----------------|
| `index-BSqv-sPT.js` (main) | 494.94 kB |
| `vendor-pdf-CzyCalQe.js` | 582.67 kB |
| `vendor-charts-FjJQI1N2.js` | 399.22 kB |
| `CustomerFormModal-*.js` | 147.26 kB |
| `Employees-*.js` | 66.36 kB |

**Already optimized:**
- CRM routes lazy-loaded via `React.lazy` in `routes.ts` and `App.tsx`
- PDF/charts/motion in separate `manualChunks` (`vite.config.ts`)
- PDF worker excluded from module preload

**Not changed (would need behavior review):**
- `CustomerFormModal` (147 kB) still pulled with Customers/Opportunities — could lazy-load modal only
- Pre-existing lint/a11y debt (76 errors) — out of scope for dead-code pass

### Backend queries / indexes
- No new indexes added — no hot-path N+1 identified with zero-risk grep during this pass
- Recommend separate perf audit on timesheet list + employee project-history endpoints

---

## Phase 4 — Deploy-safety (after cleanup)

| Gate | Result |
|------|--------|
| Frontend `typecheck` | ✅ |
| Frontend `lint` | ⚠️ unchanged pre-existing errors |
| Frontend `build` | ✅ zero errors |
| Backend `ruff F401,F841` | ✅ all passed |
| Backend `import main` | ✅ |
| Backend `pytest -q` | ⚠️ 322 pass / 3 fail (same pre-existing: boundary label, password bcrypt×2) |
| Backend `compileall` | ✅ |
| Backend `alembic current` | ✅ head |

### Size delta
- **Frontend:** main bundle unchanged (~495 kB) — removed source not in production graph
- **Backend:** ~29 unused import lines + 10 dead locals removed; no functional API changes
- **Deleted files:** `ProjectHistory.tsx` (~24 KB source), `_repro_phone_hops.cjs` (~2 KB)

---

## Phase 5 — Second verification pass (independent, 2026-07-21 PM)

A second detector run (ruff/vulture/deptry + knip/ts-prune/depcheck, Windows ground truth) with
dynamic-usage cross-checking (monkeypatch strings, React.lazy map, models/__init__ metadata,
re-export chains, tests-count-as-used) confirmed Phases 1–4 and found a small remainder.

### Additional removals (applied + re-verified)

| Item | Where | Evidence |
|---|---|---|
| `supabase` dependency | backend/requirements.txt | Zero `import supabase`/`create_client` anywhere; auth_db.py only string-matches the `supabase.com` hostname — psycopg2 makes the connection. (Upgrades Phase-1's "uncertain" verdict.) |
| `export { sectionVisible }` + its import | opportunity/FormRenderer.tsx | Consumers import it from opportunitySchema; only SectionFields/OptionsMap come from FormRenderer |
| `CUSTOMER_TYPE_OPTIONS` | opportunity/opportunitySchema.ts | Definition was the sole occurrence; form uses `customerTypeOptionsForPo()` |
| `SUPERADMIN_ROLES` | src/lib/rbac.ts | Sole occurrence; `isAdmin()` hardcodes the roles |
| `listChildMotion` | src/lib/motionPresets.ts | Sole occurrence incl. tests/docs |

Post-removal gates: `compileall` ✅ · `tsc --noEmit` ✅ · `ruff F401/F841` → only the two
protected `0002_*` migration imports remain (policy keep; `models.Base` is a metadata side-effect).

### Additional optimization flags (report-only, not applied)

1. **N+1s (Read-verified):** `services/finance.py:563-567` — per-invoice `db.get(Project)`+`db.get(Opportunity)`+`db.get(Timesheet)` inside the loop → replace with one joined select; `services/project_employees.py:832-841` — same pattern per assignment. Probable (verify before fixing): `branch_policy.py:203`, `timesheets.py:849/891`, `ai_interview_bridge.py:288`.
2. **Missing `index=True` on hot FKs:** `leave_applications.project_id`, `employees.reporting_manager_id`/`reporting_hr_id`, `interview_bookings.candidate_id`/`slot_id`, candidate-activity `requirement_id`, `contacts.branch_id`, `candidate_skills.skill_id`. Already indexed (no action): `invoices.po_id`, `timesheets.employee_id/project_id`, `timesheet_entries.timesheet_id`, `leave_applications.employee_id`.
3. **Leftover no-op expressions from the Phase-2 autofix:** `ai.py` `(q or "").lower()`, `ats.py` `(jd_text or "").lower()`, 3× bare `coach_hints_text()` calls in main.py (file I/O, result discarded) — trivial manual deletes.
4. **Dep hygiene:** add `slowapi` + explicit `starlette` to requirements.txt; run `npm install` to resync package-lock (still lists removed `@testing-library/user-event`); add `@vitest/coverage-v8` to devDependencies.

### Files touched in Phase 5 (glance for truncation before building)

`backend/requirements.txt`, `src/crm/pages/opportunity/FormRenderer.tsx`,
`src/crm/pages/opportunity/opportunitySchema.ts`, `src/lib/rbac.ts`, `src/lib/motionPresets.ts`.

---

## Items for manual review

1. **Add `slowapi` to `requirements.txt`** — imported by `rate_limit.py`, deptry flags missing
2. **Fix 3 pre-existing pytest failures** before merge (boundary metadata label, bcrypt hashing config)
3. **Frontend lint debt** — 76 a11y/hooks errors; separate cleanup PR recommended
4. **`@vitest/coverage-v8`** — add to devDependencies or remove from vitest config
5. **Knip unused exports** — trim in a follow-up with per-export grep if desired

---

_Last updated: Phase 4 complete on `chore/dead-code-cleanup`._
