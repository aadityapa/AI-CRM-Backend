"""AI interview sessions scoped to a candidate profile (Phase 5).

Backs the "AI Interview" tab on the profile detail page: schedule a session with
a date/time and the candidate details, email the invite, reschedule or cancel a
session booked by mistake, and deep-link to the full report in the admin
dashboard (?view=candidateReport&cid=<email>&iid=<record_id>).

Only PENDING, not-yet-started sessions can be edited or cancelled — once the
candidate has begun, the session is an audit record and stays put.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, gated_read, gated_write, get_crm_db
from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Opportunity,
    Requirement,
)
from models.ai_links import hr_decision_label
from schemas.common import envelope
from services.ai_interview_bridge import (
    ai_interview_autosend_enabled, ensure_l1_template_ready, l1_template_status,
    schedule_l1_interview,
)
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity, to_dict
from services.report_links import ai_report_link

router = APIRouter(prefix="/api/candidate-profiles", tags=["CRM: AI Interviews"])

VIEW_ROLES = ("TA", "RMG", "Sales", "Sales_Head", "HR")
#: AI L1 is TA's step in the pipeline — they source the candidate, run the ATS
#: scan and trigger the interview; a pass then hands the candidate to RMG.
#: Scheduling from a resume was already TA-only (routers/crm/resumes.py), but
#: the profile page let RMG and Sales trigger one too, so the same action had
#: two different answers depending on which screen you were looking at.
#: RMG added 2 Sep 2026: RMG chooses the interview ROUTE (AI L1 vs a human
#: L1) from the Applied Candidates row, so taking the AI route must be theirs
#: to click too. The RMG screening gate still applies inside.
TRIGGER_ROLES = ("TA", "RMG")


class AiInterviewCreate(BaseModel):
    """All fields optional — an empty body reproduces the old one-click behaviour
    (schedule for now, show the link, email only if AI_INTERVIEW_AUTOSEND is on)."""
    scheduled_at: str | None = Field(
        default=None,
        description='Interview date/time, "YYYY-MM-DD HH:MM" in the recruiter\'s local time')
    candidate_name: str | None = None
    candidate_email: str | None = None
    notes: str | None = None
    #: None = follow the AI_INTERVIEW_AUTOSEND default; True/False = explicit override.
    send_email: bool | None = None
    #: Required when the profile already has a FINISHED AI L1 (not attempted /
    #: failed / terminated): why a fresh link is being sent — "candidate
    #: confirmed on the phone they will sit it on Thursday". Logged as
    #: AI_INTERVIEW_RESCHEDULED beside the previous outcome (7 Oct 2026).
    reschedule_note: str | None = None
    #: 8 Oct 2026: set aside the previous AI L1 — even a PASS — because it ran
    #: on the wrong template / was not a fair test of this role. Needs a reason
    #: of at least `MIN_VOID_NOTE`; the old link is labelled "Voided".
    void_previous: bool = False


#: A reschedule note must say something (the same floor as a stage comment).
MIN_RESCHEDULE_NOTE = 5
#: Voiding a verdict needs a real reason — it overrules a recorded result.
MIN_VOID_NOTE = 10


def previous_finished_link(db: Session, profile_id: int) -> AiInterviewLink | None:
    """The newest AI L1 of the profile that already ran (any result but Pending).

    A new link over one of these is a RESCHEDULE: the old link worked once and
    is closed, the candidate gets a fresh token, and the old verdict stays on
    record. None when the profile never had an AI L1, or only a pending one."""
    return db.execute(
        select(AiInterviewLink)
        .where(AiInterviewLink.profile_id == profile_id, AiInterviewLink.result != "Pending",
               AiInterviewLink.voided_at.is_(None))
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
    ).scalars().first()


def previous_outcome_words(link: AiInterviewLink) -> str:
    """"Not attempted" · "Failed (42%)" · "Selected (override)" — for the log line."""
    if link.not_attempted:
        return "Not attempted"
    score = f" ({link.overall_score_percent}%)" if link.overall_score_percent is not None else ""
    eff = link.effective_result or link.result
    return f"{eff}{score}" + (" (recruiter override)" if eff != link.result else "")


class AiInterviewUpdate(BaseModel):
    scheduled_at: str | None = None
    candidate_name: str | None = None
    candidate_email: str | None = None
    notes: str | None = None
    resend_email: bool = False


def _profile_or_404(db: Session, profile_id: int) -> CandidateProfile:
    profile = db.get(CandidateProfile, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Candidate profile not found")
    return profile


def _link_or_404(db: Session, profile_id: int, link_id: int) -> AiInterviewLink:
    link = db.get(AiInterviewLink, link_id)
    if link is None or link.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="AI interview session not found")
    return link


def _legacy_target() -> str:
    from services.ai_interview_bridge import _legacy_db_target
    return _legacy_db_target()


def _schedule_row(invite_token: str) -> dict:
    """Legacy interview_schedule row for this session (access key, date/time, status).
    Never raises — the CRM tab degrades gracefully if the legacy row is gone."""
    try:
        from auth_db import get_schedule_by_token
        return get_schedule_by_token(_legacy_target(), invite_token) or {}
    except Exception:
        return {}


def _invite_url(invite_token: str, request=None) -> str:
    """Absolute invite link (15 Sep 2026): settings/env base, else the current
    request's origin. Never a bare "/?invite=…" — see services/invite_links.py."""
    from services.invite_links import invite_url
    return invite_url(invite_token, request, strict=False)


