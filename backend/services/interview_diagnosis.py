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

REASON_WORDS.update({
    # 18 Nov 2026
    "page_closed": "the candidate's interview page was closed or reloaded before the last question",
})

STARTED_STATUSES = {"active", "completed", "terminated"}

#: 18 Nov 2026 — every warning family, in words a recruiter can repeat to the
#: candidate: what the page saw (`cause`) and how to avoid it next time (`advice`).
WARNING_HELP: dict[str, dict[str, str]] = {
    "tab": {
        "label": "Tab switch",
        "cause": "The interview tab was hidden — the candidate opened another tab, minimised the browser or switched to another app.",
        "advice": "Keep only the interview tab open and do not minimise the browser.",
    },
    "focus": {
        "label": "Window lost focus",
        "cause": ("The interview window lost focus — a click outside the browser, Alt+Tab / the Windows key, or another app "
                  "taking the screen (a Teams / WhatsApp / Outlook pop-up, an update or antivirus window)."),
        "advice": ("Close Teams, WhatsApp, Outlook and other apps, turn on Do Not Disturb (Windows: Focus assist), "
                   "and do not click outside the browser window."),
    },
    "fullscreen": {
        "label": "Left full screen",
        "cause": "The page left full screen — Esc or F11 was pressed, or another window / a second monitor pulled focus.",
        "advice": "Stay in full screen, do not press Esc / F11, and use ONE screen (disconnect a second monitor).",
    },
    "face": {
        "label": "Another face on camera",
        "cause": "The camera saw a second face.",
        "advice": "Sit alone in a closed room with nobody behind or beside you.",
    },
    "clipboard": {
        "label": "Copy / paste",
        "cause": "Copy or paste was tried.",
        "advice": "Do not copy or paste during the interview.",
    },
    "devtools": {
        "label": "Developer tools",
        "cause": "Browser developer tools were opened (F12 / Ctrl+Shift+I).",
        "advice": "Do not open developer tools or browser extensions during the interview.",
    },
    "gaze": {
        "label": "Looked away",
        "cause": "The camera saw the candidate look away from the screen many times.",
        "advice": "Keep your eyes on the screen; keep notes, phones and a second screen out of reach.",
    },
}


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def warning_list(events: list | None) -> list[dict]:
    """The COUNTED strikes, oldest first (proctor echoes left out), PURE."""
    from services.interview_integrity import EVENT_TYPES, STRIKE_TYPES, event_type, label_for

    out: list[dict] = []
    for ev in events or []:
        t = event_type(ev)
        if t not in STRIKE_TYPES or (isinstance(ev, dict) and ev.get("echo")):
            continue
        fam = EVENT_TYPES.get(t, ("", "other", 0, False))[1]
        out.append({
            "n": len(out) + 1,
            "type": t,
            "label": label_for(t).replace(" (proctor)", ""),
            "family": fam,
            "at": _iso(parse_when(ev.get("timestamp") or ev.get("at_ist"))),
            "question": _txt(ev.get("question")),
            "details": _txt(ev.get("details"))[:160],
        })
    out.sort(key=lambda w: w["at"] or "")
    for i, w in enumerate(out, 1):
        w["n"] = i
    return out


def warning_causes(warnings: list[dict], gaze: int = 0) -> list[dict]:
    """One entry per warning family seen: count, cause, advice (PURE)."""
    counts: dict[str, int] = {}
    for w in warnings:
        counts[w["family"]] = counts.get(w["family"], 0) + 1
    if gaze:
        counts["gaze"] = gaze
    out = []
    for fam, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        help_ = WARNING_HELP.get(fam) or {"label": fam.title(), "cause": "", "advice": ""}
        out.append({"family": fam, "count": n, **help_})
    return out


