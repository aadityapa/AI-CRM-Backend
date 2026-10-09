"""Make the AI-cost ledger complete for interviews run before it was (29 Sep 2026).

The CEO's AI Costs page read "0 interviews" and a couple of rupees of "other
spend" for a month in which AI interviews DID run. Two gaps, both from before
the 28 Sep deploy that started itemising interviews:

  1. **Unattributed chat calls.** The per-turn / final evaluation calls were
     logged with an EMPTY `interview_id` (the context did not reach them), so
     the report filed them under "other spend". `attribute_orphan_calls` matches
     each such row to the ONE interview whose session window holds it
     (`interview_progress`: created → finalized / last activity, padded), and
     leaves anything ambiguous alone — a wrong candidate is worse than none.
  2. **No audio rows at all.** Speech (the spoken questions, the candidate's
     transcribed answers) was never logged before 28 Sep, and it is most of an
     interview's cost. `estimate_missing_audio` writes ONE estimated TTS row and
     ONE estimated STT row per finished interview that has none, from the
     interview's own questions and answers (`TTS_CHARS_PER_SECOND`, spoken at
     `SPEECH_WORDS_PER_SECOND`), priced per minute like any audio call, with
     `status = "estimated"` so the page can say so. Deterministic ids
     (`est-tts-<interview>`), so a re-run never double-counts.

Both run at startup (background) and from the daily `prompt_log_retention` job,
are idempotent, never raise, and touch only rows they can prove.

7 Oct 2026 — this module used to read EVERY `interview_progress` row with its
`questions` / `answers` / `meta` JSON on every run (each worker's startup and
the nightly job), a full-table scan of the heaviest columns to repair a ledger
that was repaired weeks ago. Now: the attribution reads the orphan calls
FIRST and stops when there are none; interview rows are read slim (no JSON,
the template name extracted in SQL); the audio estimate reads `questions` /
`answers` only for the pre-`AUDIO_LOGGED_SINCE` interviews that still lack
them; and a run that finds nothing to do records `ai.cost_repair_done` in the
CRM `app_settings` (value `<store hash>:<date>`), after which `repair_ai_costs`
is a no-op for that store. Delete that row (Settings ▸ Settings KV) to force
a full re-run.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone

from prompt_logger import _connect, _is_postgres

logger = logging.getLogger("karnex.ai_cost_repair")

IST = timezone(timedelta(hours=5, minutes=30))

#: Audio was logged per call from this IST day on; earlier interviews get estimates.
AUDIO_LOGGED_SINCE = "2026-09-28"
#: A session window: a little before the first activity, and long enough after
#: finalizing for the report / strengths analysis that runs once it is submitted.
WINDOW_BEFORE = timedelta(minutes=5)
WINDOW_AFTER = timedelta(minutes=45)
#: A session with no finalize or activity stamp is given this long.
DEFAULT_SESSION = timedelta(hours=3)
#: Unhurried answer speech (~150 words a minute) — for the STT estimate.
SPEECH_WORDS_PER_SECOND = 2.5
#: Call types that belong to an interview session (chat side).
INTERVIEW_CALL_PREFIXES = (
    "evaluate_", "generate_questions", "generate_followup", "strengths_weaknesses", "tts", "transcribe",
)
FINISHED = {"completed", "submitted", "terminated", "finalized", "recovered", "abandoned"}
_SKIP_ANSWERS = {"", "skip", "skipped", "[skipped]", "no response", "[no response]"}


# ------------------------------------------------------------------ pure helpers

def parse_ist(raw) -> datetime | None:
    """Any stored stamp → a naive IST datetime. PURE."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(IST).replace(tzinfo=None)
    return dt


def session_window(created, finalized, last_activity) -> tuple[datetime, datetime] | None:
    """[start, end] of a session, padded. PURE."""
    start = parse_ist(created)
    if start is None:
        return None
    end = parse_ist(finalized) or parse_ist(last_activity) or (start + DEFAULT_SESSION)
    if end < start:
        end = start + DEFAULT_SESSION
    return start - WINDOW_BEFORE, end + WINDOW_AFTER


def match_interview(at: datetime, windows: list[tuple[str, datetime, datetime]]) -> str | None:
    """The ONE interview whose window holds `at`, else None (none or ambiguous). PURE."""
    hits = [iid for iid, a, b in windows if a <= at <= b]
    return hits[0] if len(hits) == 1 else None


def _texts(items, keys: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v)
                    break
    return out


