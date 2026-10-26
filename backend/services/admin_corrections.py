"""Undoing a screener's mistake, and making an AI L1 decision move the candidacy
(23 Oct 2026, user asks).

1. "If by mistake any RMG / GM [decides wrongly] at Technical Screening or the
   manual round, Admin can change it from his login — or similar things."
   * A round verdict changed from "No Hire" to anything else REOPENS the
     candidacy the "No Hire" closed (`reopen_on_corrected_verdict`, called by
     `apply_round_verdict`, so it works from every feedback form — Admin may
     already edit every round).
   * `admin_correct` is Admin / CEO's general undo: put the candidacy on any
     stage, put the RMG screening back to Pending, clear an AI L1 decision,
     restore an archived row — always with a reason, always logged as
     `ADMIN_CORRECTION`.

2. "His AI interview feedback, done manually by RMG / GM: Selected or Rejected.
   If Rejected, TA gets Archive; if Selected, the next round (L2, Submit to
   Sales)."
   * `close_on_ai_rejection`: a "Rejected" decision on the AI L1 closes the
     candidacy as RMG Rejected — a closed row is what offers TA the Archive.
   * `forward_on_ai_selection`: "Selected" hands the candidate to RMG Review,
     where the L2 request and Submit to Sales live.
   Both run from the row's decision route AND from the report page's
   Shortlist / Reject (`ai_interview_bridge.sync_hr_decision`).

Every function here leaves the commit to the caller.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import CandidateProfile, CandidateProfileActivityLog, PipelineStatus
from services.crm_common import log_activity

logger = logging.getLogger("karnex.crm.admin_corrections")

PS = PipelineStatus

#: Written on every Admin correction and every reopened verdict.
ADMIN_CORRECTION = "ADMIN_CORRECTION"
VERDICT_CORRECTED = "VERDICT_CORRECTED"
#: An AI L1 rejection closes the candidacy from these stages (before the
#: candidate reached Sales). Later stages belong to Sales / the customer.
AI_REJECT_FROM = (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value, PS.RMG_REVIEW.value)
#: …and only these are healed lazily for older rows (RMG Review may hold an
#: L2 already under way; there the decision is RMG's to take on the row).
AI_HEAL_FROM = (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value)
#: An Admin correction needs a reason at least this long.
MIN_REASON = 10

_CHANGE_RE = re.compile(r"^\s*([A-Za-z_]+)\s*->\s*([A-Za-z_]+)\s*:?")


def _status(profile) -> str:
    raw = profile.pipeline_status
    return getattr(raw, "value", None) or str(raw or "")


def _words(status: str) -> str:
    return (status or "").replace("_", " ")


def system_actor(name: str | None = None):
    """A stand-in user for moves the report page sets off (no CRM login)."""
    return SimpleNamespace(id=None, full_name=(name or "").strip() or None,
                           username=(name or "").strip() or None, email=None)


def parse_change(comment: str | None) -> tuple[str, str] | None:
    """("Technical_Screening", "RMG_Rejected") from a STATUS_CHANGE comment. PURE."""
    m = _CHANGE_RE.match(comment or "")
    return (m.group(1), m.group(2)) if m else None


def _last_move_into(db: Session, profile_id: int, target: str) -> str | None:
    """The stage the candidacy was at when it last moved INTO `target`."""
    rows = db.execute(
        select(CandidateProfileActivityLog.comment)
        .where(CandidateProfileActivityLog.profile_id == profile_id,
               CandidateProfileActivityLog.action_type == "STATUS_CHANGE")
        .order_by(CandidateProfileActivityLog.timestamp.desc(), CandidateProfileActivityLog.id.desc())
        .limit(40)
    ).scalars().all()
    for comment in rows:
        pair = parse_change(comment)
        if pair and pair[1] == target and pair[0] != target:
            return pair[0]
    return None


# ---------------------------------------------------------------------------
# 1a. A corrected "No Hire" reopens the candidacy
# ---------------------------------------------------------------------------

def reopen_on_corrected_verdict(db: Session, profile: CandidateProfile, event, user,
                                previous_result: str | None) -> str | None:
    """The round's verdict WAS "No Hire" (which closed the candidacy) and is now
    something else → the candidacy goes back to the stage it closed from.

    Only when the profile is still exactly where that "No Hire" put it, so a
    later, unrelated move is never undone. Never raises. Returns the stage it
    reopened at, else None.
    """
    from services.candidate_profiles import NEGATIVE_ROUND_RESULTS, _ROUND_REJECTS_TO, round_label_for

    try:
        was = (previous_result or "").strip()
        now = (getattr(event, "result", None) or "").strip()
        if was not in NEGATIVE_ROUND_RESULTS or now in NEGATIVE_ROUND_RESULTS:
            return None
        target = _ROUND_REJECTS_TO.get(getattr(event, "kind", "") or "")
        current = _status(profile)
        if not target or current != target:
            return None
        back_to = _last_move_into(db, profile.id, target)
        if not back_to or back_to not in {s.value for s in PS}:
            return None
        label = round_label_for(event.kind)
        who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Someone"
        verdict = now or "no verdict"
        with db.begin_nested():
            profile.pipeline_status = PS(back_to)
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id,
                         getattr(user, "id", None), "STATUS_CHANGE",
                         f"{current} -> {back_to}: {label} verdict corrected from No Hire to {verdict}")
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id,
                         getattr(user, "id", None), VERDICT_CORRECTED,
                         f"{who} changed the {label} verdict from No Hire to {verdict} — "
                         f"the candidacy is reopened at {_words(back_to)}")
            _lift_archive(db, profile, user, "verdict corrected")
        return back_to
    except Exception:  # pragma: no cover — never break saving feedback
        logger.warning("reopen on corrected verdict failed for profile %s", profile.id, exc_info=True)
        return None


def _lift_archive(db: Session, profile: CandidateProfile, user, why: str) -> bool:
    """Bring a manually archived row back to the live list."""
    from services.candidate_status import RESTORED_ACTION, archived_profile_ids

    if profile.id not in archived_profile_ids(db, [profile.id]):
        return False
    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Admin"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, getattr(user, "id", None),
                 RESTORED_ACTION, f"{who} restored the candidate to Applied Candidates ({why})")
    return True


# ---------------------------------------------------------------------------
# 2. The AI L1 decision moves the candidacy
# ---------------------------------------------------------------------------

def close_on_ai_rejection(db: Session, profile: CandidateProfile, user, note: str = "") -> str | None:
    """RMG / GM (or a recruiter on the report page) REJECTED the candidate on the
    AI L1 → the candidacy closes as RMG Rejected and the TAs hear about it. A
    closed row is what offers TA Archive / Re-apply. Returns the new status, or
    None when the candidate is past the stages an AI L1 decides."""
    from services.candidate_profiles import _notify_rejection, _stamp_workflow_dates

    current = _status(profile)
    if current not in AI_REJECT_FROM:
        return None
    target = PS.RMG_REJECTED.value
    who = getattr(user, "full_name", None) or getattr(user, "username", None)
    reason = "AI L1 rejected" + (f" by {who}" if who else "") + (f" — {note.strip()[:200]}" if (note or "").strip() else "")
    with db.begin_nested():
        profile.pipeline_status = PS(target)
        _stamp_workflow_dates(db, profile, target)
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, getattr(user, "id", None),
                     "STATUS_CHANGE", f"{current} -> {target}: {reason}")
    _notify_rejection(db, profile, target, reason, user)
    return target


def forward_on_ai_selection(db: Session, profile: CandidateProfile, user, note: str = "") -> str | None:
    """"Selected" on the AI L1 → RMG Review (where L2 / Submit to Sales live)."""
    from services.candidate_profiles import hand_off_to_rmg_review

    if _status(profile) not in AI_HEAL_FROM:
        return None
    who = getattr(user, "full_name", None) or getattr(user, "username", None)
    return hand_off_to_rmg_review(
        db, profile, user,
        "AI L1 selected" + (f" by {who}" if who else "") + (f" — {note.strip()[:200]}" if (note or "").strip() else ""))


def apply_ai_decision_to_pipeline(db: Session, profile_id: int | None, decision: str | None,
                                  decided_by: str | None = None) -> str | None:
    """The report page's Shortlist / Reject reaches the pipeline (called by
    `sync_hr_decision`). Savepointed; never raises."""
    if not profile_id or decision not in ("selected", "rejected"):
        return None
    try:
        profile = db.get(CandidateProfile, profile_id)
        if profile is None:
            return None
        actor = system_actor(decided_by)
        with db.begin_nested():
            if decision == "rejected":
                return close_on_ai_rejection(db, profile, actor)
            return forward_on_ai_selection(db, profile, actor)
    except Exception:  # pragma: no cover
        logger.warning("AI decision → pipeline failed for profile %s", profile_id, exc_info=True)
        return None


def heal_ai_rejected(db: Session, links_by_profile: dict) -> set[int]:
    """Older rows: a REJECTED AI L1 decision left the candidacy at Sourcing /
    Technical Screening (the screenshot of 23 Oct 2026 — "Rejected" chip, no
    Archive). Close them now, unless someone reopened the candidacy AFTER the
    decision (Re-apply, an Admin correction) — that later call wins.

    `links_by_profile`: {profile id: its latest AiInterviewLink}. Returns the
    profile ids closed. Savepointed; the caller commits."""
    from services.candidate_profiles import REAPPLIED_ACTION

    wanted = {pid: link for pid, link in (links_by_profile or {}).items()
              if pid and getattr(link, "hr_decision", None) == "rejected"
              and not getattr(link, "voided_at", None)}
    if not wanted:
        return set()
    closed: set[int] = set()
    try:
        with db.begin_nested():
            reopened = dict(db.execute(
                select(CandidateProfileActivityLog.profile_id, func.max(CandidateProfileActivityLog.timestamp))
                .where(CandidateProfileActivityLog.profile_id.in_(list(wanted)),
                       CandidateProfileActivityLog.action_type.in_(
                           (REAPPLIED_ACTION, ADMIN_CORRECTION, VERDICT_CORRECTED)))
                .group_by(CandidateProfileActivityLog.profile_id)
            ).all())
            profiles = db.execute(select(CandidateProfile).where(
                CandidateProfile.id.in_(list(wanted)))).scalars().all()
            for profile in profiles:
                if _status(profile) not in AI_HEAL_FROM:
                    continue
                link = wanted[profile.id]
                later = reopened.get(profile.id)
                decided = getattr(link, "hr_decision_at", None)
                if later is not None and (decided is None or _naive(later) >= _naive(decided)):
                    continue
                if close_on_ai_rejection(db, profile, system_actor(getattr(link, "hr_decision_by", None))):
                    closed.add(profile.id)
    except Exception:  # pragma: no cover — the list must still load
        logger.warning("AI-rejected heal failed", exc_info=True)
    return closed


def latest_ai_link(db: Session, profile_id: int):
    """The candidacy's newest AI L1 link (the one every screen reads)."""
    from models import AiInterviewLink
    return db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile_id)
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
    ).scalars().first()


