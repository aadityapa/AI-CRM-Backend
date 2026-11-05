"""What happened in one AI interview, in plain words (5 Nov 2026).

Why this exists
---------------
The Integrity tab showed a COMPLETED interview with "Started —", "Finished
01:54 pm" (before the 03:00 pm slot), "Failed 0 %" and no recording, and
nobody could tell why. The answer was spread over four stores — the schedule
row, the progress row's meta, the device-check log and the CRM link — and one
in-memory dict. `diagnose()` reads them together and returns:

* `headline` / `tone` / `explanation` / `next_steps` — the verdict a recruiter
  can act on;
* `steps` — the journey: scheduled → link opened → device check → verified →
  signed in → first question → answers → ended → report → recording, each
  with its time (or what is missing);
* `flags` — anything odd (ended before the slot, a report that failed …);
* `facts` — the raw values, for whoever debugs further.

`session_never_started()` is the rule the recovery worker uses so it never
closes a session that was only PREPARED by the invite lookup (the bug the
reported case hit). Everything here is PURE; `main.py` reads the stores.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from services.device_check import reason_words as _device_reason_words

IST = timezone(timedelta(hours=5, minutes=30))

#: How a session ended, by `meta.finalization_reason`.
REASON_WORDS: dict[str, str] = {
    "candidate_submitted": "the candidate finished and submitted",
    "time_expired": "the time limit ran out",
    "candidate_ended_early": "the candidate ended the interview early",
    "background_finalize": "the candidate submitted (report built in the background)",
    "abandoned_without_answers": (
        "the recovery job closed it automatically — the session sat idle for 60 minutes with no answer"),
    "auto_recovered_after_inactivity": (
        "the recovery job closed it automatically — the candidate stopped answering for 35+ minutes"),
    "terminated_recovery": "it had been terminated; the recovery job wrote the report",
    "stale_active_recovery": "the clean-up closed it — signed in, but never submitted",
}

STARTED_STATUSES = {"active", "completed", "terminated"}


def _txt(value: Any) -> str:
    return str(value or "").strip()


def parse_when(value: Any) -> datetime | None:
    """ISO with or without a zone (a bare one is IST), "YYYY-MM-DD HH:MM", or an
    epoch number — as an aware IST datetime; None when unreadable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.replace(".", "", 1).isdigit()):
        try:
            num = float(value)
            if num <= 0:
                return None
            return datetime.fromtimestamp(num, tz=timezone.utc).astimezone(IST)
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    text = _txt(value).replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T", 1)):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return (dt.replace(tzinfo=IST) if dt.tzinfo is None else dt).astimezone(IST)
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _clock(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.strftime("%d %b, %I:%M %p").replace(" 0", " ").lstrip("0")


def _span(minutes: float) -> str:
    m = int(round(abs(minutes)))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h {r} min" if r else f"{h} h"


def session_never_started(session: dict | None, schedule: dict | None) -> bool:
    """True when the candidate never signed in or saw a question — the session
    exists only because the invite lookup PREPARED it. No answers, no
    `interview_started_epoch`, and the schedule (when known) shows no sign-in."""
    sess = session or {}
    meta = sess.get("meta") or {}
    if sess.get("answers"):
        return False
    if meta.get("interview_started_epoch") or meta.get("startup_login_accepted_at_utc"):
        return False
    if schedule is None:
        return False  # not an invite we can check — keep the old behaviour
    if _txt(schedule.get("interview_started_at")):
        return False
    return _txt(schedule.get("session_status")).lower() not in STARTED_STATUSES


def _device_step(log: list | None, reason_words) -> dict:
    events = [e for e in (log or []) if isinstance(e, dict) and e.get("check") == "screen_share"]
    if not events:
        return {"key": "device", "label": "Device check (screen share)", "at": None, "state": "missing",
                "detail": "No result reported by the candidate's page."}
    fails = [e for e in events if not e.get("ok")]
    last = events[-1]
    at = _iso(parse_when(last.get("timestamp")))
    if last.get("ok"):
        extra = f" after {len(fails)} failed attempt(s)" if fails else ""
        return {"key": "device", "label": "Device check (screen share)", "at": at, "state": "done",
                "detail": f"Screen shared{extra}."}
    return {"key": "device", "label": "Device check (screen share)", "at": at, "state": "bad",
            "detail": f"{len(fails)} failed attempt(s); last: {reason_words(_txt(last.get('reason')), _txt(last.get('surface')))}."}


def diagnose(
    schedule: dict,
    progress: dict | None = None,
    *,
    device_log: list | None = None,
    link: dict | None = None,
    prewarm: dict | None = None,
    now: datetime | None = None,
    reason_words=None,
) -> dict:
    """The verdict for one interview. `schedule` = the schedule row,
    `progress` = `auth_db.interview_progress_summary`, `link` = the CRM link
    facts (`ai_result`, `ai_score`, `not_attempted`), `prewarm` = the in-memory
    lookup state (lost on restart)."""
    reason_words = reason_words or _device_reason_words
    now = (now or datetime.now(IST)).astimezone(IST)
    sched = schedule or {}
    prog = progress or {}
    meta = prog.get("meta") or {}
    link = link or {}
    prewarm = prewarm or {}
    status = _txt(sched.get("session_status") or "pending").lower()

    slot = parse_when(sched.get("scheduled_at_local") or sched.get("scheduled_at"))
    opened = parse_when(prog.get("created_at_ist")) or parse_when(prewarm.get("started_at"))
    verified = parse_when(sched.get("verified_at"))
    signed_in = parse_when(sched.get("interview_started_at")) or parse_when(meta.get("startup_login_accepted_at_utc"))
    first_q = parse_when(meta.get("interview_started_epoch"))
    ended = parse_when(sched.get("interview_completed_at")) or parse_when(prog.get("finalized_at"))
    last_act = parse_when(prog.get("last_activity_at"))
    answers = int(prog.get("answers_count") or 0)
    questions = int(prog.get("questions_count") or 0)
    reason = _txt(meta.get("finalization_reason"))
    final_status = _txt(meta.get("final_status")).lower()
    started = bool(signed_in or first_q or answers)
    not_attempted = bool(link.get("not_attempted"))
    ai_result = _txt(link.get("ai_result"))
    ai_score = link.get("ai_score")
    recording = _txt(sched.get("recording_status")).lower()
    device = _device_step(device_log, reason_words)
    device_fails = sum(1 for e in device_log or [] if isinstance(e, dict) and e.get("check") == "screen_share" and not e.get("ok"))

    steps: list[dict] = [
        {"key": "scheduled", "label": "Scheduled", "at": _iso(slot), "state": "done" if slot else "missing",
         "detail": "The link opens from this time." if slot else "No time on the schedule."},
        {"key": "opened", "label": "Link opened", "at": _iso(opened), "state": "done" if opened else "missing",
         "detail": ("The app prepared the questions when the candidate opened the link."
                    + (f" {_span((slot - opened).total_seconds() / 60)} before the slot." if opened and slot and opened < slot else ""))
         if opened else "No record that the link was opened."},
        device,
        {"key": "verified", "label": "Identity verified", "at": _iso(verified), "state": "done" if verified else "missing",
         "detail": "Email / access key accepted." if verified else
         "Not verified (before the slot the page only shows a countdown — nothing is recorded)."},
        {"key": "signed_in", "label": "Signed in to the interview", "at": _iso(signed_in),
         "state": "done" if signed_in else "missing",
         "detail": "Interview session started." if signed_in else "The candidate never signed in."},
        {"key": "first_question", "label": "First question served", "at": _iso(first_q),
         "state": "done" if first_q else "missing",
         "detail": "The interview clock started here." if first_q else "No question was ever shown."},
        {"key": "answers", "label": "Answers", "at": _iso(last_act) if answers else None,
         "state": "done" if answers else ("missing" if status in {"completed", "terminated"} or started else "info"),
         "detail": f"{answers} answered of {questions} question(s) prepared." if questions or answers else "No answers."},
    ]
    if status in {"completed", "terminated"} or ended:
        how = REASON_WORDS.get(reason, reason.replace("_", " ") if reason else "")
        if status == "terminated":
            how = _txt(meta.get("termination_reason")) or _txt(sched.get("reason")) or how or "terminated by proctoring"
        steps.append({"key": "ended", "label": "Ended", "at": _iso(ended), "state": "bad" if status == "terminated" or not started else "done",
                      "detail": (how[0].upper() + how[1:] + ".") if how else "Ended."})
    if ai_result or not_attempted:
        verdict = "Not attempted (no scored answer)" if not_attempted else (
            f"{ai_result}" + (f" · {float(ai_score):.0f}%" if ai_score is not None else ""))
        steps.append({"key": "report", "label": "AI report", "at": None,
                      "state": "bad" if not_attempted or ai_result.lower() == "failed" else "done", "detail": verdict})
    elif _txt(prog.get("report_status")):
        steps.append({"key": "report", "label": "AI report", "at": None, "state": "info",
                      "detail": f"Report status: {prog.get('report_status')}."})
    rec_words = {"ready": "Saved.", "recording": "Recording in progress.", "missing": "No recording was saved.",
                 "corrupt": "Saved but damaged (first slice lost)."}
    steps.append({"key": "recording", "label": "Recording", "at": None,
                  "state": {"ready": "done", "recording": "info", "missing": "missing", "corrupt": "bad"}.get(recording, "missing"),
                  "detail": rec_words.get(recording, "Nothing was recorded" + ("." if started else " — recording starts after sign-in."))})

    flags: list[str] = []
    if ended and slot and ended < slot:
        flags.append(f"Ended {_span((slot - ended).total_seconds() / 60)} BEFORE the scheduled time.")
    if _txt(prog.get("report_error")):
        flags.append(f"Report note: {prog.get('report_error')}")
    if int(prog.get("recovery_attempts") or 0):
        flags.append(f"The recovery job touched this session {prog.get('recovery_attempts')} time(s).")
    if device_fails:
        flags.append(f"Screen share failed {device_fails} time(s) on the Device Check.")
    if started and status == "completed" and recording in {"", "missing"}:
        flags.append("Signed in but no recording was saved — the camera / screen recorder never uploaded.")

    headline, tone, expl, next_steps = _verdict(
        status=status, started=started, signed_in=signed_in, first_q=first_q, answers=answers, reason=reason,
        final_status=final_status, not_attempted=not_attempted, opened=opened, slot=slot, ended=ended,
        last_act=last_act, verified=verified, device_fails=device_fails, progress=bool(prog), now=now, sched=sched, meta=meta)

    facts = {
        "session_status": status,
        "progress_status": prog.get("status"),
        "interview_id": prog.get("interview_id"),
        "finalization_reason": reason or None,
        "final_status": final_status or None,
        "report_status": prog.get("report_status") or None,
        "report_error": prog.get("report_error") or None,
        "recovery_attempts": prog.get("recovery_attempts"),
        "answers": answers,
        "questions": questions,
        "current_index": prog.get("current_index"),
        "login_attempts_failed": int(sched.get("login_attempts") or 0),
        "device_bound": bool(_txt(sched.get("active_device_id"))),
        "recording_status": recording or None,
        "ai_result": ai_result or None,
        "ai_score": ai_score,
        "not_attempted": not_attempted,
        "fast_bootstrap": meta.get("fast_bootstrap"),
        "prewarm": {k: prewarm.get(k) for k in ("status", "reason", "started_at", "error") if prewarm.get(k)} or None,
    }
    return {"headline": headline, "tone": tone, "explanation": expl, "next_steps": next_steps,
            "steps": steps, "flags": flags, "facts": facts}


def _verdict(*, status, started, signed_in, first_q, answers, reason, final_status, not_attempted, opened, slot,
             ended, last_act, verified, device_fails, progress, now, sched, meta):
    reschedule = "Ask the TA to send a fresh link: Applied Candidates ▸ Reschedule AI L1 (the old link is closed)."
    if status == "terminated":
        why = _txt(meta.get("termination_reason")) or _txt(sched.get("reason")) or "the proctoring rules"
        return ("Ended by proctoring", "bad",
                [f"The interview was stopped: {why}.",
                 f"{answers} answer(s) were saved before it ended."],
                ["Open the timeline below to see each warning, then watch the recording at those times.",
                 "If the warnings were unfair (e.g. a camera glitch), reschedule the AI L1 with a note."])
    if status == "completed" and not started:
        lines = []
        if opened:
            lines.append(f"The candidate opened the link at {_clock(opened)}"
                         + (f", {_span((slot - opened).total_seconds() / 60)} before the slot ({_clock(slot)})." if slot and opened < slot else "."))
        lines.append("They never signed in and no question was ever shown"
                     + (" (the page only shows a countdown before the slot)." if slot and opened and opened < slot else "."))
        if reason == "abandoned_without_answers":
            lines.append(f"At {_clock(ended)} the recovery job saw the prepared session idle for 60 minutes and closed it "
                         "as \"abandoned\" — that closed the link and produced an empty 0 % report.")
            lines.append("This was a system fault (fixed 5 Nov 2026): a session that was only prepared is now left open.")
        else:
            lines.append(f"It was closed at {_clock(ended)}: {REASON_WORDS.get(reason, reason or 'no reason recorded')}.")
        return ("Closed by the system before the candidate started", "bad", lines, [reschedule])
    if status == "completed" and answers == 0:
        return ("Signed in but gave no answer", "warn",
                [f"Signed in at {_clock(signed_in)}" + (f", first question at {_clock(first_q)}." if first_q else ", but no question was served."),
                 f"Ended at {_clock(ended)}: {REASON_WORDS.get(reason, reason or 'no reason recorded')}."],
                ["Watch the recording (if any) to see what the candidate saw.", reschedule])
    if status == "completed" and reason == "auto_recovered_after_inactivity":
        return ("Left mid-interview — closed automatically", "warn",
                [f"{answers} answer(s) saved; the last activity was {_clock(last_act)}.",
                 "The candidate never pressed Submit; the recovery job closed it after 35+ minutes of silence "
                 "and scored what was there."],
                ["Check the timeline for a tab switch / browser close near the last activity.",
                 "Reschedule if the candidate had a technical problem."])
    if status == "completed":
        lines = [f"{answers} answer(s) saved; ended at {_clock(ended)}: {REASON_WORDS.get(reason, reason or 'submitted')}."]
        if not_attempted:
            lines.append("No scored answer — only the warm-up was answered, so the report reads \"Not attempted\".")
            return ("Answered only the warm-up", "warn", lines, [reschedule])
        return ("Completed", "ok", lines, ["Open the candidate report for the scores and the recording."])
    if status == "active":
        idle = (now - last_act).total_seconds() / 60 if last_act else None
        if idle is not None and idle >= 35:
            return ("In progress — but no activity", "warn",
                    [f"Signed in at {_clock(signed_in)}; {answers} answer(s); nothing for {_span(idle)}.",
                     "If nothing happens, the recovery job closes it and scores what was answered."],
                    ["Join the interview from this row to see the candidate's screen."])
        return ("In progress now", "info", [f"Signed in at {_clock(signed_in)}; {answers} answer(s) so far."],
                ["Watch live or join from this row."])
    # pending / verified / scheduled — not started yet
    lines: list[str] = []
    before = bool(slot and now < slot)
    if before:
        lines.append(f"The link opens at {_clock(slot)} ({_span((slot - now).total_seconds() / 60)} from now).")
    if opened:
        lines.append(f"The candidate opened the link at {_clock(opened)}.")
    if verified:
        lines.append(f"Identity verified at {_clock(verified)}, not signed in yet.")
    if device_fails:
        lines.append(f"The Device Check's screen share failed {device_fails} time(s) — see the timeline for why.")
        return ("Stuck at the Device Check", "warn", lines,
                ["Tell the candidate to choose “Entire screen” and press Share; on a Mac allow Screen Recording "
                 "for the browser in System Settings ▸ Privacy."])
    if not opened and not verified:
        lines.append("No sign that the candidate has opened the link.")
        return ("Waiting for the slot" if before else "Not opened yet", "info", lines,
                ["Check the invite email reached the candidate (CRM ▸ Emails)."])
    return ("Opened the link — not started yet", "info", lines,
            ["Nothing is wrong: the interview starts when the candidate signs in after the slot time."])
