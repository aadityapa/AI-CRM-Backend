# New Opportunity Form Rebuild — TASKS

Schema (frontend): `admin-dashboard/src/crm/pages/opportunity/opportunitySchema.ts`
Server parity: `backend/services/opportunity_form_schema.py`

## Flagged decisions (proceeding on recommendation; not blocking)
- **Opportunity ID**: system-generated (`next_sequence_number(...,"OPP")`) → read-only in the form.
- **Fixed Price === Work Package** structurally (screenshots identical).
- **Revenue (Annual) = Revenue (Monthly) × 12** → derived, read-only "calculated" cell.
- **Storage**: type-specific fields → `opportunity.details` JSONB; CTC slab → `opportunity_ctc_slab` table.
- **Dates**: store ISO/UTC, display dd-MMM-yyyy.
- **Full-screen**: dedicated overlay layer above CRM chrome (query-param router; a literal `opportunities/new` route can be added before `opportunities/:id`).

## P0 — Audit
- [x] Audited current modal, Opportunity model/schema/router, Customer master, UI tokens, router, deps
- [x] Confirmed: only Type/Skill-Eval/RFI-Value/attachments exist; T&M/Leave/Commercial/CTC-slab/Project-Scope are NEW

## P1 — Schema + renderer
- [x] Declarative form schema with per-type visibility (`opportunitySchema.ts`) — transforms clean
- [x] Per-type section/field matrix produced
- [x] Server-side schema parity (`opportunity_form_schema.py`) + tests (10 passed): rejects invalid-for-type fields, strips hidden
- [x] `<FormRenderer>` (`FormRenderer.tsx`) renders any section/field from the schema (transforms OK; bundle OK)
- [x] All 4 types render from schema alone (visibility via sectionVisible/fieldVisible; verified by state node test)

## P2 — Full-screen shell
- [x] Full-screen shell (`Modal fullScreen` + `NewOpportunityForm.tsx`): sticky header w/ progress, sticky footer (Submit / Save Draft / Reset), scroll body
- [x] Section nav with per-section status (empty/partial/complete/error) + scroll-to
- [x] Draft autosave GATED behind `isLoaded` (localStorage; restore on open)
- [x] Wired into Opportunities page (button opens the new form)
- [ ] Escape-close confirm on unsaved changes (Escape closes; confirm dialog TODO)

## P3 — Conditional logic
- [x] Per-type state preserved on switch-and-back (`opportunityFormState.ts`; node test: 9 asserts pass)
- [x] Hidden fields not submitted (client `stripHiddenFields`/`buildSubmitPayload` + server `strip_details`) — tested
- [x] Server rejects fields not valid for the type (validator + 10 tests)
- [x] Wire validator into POST/PUT (422 on invalid); `details` JSONB + `version` + `opportunity_ctc_slab` (migration 0012)
- [x] Partial update MERGES details (never nulls unsent keys); optimistic concurrency via `version` (409 on stale)
- [x] Revenue (Annual) derived server-side = Monthly × 12; serialize returns details/version/ctc_slab
- [ ] Type-switch ANIMATION (collapse/expand) — needs the renderer (P2)

## P4 — Inline customer creation
- [ ] "+" next to Customer → full-screen nested customer form (fields from Customer master: name required, etc.)
- [ ] Save → create, auto-select, refresh Branch/Contact/HiringManager, flash-highlight; cancel → no-op
- [ ] Opp form state survives nested form; duplicate name/email guard; same pattern for Branch/Contact/HM

## P5 — Guided flow
- [ ] Auto-advance focus on satisfy; next-field pulse; progressive SECTION disclosure; dependency-disable with reason
- [ ] Header progress bar (% required complete); `STRICT_SEQUENTIAL_MODE` flag (default OFF); reduced-motion

## P6 — Tables
- [ ] CTC Slab (derived Annual, currency/percent formatting), Skills, Attachments (drag-drop, **sha256** via `save_upload_hashed`)
- [ ] Add/remove spring, per-cell validation, Tab/Enter nav, drag-reorder FLIP, empty states

## P7 — Motion/theme/a11y
- [ ] Token-based motion; dark-mode contrast fix (low-contrast placeholders); WCAG AA; focus trap + return; aria

## P8/P9 — Tests + build
- [ ] Frontend component/interaction tests for the 4 types + guided flow + customer flow
- [ ] Backend: partial-update no-null, optimistic concurrency (version), 403 IDOR test
- [ ] typecheck/lint/build green; screenshots (4 types × desktop/mobile × light/dark)
