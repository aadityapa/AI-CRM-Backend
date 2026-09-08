# TA → RMG → Sales → Customer — what I found and fixed

You asked me to check whether the handoff actually works. **The backbone was
sound; four things around it were not.**

---

## What already worked

The pipeline is genuinely guarded, not free-form status editing:

- **Transitions are validated server-side** (`TRANSITION_MAP`). RMG cannot push
  a candidate straight to the customer — `RMG_Review → Customer_Screening` is
  rejected. Stages cannot be skipped.
- **Each stage has an owner** (`STAGE_AUTHORITY`): TA owns Sourcing and
  Technical Screening, RMG owns RMG Review, Sales owns Sales Screening and
  Customer Screening. Anyone else gets a 403.
- **Every transition needs a comment** of at least 5 characters, and writes an
  activity-log entry.
- **A passed AI L1 auto-advances** `Technical_Screening → RMG_Review` and
  notifies RMG with the score and a link.
- **An L2 round deliberately has no status of its own.** It is an interview
  event; the profile stays in RMG Review while it happens, because RMG has not
  decided yet. That is correct — a status per round would multiply the pipeline
  without adding information.

---

## What was broken

### 1. Sales was never told

`notify_role(db, "Sales", ...)` **did not exist anywhere in the backend.** When
RMG pressed "Submit to Sales team", the status changed and nothing else
happened. Sales discovered new candidates by browsing the list — which is how a
handoff quietly stalls.

Fixed: arriving at a stage now notifies whoever owns it. A test asserts the
notified role is one that can actually act on that stage, so a notification can
never go to someone who is powerless to respond.

| Arrives at | Notified |
|---|---|
| RMG Review | RMG |
| Sales Screening | Sales |
| Customer Screening / Interview / Approval | Sales |
| Preboarding | HR |

The person who made the change is excluded from their own notification.

### 2. Notifications went nowhere

The backend has always set a deep link (`/admin?view=crm&p=profiles/42`). The
bell rendered the title and message and **never read `link`** — clicking marked
it read and did nothing. A notification saying "RMG Review: Bibin P S" left you
to go and find Bibin by hand.

Now clicking opens the candidate, and rows with a destination show "Open →".
Only same-origin CRM links are followed; an absolute or protocol-relative URL is
ignored, so a stored string can never become an open redirect.

### 3. Sales saw everyone's work in progress

`GET /api/candidate-profiles` applied **no role-based row filter at all**. Sales
saw candidates still being sourced by TA and screened by RMG — mostly other
people's unfinished work, with the actionable rows buried.

Sales and Sales_Head now see the pipeline **from the handoff onward**, which is
exactly the "L1 and L2 done" list you asked for: by the time a candidate reaches
Sales Screening they have cleared AI L1 and whatever L2 round RMG requested.

| | Sales sees |
|---|---|
| Sourcing, Technical Screening, RMG Review | **no** — not yet theirs |
| RMG Rejected | **no** — never reached them |
| Sales Screening → Joined | yes |
| Sales Rejected, Customer Rejected, Self Withdrawn | yes — their own outcomes |

TA, RMG, HR, Finance and Admin are unrestricted. Someone holding *both* Sales
and RMG keeps the full RMG view — an unscoped role lifts the restriction rather
than intersecting with it.

### 4. AI interview status was wrong or missing in three places

You were right that it needed fixing "everywhere". There were three distinct
faults:

| Where | Was | Now |
|---|---|---|
| **Opportunity → Applicants** | no AI column at all | shows the AI outcome |
| **Candidate Profiles list** | no AI column at all | shows it, and on by default |
| **Requirement → Resumes tab** | showed the **raw AI verdict**, so a candidate already marked Selected still read "Failed 57.2%" | shows the recruiter's decision, with the AI verdict beside it |

Root cause of the first two: `GET /api/candidate-profiles` never joined
`ai_interview_links`, so the data simply was not there. Root cause of the third:
`enrich_resumes_with_ai` was never updated when the override columns were added.

All three now render through one shared `AiInterviewCell`, so they cannot drift
apart again.

---

## Notice period

The apply form has always asked for it, and it shows on the Resumes tab — but
the code that creates the candidate from an application **never copied it
across**. It sat in `Resume.application_details` JSON only, so
`candidates.notice_period` stayed NULL and the Notice Period column was blank
for everyone who applied online.

Fixed forward, and **Notice Period is now a default column** on Candidate
Profiles (it was registered but switched off, so nobody saw it).

For candidates who applied before the fix:

```bash
cd backend
python scripts/backfill_notice_period.py            # dry run
python scripts/backfill_notice_period.py --apply    # write
```

It only fills empty values — a notice period a recruiter typed by hand always
wins.

---

## Still open, for you to decide

**L2 rounds are not visible in the Sales list as such.** Sales sees that a
candidate reached Sales Screening, which implies RMG was satisfied, but not
whether an L2 actually happened. If you want an explicit "L1 ✓ L2 ✓" indicator
rather than an inference, that is a column on the profiles list reading the
interview-events count — say the word.

---

## Files changed

| File | Change |
|---|---|
| `services/candidate_profiles.py` | stage-arrival notifications; `visible_statuses_for()`; `latest_ai_interviews()` |
| `routers/crm/candidate_profiles.py` | apply the visibility scope to the list query |
| `services/resumes.py` | surface the HR override on the Resumes tab |
| `services/slot_booking.py`, `services/candidates.py` | carry notice period from the apply form |
| `routers/crm/table_preferences.py` | register the `ai_interview` column |
| `scripts/backfill_notice_period.py` | new — backfill for existing candidates |
| `crm/components/AiInterviewCell.tsx` | new — one AI status renderer for every table |
| `crm/pages/Profiles.tsx` | AI + notice period columns, both on by default |
| `crm/pages/Opportunities.tsx` | AI column on Applicants |
| `crm/pages/Requirements.tsx` | Resumes tab honours the override |
| `crm/CrmApp.tsx` | notifications navigate |
| `tests/test_pipeline_handoff.py` | new — 16 tests |

## Verify

```bash
cd backend && python -m pytest tests/test_pipeline_handoff.py -q   # 16 passed
```

No migration. Restart the backend.
