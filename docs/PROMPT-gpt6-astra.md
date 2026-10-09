# Prompt: AI interviews on GPT-6 Astra — make them work well, and show the model in the UI

> Paste everything below the line into Claude Code (or hand it to the developer). Open it on the backend repo
> (`aadityapa/AI-CRM-Backend`) with the frontend repo (`aadityapa/AI-CRM-Frontend`) checked out alongside.
>
> The evidence comes from the working trees `F:\AI-Interview-Model-B-V2` and `F:\AI-Interview-Model-F-V2` on 8 Oct 2026.
> If a line number has moved, find the code by the quoted snippet.

---

## Goal

Since 8 Oct 2026, production (karnexgroup.com) has run AI interviews on OpenAI **`gpt-6-astra`**
(`INTERVIEW_OPENAI_MODEL=gpt-6-astra`), through a local patch on the server. Make that permanent and make it work well:

1. Every interview call reaches Astra without an error, and nothing silently falls back to generic questions.
2. The candidate never waits in a long silence because a slower reasoning model is on the critical path.
3. HR / CRM pages never wait on a model call.
4. Every screen that mentions the AI shows **which model** ran it ("GPT-6 Astra" today). The name comes from ONE
   server-side source, so a later switch changes every label at once.
5. Admin / CEO can switch the interview model from Settings without a deploy or a restart (a rollback in seconds).

Read `CLAUDE.md` in both repos and `docs/GPT6_ASTRA_HANDOFF.md` first.

**Measured on the live API (8 Oct 2026):**

| What | Finding |
|---|---|
| Speed | Astra answers in 3–4.5 s per short call; gpt-4o-mini takes about 1 s |
| Price | $10 / $50 per 1M tokens in / out; cached input $1 (gpt-4o-mini: $0.15 / $0.60) |
| Rejected parameters | `temperature` ≠ 1, and `max_tokens` |
| Function tools | Refused on `/v1/chat/completions` at every `reasoning_effort` |

## Part A — the base patch (already in the F: working tree, not yet committed)

Check these are present. If your tree lacks them, implement them exactly as described. Then commit them with
everything else in this prompt.

| File | What it does |
|---|---|
| `backend/prompt_logger.py` | `is_reasoning_model(model)` matches `gpt-5*`, `gpt-6*`, `o1*`, `o3*`, `o4*`. `tracked_chat_completion` drops `temperature` and `max_tokens` for such a model. They are dropped, not translated: hidden reasoning tokens count against a cap, and a small cap returns an empty answer. Optional env `OPENAI_REASONING_EFFORT` is sent as `reasoning_effort`. |
| `backend/ai_help/assist.py` | `_model()`: `AI_ASSIST_MODEL` wins. Otherwise it uses the interview model, unless that is a reasoning model (Ask AI uses function tools), in which case `gpt-4o-mini`. |
| `backend/main.py` | `_resolve_interview_model(stored)`. **B1 below replaces its rule.** |
| `backend/services/ai_pricing.py` | `gpt-6-astra` $10 / $50 (cached $1); `gpt-6.1-sol` $2 / $10 (cached $0.10). |
| `backend/tests/test_reasoning_model_params.py` | 8 tests. Update the resolver test when you do B1. |

## Part B — backend

### B1. One switch decides the interview model

Today four places decide the model, and they disagree:

- `/setup` (HR-run interview, `main.py:3032`) and `/extract-skills` (`main.py:3406`) use
  `(custom_model or model).strip() or "gpt-4o-mini"` from the form. The legacy HR form always sends `gpt-4o-mini`,
  so **HR-run interviews still run on gpt-4o-mini**.
- `_pack_invite_config_into_notes` (`main.py:979`) and the CRM bridge (`services/ai_interview_bridge.py:342`,
  `CRM_AI_L1_MODEL`, default `gpt-4o-mini`) write a model into every schedule's notes.
- `_resolve_interview_model` (`main.py:951`, used at `main.py:1522`) treats a stored `gpt-4o-mini` as "the default"
  and keeps any other value.
- `config.py:23` `OPENAI_CHAT_MODELS = ["gpt-4o-mini"]` is served by `GET /models` (`main.py:7699`), which fills the
  HR form's model select.

Required:

