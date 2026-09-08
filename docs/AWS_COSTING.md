# AWS deployment costing — EC2 + RDS + S3, Mumbai (ap-south-1)

Rates are **ap-south-1 list prices, August 2026**, in INR. GST at 18% is shown
separately because a GST-registered business reclaims it as input credit — your
real cost is the pre-GST figure.

---

## What we are actually sizing for

Measured from your data, not guessed:

| | |
|---|---|
| Candidates | 6,719 |
| Candidate profiles | 8,101 |
| Interview rounds | 3,589 |
| Total rows, main tables | ~45,000 |
| Database size today | **~100 MB** → 5–10 GB over 3 years with audit logs |
| Resume files | 6,933 files, ~1.5 GB (avg 214 KB) |
| Internal users | ~10–50 across Sales, TA, RMG, HR, Finance, Admin |

**This is a small workload.** Six thousand candidates is a rounding error for
Postgres. The honest advice is not to over-provision — the temptation with "MNC
level" is to buy m6i.xlarge everywhere and pay 4× for idle CPU. Your bottleneck
will be OpenAI latency and network round-trips, not CPU.

---

## Tier 1 — Starter (single instance)

Right for go-live and the first few months.

| Service | Spec | ₹/month |
|---|---|---:|
| EC2 | t3.medium (2 vCPU, 4 GB), on-demand 24×7 | 2,500 |
| EBS | gp3 50 GB root | 400 |
| RDS PostgreSQL | db.t3.medium, **Single-AZ** | 5,300 |
| RDS storage | gp3 50 GB | 600 |
| RDS backups | 7-day PITR | 300 |
| S3 | Standard, 5 GB | 11 |
| S3 requests | + data out (first 100 GB free) | 100 |
| CloudWatch | basic + 5 GB logs | 300 |
| Route 53 | hosted zone + queries | 60 |
| **Subtotal (pre-GST)** | | **₹9,571** |
| GST 18% (reclaimable) | | 1,722 |
| **Total** | | **≈ ₹11,300/month** |

Single-AZ RDS means a failover is a restore, not automatic — acceptable while you
are proving the system, not for payroll-critical use.

---

## Tier 2 — Production (recommended)

High availability, private database, CDN in front.

| Service | Spec | ₹/month |
|---|---|---:|
| EC2 | t3.medium × 2, across AZs | 5,000 |
| EBS | gp3 50 GB × 2 | 800 |
| ALB | Application Load Balancer | 1,800 |
| ALB LCU | light traffic | 400 |
| RDS PostgreSQL | db.t3.medium, **Multi-AZ** | 10,600 |
| RDS storage | gp3 100 GB (Multi-AZ doubles it) | 2,400 |
| RDS backups | 7-day PITR | 600 |
| S3 | Standard 10 GB + lifecycle to IA | 25 |
| S3 requests | + CloudFront origin pulls | 200 |
| CloudFront | frontend CDN, ~50 GB egress | 500 |
| **NAT Gateway** | for private subnets | **3,300** |
| CloudWatch | alarms + 10 GB logs | 700 |
| Route 53 + ACM | certificates are free | 60 |
| AWS Backup | snapshots | 400 |
| **Subtotal (pre-GST)** | | **₹26,785** |
| GST 18% (reclaimable) | | 4,821 |
| **Total** | | **≈ ₹31,600/month** |
| **With 1-year Savings Plan / Reserved Instances** | EC2 + RDS ≈ −40% | **≈ ₹24,200/month** |

---

## Where the money actually goes

Three lines are 74% of the Tier 2 bill, and two of them surprise people:

**RDS Multi-AZ — ₹13,600 (51%).** Multi-AZ exactly doubles both compute *and*
storage. It buys automatic failover. If you can tolerate a restore-from-backup
during an outage, Single-AZ halves this to ~₹6,000 and drops the total to about
₹22,000/month.

**NAT Gateway — ₹3,300 (12%).** Charged per hour *plus* ~₹3.6/GB processed, and it
is the most common surprise on an Indian AWS bill. If your EC2 instances are in
public subnets with security groups instead, you can drop this entirely — a
reasonable trade for an internal app. Keep the *database* private either way.

**EC2 — ₹5,000 (19%).** Two t3.medium. Note t3 is burstable: sustained CPU above
baseline bills extra credits at ~₹6.6/vCPU-hour. Your workload is bursty
(recruiters clicking, not batch), so t3 fits — but watch CPU credit balance in
CloudWatch for the first month.

**S3 is ₹25.** Storage genuinely is not your problem. 1.5 GB of resumes costs
about three rupees. Do not spend design effort optimising this.

