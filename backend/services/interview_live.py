"""A recruiter joins a running AI interview (11 Oct 2026).

Phase 1 — control: a TA (or anyone with the Integrity tab) can PAUSE the AI
interviewer, put a short note on the candidate's screen, queue their OWN
question (asked next, answered and scored like any other) and RESUME.

Phase 2 — the call: a live audio/video call between the recruiter and the
candidate, browser to browser (WebRTC). This module only carries the two
session descriptions; the media never touches the app server.

Where the state lives
---------------------
Two JSON columns on the legacy ``interview_schedule`` row, one per WRITER, so
the two sides can never overwrite each other's update (the app runs as two
processes and an interview session lives in only one of them):

* ``live_control`` — written by staff only (pause / resume / note / question /
  call offer / hang up), read by the candidate's page every few seconds.
* ``live_reply`` — written by the candidate's page only (the call answer, the
  last time it checked in), read by the recruiter's page.

The clock
---------
A pause stops the interview's own clock: the time limit is measured from the
first question MINUS every second spent paused (``pause_total_sec`` plus the
running pause). A pause is capped at ``max_pause_sec()`` — past that it ends by
itself, so a recruiter who walks away can never leave a candidate frozen.

Everything here is PURE (no I/O): ``now`` is passed in, so every rule is
testable without a clock or a database.
"""
from __future__ import annotations

import os
import uuid
from typing import Any

MAX_QUESTION_CHARS = 600
MAX_MESSAGE_CHARS = 300
MAX_PENDING_QUESTIONS = 5
MAX_LOG = 60
MAX_SDP_CHARS = 60_000
#: The candidate's page checks in this often (seconds) — it is told so.
CANDIDATE_POLL_SEC = 3
#: The recruiter's panel polls this often while it is open.
STAFF_POLL_SEC = 2
#: While a recruiter has the live panel open (or the interview is paused or
#: on a call) the candidate's page checks every second instead (12 Oct 2026:
#: "when TA clicks join it must be immediate, not a buffer").
FAST_POLL_MS = 1000
#: A panel says "still here" this often; a recruiter counts as present for
#: this long after the last word.
PRESENCE_EVERY_SEC = 15
PRESENCE_WINDOW_SEC = 45

STATE_RUNNING = "running"
STATE_PAUSED = "paused"


class LiveError(ValueError):
    """A refused action — the message is shown to the recruiter as is."""


def max_pause_sec() -> int:
    """How long one pause may last before the interview resumes by itself.
    ``INTERVIEW_LIVE_MAX_PAUSE_SEC`` (default 20 min, clamped 60..3600). Kept
    under the recovery worker's 35-minute idle rule, so a paused interview is
    never mistaken for an abandoned one."""
    try:
        val = int(str(os.getenv("INTERVIEW_LIVE_MAX_PAUSE_SEC") or "1200").strip())
    except ValueError:
        val = 1200
    return max(60, min(3600, val))


