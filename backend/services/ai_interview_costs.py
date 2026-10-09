"""What each AI interview cost, and where the money goes (28 Sep 2026).

The CEO asked for interview-wise AI spend — candidate, when, how much — with
daily / weekly / monthly / quarterly / yearly analytics. Every OpenAI call is
already priced and attributed at log time (`prompt_logger`, `services/ai_pricing`);
this module only ADDS UP: one grouped query over `ai_prompt_logs` per
interview, then the interview's own facts from the legacy tables
(`interview_progress` → `interview_schedule`) and its CRM context
(`ai_interview_links` → profile → opportunity → customer). Four batched
queries for a whole page, never one per interview.

Definitions the page prints and that must not drift:
  * an interview's DATE is the day of its FIRST AI call (the day it ran);
    trends bucket on that day, at the zoom asked for — day · ISO week ·
    month · Indian-FY quarter (Q1 = Apr–Jun) · FY.
  * `cost_usd` is the SUM of the stored per-call costs — chat (question
    generation, follow-ups, per-turn and final evaluation, report analysis),
    TTS (the spoken questions incl. prewarmed clips) and STT (the candidate's
    speech). `other_spend` is every priced call that belongs to NO interview
    (ATS, resume parsing, Ask AI, the support bot) — reported beside, never
    folded into an interview.
  * rupees are USD × Settings ▸ `ai.usd_inr_rate`, printed, never stored.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import date, datetime, timedelta

from prompt_logger import _connect, _is_postgres  # the log store's own driver
from services.ai_pricing import CHAT, STT, TTS, usd_inr_rate
from services.ai_models import model_label

logger = logging.getLogger("karnex.ai_interview_costs")

GRANULARITIES = ("day", "week", "month", "quarter", "fy")
FY_START_MONTH = 4          # same rule as services/revenue_report
MAX_RANGE_DAYS = 3 * 366
DEFAULT_RANGE_DAYS = 30
MAX_EXPORT_ROWS = 5000
_CHUNK = 500

#: Non-interview call families for the "other spend" breakdown.
OTHER_FAMILIES = (
    ("ats", "ATS scoring"), ("resume_parse", "Resume parsing"), ("ai_assist", "Ask AI"),
    ("support_bot", "Support bot"), ("extract_text", "Document OCR"),
    ("strengths_weaknesses", "Report analysis (HR page)"),
    ("evaluate_", "Interview calls not matched to a session"),
    ("generate_", "Interview calls not matched to a session"),
    ("tts", "Interview audio not matched to a session"),
    ("transcribe", "Interview audio not matched to a session"),
    ("realtime", "Live voice not matched to a session"),
    ("conversation_", "Interview conversation not matched to a session"),
)


# ------------------------------------------------------------------ buckets

def bucket_key(d: date, granularity: str) -> str:
    """The trend bucket a day falls in. PURE."""
    if granularity == "day":
        return d.isoformat()
    if granularity == "week":
        y, w, _ = d.isocalendar()
        return f"{y}-W{w:02d}"
    if granularity == "month":
        return f"{d.year}-{d.month:02d}"
    fy_start = d.year if d.month >= FY_START_MONTH else d.year - 1
    if granularity == "quarter":
        q = ((d.month - FY_START_MONTH) % 12) // 3 + 1
        return f"FY{fy_start}-Q{q}"
    return f"FY{fy_start}"


def bucket_label(key: str, granularity: str) -> str:
    if granularity == "day":
        return datetime.strptime(key, "%Y-%m-%d").strftime("%d %b %Y")
    if granularity == "week":
        y, w = key.split("-W")
        start = date.fromisocalendar(int(y), int(w), 1)
        return f"{start.strftime('%d %b')} – {(start + timedelta(days=6)).strftime('%d %b %Y')}"
    if granularity == "month":
        return datetime.strptime(key, "%Y-%m").strftime("%b %Y")
    if granularity == "quarter":
        fy_s, q_s = key[2:].split("-Q")
        fy, q = int(fy_s), int(q_s)
        m0 = FY_START_MONTH + (q - 1) * 3            # 4 · 7 · 10 · 13
        y0, m0 = fy + (m0 - 1) // 12, (m0 - 1) % 12 + 1
        m1 = m0 + 2
        y1, m1 = y0 + (m1 - 1) // 12, (m1 - 1) % 12 + 1
        return f"Q{q} FY{fy}-{(fy + 1) % 100:02d} ({date(y0, m0, 1):%b}–{date(y1, m1, 1):%b})"
    fy = int(key[2:])
    return f"FY {fy}-{(fy + 1) % 100:02d}"


def all_bucket_keys(start: date, end: date, granularity: str) -> list[str]:
    """Every bucket between two days, including empty ones — a gap in a chart
    is a lie about a quiet week."""
    keys: list[str] = []
    d = start
    while d <= end:
        k = bucket_key(d, granularity)
        if not keys or keys[-1] != k:
            keys.append(k)
        d += timedelta(days=1)
    return keys


def clamp_range(date_from: str | None, date_to: str | None, today: date | None = None) -> tuple[date, date]:
    """Inclusive window, defaulting to the last 30 days and capped at 3 years."""
    today = today or date.today()
    end = _parse_day(date_to) or today
    start = _parse_day(date_from) or (end - timedelta(days=DEFAULT_RANGE_DAYS - 1))
    if start > end:
        start, end = end, start
    if (end - start).days > MAX_RANGE_DAYS:
        start = end - timedelta(days=MAX_RANGE_DAYS)
    return start, end


def _parse_day(raw: str | None) -> date | None:
    try:
        return date.fromisoformat(str(raw or "").strip()[:10]) if raw else None
    except ValueError:
        return None


# ------------------------------------------------------------------ queries

def _kind_case(alias: str) -> str:
    """SQL that buckets a call_type into chat / tts / stt — mirrors `ai_pricing.call_kind`.

    ⚠️ No LIKE here on purpose (28 Sep 2026, production 500): psycopg2 reads a bare
    `%` in the statement as a parameter marker, so `LIKE 'tts_%'` blew up on
    Postgres while the SQLite tests passed. A prefix test that needs no wildcard
    keeps this string safe under both drivers."""
    # A live voice interview's realtime calls (9 Oct 2026) are voice, so they
    # land in the voice column with text-to-speech.
    return (f"CASE WHEN {alias}.call_type = 'tts' OR SUBSTR({alias}.call_type, 1, 4) = 'tts_' "
            f"OR SUBSTR({alias}.call_type, 1, 8) = 'realtime' THEN '{TTS}' "
            f"WHEN {alias}.call_type = 'transcribe' OR SUBSTR({alias}.call_type, 1, 11) = 'transcribe_' "
            f"THEN '{STT}' ELSE '{CHAT}' END")


def _rollback(conn) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _rows(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _per_interview(conn, ph: str, start: date, end: date) -> dict[str, dict]:
    kind = _kind_case("l")
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT l.interview_id AS interview_id, {kind} AS kind,
               COUNT(*) AS calls,
               SUM(CASE WHEN l.status = 'failed' THEN 1 ELSE 0 END) AS failed,
               COALESCE(SUM(l.prompt_tokens), 0) AS tokens_in,
               COALESCE(SUM(l.completion_tokens), 0) AS tokens_out,
               COALESCE(SUM(l.audio_seconds), 0) AS audio_seconds,
               COALESCE(SUM(l.cost_usd), 0) AS cost_usd,
               COALESCE(SUM(CASE WHEN l.status = 'estimated' THEN l.cost_usd ELSE 0 END), 0) AS estimated_usd,
               MIN(l.created_at_ist) AS first_at, MAX(l.created_at_ist) AS last_at,
               MIN(l.created_date_ist) AS first_day,
               MAX(l.candidate_name) AS candidate_name, MAX(l.candidate_id) AS candidate_email,
               MAX(l.template_name) AS template_name
        FROM ai_prompt_logs l
        WHERE l.interview_id IS NOT NULL AND l.interview_id <> ''
          AND l.created_date_ist >= {ph} AND l.created_date_ist <= {ph}
        GROUP BY l.interview_id, {kind}
        """,
        (start.isoformat(), end.isoformat()),
    )
    out: dict[str, dict] = {}
    for r in _rows(cur):
        iid = str(r["interview_id"])
        row = out.setdefault(iid, {
            "interview_id": iid, "calls": 0, "failed_calls": 0, "tokens_in": 0, "tokens_out": 0,
            "audio_seconds": 0.0, "cost_usd": 0.0, "cost_chat_usd": 0.0, "cost_tts_usd": 0.0,
            "cost_stt_usd": 0.0, "estimated_usd": 0.0, "first_at": None, "last_at": None, "first_day": None,
            "candidate_name": "", "candidate_email": "", "template_name": "",
        })
        row["calls"] += int(r["calls"] or 0)
        row["failed_calls"] += int(r["failed"] or 0)
        row["tokens_in"] += int(r["tokens_in"] or 0)
        row["tokens_out"] += int(r["tokens_out"] or 0)
        row["audio_seconds"] += float(r["audio_seconds"] or 0)
        cost = float(r["cost_usd"] or 0)
        row["cost_usd"] += cost
        row["estimated_usd"] += float(r["estimated_usd"] or 0)
        row[f"cost_{r['kind']}_usd"] += cost
        row["first_at"] = min(filter(None, (row["first_at"], r["first_at"])), default=None)
        row["last_at"] = max(filter(None, (row["last_at"], r["last_at"])), default=None)
        row["first_day"] = min(filter(None, (row["first_day"], r["first_day"])), default=None)
        for k in ("candidate_name", "candidate_email", "template_name"):
            row[k] = row[k] or str(r[k] or "")
    return out