def _warn_line(w: dict) -> str:
    q = f" (question {w['question']})" if w.get("question") not in ("", None) else ""
    return f"{_clock(parse_when(w.get('at')))} · {w['label']}{q}"


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
    events: list | None = None,
    recording_available: bool | None = None,
    max_warnings: int = 3,
    gaze_limit: int = 10,
) -> dict:
    """The verdict for one interview. `schedule` = the schedule row,
    `progress` = `auth_db.interview_progress_summary`, `link` = the CRM link
    facts (`ai_result`, `ai_score`, `not_attempted`), `prewarm` = the in-memory
    lookup state (lost on restart), `events` = the integrity timeline (proctor
    echoes marked), `recording_available` = what storage holds."""
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

    # 18 Nov 2026 — the warnings and who / what ended the interview.
    warnings = warning_list(events)
    gaze = sum(1 for e in events or [] if isinstance(e, dict) and _txt(e.get("type")) == "gaze_away")
    causes = warning_causes(warnings, gaze)
    term_event = next((e for e in reversed(events or []) if isinstance(e, dict) and _txt(e.get("type")) == "termination"), None)
    term_reason = (_txt(meta.get("termination_reason")) or _txt((term_event or {}).get("reason"))
                   or (_txt(sched.get("reason")) if status == "terminated" else ""))
    by_recruiter = bool(meta.get("terminated_by_recruiter")) or term_reason.lower().startswith("ended by the recruiter") or any(
        isinstance(e, dict) and _txt(e.get("type")) == "recruiter_terminated" for e in events or [])
    terminated = status == "terminated" or final_status == "terminated" or bool(term_reason) or by_recruiter
    if terminated and status != "terminated":
        status = "terminated"  # the schedule row lost it — the session / timeline say it ended so
    via = _txt(meta.get("finalize_via") or meta.get("boundary_saved_via")).lower()
    recruiter_joins = [parse_when(e.get("timestamp")) for e in events or []
                       if isinstance(e, dict) and _txt(e.get("type")) in {"recruiter_joined", "recruiter_call", "recruiter_watch"}]
    recruiter_joins = [t for t in recruiter_joins if t]

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
    if warnings or gaze:
        steps.append({"key": "warnings", "label": "Warnings", "at": warnings[-1]["at"] if warnings else None,
                      "state": "bad" if len(warnings) > max_warnings else ("info" if warnings else "done"),
                      "detail": (f"{len(warnings)} warning(s), {max_warnings} allowed"
                                 + (f"; {gaze} look-away reminder(s)" if gaze else "") + ".")})
    if status in {"completed", "terminated"} or ended:
        how = REASON_WORDS.get(reason, reason.replace("_", " ") if reason else "")
        if reason == "candidate_ended_early" and via == "manual":
            how = "the candidate pressed End Interview before the last question"
        elif reason == "candidate_ended_early" and via == "unload":
            how = REASON_WORDS["page_closed"]
        if status == "terminated":
            how = term_reason or how or "terminated by proctoring"
        steps.append({"key": "ended", "label": "Ended", "at": _iso(ended), "state": "bad" if status == "terminated" or not started else "done",
                      "detail": (how[0].upper() + how[1:] + ".") if how else "Ended."})
    if ai_result or not_attempted:
        verdict = "Not attempted (no scored answer)" if not_attempted else (
            f"{ai_result}" + (f" · {float(ai_score):.0f}%" if ai_score is not None else ""))
        steps.append({"key": "report", "label": "AI report", "at": None,
                      "state": "bad" if not_attempted or ai_result.lower() in {"failed", "rejected"} else "done",
                      "detail": verdict + (" — RMG / GM decide (the AI score is advice)"
                                           if ai_result.lower() == "in review" else "")})
    elif _txt(prog.get("report_status")):
        steps.append({"key": "report", "label": "AI report", "at": None, "state": "info",
                      "detail": f"Report status: {prog.get('report_status')}."})
    rec_words = {"ready": "Saved.", "recording": "Recording in progress.", "missing": "No recording was saved.",
                 "corrupt": "Saved but damaged (first slice lost)."}
    if recording_available and recording not in {"corrupt", "recording"}:
        recording = "ready"  # storage holds the file even when the schedule row was never stamped
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
    if started and status in {"completed", "terminated"} and recording in {"", "missing"} and recording_available is not True:
        flags.append("Signed in but no recording was saved — the camera / screen recorder never uploaded.")
    early = reason in {"candidate_ended_early", "page_closed"} or (status == "terminated" and not by_recruiter)
    if recruiter_joins and ended and early:
        first_join = min(recruiter_joins)
        if first_join <= ended:
            flags.append(f"A recruiter joined this live interview at {_clock(first_join)}. Before 18 Nov 2026, a recruiter "
                         "signed in to the dashboard in the SAME browser as the candidate made the candidate's page reload "
                         "and end the interview (fixed). Test with two devices or two browser profiles.")

    headline, tone, expl, next_steps = _verdict(
        status=status, started=started, signed_in=signed_in, first_q=first_q, answers=answers, reason=reason,
        final_status=final_status, not_attempted=not_attempted, opened=opened, slot=slot, ended=ended,
        last_act=last_act, verified=verified, device_fails=device_fails, progress=bool(prog), now=now, sched=sched, meta=meta,
        warnings=warnings, causes=causes, gaze=gaze, term_reason=term_reason, by_recruiter=by_recruiter, via=via,
        max_warnings=max_warnings, gaze_limit=gaze_limit, questions=questions)

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
    facts["termination_reason"] = term_reason or None
    facts["terminated_by_recruiter"] = by_recruiter
    facts["finalize_via"] = via or None
    facts["warnings_counted"] = len(warnings)
    return {"headline": headline, "tone": tone, "explanation": expl, "next_steps": next_steps,
            "steps": steps, "flags": flags, "facts": facts,
            "warnings": warnings, "causes": causes, "max_warnings": max_warnings,
            "terminated": status == "terminated", "terminated_by_recruiter": by_recruiter}


def _candidate_advice(causes: list[dict]) -> list[str]:
    """What to tell the candidate before the next attempt, one line per cause."""
    return [f"Tell the candidate ({c['label'].lower()} ×{c['count']}): {c['advice']}" for c in causes if c.get("advice")]


