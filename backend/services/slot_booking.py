"""Shared automated-pipeline services: candidate creation from a resume,
profile bootstrap, slot-booking invites and the ATS auto-threshold hook.

Used by routers/crm/resumes.py (manual scheduling + scan hooks) and
routers/crm/slots.py (public booking confirmation) so the find-or-create
logic lives in exactly one place.
"""
from __future__ import annotations

import logging
import re
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AtsStatus, Candidate, CandidateProfile, InterviewSlot, PipelineStatus, Requirement,
    RequirementActivityLog, Resume, SlotBooking,
)
from services.candidate_comms import notify_candidate, slot_invite_message
from services.candidates import apply_cv_profile_to_candidate
from services.crm_common import get_app_setting, log_activity
from services.notify import notify_role

logger = logging.getLogger("karnex.crm.slot_booking")

BOOKING_PATH_PREFIX = "/book/"


# ------------------------------------------------------- candidate / profile

def split_candidate_name(full_name: str) -> tuple[str, str | None]:
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "Unknown", None
    return parts[0], (parts[1] if len(parts) > 1 else None)


def _years_from_str(v) -> float | None:
    if not v:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", str(v))
    try:
        return float(m.group(1)) if m else None
    except (TypeError, ValueError):
        return None


def _ctc_from_str(v) -> float | None:
    """Parse a self-reported CTC ('12 LPA', '18,00,000', '1.5 Cr') to rupees."""
    if not v:
        return None
    s = str(v).lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except (TypeError, ValueError):
        return None
    if "cr" in s or "crore" in s:
        return num * 10_000_000
    if "lpa" in s or "lakh" in s or "lac" in s:
        return num * 100_000
    # Bare number: small values are almost certainly in lakhs; large ones rupees.
    return num * 100_000 if num < 1000 else num


def _skills_from_str(v) -> list[str]:
    if not v:
        return []
    return [p.strip() for p in re.split(r"[,;/|\n]+", str(v)) if p.strip()][:40]


def profile_from_resume_application(resume: Resume) -> dict:
    """Build a candidate-profile dict from the apply-form data captured on a
    Resume (application_details JSON + applicant_experience), shaped for
    apply_cv_profile_to_candidate."""
    details = resume.application_details or {}
    edu = (details.get("education") or "").strip()
    return {
        "technical_domain": (details.get("technical_domain") or "").strip(),
        "experience_years": _years_from_str(resume.applicant_experience),
        "linkedin_url": "",
        "current_ctc": _ctc_from_str(details.get("current_ctc")),
        "expected_ctc": _ctc_from_str(details.get("expected_ctc")),
        "preferred_location": (details.get("preferred_location") or "").strip(),
        "designation": "",
        # The apply form asks for notice period and it shows on the Resumes tab,
        # but it was never copied onto the candidate — so the Notice Period
        # column on Candidate Profiles was blank for everyone who applied online,
        # which is most people.
        "notice_period": (str(details.get("notice_period") or "").strip() or None),
        "skills": _skills_from_str(details.get("skills")),
        "education": [{"course": edu}] if edu else [],
        "experience": [],
    }