def _per_interview_models(conn, ph: str, start: date, end: date) -> dict[str, list[dict]]:
    """interview_id -> [{model, calls, cost_usd}] (9 Oct 2026: which model ran
    which interview). ONE grouped query; no LIKE (psycopg2 rule above)."""
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT l.interview_id AS interview_id, l.model AS model,
               COUNT(*) AS calls, COALESCE(SUM(l.cost_usd), 0) AS cost_usd
        FROM ai_prompt_logs l
        WHERE l.interview_id IS NOT NULL AND l.interview_id <> ''
          AND l.created_date_ist >= {ph} AND l.created_date_ist <= {ph}
        GROUP BY l.interview_id, l.model
        """,
        (start.isoformat(), end.isoformat()),
    )
    out: dict[str, list[dict]] = {}
    for r in _rows(cur):
        out.setdefault(str(r["interview_id"]), []).append({
            "model": str(r["model"] or "").strip() or "unknown",
            "calls": int(r["calls"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
        })
    return out


def by_model(rows: list[dict], models: dict[str, list[dict]], rate: float) -> list[dict]:
    """The interviews on screen, split by model: label · calls · interviews · cost."""
    agg: dict[str, dict] = {}
    for row in rows:
        for m in models.get(row["interview_id"], []):
            a = agg.setdefault(m["model"], {"model": m["model"], "label": model_label(m["model"]),
                                            "calls": 0, "interviews": 0, "cost_usd": 0.0})
            a["calls"] += m["calls"]
            a["interviews"] += 1
            a["cost_usd"] += m["cost_usd"]
    return sorted(
        (dict(a, cost_usd=round(a["cost_usd"], 4), cost_inr=round(a["cost_usd"] * rate, 2)) for a in agg.values()),
        key=lambda a: -a["cost_usd"],
    )


def _other_spend(conn, ph: str, start: date, end: date) -> dict:
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT call_type, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS cost_usd
        FROM ai_prompt_logs
        WHERE (interview_id IS NULL OR interview_id = '')
          AND created_date_ist >= {ph} AND created_date_ist <= {ph}
        GROUP BY call_type
        """,
        (start.isoformat(), end.isoformat()),
    )
    families: dict[str, dict] = {}
    total = 0.0
    for r in _rows(cur):
        ct = str(r["call_type"] or "")
        label = next((lab for prefix, lab in OTHER_FAMILIES if ct.startswith(prefix)), "Other")
        f = families.setdefault(label, {"label": label, "calls": 0, "cost_usd": 0.0})
        f["calls"] += int(r["calls"] or 0)
        f["cost_usd"] += float(r["cost_usd"] or 0)
        total += float(r["cost_usd"] or 0)
    return {"cost_usd": round(total, 4),
            "families": sorted((dict(f, cost_usd=round(f["cost_usd"], 4)) for f in families.values()),
                               key=lambda f: -f["cost_usd"])}