def _naive(dt):
    from datetime import timezone
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


# ---------------------------------------------------------------------------
# 1b. Admin / CEO's general correction
# ---------------------------------------------------------------------------

def correction_options(db: Session, profile: CandidateProfile) -> dict:
    """What the Admin correction dialog needs: where the candidacy is, the last
    moves (so the right one can be undone), the screening and AI decision."""
    from services.candidate_profiles import RMG_SCREENING_VALUES, TERMINAL_STATUSES
    from services.candidate_status import archived_profile_ids

    current = _status(profile)
    rows = db.execute(
        select(CandidateProfileActivityLog)
        .where(CandidateProfileActivityLog.profile_id == profile.id,
               CandidateProfileActivityLog.action_type == "STATUS_CHANGE")
        .order_by(CandidateProfileActivityLog.timestamp.desc(), CandidateProfileActivityLog.id.desc())
        .limit(12)
    ).scalars().all()
    from services.revenue_report import _user_names
    names = _user_names(db, {r.user_id for r in rows if getattr(r, "user_id", None)})
    moves = []
    for r in rows:
        pair = parse_change(r.comment)
        if not pair:
            continue
        note = (r.comment or "").split(":", 1)[1].strip() if ":" in (r.comment or "") else ""
        moves.append({"from": pair[0], "to": pair[1], "note": note[:300],
                      "by": names.get(getattr(r, "user_id", None)),
                      "at": r.timestamp.isoformat() if r.timestamp else None})
    undo = next((m["from"] for m in moves if m["to"] == current and m["from"] != current), None)
    link = latest_ai_link(db, profile.id)
    return {
        "current": current,
        "closed": current in TERMINAL_STATUSES,
        "stages": [s.value for s in PS],
        "undo_to": undo,
        "moves": moves,
        "screening": profile.rmg_screening_status,
        "screening_values": list(RMG_SCREENING_VALUES),
        "ai_decision": getattr(link, "hr_decision", None) if link else None,
        "ai_result": getattr(link, "result", None) if link else None,
        "archived": profile.id in archived_profile_ids(db, [profile.id]),
        "min_reason": MIN_REASON,
    }


