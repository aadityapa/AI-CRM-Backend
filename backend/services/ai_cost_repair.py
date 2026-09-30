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
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

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

def _interviews(cur) -> list[dict]:
    cur.execute(
        "SELECT interview_id, candidate_name, candidate_email, status, created_at_ist, "
        "finalized_at, last_activity_at, questions, answers, meta FROM interview_progress")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def attribute_orphan_calls(db_target: str) -> int:
    """Give each unattributed interview-kind call its interview, when exactly
    one session window holds it. Returns rows updated."""
    ph = "%s" if _is_postgres(db_target) else "?"
    updated = 0
    try:
        conn = _connect(db_target)
    except Exception:
        return 0
    try:
        cur = conn.cursor()
        interviews = _interviews(cur)
        windows: list[tuple[str, datetime, datetime]] = []
        facts: dict[str, dict] = {}
        for iv in interviews:
            w = session_window(iv.get("created_at_ist"), iv.get("finalized_at"), iv.get("last_activity_at"))
            if w:
                windows.append((str(iv["interview_id"]), w[0], w[1]))
                meta = _json(iv.get("meta")) if not isinstance(iv.get("meta"), dict) else iv["meta"]
                facts[str(iv["interview_id"])] = {
                    "name": str(iv.get("candidate_name") or ""),
                    "email": str(iv.get("candidate_email") or "").lower(),
                    "template": str((meta or {}).get("job_title") or "") if isinstance(meta, dict) else "",
                }
        if not windows:
            return 0
        cur.execute(
            "SELECT id, call_type, created_at_ist FROM ai_prompt_logs "
            "WHERE interview_id IS NULL OR interview_id = ''")
        for row_id, call_type, at in cur.fetchall():
            ct = str(call_type or "")
            if not ct.startswith(INTERVIEW_CALL_PREFIXES):
                continue
            t = parse_ist(at)
            iid = match_interview(t, windows) if t else None
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

    ph = "%s" if _is_postgres(db_target) else "?"
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
        for iv in _interviews(cur):
            iid = str(iv["interview_id"])
            created = parse_ist(iv.get("created_at_ist"))
            status = str(iv.get("status") or "").strip().lower()
            finished = bool(str(iv.get("finalized_at") or "").strip()) or status in FINISHED
            if iid in has_audio or created is None or not finished or created.date().isoformat() >= since:
                continue
            tts_s, stt_s, chars = audio_estimate(_json(iv.get("questions")), _json(iv.get("answers")))
            meta = iv.get("meta") if isinstance(iv.get("meta"), dict) else _json(iv.get("meta"))
            template = str(meta.get("job_title") or "") if isinstance(meta, dict) else ""
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


def repair_ai_costs(db_target: str) -> dict:
    """Both repairs, in order (attribution first, so the audio check sees it)."""
    if not db_target:
        return {"attributed": 0, "estimated_rows": 0}
    out = {"attributed": attribute_orphan_calls(db_target),
           "estimated_rows": estimate_missing_audio(db_target)}
    if out["attributed"] or out["estimated_rows"]:
        logger.info("AI cost ledger repaired: %s", out)
    return out
