# KARNEX — Production Readiness Runbook

Run these on your machine (where the files are intact). Paste any error back to me
and I'll fix it. I can't run these from my sandbox — its file-mount currently
serves truncated copies of many files, so any build/test there fails falsely.

## 1. Backend — migrations (apply this session's DB changes)

```bash
cd AI-Interview-Model-B-V2/backend
pip install -r requirements.txt          # pulls bcrypt (new), psycopg2, etc.
alembic upgrade head                      # applies 0010 (CEO+tab_access), 0011 (resume sha256), 0012 (opportunity details/version/ctc_slab)
alembic current                           # expect: 0012 (head)
```

New migrations to expect: `0010_ceo_role_and_tab_access`, `0011_resume_file_checksum`,
`0012_opportunity_details_ctc_slab`. `0010` also seeds **Karan → CEO, Vishal → Admin**.

## 2. Backend — tests

```bash
cd AI-Interview-Model-B-V2/backend
python -m pytest -q                       # full suite
# If you only want the pieces added this project:
python -m pytest tests/test_password_security.py tests/test_template_persistence.py \
  tests/test_opportunity_form_schema.py tests/test_resume_checksum.py \
  tests/test_ats_scoring.py tests/test_question_bank.py -q
```
Expected: green. If a test needs Postgres, point `CRM_DATABASE_URL` / `AUTH_DB_TARGET`
at a test DB first.

## 3. Frontend — typecheck, lint, build

```bash
cd AI-Interview-Model-F-V2/frontend/admin-dashboard
npm install
npx tsc --noEmit                          # typecheck (should be clean)
npm run lint                              # if a lint script exists
npm run build                             # production build — must be zero errors
```
If `tsc` flags anything in the files touched this project — `App.tsx`,
`crm/CrmApp.tsx`, `lib/rbac.ts`, `crm/pages/UsersAdmin.tsx`, `crm/pages/TemplateForm.tsx`,
`crm/pages/opportunity/opportunitySchema.ts`, `opportunityFormState.ts` — paste it and I'll fix it.

## 4. Prove no secret leaked into the client bundle

```bash
cd AI-Interview-Model-F-V2/frontend/admin-dashboard
grep -rEi "sk-[a-z0-9]{20}|OPENAI_API_KEY|AUTH_SECRET|BEGIN (RSA|PRIVATE)" dist/ || echo "clean: no secrets in bundle"
```

## 5. Outstanding production hardening (from the crypto audit — real risks)

- **Plain HTTP → HTTPS/TLS.** The app is served over HTTP (screenshots show `Not secure`).
  Terminate TLS at a reverse proxy (nginx/Caddy) or load balancer and redirect 80→443.
  HSTS header is already set by the app (`security_headers` middleware) and activates once served over TLS.
- **Secrets in env, not repo.** Confirm `AUTH_SECRET` / `REPORT_CODE` are strong and NOT the
  defaults (the app logs a warning if they are). Rotate anything ever committed.
- **AES-256-GCM at rest** for sensitive candidate PII — not yet implemented (documented follow-up).
- **Data retention / candidate delete** — policy + endpoint not yet implemented.

## 6. What was delivered and verified this project (safe to ship after 1–3 pass)

- Template data-loss root cause fixed (missing `GET /job/config/{id}` + save-guard) + tests.
- Password hashing → bcrypt with transparent legacy-pbkdf2 upgrade on login; constant-time compares; security headers; login rate-limit.
- Resume SHA-256 checksum + dedupe; opportunity attachment checksums.
- CEO super-role + per-user tab access (Admin/CEO editable).
- Opportunity form: declarative type-driven schema + server-side schema parity (rejects
  invalid-for-type fields), `details` JSONB storage, optimistic concurrency (`version`),
  partial-update merge (no field nulling), CTC-slab table with derived Revenue (Annual).
```
