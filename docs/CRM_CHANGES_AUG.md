# CRM changes — leave policy, contact roles, onboarding status, approval flow

Run the migration first:

```
cd backend
python -m alembic upgrade head     # applies 0058 (seeds the PMO contact role)
```

Then rebuild the dashboard and restart the backend.

---

## 1. Leave Billing Policy — Initial Credit Balance and Effective Date removed

Both fields are gone from the form. The modal exists **twice** in the codebase and
both copies were changed:

| File | Used by |
|---|---|
| `crm/components/BranchWizardModal.tsx` | Branch create / edit |
| `crm/components/LeaveBillingPolicyModal.tsx` | Project create / edit |

The summary-grid columns in `ProjectPolicySections.tsx` were removed too — a
column nobody can fill would always read "—".

**The database columns stay.** `initial_credit_balance` is NOT NULL with a
`server_default` of 0, and both values are still sent in the save payload, so
**editing an existing policy never zeroes a value it already had**. Only newly
created policies get 0 / NULL.

Worth knowing: both fields still drive real logic —
`services/branch_policy.py:_annual_leave()` adds `initial_credit_balance` into the
annual entitlement, and `services/project_employee_leave_credit.py:accrual_start()`
uses `effective_date` to decide when accrual begins. New policies now behave as
"initial balance 0, accrual starts at onboarding". If either needs to be set
again, it can go back on the form or be handled by a migration.

`effective_date` is still shown read-only on the **Branch Policy** page
(`crm/pages/BranchPolicy.tsx`) — that is a diagnostic view, and the value explains
why accrual started on a given date. Ask if you want it removed there as well.

## 2. Customer Contact Persons — PMO plus inline "Add new role"

The Role dropdown in the opportunity wizard's contact modal was a hardcoded
`["Finance", "Operational", "Procurement", "HR"]`, so PMO literally could not be
recorded. It now uses `SearchableSelect` against the `/api/contact-roles` master
with `allowAdd`, matching what the Customer form already did — searchable, and any
new role typed in is created in the master and reused everywhere.

* **PMO** added to the seed list (`crm/constants/geo.ts`) and to the master via
  migration `0058`. The insert is idempotent, so it is safe if someone already
  added PMO through the UI.
* **Permission fix:** `POST /api/contact-roles` was **Admin-only**, so "add new
  role" would have failed with 403 for the Sales user who needs it. It now allows
  Sales, Sales_Head, RMG, TA, HR and Finance (`routers/crm/masters.py`). This also
  fixes the same latent failure in the existing Customer form.

## 3. Onboarding Status — limited for Sales

A plain **Sales** user now sees only **Sales Validation** and **Sourcing**. The
later stages (Interviewing, Offered, Onboarded, Closed) reflect what TA and RMG
are doing, not something Sales sets when raising the opportunity.

**Sales_Head and Admin keep the full list**, since they oversee the whole pipeline.
The allowed set is `SALES_ONBOARDING_STATUS_VALUES` in
`crm/pages/opportunity/opportunitySchema.ts`.

## 4. Sales Head approval — review in edit mode, then approve

### What already worked

The approval gate was already built end to end, so the reject half of your request
needed no changes:

* Sales creates an opportunity → `approval_status = Pending_Sales_Head_Approval`,
  Sales_Head notified. A Sales_Head/Admin-created one is auto-approved.
* **Reject requires a note** — minimum 10 characters, enforced server-side
  (`RejectIn.validated_reason`, HTTP 400 below that).
* The note is stored on `approval_rejection_reason`, written to the opportunity
  activity log, **and pushed to the creator as a notification**.
* The Sales person sees a red banner with the reason and a **Resubmit for
  approval** button, which returns it to pending and clears the reason.
* Until approved, stage transitions stay hidden and no Requirement is spawned.

### What was added

Previously **Approve** committed immediately — there was no way to correct
anything first. Now the pending banner offers:

* **Review & Approve** — opens the full opportunity wizard in edit mode, titled
  *"Review & Approve — C-2026-000xx"*. Sales Head changes whatever is needed, and
  the footer button reads **Save & Approve**: it PUTs the edits and then calls
  `/approve` in the same action, so a correction can never be left sitting
  un-approved. The approval is logged as *"Reviewed and approved by Sales Head"*.
* **Approve as-is** — the original one-click path, kept for when nothing needs
  changing.

No backend change was required: `PUT /api/opportunities/{id}` never blocked a
pending opportunity, and `POST /{id}/approve` already accepted an optional comment
that the UI simply was not sending. Approving still spawns the Requirement and
notifies the creator.

---

## Verification

* `tsc --noEmit` — **0 errors**
* `vite build` — **succeeds**, all chunks emitted
* Compiled bundle checked directly: "Initial Credit Balance" gone (0 chunks),
  PMO + `/api/contact-roles` + "Add new role" present, the Sales-only
  `["Sales Validation","Sourcing"]` list present, and "Review & Approve" /
  "Approve as-is" / "Save & Approve" all present
* Backend: `py_compile` + `ruff` clean, alembic single head at **0058**
* Reject validator exercised directly — `""` and `"too short"` rejected,
  a real reason accepted

Not run here: the app against a live database (Postgres is on your machine) and
the vitest suite (its rollup binary is Windows-built). Please click through the
Sales → Sales Head approval round-trip once before relying on it.
