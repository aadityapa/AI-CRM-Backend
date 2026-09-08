# Karnex — email notifications

Adds staff and employee email notifications to the Karnex backend, on top of a
durable outbox. Built against `AI-Interview-Model-B-V2` @ `wip/pipeline-users-admin`.

## Apply

```bash
cd /path/to/AI-Interview-Model-B-V2      # the folder containing backend/
python apply_email_notifications.py --check   # dry run: shows every edit, changes nothing
python apply_email_notifications.py           # apply

cd backend
alembic upgrade head                          # creates email_outbox (migration 0064)
# restart the backend — the outbox worker starts with the app
python scripts/test_smtp.py --check           # confirm SMTP still authenticates
```

The script is idempotent — re-running it reports `already applied` rather than
double-patching. If any line says `NO MATCH`, that file changed since this
bundle was built; the script tells you which one and applies nothing to it.

## What was wrong

Every email the application sent went to a **candidate** — interview invites,
slot invites, L2 invitations. The only staff email in the entire system was the
password reset. Everything else was a bell row in the `notifications` table.

That has two failure modes:

1. A Sales Head who is not looking at the app does not learn an opportunity is
   waiting on them. Twenty-seven such notifications exist in the code today.
2. `notify_user` needs a `user_id`. Both leave and timesheets guard with
   `if emp.user_id`, and `employees.user_id` is nullable — so contractors, new
   joiners and anyone without a login were **silently dropped**. Exactly the
   people least likely to be watching a bell were the ones guaranteed to miss it.

## Design

**Sender identity.** The envelope sender is always `SMTP_FROM`, so SPF and DKIM
stay aligned with your tenant. The acting user's identity rides in the headers:

```
From:     "Pavan Sanap (Karnex)" <SMTP_FROM>
Reply-To: pavan.sanap@karnex.in
```

The candidate sees who is handling them, and hitting reply reaches that
recruiter's own inbox rather than a shared one. This works today with no
Exchange changes.

A literal `From: pavan.sanap@karnex.in` is possible but needs your Office 365
admin to grant the service account Send As on each mailbox — otherwise O365
rejects with `5.7.60 Client does not have permissions to send as this sender`:

```powershell
Add-RecipientPermission -Identity pavan.sanap@karnex.in `
  -Trustee svc-karnex@karnex.in -AccessRights SendAs
```

Once that is in place, changing `msg["From"]` in `email_smtp.send_email` to use
`reply_to` is a one-line switch. The outbox already stores both addresses.

**Durable outbox.** `email_smtp.send_email` is synchronous `smtplib` with a
30-second timeout, and role fan-outs hit several people. Sending inline would
put minutes of SMTP latency inside a request; fire-and-forget would repeat the
`hr_records.json` failure mode — a transient error swallowed on a detached
thread with nobody ever learning the message was lost.

So handlers `INSERT` into `email_outbox` **in the same transaction as the
business change** (a submit that rolls back never mails anyone), and a daemon
worker drains it with backoff — 1, 5, 15, 60, 240 minutes, then `Failed` after
5 attempts. The claim query uses `FOR UPDATE SKIP LOCKED`, so raising
`UVICORN_WORKERS` above 1 will not double-send.

**One call site, both channels.** `notify_user` / `notify_role` now queue an
email alongside the bell row. All 27 existing notifications gain email without
touching their call sites. Two helpers are new:

- `notify_roles(db, ("HR", "Finance", "RMG"), ...)` — the union of several
  roles, each person once.
- `notify_employee(db, emp, ...)` — bell when they have a login, email
  **always**, using `employees.email` (NOT NULL). This is the one that fixes
  failure mode 2.

## What now sends email

| Event | Recipients | Status |
|---|---|---|
| **Timesheet submitted** | HR + Finance + RMG | **new** — notified nobody before, not even a bell |
| **Timesheet approved** | the employee | **new** |
| **Timesheet rejected** | the employee (with reason) | **new** — rejection silently reverses their leave/comp-off ledger |
| **Leave submitted** | HR | **new** — approve/reject notified the employee, submit notified nobody |
| Leave approved / rejected | the employee | bell → **bell + email**, now reaches employees with no login |
| Timesheet due reminder | the employee | bell → **bell + email** |
| Invoice generated from timesheet | Finance | bell → **bell + email** |
| Opportunity submitted / approved / rejected / resubmitted | Sales Head, creator | bell → **bell + email** |
| Requirement submitted → SH approve → RMG approve → open for sourcing | Sales Head, RMG, TA, creator | bell → **bell + email** |
| Requirement fulfilled / closed / cancelled | creator | bell → **bell + email** |
| Candidate stage arrivals (8 stages) | RMG / Sales / Sales_Head / HR | bell → **bell + email** |
| AI interview completed · L1 passed → RMG | TA, RMG | bell → **bell + email** |
| L2 face-to-face scheduled | TA | bell → **bell + email** |
| Slot confirmed · auto-shortlist needing follow-up | TA | bell → **bell + email** |
| Template request raised / fulfilled / prepared | RMG, requesting TA | bell → **bell + email** |

## Operating it

`GET /api/email-outbox/stats` (Admin) is the first place to look when someone
says notifications stopped:

```json
{ "by_status": {"Queued": 3, "Sent": 812, "Failed": 0, "Skipped": 0},
  "top_failing_events": [], "oldest_pending_at": "...",
  "smtp_configured": true, "notifications_enabled": true }
```

- `GET  /api/email-outbox?status=Failed` — what did not go out, and why
- `POST /api/email-outbox/drain` — send now instead of waiting for the tick
- `POST /api/email-outbox/{id}/retry` — reset one row

**Switches.** `EMAIL_NOTIFICATIONS_ENABLED=false` kills all outbound mail.
Per-event opt-out is an `app_settings` row — `email_event_timesheet.submitted`
= `false` — checked both when queuing and when sending, so turning a noisy event
off also stops its backlog. `EMAIL_OUTBOX_WORKER=false` if you would rather a
cron call the drain endpoint.

## Before you turn it on

1. **Rotate the SMTP password.** It is in `backend/.env` in plaintext and has
   been shared in chat. Switch to an App Password.
2. **Set `PUBLIC_BASE_URL` to a public hostname.** It is currently a LAN
   address, so every "Open in Karnex" link in these emails will be unreachable
   from outside the office.
3. **Check the O365 send limit** — 30 messages/minute, 10,000 recipients/day on
   a normal mailbox. A `scan-all` over a large requirement plus a role fan-out
   can approach the per-minute cap; `EMAIL_OUTBOX_BATCH` and
   `EMAIL_OUTBOX_INTERVAL_SEC` are the throttle (default 25 per 20s).

## Not included

Deliberately out of scope for this pass, all documented in the analysis report:

- **Reminders and dunning** — interview reminders (T-24h/T-1h), PO expiry,
  invoice overdue, leave use-it-or-lose-it warnings. All of these need a
  scheduler, and the codebase has none (`timesheets.py` says so in a comment).
  The outbox worker is the natural place to hang one.
- **Candidate-facing rejection and acknowledgement mail** — resume rejected,
  application received, AI interview cancelled. Each needs copy you are happy to
  send externally; the plumbing is ready.
- **New-user welcome mail** — worth adding, but it must not carry the password
  the admin typed. It should point at "Forgot password" instead.
- **Customer-facing invoice delivery** — `ContactPerson.notification` already
  stores `Email|SMS|Both|None` per contact and nothing reads it.
