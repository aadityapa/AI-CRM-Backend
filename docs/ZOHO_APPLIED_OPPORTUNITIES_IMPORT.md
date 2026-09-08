# Zoho applied-opportunities import — runbook

Imports `candidate_applied_opportunities.json` (Zoho Creator NEXUS export) as the
authoritative candidate ↔ opportunity linkage, and joins it to
`ta_all_candidate_profiles.ndjson` to bring in interview history.

## Why this exists

`tools/link_candidates_opportunities.py` documents the original problem: *"The
candidate <-> opportunity relation is NOT present in the Zoho exports we have —
the opportunities' Candidates subform came back empty and the candidate file's
Opportunity_ID column is blank."* That is why some opportunities show no
applicants: there were simply no `candidate_profiles` rows for them.

This export supplies the missing mapping — 8,003 applications across 85
opportunities — and `profile_id` joins each one to its Zoho profile record, which
carries the interview rounds, skill evaluations, offers and activity history.

## Order of operations

The importer resolves against data already in the database, so masters go first.

```
cd backend

# 1. schema
python -m alembic upgrade head          # applies 0055

# 2. masters (skip any that are already loaded)
python tools/replace_customers.py --apply
python tools/import_opportunities_full.py --apply
python tools/import_candidates_full.py --apply

# 3. dry run — writes nothing, prints exactly what would happen
python tools/import_applied_opportunities.py

# 4. commit
python tools/import_applied_opportunities.py --apply --replace
```

`--replace` deletes `candidate_profiles` rows absent from the JSON (and their
activity, interview, skill-evaluation and offer children), making Zoho the single
source of truth. Drop the flag to upsert without deleting.

## Expected result

Simulated against the current export files:

| | |
|---|---|
| Applications parsed | 8,003 |
| Candidate resolved via Zoho-id bridge | 7,986 (99.8%) |
| Skipped — candidate not in DB | 17 |
| Skipped — opportunity not in DB | 9 |
| Duplicate (candidate, opportunity) collapsed | 11 |
| **`candidate_profiles` rows** | **7,966** |
| Opportunities with applicants | 85 |
| Distinct candidates linked | 6,530 |
| Interview rounds imported | ~3,479 |
| Activity-log entries | ~17,521 |
| Unlinked in Zoho (no candidate attached) | 74 |

The 155 remaining opportunities have no applicants in Zoho either — those stay
empty legitimately.

Skipped and unlinked rows are written to
`import_templates/import_applied_unlinked.csv` with a reason per row.

## Field mapping

### JSON → `candidate_profiles`

| JSON | Column | Notes |
|---|---|---|
| `profile_id` | `zoho_profile_id` | durable external id; makes re-imports idempotent |
| `candidate_id` | → `candidate_id` | via `all_candidates_clean.csv` `zohoRecId` → email → app candidate |
| `opportunity_id` | → `opportunity_id` | joins `opportunities.opp_id` (e.g. `C-2026-00047`) |
| `status` | `pipeline_status` | `STATUS_MAP` in `import_candidate_profiles.py` |
| `expected_ctc` | `expected_ctc` | lakhs → rupees when ≤ 500 |
| `applied_on` | `applied_on` | `created_at` stays the row's insert time |
| `ta_person` | `ta_owner_name` + `ta_owner_id` | id set when the name matches a CRM user |
| `email`, `phone` | — | HTML-stripped, used for matching only |

### NDJSON subforms → child tables

| Zoho subform | Table |
|---|---|
| `Interview_Round` | `interview_events` (stage, mode, status, result, interviewer, feedback, meeting link) |
| `Skill_Evaluation` | `skill_evaluations` |
| `Offer_Release_History` | `offer_history` |
| `Activity_History` | `candidate_profile_activity_log` |

## Known information loss

Zoho's status vocabulary is richer than the app's 15 pipeline states and gets
collapsed on import: `HR Screening` and `L1-Technical Schedhuled` both become
`Technical_Screening`; `Hold` becomes `Shortlisted`. If those distinctions matter
operationally, they need new `PipelineStatus` values plus transition-map entries
in `services/candidate_profiles.py`.

Profiles are keyed to **opportunity**, not requirement, matching Zoho. A candidate
applying to two requirements under one opportunity produces two `resumes` rows but
a single `candidate_profiles` row.

## Re-running

Safe to re-run. Profiles upsert on `(candidate_id, opportunity_id)`; children are
de-duplicated before insert — interview rounds by `(kind, scheduled_at)`, skill
evaluations by skill, offers by date + CTC, activity by action + comment.

## If you already ran the import once (duplicate interview rounds)

The first version of this importer de-duplicated interview rounds on
`(kind, scheduled_at, stage)`. Rows written by the older
`import_candidate_profiles.py` have no `stage` — it packed the whole round into
`note` as `"Round: … / Stage: … / Result: … / <feedback>"` — so the keys never
matched and a **second row was inserted for every round that already existed**.
The Interviews tab then showed each round twice, with the feedback repeated.

Both the importer and the reader are fixed. To repair rows already in the database:

```
cd backend
python scripts/dedupe_interview_events.py            # report only
python scripts/dedupe_interview_events.py --apply    # write
python scripts/dedupe_interview_events.py --profile 15920   # one profile
```

It groups by `(profile_id, kind, scheduled_at)`, keeps the richest row, merges any
field the duplicates hold, deletes the rest, parses legacy notes into the real
columns (clearing the note so the feedback shows once), and rewrites interviewer
values that were stored as a raw Python list literal
(`[{'id': '270…', 'text': 'Mohit Arya'}]` → `Mohit Arya`).

Verified against the current export: all **3,479** interview rounds now merge into
existing rows, **0** new duplicates, and all **2,438** interviewer values resolve to
a name. This is a one-time repair — re-running the import will not re-create them.
