# Deployment guide — Karnex AI HR Suite

Written for an India-based company serving India-based users. Latency is decided
mostly by **where the database sits relative to the API**, not by how much you
spend, so that is the decision this guide organises everything around.

---

## 1. Do these before you deploy anything

### Rotate the credentials that have been exposed

`.env` is correctly gitignored and was never committed — but it has been read by
tooling, so treat all of it as compromised:

| Credential | Where | Action |
|---|---|---|
| OpenAI API keys (4) | `.env` | Revoke at platform.openai.com, issue new ones |
| Office 365 mailbox password | `.env` `SMTP_PASSWORD` | Reset; prefer an app password on a dedicated no-reply mailbox |
| Postgres password | `.env`, `docker-compose.yml` | Change; stop using `root` |
| `AUTH_SECRET` | now generated | Already set — rotating signs everyone out, which is intended |

### Apply the migrations

```
cd backend
python -m alembic upgrade head        # through 0061
```

### Set these in the production environment

```
KARNEX_ENV=production          # turns on the production guards
AUTH_SECRET=<48+ random bytes> # the app now REFUSES to start without it
CORS_ALLOW_ORIGINS=https://crm.karnex.in
ALLOW_PUBLIC_HR_REGISTRATION=false
PUBLIC_BASE_URL=https://crm.karnex.in
MAX_UPLOAD_BYTES=20971520
```

`PUBLIC_BASE_URL` currently points at a LAN IP. Interview invite links are built
from it, so external candidates get an unreachable URL until this is a public
hostname.

---

## 2. Recommended architecture

```
                    Cloudflare (DNS + CDN + WAF)
                              │
        ┌─────────────────────┴─────────────────────┐
        │                                           │
   Static frontend                            FastAPI backend
   (dist/ on a CDN)                        (containers, 2+ instances)
   Vercel / Cloudflare Pages                Render / Railway / AWS ECS
        │                                           │
        └──────────── /api/* proxied ───────────────┤
                                                    │
                              ┌─────────────────────┼──────────────────┐
                              │                     │                  │
                       Postgres 16            Object storage      Redis
                    (managed, Mumbai)         (S3 / R2)          (cache,
                    + PITR backups            for CV files       rate limit)
```

### Region is the whole game

Put **every** tier in **Mumbai (`ap-south-1`)**. Users in Bangalore, Pune and
Hyderabad get 20–40 ms to Mumbai versus 200–250 ms to Virginia. Your list
endpoints make several queries per request, so each round trip is multiplied —
an API in Singapore with a database in Mumbai will feel slow no matter how fast
the code is.

**The single most important rule: the API and the database must be in the same
region, ideally the same availability zone.**

---

## 3. Concrete options

### Frontend — static `dist/`, so this is easy and cheap

| Option | Why |
|---|---|
| **Cloudflare Pages** (recommended) | Free tier is generous, has Indian PoPs, unlimited bandwidth |
| **Vercel** | Best DX, already have `vercel.json`; free tier fine, Pro if you need more |
| **S3 + CloudFront** | If you standardise on AWS |

The frontend is already a static build with lazy-loaded routes, so a CDN serves
it in ~20 ms. Keep `Cache-Control: public, max-age=31536000, immutable` on
`/assets/*` (already set in `vercel.json`) and no-cache on `index.html`.

### Backend — needs to be in Mumbai

| Option | Notes |
|---|---|
| **Render** (Singapore only) | Simplest, but no Indian region — adds ~60 ms. Acceptable to start; revisit at scale |
| **Railway** | Similar trade-off |
| **AWS ECS Fargate / App Runner, `ap-south-1`** (recommended for MNC scale) | Real Mumbai region, autoscaling, VPC-private database |
| **Azure Container Apps, Central India** | If you are already on Microsoft 365 |
| **A Mumbai VPS** (Hetzner has no India; DigitalOcean Bangalore, E2E Networks) | Cheapest, but you own patching and uptime |

Run **at least 2 instances** behind a load balancer. The app is stateless apart
from uploads (see below), so this works once files move to object storage.

### Database — managed, Mumbai, with backups

| Option | Notes |
|---|---|
| **AWS RDS Postgres, `ap-south-1`** (recommended) | Multi-AZ, automated backups, point-in-time recovery |
| **Azure Database for Postgres, Central India** | Equivalent |
| **Neon / Supabase** | Fast to start; check the region is Mumbai before committing |

Non-negotiables: **automated daily backups with PITR**, a **private subnet** (the
database must never have a public IP), and **connection pooling** — PgBouncer or
RDS Proxy. FastAPI with several workers opens a lot of connections.

### File storage — move uploads off the container disk