def _link_out(db: Session, link: AiInterviewLink, candidate: Candidate | None, request=None) -> dict:
    email = (candidate.email or "").lower() if candidate else ""
    data = to_dict(link)
    data["report_link"] = ai_report_link(email, link.interview_record_id)
    data["pending"] = link.result == "Pending"
    # The recruiter's override, when they disagreed with the AI. `result` stays
    # the AI's own verdict so the UI can show both — "Selected (HR override)"
    # alongside "AI scored 57.2% — Failed" — rather than silently rewriting
    # history. `effective_result` is what a human should act on.
    data["hr_decision_label"] = hr_decision_label(link.hr_decision)
    data["effective_result"] = link.effective_result
    data["is_overridden"] = bool(link.hr_decision) and link.effective_result != link.result
    # Legacy schedule details so the UI can show and edit what the candidate received.
    row = _schedule_row(link.invite_token)
    data["scheduled_at_local"] = row.get("scheduled_at_local")
    data["access_key"] = row.get("access_key")
    data["candidate_name"] = row.get("candidate_name")
    data["candidate_email"] = row.get("candidate_email")
    data["session_status"] = row.get("session_status") or row.get("status")
    data["invite_url"] = _invite_url(link.invite_token, request)
    # A session already under way must not be silently rescheduled or deleted.
    started = bool(row.get("interview_started_at") or row.get("verified_at"))
    data["started"] = started
    data["can_modify"] = bool(link.result == "Pending" and not started)
    # 8 Oct 2026: a voided interview stays on record but never counts.
    data["voided"] = link.voided_at is not None
    return data


def _requirement_for(db: Session, profile: CandidateProfile) -> Requirement | None:
    return db.execute(
        select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
        .order_by(Requirement.id.desc())
    ).scalars().first()


def _role_title(db: Session, profile: CandidateProfile, requirement: Requirement | None) -> str:
    if requirement is not None:
        return requirement.title
    opp = db.get(Opportunity, profile.opportunity_id)
    return (opp.title if opp else None) or "Karnex screening"


def _when_text(scheduled_at_local: str | None) -> str:
    """Human date for the email body; falls back to the raw text the recruiter typed."""
    from services.ist import human_when
    return human_when(scheduled_at_local)


