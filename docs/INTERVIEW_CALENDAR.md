# Interview Calendar

A week view over every scheduled interview, for **Admin, CEO, TA and RMG**.
Modelled on the Outlook/Teams calendar, including the Sunday-first columns.

---

## Why this needed a new endpoint

The platform books interviews in two unrelated places:

| | Table | Where the time lives |
|---|---|---|
| AI L1 sessions | `ai_interview_links` | **not on this table** — on the legacy `interview_schedule` row, joined by `invite_token`, as a free-form local string like `"2026-08-07 12:30"` |
| Panel rounds (L2 face-to-face, customer) | `interview_events` | a real tz-aware `scheduled_at` column |

Neither knows about the other. The only cross-candidate endpoint that existed,
`/api/dashboard/interviews`, covered **panel rounds only** and was capped at 100
rows — so nothing in the product could answer "what interviews are happening on
Friday".

`GET /api/calendar/interviews` merges both into one event shape.

```
?start=2026-08-02&end=2026-08-09      window (local dates)
&sources=ai_l1,manual_round           filter by type
&mine=true                            only interviews you scheduled
```

---

## Two things that had to be got right

**Times are local wall time, everywhere.** The legacy AI schedule string has no
timezone at all, and a recruiter and candidate agree on a *clock time*, not an
instant. Treating that string as UTC would shift every AI interview by the
server's offset — 5½ hours in India, i.e. onto a different part of the day, and
across midnight, a different day. So the backend parses it as local and returns
it naive, tz-aware panel rounds are normalised down to local, and the frontend
parses ISO strings by component rather than through `new Date(string)`, whose
timezone behaviour varies between engines.

**Overlapping interviews must not hide each other.** Your Friday screenshot has
four things at 11 AM–1 PM. Drawn naively they stack and only the last is
visible. `layoutDayEvents` groups mutually-overlapping events into clusters and
splits each cluster into columns — the same approach Outlook and Google
Calendar use.

Both are covered by tests, and the week maths caught a real bug during
development: my first `startOfWeek` was Monday-first, which put Sunday in the
*previous* week. Your Outlook is Sunday-first ("August 2–8, 2026" with Sunday in
column 1), so the grid now matches it exactly — verified by asserting the header
label renders as that exact string.

---

## What's on the page

- **Week grid**, Sunday-first, 8 AM–9 PM visible with everything else
  scrollable, hour lines, a live red "now" line on today's column.
- **Colour coding** — AI sessions in brand blue, panel rounds in violet, with
  counts in the filter pills.
- **Filters** — AI / Panel / Mine. Turning both type filters off is prevented,
  because an empty grid reads as "no interviews" rather than "no filters".
- **Click an interview** → a meeting card with the candidate (linked to their
  profile), opportunity and customer, panel, organiser, status and result,
  meeting link with **Join**, CV, full report, and Reschedule.
- **Click an empty slot** → pick a candidate application, then the normal
  scheduling modal opens with that date and time already filled in.
- **"Not yet on the calendar"** — rounds recorded with a written time but no
  exact date. They cannot be placed on the grid, and silently dropping them
  would hide real work; the old dashboard widget listed them as "Date TBC".

A session the candidate has already opened shows *why* it cannot be moved rather
than just hiding the button.

---

## Dashboard changes

The **Upcoming interviews (next 30 days)** card is **removed** from the
Dashboard for everyone — it is superseded by this tab.

**Sales and Sales_Head lose it entirely.** They were dropped from the backend
role gate on `/api/dashboard/interviews` as well, not just from the UI:
removing a widget while leaving its endpoint open to those roles would keep the
data reachable by URL.

| Role | Before | After |
|---|---|---|
| TA, RMG | Dashboard card | Interview Calendar tab |
| Admin, CEO | Dashboard card | Interview Calendar tab |
| Sales, Sales_Head | Dashboard card | **nothing** |

---

## Groundwork for Teams / Outlook

You mentioned integrating Teams and Outlook next. Three things here are already
shaped for it:

- Every event carries `meeting_link`, so a Teams join URL renders as **Join**
  today — the manual-round form already accepts one.
- The event shape is a superset of what a calendar-sync payload needs
  (start, end, title, attendees, organiser, link, external id).
- `_legacy_schedules()` is the single place AI interview times are read, so an
  Outlook-sourced time would have one place to land.

The missing piece is OAuth and a `provider` / `external_event_id` column pair to
make sync idempotent. Worth doing as its own piece of work.

---

## Files

| File | Change |
|---|---|
| `backend/services/interview_calendar.py` | new — merges both sources, local-time parsing, organiser batching |
| `backend/routers/crm/calendar.py` | new — `GET /api/calendar/interviews`, TA/RMG (+Admin/CEO) |
| `backend/auth_db.py` | `get_schedules_by_tokens()` — bulk legacy read, avoids one query per interview |
| `backend/routers/crm/dashboards.py` | Sales/Sales_Head removed from the interviews gate |
| `crm/lib/calendarDates.ts` | new — week maths and overlap layout (no date library in this project) |
| `crm/pages/Calendar.tsx` | new — grid, detail card, schedule-into-slot |
| `crm/components/ScheduleAiInterviewModal.tsx` | new — lifted out of Profiles.tsx so the calendar can reuse it |
| `crm/pages/Profiles.tsx` | imports the shared modal; 177 duplicated lines removed |
| `crm/pages/CrmDashboard.tsx` | widget removed |
| `crm/nav.ts`, `crm/routes.ts`, `lib/rbac.ts` | tab registered for Admin/CEO/TA/RMG |
| `tests/test_interview_calendar.py` | new — 14 tests |

## Verify

```bash
cd backend && python -m pytest tests/test_interview_calendar.py -q   # 14 passed
```

No migration. Restart the backend so the new router registers.