def _progress_rows(conn, ph: str, ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cur = conn.cursor()
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        marks = ", ".join([ph] * len(chunk))
        try:
            cur.execute(
                f"SELECT interview_id, invite_token, candidate_name, candidate_email, status, "
                f"current_index, finalized_at, created_at_ist, report_status, meta "
                f"FROM interview_progress WHERE interview_id IN ({marks})", chunk)
        except Exception:
            # A failed statement poisons a Postgres transaction; roll it back so
            # the next lookup on this connection still runs.
            logger.warning("interview_progress lookup failed", exc_info=True)
            _rollback(conn)
            return out
        for r in _rows(cur):
            meta = r.get("meta")
            if isinstance(meta, (str, bytes)):
                try:
                    meta = json.loads(meta or "{}")
                except Exception:
                    meta = {}
            r["meta"] = meta if isinstance(meta, dict) else {}
            out[str(r["interview_id"])] = r
    return out


def _schedule_rows(conn, ph: str, tokens: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    cur = conn.cursor()
    for i in range(0, len(tokens), _CHUNK):
        chunk = tokens[i:i + _CHUNK]
        marks = ", ".join([ph] * len(chunk))
        try:
            cur.execute(
                f"SELECT invite_token, candidate_name, candidate_email, scheduled_at_local, hr_username, "
                f"status, session_status, interview_started_at, interview_completed_at "
                f"FROM interview_schedule WHERE invite_token IN ({marks})", chunk)
        except Exception:
            logger.warning("interview_schedule lookup failed", exc_info=True)
            _rollback(conn)
            return out
        for r in _rows(cur):
            out[str(r["invite_token"])] = r
    return out


def crm_context_by_token(crm_db, tokens: list[str]) -> dict[str, dict]:
    """Customer · opportunity · TA owner · AI result per invite token, batched.
    Never raises — a CRM without links still shows the interview."""
    if crm_db is None or not tokens:
        return {}
    try:
        from sqlalchemy import select
        from models import CandidateProfile, Customer, Opportunity
        from models.ai_links import AiInterviewLink

        out: dict[str, dict] = {}
        for i in range(0, len(tokens), _CHUNK):
            chunk = tokens[i:i + _CHUNK]
            stmt = (
                select(AiInterviewLink.invite_token, AiInterviewLink.profile_id, AiInterviewLink.result,
                       AiInterviewLink.overall_score_percent, CandidateProfile.ta_owner_name,
                       Opportunity.id, Opportunity.opp_id, Opportunity.title, Customer.id, Customer.name)
                .join(CandidateProfile, CandidateProfile.id == AiInterviewLink.profile_id)
                .join(Opportunity, Opportunity.id == AiInterviewLink.opportunity_id)
                .outerjoin(Customer, Customer.id == Opportunity.customer_id)
                .where(AiInterviewLink.invite_token.in_(chunk))
            )
            for tok, pid, result, score, ta, oid, opp_id, title, cid, cname in crm_db.execute(stmt).all():
                out[str(tok)] = {
                    "profile_id": pid, "ai_result": result,
                    "ai_score": float(score) if score is not None else None,
                    "ta_owner_name": ta, "opportunity_id": oid, "opp_id": opp_id,
                    "opportunity_title": title, "customer_id": cid, "customer_name": cname,
                }
        return out
    except Exception:
        logger.debug("CRM context lookup failed", exc_info=True)
        return {}


# ------------------------------------------------------------------ assembly

def _minutes_between(a: str | None, b: str | None) -> float | None:
    try:
        if not a or not b:
            return None
        da = datetime.fromisoformat(str(a).replace("Z", "+00:00"))
        db_ = datetime.fromisoformat(str(b).replace("Z", "+00:00"))
        if da.tzinfo != db_.tzinfo:
            da, db_ = da.replace(tzinfo=None), db_.replace(tzinfo=None)
        return round(max(0.0, (db_ - da).total_seconds() / 60.0), 1)
    except Exception:
        return None


def build_rows(per: dict[str, dict], progress: dict[str, dict], schedules: dict[str, dict],
               crm: dict[str, dict], rate: float) -> list[dict]:
    rows = []
    for iid, agg in per.items():
        p = progress.get(iid) or {}
        tok = str(p.get("invite_token") or "")
        s = schedules.get(tok) or {}
        c = crm.get(tok) or {}
        meta = p.get("meta") or {}
        started = s.get("interview_started_at") or p.get("created_at_ist") or agg["first_at"]
        completed = s.get("interview_completed_at") or p.get("finalized_at") or None
        status = str(p.get("status") or s.get("session_status") or "").strip() or "unknown"
        rows.append({
            **agg,
            "invite_token": tok,
            "candidate_name": p.get("candidate_name") or s.get("candidate_name") or agg["candidate_name"] or "—",
            "candidate_email": p.get("candidate_email") or s.get("candidate_email") or agg["candidate_email"] or "",
            "template_name": agg["template_name"] or str(meta.get("job_title") or ""),
            "status": status,
            "scheduled_at": s.get("scheduled_at_local") or None,
            "started_at": started,
            "completed_at": completed,
            "duration_min": _minutes_between(started, completed or agg["last_at"]),
            "questions_answered": int(p.get("current_index") or 0) or None,
            "scheduled_by": s.get("hr_username") or None,
            "tokens": agg["tokens_in"] + agg["tokens_out"],
            "audio_minutes": round(agg["audio_seconds"] / 60.0, 2),
            "cost_usd": round(agg["cost_usd"], 4),
            "cost_chat_usd": round(agg["cost_chat_usd"], 4),
            "cost_tts_usd": round(agg["cost_tts_usd"], 4),
            "cost_stt_usd": round(agg["cost_stt_usd"], 4),
            "estimated_usd": round(agg["estimated_usd"], 4),
            "cost_inr": round(agg["cost_usd"] * rate, 2),
            "day": agg["first_day"],
            **{k: c.get(k) for k in ("profile_id", "ai_result", "ai_score", "ta_owner_name",
                                    "opportunity_id", "opp_id", "opportunity_title",
                                    "customer_id", "customer_name")},
        })
    return rows


def _matches(row: dict, *, search: str, customer_id: int | None, ta: str, status: str, template: str) -> bool:
    if customer_id is not None and row.get("customer_id") != customer_id:
        return False
    if ta and (row.get("ta_owner_name") or "").strip().lower() != ta.strip().lower():
        return False
    if status and row.get("status") != status:
        return False
    if template and (row.get("template_name") or "").strip().lower() != template.strip().lower():
        return False
    if search:
        q = search.strip().lower()
        hay = " ".join(str(row.get(k) or "") for k in (
            "candidate_name", "candidate_email", "template_name", "customer_name", "opp_id",
            "opportunity_title", "ta_owner_name", "interview_id", "invite_token")).lower()
        if q not in hay:
            return False
    return True


_SORTS = {
    "cost": lambda r: -(r["cost_usd"] or 0),
    "date": lambda r: (r.get("first_at") or ""),
    "date_desc": lambda r: (r.get("first_at") or ""),
    "candidate": lambda r: (r.get("candidate_name") or "").lower(),
    "duration": lambda r: -(r.get("duration_min") or 0),
    "tokens": lambda r: -(r.get("tokens") or 0),
}


def _series(rows: list[dict], start: date, end: date, granularity: str) -> list[dict]:
    keys = all_bucket_keys(start, end, granularity)
    acc = {k: {"key": k, "label": bucket_label(k, granularity), "interviews": 0, "cost_usd": 0.0,
               "cost_chat_usd": 0.0, "cost_tts_usd": 0.0, "cost_stt_usd": 0.0, "tokens": 0,
               "audio_minutes": 0.0} for k in keys}
    for r in rows:
        d = _parse_day(r.get("day"))
        if d is None:
            continue
        b = acc.get(bucket_key(d, granularity))
        if b is None:
            continue
        b["interviews"] += 1
        for k in ("cost_usd", "cost_chat_usd", "cost_tts_usd", "cost_stt_usd", "audio_minutes"):
            b[k] += float(r.get(k) or 0)
        b["tokens"] += int(r.get("tokens") or 0)
    out = []
    for k in keys:
        b = acc[k]
        for f in ("cost_usd", "cost_chat_usd", "cost_tts_usd", "cost_stt_usd"):
            b[f] = round(b[f], 4)
        b["audio_minutes"] = round(b["audio_minutes"], 1)
        b["avg_cost_usd"] = round(b["cost_usd"] / b["interviews"], 4) if b["interviews"] else 0.0
        out.append(b)
    return out


def _group(rows: list[dict], key: str, label_key: str, top: int = 12) -> list[dict]:
    acc: dict = {}
    for r in rows:
        k = r.get(key)
        g = acc.setdefault(k, {"id": k, "label": r.get(label_key) or "Unlinked", "interviews": 0, "cost_usd": 0.0})
        g["interviews"] += 1
        g["cost_usd"] += float(r.get("cost_usd") or 0)
    out = sorted(acc.values(), key=lambda g: -g["cost_usd"])[:top]
    for g in out:
        g["cost_usd"] = round(g["cost_usd"], 4)
        g["avg_cost_usd"] = round(g["cost_usd"] / g["interviews"], 4) if g["interviews"] else 0.0
    return out


def interview_cost_report(db_target: str, crm_db=None, *, date_from: str | None = None,
                          date_to: str | None = None, granularity: str = "day", search: str = "",
                          customer_id: int | None = None, ta: str = "", status: str = "",
                          template: str = "", sort: str = "cost", page: int = 1, limit: int = 50,
                          today: date | None = None) -> dict:
    """The whole page in one call: KPIs, trend, breakdowns, filter options and
    one page of interviews. Filters narrow everything except `other_spend`."""
    granularity = granularity if granularity in GRANULARITIES else "day"
    start, end = clamp_range(date_from, date_to, today)
    rate = usd_inr_rate()
    ph = "%s" if _is_postgres(db_target) else "?"

    conn = _connect(db_target)
    try:
        per = _per_interview(conn, ph, start, end)
        per_models = _per_interview_models(conn, ph, start, end)
        other = _other_spend(conn, ph, start, end)
        progress = _progress_rows(conn, ph, list(per.keys()))
        tokens = [str(p.get("invite_token") or "") for p in progress.values() if p.get("invite_token")]
        schedules = _schedule_rows(conn, ph, tokens)
    finally:
        conn.close()
    crm = crm_context_by_token(crm_db, tokens)

    every = build_rows(per, progress, schedules, crm, rate)
    for row in every:
        used = sorted(per_models.get(row["interview_id"], []), key=lambda m: -m["cost_usd"])
        row["models"] = [model_label(m["model"]) for m in used if m["model"] != "unknown"]
    options = {
        "customers": sorted({(r["customer_id"], r["customer_name"]) for r in every if r.get("customer_id")},
                            key=lambda x: (x[1] or "")),
        "ta_owners": sorted({r["ta_owner_name"] for r in every if r.get("ta_owner_name")}),
        "templates": sorted({r["template_name"] for r in every if r.get("template_name")}),
        "statuses": sorted({r["status"] for r in every if r.get("status")}),
    }
    rows = [r for r in every if _matches(r, search=search, customer_id=customer_id, ta=ta,
                                         status=status, template=template)]
    rows.sort(key=_SORTS.get(sort, _SORTS["cost"]), reverse=(sort == "date_desc"))

    total_cost = sum(r["cost_usd"] for r in rows)
    completed = [r for r in rows if r["status"] == "completed"]
    summary = {
        "interviews": len(rows),
        "completed": len(completed),
        "cost_usd": round(total_cost, 4),
        "cost_inr": round(total_cost * rate, 2),
        "avg_cost_usd": round(total_cost / len(rows), 4) if rows else 0.0,
        "avg_cost_inr": round(total_cost * rate / len(rows), 2) if rows else 0.0,
        "max_cost_usd": round(max((r["cost_usd"] for r in rows), default=0.0), 4),
        "cost_chat_usd": round(sum(r["cost_chat_usd"] for r in rows), 4),
        "cost_tts_usd": round(sum(r["cost_tts_usd"] for r in rows), 4),
        "cost_stt_usd": round(sum(r["cost_stt_usd"] for r in rows), 4),
        # Audio before 28 Sep 2026 was not logged; `services/ai_cost_repair`
        # estimated it from the transcripts. The page says how much that is.
        "estimated_usd": round(sum(r["estimated_usd"] for r in rows), 4),
        "estimated_interviews": sum(1 for r in rows if r["estimated_usd"]),
        "calls": sum(r["calls"] for r in rows),
        "failed_calls": sum(r["failed_calls"] for r in rows),
        "tokens": sum(r["tokens"] for r in rows),
        "audio_minutes": round(sum(r["audio_minutes"] for r in rows), 1),
        "avg_duration_min": (round(sum(r["duration_min"] for r in completed if r["duration_min"]) /
                                   max(1, len([r for r in completed if r["duration_min"]])), 1)
                             if completed else None),
        "other_spend_usd": other["cost_usd"],
        "other_spend_inr": round(other["cost_usd"] * rate, 2),
        "total_spend_usd": round(total_cost + other["cost_usd"], 4),
        "total_spend_inr": round((total_cost + other["cost_usd"]) * rate, 2),
    }
    page = max(1, int(page or 1))
    limit = max(1, min(int(limit or 50), MAX_EXPORT_ROWS))
    start_i = (page - 1) * limit
    return {
        "period": {"date_from": start.isoformat(), "date_to": end.isoformat(),
                   "granularity": granularity, "days": (end - start).days + 1},
        "usd_inr_rate": rate,
        "summary": summary,
        "series": _series(rows, start, end, granularity),
        "by_kind": [
            {"kind": CHAT, "label": "Questions & evaluation (chat)", "cost_usd": summary["cost_chat_usd"]},
            {"kind": TTS, "label": "Spoken questions (TTS)", "cost_usd": summary["cost_tts_usd"]},
            {"kind": STT, "label": "Candidate speech (transcription)", "cost_usd": summary["cost_stt_usd"]},
        ],
        "by_model": by_model(rows, per_models, rate),
        "by_customer": _group(rows, "customer_id", "customer_name"),
        "by_template": _group(rows, "template_name", "template_name"),
        "by_ta": _group(rows, "ta_owner_name", "ta_owner_name"),
        "other_spend": other,
        "top_interviews": sorted(rows, key=_SORTS["cost"])[:5],
        "options": {
            "customers": [{"id": cid, "name": name} for cid, name in options["customers"]],
            "ta_owners": options["ta_owners"], "templates": options["templates"],
            "statuses": options["statuses"], "granularities": list(GRANULARITIES),
        },
        "interviews": rows[start_i:start_i + limit],
        "meta": {"page": page, "limit": limit, "total": len(rows), "sort": sort},
    }


CSV_COLUMNS = [
    ("day", "Date"), ("candidate_name", "Candidate"), ("candidate_email", "Email"),
    ("customer_name", "Customer"), ("opp_id", "Opportunity"), ("opportunity_title", "Opportunity title"),
    ("template_name", "Interview template"), ("ta_owner_name", "TA"), ("scheduled_by", "Scheduled by"),
    ("status", "Status"), ("scheduled_at", "Scheduled at"), ("started_at", "Started at"),
    ("completed_at", "Completed at"), ("duration_min", "Duration (min)"),
    ("questions_answered", "Questions answered"), ("ai_result", "AI result"), ("ai_score", "AI score %"),
    ("calls", "AI calls"), ("failed_calls", "Failed calls"), ("tokens_in", "Tokens in"),
    ("tokens_out", "Tokens out"), ("audio_minutes", "Audio minutes"),
    ("cost_chat_usd", "Chat cost (USD)"), ("cost_tts_usd", "TTS cost (USD)"),
    ("cost_stt_usd", "Transcription cost (USD)"), ("cost_usd", "Total cost (USD)"),
    ("cost_inr", "Total cost (INR)"), ("estimated_usd", "Of which estimated (USD)"),
    ("interview_id", "Interview id"),
]


def _csv_safe(v) -> str:
    s = "" if v is None else str(v)
    return ("'" + s) if s[:1] in ("=", "+", "-", "@") else s   # formula injection


def interview_costs_csv(report: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label in CSV_COLUMNS])
    for r in report.get("interviews", []):
        w.writerow([_csv_safe(r.get(k)) for k, _ in CSV_COLUMNS])
    return buf.getvalue()