1. **New pure module `backend/services/ai_models.py`.** It is the one place that knows model names, and it does no DB
   work at import. It provides:
   - `interview_model()`: Settings `ai.interview_model` (B9) → env `INTERVIEW_OPENAI_MODEL` → `"gpt-4o-mini"`.
   - `fast_interview_model(session_model)` (B2), `ocr_model()` (B7).
   - `model_label(id)`, `describe(id)`, `engine()` (B8), and `SUPPORTED_INTERVIEW_MODELS` (B9).
2. **The server decides the model when the session starts.** A model stored in a schedule is ignored unless the
   schedule config carries `"model_locked": true`. Nothing sets that flag today, because no UI lets anyone pick a
   model per interview. Keep it as the hook for a future per-template choice.
   - New schedules store `"model": ""`.
   - Drop `CRM_AI_L1_MODEL` from the bridge, or honour it only together with a lock.
   - Update `_resolve_interview_model` and its test.
3. `/setup` and `/extract-skills` use `interview_model()`. `custom_model` counts only with a lock. Their OCR call uses
   `ocr_model()` (B7), never the interview model.
4. `GET /models` returns `{"provider": "openai", "models": [interview_model()], "default": interview_model(),
   "labels": {id: label}}`.
5. A session keeps the model it started with (`meta["model"]`). A switch applies to interviews that start afterwards.
6. Re-score (`rescore_interview_record`, `main.py:5982`) scores with the `interview_model()` of the moment. The report
   records which model scored it (B8.4).

### B2. A fast model for the moments the candidate is waiting

These calls run while the candidate waits after answering:

- `services/interview/conversation.py`: `plan_turn`, `clarify_text` and `answer_closing_question`. Their deadlines are
  `TURN_PLAN_TIMEOUT_S = 6.0` and `REPLY_TIMEOUT_S = 8.0`. Their model comes from `_model_for(meta)` (line 371), which
  is `INTERVIEW_FOLLOWUP_MODEL` or the session model.
- `main.py:4463` and `main.py:4517`: `generate_followup_with_model(...)` runs inline in `/answer` (adaptive next
  question, and follow-up mode) with **no deadline**.

On Astra these add 3–10 s of silence per answer. A call past the 6 s deadline silently drops the follow-up.

Required:

1. `fast_interview_model(session_model)` resolves in this order:
   1. Settings `ai.interview_fast_model`
   2. env `INTERVIEW_FAST_MODEL`
   3. env `INTERVIEW_FOLLOWUP_MODEL` (the existing name; keep it working)
   4. `"gpt-4o-mini"` when the session model is a reasoning model
   5. otherwise, the session model
2. `conversation._model_for` and both inline follow-up calls use it.
3. The two inline follow-up calls get a hard deadline, using the same pattern as `conversation._chat_json` (line 376):
   - a worker thread with `fut.result(timeout=...)`;
   - the interview log context copied with `contextvars.copy_context()`;
   - env `INTERVIEW_INLINE_AI_TIMEOUT_S`, default 6;
   - on timeout, `generate_followup_fallback` as today.
4. Astra keeps everything that decides quality: question generation, per-answer evaluation, the final report and
   re-score. To put the in-path calls on Astra too (and accept the wait), set `INTERVIEW_FAST_MODEL=gpt-6-astra`.
5. Make the wait measurable. nginx logs in the default `combined` format, which has no request time. So log one
   `interview.answer.timing` line per `/answer` with `elapsed_ms`, plus the time spent in each in-path model call
   (conversation plan, follow-up) and whether a pool top-up started.

### B3. Never call the model while holding the session lock

`_apply_turn_evaluation` (`main.py:4031`) runs in a background thread (`_schedule_turn_evaluation`, `main.py:4019`).
But it makes the model call **inside** `with session_lock(sk):` (`main.py:4034`), and `/answer` holds the same lock
for its whole body (`main.py:4203`). On Astra the evaluation holds the lock for 3–5 s, so a quick next answer (or a
skip) waits behind it.

Required:

1. Read the inputs under the lock.
2. Call `evaluate_turn_with_model` outside the lock.
3. Take the lock again only to write `last_turn_*` and `session_difficulty`. Skip the write if the session has moved
   past that turn or is finalizing.

### B4. Time-mode question top-up off the critical path