def _send_invite(db: Session, profile: CandidateProfile, candidate: Candidate,
                 requirement: Requirement | None, *, to_email: str, to_name: str,
                 invite_url: str, access_key: str, when_text: str,
                 user=None, level: str = "L1", scheduled_at_raw: str = "") -> dict:
    """Full invitation when we know who is sending it; the old short note otherwise.

    `user` is the acting CurrentUser — its name, designation and phone become the
    signature, so the candidate can see and reply to the person handling them.
    """
    position = _role_title(db, profile, requirement)
    if user is not None:
        from services.candidate_comms import (
            _plain_html, ai_invite_signature_tokens, candidate_template_override,
        )
        from services.interview_invite_email import (
            _env, build_ai_interview_invite, format_when, sender_details,
        )

        msg = build_ai_interview_invite(
            db, user,
            candidate_name=to_name,
            position=position,
            level=level or "L1",
            scheduled_at_raw=scheduled_at_raw,
            invite_url=invite_url,
            access_key=access_key,
        )
        # Admin-authored draft (Settings → Email Drafts) wins here too — until
        # 3 Sep 2026 only the short fallback below honoured it, so editing
        # "your AI interview link" changed nothing for a recruiter-sent invite.
        tokens = {
            "candidate": to_name, "role": position,
            "when": format_when(scheduled_at_raw) or when_text or "See your recruiter",
            "link": invite_url, "access_key": access_key or "",
            **ai_invite_signature_tokens(sender_details(db, user)),
        }
        tokens["level"] = {"L1": "L1 — AI Screening Interview",
                           "L2": "L2 — Technical Interview"}.get((level or "L1").upper(), level or "")
        tokens["duration"] = _env("INTERVIEW_DEFAULT_DURATION", "")
        subject, text, overridden = candidate_template_override(
            db, "candidate.ai_invite", msg["subject"], msg["text"], tokens)
        if overridden:
            msg = {"subject": subject, "text": text, "html": _plain_html(subject, text)}
    else:
        msg = interview_link_message(to_name, position, when_text, invite_url, access_key, db=db)
    return notify_candidate(to_email, candidate.phone, msg["subject"], msg["text"], msg["html"],
                            db=db, event="candidate.ai_invite", actor=user, to_name=to_name,
                            candidate_id=candidate.id)


@router.get("/{profile_id}/ai-template-status")
def ai_template_status(profile_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(gated_read("profiles", *VIEW_ROLES))):
    """Is the AI L1 template ready for this candidate's position? Asked by every
    "Schedule AI L1" button BEFORE it opens the form (6 Oct 2026, user ask)."""
    profile = _profile_or_404(db, profile_id)
    requirement = _requirement_for(db, profile)
    opportunity = db.get(Opportunity, profile.opportunity_id)
    return envelope(l1_template_status(db, opportunity, requirement))


@router.get("/{profile_id}/ai-interviews")
def list_ai_interviews(profile_id: int, request: Request, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(gated_read("profiles", *VIEW_ROLES))):
    profile = _profile_or_404(db, profile_id)
    candidate = db.get(Candidate, profile.candidate_id)
    links = db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile.id)
        .order_by(AiInterviewLink.created_at.desc())
    ).scalars().all()
    return envelope(
        data=[_link_out(db, l, candidate, request) for l in links],
        meta={"pending_count": sum(1 for l in links if l.result == "Pending"),
              "page": 1, "limit": len(links) or 1, "total": len(links), "pages": 1},
    )


def _interview_record(interview_record_id: str | None) -> dict | None:
    """The legacy `interview_records` payload behind a link. Never raises."""
    rid = str(interview_record_id or "").strip()
    if not rid:
        return None
    try:
        from auth_db import get_interview_record_payload
        return get_interview_record_payload(_legacy_target(), rid) or None
    except Exception:
        return None


@router.get("/{profile_id}/ai-interviews/{link_id}/summary")
def ai_interview_summary(profile_id: int, link_id: int, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(gated_read("profiles", *VIEW_ROLES))):
    """Compact overview for the Interviews tab (23 Sep 2026).

    Same gate as the list — anyone who can see the profile can read the
    verdict without leaving it. ONE legacy read per call; the card fetches it
    lazily, so a profile page never pays for reports it does not show.
    """
    from services.ai_interview_summary import summarize_interview_record

    profile = _profile_or_404(db, profile_id)
    link = _link_or_404(db, profile.id, link_id)
    record = _interview_record(link.interview_record_id)
    data = summarize_interview_record(record)
    # The link row is the CRM's own truth for the headline; the record can lag
    # it by a few seconds while the background upgrade runs.
    if data.get("overall_score_percent") is None and link.overall_score_percent is not None:
        data["overall_score_percent"] = float(link.overall_score_percent)
    data["result"] = link.result
    data["effective_result"] = link.effective_result
    data["hr_decision_label"] = hr_decision_label(link.hr_decision)
    data["level"] = link.level
    return envelope(data=data)


def void_link(db: Session, profile: CandidateProfile, link: AiInterviewLink,
              reason: str, user: CurrentUser) -> None:
    """Mark an AI L1 as not counting (8 Oct 2026). The verdict, score and
    report stay readable — the history must show what happened — but every
    screen reads the NEWER link, and this one is labelled "Voided"."""
    link.voided_at = datetime.now(timezone.utc)
    link.voided_by = user.id
    link.voided_reason = reason
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "AI_INTERVIEW_VOIDED",
                 f"AI L1 voided — {previous_outcome_words(link)} no longer counts. {reason}")