def _verdict(*, status, started, signed_in, first_q, answers, reason, final_status, not_attempted, opened, slot,
             ended, last_act, verified, device_fails, progress, now, sched, meta,
             warnings=(), causes=(), gaze=0, term_reason="", by_recruiter=False, via="",
             max_warnings=3, gaze_limit=10, questions=0):
    reschedule = "Ask the TA to send a fresh link: Applied Candidates ▸ Reschedule AI L1 (the old link is closed)."
    warnings = list(warnings or [])
    causes = list(causes or [])
    if status == "terminated":
        if by_recruiter:
            why = term_reason or "Ended by the recruiter"
            return ("Ended by the recruiter", "bad",
                    [f"{why}.", f"{answers} answer(s) were saved before it ended."],
                    ["The recruiter's reason is above; the timeline shows when the recruiter joined and ended it.",
                     "If it was ended by mistake, reschedule the AI L1 with a note."])
        lines: list[str] = []
        looked_away = "looked away" in term_reason.lower() or (gaze_limit and gaze >= gaze_limit and len(warnings) <= max_warnings)
        if looked_away:
            headline = f"Terminated — looked away from the screen {gaze} times"
            lines.append(f"The camera counted {gaze} look-away reminders; the {_ordinal(gaze_limit)} ends the interview.")
        elif len(warnings) > max_warnings:
            last = warnings[max_warnings]  # the warning that crossed the limit
            headline = f"Terminated — {len(warnings)} warnings, {max_warnings} allowed"
            lines.append(f"The {_ordinal(max_warnings + 1)} warning ended the interview at {_clock(parse_when(last['at']))}: "
                         f"{last['label']}" + (f" during question {last['question']}." if last.get("question") else "."))
        else:
            headline = f"Terminated — {term_reason or 'proctoring rules'}"
            lines.append(f"The interview was stopped: {term_reason or 'the proctoring rules'}.")
        if warnings:
            lines.append("Warnings, in order: " + "; ".join(f"{w['n']}. {_warn_line(w)}" for w in warnings) + ".")
        for c in causes:
            if c.get("cause"):
                lines.append(f"{c['label']} ×{c['count']} — {c['cause']}")
        lines.append(f"{answers} answer(s) of {questions or '?'} were saved before it ended; the report scores only those.")
        return (headline, "bad", lines,
                ["Watch the recording at each warning's time (the timeline below) to see what happened.",
                 *_candidate_advice(causes),
                 "If the warnings were not the candidate's fault (a pop-up, a camera glitch), reschedule the AI L1 "
                 "with that note — otherwise decide on what was answered."])
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
        warn_lines: list[str] = []
        if warnings:
            warn_lines.append(f"{len(warnings)} warning(s) during the interview ({max_warnings} allowed — it was NOT ended "
                              "for them): " + "; ".join(_warn_line(w) for w in warnings) + ".")
            warn_lines += [f"{c['label']} ×{c['count']} — {c['cause']}" for c in causes if c.get("cause")]
        early = reason in {"candidate_ended_early", "page_closed"}
        if early:
            of = f"{answers} of {questions}" if questions else f"{answers}"
            if reason == "page_closed" or via == "unload":
                headline = "Ended early — the interview page was closed or reloaded"
                lines = [f"{of} question(s) answered; at {_clock(ended)} the candidate's page closed or reloaded and an older "
                         "version of the page submitted the interview, which closed the link.",
                         "A reload no longer ends an interview (fixed 18 Nov 2026): reopening the same link resumes it."]
            elif via == "manual":
                headline = "Ended early — the candidate pressed End Interview"
                lines = [f"{of} question(s) answered; at {_clock(ended)} the candidate pressed End Interview before the last question."]
            else:
                headline = "Ended early — submitted before the last question"
                lines = [f"{of} question(s) answered; ended at {_clock(ended)} before the last question.",
                         "The page did not say how: End Interview was pressed, or the page was closed / reloaded "
                         "(before 18 Nov 2026 a reload submitted the interview)."]
            steps_ = ["Call the candidate and ask what happened; watch the end of the recording.",
                      *_candidate_advice(causes), reschedule + " Add the reason as the note."]
            return (headline, "warn", lines + warn_lines, steps_)
        lines = [f"{answers} answer(s) saved; ended at {_clock(ended)}: {REASON_WORDS.get(reason, reason or 'submitted')}."]
        if not_attempted:
            lines.append("No scored answer — only the warm-up was answered, so the report reads \"Not attempted\".")
            return ("Answered only the warm-up", "warn", lines + warn_lines, [reschedule])
        return ("Completed" + (" — with warnings" if warnings else ""), "warn" if len(warnings) >= max_warnings else "ok",
                lines + warn_lines,
                ["Open the candidate report for the scores and the recording.",
                 *(["Watch the recording at each warning's time before deciding."] if warnings else [])])
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
