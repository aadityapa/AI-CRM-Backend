# Karnex AI — Phase 1 Optimization Analysis Report
**Date:** 2026-07-01  
**Scope:** Backend `E:\AI-Interview-Model-B-V2` · Frontend `E:\AI-Interview-Model-F-V2`

---

## Executive Summary

| Area | Finding | Risk if unchanged |
|------|---------|-------------------|
| Backend | **2,917 duplicate files** in nested `services/question_bank/question_bank/...` | Disk/CI bloat, confusion |
| Backend | `export_questions_csv` silently truncates at 100 rows | Data loss on QB export |
| Backend | `list_interview_schedules` unbounded | Slow HR schedules API |
| Backend | `test-prompt` OpenAI calls uncached | Repeated HR preview cost |
| Frontend | `getCandidateById` fetches full 200-candidate dashboard | ~200KB+ redundant payload per report view |
| Frontend | 67 `console.info/warn` in candidate JS | Runtime overhead, log noise |
| Frontend | Three.js rAF loop never paused during interview | GPU/battery drain |

Protected feature areas (Interview Flow, Auth, QB, Evaluation, Reports, ATS, Integrity, AI Logs, Templates, Dashboard, OpenAI, Candidate Flow) were **not** removed.

---

## 1. Unused / Dead Code

### Backend — SAFE TO DELETE
| Item | Path | Verification |
|------|------|--------------|
| Nested QB duplicate tree | `backend/services/question_bank/question_bank/` (2,917 files) | Zero imports; production uses `services/question_bank/` only |
| Dead import | `get_database_snapshot` in `main.py:104` | Imported, never called |

### Backend — KEEP (documented, not deleted)
| Item | Reason |
|------|--------|
| `prompts/interview/system_prompt.py`, `user_prompt.py` | Used by tests |
| `hr/repository.py` | JSON fallback when DB empty |
| `generate_https_certs.py` | Standalone CLI |
| `get_database_snapshot()` in `auth_db.py` | Not exposed; keep function, remove dead import |

### Frontend — No orphaned source files
All 61 `admin-dashboard/src/` files reachable from `main.tsx` → `App.tsx`.

### Frontend — Dead API exports (safe to remove)
| Export | File | Used? |
|--------|------|-------|
| `getCandidates()` | `api/index.ts` | No |
| `getSessions()` | `api/index.ts` | No |
| `getSessionById()` | `api/index.ts` | No |
| `getInterviewRecord()` | `api/index.ts` | No |

`getCandidateById()` used in `CandidateReportPage.tsx` — **replace** with `hist.candidate` from history endpoint.

### Candidate JS
| File | Status |
|------|--------|
| `avatar.js` | No-op stub, still imported — keep (removing import risks breakage) |

---

## 2. Duplicate Logic

| Duplication | Locations |
|-------------|-----------|
| Monolith DB vs layered repos | `auth_db.py` (~3,500 lines) vs `services/question_bank/repository.py` |
| Dual HR record storage | Postgres `interview_records` + JSON `hr/repository.py` |
| Question generation paths | `ai.generate_questions_with_model` vs `services/interview/question_service` |
| Dashboard data fetch | `HrDashboard.tsx` + `Dashboard.tsx` both call `getDashboardData(200)` |
| Two dashboard page implementations | `HrDashboard.tsx` vs `Dashboard.tsx` |

---

## 3. Console Logs / Debug / TODOs

### Backend
- **No `logger.debug`** in application code
- **`print()`** only in `generate_https_certs.py`, `scripts/verify_invite_login_next.py`
- **50+ `logger.info`** on hot paths (`main.py` answer flow, invite bootstrap)
- **Sensitive:** `transcript_preview` in analyze-answer-completion logs (`main.py:3648`)
- **TODO/FIXME:** None in production paths

### Frontend Admin (`src/`)
- **1** `console.error` in `PromptLogs.tsx:190`
- Vite does not strip `console.*` in production builds

### Frontend Candidate (`frontend/js/`)
| File | console.info | console.warn | Total |
|------|-------------|--------------|-------|
| `candidate.js` | 32 | 8 | 42 |
| `app.js` | 7 | 3 | 11 |
| `interview_auto_advance.js` | 3 | 2 | 5 |
| `interview_finalization.js` | 3 | 1 | 4 |
| Others | 2 | 3 | 5 |
| **Total** | **47** | **17** | **67** |

---

## 4. API / Query Performance Issues

### Slow / unbounded endpoints
| Endpoint | Issue |
|----------|-------|
| `GET /hr/schedules` | No LIMIT — returns all schedules |
| `GET /hr-records` | Hard limit 200, no offset |
| `GET /job/configs` | All templates with full `jd_text` |
| `GET /api/question-bank/export` | Truncates at 100 due to `list_questions` cap |
| `interview_progress` queries | `SELECT *` (all columns needed for restore) |

### N+1 patterns
| Location | Pattern |
|----------|---------|
| `_bulk_rescore_interviews` | Per-ID OpenAI rescore |
| `/candidates/ranked` | `ats_score()` per record in-memory |
| `IntegrityLogs.tsx` | Per-row detail fetch when violations already in list |

### Already optimized
- `/hr/dashboard` batches templates via `get_job_template_summaries_batch`
- `list_interview_integrity_logs` single-query (no N+1)
- QB list endpoints paginated

### OpenAI caching (existing)
- `sample-questions` — cached when `OPENAI_RESPONSE_CACHE_ENABLED`
- `evaluate_turn` — cached

### OpenAI caching candidates
- `test-prompt`, `analyze_answer_completion`, ATS LLM, strength/weakness analysis, transcribe/TTS

---

## 5. Database Index Gaps

**Existing:** email, name, dates, job_id, schedule hr_username, progress invite/status

**Recommended (implemented in Phase 3):**
- `interview_schedule (hr_username, created_at_ist DESC)` composite

---

## 6. Security Findings (safe fixes only)

| Issue | Severity | Action |
|-------|----------|--------|
| `transcript_preview` in logs | Medium | Remove preview, keep length |
| Default JWT secret fallback | High | Document; no code change (env-dependent) |
| `access_key` in schedule API | Medium | Document; masking is behavior change |
| SQL injection | Low | Parameterized queries throughout |

---

## 7. Frontend Performance Gaps

| Gap | Impact |
|-----|--------|
| `getCandidateById` → full dashboard | Large redundant network on report page |
| Static `ReportCharts` import (recharts) | Heavy report chunk |
| `vendor-pdf` preloaded on all admin pages | Unnecessary initial load |
| No `manualChunks` for framer-motion/recharts | Suboptimal caching |
| Three.js rAF never paused | GPU during interviews |
| Toast `setTimeout` without cleanup | Minor timer leaks |

---

## 8. Items NOT Deleted (risky)

- `prompts/interview/*` — test dependencies
- `hr/repository.py` — legacy JSON mirror
- `avatar.js` — imported stub
- Candidate `app.js` monolith — code-splitting is high-risk without full E2E

---

*Phase 2–4 implementations follow in code changes and final verification report.*