#: Event the screeners hear on a reschedule (admin-editable in Email Flows).
RESCHEDULED_EVENT = "ai_interview.rescheduled"


def _record_reschedule(db: Session, profile: CandidateProfile, candidate: Candidate,
                       previous: AiInterviewLink, when: str, note: str, user: CurrentUser) -> None:
    """Activity row + a bell / mail to everyone who screens (never raises).

    The row keeps the previous outcome beside the reason, so the history reads
    "1st link → Not attempted → rescheduled by TA (why) → 2nd link → result".
    """
    outcome = previous_outcome_words(previous)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "AI_INTERVIEW_RESCHEDULED",
                 f"AI L1 rescheduled for {when or 'now'} — previous AI L1: {outcome}. {note}")
    try:
        with db.begin_nested():
            from services.candidate_profiles import screening_notify_user_ids
            from services.notify import notify_roles
            cname = f"{candidate.first_name} {candidate.last_name or ''}".strip()
            notify_roles(
                db, ["RMG", "GM"],
                f"AI L1 rescheduled: {cname}",
                f"{user.full_name or user.username} sent a fresh AI L1 link"
                f"{' for ' + when if when else ''}. Previous AI L1: {outcome}. {note}",
                f"/admin/?view=crm&p=screening-desk&focus={profile.id}",
                exclude_user_id=user.id,
                event=RESCHEDULED_EVENT,
                user_ids=screening_notify_user_ids(db),
                dedupe_prefix=f"ai_resched:{profile.id}:{previous.id}",
            )
    except Exception:  # noqa: BLE001 — a notice must never undo a schedule
        pass