def find_or_create_candidate_from_resume(db: Session, resume: Resume) -> Candidate:
    """Match an existing Candidate by email (or full name when no email),
    else create one. candidates.email is NOT NULL + unique, so a placeholder
    is synthesized when the resume has no email. Flushes; caller commits."""
    candidate = None
    email = (resume.email or "").strip().lower()
    first, last = split_candidate_name(resume.candidate_name)
    if email:
        candidate = db.execute(
            select(Candidate).where(func.lower(Candidate.email) == email)
        ).scalars().first()
    else:
        stmt = select(Candidate).where(func.lower(Candidate.first_name) == first.lower())
        if last:
            stmt = stmt.where(func.lower(func.coalesce(Candidate.last_name, "")) == last.lower())
        candidate = db.execute(stmt).scalars().first()
    if candidate is None:
        candidate = Candidate(
            first_name=first,
            last_name=last,
            email=email or f"resume-{resume.id}@noemail.karnex.local",
            phone=resume.phone,
            cv_url=resume.resume_file_url,
        )
        db.add(candidate)
        db.flush()
    else:
        # Backfill core identifiers on an already-known candidate.
        if not candidate.phone and resume.phone:
            candidate.phone = resume.phone
        if not candidate.cv_url and resume.resume_file_url:
            candidate.cv_url = resume.resume_file_url
    # Carry the applicant's self-reported details (experience, education, domain,
    # skills, CTC) from the apply form into the candidate profile so every role
    # sees them. Fills only empty fields; best-effort.
    try:
        apply_cv_profile_to_candidate(db, candidate, profile_from_resume_application(resume))
    except Exception:
        logger.warning("apply_form_autofill_failed for resume %s", getattr(resume, "id", "?"), exc_info=True)
    return candidate


def ensure_sourcing_profile(db: Session, resume: Resume, requirement: Requirement,
                            *, ta_user=None, source: str = "ats",
                            notify_rmg: bool = True) -> CandidateProfile | None:
    """Every resume is an application: make sure a CandidateProfile exists for
    (candidate, opportunity) so the person shows up in the Applicants tab the
    moment their resume lands — not only once an AI interview is scheduled.

    Created at SOURCING (the pipeline's entry stage). An existing profile is
    left completely alone: this must never move anyone backwards, and a
    re-uploaded resume must not reset a candidate who is already mid-pipeline.

    Best-effort by design — returns None rather than raising, because a broken
    profile bootstrap must never block a resume upload or a public application.
    The DB work runs in a SAVEPOINT: on Postgres a failed statement poisons the
    whole transaction, so swallowing the exception without rolling back to a
    savepoint would make every LATER statement in the caller's request fail
    too — the exact opposite of "best-effort". Flushes; the caller commits.
    """
    try:
        if resume.candidate_id is None:
            return None
        with db.begin_nested():
            profile = db.execute(
                select(CandidateProfile).where(
                    CandidateProfile.candidate_id == resume.candidate_id,
                    CandidateProfile.opportunity_id == requirement.opportunity_id,
                )
            ).scalars().first()
            if profile is not None:
                return profile
            candidate = db.get(Candidate, resume.candidate_id)
            profile = CandidateProfile(
                candidate_id=resume.candidate_id,
                opportunity_id=requirement.opportunity_id,
                pipeline_status=PipelineStatus.SOURCING,
                expected_ctc=getattr(candidate, "expected_ctc", None),
                source=source,
                ta_owner_id=getattr(ta_user, "id", None),
                ta_owner_name=(getattr(ta_user, "full_name", None)
                               or getattr(ta_user, "username", None)),
            )
            from datetime import datetime, timezone
            profile.applied_on = datetime.now(timezone.utc)
            # RMG screening gate: a new applicant starts Pending; the AI-L1
            # actions stay locked until RMG shortlists. Bulk callers pass
            # notify_rmg=False and send ONE summary notification instead of
            # fifty individual ones.
            from services.candidate_profiles import RMG_SCREENING_PENDING, rmg_gate_enabled
            gate_on = rmg_gate_enabled()
            if gate_on:
                profile.rmg_screening_status = RMG_SCREENING_PENDING
            db.add(profile)
            db.flush()
            if gate_on and notify_rmg:
                from services.candidate_profiles import notify_rmg_new_applicant
                notify_rmg_new_applicant(db, profile, actor=ta_user)
            return profile
    except Exception:
        logger.warning("ensure_sourcing_profile failed for resume %s",
                       getattr(resume, "id", "?"), exc_info=True)
        return None