`/answer` calls `_expand_time_mode_pool(s)` inline (`main.py:4543`) while holding the lock. That call generates up to
10 questions with the session model whenever 8 or fewer unasked questions remain (`main.py:4092`). On Astra this is a
10–30 s pause, roughly every 8 questions of a timed interview.

`/next` does the same when the pool is empty (`main.py:3509`), and the candidate's `/next` aborts after 30 s
(`frontend/js/interview_engine.js:16`).

Required:

1. When 8 or fewer remain, start a background top-up:
   - one per session at a time (a flag in meta, or a module-level set);
   - a daemon thread, with the interview context copied;
   - generate outside the lock, then append under the lock with the same similarity filter;
   - call `_persist_interview_progress`.
2. Generate synchronously only when the pool is truly empty (`current >= len(questions)`). Do it under the deadline
   `INTERVIEW_POOL_SYNC_TIMEOUT_S` (default 20; it must stay below the client's 30 s). On timeout, use
   `generate_questions_fallback`, so the interview never ends early with time left (the 16 Sep 2026 rule).

### B5. Question prewarm vs. login

Questions are generated by a background prewarm when the candidate opens the link (`_maybe_prewarm_invite_session`,
`main.py:1410`). Login waits for it only 12 s (`_wait_for_invite_prewarm(token, timeout_sec=12.0)`, `main.py:8327`).
If the prewarm is still running, login does a **fast bootstrap** (`main.py:8339`, `fast_only=True`). The candidate
then gets generic, non-AI questions (`generate_questions_fallback`) unless the template has saved preview or manual
questions. Astra generates more slowly, so this happens more often.

Required:

1. Add `INTERVIEW_PREWARM_WAIT_SEC`. Default 45 when `interview_model()` is a reasoning model, else 12.
   The client already waits up to 90 s for login (`frontend/js/app.js:876`) and shows "Starting interview session…".
2. Log every fast bootstrap at WARNING, with the model, the prewarm status and its latency. Also stamp
   `meta["fast_bootstrap"] = True`, so a report can say its questions were generic.
3. Optional — only if the logs still show fast bootstraps: when the prewarm finishes after a fast bootstrap, swap in
   the AI questions for every question not yet asked.

### B6. No model call inside a page load

`GET /hr/dashboard` (`main.py:5116–5117`) and `GET /interview/integrity-logs` (`main.py:8931–8932`) both call
`_recover_interviews_once(limit=50)` and `_cleanup_expired_integrity_rows(...)` inline. Both paths can end in
`_finalize_interview_snapshot`, which is a full report evaluation:

- `_recover_interviews_once` at `main.py:2551`;
- `_auto_finalize_stale_active_row` at `main.py:8692`.

On Astra, one stale interview turns opening the dashboard into a 30–90 s wait.

Required:

1. The page loads only *start* the work and return at once. Add `_kick_recovery_async()`: one background pass at a
   time, under the same `interview_recovery_lock`.
2. The stale-active finalize hands its token to that background pass instead of evaluating inline.
3. The row updates on the next refresh. The 5-minute recovery worker keeps running as it does today.

### B7. OCR never uses the interview model

`_extract_text_from_upload` (`main.py:7592`) passes the interview model to `ai.extract_text_from_image_bytes`
(`ai.py:2898`). That function makes a **direct** `chat.completions.create(..., temperature=0)` call — the only one
left. It bypasses `tracked_chat_completion` so the base64 image is not logged. After B1 it would get Astra, and that
call would fail with HTTP 400.

Required:

1. `ocr_model()` = env `INTERVIEW_OCR_MODEL` → `"gpt-4o-mini"`.
2. Add `prompt_logger.chat_params(model, *, temperature=None, max_tokens=None) -> dict`. It applies the same
   reasoning-model rule as `tracked_chat_completion`; make `tracked_chat_completion` use it too.
3. Use `chat_params` in the OCR call.
4. Add a source-scan test: no `chat.completions.create(` outside `prompt_logger.py` and the OCR function
   (`scripts/` excepted).

### B8. Record and expose which model ran

1. **Labels.** `services/ai_models.MODEL_LABELS` maps an id prefix to a label; the longest matching prefix wins.

   | Prefix | Label |
   |---|---|
   | `gpt-6-astra` | GPT-6 Astra |
   | `gpt-6.1-sol` | GPT-6.1 Sol |
   | `gpt-5` | GPT-5 |
   | `gpt-4.1-mini` | GPT-4.1 mini |
   | `gpt-4.1` | GPT-4.1 |
   | `gpt-4o-mini-tts` | GPT-4o mini TTS |
   | `gpt-4o-mini-transcribe` | GPT-4o mini Transcribe |
   | `gpt-4o-mini` | GPT-4o mini |
   | `gpt-4o` | GPT-4o |
   | `gpt-realtime-mini` | GPT Realtime mini |
   | `gpt-realtime` | GPT Realtime |

   - An unknown id is prettified ("gpt-7-nova" → "GPT-7 Nova") and is never blank.
   - `describe(id)` → `{id, label, provider: "OpenAI", reasoning}`.
2. **The engine.** `engine()` → `{interview, fast, live_voice, transcription, voice, ask_ai, ocr}`, each built with
   `describe()`. Each entry reads the same function or env the code already uses:
   - `interview_model()`;
   - `fast_interview_model(interview_model())`;
   - `realtime_voice.realtime_model()`;
   - `OPENAI_TRANSCRIBE_MODEL`;
   - `OPENAI_TTS_MODEL`;
   - `assist._model()`;
   - `ocr_model()`.

   Add `show_to_candidates` (B9). Return model ids and labels only — never keys, base URLs or env names.
3. **`GET /interview/ai-engine`.** Staff only (`_require_user(request, {"hr"})`; CRM users sign in with the same
   token). It returns raw JSON like the other `/interview/*` routes.
4. **Reports record the model that scored them.**
   - `_evaluate_and_store_report` (`main.py:2096`) stamps `report["evaluation_model"]`.
   - `build_report_record` already stores the session `model` (`hr/service.py:130`).
   - Add top-level `model_label`, `evaluation_model` and `evaluation_model_label` to `_interview_summary_payload`
     (`main.py:5593`) and to the record returned by `hr_candidate_interview_detail` (`main.py:5867`, which the report
     page reads).
   - Records written before this change carry `model` (`gpt-4o-mini`) but no `evaluation_model`. Treat a missing
     `evaluation_model` as equal to `model`.
5. **Profile AI card.** `services/ai_interview_summary.summarize_interview_record` adds `model` and `model_label`.
6. **Candidate login.** The invite login response (`candidate_invite_login`, `main.py:8256`) adds
   `ai_model: {id, label}`, plus `voice_model` when the template runs live voice (`meta["realtime_model"]`). Include
   them only while `show_to_candidates` is on.
7. **AI Costs.** `services/ai_interview_costs.py` adds:
   - `by_model: [{model, label, calls, interviews, cost_usd}]`, from one grouped query over the window's interview
     calls (`GROUP BY model`);
   - `models: [label]` on each interview row.

   Keep the psycopg2 rule: no bare `%`, so no `LIKE 'x%'`; use `SUBSTR` as that module already does.
8. **Optional: reasoning tokens.** Log `usage.completion_tokens_details.reasoning_tokens` on `ai_prompt_logs`, added
   the way `cost_usd` was (`_ADDED_COLUMNS`, both dialects), and show the figure on AI Costs. Cost is already right:
   reasoning tokens are billed inside `completion_tokens`.
9. **Re-price the Astra calls already logged.** The server patch of 8 Oct did not include the pricing rows (Part A's
   `ai_pricing.py` change exists only in the F: tree). So every Astra call logged on production since 8 Oct was priced
   at the `gpt-4o-mini` fallback (`ai_pricing.price_for`), and AI Costs under-reports that spend 65–85×. A priced
   row is never re-priced (`backfill_prompt_log_costs` only fills NULLs).
   - Add a one-off, idempotent re-price: rows with `model = 'gpt-6-astra'` (exact match — no `LIKE`, psycopg2 rule)
     get `cost_usd` recomputed from their stored tokens at the new price, with the same SQL shape as
     `backfill_prompt_log_costs` (`prompt_logger.py:289`).
   - Record completion in `app_settings` (e.g. `ai.reprice_gpt6_done`), the way `ai.cost_repair_done` works.
   - Cached tokens are not stored per row, so these rows are priced at the full input rate — a slight over-statement.
     Say so in the AI Costs footnote.

### B9. Admin switch in Settings (no deploy, no restart)

Add three keys to `services/org_settings.KEYS`:

| Key | Env fallback | Default |
|---|---|---|
| `ai.interview_model` | `INTERVIEW_OPENAI_MODEL` | `gpt-4o-mini` |
| `ai.interview_fast_model` | `INTERVIEW_FAST_MODEL` | `""` |
| `ui.show_ai_model_to_candidates` | `SHOW_AI_MODEL_TO_CANDIDATES` | `true` |

Rules:

- **Validation.** `validation_error` refuses a model outside `ai_models.SUPPORTED_INTERVIEW_MODELS`. Start the list
  with `gpt-6-astra`, `gpt-6.1-sol`, `gpt-4.1`, `gpt-4.1-mini`, `gpt-4o` and `gpt-4o-mini`. A blank value means "fall
  back". Add this as an allow-list check, **not** through `_FORMATS`: `normalize_value` upper-cases every `_FORMATS`
  key, and a model id must stay lower-case.
- **Saving.** Use the existing admin route (`PUT /api/org-settings` in `routers/crm/email_flows.py:988`, Admin / CEO).
  That route records nothing today, so log who changed a model key and from what to what.
- **Timing.** The second app process picks up a change within the 60 s settings cache. A switch only affects
  interviews that start after it.

## Part C — frontend

### C0. One source for model names

- **`src/lib/aiEngine.ts`.**
  - Types: `AiModel {id, label, provider, reasoning}` and the engine shape.
  - `useAiEngine()` fetches `GET /interview/ai-engine` once per page load (a module-level promise through
    `authFetch`, retried after a failure). It returns `null` while loading or on an error, and every caller falls back
    gracefully.
  - The client never carries its own table of names. It only prettifies an id that arrived without a label (records
    written before this change).
- **`src/components/AiModelChip.tsx`.**
  - A small pill: a sparkle icon and the label, e.g. "GPT-6 Astra".
  - Tooltip: "OpenAI GPT-6 Astra · reasoning model".
  - Use design tokens only. Never put an alpha modifier on a `var()` token (see F-V2 `CLAUDE.md`).

### C1. HR setup screen (legacy runtime)

- **Model select.** `frontend/index.html:8834`: the hidden `<select id="model">` keeps its id but loses the
  hard-coded `gpt-4o-mini` option. `frontend/js/hr.js:27–39` fills it from `/models` (the server's model).
  `frontend/js/hr.js:748` sends `model: ""`, so the server decides (B1).
- **Provider label.** Change `#kxAiProviderLabel` (`frontend/index.html:8724`, synced by
  `frontend/js/hrSetupUi.js:757–765`):
  - It now reads "AI interviewer: OpenAI GPT-6 Astra", taken from `/interview/ai-engine` (`interview.label`).
  - If the call fails, it reads "AI interviewer: OpenAI".
  - Remove the hard-coded "GPT-4o optimized".
- **Brand line.** `.kx-brand-powered` (`frontend/index.html:8487`) reads "Powered by OpenAI GPT-6 Astra" once the
  engine loads. The current text stays as the fallback.
- **Status line.** `frontend/js/hr.js:38` ("AI provider: OpenAI.") includes the label.

### C2. Candidate interview screen

- **Model chip.** The "AI Interviewer" chip (`frontend/index.html:8949`) becomes "AI Interviewer · GPT-6 Astra",
  taken from the login response's `ai_model.label` (B8.6). When that is absent (setting off, or an older server), it
  stays "AI Interviewer".
- **Live voice.** Live-voice templates also show a "Live voice · GPT Realtime mini" chip, from `voice_model.label`.
- **Cache-bust.** Bump `js/app.js?v=39` → `?v=40` (`frontend/index.html:9099`).

### C3. Candidate report page

In `src/pages/CandidateReportPage.tsx`, add an `AiModelChip` to the header row that holds `StatusBanner` and
"Interview: …" (lines 1049–1054):

- One chip, "GPT-6 Astra", when `model` equals `evaluation_model`.
- "Questions: GPT-4o mini · Scoring: GPT-6 Astra" when they differ (an older interview re-scored on Astra).
- Older records show their own model ("GPT-4o mini"), which is correct: that is what ran them.

Extend two types in `src/types.ts`:

- `InterviewRecord` (line 54) has no model fields today. Add `model`, `model_label`, `evaluation_model` and
  `evaluation_model_label`.
- `CandidateInterviewSummary` (line 82) has `model` at line 101. Add `model_label`, `evaluation_model` and
  `evaluation_model_label`.

### C4. Profile ▸ AI interview card

In `src/crm/components/AiInterviewOverview.tsx`, add a chip beside the recommendation (line 195), taken from the
summary's `model_label`.

### C5. Schedule AI L1 dialog

In `src/crm/components/ScheduleAiInterviewModal.tsx` (title and subtitle at lines 218–223), add a subtitle line:
"Runs on GPT-6 Astra · questions and scoring", taken from `useAiEngine()`.

### C6. Template authoring previews

`src/pages/TemplateForm.tsx` generates questions at lines 843, 892 and 953. While that runs, show "Generating with
GPT-6 Astra — this can take up to a minute". Reasoning models are slower, and nginx allows 300 s.

### C7. AI Costs

- **By-model panel.** In `src/pages/InterviewCosts.tsx`, add a "By model" panel next to "Where the money goes"
  (line 348), built from `by_model` (label, calls, interviews, ₹ / $).
- **Row chips.** Show the model chip(s) on each interview row.
- **Types.** Extend the types in `src/api/aiCosts.ts`.

### C8. Settings ▸ AI engine

In `src/crm/pages/CrmSettings.tsx`, add a new entry to the Operations group (`SETTINGS_GROUPS`, line 1370):
`{ key: "ai-engine", label: "AI engine", blurb: "Which OpenAI model runs interviews, fast replies, live voice,
transcription and Ask AI." }`. Render it like `OperationsTab` (line 1587).

What it shows:

- **Everyone:** a read-only list from `/interview/ai-engine`: role · model label · a "reasoning" tag.
- **Admin / CEO:**
  - Two selects, `ai.interview_model` and `ai.interview_fast_model`, limited to the supported list.
  - A "Show the AI model to candidates" switch.
  - All saved through the existing settings API.

The text under the selects: "Applies to interviews that start from now on; interviews in progress keep their model.
The other server process picks the change up within a minute."

### C9. Leave alone

- `src/pages/ATS.tsx:89` — the legacy ATS scoring stays on gpt-4o-mini by design.
- Ask AI — it stays on its own model (see Part A).

## Part D — tests

**Backend** (run the full suite from a writable copy of `backend/`):

- **New `tests/test_ai_models.py`:**
  - labels (Astra, Sol, 4o-mini; an unknown id prettified, never blank);
  - `interview_model()` order: setting → env → default;
  - `fast_interview_model()`: a reasoning session gives gpt-4o-mini; an explicit env wins;
    `INTERVIEW_FOLLOWUP_MODEL` still works;
  - `org_settings.validation_error` refuses an unsupported model.
- **Resolver:** a stored model is ignored without `model_locked`; `/setup` starts a session on `interview_model()`.
- **B3:** while the patched `evaluate_turn_with_model` runs, another thread can take `session_lock(sk)`.
- **B4:** `/answer` returns before a slow patched generator finishes (`threading.Event`), and the questions are
  appended afterwards.
- **B6:** `GET /hr/dashboard` does not call `_finalize_interview_snapshot` in the request thread.
- **B7:**
  - OCR uses `ocr_model()`, and `chat_params` drops `temperature` for a reasoning model.
  - The source scan finds no direct `chat.completions.create(`.
- **Payloads:**
  - `/interview/ai-engine` has the right shape and nothing secret (scan the JSON for `sk-` and env names).
  - The login carries `ai_model` only while the setting is on.
  - The AI Costs report has `by_model`.
  - The Astra re-price (B8.9) runs once, and a second run changes nothing.

**Frontend:**

- `src/lib/aiEngine.test.ts`: label fallback, and one fetch per page load.
- `AiModelChip.test.tsx`.
- Report page chip: same models, different models, no model.
- Settings ▸ AI engine: editable for Admin / CEO, read-only for everyone else.
- `npm run typecheck`, `npm run lint` and `npm run test:run` are green.

## Part E — acceptance (the operator checks these on production after deploy)

1. **Every interview call type runs on Astra.**

   ```sql
   SELECT call_type, model, status, count(*), round(avg(response_time_ms)) AS avg_ms
   FROM ai_prompt_logs
   WHERE created_at >= now() - interval '1 day'
   GROUP BY 1, 2, 3 ORDER BY 1;
   ```

   - These are on `gpt-6-astra` with `status = success`: question generation (`generate_questions_*`),
     per-answer / per-question evaluation, skill and communication evaluation, strengths / weaknesses.
   - These are on the fast model: `conversation_*` and the follow-ups.
2. **No rejected-parameter errors.** `journalctl -u karnex -u karnex-2 --since today | grep -c "Unsupported value"`
   returns 0.
3. **No generic questions.** No `interview.invite.login.fast_bootstrap` WARNING for an AI-generated template.
4. **Short waits between answers.** Over a 15-minute timed test interview, the `interview.answer.timing` lines (B2.5)
   show `/answer` at p50 < 2.5 s and p95 < 5 s. No 10–30 s gap appears when the pool tops up.
5. **Fast staff pages.** `/hr/dashboard` and `/interview/integrity-logs` load in under 2 s while a stale interview
   exists.
6. **The UI shows "GPT-6 Astra":**
   - the HR setup label and brand line;
   - the candidate chip;
   - the report page chip;
   - the profile AI card;
   - the Schedule AI L1 dialog;
   - AI Costs ▸ By model;
   - Settings ▸ AI engine.
7. **The switch works without a restart.**
   1. Switch Settings ▸ AI engine to GPT-4o mini.
   2. Within 60 s, the next interview's `meta.model` is `gpt-4o-mini` and every label follows.
   3. Switch back.

## Part F — constraints

- Follow `CLAUDE.md`:
  - legacy `main.py` routes return raw JSON; CRM routes go through `envelope()`;
  - use savepoints for best-effort DB work;
  - change both dialects in `auth_db.py`;
  - never re-import a module-level name inside a function (`tests/test_no_shadowed_imports.py`).
- **No Alembic migration is needed.** Settings live in `app_settings`, and the model is already on every record. Only
  the optional reasoning-tokens column (B8.8) is an ALTER on the legacy prompt-log table.
- `tracked_chat_completion` stays the only path for chat calls. The one exception is OCR, which uses `chat_params`.
- Never return or log API keys, base URLs or env-var values from the new endpoint.
- Do not commit frontend build output (`frontend/admin-dashboard/dist/` is untracked).
- Commit the Part A files together with this work. Add a dated note to `CLAUDE.md` in both repos.

## Part G — deploy notes (for the operator)

1. **Pull cleanly.** Back up with `sudo karnex-backup`. The server carries the base patch
   (`/srv/karnex/patches/2026-10-08-gpt6-astra-interviews.patch`) as uncommitted edits, so before `git pull`:
   - restore the three patched files with `git checkout -- backend/prompt_logger.py backend/ai_help/assist.py
     backend/main.py`;
   - remove the untracked `backend/tests/test_reasoning_model_params.py`.

   Then pull both repos. From now on, nothing needs re-applying after a pull.
2. **Deploy.** No migration. Build the frontend, then run `sudo karnex-rolling-restart` and restart nginx.
3. **Environment** (`/etc/karnex/karnex.env`):
   - Keep `INTERVIEW_OPENAI_MODEL=gpt-6-astra`.
   - **Delete `CRM_AI_L1_MODEL`** (B1: one switch).
   - Optional: `INTERVIEW_FAST_MODEL`, `INTERVIEW_PREWARM_WAIT_SEC`, `INTERVIEW_INLINE_AI_TIMEOUT_S`,
     `INTERVIEW_POOL_SYNC_TIMEOUT_S`, `INTERVIEW_OCR_MODEL`. The defaults are right.
4. **Rollback.**
   - Fastest: Settings ▸ AI engine → GPT-4o mini (no restart).
   - Or: env plus `sudo karnex-rolling-restart`. The code is correct for either model.
5. **Owner, on platform.openai.com:**
   - Set a monthly budget / usage alert. Astra costs 65–85× gpt-4o-mini per token.
   - Check the `gpt-6-astra` rate limits for the organisation's tier.