This is the change that matters most for multi-instance deployment. Today
`save_upload` writes to `data/crm_uploads` on local disk, so:

- two instances do not see each other's uploads
- a container restart loses every CV

Move to **S3 (`ap-south-1`) or Cloudflare R2**, serving via pre-signed URLs.
`services/crm_common.py` is the only place that writes, and
`routers/crm/files.py` the only place that reads, so this is a contained change.
Until then you must run a **single instance with a persistent volume**.

---

## 4. What makes it fast

Already done in this codebase:

- **Route-level code splitting** — all 37 CRM routes and every page lazy-loaded
- **Vendor chunking** — react/motion/icons/charts/pdf split; heavy parsers
  (pdf.js 334 kB, mammoth 498 kB) load only when a file is previewed
- **Immutable asset caching** with hashed filenames
- **Batched list serialisation** — the profiles list loads candidates,
  opportunities, customers, CTC slabs and latest interviews in one query each
- **Indexes for every hot filter and sort** (migration 0061), including
  `pg_trgm` GIN for candidate search — that search was a full scan of 6,700 rows
  evaluating a concatenation per row, twice per request
- **The `COUNT(*)` on every list request** now skipped when page 1 answers it

Worth doing next, in order:

1. **Redis** for the API response cache and real rate limiting.
2. **`LazyMotion` for framer-motion** — it is eagerly loaded at 126 kB and is the
   biggest remaining first-paint win (~100 kB).
3. **Cache master lists** (skills, customers, locations) client-side. They are
   refetched on every page mount today.
4. **Paginate the report endpoints** — `candidate_profiles_report` selects all
   8,000 rows with no limit.
5. **Batch the timesheet report N+1s** — roughly 22 queries per row, unbounded.
6. **Split the mega-pages** — `Requirements.tsx` is 3,330 lines and 83 kB.

---

## 5. Still open before you would call this production-hardened

These came out of the audit and are **not** fixed:

| Issue | Risk |
|---|---|
| **Rate limiting is inert** — `slowapi` is not in `requirements.txt`, and the decorators are evaluated before setup runs | Unlimited OpenAI-backed calls (`/candidate/tts`, `/answer`) = billing DoS; unlimited `/auth/forgot-password` = mail bombing |
| **Public apply form has no size cap enforced** | `_MAX_RESUME_BYTES` is defined and never used. Now partly mitigated by the global `MAX_UPLOAD_BYTES`, but add a per-IP limit |
| **`/` serves the whole frontend working tree** | `main.py` mounts `FRONTEND_DIR` which contains `src/`, `package.json`, `vite.config.ts`. Mount only `dist/` |
| **CORS falls back to any-IPv4 regex** | Fixed by setting `CORS_ALLOW_ORIGINS`; the fallback should also fail closed |
| **`_is_local_request` trusts `request.client.host`** | Behind a reverse proxy every request looks local, exposing `/admin/hr-code/rotate` |
| **`docker-compose.yml` has a hardcoded DB password and publishes 5432** | Use `${POSTGRES_PASSWORD:?}` and bind `127.0.0.1:5432:5432` |
| **Mammoth HTML rendered via `dangerouslySetInnerHTML`** | A crafted .docx from the public apply form can carry a `javascript:` link. Sanitise with DOMPurify |
| **`frontend/js/hr.js` interpolates candidate data into `innerHTML`** | The file's own `escapeHtml` helper was not used on four fields |
| **No token revocation** | `/auth/logout` is a stub; a stolen token stays valid up to 8 hours |

---

## 6. Suggested rollout

1. Rotate every credential above.
2. Provision Postgres in Mumbai, restore a dump, run `alembic upgrade head`.
3. Move uploads to S3/R2 (or accept single-instance for now).
4. Deploy the backend with the production env vars; confirm it **refuses to
   start** without `AUTH_SECRET` — that is the new guard working.
5. Deploy the frontend to a CDN; point `/api/*` at the backend.
6. Add Redis, then turn on real rate limiting.
7. Add uptime monitoring and error tracking (Sentry) before real users arrive.
8. Load-test the profiles list and the dashboards — those are the heaviest.

---

## 7. Honest scope note

I have fixed the issues that would have made this unsafe to expose: the forged-token
hole, public HR self-registration, the credential-leaking snapshot endpoint, the
avatar stored-XSS, unbounded uploads, inline serving of user-supplied files, plus
a latent `NameError` in the template-prompt path. I have added the indexes and
removed the per-request `COUNT(*)`.

Section 5 is deliberately a list rather than a claim of completion. "All future
bugs" is not something any audit can deliver — what this gives you is the current
known set, fixed where fixing was safe to do without your sign-off, and named
where it was not.