def get_or_create_profile(db: Session, candidate: Candidate,
                          requirement: Requirement) -> CandidateProfile:
    """Profile for (candidate, requirement.opportunity) — created in
    Technical_Screening (or bumped from Sourcing). Flushes; caller commits."""
    profile = db.execute(
        select(CandidateProfile).where(
            CandidateProfile.candidate_id == candidate.id,
            CandidateProfile.opportunity_id == requirement.opportunity_id,
        )
    ).scalars().first()
    if profile is None:
        profile = CandidateProfile(
            candidate_id=candidate.id,
            opportunity_id=requirement.opportunity_id,
            expected_ctc=candidate.expected_ctc,
            pipeline_status=PipelineStatus.TECHNICAL_SCREENING,
        )
        db.add(profile)
        db.flush()
    elif profile.pipeline_status == PipelineStatus.SOURCING:
        profile.pipeline_status = PipelineStatus.TECHNICAL_SCREENING
    return profile


# ------------------------------------------------------------- slot booking

def get_or_create_booking(db: Session, resume: Resume) -> SlotBooking:
    """One SlotBooking per resume; token is an opaque urlsafe secret.
    Flushes; caller commits."""
    booking = db.execute(
        select(SlotBooking).where(SlotBooking.resume_id == resume.id)
    ).scalars().first()
    if booking is not None:
        return booking
    booking = SlotBooking(
        token=secrets.token_urlsafe(24),
        resume_id=resume.id,
        requirement_id=resume.requirement_id,
        candidate_id=resume.candidate_id,
    )
    db.add(booking)
    db.flush()
    return booking


def booking_url_for(base_url: str, booking: SlotBooking) -> str:
    return f"{(base_url or '').rstrip('/')}{BOOKING_PATH_PREFIX}{booking.token}"


def send_slot_invite(db: Session, resume: Resume, requirement: Requirement,
                     base_url: str, actor=None) -> tuple[SlotBooking, dict]:
    """Create the booking (if missing) and send the slot-invite message to the
    candidate's email/WhatsApp. Returns (booking, per-channel results).
    Flushes; caller commits.

    `actor` (the sending TA, when a human clicked the button) shapes the email
    headers — From-name "Name (Karnex)" + Reply-To their own inbox — so the
    candidate sees who is handling them and a reply reaches that recruiter."""
    booking = get_or_create_booking(db, resume)
    # Stamp WHO invited (0084): the confirmation email goes to exactly this
    # person. A re-send re-stamps — the most recent sender is the one waiting.
    if actor is not None and getattr(actor, "id", None):
        booking.invited_by = actor.id
    url = booking_url_for(base_url, booking)
    msg = slot_invite_message(resume.candidate_name, requirement.title, url, db=db)
    results = notify_candidate(resume.email, resume.phone, msg["subject"], msg["text"], msg["html"],
                               db=db, event="candidate.slot_invite", actor=actor,
                               to_name=resume.candidate_name,
                               candidate_id=resume.candidate_id)
    return booking, results


def upcoming_open_slots(db: Session, requirement_id: int) -> list[InterviewSlot]:
    """Future slots on the requirement that still have capacity left."""
    return db.execute(
        select(InterviewSlot)
        .where(
            InterviewSlot.requirement_id == requirement_id,
            InterviewSlot.slot_at > func.now(),
            InterviewSlot.booked_count < InterviewSlot.capacity,
        )
        .order_by(InterviewSlot.slot_at.asc())
    ).scalars().all()


# ------------------------------------------------------ ATS threshold hook

def profile_for_resume(db: Session, resume: Resume, requirement: Requirement):
    """The (candidate, opportunity) profile behind a resume row, or None."""
    if resume.candidate_id is None or requirement is None:
        return None
    from models import CandidateProfile
    return db.execute(
        select(CandidateProfile).where(
            CandidateProfile.candidate_id == resume.candidate_id,
            CandidateProfile.opportunity_id == requirement.opportunity_id,
        )
    ).scalars().first()


