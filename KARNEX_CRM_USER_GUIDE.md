# Karnex CRM — Role-wise User Guide

Everything happens at `https://<your-ip>:2020/admin` → **CRM** nav item. A user's
sidebar and permissions come from their assigned CRM role. Roles are assigned by an
Admin in **CRM → Users**, or via CLI:

```bat
cd AI-Interview-Model-B-V2\backend
python assign_crm_role.py <username> <Role>
```

Every rule below is enforced by the server (API-level RBAC) — the UI only mirrors it,
so nothing can be bypassed from the browser.

---

## The workflow at a glance

```
Sales creates Requirement → Sales Head approves → RMG (Engineering) approves
→ TA sources (job portals → resumes → ATS scan → shortlist → AI L1 interview)
→ Candidate pipeline (screening stages → offer → Joined)
→ Project staffing → Timesheets → PO → Invoice → Payment / TDS
```

---

## Roles and access

| Role | Sidebar | Core responsibilities |
|---|---|---|
| **Admin** | Everything | Users & roles, Settings (master data, AI pass threshold), full access to all modules |
| **Sales** | Dashboard, Opportunities, My Requirements, Candidate Profiles, Projects, Reports | Customers & opportunities, create/submit requirements, sales-stage candidate approvals |
| **Sales Head** | Dashboard, Opportunities, Requirement Approvals, Candidate Profiles, Projects, Reports | Requirement approval (stage 1), executive dashboard, archive/close, late pipeline stages |
| **RMG** | Dashboard, Opportunities, Engineering Review Queue, Candidate Profiles, Reports | Engineering review (stage 2), RMG pipeline stage, pipeline-volume dashboard |
| **TA** | Dashboard, Requirements (sourcing view), Candidates, Candidate Profiles, Reports | Job postings, resume upload, ATS, shortlisting, AI L1 scheduling, early pipeline stages |
| **HR** | Dashboard, Employees, Timesheets, Reports | Employee records, leave balances, timesheet approval, Preboarding → Joined |
| **Finance** | Dashboard, Purchase Orders, Invoices, TDS, Reports | POs, invoices, payments, TDS, project billing, finance dashboard |

---

## Role-by-role walkthrough

### Admin — set up first
1. **Settings** → verify master data (departments, designations, skills, locations,
   currencies, document types, leave types) and the AI interview pass threshold
   (default 60%).
2. **Users** → create users for your team and assign roles (Create User → tick roles).
   Toggle portal access; deactivate leavers.
3. Admin implicitly passes every permission check and sees every module.

### Sales
1. **Customers** (write access shared with Sales Head): create the customer with
   branches, billing policy (this drives invoicing!), and contact persons.
2. **Opportunities** → New Opportunity (auto `OPP-2026-NNN`), add skills, move stages:
   New → Active → Closed Won / Closed Lost (On Hold / Rejected available; Archived is
   Sales Head only).
3. **My Requirements** → New Requirement against an opportunity (positions, experience
   range, budget CTC, skills with mandatory flags + min ratings) → **Submit**.
   - You see only your own requirements.
   - You can edit only in Draft or after a rejection (then resubmit).
4. Pipeline duty: approve candidates at **Sales Screening** and **Customer Screening**;
   with Sales Head, drive Customer Interview → Shortlisted → Customer Approval; record
   offers.

### Sales Head
1. **Requirement Approvals** → queue of `Pending Sales Head Approval` with
   Approve / Reject buttons. Rejection requires a reason (min 10 characters) and sends
   it back to Sales.
2. Executive dashboard: headcount, customers, projects, opportunity funnel, quarterly
   hiring/revenue matrix, requirement funnel.
3. Extra powers: archive opportunities, Close/Cancel requirements, create projects,
   late-stage pipeline moves, offer approvals.

### RMG (Engineering)
1. **Engineering Review Queue** → requirements approved by Sales Head, awaiting
   technical review. Approve → requirement becomes `Open For Sourcing` and **only then**
   visible to TA. Reject (≥10-char reason) → back to Sales.
2. Pipeline duty: the **RMG Review** stage (approve → Sales Screening, or RMG Reject).
3. Dashboard: pipeline volume per open position.

