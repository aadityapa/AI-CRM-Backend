# Access Templates (department/role-wise tab + field permissions) — Implementation Handoff

ROLE
You are continuing the "Access Templates" module for KARNEX CRM. Admin/CEO create named
templates (Sales, RMG, TA, HR, …) that grant, per CRM tab and per field, a mode of
**View** or **Insert/Edit**, and assign them to users (LIVE link — editing a template updates
every assigned user). A per-user override still wins on top; Admin/CEO always have full access.
Do NOT rename fields or change the data shapes below. Keep the single `effective_access`
resolver as the one source of truth. Track progress in TASKS.md.

====================================================================
DATA MODEL (already implemented — do not change shapes)
====================================================================
- `access_templates` (models/access_templates.py): id, name (unique), description,
  department_id FK→departments, role (str tag), is_active,
  `tab_access` JSON = { "<tab_key>": "view" | "edit" },
  `field_access` JSON = { "<tab_key>": { "<field_key>": "view" | "edit" } }, timestamps.
- `user_profiles.access_template_id` FK→access_templates (the live link).
- Migration: **alembic 0029_access_templates** (creates table + adds the FK column).

====================================================================
ALREADY DONE (backend) — 13 tests passing
====================================================================
- Model + migration 0029 + `AccessTemplate` exported from models/__init__.
- `services/access_registry.py`: catalogue of grantable tabs (21) + fields per tab,
  MODES=(view,edit), `registry()` (for the editor), `validate_access()` (rejects unknown tab/field/mode).
- `services/access_templates.py`:
  - CRUD: `create/update/delete/list/get` (name-unique, registry-validated).
  - `assign_template(db, user_id, template_id|None)` (delete unlinks users).
  - `effective_access(db, user_id, roles)` → the ONE resolver:
    Admin/CEO → {full:true}; else template (live) + per-user legacy override (override wins per
    tab/field, granted as "edit"); `visible_tabs=None` means role-defaults (no restriction).
  - `can_edit_tab/can_view_tab` helpers.
- `routers/crm/access_templates.py` (Admin/CEO only, registered in register_crm_routers):
  `GET/POST /api/access-templates`, `GET/PUT/DELETE /{id}`, `GET /registry`, `POST /assign`.
- `routers/crm/me.py`: `/api/me` now returns `access` = { full, template_id, tabs:{tab:mode},
  fields:{tab:{field:mode}}, visible_tabs, source } (legacy tab_access/field_access kept).
- `crm_deps.require_access(tab, *, mode="view"|"edit", field=None)`: reusable FastAPI dependency
  that 403s unauthorized reads/writes using effective_access.
- Tests: tests/test_access_templates.py (9), tests/test_access_enforcement.py (4).

ALREADY DONE (frontend)
- `src/crm/pages/AccessTemplates.tsx`: template list + editor (per-tab & per-field
  No-access/View/Insert dropdowns from /registry), create/update/delete, assign-to-user.
- Route `access-templates` (routes.ts) + Admin-only nav item (CrmApp NAV) + an
  "Access Templates" button in the Users page header (UsersAdmin.tsx).
- Fixed the crmGet/crmPost unwrapping bug (responses are `{data,...}`; use `.data`).

====================================================================
TO IMPLEMENT / VERIFY (remaining work)
====================================================================
1) DEPLOY (one-time, on the server — required before anything is visible/works):
   - Backend: `cd backend && python -m alembic upgrade head` then RESTART the backend process
     (it does not hot-reload; the new endpoints/guard only load on restart).
   - Frontend: `npm run build` (or restart the Vite dev server) + hard refresh (Ctrl+Shift+R).

2) ENFORCE ON REAL WRITE ENDPOINTS (guard exists, not yet attached):
   Attach `Depends(require_access("<tab>", mode="edit"))` (and `mode="view"` on read endpoints)
   to the actual CRUD routers you want gated (customers, opportunities, projects, timesheets,
   invoices, pos, employees, …). Currently only demo endpoints are guarded (in tests). Do this
   incrementally; role-default users (no template) are unaffected.

3) FRONTEND UI GATING from `/api/me`.access:
   - Nav already filters by the legacy `tab_access` list. Extend the app to also read
     `me.access.tabs` / `me.access.fields` and: hide tabs with no access, render pages as
     read-only when mode=="view", and disable/omit inputs whose field mode is "view" or absent.
   - There is no client-side field-level enforcement yet — add it (server is the real gate).

4) PER-USER ASSIGN IN THE USERS TABLE (nice-to-have):
   Add an "Access Template" dropdown/column per user row in UsersAdmin.tsx that calls
   `POST /api/access-templates/assign` — so assigning doesn't require opening the manager page.

5) REGISTRY COVERAGE:
   `services/access_registry.py` currently lists 21 tabs and a curated set of fields for the main
   tabs. Expand `FIELDS_BY_TAB` to every field you actually want grantable (keys must match what
   the pages/serializers use).

6) RECONCILE WITH THE EXISTING PER-USER "Edit Tab Access" MODAL:
   The Users page already has a per-user tab/field access modal (legacy list format). Decide the
   UX: keep both (template = default, per-user = override, which is the current resolver behavior)
   and make the modal show the effective/merged view, or migrate the modal to write modes too.

7) OPTIONAL — DEPARTMENT AUTO-ASSIGN:
   If desired, auto-assign a user's template from their department (access_templates.department_id
   ↔ user's department) at user-create time.

8) TESTS TO ADD:
   - Wire-in tests once real endpoints are guarded (allowed/blocked per template).
   - Browser/E2E (Playwright) for the editor + gating (not covered — sandbox can't build the FE).

CONSTRAINTS
- Keep the single `effective_access` resolver everywhere access is decided (server + `/me`).
- Do not change the JSON shapes of tab_access/field_access or the "view"/"edit" mode strings.
- Admin/CEO must always resolve to full access.
