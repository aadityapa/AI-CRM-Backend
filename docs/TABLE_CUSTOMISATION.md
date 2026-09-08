# Per-user table layout — Candidate Profiles

```
cd backend
python -m alembic upgrade head     # applies 0060
```

Then rebuild the dashboard.

## What a user gets

A **Columns** button on the Candidate Profiles toolbar opens *Customise this table*:

* **Columns** — tick what to show, drag (or use the arrows) to set the order.
  26 columns are available; 15 show by default.
* **Sort priority** — stack up to 4 rules, Excel-style. The first rule wins and
  each later one breaks ties, e.g. *Customer A→Z, then Status, then Expected CTC
  high→low*.
* **Reset to default** — back to the shipped layout.

Saved **against the signed-in user**, so the layout follows them to any machine
and survives clearing browser data. Every role gets this; one person's layout
never affects anyone else's.

## Available columns

Candidate, Email, Phone, Exp (Yrs), Notice Period, Domain, Opportunity, Customer,
Status, Stage, Current CTC (Lac), Expected CTC (Lac), Hike %, Approved CTC Budget
(Lac), CTC Approval (Lac), Interview Round, Interview Status, Interview
Date/Time, CV, Resignation Cert., Commercial Approval, Submitted to Customer,
Onboarding Date, TA Owner, Applied On, Created.

Sortable is a deliberate subset — **Approved CTC Budget**, **Interview Round /
Status / Date** and **CV** are computed or come from a related table per row, so
there is no SQL column to order on. They can be shown, just not sorted.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/me/table-preferences/{table_key}` | layout + which columns exist and which are sortable |
| PUT | `/api/me/table-preferences/{table_key}` | save |
| DELETE | `/api/me/table-preferences/{table_key}` | reset to default |

The list itself takes `?sort=customer:asc,pipeline_status:desc,expected_ctc:desc`.

`user_table_preferences` is `(user_id, table_key)` unique with the layout as
JSONB, so making another list customisable needs a `table_key` entry in
`TABLE_REGISTRY` — no migration.

## Design decisions worth knowing

**Saved layouts are cleaned on read as well as write.** A column removed in a
later release is dropped, and a newly added one is appended hidden. So a stale
layout degrades instead of breaking the page, and a new column never silently
rearranges something a user carefully tuned.

**Unknown sort keys are ignored, not rejected.** `?sort=bogus:asc,customer:desc`
sorts by customer rather than returning 400 — a saved layout pointing at a
retired column should still load.

**Every sort ends with `id DESC`.** Without a stable tiebreak, two rows with the
same customer could swap places between page 1 and page 2, so a row would appear
twice or not at all.

**Sorting only joins what it needs.** Candidate is joined only for name/email/
experience sorts, Opportunity and Customer only when sorting on those — and the
code guards against joining Candidate twice when a search has already joined it.

**Sort depth is capped at 4.** Past that the ordering stops being meaningful and
just costs the database work.

**At least one column must stay visible** — enforced server-side too, not only in
the dialog.

## Related fix

`GET /api/candidate-profiles` previously ignored `sort_by` entirely and always
ordered by `id`, so the list could not be sorted at all. It now honours the
multi-level `sort` parameter.
