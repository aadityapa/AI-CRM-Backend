# Karnex — What's New & How To Use It (12 Aug 2026)

A plain-language guide to everything added this week. Written for admins
first; share the relevant sections with each team.

---

## 0. Deploy first (one time)

```bat
cd F:\AI-Interview-Model-B-V2
start_app.bat          :: runs migrations 0068 → 0072, starts backend
rebuild_all.bat        :: also rebuilds the dashboard (use this if in doubt)
```

Then every user must **log out and log back in** (or Ctrl+Shift+R).
Access is snapshotted at page load — old sessions show old buttons.

Migrations applied: 0068 PO renewal link · 0069 project without opportunity ·
0070 template grants upgraded · 0071 email templates · 0072 week-off pattern.

---

## 1. Access Templates — who can do what

**The rule now: for a user WITH a template, the template alone decides.**
Roles only matter for users without a template. Admin/CEO always see everything.

How to set up a role's access:

1. **Users → Access Templates → New template** (e.g. "Sales").
2. For each tab pick a level: **No access / View / View + Edit / View + Edit + Create.**
   Higher includes lower. "Create" is needed for New buttons (New Project,
   New Candidate…).
3. Expand a tab to lock **individual fields** — e.g. Candidate Profiles →
   Approved CTC → *View only*. That field greys out in the form for this
   user AND the server refuses to save it. Fields inherit the tab level
   unless you set them.
4. **Assign to user** at the bottom. The user re-logs in.

Buttons that follow the template now: New Project, Map employee, Assign
employee, Edit project, Create PO, and all page edit/create actions.

**If someone says "I can't see X":**
```bat
cd F:\AI-Interview-Model-B-V2\backend
python scripts\diagnose_access.py <username or email> --tab projects
```
It prints their roles, template, and a verdict line saying exactly why.
Common causes: template tab set to View when Edit/Create is needed; template
inactive (an inactive-but-assigned template hides EVERYTHING); user didn't
re-login.

---

## 2. Settings you control from the UI (no code changes)

All under **CRM → Settings** (Admin/CEO):

| Tab | What you can change |
| --- | --- |
| **Organisation** | Company identity, public URL, sender domains, interview defaults, **all scheduler switches + run hour**, **TDS rate**, **Tax-invoice seller details** (name, address, GSTIN, PAN, bank account) |
| **Operations** | Live health of the 4 daily jobs (red = enabled but silent 48h), backups, **Run now** buttons |
| **UI Text** | Rewrite any status tooltip or empty-page lesson. Blank = built-in text |
| **App Settings** | Raw key/value store (AI pass threshold etc.) |
| Masters tabs | Departments, designations, skills, locations, document types, leave types |

Under **Users** (Admin/CEO):

- **Email flows** — per event: which roles get it, extra addresses, on/off,
  and now **custom wording**: subject and body templates with placeholders
  `{subject} {body} {recipient} {company}`. Example body:
  `Dear {recipient},` / `` / `{body}` / `` / `Regards, {company}`.
  Blank = standard text. A typo'd placeholder shows literally — mail is
  never lost.
- **Action permissions** — who may approve/reject timesheets, generate
  invoices, manage project employees / POs. Applies without restart.
- **Access templates**, per-user pause, invitations.

---

## 3. Daily work — what each role sees

**Dashboard → "Your work today"** is each person's to-do list: timesheets
awaiting their approval, own unsubmitted sheets, opportunities/requirements
waiting on them, expiring POs, overdue invoices, pending leave. Red items
first. Items for tabs their template hides never appear. Empty = "All clear".

**Learning aids everywhere:**
- Hover any status badge (the small ⓘ) → what it means and **who acts next**.
- Empty pages explain what the page is for and the workflow in one line.
- **Ask AI** (Ctrl+/) is now a full chat: full-screen mode with history,
  and it can look up YOUR data (open requirements, expiring POs, pipeline
  counts, timesheet status…). A "Checked your …" line under a reply means
  the answer came from your records. Finance data only for Finance/Sales
  Head; it can never change anything.

---

## 4. Timesheets & billing

- **Week off billable + Holidays billable** (Customer form → Leave & Holiday
  Billing) now work fully: with both on, a 31-day month bills 31 days.
  Comp-off fields are enabled only when Comp Off Billable is OFF (credit
  mode) — billed comp-off has nothing to configure.
- **Week-off days** (same section): tick which weekdays are the weekend
  (default Sat+Sun; Gulf customers Fri+Sat). Affects newly generated
  timesheets from next month; existing sheets keep their days.
- **Project → Timesheet tab** shows EVERY month owed since onboarding —
  months never created show a red **"Due — not created"**. Filter by
  employee, month, year, status (including Due) to chase gaps.
- Timesheet page: **Save entries / Submit moved to the bottom** — review all
  days, then act. Approvers already decide at the bottom the same way.
- **PO & Invoices tab** on a project: search + status/month/year filters,
  PO end-date column.

---

## 5. Sales / delivery flow changes

- **Projects need no Opportunity** any more — Name, Customer, Branch is enough.
- **PO renewal**: open a PO → **Renew PO**. Enter the customer's new PO
  number and value; customer, branches, contact, tax and terms carry over.
  The old PO is untouched, unspent balance does NOT carry, and both POs
  show the renewal chain. Cancelled POs can't be renewed.
- **Duplicate candidate warning**: creating a candidate checks phone
  (any formatting), email and name. Matches show who/what matched with a
  **View** link; "Create anyway — different person" proceeds. It warns,
  never blocks.

---

## 6. Leave — now self-healing

The monthly leave credit runs **daily by itself** (scheduler), repairs any
month it missed (up to 12 months back), and replays a missed 31-Dec
carry-forward. If a repair actually moved balances, HR + CEO get a
"Leave accrual repaired" email naming the months — check payslips issued
for those months.

Manual repair beyond 12 months:
```bat
cd F:\AI-Interview-Model-B-V2\backend
python scripts\run_pe_leave_credit.py --from 2025-01 --dry-run   :: preview
python scripts\run_pe_leave_credit.py --from 2025-01             :: apply
```

Watch **Settings → Operations**: an enabled job with no run in 48h shows red.

---

## 7. Customer form fixes (quick list)

City picks the State automatically · Add contact button sits below the list ·
Documents save, show on edit, and fit without scrolling (replace = delete +
re-add; date/type/status edits save) · Leave Billing Policy appears only when
Leave billable is ticked · Leave Name in the policy popup is locked once
picked (Change button to reselect; duplicate Name field removed).

---

## 8. If something looks wrong

| Symptom | First check |
| --- | --- |
| Button/tab missing for a user | `diagnose_access.py <user> --tab <tab>`, then template level + re-login |
| Save rejected: "view-only access to…" | Template field lock — intended; raise the field in the template if wrong |
| Leave balance looks short | Settings → Operations: is Monthly leave credit green? |
| No emails arriving | Users → Email flows (enabled? roles?), then Settings → Operations outbox stats |
| Timesheet bills wrong days | Customer/branch/project: billable flags + week-off days; regenerated next month |
| Reminder/PO notice sent twice | It can't — every scheduler mail has a one-time key; check the outbox to confirm |

Everything above is also in `CLAUDE.md` (technical) — this file is the
human version.
