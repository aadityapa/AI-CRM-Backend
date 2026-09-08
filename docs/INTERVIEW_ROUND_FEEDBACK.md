# Interview round feedback (Candidate Profiles → Interviews)

RMG records feedback for every interview round on a candidate's application. The
fields mirror the Zoho `Interview_Round` subform, so rounds imported from Zoho and
rounds entered in the app are directly comparable and editable side by side.

## The form

**Candidate Profiles → open a profile → Interviews → Add interview feedback**

| Field | Control | Values |
|---|---|---|
| Interview Category | dropdown | Internal, External |
| Interview Round | dropdown | L1 / L2 / L3 / L4 - Interview |
| Employee | dropdown | active employees from the Employees tab |
| Interviewer name | text | only for an external panellist with no employee record |
| Interview Duration | dropdown | 15, 30, 45, 60, 90, 120, 180 minutes |
| Interview Status | dropdown | Cancelled, Completed, In-Progress, No Show, Pending, Rescheduled Requested By Candidate, Rescheduled Requested By Panel, Scheduled |
| Interview Date/Time From | calendar | date + time picker |
| Result | dropdown | No Hire, Leaning No, Leaning Hire, Hire, Strong Hire |
| Overall Feedback | textarea | free text |
| User Role | dropdown | Customer, HR, Interviewer, RMG, Sales, TA |

Picking an **Employee** links a real FK (`employee_id`) and fills `interviewer`
from the employee record; the free-text name box disables. Leave the dropdown on
"External / not listed" to type a panellist's name instead. One of the two is
required.

Every round on the tab has **edit** and **remove** buttons for the same roles.

## Access

`RMG` — plus `Admin` and `CEO`, which `role_required` grants implicitly. Everyone
with CRM access still *sees* the rounds; only these roles can add, edit or remove.

To widen it, change one line in `backend/services/interview_rounds.py`:

```python
WRITE_ROLES = ("RMG",)          # e.g. ("RMG", "TA")
```

The endpoints and the UI both read from that constant, so they cannot drift apart.

## Endpoints

| Method | Path | Roles |
|---|---|---|
| GET | `/api/candidate-profiles/{id}/interview-rounds/options` | any CRM role |
| GET | `/api/candidate-profiles/{id}/interview-rounds` | any CRM role |
| POST | `/api/candidate-profiles/{id}/interview-rounds` | RMG, Admin, CEO |
| PUT | `/api/candidate-profiles/{id}/interview-rounds/{event_id}` | RMG, Admin, CEO |
| DELETE | `/api/candidate-profiles/{id}/interview-rounds/{event_id}` | RMG, Admin, CEO |

The dropdown vocabulary is served by `/options` rather than hard-coded in the UI,
so the form and the server's validation come from one definition in
`services/interview_rounds.py`. `/options` also returns the employee list from the
interview router rather than the Employees API, so the picker does not depend on
the Employees tab access template.

`PUT` is a partial update — only the fields you send are changed.

Add, edit and delete are written to the profile's Activity Log as
`INTERVIEW_ROUND_ADDED`, `INTERVIEW_ROUND_UPDATED`, `INTERVIEW_ROUND_DELETED`.

## Schema (migration 0056)

Added to `interview_events`:

| Column | Type | Zoho source |
|---|---|---|
| `interview_category` | VARCHAR(20) | `Interviewer_Category` |
| `duration_minutes` | INTEGER | `Interview_Duration` |
| `user_role` | VARCHAR(40) | `UserRole` |
| `employee_id` | INTEGER FK employees | `Employee` |

Existing columns already cover the rest: `kind` (Interview Round), `status`,
`scheduled_at`, `result`, `feedback`, `interviewer`.

```
cd backend
python -m alembic upgrade head
```

## Imported data

The importer maps these fields too, snapping values onto the app's vocabulary so
imported rounds open cleanly in the form. Verified against the full export —
**every value lands inside a dropdown, none outside**:

| Field | Imported values |
|---|---|
| Category | Internal 3,334 · External 141 |
| Duration | 15 min 2,328 · 30 min 910 · 60 min 117 · 45 min 12 · 180 min 6 |
| User Role | TA 2,597 · RMG 840 · Sales 54 · HR 2 |
| Status | Completed 1,759 · Pending 895 · Scheduled 772 · Rescheduled (candidate) 67 · In-Progress 1 |
| Result | Hire 938 · No Hire 767 · Leaning Hire 33 · Leaning No 4 · Strong Hire 1 |
| Round | L1 3,025 · L2 465 · L3 5 · Other 5 |

Casing is normalised on import — Zoho's "ReScheduled Requested By Candidate" and
"Leaning hire" store as the canonical spellings. Panel members whose name matches
an active employee also get a real `employee_id`.

Five rounds carry a round type outside L1–L4 and import as `Other`. They display
as "Interview", and the edit form keeps that value visible as an extra option so
opening one does not silently change its type.

## Note on the existing L2 button

The RMG decision banner's **"L2 — Face to face"** action is unchanged. It still
schedules a round with just date, link and note, and emails the candidate an
`.ics` invite. Use it to *schedule*; use the Interviews tab to *record the
outcome* — the round it creates can be opened with Edit and completed with
category, duration, result and feedback.
