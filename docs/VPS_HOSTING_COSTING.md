# Self-hosted VPS costing — Karnex AI HR Suite

**Scope:** everything runs on VPS infrastructure we control. No Vercel, no Render,
no managed database, no PaaS. Backend, frontend, Postgres, Redis, reverse proxy
and TLS all live on servers we rent and administer.

Prices are **August 2026 list prices**. USD converted at **₹95.45/$**
(RBI reference range for the week was 94.89–95.45). GST at 18% is shown as a
separate line because a GST-registered entity reclaims it as input credit — the
true cost to the business is the pre-GST figure.

---

## 1. What we are sizing for

Measured from the repository and database, not estimated:

| Metric | Value |
|---|---|
| Candidates | 6,719 |
| Candidate profiles | 8,101 |
| Interview rounds | 3,589 |
| Rows across main tables | ~45,000 |
| Postgres data size | ~100 MB today → 5–10 GB over 3 years with audit logs |
| Uploaded files (`data/crm_uploads`) | 421 MB local working set; ~1.5 GB in production |
| Internal users | 10–50 across Sales, TA, RMG, HR, Finance, Admin |
| Concurrency | Low. Recruiters clicking, not batch processing |

**This is a small workload.** The honest engineering read is that a single
well-configured server carries it with room to spare. The bottleneck is OpenAI
round-trip latency and network distance to the user, not CPU or disk.

### What actually runs on the box

| Component | Resource profile |
|---|---|
| FastAPI + Uvicorn (`backend/main.py`) | 1 worker today — sessions are in-memory. ~400 MB RSS |
| PostgreSQL 16 | 1.5–2 GB with sensible `shared_buffers` |
| Redis 7 | 256–512 MB, used for response cache and rate limiting |
| Caddy (reverse proxy + TLS + static frontend) | ~50 MB |
| PDF generation (WeasyPrint / ReportLab) | Bursty, up to 500 MB per concurrent render |

**4 GB is the practical floor. 8 GB is the comfortable number** once WeasyPrint
invoice rendering and Postgres are competing for the same RAM.

---

## 2. Architecture — fully self-hosted

```
                        Internet
                            │
                    DNS (registrar)
                            │
        ┌───────────────────▼───────────────────┐
        │              VPS (Bangalore)          │
        │                                       │
        │   Caddy  ── TLS via Let's Encrypt     │
        │     ├── /            → frontend dist/ │
        │     └── /api/*       → FastAPI :2020  │
        │                                       │
        │   Docker Compose                      │
        │     ├── ai-interview  (FastAPI)       │
        │     ├── postgres:16   (volume)        │
        │     └── redis:7       (volume)        │
        │                                       │
        │   /var/lib/karnex/uploads  (CV files) │
        └───────────────────┬───────────────────┘
                            │ nightly pg_dump + rsync
                            ▼
                  Object storage (offsite backup)
```

The frontend is a static build. Caddy serves it directly from disk with
immutable cache headers — no separate hosting product, no separate bill.

**One deliberate exception to "no external services": offsite backup storage.**
Backups that live on the same server as the data are not backups. This is the
one ~₹500/month line we should not remove.

---

## 3. Costing — three tiers

### Tier A — Lean (proving it in production)

Single server, everything co-located. Right for the first 2–3 months while real
usage is being observed.

| Line item | Spec | USD/mo | ₹/mo |
|---|---|---:|---:|
| VPS | 2 vCPU, 4 GB RAM, 80 GB SSD, 4 TB transfer | 24.00 | 2,291 |
| Automated backups | Weekly, 20% of server cost | 4.80 | 458 |
| Object storage | 250 GB offsite backup target | 5.00 | 477 |
| Cloud firewall | Included | 0.00 | 0 |
| TLS certificates | Let's Encrypt, auto-renewing | 0.00 | 0 |
| **Subtotal (pre-GST)** | | **33.80** | **3,226** |
| GST 18% (reclaimable) | | | 581 |
| **Total** | | | **₹3,807/mo** |
| **Annual** | | | **₹45,683** |

Trade-off: weekly backups mean up to 7 days of data loss in a disaster. Fine
while we are pre-production; not fine once the recruitment pipeline is the
system of record.

---

### Tier B — Go-live (recommended)

Same single-server shape, correctly sized, with daily backups.

| Line item | Spec | USD/mo | ₹/mo |
|---|---|---:|---:|
| VPS | 4 vCPU, 8 GB RAM, 160 GB SSD, 5 TB transfer | 48.00 | 4,582 |
| Automated backups | Daily, 30% of server cost | 14.40 | 1,374 |
| Object storage | 250 GB offsite backup target | 5.00 | 477 |
| Cloud firewall | Included | 0.00 | 0 |
| TLS certificates | Let's Encrypt | 0.00 | 0 |
| Monitoring | Uptime Kuma, self-hosted on the same box | 0.00 | 0 |
| **Subtotal (pre-GST)** | | **67.40** | **6,433** |
| GST 18% (reclaimable) | | | 1,158 |
| **Total** | | | **₹7,591/mo** |
| **Annual** | | | **₹91,096** |

