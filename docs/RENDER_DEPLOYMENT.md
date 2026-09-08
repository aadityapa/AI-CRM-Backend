# Deploy Backend on Render (Supabase database)

Repo: [AI-Interview-Model-Backend-v2](https://github.com/aadityapa/AI-Interview-Model-Backend-v2)

- **Backend:** Render Web Service (Docker)
- **Database:** [Supabase](https://supabase.com) PostgreSQL (not Render Postgres)
- **Frontend:** Vercel — `https://ai-interview-model-frontend-v2.vercel.app`

## 1. Render Web Service

1. [Render Dashboard](https://dashboard.render.com/) → **New** → **Web Service**
2. Connect `aadityapa/AI-Interview-Model-Backend-v2`
3. **Runtime:** Docker · **Health check:** `/health/live`
4. Do **not** add a Render PostgreSQL instance.

## 2. Supabase connection string

In Supabase: **Project Settings → Database → Connection string → URI** (Session pooler, port 5432).

Set on Render as `AUTH_DB_URL` (paste your full URI). Example shape:

```
postgresql://postgres.[PROJECT-REF]:[PASSWORD]@aws-0-[region].pooler.supabase.com:5432/postgres
```

`sslmode=require` is added automatically for cloud hosts. **Never commit this URL to GitHub.**

## 3. Required Render environment variables

| Variable | Value |
|----------|--------|
| `AUTH_DB_URL` | Your Supabase connection URI |
| `OPENAI_API_KEY` or per-purpose keys | Your OpenAI keys |
| `AUTH_SECRET` | Long random string (32+ chars) |
| `REPORT_CODE` | Random secret |
| `CORS_ALLOW_ORIGINS` | `https://ai-interview-model-frontend-v2.vercel.app` |
| `PUBLIC_BASE_URL` | `https://ai-interview-model-frontend-v2.vercel.app` |
| `ALLOW_PUBLIC_HR_REGISTRATION` | `true` (until first HR user exists) |
| `SMTP_ENABLED` | `false` |
| `UVICORN_WORKERS` | `1` |

## 4. Verify

```bash
curl https://YOUR-SERVICE.onrender.com/health/ready
```

Expect `"database_connected": true` and `"database_backend": "postgresql"`.

## 5. Frontend (Vercel)

API routes are proxied via `vercel.json` to your Render backend URL. No CORS changes needed in the browser when using the Vercel proxy.

## Notes

- Render free tier sleeps after inactivity (~30–60s cold start).
- SMTP is off by default; invite emails are skipped until you set `SMTP_ENABLED=true` and SMTP credentials.
- Use Supabase **Session pooler** (port 5432), not Transaction pooler, for this app.
