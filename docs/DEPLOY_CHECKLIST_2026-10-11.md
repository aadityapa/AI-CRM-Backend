# Deploy checklist — 11 Oct 2026 (final interview test, then go-live)

Covers both repos: **AI-Interview-Model-B-V2** (backend) and **AI-Interview-Model-F-V2** (dashboard + candidate page).

## 1. What changed in this release

| Area | Change |
| --- | --- |
| Interview stability | `/next` and `/submit` now wait for each other per session (no clash on a double click or a second tab). A second Submit gets the report already made. |
| Candidate errors | A gateway error page (nginx 502/504, proxy timeout) shows a plain sentence ("The interview server is restarting or busy… your answers so far are saved") instead of `Unexpected token '<'`. Network failures say so too. |
| Template questions | "Where the questions come from" is now two choices: **We write the questions** (question + expected answer; the AI asks, listens and scores against your answer; pseudo-code / coding optional) and **AI writes the questions**. Question Bank and the fixed list only show on old templates that still use them. Every question you write is asked; no skill picker unless you turn it on under *More options*. "Paste many" takes `Q:` / `A:` blocks. |
| Recruiter joins a live interview | Integrity ▸ open a live row ▸ **Join this interview**: pause the AI, send a note to the candidate's screen, queue your own question (asked next by the AI voice, scored), and **call the candidate** (live audio/video, browser to browser). While paused the interview clock stops and integrity warnings are not counted. A pause ends by itself after 20 min. Everything the recruiter did is on the Integrity timeline and in the report (`live_intervention`). |
| Reports tab | Rebuilt as one list of every AI interview: score ring, AI verdict (Recommended / Consider / Not recommended / Not attempted / Terminated), your decision, filters (verdict, customer, position, date), sort, grouping, search, one-click Open. |
| Screen recording | The candidate must share the **Entire screen** (a tab or window is refused with instructions). |
| Hardening | Candidate page tears down face monitoring + security watchers on every finish path; `job_title` escaped; HR "result" screen fixed. |

## 2. Deploy steps

```bash
# Backend
cd AI-Interview-Model-B-V2
git pull
pip install -r requirements.txt          # imageio-ffmpeg (combined recording), boto3
cd backend && python -m alembic upgrade head   # head = 0132 (no new migration in this release)
# restart BOTH app processes (the startup ALTER adds interview_schedule.live_control / live_reply)

# Frontend
cd AI-Interview-Model-F-V2/frontend/admin-dashboard
npm ci && npm run build
```

The candidate page is cache-busted (`app.js?v=48`) — no extra step.

### Added 12 Oct 2026 (after the first test interview)

| Area | Change |
| --- | --- |
| Template questions | Step 1 sets **how many spoken, pseudo-code and coding** questions each interview asks; spoken questions are added in bulk (one per line), no expected answer needed. A longer list gives each candidate a different draw. |
| Ends on time / count | **Count mode has no clock**; **time mode stops at the limit** — the server now also tells the candidate page "time up" through the live poll. Typed questions never run past the interview clock. |
| Rules page | Shows the real question count and mix, how it ends, and today's rules (entire screen recorded, keyboard only in the code editor, recruiter may join). |
| Live join | Candidate page checks every second while a recruiter has the panel open; **Join this interview now** pauses and calls in one click. |

## 3. Server settings (env)

| Variable | Value | Why |
| --- | --- | --- |
| `UVICORN_WORKERS` | `1` per process | Interview sessions live in process memory. |
| nginx | **sticky routing for interview traffic** (e.g. `ip_hash` or route `/candidate`, `/next`, `/answer`, `/submit`, `/interview/violation` to one process) | With two processes behind nginx and no sticky routing, `/next` can land on the process that does not hold the session. **Confirm before the test.** Live-join state is in the database, so it works from either process. |
| `INTERVIEW_SCREEN_RECORDING_ENABLED` | unset or `true` | One file with camera + screen on the report page. |
| `INTERVIEW_LIVE_JOIN_ENABLED` | unset or `true` | Recruiter join (set `false` to hide it). |
| `INTERVIEW_LIVE_MAX_PAUSE_SEC` | unset (1200) | Longest pause before the interview resumes by itself (60–3600). |
| `INTERVIEW_TURN_URLS`, `INTERVIEW_TURN_USERNAME`, `INTERVIEW_TURN_CREDENTIAL` | optional | **The live call needs a TURN relay when either side is behind a strict office firewall.** Without it, calls on most home / mobile-hotspot networks still connect through Google STUN. Self-host coturn or use a TURN service, e.g. `turn:turn.example.com:3478,turns:turn.example.com:443`. |
| `INTERVIEW_STUN_URLS` | optional | Override the STUN list (`none` to disable). |
| `INTERVIEW_ALLOW_MOBILE` | **unset** | Interviews stay laptop / PC only. |
| `CORS_ALLOW_ORIGINS` | your dashboard origins | Never leave unset in production. |
| `CRM_AI_L1_MODEL` | **remove** | The model is chosen in Settings ▸ AI engine. |

HTTPS is required for camera, microphone, screen share and the live call.

## 4. Final interview test — script

| # | Do this | Expect |
| --- | --- | --- |
| 1 | Open the invite on a phone | "Laptop or PC required" page |
| 2 | Open on a laptop; wrong key, then right key | "N attempts left", then proceeds |
| 3 | Device check: mic (one / two / clap), speaker, camera, **Share screen → pick a tab** | Refused: "You shared this tab… choose Entire screen" |
| 4 | Share screen → **Entire screen** | Tile green |
| 5 | Warm-up, then 3–4 spoken answers; double-click Send once | No repeated or lost question |
| 6 | TA: Integrity ▸ live row ▸ **Watch live** | Camera + screen, ~15–30 s behind |
| 7 | TA: **Join this interview** ▸ **Pause** | Candidate sees the "paused" card within ~3 s; timer stops |
| 8 | TA: send a note | Note appears on the candidate's card |
| 9 | TA: queue a question ▸ **Resume** | AI asks the TA's question next, then carries on |
| 10 | TA: **Call the candidate** (allow camera/mic) | AI pauses; both see/hear each other; candidate's card says "on a call" |
| 11 | During the pause, candidate switches tab | No warning counted (timeline shows nothing as a strike) |
| 12 | TA: **End call** ▸ **Resume** | Interview continues; clock resumes from where it stopped |
| 13 | Finish | Thank-you page; Reports tab shows the row (Recruiter joined chip) |
| 14 | Open the report | Questions, scores, the recruiter's question; **one recording with camera + screen** (the TA's voice is in it) |
| 15 | Templates ▸ new template ▸ **We write the questions** ▸ paste 8 spoken questions (one per line) ▸ Ask 5 spoken + 1 coding ▸ Question count ▸ save ▸ schedule an AI L1 | Rules page says **6 questions · no time limit**; candidate gets 5 of the 8 + the coding one (+ warm-up) |
| 16 | Same template set to **Time limit 5 min** ▸ new AI L1 ▸ answer slowly | Rules page says 5 minutes; interview submits itself at 5:00 even with questions left |
| 17 | TA: Integrity ▸ live row ▸ **Join this interview now** | Candidate's page pauses and rings within ~1–2 s |

## 5. Known limits (not blocking)

- The live call is browser to browser. Behind a corporate firewall without TURN it shows "could not connect on this network" — pause, note and queued questions still work.
- The live recording view runs ~15–30 s behind; the call is real time.
- Older open items (invite-login hardening A1–A3, `/proctor/*` ownership, auth failures as HTTP 200, CORS fallback) are listed in `CLAUDE.md` §10 and do not affect a controlled interview test.
