# Opportunity → Customer: does the flow work?

Traced end to end. **Steps 1–7 already worked.** Step 8 did not exist and now does.

---

## The flow, stage by stage

| # | Stage | Status | Pipeline status shown |
|---|---|---|---|
| 1 | Sales creates the Opportunity | works | — (opportunity, not profile) |
| 2 | Sales Head approves | works | — |
| 3 | RMG reviews and **adds the JD** | works | — |
| 4 | TA can source | works | `Sourcing` |
| 5 | TA uploads CVs → ATS scan → AI L1 | works | `Technical_Screening` |
| 6 | RMG reads the report, L2 or forward | works | `RMG_Review` |
| 7 | Sales submits to customer | works | `Sales_Screening` → `Customer_Screening` |
| 8 | **Sales records customer feedback** | **was missing — now built** | `Customer_Interview` → `Shortlisted` / `Customer_Rejected` |

### Things I expected to be gaps and were not

- **The JD is genuinely mandatory.** `engineering-approve` refuses with a 400
  unless there is either `rmg_jd_text` or an attachment of kind `rmg_jd`.
  RMG cannot approve an empty requirement.
- **TA visibility is a real whitelist**, not a convention:
  `Open_For_Sourcing / Posted_On_Portals / In_Progress / Fulfilled`.
- **Every stage transition is guarded** by both a transition map and a
  stage-authority map, and every one demands a comment.
- **"TA informs RMG" is automatic.** On a passing AI L1 the profile
  auto-advances `Technical_Screening → RMG_Review` and RMG is notified with
  the score and a deep link. There is no manual step for TA to forget.

### Your Sales visibility rule already holds

> only Sales Person received profiles that have clear all round & only customer
> round is pending

Sales and Sales_Head see `Sales_Screening` onward. Reaching `Sales_Screening`
*means* the candidate cleared AI L1 and whatever L2 RMG asked for — that is
exactly "internal rounds done, customer round pending". `Sourcing`,
`Technical_Screening`, `RMG_Review` and `RMG_Rejected` are hidden from them.

---

## What was missing: step 8

`Customer_Interview` was already a legitimate `InterviewEvent.kind` — the data
model says so in a comment, the display ordering ranks it last, and
Zoho-imported history contains rows of that kind. But the live API had **two**
independent blocks:

1. `ROUND_VALUES` only listed `L1–L4`, so the value was rejected as invalid.
2. `WRITE_ROLES = ("RMG",)`, so Sales could not write any round at all.

The net effect: the final step of your pipeline could be *read* from imported
history but never *written* in the product. The only trace Sales could leave
was a free-text comment on a status change, in the activity log.

### The fix: rounds are owned per kind

A single `WRITE_ROLES` tuple cannot express "RMG owns L1–L4, Sales owns the
customer round", so it became a map:

| Round | Owner |
|---|---|
| L1 Interview, L2 F2F, L3, L4 | RMG |
| **Customer Interview** | **Sales, Sales_Head** |

Neither can write the other's. That is not bureaucracy — Sales recording an L2
result would be inventing an engineering opinion, and RMG recording customer
feedback would be inventing the client's.

Enforced in four places, all covered by tests:

- **create** — checks the kind being written
- **update** — checks the round as it stands *and* as it would become, so an
  edit cannot convert someone else's round into one you own
- **delete** — checks the round being removed
- **options** — the form is served only the rounds this role may save, so a
  user is never offered a choice the save would reject with a 403

The customer round reuses the existing `InterviewEvent` fields rather than a
new table: date, duration, panel member, status, result (`No Hire` → `Strong
Hire`), and free-text feedback. Sales sees the same form RMG uses, with the
round preselected and the button labelled "Add customer feedback".

---

## Status column

`pipeline_status` carries the stage, and the list already decorates it with the
latest round and the AI score, so the directory shows all three at once.

One honest gap: **there is no status meaning "customer feedback recorded"**.
The profile sits in `Customer_Interview` until Sales moves it to `Shortlisted`
or `Customer_Rejected`. The feedback is visible on the Interviews tab, but the
status column cannot distinguish "waiting on the customer" from "customer has
answered, awaiting our decision". Adding one is a migration plus a transition-map
change — say the word if you want it.

---

## Files

| File | Change |
|---|---|
| `services/interview_rounds.py` | `Customer_Interview` round; `ROUND_WRITE_ROLES`; `roles_for_round`, `rounds_writable_by`, `ensure_may_write_round`; per-user `options()` |
| `routers/crm/candidate_profiles.py` | per-kind checks on create/update/delete; options passes the user |
| `crm/pages/Profiles.tsx` | per-round edit controls; Sales-aware button label; round dropdown filtered to writable kinds; default kind from the server |
| `tests/test_interview_round_authority.py` | new — 18 tests |

## Verify

```bash
cd backend && python -m pytest tests/test_interview_round_authority.py -q   # 18 passed
```

396 tests pass overall; the 6 failures are pre-existing or sandbox-environmental.
No migration. Restart the backend.