**This is the recommendation.** 8 GB removes the memory contention between
Postgres and PDF rendering, 160 GB of SSD holds the database and every CV with
100× headroom, and daily backups cap data loss at 24 hours.

---

### Tier C — High availability (when it becomes business-critical)

Two application servers behind a load balancer, database on its own machine.

| Line item | Spec | USD/mo | ₹/mo |
|---|---|---:|---:|
| App servers | 2 × (2 vCPU, 4 GB) | 48.00 | 4,582 |
| Database server | 4 vCPU, 8 GB, private network only | 48.00 | 4,582 |
| Load balancer | Managed, TLS termination | 12.00 | 1,145 |
| Block storage | 100 GB, shared uploads volume | 10.00 | 954 |
| Automated backups | Daily, all three servers | 28.80 | 2,749 |
| Object storage | 250 GB | 5.00 | 477 |
| **Subtotal (pre-GST)** | | **151.80** | **14,489** |
| GST 18% (reclaimable) | | | 2,608 |
| **Total** | | | **₹17,097/mo** |
| **Annual** | | | **₹2,05,169** |

**Do not buy this yet.** Two prerequisites block it in code, not in budget:

1. **Sessions are in-memory.** `UVICORN_WORKERS=1` with the comment
   *"In-memory sessions require a single worker until REDIS_URL backs session
   state."* Two app servers means a candidate mid-interview gets logged out
   whenever the load balancer routes them to the other box.
2. **Uploads are on local disk.** `save_upload` writes to `data/crm_uploads`.
   Two servers do not see each other's files.

Both are contained fixes — session state to Redis, uploads to a shared volume or
object storage — but until they land, Tier C buys nothing over Tier B.

---

## 4. OpenAI API — derived per unit, not guessed

The application uses `gpt-4o-mini` for question generation and evaluation,
`gpt-4o-mini-tts` for spoken questions, and `gpt-4o-mini-transcribe` for
candidate answers.

**Per interview (6 questions, ~1 minute per spoken answer):**

| Stage | Model | Cost | ₹ |
|---|---|---:|---:|
| Question generation | gpt-4o-mini (2k in / 1k out) | $0.0009 | 0.09 |
| Text-to-speech | gpt-4o-mini-tts, ~3 min audio | $0.0450 | 4.30 |
| Transcription | gpt-4o-mini-transcribe, ~6 min | $0.0180 | 1.72 |
| Evaluation | gpt-4o-mini (8k in / 2k out) | $0.0024 | 0.23 |
| **Total per interview** | | **$0.0663** | **₹6.33** |

**Monthly, by volume:**

| Interviews/month | ₹/month |
|---:|---:|
| 100 | 633 |
| 200 | 1,266 |
| 500 | 3,164 |

Resume parsing and ATS scoring cost **₹0.13 per resume** — 500 resumes a month
is ₹64. Not a line worth managing.

**Budget ₹1,500–4,000/month** for OpenAI at realistic volumes. Two caveats:

- TTS dominates at 68% of per-interview cost. The existing response cache
  (`OPENAI_RESPONSE_CACHE_TTL_S=86400`) already deduplicates repeated questions —
  keep it on; it is doing real work.
- These figures assume the endpoints are rate-limited. They are not, today.
  See section 7.

---

## 5. Everything else

| Item | Frequency | Cost |
|---|---|---|
| Domain (`.in`) | Annual | ₹1,000–1,200/yr (≈ ₹100/mo) |
| TLS certificates | — | ₹0 — Let's Encrypt via Caddy, auto-renews |
| Email delivery | — | ₹0 incremental — existing Office 365 tenant via SMTP |
| Uptime monitoring | — | ₹0 — Uptime Kuma self-hosted, or Better Stack free tier |
| Error tracking | — | ₹0 — Sentry free tier covers 5,000 events/month |
| DNS hosting | — | ₹0 — registrar's nameservers are sufficient |
| Log retention | — | ₹0 — `journald` + logrotate on the box |

**One-time setup:**

| Item | Cost |
|---|---|
| Server provisioning, hardening, Docker/Caddy setup | 8–16 engineer-hours in-house, or ₹15,000–40,000 contracted |
| Data migration (pg dump/restore + 421 MB of uploads) | Included above |
| Domain registration | ₹1,000 |

**Ongoing operational effort:** roughly **2–4 hours/month** — OS patching
(automate with `unattended-upgrades`), backup restore drills, certificate
renewal is automatic, log review.

