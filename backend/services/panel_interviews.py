"""The panel member's own interviews — "My Interviews" (7 Oct 2026).

User ask: "whoever takes the interview of a candidate should add that interview's
feedback, and only see THAT — the candidates whose interview they took, not
anything else." Eight new logins arrive this way; they hold no built-in role,
only the seeded **Interviewer** custom role (migration 0128), whose one grant is
the `my-interviews` tab.

The link between a round and a login is the EMPLOYEE the scheduler picked:
`interview_events.employee_id` → `employees` → the login, through
`employees.user_id` when HR linked one, else the official mailbox
(`employees.email` == the login's email, case-insensitive). `my_employee_ids`
is that rule in one place; `panel_user_ids` is its transpose (round → logins),
used to tell the panel member a round was booked for them.

Scope is enforced HERE, not by a tab grant: every reader and the one writer
take the round only when it is one of the caller's own
(`PANEL_ROUND_KINDS` = the technical ladder — the customer's rounds belong to
Sales and the HR round to HR, user decision). A round of somebody else answers
404, never 403, so a panel member cannot probe who else interviewed whom.

The feedback a panel member records goes through the SAME verdict path the
RMG / TA feedback form uses (`candidate_profiles.apply_round_verdict`): a
"No Hire" closes the candidacy, the screeners see "Results to review", TA is
told — recording from this page and from the profile are one act.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink, Candidate, CandidateProfile, Customer, Employee, InterviewEvent,
    Opportunity, Requirement,
)
from services.interview_rounds import NOT_HELD_STATUSES, RESULTS, round_label

#: The rounds a panel member records from this page — the technical ladder.
PANEL_ROUND_KINDS: tuple[str, ...] = ("L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview")
#: The seeded custom role's name (migration 0128) — the ONE grant is `my-interviews`.
INTERVIEWER_ROLE_NAME = "Interviewer"
INTERVIEWER_ROLE_DESCRIPTION = (
    "Panel member: sees only the candidates whose technical interview they take "
    "and records that round's feedback. Nothing else.")
INTERVIEWER_TAB_ACCESS = {"my-interviews": "edit"}
#: The `user_role` stamped on a round recorded from this page.
PANEL_USER_ROLE = "Interviewer"
#: Feedback is the point of the page — a verdict with no words is refused.
MIN_FEEDBACK_CHARS = 5
#: "Upcoming" looks this far ahead; "done" this far back.
UPCOMING_DAYS = 60
HISTORY_DAYS = 180
#: The bell / mail a panel member gets when a round is booked for them.
PANEL_ASSIGNED_EVENT = "interview.panel_assigned"
DEFAULT_DURATION_MIN = 60

SCOPES = ("pending", "upcoming", "done", "all")


def _lower_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def my_employee_ids(db: Session, user) -> list[int]:
    """The employee rows that ARE this login: `employees.user_id`, else the
    official mailbox. ONE query; empty for a login nobody linked."""
    uid = getattr(user, "id", None)
    email = _lower_email(getattr(user, "email", None))
    conds = []
    if uid is not None:
        conds.append(Employee.user_id == uid)
    if email:
        conds.append(func.lower(func.coalesce(Employee.email, "")) == email)
    if not conds:
        return []
    return list(db.execute(select(Employee.id).where(or_(*conds))).scalars().all())


def panel_user_ids(db: Session, employee_id: int | None) -> list[int]:
    """The logins behind an employee row (the transpose of `my_employee_ids`).
    `employees.user_id` wins; else the login whose email is the official
    mailbox (raw SQL — `registration_data` has no ORM model). Never raises."""
    if not employee_id:
        return []
    emp = db.get(Employee, employee_id)
    if emp is None:
        return []
    if emp.user_id:
        return [int(emp.user_id)]
    email = _lower_email(emp.email)
    if not email:
        return []
    try:
        # `is_active` is BOOLEAN on Postgres and INTEGER on SQLite — read it
        # back and test in Python rather than compare in SQL.
        rows = db.execute(
            sa.text("SELECT id, is_active FROM registration_data WHERE lower(email) = :email"),
            {"email": email},
        ).fetchall()
    except Exception:
        return []
    return [int(r[0]) for r in rows if r[1] is None or bool(r[1])]


def employee_by_name(db: Session, name: str | None) -> Employee | None:
    """The ONE active employee whose full name is `name` (case- and
    space-insensitive; with or without the middle name). None when nobody or
    more than one matches — a guess would put a round on the wrong login."""
    clean = " ".join(str(name or "").split()).lower()
    if not clean:
        return None
    rows = db.execute(select(Employee).where(Employee.is_active.is_(True))).scalars().all()
    hits = []
    for e in rows:
        full = " ".join(p for p in (e.first_name, e.middle_name, e.last_name) if p).lower()
        short = " ".join(p for p in (e.first_name, e.last_name) if p).lower()
        if clean in {" ".join(full.split()), " ".join(short.split())}:
            hits.append(e)
    return hits[0] if len(hits) == 1 else None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def phase_of(event: InterviewEvent, now: datetime) -> str:
    """pending (held, no verdict) · upcoming (not yet held) · done (verdict) ·
    not_held (cancelled / no-show …). PURE."""
    if (event.status or "") in NOT_HELD_STATUSES:
        return "not_held"
    if (event.result or "").strip():
        return "done"
    when = _aware(event.scheduled_at)
    if when is None:
        return "pending"
    ends = when + timedelta(minutes=int(event.duration_minutes or DEFAULT_DURATION_MIN))
    return "pending" if ends <= now else "upcoming"


def _base_query(employee_ids: list[int]):
    return (
        select(InterviewEvent, CandidateProfile, Candidate, Opportunity, Customer.name)
        .join(CandidateProfile, CandidateProfile.id == InterviewEvent.profile_id)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .outerjoin(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .outerjoin(Customer, Customer.id == Opportunity.customer_id)
        .where(InterviewEvent.employee_id.in_(employee_ids),
               InterviewEvent.kind.in_(PANEL_ROUND_KINDS))
    )


def _own_round(db: Session, user, event_id: int):
    """The round + its context, only when it is one of the caller's own — 404 otherwise."""
    ids = my_employee_ids(db, user)
    row = None
    if ids:
        row = db.execute(_base_query(ids).where(InterviewEvent.id == event_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Interview not found")
    return row


def _name(c: Candidate) -> str:
    return " ".join(p for p in (c.first_name, c.last_name) if p) or f"Candidate #{c.id}"


def _iso(dt: datetime | None) -> str | None:
    dt = _aware(dt)
    return dt.isoformat() if dt else None


def _money(v) -> float | None:
    return float(v) if v is not None else None


def _row(event: InterviewEvent, profile: CandidateProfile, cand: Candidate, opp, customer_name,
         ai: dict | None, now: datetime) -> dict:
    return {
        "id": event.id,
        "profile_id": profile.id,
        "kind": event.kind,
        "round_label": round_label(event.kind),
        "phase": phase_of(event, now),
        "scheduled_at": _iso(event.scheduled_at),
        "raw_when": event.raw_when,
        "duration_minutes": event.duration_minutes,
        "meeting_link": event.meeting_link,
        "status": event.status,
        "result": event.result,
        "feedback": event.feedback,
        "note": event.note,
        "interviewer": event.interviewer,
        "candidate": {
            "id": cand.id,
            "name": _name(cand),
            "email": cand.email,
            "phone": cand.phone,
            "experience_years": _money(cand.experience_years),
            "notice_period": cand.notice_period,
            "technical_domain": cand.technical_domain,
            "cv_url": cand.cv_url,
            "current_ctc": _money(profile.current_ctc if profile.current_ctc is not None else cand.current_ctc),
            "expected_ctc": _money(profile.expected_ctc if profile.expected_ctc is not None else cand.expected_ctc),
        },
        "position": {
            "opportunity_id": getattr(opp, "id", None),
            "opp_id": getattr(opp, "opp_id", None),
            "title": getattr(opp, "title", None),
            "customer_name": customer_name,
        },
        "ai_l1": ai,
    }


def my_rounds(db: Session, user, scope: str = "pending", *, now: datetime | None = None,
              limit: int = 300) -> dict:
    """Every technical round this login takes, newest-first, with counts per phase."""
    if scope not in SCOPES:
        raise HTTPException(status_code=400, detail=f"scope must be one of {', '.join(SCOPES)}")
    from services.candidate_profiles import latest_ai_interviews

    now = _aware(now) or datetime.now(timezone.utc)
    ids = my_employee_ids(db, user)
    empty = {"rows": [], "counts": {"pending": 0, "upcoming": 0, "done": 0}, "linked": False}
    if not ids:
        return empty
    rows = db.execute(
        _base_query(ids)
        .where(or_(InterviewEvent.scheduled_at.is_(None),
                   InterviewEvent.scheduled_at >= now - timedelta(days=HISTORY_DAYS)))
        .order_by(InterviewEvent.scheduled_at.desc().nullslast(), InterviewEvent.id.desc())
        .limit(limit)
    ).all()
    ai_by_profile = latest_ai_interviews(db, [p for _, p, _, _, _ in rows])
    out, counts = [], {"pending": 0, "upcoming": 0, "done": 0}
    for ev, profile, cand, opp, cname in rows:
        phase = phase_of(ev, now)
        if phase == "not_held":
            continue
        counts[phase] += 1
        if scope != "all" and phase != scope:
            continue
        out.append(_row(ev, profile, cand, opp, cname, ai_by_profile.get(profile.id), now))
    # Pending first, oldest first — the one that waited longest is on top.
    if scope in ("pending", "all"):
        out.sort(key=lambda r: ({"pending": 0, "upcoming": 1, "done": 2}[r["phase"]],
                                r["scheduled_at"] or "", r["id"]))
    return {"rows": out, "counts": counts, "linked": True}


def _required_skills(db: Session, opp) -> list[dict]:
    """The position's skills (latest requirement of the deal), so the panel
    knows what to probe. ONE query; the desk's helper, reused."""
    if opp is None:
        return []
    req_id = db.execute(
        select(func.max(Requirement.id)).where(Requirement.opportunity_id == opp.id)
    ).scalar()
    if not req_id:
        return []
    from services.screening_desk import _skills_by_requirement
    return _skills_by_requirement(db, [req_id]).get(req_id, [])


def round_detail(db: Session, user, event_id: int, *, now: datetime | None = None) -> dict:
    """One of the caller's own rounds + the position's skills + the other
    rounds of THIS candidacy the caller also took (never anyone else's)."""
    from services.candidate_profiles import latest_ai_interviews

    now = _aware(now) or datetime.now(timezone.utc)
    ev, profile, cand, opp, cname = _own_round(db, user, event_id)
    ai = latest_ai_interviews(db, [profile]).get(profile.id)
    data = _row(ev, profile, cand, opp, cname, ai, now)
    data["skills"] = _required_skills(db, opp)
    data["results_scale"] = list(RESULTS)
    ids = my_employee_ids(db, user)
    siblings = db.execute(
        _base_query(ids).where(InterviewEvent.profile_id == profile.id, InterviewEvent.id != ev.id)
        .order_by(InterviewEvent.scheduled_at.asc().nullslast(), InterviewEvent.id.asc())
    ).all()
    data["my_other_rounds"] = [
        {"id": e.id, "kind": e.kind, "round_label": round_label(e.kind), "scheduled_at": _iso(e.scheduled_at),
         "result": e.result, "phase": phase_of(e, now)}
        for e, _, _, _, _ in siblings
    ]
    return data


def ai_summary_for_round(db: Session, user, event_id: int) -> dict:
    """The AI L1 verdict card for one of the caller's own rounds — the same
    shape the profile's Interviews tab renders (`summarize_interview_record`),
    served here so the panel never needs the Candidate Profiles tab."""
    from models.ai_links import hr_decision_label
    from services.ai_interview_summary import summarize_interview_record

    ev, profile, _, _, _ = _own_round(db, user, event_id)
    link = db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile.id)
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc()).limit(1)
    ).scalars().first()
    if link is None:
        return {"available": False, "reason": "No AI interview was taken for this candidacy."}
    record = None
    try:
        from auth_db import get_interview_record_payload
        from services.ai_interview_bridge import _legacy_db_target
        rid = str(link.interview_record_id or "").strip()
        record = get_interview_record_payload(_legacy_db_target(), rid) or None if rid else None
    except Exception:
        record = None
    data = summarize_interview_record(record)
    if data.get("overall_score_percent") is None and link.overall_score_percent is not None:
        data["overall_score_percent"] = float(link.overall_score_percent)
    data["result"] = link.result
    data["effective_result"] = link.effective_result
    data["hr_decision_label"] = hr_decision_label(link.hr_decision)
    data["not_attempted"] = bool(link.not_attempted)
    data["level"] = link.level
    return data


