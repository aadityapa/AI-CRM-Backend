# AI interview scheduling + invite email

## What the "AI Interview" tab does now

Scheduling used to be a single confirm click: it booked a session for *now*, took
no candidate details, and only emailed the invite if `AI_INTERVIEW_AUTOSEND` was
already on. There was no way to correct a session booked by mistake.

**Schedule AI interview** now opens a form:

| Field | Notes |
|---|---|
| Interview date & time | Required. Recruiter's local wall-clock time — defaults to one hour from now. This is what appears in the candidate's email. |
| Candidate name | Prefilled from the candidate record, editable. |
| Candidate email | Prefilled, editable. **The invite goes here.** Editing it does not change the candidate master. |
| Note for the candidate | Optional, appended to the session notes. |
| Email the invite now | On by default. Uncheck to only generate the link and share it yourself. |

Each pending session row has **Edit** and **Delete**:

- **Edit** — change date/time, name or email, and optionally re-send the invite.
  The invite token and access key are preserved, so a link already shared with the
  candidate keeps working.
- **Delete** — cancels the session and removes the legacy `interview_schedule` row,
  so the invite link stops working.

Both are blocked once the candidate has opened or verified the session
(`interview_started_at` / `verified_at` set), and completed interviews can never be
edited or deleted — the result is part of the record. The server enforces this;
the UI just reflects `can_modify` from the API.

Every action is written to the profile's Activity Log:
`AI_INTERVIEW_SCHEDULED`, `AI_INTERVIEW_RESCHEDULED`, `AI_INTERVIEW_CANCELLED`,
`AI_INTERVIEW_INVITE_EMAIL` (with the send result or the error).

## Endpoints

| Method | Path | Roles |
|---|---|---|
| GET | `/api/candidate-profiles/{id}/ai-interviews` | TA, RMG, Sales, Sales_Head, HR |
| POST | `/api/candidate-profiles/{id}/ai-interviews` | TA, RMG, Sales |
| PUT | `/api/candidate-profiles/{id}/ai-interviews/{link_id}` | TA, RMG, Sales |
| DELETE | `/api/candidate-profiles/{id}/ai-interviews/{link_id}` | TA, RMG, Sales |

`POST` body (every field optional — an empty body reproduces the old behaviour):

```json
{
  "scheduled_at": "2026-08-12 15:30",
  "candidate_name": "Kumar Saurabh",
  "candidate_email": "candidate@example.com",
  "notes": "Please join from a quiet room",
  "send_email": true
}
```

The response includes `email_sent` and `email_error`, so the UI can tell you the
interview was scheduled *but the email failed* rather than reporting a false success.

## Email configuration

Set in `.env` (gitignored — never committed):

```
SMTP_ENABLED=true
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USE_TLS=true
SMTP_USER=pavan.sanap@karnex.in
SMTP_PASSWORD=<password or app password>
SMTP_FROM=pavan.sanap@karnex.in
AI_INTERVIEW_AUTOSEND=true
PUBLIC_BASE_URL=https://192.168.1.87:2020
```

`AI_INTERVIEW_AUTOSEND` is only the *default* for the checkbox now — the recruiter's
choice in the modal wins either way.

### Test mail delivery first

```
cd backend
python scripts/test_smtp.py --check              # config + login only
python scripts/test_smtp.py                      # sends to SMTP_USER
python scripts/test_smtp.py someone@example.com  # sends to a specific address
```

It reports config, TLS, and authentication as separate steps, so a failure points at
the actual cause. **Run this before testing in the UI** — if it fails, the invite
email fails identically.

### If Office 365 rejects the login

Microsoft disables SMTP AUTH by default on new tenants, and blocks plain passwords
when MFA is on. Fixes, in order of preference:

1. Microsoft 365 admin centre → Users → *the mailbox* → Mail → **Manage email apps**
   → tick **Authenticated SMTP**.
2. If the account has MFA, generate an **App Password** and use that as
   `SMTP_PASSWORD` (the normal sign-in password will not work).
3. Check that security defaults / conditional access are not blocking legacy auth
   for this mailbox.

If IT will not enable SMTP AUTH, switch to a transactional provider (SendGrid, SES,
Postmark) — only the four `SMTP_*` values change; no code change is needed.

### `PUBLIC_BASE_URL` matters for real candidates

It is currently `https://192.168.1.87:2020`, a LAN address. Invite links built from
it only work for someone on your office network — fine for testing, but external
candidates will get an unreachable link. Point it at the public hostname before
sending real invites.

## Security note

`SMTP_PASSWORD` is a live credential in `.env`. `.env` is gitignored and untracked,
so it will not be committed — but rotate the password if it has been shared in
chat, email or a ticket, and prefer an app password or a dedicated no-reply mailbox
over a personal account.