---

## 6. Bottom line

| | Tier A Lean | **Tier B Go-live** | Tier C HA |
|---|---:|---:|---:|
| Infrastructure (incl GST) | ₹3,807 | **₹7,591** | ₹17,097 |
| OpenAI (200 interviews/mo) | ₹1,266 | **₹1,266** | ₹1,266 |
| Domain (amortised) | ₹100 | **₹100** | ₹100 |
| **Total monthly** | **₹5,173** | **₹8,957** | **₹18,463** |
| **Total annual** | **₹62,076** | **₹1,07,484** | **₹2,21,556** |
| Pre-GST annual (true cost) | ₹55,104 | ₹93,588 | ₹1,90,260 |

**Recommendation: Tier B at ₹8,957/month, ≈ ₹1.07 lakh/year all-in.**

For context, the equivalent AWS build (EC2 + RDS Multi-AZ + ALB + NAT Gateway,
`ap-south-1`) prices at roughly ₹31,600/month. The VPS route delivers the same
capability for this workload at **28% of the cost**, and the difference is
almost entirely the managed-service premium on RDS and the NAT Gateway — neither
of which buys us anything at 45,000 rows.

### Provider options, Bangalore/Mumbai

| Provider | 4 vCPU / 8 GB | Notes |
|---|---:|---|
| **DigitalOcean, Bangalore** | $48 (₹4,582) | Recommended. Clean pricing, good docs, free firewall, real Indian region |
| AWS Lightsail, Mumbai | $44 (₹4,200) — but 2 vCPU, not 4 | Flat predictable bill and an AWS migration path. Note Mumbai gets **half** the bundled transfer other regions do |
| Vultr, Mumbai/Delhi | ~$48 (₹4,582) | Equivalent; three Indian locations |
| Hostinger VPS KVM 2, India | ₹899 | Far cheaper, but shared/oversubscribed CPU and renewal pricing jumps. Viable for Tier A only |
| E2E Networks, Mumbai | ₹3,000–6,000 | Indian entity — simpler GST and INR invoicing. Prices rose July 2026 |

Hostinger at ₹899 is genuinely tempting and worth a serious look if budget is
the binding constraint — the caveat is that burstable shared-CPU plans behave
unpredictably under WeasyPrint PDF rendering, and the advertised rate is an
introductory one. Price the renewal, not the promo.

---

## 7. Two things that must be fixed before this is internet-facing

Neither costs money. Both are cheap to fix and expensive to skip.

**Rate limiting is inert.** `slowapi` is now in `requirements.txt`, but
`RATE_LIMIT_ENABLED` is commented out in `docker-compose.yml`. On a public IP,
`/candidate/tts` and `/answer` are unauthenticated OpenAI-backed endpoints —
someone else can spend our API budget without limit. Turn it on and verify the
decorators actually fire.

**Credentials in `.env` should be treated as compromised.** Four OpenAI keys, the
Office 365 mailbox password, and the Postgres password have all been read by
tooling. Rotate before go-live, not after. Also change the hardcoded
`karnex_password` in `docker-compose.yml` to `${POSTGRES_PASSWORD:?}` and bind
Postgres to `127.0.0.1:5432` rather than publishing it.

---

## Sources

Compute and storage pricing: [DigitalOcean Droplet pricing](https://www.digitalocean.com/pricing/droplets) ·
[DigitalOcean pricing documentation](https://docs.digitalocean.com/products/droplets/details/pricing/) ·
[DigitalOcean pricing breakdown 2026, 1DollarVPS](https://onedollarvps.com/pricing/digitalocean-pricing) ·
[Amazon Lightsail pricing 2026, Cloud Burn](https://cloudburn.io/blog/amazon-lightsail-pricing) ·
[Hostinger India VPS plans 2026](https://www.hostinger.com/in/tutorials/best-vps-hosting/) ·
[E2E Networks price change, July 2026](https://gbnodes.host/blogs/e2e-networks-price-increase-2026-explained/)

Model pricing: [OpenAI API pricing 2026, CloudZero](https://www.cloudzero.com/blog/openai-pricing/) ·
[gpt-4o-mini-tts specs and pricing](https://gate.ai/blog/gpt-4o-mini-tts-openai-specs-pricing-api-use-cases) ·
[gpt-4o-mini-transcribe specs and pricing](https://gate.ai/blog/gpt-4o-mini-transcribe-openai-specs-pricing-api-use-cases)

Exchange rate: [USD/INR August 2026, exchangerates.org.uk](https://www.exchangerates.org.uk/USD-INR-spot-exchange-rates-history-2026.html)

Confirm against each provider's own checkout before committing. List prices move,
and Indian GST handling differs between overseas and domestic providers.