@router.post("/{profile_id}/ai-interviews")
def trigger_ai_interview(profile_id: int, request: Request, payload: AiInterviewCreate | None = None,
                         db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(gated_write("profiles", *TRIGGER_ROLES))):
    body = payload or AiInterviewCreate()
    profile = _profile_or_404(db, profile_id)
    candidate = db.get(Candidate, profile.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found for this profile")

    to_email = ((body.candidate_email or "").strip() or (candidate.email or "")).strip().lower()
    if not to_email:
        raise HTTPException(status_code=400,
                            detail="Candidate has no email — add one before scheduling")

    pending = db.execute(
        select(AiInterviewLink).where(
            AiInterviewLink.profile_id == profile.id, AiInterviewLink.result == "Pending"
        )
    ).scalars().first()
    if pending is not None:
        raise HTTPException(
            status_code=409,
            detail="An AI interview is already pending for this profile — "
                   "reschedule or cancel it first",
        )
    # A fresh link over a FINISHED one is a reschedule (7 Oct 2026, user flow:
    # the candidate could not attempt the first link, confirmed they are ready,
    # TA sends another). It needs a note — the history must say why the old
    # verdict is being set aside — and the screeners are told so nobody acts on
    # the old result meanwhile. A PASSED interview is never rescheduled.
    previous = previous_finished_link(db, profile.id)
    reschedule_note = (body.reschedule_note or "").strip()
    if previous is not None and body.void_previous:
        # Wrong template / not a fair test (8 Oct 2026): any verdict may be set
        # aside, a pass included — with a real reason, kept on the old link.
        if len(reschedule_note) < MIN_VOID_NOTE:
            raise HTTPException(
                status_code=400,
                detail="Say why the previous AI L1 does not count — e.g. it ran on the wrong "
                       "interview template (at least 10 characters)",
            )
    elif previous is not None:
        if (previous.effective_result or previous.result) in ("Passed", "Selected"):
            raise HTTPException(
                status_code=409,
                detail="The candidate already passed the AI L1 — there is nothing to reschedule. "
                       "If it ran on the wrong template, send a fresh link and void the previous one.",
            )
        if len(reschedule_note) < MIN_RESCHEDULE_NOTE:
            raise HTTPException(
                status_code=400,
                detail="Say why a fresh AI L1 link is being sent — e.g. when the candidate "
                       "confirmed they will attempt it (at least 5 characters)",
            )

    # RMG screening gate (25 Aug 2026): server-side, so a greyed-out button in
    # the UI is a convenience, not the boundary.
    from services.candidate_profiles import rmg_screening_blocks_l1
    blocked = rmg_screening_blocks_l1(profile)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)

    requirement = _requirement_for(db, profile)
    from services.requirements import ensure_not_on_hold
    ensure_not_on_hold(requirement)
    # Refuse to schedule when no interview template is ready for this opportunity:
    # without one the candidate is sent into a session with no questions and gets
    # stuck on "Preparing your interview". The template is built by RMG from a
    # Template Request, so point the recruiter there.
    opportunity = db.get(Opportunity, profile.opportunity_id)
    ensure_l1_template_ready(db, opportunity, requirement)
    bridge = schedule_l1_interview(
        db, candidate, requirement, profile, scheduled_by=user.id,
        scheduled_at_local=body.scheduled_at,
        candidate_name_override=body.candidate_name,
        candidate_email_override=to_email,
        extra_notes=body.notes or "",
        request=request,
    )
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502,
                            detail=f"AI interview scheduling failed: {bridge.get('error')}")

    when = (body.scheduled_at or "").strip()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "AI_INTERVIEW_SCHEDULED",
                 f"AI L1 interview scheduled for {when or 'now'} "
                 f"(session {bridge.get('session_ref')})")
    if previous is not None:
        if body.void_previous:
            void_link(db, profile, previous, reschedule_note, user)
        _record_reschedule(db, profile, candidate, previous, when, reschedule_note, user)

    should_send = ai_interview_autosend_enabled() if body.send_email is None else body.send_email
    notified = {"email": {"sent": False, "error": "not_requested"},
                "whatsapp": {"sent": False, "error": "not_requested"}}
    to_name = (body.candidate_name or "").strip() or \
        f"{candidate.first_name} {candidate.last_name or ''}".strip()
    if should_send:
        notified = _send_invite(
            db, profile, candidate, requirement, to_email=to_email, to_name=to_name,
            invite_url=bridge.get("invite_url", ""), access_key=bridge.get("access_key", ""),
            when_text=_when_text(when), user=user, level="L1", scheduled_at_raw=when,
        )
        sent = bool((notified.get("email") or {}).get("sent"))
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "AI_INTERVIEW_INVITE_EMAIL",
                     f"Invite email to {to_email}: "
                     f"{'sent' if sent else (notified.get('email') or {}).get('error')}")

    db.commit()
    email_result = notified.get("email") or {}
    return envelope(
        data={
            "session_ref": bridge.get("session_ref"),
            "invite_url": bridge.get("invite_url"),
            "access_key": bridge.get("access_key"),
            "link_id": bridge.get("link_id"),
            "job_id": bridge.get("job_id", ""),
            "candidate_name": to_name,
            "candidate_email": to_email,
            "scheduled_at": when or None,
            "notified": notified,
            "email_sent": bool(email_result.get("sent")),
            "email_error": email_result.get("error"),
            "autosend": ai_interview_autosend_enabled(),
        },
        message=(
            (f"AI interview scheduled — invite queued for {to_email} (sent by the mail outbox within a minute; "
             f"check the Emails tab if it does not arrive)"
             if email_result.get("queued") else f"AI interview scheduled — invite emailed to {to_email}")
            if email_result.get("sent")
            else ("AI interview ready — copy the invite link to share with the candidate"
                  + (f" (email not sent: {email_result.get('error')})"
                     if should_send and email_result.get("error") else ""))
        ),
    )


