"""Bridge between Karnex CRM and the existing AI Interview Platform (Phase 5).

Outbound: schedule an L1 interview for a CRM candidate by creating a real
interview_schedule row (auth_db.create_interview_schedule) with the interview
config packed into notes — exactly like POST /hr/schedule-interview does —
and record the linkage in ai_interview_links (join key: invite_token).

Inbound: sync_completed_interview(record) is called from main.py after an
interview record is persisted. If the record's invite_token matches a CRM
link, the result flows back: resume ai_interview_status Passed/Failed against
the configurable threshold, skill_evaluations.reviewer_rated per skill, an
activity-log entry, and a TA notification. Standalone (non-CRM) interviews
are untouched — no link row means the sync is a no-op.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_db import CrmNotConfiguredError, crm_database_url, get_session_factory
from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Opportunity,
    OpportunitySkill, PipelineStatus, Requirement, RequirementSkill, Resume, Skill,
    TemplateRequest, TemplateRequestStatus,
)
from services.crm_common import get_app_setting, log_activity
from services.notify import notify_role

logger = logging.getLogger("karnex.crm.ai_bridge")

CFG_MARKER = "__KARNEX_CFG__:"  # must match main.py::_pack_invite_config_into_notes


def ai_interview_autosend_enabled() -> bool:
    """When True, schedule triggers email/WhatsApp the invite. Default: off (show link only).

    Settings-page value first (interview.autosend, Admin-editable), the
    AI_INTERVIEW_AUTOSEND env flag as the fallback.
    """
    try:
        from services.org_settings import setting_bool

        return setting_bool("interview.autosend")
    except Exception:
        return os.getenv("AI_INTERVIEW_AUTOSEND", "false").strip().lower() in ("1", "true", "yes", "on")


def _legacy_db_target() -> str:
    """The auth_db DSN — same Postgres the CRM uses (strip SQLAlchemy driver suffix)."""
    url = crm_database_url()
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


def _pack_notes(notes: str, cfg: dict) -> str:
    return f"{notes or ''}\n{CFG_MARKER}{json.dumps(cfg, ensure_ascii=False)}"


def _skills_for(db: Session, requirement: Requirement | None, opportunity_id: int) -> list[str]:
    """Skill names for the interview: requirement skills first, else opportunity skills."""
    if requirement is not None:
        rows = db.execute(
            select(Skill.name).join(RequirementSkill, RequirementSkill.skill_id == Skill.id)
            .where(RequirementSkill.requirement_id == requirement.id)
        ).scalars().all()
        if rows:
            return list(rows)
    rows = db.execute(
        select(Skill.name).join(OpportunitySkill, OpportunitySkill.skill_id == Skill.id)
        .where(OpportunitySkill.opportunity_id == opportunity_id)
    ).scalars().all()
    return list(rows)


def _matching_job_template_id(opportunity: Opportunity | None) -> str:
    """Best-effort: reuse an existing job template tagged with this opportunity's opp_id."""
    if opportunity is None:
        return ""
    try:
        from auth_db import list_job_templates
        templates = list_job_templates(_legacy_db_target()) or []
        for tpl in templates:
            if str(tpl.get("opportunityId") or "").strip().lower() == str(opportunity.opp_id).strip().lower():
                return str(tpl.get("jobId") or tpl.get("job_id") or "")
    except Exception as exc:  # pragma: no cover — template reuse is optional
        logger.warning("Job template lookup failed: %s", exc)
    return ""