def audio_estimate(questions, answers) -> tuple[float, float, int]:
    """(tts_seconds, stt_seconds, question_chars) for one interview. PURE."""
    from services.ai_pricing import TTS_CHARS_PER_SECOND

    q_text = _texts(questions, ("question", "text", "q", "prompt"))
    chars = sum(len(t.strip()) for t in q_text)
    a_text = [t for t in _texts(answers, ("answer", "transcript", "text", "response"))
              if t.strip().lower() not in _SKIP_ANSWERS]
    words = sum(len(t.split()) for t in a_text)
    return round(chars / TTS_CHARS_PER_SECOND, 1), round(words / SPEECH_WORDS_PER_SECOND, 1), chars


def _json(raw):
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


# ------------------------------------------------------------------ DB work

#: Slim interview columns — never the JSON bodies.
_SLIM = "interview_id, candidate_name, candidate_email, status, created_at_ist, finalized_at, last_activity_at"


def _template_sql(pg: bool) -> str:
    """`meta.job_title` pulled out in SQL so `meta` itself never travels."""
    if pg:
        return "meta->>'job_title'"
    return "CASE WHEN json_valid(meta) THEN json_extract(meta, '$.job_title') END"


def _interview_call_clause() -> str:
    """`call_type` starts with one of INTERVIEW_CALL_PREFIXES — spelled with
    SUBSTR, never LIKE: psycopg2 reads a bare `%` as a parameter marker."""
    return "(" + " OR ".join(
        f"SUBSTR(call_type, 1, {len(p)}) = '{p}'" for p in INTERVIEW_CALL_PREFIXES) + ")"