@router.put("/{profile_id}/ai-interviews/{link_id}")
def update_ai_interview(profile_id: int, link_id: int, payload: AiInterviewUpdate, request: Request,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(gated_write("profiles", *TRIGGER_ROLES))):
    """Reschedule a pending session and/or re-send the invite email.

    The invite token and access key are preserved, so a link already shared with
    the candidate keeps working.
    """
    profile = _profile_or_404(db, profile_id)
    link = _link_or_404(db, profile_id, link_id)
    candidate = db.get(Candidate, profile.candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Candidate not found for this profile")
    if link.result != "Pending":
        raise HTTPException(status_code=409,
                            detail="This interview is already complete and cannot be changed")

    row = _schedule_row(link.invite_token)
    if row.get("interview_started_at") or row.get("verified_at"):
        raise HTTPException(status_code=409,
                            detail="The candidate has already started this interview")

    updates: dict[str, str] = {}
    if payload.scheduled_at and payload.scheduled_at.strip():
        updates["scheduled_at_local"] = payload.scheduled_at.strip()
    if payload.candidate_name and payload.candidate_name.strip():
        updates["candidate_name"] = payload.candidate_name.strip()
    if payload.candidate_email and payload.candidate_email.strip():
        updates["candidate_email"] = payload.candidate_email.strip().lower()
    if payload.notes is not None:
        # Keep the packed config block intact — it drives the interview engine
        # (job, skills, timing). The marker is CFG_MARKER ("__KARNEX_CFG__:");
        # this used to split on "--- karnex-cfg", which never matched, so the
        # note landed AFTER the JSON, the config stopped parsing, and the
        # candidate was interviewed against the first job template in the
        # list (15 Sep 2026).
        from services.ai_interview_bridge import CFG_MARKER
        raw_notes = row.get("notes") or ""
        if CFG_MARKER in raw_notes:
            head, cfg_tail = raw_notes.split(CFG_MARKER, 1)
            cfg_block = f"\n{CFG_MARKER}{cfg_tail.strip()}"
        else:
            head, cfg_block = raw_notes, ""
        head = head.strip()
        headline = head.split("\n", 1)[0] if head else ""
        new_note = payload.notes.strip()
        body_txt = "\n".join(x for x in (headline, new_note) if x)
        updates["notes"] = f"{body_txt}{cfg_block}"

    if updates:
        try:
            from auth_db import update_schedule_field
            update_schedule_field(_legacy_target(), link.invite_token, **updates)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Could not update the session: {exc}")
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "AI_INTERVIEW_RESCHEDULED",
                     "AI L1 interview updated: " +
                     ", ".join(f"{k}={v}" for k, v in updates.items() if k != "notes"))

    notified: dict = {"email": {"sent": False, "error": "not_requested"}}
    if payload.resend_email:
        fresh = _schedule_row(link.invite_token)
        to_email = (updates.get("candidate_email")
                    or fresh.get("candidate_email") or candidate.email or "").strip().lower()
        if not to_email:
            raise HTTPException(status_code=400, detail="No candidate email to send to")
        to_name = (updates.get("candidate_name") or fresh.get("candidate_name")
                   or f"{candidate.first_name} {candidate.last_name or ''}".strip())
        notified = _send_invite(
            db, profile, candidate, _requirement_for(db, profile),
            to_email=to_email, to_name=to_name,
            invite_url=_invite_url(link.invite_token, request),
            access_key=fresh.get("access_key") or "",
            when_text=_when_text(fresh.get("scheduled_at_local")),
        )
        sent = bool((notified.get("email") or {}).get("sent"))
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "AI_INTERVIEW_INVITE_EMAIL",
                     f"Invite email re-sent to {to_email}: "
                     f"{'sent' if sent else (notified.get('email') or {}).get('error')}")

    db.commit()
    db.refresh(link)
    email_result = notified.get("email") or {}
    data = _link_out(db, link, candidate, request)
    data["email_sent"] = bool(email_result.get("sent"))
    data["email_error"] = email_result.get("error")
    return envelope(
        data=data,
        message=("Interview updated — invite re-sent" if email_result.get("sent")
                 else "Interview updated"),
    )


@router.delete("/{profile_id}/ai-interviews/{link_id}")
def cancel_ai_interview(profile_id: int, link_id: int, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(gated_write("profiles", *TRIGGER_ROLES))):
    """Cancel a session scheduled by mistake. Removes the legacy interview_schedule
    row too, so the invite link stops working. Completed interviews are kept."""
    profile = _profile_or_404(db, profile_id)
    link = _link_or_404(db, profile_id, link_id)
    if link.result != "Pending":
        raise HTTPException(
            status_code=409,
            detail="This interview is already complete — its result is part of the record",
        )
    row = _schedule_row(link.invite_token)
    if row.get("interview_started_at") or row.get("verified_at"):
        raise HTTPException(status_code=409,
                            detail="The candidate has already started this interview")

    try:
        from auth_db import delete_interview_schedule_by_token
        delete_interview_schedule_by_token(_legacy_target(), link.invite_token)
    except Exception:
        # The CRM link is the source of truth for this tab; a stale legacy row is
        # harmless once the link is gone, so never block the cancel on it.
        pass

    token = link.invite_token
    db.delete(link)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "AI_INTERVIEW_CANCELLED", f"AI L1 interview cancelled (session {token})")
    db.commit()
    return envelope(data={"id": link_id}, message="AI interview cancelled")