def _slot_invite_blocked(db: Session, resume: Resume, requirement: Requirement) -> str | None:
    """The manual-route reason the AI-L1 slot invite must not go out, or None.
    Shared by the auto-threshold hook and TA's Send-invite button so the two
    can never disagree. (The RMG screening gate stays where it was — on the
    manual send/preview endpoints — unchanged.)"""
    from services.candidate_profiles import manual_route_blocks_slot_invite
    profile = profile_for_resume(db, resume, requirement)
    if profile is None:
        return None
    return manual_route_blocks_slot_invite(db, profile)


slot_invite_blocked = _slot_invite_blocked


def auto_pipeline_after_scan(db: Session, resume: Resume, requirement: Requirement,
                             user_id: int, base_url: str = "") -> dict:
    """ATS auto-threshold hook, called right after a successful scan.

    When ats_auto_invite is enabled and the score clears ats_auto_threshold:
      * auto-shortlist the resume (Scored -> Shortlisted) + activity log,
      * create the slot booking and send the invite (email/WhatsApp) when the
        resume has contact info; otherwise notify TA to follow up manually.

    NEVER raises — the scan result must be returned normally regardless.
    Returns {"auto_shortlisted": bool, "slot_invite_sent": bool, ...details}.
    """
    info: dict = {"auto_shortlisted": False, "slot_invite_sent": False}
    try:
        if (get_app_setting(db, "ats_auto_invite", "true") or "").strip().lower() != "true":
            return info
        try:
            threshold = float(get_app_setting(db, "ats_auto_threshold", "50") or "50")
        except (TypeError, ValueError):
            threshold = 50.0
        if resume.ats_score is None or float(resume.ats_score) < threshold:
            return info
        if resume.ats_status != AtsStatus.SCORED:
            return info

        resume.ats_status = AtsStatus.SHORTLISTED
        resume.screened_by = resume.screened_by or user_id
        info["auto_shortlisted"] = True
        score = float(resume.ats_score)
        log_activity(db, RequirementActivityLog, "requirement_id", requirement.id, user_id,
                     "ATS_AUTO_SHORTLISTED",
                     f"ATS_AUTO_SHORTLISTED (score {score:g}%) — {resume.candidate_name} "
                     f"cleared the auto threshold ({threshold:g}%)")

        # Manual route / past the AI stage (3 Sep 2026): shortlist stands, but
        # the AI booking link must not go out — RMG decided a human L1.
        blocked = _slot_invite_blocked(db, resume, requirement)
        if blocked:
            info["slot_invite_skipped"] = blocked
            log_activity(db, RequirementActivityLog, "requirement_id", requirement.id, user_id,
                         "SLOT_INVITE_SKIPPED",
                         f"SLOT_INVITE_SKIPPED for {resume.candidate_name} — {blocked}")
        elif (resume.email or "").strip() or (resume.phone or "").strip():
            booking, results = send_slot_invite(db, resume, requirement, base_url)
            channels = [ch for ch, res in results.items() if res.get("sent")]
            info["slot_invite_sent"] = bool(channels)
            info["booking_id"] = booking.id
            info["notified"] = results
            log_activity(db, RequirementActivityLog, "requirement_id", requirement.id, user_id,
                         "SLOT_INVITE_SENT",
                         f"SLOT_INVITE_SENT ({'/'.join(channels) if channels else 'no channel delivered'}) "
                         f"to {resume.candidate_name} — booking #{booking.id}")
        else:
            notify_role(db, "TA",
                        f"Auto-shortlisted, manual follow-up needed: {resume.candidate_name}",
                        f"ATS score {score:g}% cleared the auto threshold but the resume has "
                        f"no email/phone — send the slot invite manually.",
                        f"/admin?view=crm&p=requirements/{requirement.id}",
                        event="slot.manual_followup")
    except Exception as exc:  # pragma: no cover — must never break the scan
        logger.error("ATS auto-pipeline hook failed for resume %s: %s", resume.id, exc)
        info["error"] = str(exc)
    return info
