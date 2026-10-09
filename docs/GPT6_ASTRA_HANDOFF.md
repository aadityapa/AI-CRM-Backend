# AI interviews on GPT-6 Astra — developer hand-off

**Date:** 8 Oct 2026 · **Status:** live on karnexgroup.com since 8 Oct, running as a LOCAL PATCH on the server.
**Already applied to this working tree** (`F:\AI-Interview-Model-B-V2`, on top of `4d78b99` + your uncommitted work).
**Please review and commit it** so the server can go back to a clean `git pull`.

---

## 1. What was asked

The business asked for the AI L1 interviews to run on **`gpt-6-astra`** (OpenAI's top model; there is no "GPT-5 Astra").
Production now has, in `/etc/karnex/karnex.env`:

```
INTERVIEW_OPENAI_MODEL=gpt-6-astra
CRM_AI_L1_MODEL=gpt-6-astra
```

Changing the env alone was **not** enough. Live calls against the OpenAI API showed three problems first.

## 2. What broke, and the fix for each

### 2.1 Astra refuses `temperature` and `max_tokens` → every interview call would 400

```
400 Unsupported value: 'temperature' does not support 0.45 with this model.
    Only the default (1) value is supported.
```

Astra is a **reasoning model** (like the GPT-5 family and the o-series). The interview code sends
`temperature` on every question and every evaluation (0.45, 0.72, 0…), and some calls send `max_tokens`.

**Fix — `backend/prompt_logger.py`:**
- New `is_reasoning_model(model)` returns True for `gpt-5*`, `gpt-6*`, `o1*`, `o3*` and `o4*`.
- `tracked_chat_completion` no longer sends `temperature` or `max_tokens` to such a model. Callers are unchanged.
- `max_tokens` is **dropped, not translated** to `max_completion_tokens`. Hidden reasoning tokens count against that
  cap, so a small one (300) returns an **empty** answer.
- Optional env `OPENAI_REASONING_EFFORT` (low · medium · high · xhigh) is sent as `reasoning_effort`. It is unset in
  production; "low" was not meaningfully faster. `none` / `minimal` are rejected by Astra.

> ⚠️ **Rule going forward:** every OpenAI chat call must go through `tracked_chat_completion`.
> A direct `client.chat.completions.create(..., temperature=…)` breaks the day the env names a reasoning model.
> Today the only direct call left is `ai.extract_text_from_image_bytes` (fixed default `gpt-4o-mini`, so it is safe).

### 2.2 Ask AI cannot use Astra at all on Chat Completions

```
400 Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions.
    To use function tools, use /v1/responses or set reasoning_effort to 'none'.
400 Unsupported value: 'reasoning_effort' does not support 'none' with this model.
```

Ask AI uses function tools, and `ai_help/assist._model()` inherited `INTERVIEW_OPENAI_MODEL`.

**Fix — `backend/ai_help/assist.py::_model()`:**
- `AI_ASSIST_MODEL` wins when it is set.
- Otherwise Ask AI uses the interview model, **unless that is a reasoning model**, in which case it falls back to
  `gpt-4o-mini`.

Ask AI stays on gpt-4o-mini in production. Moving Ask AI to Astra would need the **Responses API**
(`/v1/responses`) — a bigger change, not done.

### 2.3 Already-scheduled (and HR-scheduled) interviews ignored the env

Every schedule stores `"model": "gpt-4o-mini"` inside its notes:

- `main._pack_invite_config_into_notes` defaults it.
- The legacy HR form sends it from a hidden `<select id="model">` (`frontend/index.html:8834`).

At interview time `main.py` preferred that stored value over the env, so most candidates would have stayed on
gpt-4o-mini.

**Fix — `backend/main.py::_resolve_interview_model(stored)`:**
- A stored `"gpt-4o-mini"` (the hard-wired default) or a blank means "use the server's model", i.e.
  `INTERVIEW_OPENAI_MODEL`.
- Any **other** stored model was a deliberate choice and is kept.

Used where the session picks its model (`selected_model`).

### 2.4 AI Costs would have under-reported Astra about 65×

`services/ai_pricing.py` had no `gpt-6-astra` row. An unlisted model is priced as `gpt-4o-mini`, so the
**AI Costs** page would have read about 65× low.

**Fix:** added rows for both models.

| Model | Input / 1M | Cached input / 1M | Output / 1M |
|---|---|---|---|
| `gpt-6-astra` | $10.00 | $1.00 | $50.00 |
| `gpt-6.1-sol` | $2.00 | $0.10 | $10.00 |

Rows already logged keep the price they were logged with. `ai_pricing` prices at log time.

## 3. Files changed

| File | Change |
|---|---|
| `backend/prompt_logger.py` | `is_reasoning_model()`; reasoning-model parameter handling in `tracked_chat_completion` |
| `backend/ai_help/assist.py` | `_model()` — `AI_ASSIST_MODEL`, never inherits a reasoning model |
| `backend/main.py` | `_resolve_interview_model()`; `selected_model` uses it |
| `backend/services/ai_pricing.py` | `gpt-6-astra`, `gpt-6.1-sol` prices |
| `backend/tests/test_reasoning_model_params.py` | **new**, 8 tests (detection, dropped params, effort env, tools untouched on classic models, Ask AI model choice, interview model resolution, pricing) |
| `CLAUDE.md` | dated note (8 Oct 2026) |

**Test result on this tree:** 1,926 passed, 2 skipped (the full suite + the new file).

## 4. Things to check / decide (not changed)

1. **Adaptive follow-up timing** (`services/interview/conversation.py`, your uncommitted work). It runs on the
   session's model under hard deadlines `TURN_PLAN_TIMEOUT_S = 6` and `REPLY_TIMEOUT_S = 8`.
   - Astra measured 3–4.5 s on short prompts, so it fits, but with little margin.
   - A timeout skips the follow-up silently (logged `conversation.* timed out`).
   - If follow-ups go missing, either raise the deadlines or pin `INTERVIEW_FOLLOWUP_MODEL=gpt-4o-mini`.
2. **Latency the candidate feels.** Each question / evaluation call is ~3–4.5 s on Astra vs ~1–2 s before. Worth one
   real end-to-end interview to judge the pause between answer and next question.
3. **Cost.** About $0.006–0.007 per question or evaluation call, vs ~$0.0001 on gpt-4o-mini. Expect roughly
   **$0.05–0.15 per interview**, and watch AI Costs for the first week.
4. **Cosmetic:** `frontend/js/hrSetupUi.js:761` still prints "AI provider: OpenAI gpt-4o-mini optimized" on the HR
   setup screen. It reads the hidden select, which is now only a placeholder. Show the server's model, or remove the
   line.
5. **Responses API.** Moving every call to `/v1/responses` would let Ask AI use Astra too, and is OpenAI's
   recommended path for reasoning models. That is a separate piece of work.

## 5. Deploy / operations notes

- **No migration, no dependency change.** Restart only.
- **⚠️ Never run `INTERVIEW_OPENAI_MODEL=gpt-6-astra` on code WITHOUT this change.** Every AI interview call would
  fail with HTTP 400. Until it is committed, the server operator re-applies
  `/srv/karnex/patches/2026-10-08-gpt6-astra-interviews.patch` after each pull.
- **Rollback in a minute:** set the two env lines back to `gpt-4o-mini` (or delete them), then run
  `sudo karnex-rolling-restart`. The code is correct for either model.
- Optional env added:

  | Variable | Values / effect |
  |---|---|
  | `OPENAI_REASONING_EFFORT` | low · medium · high · xhigh |
  | `AI_ASSIST_MODEL` | Ask AI's own model |
  | `INTERVIEW_FOLLOWUP_MODEL` | already existed; pins the follow-up model |

## 6. References

- [GPT-6 Astra — model page](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [GPT-6.1 Sol — model page](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Azure OpenAI reasoning models: parameter limits](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning)
