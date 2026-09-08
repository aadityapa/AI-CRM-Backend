# Interview fixes — batch 1

Status bug, interview speed, template limits, keyboard lockdown, rules screen.
The candidate-screen redesign is batch 2 and has not started.

---

## 1. "Selected" on the report, "Failed" in the CRM

### What was wrong

Not a display bug — a **missing hop between two databases**.

| | Report page | CRM Candidate Profile |
|---|---|---|
| Database | legacy auth DB | CRM Postgres |
| Table | `hr_candidate_decisions`, `interview_records.payload` | `ai_interview_links` |
| Field | `hr_decision`, `hr_interview_status` | `result` |
| Written by | recruiter pressing Shortlist | score vs pass threshold, once at completion |

`ai_interview_links.result` is written exactly once when the interview finishes:
57.2% against a 60% threshold → `Failed`. Nothing ever wrote to it again. The
CRM had never seen the Shortlist click, so it kept reporting the AI's verdict.

### What changed

Migration **0062** adds `hr_decision`, `hr_decision_by`, `hr_decision_at` to
`ai_interview_links`. Both decision endpoints now mirror into the CRM:

- `PATCH /hr/interviews/{id}/status` (per-interview: Selected / On Hold / Rejected / Pending Review)
- `PUT /hr/candidates/{id}/hr-decision` (candidate-level: Shortlist / On Hold / Reject)

`services/ai_interview_bridge.sync_hr_decision()` resolves the link by
invite token → interview record id → the candidate's newest completed interview,
and is idempotent so repeated clicks do not spam the activity log. It never
raises: a CRM outage must not break the report page.

**`result` is deliberately not overwritten.** The AI verdict and the human
verdict are both facts. A candidate selected at 57.2% should still visibly be a
57.2% candidate — overwriting would erase the evidence that someone overrode the
score, which is exactly the thing an audit needs to see.

The profile now reads:

```
Selected   (AI: Failed 57.2%)
```

with who overrode it and when in the tooltip. The header badge follows the human
decision, because that is the at-a-glance status people act on.

---

## 2. Interview speed

Three separate causes, all now fixed.

### Time to first question

Login blocked up to **12 seconds** waiting for the entire question batch. The
warmup line ("Please introduce yourself.") is a **fixed string**, so it is now
synthesised the moment the invite link is opened — long before the candidate
finishes the rules and device-test screens. The first real question is warmed as
soon as generation completes, still during that dead time.

The prewarm poll also backed off from a flat 200 ms to 20 ms → 250 ms, so a
session that becomes ready just after a tick no longer costs up to 200 ms of
pure sleep on the login path.

### Every question's audio was fully buffered twice

`synthesize_speech_bytes` used `with_streaming_response` but then called
`.read()` — waiting for the whole MP3 — and the client did `await res.blob()`,
waiting again. Nothing played until the last byte of a whole spoken question had
been generated, transferred to the server, and transferred to the browser.

Now `stream_speech_bytes()` yields 8 KB chunks and `/candidate/tts` returns a
`StreamingResponse`; the client feeds them into a `MediaSource` and starts
playback on the **first** chunk. Falls back to the blob path wherever streaming
is unavailable, so behaviour is unchanged on older browsers.

### No prefetch at all

Questions are generated in one batch up front, so the text of question N+1 is
already known while the candidate answers question N — but nothing used that.

`services/tts_prewarm.py` now holds pre-synthesised audio (LRU-bounded at 24
entries, 15-minute TTL, max 4 concurrent warms so a burst cannot exhaust the
rate limit the live request depends on). `next_question_payload()` kicks off the
following question's synthesis as it serves the current one. `/candidate/tts`
then answers from memory with **no OpenAI call on the critical path**, and says
so in the `X-Karnex-TTS: cache` header.

Adaptive follow-ups invalidate correctly: `_replace_question_slot()` drops the
audio for the question it replaced and warms the replacement, so the candidate
can never hear a question that is no longer in the script.

### Submit

Two very different situations shared one 8-second ceiling. When a transcript
already exists the wait only catches trailing words, so that is now **2.5 s**.
When nothing has been captured the wait *is* the answer, so it stays at 8 s —
cutting it short would submit an empty response and waste the question. Both are
ceilings, not delays.

---

## 3. Template time and question limits

Each mode previously enforced only half its contract:

