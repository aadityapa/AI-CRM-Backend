# Candidate Profiles — new fields and workflow actions

Run the migration, then rebuild the dashboard:

```
cd backend
python -m alembic upgrade head     # applies 0059
```

## What the 13 requested items turned out to be

They were three different kinds of thing, so they are handled three different ways.

### Already pipeline statuses — no new column

`Customer Approval Pending` and `Rejected` are existing `pipeline_status` values
(9 and 116 rows in the export), alongside RMG / Customer / Sales Rejected. They
are shown by the Status column that was already there.

`Customer Approved` does not exist anywhere in the Zoho data — the nearest real
value is `Project Allocation Pending`.

### Data columns — added in migration 0059

| Field | Column | Where it shows |
|---|---|---|
| CV | `resume_url` + `cv_original_filename` | list column + Workflow panel |
| Interview Round | latest `interview_events.kind` | list column |
| Interview Status | latest `interview_events.status` | list column |
| Interview Date/Time | latest `interview_events.scheduled_at` | list column |
| Submit to Customer | `customer_submission_date` | Workflow panel + action |
| Onboarding Date | `customer_onboarding_date` | Workflow panel |
| Commercial Approval Status | `commercial_approval_status` | Workflow panel |
| Resignation Certificate | `resignation_certificate_url` | Workflow panel |

Also added, since the import needs somewhere to put them: `sales_submission_date`,
`technical_submission_date`, `approved_ctc`, `offer_letter_reference`, `stage`,
`candidate_pre_status`, `employee_ref`, `created_by_name`, `user_role`,
`comments_text`, and the four `is_archive_*` flags.

Plus `source` and `is_hidden` — `source` defaults to `'app'` for existing rows and
the importer stamps `'zoho_import'`; `is_hidden` lets a profile be hidden from
lists without deleting it. The list endpoint takes `?source=` and
`?include_hidden=true`.

`interview_events` gains a unique `zoho_interview_id` plus interviewer email and
phone, end time, weightage, scorecard reference and venue.

### Actions, not fields

`Schedule Technical Interview`, `Submit Technical Feedback` and `Submit to
Customer` appear nowhere in the data as values — they are things a user *does*.
They are now buttons at the bottom of the Overview tab:

* **Schedule Technical Interview** — jumps to the Interviews tab with the add-round
  form open. Reuses the existing form rather than duplicating it.
* **Submit Technical Feedback** — opens the *latest* round for editing so the
  result and feedback land on the interview that just happened. With no rounds
  yet it opens a blank one and says so.
* **Submit to Customer** — `POST /api/candidate-profiles/{id}/submit-to-customer`
  (Sales / Sales_Head). Stamps `customer_submission_date` and, when the profile is
  at Sales Screening *and* the user has authority over that stage, advances it to
  Customer Screening through the normal `perform_transition`, so stage rules and
  the activity log behave as they do everywhere. Authority is checked before
  anything is written, so the date and the stage can never disagree. Re-clicking
  returns 409 with the existing date rather than silently overwriting it.

## Fill rates — worth knowing before you look

From `candidate_profiles.json` (8,101 rows):

| Field | Filled |
|---|---:|
| CV / resume_file | 8,014 |
| Interview round + status | ~3,584 |
| Interview date/time | 2,655 |
| Resignation certificate | 436 |
| Customer submission date | 299 |
| Commercial approval status | **19** |
| Customer onboarding date | **7** |
| Technical submission date | **0 — entirely empty in Zoho** |

This is why the near-empty ones are in the Workflow panel rather than the table:
as columns they would read "—" for effectively every row and push the useful
columns off-screen.

## Layout

**List** gained: Interview Round, Interview Status, Interview Date/Time, CV.
Together with the earlier change, Hike % is replaced by Approved CTC Budget (Lac).

**Overview tab** gained a **Workflow** panel: stage, the three submission dates,
onboarding date, commercial approval status, offer letter reference, employee
reference, created-by, CV, resignation certificate and comments.

## Still to come

The values for most of these arrive with the `candidate_profiles.json` /
`interviews.json` import, which is still waiting on the output of
`scripts/report_crm_state.py`. The columns exist and render now; they will read
"—" until that import runs.