### TA (Recruiter)
You cannot see any requirement before RMG approval (server returns 404 — drafts don't
exist for you). On an approved requirement's detail page:
1. **Job Postings tab** → add portal + URL (Naukri/LinkedIn/Indeed/Other). First
   posting auto-moves status → `Posted On Portals`.
2. **Resumes tab**:
   - **Upload Resume** (file + name/email/phone/source). First resume auto-moves the
     requirement → `In Progress`.
   - **Run ATS Scan** (per resume or Scan All) → score /100, colour-coded
     (≥70 green, 40–69 amber, <40 red). Click the score for the full breakdown
     (matched skills green, missing red, experience & education points).
   - **Shortlist** (only possible after scoring) or Reject.
   - **Schedule AI L1 Interview** on a shortlisted resume → auto-creates the candidate
     + pipeline profile, schedules a real AI interview session, and gives you the
     invite link + access key to share with the candidate.
3. When the interview completes you're notified; the resume flips to Passed/Failed
   (vs the configured threshold) and per-skill scores appear in the profile's skill
   grid automatically.
4. Pipeline duty: **Sourcing → Technical Screening** moves.
5. Dashboard: open requirements, resumes pending scan, interview queue, recruiter
   productivity.

### HR
1. **Employees** → create employee records (department, designation, reporting lines,
   PAN/Aadhar, bank details). Leave Balances tab: accrued/consumed/carry-forward per
   leave type per year — balance is always recomputed server-side.
2. **Timesheets** → review submitted timesheets: Approve, or Reject with a reason
   (≥10 chars). The daily grid computes billable hours/days from the customer's
   billing policy automatically; the footer shows rollups (working days, comp-off,
   leave, holidays, billable totals).
3. Pipeline duty: **Preboarding → Joined**; offer records.

### Finance
1. **Purchase Orders** → New PO: pick customer/branch/contact, enter tax slab % —
   SGST/CGST/IGST auto-split (intra vs inter-state). Allocate amounts to projects, or
   use the one-click **Create PO** button on a project page.
2. **Invoices** → raise against a PO (PO consumed/balance auto-update; blocked if the
   PO balance is insufficient). Then one-click:
   - **Generate PDF** → downloadable invoice PDF
   - **Record Payment** → paid/balance/status auto-update (Unpaid → Partially Paid → Paid)
   - **Record TDS** / **TDS Payment** → TDS ledger auto-updates
3. **TDS** page: compliance ledger of all TDS records with status filters.
4. Dashboard: outstanding invoices, TDS pending, PO consumption bars.

---

## Candidate pipeline — stages and who moves them

```
Sourcing → Technical_Screening → RMG_Review → Sales_Screening → Customer_Screening
→ Customer_Interview → Shortlisted → Customer_Approval → Preboarding → Joined
```

| Stage being moved | Who can move it |
|---|---|
| Sourcing, Technical Screening | TA |
| RMG Review | RMG |
| Sales Screening, Customer Screening | Sales |
| Customer Interview, Shortlisted, Customer Approval | Sales or Sales Head |
| Preboarding | HR or Sales Head |
| Any stage | Admin |

Exit states: `Sales_Rejected`, `RMG_Rejected`, `Customer_Rejected`, `Self_Withdrawn`,
`Rejected` — reachable from the relevant stages. `Joined` and all rejections are
terminal. Every transition requires a comment and is written to the activity timeline.
The UI's status dropdown only ever offers moves *your* role is allowed to make.

---

## Key access rules (quick reference)

- Only **Sales** creates requirements; only **Sales Head** approves stage 1; only
  **RMG** approves stage 2.
- **TA cannot see** requirements before Engineering approval — not even that they exist.
- Rejections (requirements, timesheets) always require a reason of at least 10 characters.
- Shortlisting a resume requires an ATS score first.
- Invoices cannot exceed the PO balance; all PO/invoice/TDS/leave balances are
  server-computed.
- Deactivated users cannot log in to the CRM at all.

---

*See `KARNEX_CRM_README.md` for setup, environment variables, migrations, network
access, and the automated verification suite.*