def _template_job_id_from_request(
    db: Session,
    opportunity: Opportunity | None,
    requirement: Requirement | None,
) -> str:
    """The template linked to this opportunity/requirement via a Template Request.

    A non-null ``template_job_id`` means RMG has linked a template — that is what
    makes an interview runnable, so we accept it regardless of the request's exact
    status (TEMPLATE_READY / PREPARED, or any later state) rather than gating on a
    narrow status set that a workflow change could silently break.
    """
    stmt = (
        select(TemplateRequest)
        .where(TemplateRequest.template_job_id.isnot(None))
        .order_by(TemplateRequest.id.desc())
    )
    if requirement is not None:
        stmt = stmt.where(TemplateRequest.requirement_id == requirement.id)
    elif opportunity is not None:
        stmt = stmt.where(TemplateRequest.opportunity_id == opportunity.id)
    else:
        return ""
    tr = db.execute(stmt).scalars().first()
    if tr is None and requirement is not None and opportunity is not None:
        # Fallback: any linked request on the opportunity (if req-specific miss).
        tr = db.execute(
            select(TemplateRequest)
            .where(
                TemplateRequest.opportunity_id == opportunity.id,
                TemplateRequest.template_job_id.isnot(None),
            )
            .order_by(TemplateRequest.id.desc())
        ).scalars().first()
    job = (tr.template_job_id or "").strip() if tr else ""
    return job


def describe_l1_template_gap(
    db: Session,
    opportunity: Opportunity | None,
    requirement: Requirement | None,
) -> str:
    """A precise, actionable reason an AI L1 has no template — for the 400 error.

    Only called when resolve_l1_template_job_id() already returned "" (no linked
    template AND no opp-stamped job template), so it explains what to do next.
    """
    opp_id = opportunity.id if opportunity is not None else None
    # Is there a template request at all for this opportunity/requirement?
    tr = None
    if requirement is not None:
        tr = db.execute(
            select(TemplateRequest)
            .where(TemplateRequest.requirement_id == requirement.id)
            .order_by(TemplateRequest.id.desc())
        ).scalars().first()
    if tr is None and opp_id is not None:
        tr = db.execute(
            select(TemplateRequest)
            .where(TemplateRequest.opportunity_id == opp_id)
            .order_by(TemplateRequest.id.desc())
        ).scalars().first()

    if tr is not None and not (tr.template_job_id or "").strip():
        return (
            f"Template request {tr.tr_number} exists but RMG has not linked a template "
            f"to it yet. Ask RMG to open Template Requests, pick the interview template, "
            f"and fulfil it — then the interview can be scheduled."
        )
    return (
        "No interview template is linked to this opportunity yet. TA should raise a "
        "Template Request from the requirement, and RMG links the AI interview template "
        "to it (Template Requests). Once linked, schedule the interview here."
    )


def resolve_l1_template_job_id(
    db: Session,
    opportunity: Opportunity | None,
    requirement: Requirement | None,
) -> str:
    """The job_template id the AI L1 will use, or "" when none is available.

    Prefers an RMG-fulfilled Template Request linked to this opportunity /
    requirement; falls back to a name match. An empty string means no interview
    template exists yet — scheduling should be refused so a candidate is never
    sent into a session with no questions.
    """
    return (
        _template_job_id_from_request(db, opportunity, requirement)
        or _matching_job_template_id(opportunity)
    )