def admin_correct(db: Session, profile: CandidateProfile, user, *, target_status: str | None,
                  screening: str | None, clear_ai_decision: bool, restore: bool,
                  note: str) -> list[str]:
    """Apply an Admin / CEO correction. Returns what changed, in words (400 when
    nothing would change or the reason is short). The caller commits."""
    from services.candidate_profiles import (
        RMG_SCREENING_PENDING, RMG_SCREENING_VALUES, TA_HOLD, _stamp_workflow_dates,
    )

    note = (note or "").strip()
    if len(note) < MIN_REASON:
        raise HTTPException(status_code=400,
                            detail=f"Say what was wrong and why it is being corrected (at least {MIN_REASON} characters).")
    uid = getattr(user, "id", None)
    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "Admin"
    done: list[str] = []
    current = _status(profile)

    if target_status:
        if target_status not in {s.value for s in PS}:
            raise HTTPException(status_code=400, detail=f"Unknown stage '{target_status}'.")
        if target_status != current:
            profile.pipeline_status = PS(target_status)
            if target_status != PS.SELF_WITHDRAWN.value:
                profile.withdrawn_from_status = None
            if profile.budget_status == TA_HOLD and target_status not in (PS.SOURCING.value,):
                profile.budget_status = None
            _stamp_workflow_dates(db, profile, target_status)
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, "STATUS_CHANGE",
                         f"{current} -> {target_status}: Admin correction — {note}")
            done.append(f"stage {_words(current)} → {_words(target_status)}")

    if screening is not None:
        wanted = None if screening in ("", "none") else screening
        if wanted is not None and wanted not in RMG_SCREENING_VALUES:
            raise HTTPException(status_code=400, detail=f"Unknown screening value '{screening}'.")
        if (profile.rmg_screening_status or None) != wanted:
            before = profile.rmg_screening_status or "not sent"
            profile.rmg_screening_status = wanted
            profile.rmg_screening_note = None
            if wanted in (None, RMG_SCREENING_PENDING):
                profile.rmg_screening_by = None
                profile.rmg_screening_at = None
            done.append(f"RMG screening {before} → {wanted or 'not sent'}")

    if clear_ai_decision:
        link = latest_ai_link(db, profile.id)
        if link is not None and link.hr_decision:
            label = link.hr_decision.replace("_", " ")
            link.hr_decision = None
            link.hr_decision_by = None
            link.hr_decision_at = None
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, "AI_INTERVIEW_DECISION",
                         f"AI interview decision ({label}) cleared by {who} — AI verdict stands: {link.result}")
            done.append(f"AI L1 decision '{label}' cleared")

    if restore and _lift_archive(db, profile, user, "Admin correction"):
        done.append("restored from Archive")

    if not done:
        raise HTTPException(status_code=400, detail="Nothing to change — pick a stage, the screening or the AI decision.")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, uid, ADMIN_CORRECTION,
                 f"{who} corrected: " + "; ".join(done) + f" — {note}")
    return done