def record_panel_feedback(db: Session, user, event_id: int, result: str | None,
                          feedback: str | None) -> tuple[dict, str | None]:
    """The panel member's verdict on their own round. Validates against the
    five-step scale, stamps Completed + `PANEL_USER_ROLE`, then the ONE verdict
    path (`apply_round_verdict`). Returns (row, moved-to-status)."""
    from services.candidate_profiles import apply_round_verdict, latest_ai_interviews
    from services.crm_common import log_activity
    from models import CandidateProfileActivityLog

    ev, profile, cand, opp, cname = _own_round(db, user, event_id)
    verdict = next((r for r in RESULTS if r.lower() == str(result or "").strip().lower()), None)
    if verdict is None:
        raise HTTPException(status_code=400,
                            detail=f"Pick a result: {', '.join(RESULTS)}")
    words = " ".join(str(feedback or "").split())
    if len(words) < MIN_FEEDBACK_CHARS:
        raise HTTPException(status_code=400,
                            detail="Write the feedback — what the candidate answered well and what they lacked.")
    previous = ev.result
    ev.result = verdict
    ev.feedback = words
    ev.status = "Completed"
    ev.user_role = PANEL_USER_ROLE
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_UPDATED" if previous else "INTERVIEW_ROUND_ADDED",
                 f"{round_label(ev.kind)} feedback recorded by the panel ({ev.interviewer or user.full_name}) — {verdict}")
    moved = apply_round_verdict(db, profile, ev, user, previous)
    now = datetime.now(timezone.utc)
    ai = latest_ai_interviews(db, [profile]).get(profile.id)
    return _row(ev, profile, cand, opp, cname, ai, now), moved