def schedule_l1_interview(
    db: Session,
    candidate: Candidate,
    requirement: Requirement | None,
    profile: CandidateProfile,
    resume: Resume | None = None,
    scheduled_by: int | None = None,
    scheduled_at_local: str | None = None,
    candidate_name_override: str | None = None,
    candidate_email_override: str | None = None,
    extra_notes: str = "",
) -> dict:
    """Schedule an AI L1 interview for a CRM candidate (real session).

    `scheduled_at_local` is the interview date/time as the recruiter typed it
    ("YYYY-MM-DD HH:MM"); when omitted it defaults to now, preserving the old
    fire-immediately behaviour. The name/email overrides let the recruiter correct
    candidate details at scheduling time without editing the candidate master.

    Returns {"scheduled": bool, "session_ref": invite_token|None, "invite_url": str,
             "access_key": str, "link_id": int|None, "job_id": str, "error": str|None}.
    The caller commits (link row is added to the given session).
    """
    from auth_db import create_interview_schedule  # deferred import — avoids cycles

    opportunity = db.get(Opportunity, profile.opportunity_id)
    skills = _skills_for(db, requirement, profile.opportunity_id)
    job_id = resolve_l1_template_job_id(db, opportunity, requirement)

    cfg = {
        "job_id": job_id,
        "final_skills": [s.lower() for s in skills],
        "num_q": int(os.getenv("CRM_AI_L1_NUM_QUESTIONS", "5")),
        "difficulty": os.getenv("CRM_AI_L1_DIFFICULTY", "medium"),
        "followup_mode": "false",
        "timing_mode": "count",
        "time_limit_sec": 0,
        "mic_always_on": "false",
        "show_spoken_text": "false",
        "model": os.getenv("CRM_AI_L1_MODEL", "gpt-4o-mini"),
    }
    # THE TEMPLATE'S OWN SETTINGS WIN (fix, 27 Aug 2026). This dict used to be
    # entirely hardcoded (count mode, no time limit, 5 questions) — a template
    # configured "Time limit: 15 min" ran unlimited, and the welcome page said
    # "Duration: No fixed limit". Copy the wizard's timing/format so the CRM
    # pipeline runs interviews exactly as the template author configured them.
    if job_id:
        try:
            from auth_db import get_job_template  # deferred — avoids cycles
            tpl = get_job_template(_legacy_db_target(), str(job_id)) or {}
        except Exception:
            tpl = {}
        if tpl:
            timing = str(tpl.get("timingMode") or tpl.get("timing_mode") or "").strip().lower()
            if timing in ("count", "time"):
                cfg["timing_mode"] = timing
            try:
                limit = int(tpl.get("timeLimitSec") or tpl.get("time_limit_sec") or 0)
            except (TypeError, ValueError):
                limit = 0
            if limit > 0:
                cfg["time_limit_sec"] = limit
            try:
                nq = int(tpl.get("numQ") or tpl.get("num_q") or 0)
            except (TypeError, ValueError):
                nq = 0
            if nq > 0:
                cfg["num_q"] = nq
            if (tpl.get("difficulty") or "").strip():
                cfg["difficulty"] = str(tpl["difficulty"]).strip()
            cfg["followup_mode"] = "true" if tpl.get("followupMode") else "false"
            cfg["mic_always_on"] = "true" if tpl.get("micAlwaysOn") else "false"
            show_text = bool(tpl.get("showSpokenText") or tpl.get("enableTranscriptInput"))
            cfg["show_spoken_text"] = "true" if show_text else "false"
            cfg["enable_transcript_input"] = "true" if show_text else "false"
    title = requirement.title if requirement is not None else (opportunity.title if opportunity else "CRM Screening")
    headline = f"Karnex CRM AI L1 interview — {title}"
    if (extra_notes or "").strip():
        headline = f"{headline}\n{extra_notes.strip()}"
    notes = _pack_notes(headline, cfg)
    candidate_name = (candidate_name_override or "").strip() or \
        f"{candidate.first_name} {candidate.last_name or ''}".strip()
    candidate_email = ((candidate_email_override or "").strip() or (candidate.email or "")).lower()
    when = (scheduled_at_local or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M")

    try:
        schedule = create_interview_schedule(
            _legacy_db_target(),
            hr_username="karnex-crm",
            candidate_name=candidate_name,
            candidate_email=candidate_email,
            scheduled_at_local=when,
            provider="karnex-link",
            meeting_link="",
            notes=notes,
        )
    except Exception as exc:
        logger.error("AI L1 scheduling failed for candidate %s: %s", candidate.id, exc)
        return {"scheduled": False, "session_ref": None, "invite_url": "", "access_key": "",
                "link_id": None, "job_id": job_id, "error": str(exc)}

    link = AiInterviewLink(
        invite_token=schedule["invite_token"],
        schedule_id=str(schedule.get("id") or ""),
        candidate_id=candidate.id,
        opportunity_id=profile.opportunity_id,
        profile_id=profile.id,
        requirement_id=requirement.id if requirement is not None else None,
        resume_id=resume.id if resume is not None else None,
        scheduled_by=scheduled_by,
        level="L1",
    )
    db.add(link)
    db.flush()

    base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    invite_url = f"{base}/?invite={schedule['invite_token']}" if base else f"/?invite={schedule['invite_token']}"
    return {
        "scheduled": True,
        "session_ref": schedule["invite_token"],
        "invite_url": invite_url,
        "access_key": schedule.get("access_key", ""),
        "link_id": link.id,
        "job_id": job_id,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Inbound: interview completed → write results back into the CRM
# ---------------------------------------------------------------------------

def _score_percent(report: dict) -> Decimal | None:
    pct = report.get("overall_score_percent")
    if pct is None and report.get("overall_score") is not None:
        try:
            pct = float(report["overall_score"]) * 10.0  # 0-10 scale → percent
        except (TypeError, ValueError):
            pct = None
    if pct is None:
        return None
    return Decimal(str(round(float(pct), 2)))


def _rating_from_score(score_0_10) -> int:
    """Map a 0-10 skill score onto the CRM's 1-5 rating scale."""
    try:
        val = float(score_0_10)
    except (TypeError, ValueError):
        return 1
    return max(1, min(5, round(val / 2)))


def _applied_link(db, profile_id: int) -> str:
    """Applied Candidates row for the RMG review notification (8 Sep 2026)."""
    from models import CandidateProfile
    from services.candidate_profiles import applied_candidates_link
    prof = db.get(CandidateProfile, profile_id)
    return applied_candidates_link(db, prof) if prof else f"/admin?view=crm&p=profiles/{profile_id}"


def sync_completed_interview(record: dict) -> bool:
    """Best-effort CRM write-back after an interview record is persisted.

    Returns True when a CRM link was found and updated. NEVER raises — the
    interview platform must keep working even if the CRM is down.
    """
    try:
        invite_token = str(record.get("invite_token") or "").strip()
        report = record.get("report") or {}
        if not invite_token or not isinstance(report, dict) or not report:
            return False
        try:
            session_factory = get_session_factory()
        except CrmNotConfiguredError:
            return False
        db: Session = session_factory()
        try:
            link = db.execute(
                select(AiInterviewLink).where(AiInterviewLink.invite_token == invite_token)
            ).scalar_one_or_none()
            if link is None:
                return False  # standalone interview — not CRM-scoped

            pct = _score_percent(report)
            threshold = Decimal(get_app_setting(db, "ai_interview_pass_threshold", "60") or "60")
            passed = pct is not None and pct >= threshold

            # Idempotency: the HR record is re-persisted many times (submit,
            # report generation, later edits) and each persist triggers this
            # sync. If THIS record was already synced with the SAME score and
            # result, do nothing — otherwise every re-persist appended another
            # "AI INTERVIEW COMPLETED" activity entry and re-notified TA.
            record_id = str(record.get("id") or "")
            already_synced = (
                link.completed_at is not None
                and record_id
                and str(link.interview_record_id or "") == record_id
                and link.overall_score_percent == pct
                and link.result == ("Passed" if passed else "Failed")
            )
            if already_synced:
                return True

            link.interview_record_id = str(record.get("id") or "") or link.interview_record_id
            link.overall_score_percent = pct
            link.result = "Passed" if passed else "Failed"
            link.completed_at = datetime.now(timezone.utc)

            if link.resume_id:
                resume = db.get(Resume, link.resume_id)
                if resume is not None:
                    resume.ai_interview_status = "Passed" if passed else "Failed"

            # skill_evaluations.reviewer_rated ← report.skill_scores[i].{skill,score}
            skill_scores = report.get("skill_scores") or []
            if isinstance(skill_scores, list) and skill_scores:
                names = [str(s.get("skill") or "").strip() for s in skill_scores if isinstance(s, dict)]
                names = [n for n in names if n]
                skill_rows = db.execute(
                    select(Skill).where(sa.func.lower(Skill.name).in_([n.lower() for n in names]))
                ).scalars().all() if names else []
                by_name = {s.name.lower(): s for s in skill_rows}
                for item in skill_scores:
                    if not isinstance(item, dict):
                        continue
                    skill = by_name.get(str(item.get("skill") or "").strip().lower())
                    if skill is None:
                        continue
                    rating = _rating_from_score(item.get("score"))
                    existing = db.execute(sa.text(
                        "SELECT id FROM skill_evaluations WHERE profile_id = :p AND skill_id = :s"
                    ), {"p": link.profile_id, "s": skill.id}).first()
                    if existing:
                        db.execute(sa.text(
                            "UPDATE skill_evaluations SET reviewer_rated = :r WHERE id = :i"
                        ), {"r": rating, "i": existing[0]})
                    else:
                        db.execute(sa.text(
                            "INSERT INTO skill_evaluations (profile_id, skill_id, reviewer_rated) "
                            "VALUES (:p, :s, :r)"
                        ), {"p": link.profile_id, "s": skill.id, "r": rating})

            if link.scheduled_by:
                log_activity(
                    db, CandidateProfileActivityLog, "profile_id", link.profile_id, link.scheduled_by,
                    "AI_INTERVIEW_COMPLETED",
                    f"AI L1 interview completed — score {pct if pct is not None else 'n/a'}% "
                    f"({'Passed' if passed else 'Failed'}, threshold {threshold}%)",
                )
            candidate = db.get(Candidate, link.candidate_id)
            cname = f"{candidate.first_name} {candidate.last_name or ''}".strip() if candidate else f"#{link.candidate_id}"
            notify_role(
                db, "TA",
                f"AI interview completed: {cname}",
                f"Score {pct if pct is not None else 'n/a'}% — {'Passed' if passed else 'Failed'}",
                # Deep-link straight to the AI Interview tab — the score and the
                # report are there, and landing on Overview makes the reader hunt
                # for the thing the notification is about.
                f"/admin?view=crm&p=profiles/{link.profile_id}&tab=ai",
                event="ai_interview.completed",
            )

            # PASSED L1 → hand off to RMG: auto-advance the profile to RMG_Review
            # (from Technical_Screening) and notify RMG to review the report and
            # decide — request an L2 round or submit to the Sales team.
            if passed and link.profile_id:
                profile = db.get(CandidateProfile, link.profile_id)
                cur = getattr(profile.pipeline_status, "value", profile.pipeline_status) if profile else None
                # ANY pre-RMG stage hands off (fix, 28 Aug 2026). This used to
                # require exactly Technical_Screening, so a candidate applied
                # from the Candidates page — who sits at SOURCING, having never
                # come through the resume/ATS path — passed their AI L1 and then
                # stalled: RMG was never asked to review, so the L2 button never
                # appeared for anyone.
                if profile is not None and cur in (PipelineStatus.SOURCING.value,
                                                   PipelineStatus.TECHNICAL_SCREENING.value):
                    profile.pipeline_status = PipelineStatus.RMG_REVIEW
                    log_activity(
                        db, CandidateProfileActivityLog, "profile_id", profile.id,
                        # No scheduling user -> system user, NOT the candidate id.
                        link.scheduled_by or None,
                        "STATUS_CHANGE",
                        f"{cur} -> RMG_Review: AI L1 passed at {pct}% "
                        f"(threshold {threshold}%) — auto-forwarded for RMG review",
                    )
                if profile is not None and getattr(profile.pipeline_status, "value", profile.pipeline_status) == PipelineStatus.RMG_REVIEW.value:
                    notify_role(
                        db, "RMG",
                        f"AI L1 passed — review {cname}",
                        f"Score {pct}%. Review the interview report and decide: "
                        f"request an L2 round or submit to the Sales team.",
                        _applied_link(db, link.profile_id),
                        event="ai_interview.passed_review",
                    )
            db.commit()
            return True
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover — must never break the interview flow
        logger.error("CRM interview sync failed: %s", exc)
        return False


def sync_hr_decision(*, decision: str | None, decided_by: str | None = None,
                     invite_token: str | None = None, interview_record_id: str | None = None,
                     candidate_email: str | None = None) -> bool:
    """Push a recruiter's Shortlist / On Hold / Reject decision into the CRM.

    The report page stores this in the legacy auth DB. The CRM lives in a
    different database and only ever knew the AI's score-threshold verdict, so a
    candidate the recruiter had SELECTED still showed as "Failed · 57.2%" on the
    Candidate Profile. This is the missing hop.

    The AI verdict in `link.result` is deliberately left alone — the override is
    recorded alongside it, so the profile can show "Selected (HR override)" while
    still reporting what the AI actually scored. Overwriting `result` would erase
    the evidence the recruiter overrode.

    Identified by invite_token, else interview_record_id, else the newest
    completed interview for that candidate's email. Never raises: a CRM outage
    must not break the report page.
    """
    from models.ai_links import hr_decision_label, normalize_hr_decision

    try:
        normalized = normalize_hr_decision(decision)
        if decision and normalized is None:
            logger.warning("Ignoring unrecognised HR decision %r", decision)
            return False

        try:
            session_factory = get_session_factory()
        except CrmNotConfiguredError:
            return False

        db: Session = session_factory()
        try:
            link = _find_link(db, invite_token, interview_record_id, candidate_email)
            if link is None:
                return False

            previous = link.hr_decision
            if previous == normalized:
                return True  # idempotent: repeated clicks must not spam the log

            link.hr_decision = normalized
            link.hr_decision_by = (decided_by or "").strip()[:255] or None
            link.hr_decision_at = datetime.now(timezone.utc) if normalized else None

            if link.profile_id:
                label = hr_decision_label(normalized)
                who = f" by {link.hr_decision_by}" if link.hr_decision_by else ""
                message = (
                    f"AI interview decision cleared{who} "
                    f"(AI verdict stands: {link.result}"
                    f"{f' at {link.overall_score_percent}%' if link.overall_score_percent is not None else ''})"
                    if normalized is None else
                    f"Interview marked {label}{who} — AI verdict was {link.result}"
                    f"{f' at {link.overall_score_percent}%' if link.overall_score_percent is not None else ''}"
                )
                log_activity(
                    db, CandidateProfileActivityLog, "profile_id", link.profile_id,
                    link.scheduled_by or None, "AI_INTERVIEW_DECISION", message,
                )
            db.commit()
            return True
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover — must never break the report page
        logger.error("CRM HR-decision sync failed: %s", exc)
        return False


def _find_link(db: Session, invite_token: str | None, interview_record_id: str | None,
               candidate_email: str | None) -> AiInterviewLink | None:
    """Locate the CRM link row for an interview, most specific key first."""
    if invite_token:
        link = db.execute(
            select(AiInterviewLink).where(AiInterviewLink.invite_token == str(invite_token).strip())
        ).scalar_one_or_none()
        if link is not None:
            return link

    if interview_record_id:
        link = db.execute(
            select(AiInterviewLink)
            .where(AiInterviewLink.interview_record_id == str(interview_record_id).strip())
        ).scalar_one_or_none()
        if link is not None:
            return link

    # Last resort: the candidate-level decision on the report page carries only an
    # email. Apply it to that candidate's most recent COMPLETED interview, which is
    # the one whose report the recruiter was looking at.
    email = (candidate_email or "").strip().lower()
    if email:
        return db.execute(
            select(AiInterviewLink)
            .join(Candidate, Candidate.id == AiInterviewLink.candidate_id)
            .where(sa.func.lower(Candidate.email) == email,
                   AiInterviewLink.completed_at.isnot(None))
            .order_by(AiInterviewLink.completed_at.desc(), AiInterviewLink.id.desc())
            .limit(1)
        ).scalar_one_or_none()
    return None


def link_schedule_to_crm(invite_token: str, schedule_id: str, candidate_id: int,
                         opportunity_id: int) -> bool:
    """Optional CRM linkage for /hr/schedule-interview when candidate_id +
    opportunity_id are supplied. Creates the profile if needed. Never raises."""
    try:
        try:
            session_factory = get_session_factory()
        except CrmNotConfiguredError:
            return False
        db: Session = session_factory()
        try:
            profile = db.execute(
                select(CandidateProfile).where(
                    CandidateProfile.candidate_id == candidate_id,
                    CandidateProfile.opportunity_id == opportunity_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                profile = CandidateProfile(candidate_id=candidate_id, opportunity_id=opportunity_id,
                                           pipeline_status="Technical_Screening")
                db.add(profile)
                db.flush()
            db.add(AiInterviewLink(
                invite_token=invite_token, schedule_id=schedule_id,
                candidate_id=candidate_id, opportunity_id=opportunity_id, profile_id=profile.id,
            ))
            db.commit()
            return True
        finally:
            db.close()
    except Exception as exc:  # pragma: no cover
        logger.error("CRM schedule linkage failed: %s", exc)
        return False