def enabled() -> bool:
    """``INTERVIEW_LIVE_JOIN_ENABLED`` (default on)."""
    return str(os.getenv("INTERVIEW_LIVE_JOIN_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def ice_servers() -> list[dict]:
    """STUN/TURN servers both browsers use to find each other.

    A public STUN server is enough on most home and office networks. Behind a
    strict corporate firewall a TURN relay is needed: set
    ``INTERVIEW_TURN_URLS`` (comma separated, e.g. ``turn:turn.example.com:3478,
    turns:turn.example.com:443``), ``INTERVIEW_TURN_USERNAME`` and
    ``INTERVIEW_TURN_CREDENTIAL``. ``INTERVIEW_STUN_URLS`` overrides the STUN list
    (``none`` switches it off)."""
    out: list[dict] = []
    stun_raw = str(os.getenv("INTERVIEW_STUN_URLS") or "stun:stun.l.google.com:19302,stun:stun1.l.google.com:19302").strip()
    if stun_raw.lower() not in {"", "none", "off"}:
        urls = [u.strip() for u in stun_raw.split(",") if u.strip()]
        if urls:
            out.append({"urls": urls})
    turn_raw = str(os.getenv("INTERVIEW_TURN_URLS") or "").strip()
    if turn_raw:
        urls = [u.strip() for u in turn_raw.split(",") if u.strip()]
        if urls:
            entry: dict[str, Any] = {"urls": urls}
            user = str(os.getenv("INTERVIEW_TURN_USERNAME") or "").strip()
            cred = str(os.getenv("INTERVIEW_TURN_CREDENTIAL") or "").strip()
            if user:
                entry["username"] = user
            if cred:
                entry["credential"] = cred
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------

def empty_control() -> dict:
    return {
        "rev": 0,
        "state": STATE_RUNNING,
        "paused_at": None,
        "pause_total_sec": 0.0,
        "pause_count": 0,
        "paused_by": "",
        "pause_reason": "",
        "joined": [],          # [{id, name, at}] — who opened the live controls
        "message": None,       # {id, text, by, at}
        "questions": [],       # [{id, text, by, at}] — queued, asked on resume
        "asked": [],           # [{id, text, by, at, index}] — already put to the candidate
        "call": None,          # {id, offer, by, at, video}
        "log": [],             # [{at, by, action, text}]
    }


def empty_reply() -> dict:
    return {"call_id": "", "answer": "", "seen_at": None, "applied_question_ids": []}


def normalize_control(raw: Any) -> dict:
    base = empty_control()
    if isinstance(raw, dict):
        for key in base:
            if key in raw and raw[key] is not None:
                base[key] = raw[key]
    for key in ("joined", "questions", "asked", "log"):
        if not isinstance(base[key], list):
            base[key] = []
    try:
        base["pause_total_sec"] = max(0.0, float(base.get("pause_total_sec") or 0))
    except (TypeError, ValueError):
        base["pause_total_sec"] = 0.0
    try:
        base["paused_at"] = float(base["paused_at"]) if base.get("paused_at") not in (None, "") else None
    except (TypeError, ValueError):
        base["paused_at"] = None
    base["state"] = STATE_PAUSED if base["paused_at"] else STATE_RUNNING
    if not isinstance(base.get("call"), dict) or not base["call"].get("id"):
        base["call"] = None
    if not isinstance(base.get("message"), dict) or not base["message"].get("text"):
        base["message"] = None
    try:
        base["rev"] = int(base.get("rev") or 0)
    except (TypeError, ValueError):
        base["rev"] = 0
    return base


def normalize_reply(raw: Any) -> dict:
    base = empty_reply()
    if isinstance(raw, dict):
        for key in base:
            if key in raw and raw[key] is not None:
                base[key] = raw[key]
    if not isinstance(base.get("applied_question_ids"), list):
        base["applied_question_ids"] = []
    return base


def _clean_text(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split()).strip()[:limit]


def _log(ctrl: dict, now: float, by: str, action: str, text: str = "") -> None:
    ctrl["log"] = (list(ctrl.get("log") or []) + [{"at": now, "by": by, "action": action, "text": text[:300]}])[-MAX_LOG:]


def _bump(ctrl: dict) -> dict:
    ctrl["rev"] = int(ctrl.get("rev") or 0) + 1
    return ctrl


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

def running_pause_sec(ctrl: dict, now: float) -> float:
    """Seconds of the pause in progress (capped at the maximum), else 0."""
    at = ctrl.get("paused_at")
    if not at:
        return 0.0
    return max(0.0, min(float(now) - float(at), float(max_pause_sec())))


def total_paused_sec(ctrl: dict, now: float) -> float:
    return float(ctrl.get("pause_total_sec") or 0) + running_pause_sec(ctrl, now)


def is_paused(ctrl: dict, now: float) -> bool:
    at = ctrl.get("paused_at")
    return bool(at) and (float(now) - float(at)) < max_pause_sec()


def expire_pause(ctrl: dict, now: float) -> bool:
    """End a pause that ran past the maximum. True when it changed ``ctrl``."""
    at = ctrl.get("paused_at")
    if not at or (float(now) - float(at)) < max_pause_sec():
        return False
    ctrl["pause_total_sec"] = float(ctrl.get("pause_total_sec") or 0) + float(max_pause_sec())
    ctrl["paused_at"] = None
    ctrl["state"] = STATE_RUNNING
    _log(ctrl, now, "system", "auto_resumed", "The pause reached its time limit — the interview resumed by itself.")
    _bump(ctrl)
    return True


# ---------------------------------------------------------------------------
# Staff actions
# ---------------------------------------------------------------------------

def join(ctrl: dict, by_id: str, by_name: str, now: float) -> dict:
    joined = [j for j in ctrl.get("joined") or [] if str(j.get("id")) != str(by_id)]
    joined.append({"id": str(by_id), "name": by_name, "at": now})
    ctrl["joined"] = joined[-10:]
    _log(ctrl, now, by_name, "joined")
    return _bump(ctrl)


def presence(ctrl: dict, by_id: str, by_name: str, now: float) -> dict:
    """The panel is still open — refresh the recruiter's stamp WITHOUT a log
    line (the log says "joined" once)."""
    joined = [j for j in ctrl.get("joined") or [] if str(j.get("id")) != str(by_id)]
    joined.append({"id": str(by_id), "name": by_name, "at": now})
    ctrl["joined"] = joined[-10:]
    return ctrl


def recruiter_present(ctrl: dict, now: float) -> bool:
    for j in ctrl.get("joined") or []:
        try:
            if float(now) - float(j.get("at") or 0) < PRESENCE_WINDOW_SEC:
                return True
        except (TypeError, ValueError):
            continue
    return False


def candidate_poll_ms(ctrl: dict, now: float) -> int:
    """How soon the candidate's page should check again."""
    if is_paused(ctrl, now) or ctrl.get("call") or recruiter_present(ctrl, now):
        return FAST_POLL_MS
    return CANDIDATE_POLL_SEC * 1000


def pause(ctrl: dict, by_name: str, reason: str, now: float) -> dict:
    expire_pause(ctrl, now)
    if ctrl.get("paused_at"):
        return ctrl  # already paused — idempotent
    ctrl["paused_at"] = float(now)
    ctrl["state"] = STATE_PAUSED
    ctrl["paused_by"] = by_name
    ctrl["pause_reason"] = _clean_text(reason, MAX_MESSAGE_CHARS)
    ctrl["pause_count"] = int(ctrl.get("pause_count") or 0) + 1
    _log(ctrl, now, by_name, "paused", ctrl["pause_reason"])
    return _bump(ctrl)


def resume(ctrl: dict, by_name: str, now: float) -> dict:
    if not ctrl.get("paused_at"):
        return ctrl
    ctrl["pause_total_sec"] = float(ctrl.get("pause_total_sec") or 0) + running_pause_sec(ctrl, now)
    ctrl["paused_at"] = None
    ctrl["state"] = STATE_RUNNING
    ctrl["message"] = None
    _log(ctrl, now, by_name, "resumed")
    return _bump(ctrl)


def set_message(ctrl: dict, text: str, by_name: str, now: float) -> dict:
    clean = _clean_text(text, MAX_MESSAGE_CHARS)
    ctrl["message"] = {"id": uuid.uuid4().hex[:12], "text": clean, "by": by_name, "at": now} if clean else None
    if clean:
        _log(ctrl, now, by_name, "message", clean)
    return _bump(ctrl)


def add_question(ctrl: dict, text: str, by_name: str, now: float) -> dict:
    clean = _clean_text(text, MAX_QUESTION_CHARS)
    if len(clean) < 5:
        raise LiveError("Type the question you want the candidate to answer (at least 5 characters).")
    pending = list(ctrl.get("questions") or [])
    if len(pending) >= MAX_PENDING_QUESTIONS:
        raise LiveError(f"At most {MAX_PENDING_QUESTIONS} questions can wait at once — resume the interview first.")
    pending.append({"id": uuid.uuid4().hex[:12], "text": clean, "by": by_name, "at": now})
    ctrl["questions"] = pending
    _log(ctrl, now, by_name, "question_added", clean)
    return _bump(ctrl)


def remove_question(ctrl: dict, question_id: str, by_name: str, now: float) -> dict:
    before = list(ctrl.get("questions") or [])
    ctrl["questions"] = [q for q in before if str(q.get("id")) != str(question_id)]
    if len(ctrl["questions"]) != len(before):
        _log(ctrl, now, by_name, "question_removed")
        _bump(ctrl)
    return ctrl


def mark_questions_asked(ctrl: dict, applied: list[dict], now: float) -> dict:
    """Move the questions the candidate's session put into the interview from
    the queue to ``asked`` (with the position they took)."""
    ids = {str(a.get("id")) for a in applied}
    if not ids:
        return ctrl
    ctrl["questions"] = [q for q in ctrl.get("questions") or [] if str(q.get("id")) not in ids]
    ctrl["asked"] = (list(ctrl.get("asked") or []) + [dict(a, asked_at=now) for a in applied])[-30:]
    return _bump(ctrl)


def start_call(ctrl: dict, offer_sdp: str, by_name: str, now: float, *, video: bool = True) -> dict:
    sdp = str(offer_sdp or "")
    if not sdp.startswith("v=") or len(sdp) > MAX_SDP_CHARS:
        raise LiveError("The call could not be started (invalid connection details). Please try again.")
    ctrl["call"] = {"id": uuid.uuid4().hex[:12], "offer": sdp, "by": by_name, "at": now, "video": bool(video)}
    _log(ctrl, now, by_name, "call_started")
    return _bump(ctrl)


def end_call(ctrl: dict, by_name: str, now: float) -> dict:
    if ctrl.get("call"):
        ctrl["call"] = None
        _log(ctrl, now, by_name, "call_ended")
        _bump(ctrl)
    return ctrl


# ---------------------------------------------------------------------------
# Candidate side
# ---------------------------------------------------------------------------

def set_answer(reply: dict, ctrl: dict, call_id: str, answer_sdp: str, now: float) -> dict:
    call = ctrl.get("call") or {}
    if not call or str(call.get("id")) != str(call_id):
        raise LiveError("This call has already ended.")
    sdp = str(answer_sdp or "")
    if not sdp.startswith("v=") or len(sdp) > MAX_SDP_CHARS:
        raise LiveError("Invalid call answer.")
    reply["call_id"] = str(call_id)
    reply["answer"] = sdp
    reply["seen_at"] = now
    return reply


def first_name(name: str) -> str:
    return (str(name or "").strip().split() or ["The recruiter"])[0]


def candidate_view(ctrl: dict, reply: dict, now: float) -> dict:
    """What the candidate's page needs — never the internal log or who else joined."""
    call = ctrl.get("call") or None
    paused = is_paused(ctrl, now)
    view: dict[str, Any] = {
        "rev": int(ctrl.get("rev") or 0),
        "paused": paused,
        "paused_for_sec": int(running_pause_sec(ctrl, now)) if paused else 0,
        "pause_total_sec": round(total_paused_sec(ctrl, now), 1),
        "max_pause_sec": max_pause_sec(),
        "by": first_name(ctrl.get("paused_by") or (call or {}).get("by") or ""),
        "message": (ctrl.get("message") or {}).get("text") or "",
        "message_id": (ctrl.get("message") or {}).get("id") or "",
        "questions_waiting": len(ctrl.get("questions") or []),
        "poll_sec": CANDIDATE_POLL_SEC,
        "poll_ms": candidate_poll_ms(ctrl, now),
        "call": None,
    }
    if call:
        view["call"] = {
            "id": call.get("id"),
            "video": bool(call.get("video", True)),
            "by": first_name(call.get("by") or ""),
            # The offer only until this page has answered it.
            "offer": call.get("offer") if str(reply.get("call_id") or "") != str(call.get("id")) else None,
        }
    return view


def staff_view(ctrl: dict, reply: dict, now: float) -> dict:
    call = ctrl.get("call") or None
    answered = bool(call and str(reply.get("call_id") or "") == str(call.get("id")) and reply.get("answer"))
    seen_at = reply.get("seen_at")
    return {
        "enabled": enabled(),
        "rev": int(ctrl.get("rev") or 0),
        "state": STATE_PAUSED if is_paused(ctrl, now) else STATE_RUNNING,
        "paused": is_paused(ctrl, now),
        "paused_at": ctrl.get("paused_at"),
        "paused_for_sec": int(running_pause_sec(ctrl, now)),
        "pause_total_sec": round(total_paused_sec(ctrl, now), 1),
        "pause_count": int(ctrl.get("pause_count") or 0),
        "paused_by": ctrl.get("paused_by") or "",
        "max_pause_sec": max_pause_sec(),
        "message": ctrl.get("message"),
        "questions": list(ctrl.get("questions") or []),
        "asked": list(ctrl.get("asked") or []),
        "joined": list(ctrl.get("joined") or []),
        "call": ({"id": call.get("id"), "by": call.get("by"), "at": call.get("at"),
                  "video": bool(call.get("video", True)), "answered": answered,
                  "answer": reply.get("answer") if answered else None} if call else None),
        "candidate_seen_at": seen_at,
        "candidate_online": bool(seen_at and (float(now) - float(seen_at)) < CANDIDATE_POLL_SEC * 4),
        "log": list(ctrl.get("log") or [])[-30:],
        "poll_sec": STAFF_POLL_SEC,
        "presence_every_sec": PRESENCE_EVERY_SEC,
        "ice_servers": ice_servers(),
    }


def report_summary(ctrl: dict, now: float) -> dict | None:
    """The report's "Recruiter joined" section — None when nobody did anything."""
    if not ctrl.get("joined") and not ctrl.get("pause_count") and not ctrl.get("asked") and not ctrl.get("log"):
        return None
    return {
        "joined": [{"name": j.get("name"), "at": j.get("at")} for j in ctrl.get("joined") or []],
        "pause_count": int(ctrl.get("pause_count") or 0),
        "paused_total_sec": round(total_paused_sec(ctrl, now), 1),
        "questions": [{"text": q.get("text"), "by": q.get("by"), "index": q.get("index")}
                      for q in ctrl.get("asked") or []],
        "calls": sum(1 for e in ctrl.get("log") or [] if e.get("action") == "call_started"),
        "log": [{"at": e.get("at"), "by": e.get("by"), "action": e.get("action"), "text": e.get("text")}
                for e in (ctrl.get("log") or [])[-40:]],
    }


# ---------------------------------------------------------------------------
# Session (in-memory) side — pure helpers over the session dict
# ---------------------------------------------------------------------------

META_PAUSE_TOTAL = "live_pause_total_sec"
META_PAUSED_AT = "live_paused_at"
META_INSERTED = "live_questions_inserted"
META_INDICES = "live_question_indices"
META_APPLIED = "live_applied_question_ids"


def mirror_clock(meta: dict, ctrl: dict, now: float) -> None:
    """Copy the pause clock onto the session meta (what the elapsed-time
    calculation reads). The completed pauses plus, while paused, the start of
    the running one — capped so the clock never waits past the maximum."""
    meta[META_PAUSE_TOTAL] = float(ctrl.get("pause_total_sec") or 0)
    meta[META_PAUSED_AT] = float(ctrl["paused_at"]) if is_paused(ctrl, now) else None
    if not is_paused(ctrl, now) and ctrl.get("paused_at"):
        # An expired pause that nobody wrote back yet still counts its maximum.
        meta[META_PAUSE_TOTAL] += float(max_pause_sec())


def paused_seconds_from_meta(meta: dict, now: float) -> float:
    try:
        total = float(meta.get(META_PAUSE_TOTAL) or 0)
    except (TypeError, ValueError):
        total = 0.0
    at = meta.get(META_PAUSED_AT)
    if at:
        try:
            total += max(0.0, min(float(now) - float(at), float(max_pause_sec())))
        except (TypeError, ValueError):
            pass
    return total


def questions_inserted(meta: Any) -> int:
    try:
        return max(0, int((meta or {}).get(META_INSERTED) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


def is_recruiter_index(meta: Any, idx: int) -> bool:
    try:
        return int(idx) in {int(i) for i in (meta or {}).get(META_INDICES) or []}
    except (TypeError, ValueError, AttributeError):
        return False


def insert_questions(session: dict, queued: list[dict]) -> list[dict]:
    """Put the recruiter's queued questions into the interview at the current
    turn (after the warm-up when it is the warm-up's turn). Each question is
    inserted once — ids already applied are skipped. Returns what was inserted,
    with the index it took."""
    meta = session.setdefault("meta", {})
    applied_ids = {str(i) for i in meta.get(META_APPLIED) or []}
    todo = [q for q in queued or [] if str(q.get("id")) not in applied_ids and str(q.get("text") or "").strip()]
    if not todo:
        return []
    from services.interview import skill_bank  # local: keep the module import-light
    from services.interview.conversation import followup_indices

    questions = session.setdefault("questions", [])
    idx = max(0, min(int(session.get("current") or 0), len(questions)))
    warm = {int(i) for i in meta.get("warmup_indices") or [] if str(i).lstrip("-").isdigit()}
    while idx in warm:
        idx += 1
    idx = min(idx, len(questions))
    out: list[dict] = []
    for offset, q in enumerate(todo):
        at = idx + offset
        questions.insert(at, str(q["text"]))
        # Everything recorded by index at or after the insert point moves down.
        meta["followup_indices"] = sorted({(i + 1 if i >= at else i) for i in followup_indices(meta)})
        meta[META_INDICES] = sorted({*((int(i) + 1 if int(i) >= at else int(i)) for i in meta.get(META_INDICES) or []), at})
        lead = meta.get("lead_in")
        if isinstance(lead, dict):
            try:
                if int(lead.get("index")) >= at:
                    lead["index"] = int(lead["index"]) + 1
            except (TypeError, ValueError):
                pass
        skill_bank.shift_after_insert(meta, at)
        out.append({"id": str(q["id"]), "text": str(q["text"]), "by": q.get("by") or "", "index": at})
    meta[META_INSERTED] = questions_inserted(meta) + len(out)
    meta[META_APPLIED] = sorted(applied_ids | {o["id"] for o in out})
    return out
