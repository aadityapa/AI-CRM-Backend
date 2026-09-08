# Pipeline tail + Candidate Profiles filters

411 tests pass, TypeScript clean, app boots. No migration.

---

## 1 · Multi-select status filter

The status filter was single-select, so "everything waiting on me" took four
visits. It now accepts several statuses at once, in both **In pipeline** and
**Closed**.

Each selected status becomes its own removable chip rather than one "3
statuses" chip — otherwise narrowing is all-or-nothing. The server accepts a
comma-separated list and rejects unknown values by name, so a typo in a shared
URL says which status is wrong instead of silently returning everything.

## 2 · "Shortlisted" → "Customer Shortlisted"

**Label only.** The stored value stays `Shortlisted`, so there is no migration,
no data rewrite, and no risk to the transition map or to imported history.
`Customer_Approval` also now reads "Customer Approved", matching how you
describe it.

"Shortlisted" was ambiguous — we shortlist internally too. This stage
specifically means the *customer* shortlisted them.

## 3 · Change Status dialog

Now shows **who** you are moving and **which opportunity**, because a reviewer
moving several candidates in a row was looking at a dialog that only said
from-status → to-status.

The mandatory field is relabelled **Feedback**. "Comment" undersold it: at a
customer stage it *is* the customer's verdict.

## 4 · Customer feedback appears on the Interviews tab

Previously the Interviews tab showed L1 and L2 and then simply stopped — the
customer's decision existed only as a line in the activity log.

Now, when Sales leaves `Customer Interviewing` with a verdict, the feedback is
saved as a **Customer Interview round**:

| Moving to | Recorded result |
|---|---|
| Customer Shortlisted | Hire |
| Customer Approved | Hire |
| Customer Rejected | No Hire |

A bounce back to Customer Screening records nothing — that is a reschedule, not
the client's answer.

If Sales already logged the customer round explicitly, the verdict is filled in
and the note appended rather than a duplicate round being created. The whole
thing is best-effort: a bookkeeping failure can never roll back a legitimate
status change.

## 5 · Sales Head owns the final approval

This was the real structural change. Before, `Customer_Approval` authority was
`{Sales, Sales_Head}` — **Sales could approve its own offer.**

Now:

```
Sales                 Sales Head            HR
  │                        │                 │
  ├─ records the offer     │                 │
  │  (rate, joining date)  │                 │
  ├─ moves to ────────────►│                 │
  │  Customer Approved     ├─ reviews, edits │
  │                        ├─ approves ─────►│
  │                        │  → Preboarding  ├─ takes over
```

- `STAGE_AUTHORITY[Customer_Approval] = {"Sales_Head"}` — the person who
  proposes terms is not the person who signs them off.
- Sales Head is notified on arrival, with what they are being asked to do.
- Sales Head sees the profile in normal edit mode, so changing the rate or
  joining date before approving needs no special screen.
- Approving moves it to `Preboarding` and notifies HR.

### An offer is now required to enter Customer Approval

Customer Approval means "these are the terms we are asking you to approve".
Without an offer on record there are no terms, and Sales Head would be
approving an empty proposal. Entering the stage without one returns:

> Record the offer first — the candidate's rate and joining date are what Sales
> Head is being asked to approve. Add it on the Offers tab.

Rate and joining date already live on `offer_history` (`ctc`, `joining_date`),
so this needed no new fields.

---

## Still open

**There is no status meaning "customer feedback recorded".** A profile sits in
`Customer Interviewing` until Sales moves it on, so the status column cannot
distinguish "waiting on the customer" from "customer answered, awaiting our
decision". The feedback itself is now visible on the Interviews tab. Adding the
state is a transition-map change — worth doing if that distinction matters day
to day.

**Sales Head's edit step is the normal profile editor**, not a dedicated
approval screen with the offer terms front and centre. That is a smaller,
safer change than building a bespoke approval view, but if you want the rate
and joining date presented as an approve/reject card, say so.

---

## Files

| File | Change |
|---|---|
| `services/candidate_profiles.py` | Sales Head authority; `ENTRY_REQUIREMENTS`; `_record_customer_round_from_transition`; `_CUSTOMER_VERDICT`; arrival notice wording |
| `routers/crm/candidate_profiles.py` | `pipeline_status` accepts a CSV |
| `crm/components/MultiSelectFilter.tsx` | new |
| `crm/components/ui.tsx` | Customer Shortlisted / Customer Approved labels |
| `crm/pages/profiles/ProfileToolbar.tsx` | multi-select status |
| `crm/pages/profiles/ProfilesListPage.tsx` | one chip per status |
| `crm/pages/Profiles.tsx` | dialog context header; Comment → Feedback |
| `tests/test_pipeline_tail.py` | new — 15 tests |

```bash
cd backend && python -m pytest tests/test_pipeline_tail.py -q   # 15 passed
```
