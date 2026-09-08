# Nexus (Zoho Creator) vs Karnex CRM — Flow Comparison & Gap Analysis

Scanned: https://creatorapp.zoho.in/karnex_internal_app/nexus-karnex-internal-app/ (live walk of
nav tree, TA Dashboard, Careers page, Opportunity form, Candidate Profile form) — 2026-07-12.

## Verdict

Same objective, same core flow. Nexus is the reference app Karnex CRM was specified from, and the
spine matches end to end: Customer → Opportunity (Sales → Sales Head approval → RMG → TA) →
Candidate Profile pipeline → Project → Timesheet → PO → Invoice → Payments/TDS, with the same
master data (leave names, policy types, holidays, financial/calendar years, branches). The
Opportunity form is a near field-for-field match (Customer Details with CP/HM autofill, RFI,
Leave & Holiday, Commercial, Work Page, Attachments, Skill Evaluation, Activity Histories).

Karnex is AHEAD on: AI interviews + ATS scoring + auto-invite + public slot booking, real RBAC with
per-tab/field access, timesheet→invoice generation with the comp-off engine, GST/TDS computation +
PDF invoices, credit notes, PO activity log/attachments/expiry reports, leave applications with
accrual audit events, and the new design system UI. Nexus has none of the AI layer.

## Where Karnex lacks vs Nexus (gaps found)

1. CANDIDATE PIPELINE DEPTH — Nexus tracks 10 forward stages: Sourcing → Technical Screening →
   Technical Interviewing → Sales Screening → Customer Screening → Customer Interviewing →
   Customer Shortlisted → Customer Approval → Preboarding → Joined, plus granular terminal states
   (Sales Rejected, RMG Rejected, Customer Rejected, Self Withdrawn, HR Screening,
   L1-Technical Scheduled, Project Allocation Pending). Karnex has a shorter status list and no
   CUSTOMER-side stages, no Preboarding/Joined, no Self-Withdrawn tracking. This matters for a
   staffing business: the client-interview half of the funnel is invisible in Karnex today.

2. PUBLIC CAREERS PAGE + APPLY INTAKE — Nexus has a Careers page (Job ID, title, exp range,
   location, job type, JD, Apply) feeding a Job Seekers table via a JD Form repository. Karnex has
   internal job postings on requirements but no public listing/apply funnel into Candidates.

3. TA DASHBOARD (LANDING) — Nexus lands TA users on a position × stage count matrix with
   drill-down links, a candidate-status distribution, and a PER-TA PRODUCTIVITY table
   (profiles processed today / yesterday / last 7 days). Karnex's TA dashboard widgets are
   lighter and have no recruiter-productivity metrics.

4. OFFER LETTERS MODULE — Nexus manages offer letters as first-class records. Karnex tracks offer
   history (amounts/status) but does not generate or store offer letter documents.

5. ROLE-SCOPED SAVED VIEWS — Nexus exposes each workflow state as a named nav report per role
   (Sales Hold, Sales Rejected, Acceptance Pending — TA, Approval Pending — RMG, Closed — TA...).
   Karnex covers this with tabs+filters on one page; users coming from Nexus will miss one-click
   state views. (Cheap fix: pinned/saved filter presets per role.)

6. CANDIDATE PROFILE EXTRAS — Nexus profile form has: dual skill ratings (Self Rating vs RMG
   Rating per skill), threaded profile Comments (separate from activity history), and Separation
   Details on the CANDIDATE (notice period, last working day, resignation certificate upload) for
   lateral hires. Karnex has skill evaluations + activity log but no self-vs-RMG split, no
   comment thread, and separation data lives only on Employees.

7. ANALYTICS PAGES — Nexus has extra dashboards: Development Dashboard, RMG Active Dashboard,
   Sales Head Dashboard, and a "this year" annual report. Karnex has one role-aware dashboard;
   no yearly rollup view.

8. PROJECT PO USAGE FORM — Nexus has a dedicated PO-usage-per-project entry. Karnex covers this
   via PO project allocations + consumption (parity in data, different entry ergonomics).

## Suggested priorities

P1 (business-visible, low-medium effort)
- Extend candidate_profile pipeline statuses to the full Nexus set incl. customer stages,
  Preboarding, Joined, Self Withdrawn (enum + pills + transition rules; keep existing statuses).
- TA dashboard parity: position × stage matrix + per-TA productivity (counts by updated_by/day).
- Saved role views: preset filter chips ("Acceptance Pending", "On Hold", "Rejected") per role.

P2 (new modules)
- Public Careers page + Apply form → creates Candidate with resume upload (feeds existing ATS
  scoring automatically — Karnex would instantly out-do Nexus here since applicants get AI
  screening).
- Offer letter generation (template + PDF, reuse invoice_pdf pattern) linked to OfferHistory.

P3 (refinements)
- Self vs RMG skill rating columns on profile skill evaluations.
- Profile comment thread (reuse activity-log table with a "Comment" action type + UI thread).
- Candidate separation details (notice period, LWD, resignation certificate) on profiles.
- Annual report page (FY rollup: revenue, joins, closures).

## Bottom line

Objectives are identical; Karnex already implements ~90% of Nexus's flow and exceeds it on AI,
finance automation, and access control. The real gaps are the customer-facing half of the
candidate pipeline, the public hiring funnel, offer letters, and TA productivity analytics.