| | Question cap | Clock |
|---|---|---|
| count mode | server ✓ | none |
| time mode | **none** | browser only ✗ |

A time-mode interview had no question ceiling at all, and a count-mode one had
no time limit. The browser timer is also not a limit: it can be throttled in a
background tab or reset by reloading the page.

Now both are read whenever the template sets them, in either mode, and enforced
in `next_question_payload()` on the server. Whichever arrives first ends the
interview, and `completion_reason` records which one did
(`question_limit` / `time_limit` / `questions_exhausted`).

The clock starts when the **first question is served**, not at login — time
spent reading the rules or running the device test is not interview time — and
`mark_interview_started()` is idempotent so a reload cannot buy extra minutes.
Each question payload carries a server-authoritative `time_remaining_sec` and
`questions_remaining` for the client to count down from.

Pinned by `tests/test_interview_template_limits.py` (12 tests).

---

## 4. Keyboard lockdown

Answers are voice-only — there is no answer field to type into — so keys are now
**swallowed**, not merely logged. `keydown` and `keypress` are intercepted in the
capture phase, along with `copy`, `cut`, `paste` and `contextmenu`.

Two deliberate carve-outs:

1. **A template can enable a typed transcript** (`enable_transcript_input`).
   When focus is in that field, plain typing works or the interview becomes
   impossible to complete. Ctrl/Cmd/Alt combinations stay blocked even there —
   those are copy, paste, find, new tab, print, devtools.
2. **Tab still moves focus.** Removing it would strand anyone using a screen
   reader or who cannot use a mouse, and moving focus between two on-screen
   buttons is not an integrity risk.

A swallowed key with no explanation reads as a broken page, so the candidate
gets a toast — throttled to once every 4 seconds, because someone resting a hand
on the keyboard should not trigger a strobing message or a hundred log events.

---

## 5. Pre-interview rules screen

The old welcome card gave *advice* ("sit somewhere quiet"). The new screen states
the rules that are **actually enforced in code**, and requires an explicit
acknowledgement before Continue unlocks.

It sits **before** the device test, so the candidate knows what they are
consenting to before granting camera and microphone access.

The two rules that can end the interview — leaving fullscreen/switching tabs,
and the disabled keyboard — are visually separated with a red edge, so someone
skimming still catches the ones that can cost them the session. Duration and
question count are injected from the template at runtime, so the candidate sees
the real numbers rather than a generic promise.

This ordering matters: enforcing auto-termination for tab switching against a
candidate who was never told is not a security feature, it is a trap.

---

## What you need to do

```bash
cd backend
python -m alembic upgrade head        # applies 0062
# restart the backend — the TTS cache and streaming endpoint are new
```

Then, to verify:

1. Open a completed interview report, press **Shortlist**. The CRM Candidate
   Profile should immediately read `Selected (AI: Failed 57.2%)`.
2. Start a test interview and watch how long the first question takes to speak,
   and how long submit → next question takes. Both should be markedly shorter.
3. Set a template to a short limit (say 2 minutes / 3 questions) and confirm it
   auto-submits on whichever hits first.
4. Try typing during the interview — nothing should happen, and a toast should
   explain why.

## Files changed

| File | Change |
|---|---|
| `alembic/versions/0062_ai_link_hr_decision.py` | new — override columns + partial index |
| `models/ai_links.py` | override columns, `effective_result`, decision normaliser |
| `services/ai_interview_bridge.py` | `sync_hr_decision()`, `_find_link()` |
| `routers/crm/ai_interviews.py` | expose override in the API |
| `main.py` | mirror decisions to CRM, streaming TTS, prewarm endpoint, slot replacement |
| `ai.py` | `stream_speech_bytes()` |
| `services/tts_prewarm.py` | new — bounded audio cache |
| `candidate/service.py` | server-side time/question limits, prefetch trigger |
| `crm/pages/Profiles.tsx` | `AiResultBadges`, header badge follows the override |
| `js/candidate.js` | progressive playback, prefetch helpers, submit waits |
| `js/interview_security.js` | keyboard/clipboard lockdown |
| `js/interview_rules.js` | new — rules gate |
| `js/app.js` | rules gate in the flow, keyboard toast |
| `index.html` | rules screen markup + styles |
| `tests/test_interview_template_limits.py` | new — 12 tests |