def _interviews(cur, pg: bool, *, created_before: str | None = None, pre_audio: bool = False) -> list[dict]:
    """Slim interview rows (+ `template`), optionally bounded. No JSON columns."""
    where: list[str] = []
    params: list = []
    ph = "%s" if pg else "?"
    if created_before:
        where.append(f"created_at_ist <= {ph}")
        params.append(created_before)
    if pre_audio:
        where.append(f"SUBSTR(created_at_ist, 1, 10) < {ph}")
        params.append(AUDIO_LOGGED_SINCE)
        fin = ", ".join([ph] * len(FINISHED))
        where.append(f"(COALESCE(finalized_at, '') <> '' OR LOWER(COALESCE(status, '')) IN ({fin}))")
        params.extend(sorted(FINISHED))
    sql = f"SELECT {_SLIM}, {_template_sql(pg)} AS template FROM interview_progress"
    if where:
        sql += " WHERE " + " AND ".join(where)
    cur.execute(sql, tuple(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _interview_bodies(cur, pg: bool, interview_id: str) -> tuple[list, list]:
    """`questions` / `answers` of ONE interview — read only when an estimate is written."""
    ph = "%s" if pg else "?"
    cur.execute(f"SELECT questions, answers FROM interview_progress WHERE interview_id = {ph}", (interview_id,))
    row = cur.fetchone()
    if not row:
        return [], []
    return _json(row[0]), _json(row[1])


def attribute_orphan_calls(db_target: str) -> int:
    """Give each unattributed interview-kind call its interview, when exactly
    one session window holds it. Returns rows updated. Reads the orphans
    first and touches `interview_progress` only when there are some."""
    pg = _is_postgres(db_target)
    ph = "%s" if pg else "?"
    updated = 0
    try:
        conn = _connect(db_target)
    except Exception:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, call_type, created_at_ist FROM ai_prompt_logs "
            f"WHERE (interview_id IS NULL OR interview_id = '') AND {_interview_call_clause()}")
        orphans = [(row_id, str(ct or ""), parse_ist(at)) for row_id, ct, at in cur.fetchall()]
        orphans = [o for o in orphans if o[2] is not None]
        if not orphans:
            return 0
        # Only a session that started before the last orphan (plus the pad) can hold one.
        latest = max(o[2] for o in orphans) + WINDOW_BEFORE
        windows: list[tuple[str, datetime, datetime]] = []
        facts: dict[str, dict] = {}
        for iv in _interviews(cur, pg, created_before=latest.replace(tzinfo=IST).isoformat()):
            w = session_window(iv.get("created_at_ist"), iv.get("finalized_at"), iv.get("last_activity_at"))
            if w:
                windows.append((str(iv["interview_id"]), w[0], w[1]))
                facts[str(iv["interview_id"])] = {
                    "name": str(iv.get("candidate_name") or ""),
                    "email": str(iv.get("candidate_email") or "").lower(),
                    "template": str(iv.get("template") or ""),
                }
        if not windows:
            return 0
        for row_id, _ct, t in orphans:
            iid = match_interview(t, windows)
            if not iid:
                continue
            f = facts[iid]
            cur.execute(
                f"UPDATE ai_prompt_logs SET interview_id = {ph}, "
                f"candidate_name = CASE WHEN COALESCE(candidate_name, '') = '' THEN {ph} ELSE candidate_name END, "
                f"candidate_id = CASE WHEN COALESCE(candidate_id, '') = '' THEN {ph} ELSE candidate_id END, "
                f"template_name = CASE WHEN COALESCE(template_name, '') = '' THEN {ph} ELSE template_name END "
                f"WHERE id = {ph} AND (interview_id IS NULL OR interview_id = '')",
                (iid, f["name"], f["email"], f["template"], row_id))
            updated += int(cur.rowcount or 0)
        conn.commit()
    except Exception as exc:
        logger.warning("AI call attribution failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return updated


def estimate_missing_audio(db_target: str, since: str = AUDIO_LOGGED_SINCE) -> int:
    """One estimated TTS + one STT row per finished pre-`since` interview that
    has no audio rows. Returns rows inserted."""
    from services.ai_pricing import estimate_cost_usd

    pg = _is_postgres(db_target)
    ph = "%s" if pg else "?"
    inserted = 0
    try:
        conn = _connect(db_target)
    except Exception:
        return 0
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT interview_id FROM ai_prompt_logs WHERE interview_id IS NOT NULL AND interview_id <> '' "
            "AND (call_type = 'tts' OR SUBSTR(call_type, 1, 4) = 'tts_' "
            "OR call_type = 'transcribe' OR SUBSTR(call_type, 1, 11) = 'transcribe_')")
        has_audio = {str(r[0]) for r in cur.fetchall()}
        # Slim rows, already narrowed in SQL to finished pre-`since` interviews.
        for iv in _interviews(cur, pg, pre_audio=since == AUDIO_LOGGED_SINCE):
            iid = str(iv["interview_id"])
            created = parse_ist(iv.get("created_at_ist"))
            status = str(iv.get("status") or "").strip().lower()
            finished = bool(str(iv.get("finalized_at") or "").strip()) or status in FINISHED
            if iid in has_audio or created is None or not finished or created.date().isoformat() >= since:
                continue
            # The JSON bodies travel only for an interview that still needs its estimate.
            tts_s, stt_s, chars = audio_estimate(*_interview_bodies(cur, pg, iid))
            template = str(iv.get("template") or "")
            for kind, model, seconds, tokens in (
                ("tts", "gpt-4o-mini-tts", tts_s, (chars + 3) // 4),
                ("transcribe", "gpt-4o-mini-transcribe", stt_s, 0),
            ):
                if seconds <= 0:
                    continue
                row_id = f"est-{kind}-{iid}"[:120]
                cur.execute(f"SELECT 1 FROM ai_prompt_logs WHERE id = {ph}", (row_id,))
                if cur.fetchone():
                    continue
                cost = estimate_cost_usd(model=model, call_type=kind, prompt_tokens=tokens, audio_seconds=seconds)
                cur.execute(
                    "INSERT INTO ai_prompt_logs (id, interview_id, candidate_name, candidate_id, template_name, "
                    "call_type, model, prompt_tokens, completion_tokens, total_tokens, status, error_log, "
                    "created_at, created_at_ist, created_date_ist, created_time_ist, audio_seconds, cost_usd) VALUES "
                    f"({', '.join([ph] * 18)})",
                    (row_id, iid, str(iv.get("candidate_name") or ""), str(iv.get("candidate_email") or "").lower(),
                     template, f"{kind}_estimated", model, tokens, 0, tokens, "estimated",
                     "Estimated from the interview's questions and answers — audio was not logged "
                     f"before {since}.", created.replace(tzinfo=IST).isoformat(), created.isoformat(),
                     created.date().isoformat(),
                     created.strftime("%H:%M:%S"), seconds, cost))
                inserted += 1
        conn.commit()
    except Exception as exc:
        logger.warning("Audio cost estimate failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        conn.close()
    return inserted


# ------------------------------------------------------------------ completion marker

#: CRM `app_settings` key; value is `<store hash>:<ISO date>` of the run that found nothing left.
REPAIR_DONE_KEY = "ai.cost_repair_done"
_DONE_IN_PROCESS: dict[str, str] = {}


def _store_hash(db_target: str) -> str:
    return hashlib.sha1(str(db_target or "").encode("utf-8")).hexdigest()[:12]


def _marker_read(key: str = REPAIR_DONE_KEY) -> str:
    """The stored marker, or "" — never raises (no CRM DB in the legacy tests)."""
    try:
        from sqlalchemy import text

        from crm_db import get_session_factory

        session = get_session_factory()()
        try:
            row = session.execute(text("SELECT value FROM app_settings WHERE key = :k"),
                                  {"k": key}).first()
            return str(row[0] or "") if row else ""
        finally:
            session.close()
    except Exception:
        return ""


def _marker_write(value: str, key: str = REPAIR_DONE_KEY,
                  description: str = "AI cost ledger repair finished (delete this row to run it again)") -> bool:
    try:
        from sqlalchemy import text

        from crm_db import get_session_factory

        session = get_session_factory()()
        try:
            with session.begin():
                session.execute(text(
                    "INSERT INTO app_settings (key, value, description) VALUES (:k, :v, :d) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                    {"k": key, "v": value, "d": description})
            return True
        finally:
            session.close()
    except Exception as exc:
        logger.debug("cost repair marker not written: %s", exc)
        return False


def repair_done(db_target: str) -> bool:
    """True once a run on THIS store found nothing left to repair."""
    h = _store_hash(db_target)
    if _DONE_IN_PROCESS.get(h):
        return True
    return _marker_read().startswith(h + ":")


#: 9 Oct 2026: the production patch of 8 Oct moved interviews to gpt-6-astra
#: WITHOUT its price rows, so every Astra call logged since was priced at the
#: gpt-4o-mini fallback (65-85x low). One idempotent re-price from the stored
#: tokens at today's price; cached tokens are not stored per row, so these rows
#: are priced at the full input rate (a slight over-statement, footnoted on AI
#: Costs). A priced row is otherwise never re-priced.
REPRICE_DONE_KEY = "ai.reprice_gpt6_done"
REPRICE_MODELS = ("gpt-6-astra",)


def reprice_model_rows(db_target: str, models: tuple[str, ...] = REPRICE_MODELS) -> int:
    """Re-price the logged calls of `models` once per store (marker
    `ai.reprice_gpt6_done`). Exact model match — no LIKE (psycopg2 rule)."""
    if not db_target:
        return 0
    h = _store_hash(db_target)
    if _DONE_IN_PROCESS.get(f"reprice:{h}") or _marker_read(REPRICE_DONE_KEY).startswith(h + ":"):
        return 0
    from services.ai_pricing import estimate_cost_usd, pricing_table

    table = pricing_table()
    ph = "%s" if _is_postgres(db_target) else "?"
    updated = 0
    try:
        conn = _connect(db_target)
        try:
            cur = conn.cursor()
            for model in models:
                unit_in = estimate_cost_usd(model=model, call_type="chat_completion",
                                            prompt_tokens=1_000_000, table=table)
                unit_out = estimate_cost_usd(model=model, call_type="chat_completion",
                                             completion_tokens=1_000_000, table=table)
                cur.execute(
                    "UPDATE ai_prompt_logs SET cost_usd = "
                    f"(COALESCE(prompt_tokens, 0) * {ph} + COALESCE(completion_tokens, 0) * {ph}) / 1000000.0 "
                    f"WHERE model = {ph} AND COALESCE(status, '') <> 'estimated'",
                    (unit_in, unit_out, model),
                )
                updated += int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("AI cost re-price failed: %s", exc)
        return 0
    marker = f"{h}:{date.today().isoformat()}"
    _DONE_IN_PROCESS[f"reprice:{h}"] = marker
    _marker_write(marker, REPRICE_DONE_KEY, "gpt-6-astra calls re-priced (delete this row to run it again)")
    logger.info("AI cost re-price: %d %s call(s) re-priced", updated, "/".join(models))
    return updated


def repair_ai_costs(db_target: str) -> dict:
    """Both repairs, in order (attribution first, so the audio check sees it).
    A no-op once a run found nothing to do (`repair_done`). The one-off
    gpt-6-astra re-price runs first, under its own marker."""
    if not db_target:
        return {"attributed": 0, "estimated_rows": 0}
    reprice_model_rows(db_target)
    if repair_done(db_target):
        return {"attributed": 0, "estimated_rows": 0, "skipped": True}
    out = {"attributed": attribute_orphan_calls(db_target),
           "estimated_rows": estimate_missing_audio(db_target)}
    if out["attributed"] or out["estimated_rows"]:
        logger.info("AI cost ledger repaired: %s", out)
    else:
        marker = f"{_store_hash(db_target)}:{date.today().isoformat()}"
        _DONE_IN_PROCESS[_store_hash(db_target)] = marker
        if _marker_write(marker):
            logger.info("AI cost ledger repair complete — recorded %s", REPAIR_DONE_KEY)
    return out