---

## Costs that are NOT AWS

Easy to forget when budgeting:

| | Estimate |
|---|---|
| **OpenAI API** — AI interviews, CV parsing, ATS scoring | Usually the **largest single line**. Depends entirely on interview volume; 100 interviews/month at ~5 questions each is roughly ₹3,000–8,000. Meter it. |
| Domain | ~₹1,000/year |
| Sentry / error tracking | Free tier is fine at this size |
| Email | Office 365 you already have — but see the SMTP AUTH note below |

---

## Will it run smoothly? — honestly

**Yes, on Tier 1, with three conditions.**

**1. Move uploads to S3 before running two instances.** Today `save_upload` writes
to local disk. With two EC2 instances behind an ALB, an upload lands on one and
is a 404 from the other, and a redeploy wipes them. This is the one change that
genuinely blocks Tier 2. It touches two files (`services/crm_common.py` writes,
`routers/crm/files.py` reads).

**2. Run the migrations, including 0061.** The candidate search was a full table
scan of 6,700 rows evaluating a string concatenation per row — twice per request,
because `paginate()` also ran a `COUNT(*)`. Both are fixed, but only if the
indexes exist.

**3. Rate limiting is currently inert** — `slowapi` is not in `requirements.txt`,
so the decorators are no-ops. On a public IP, the OpenAI-backed endpoints
(`/candidate/tts`, `/answer`) are an open door to billing abuse. **Fix this before
the app is internet-facing**, not after.

Expected performance once deployed in Mumbai: **80–150 ms** for list pages from
anywhere in India, dominated by network rather than query time. AI interview
endpoints will be 1–3 s because that is OpenAI, not you.

---

## Sizing triggers — when to scale up

Do not pre-buy capacity. Move when you see:

| Signal | Action |
|---|---|
| EC2 CPU credit balance trending to zero | t3.medium → **m6i.large** (₹5,800) |
| RDS CPU > 70% sustained, or freeable memory low | db.t3.medium → **db.m6g.large** |
| DB > 80 GB | Raise gp3 storage (it is independent of instance size) |
| More than ~200 concurrent users | Add EC2 instances; ALB already handles it |
| Read-heavy dashboards slow | Add an RDS **read replica** before upsizing the primary |

**Graviton (m6g/t4g) is ~20% cheaper for the same performance** and your stack
(Python + Postgres) runs on ARM without changes. Worth doing at the first resize.

---

## How to cut it 30–40%

1. **1-year Savings Plan on EC2 + Reserved Instance on RDS** — ~40% off the two
   biggest lines, ₹7,400/month saved. Do this once the sizing has settled, not on
   day one.
2. **Drop NAT Gateway** if EC2 can sit in public subnets — ₹3,300/month.
3. **Single-AZ RDS** while pre-production — ₹6,500/month.
4. **Graviton** at the first resize — ~20% off compute.
5. **S3 lifecycle** — resumes older than 90 days to Standard-IA. Saves little here
   (storage is ₹25) but matters if you ever store interview video.
6. **Frontend on CloudFront + S3, not EC2.** It is a static build; serving it from
   the API instance wastes the instance and is slower.

---

## Recommendation

**Start on Tier 1 (₹11,300/month) but with the Tier 2 network layout** — private
subnets and security groups from the beginning, because retrofitting a VPC later
is genuinely painful. Then:

- Move files to S3 (blocks HA anyway)
- Add the second EC2 + ALB when you want zero-downtime deploys
- Switch RDS to Multi-AZ before the system becomes payroll-critical
- Buy the Savings Plan at month 3, once real usage is visible

That lands you at roughly **₹11,000/month now**, **₹24,000/month** fully
production-hardened with commitments — for a system carrying your entire
recruitment pipeline.

---

## Sources

Mumbai EC2 and S3 rates: [AWS Pricing in Mumbai (2026), PrecisionTech](https://precisiontech.in/cloud/amazon-aws-cloud/aws-pricing/aws-pricing-in-mumbai/) ·
RDS regional premium and Multi-AZ doubling: [AWS RDS Cost Breakdown 2026](https://selfhost.dev/blog/aws-rds-cost-breakdown-2026/), [Understanding AWS RDS Pricing, Bytebase](https://www.bytebase.com/blog/understanding-aws-rds-pricing/) ·
Instance specs: [Vantage EC2 instance comparison](https://instances.vantage.sh/aws/ec2/t3.medium)

Confirm against the [AWS Pricing Calculator](https://calculator.aws/) before
committing — these are list prices and AISPL INR rates can differ slightly.