def notify_panel_member(db: Session, profile, event: InterviewEvent, actor) -> int:
    """Bell + mail to the login(s) behind the round's employee: "you are taking
    the Technical L1 of X on <when>". Only for the technical ladder, never to
    the person who booked it, deduped per round. Best-effort, never raises."""
    if event.kind not in PANEL_ROUND_KINDS or not event.employee_id:
        return 0
    try:
        with db.begin_nested():
            from services.notify import notify_user
            from services.ist import to_ist

            targets = [u for u in panel_user_ids(db, event.employee_id)
                       if u != getattr(actor, "id", None)]
            if not targets:
                return 0
            cand = db.get(Candidate, profile.candidate_id)
            cname = _name(cand) if cand else f"Candidate #{profile.candidate_id}"
            when = ""
            if event.scheduled_at is not None:
                try:
                    when = to_ist(event.scheduled_at).strftime("%d %b %Y, %I:%M %p IST")
                except Exception:
                    when = event.raw_when or ""
            elif event.raw_when:
                when = event.raw_when
            label = round_label(event.kind)
            title = f"You are taking the {label}: {cname}"
            message = (f"{label} with {cname}" + (f" on {when}" if when else "")
                       + (f" — {event.meeting_link}" if event.meeting_link else "")
                       + ". Open My Interviews to see the CV and the AI interview, and record "
                         "your feedback once the round is over.")
            link = f"/admin/?view=crm&p=my-interviews&focus={event.id}"
            for uid in targets:
                notify_user(db, uid, title, message, link, actor=actor, event=PANEL_ASSIGNED_EVENT,
                            dedupe_key=f"panel_assigned:{event.id}:{uid}",
                            related_type="candidate", related_id=profile.candidate_id)
            return len(targets)
    except Exception:
        import logging
        logging.getLogger("karnex.crm.panel_interviews").warning(
            "panel notice failed for round %s", getattr(event, "id", "?"), exc_info=True)
        return 0


def link_round_to_employee(db: Session, event: InterviewEvent, employee_id: int | None,
                           typed_name: str | None) -> None:
    """Set the round's employee: the picked id, else the ONE active employee
    whose name was typed (older clients send the name only). The display name
    always follows the employee when one is found."""
    emp = db.get(Employee, employee_id) if employee_id else employee_by_name(db, typed_name)
    if emp is None:
        return
    event.employee_id = emp.id
    event.interviewer = (" ".join(p for p in (emp.first_name, emp.middle_name, emp.last_name) if p)
                         or event.interviewer)[:200]


def is_panel_only(user) -> bool:
    """A login that holds NO built-in role (only custom roles such as
    Interviewer) and is not Admin / CEO — the desk gives it its own tab alone."""
    from models.rbac import RoleName
    roles = set(getattr(user, "roles", None) or ())
    builtin = {r.value for r in RoleName}
    return bool(roles) and not (roles & builtin)
