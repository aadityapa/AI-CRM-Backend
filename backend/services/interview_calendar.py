"""One calendar view over every kind of scheduled interview.

The platform schedules interviews in two unrelated places:

  * `interview_events`   — manual rounds (L2 face-to-face, customer interviews)
                           booked by RMG/TA. Time is a real tz-aware column.
  * `ai_interview_links` — AI L1 sessions. The CRM row has no time at all; the
                           scheduled time lives on the legacy `interview_schedule`
                           row, joined by invite_token, as a free-form local
                           string like "2026-08-07 12:30".

Neither knows about the other, so nothing in the product could answer "what
interviews are happening on Friday". This module merges them into one list of
calendar events with a common shape.

Design notes worth keeping in mind:

  * The legacy time is a LOCAL string with no zone. It is parsed as local wall
    time and returned as such — converting it to UTC would silently shift every
    AI interview by the server's offset. Manual rounds ARE tz-aware, so both are
    normalised to naive local wall time for the grid, and the original ISO value
    is preserved for anything that needs it.
  * Range filtering for AI interviews happens in Python, because the legacy
    column is TEXT and cannot be compared as a timestamp in SQL portably. The
    row count is small (one org's interviews), so this is fine — but it is why
    the AI query is bounded by a candidate-id join rather than by date.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import (
    USERS_TABLE, AiInterviewLink, Candidate, CandidateProfile, Customer, Employee,
    InterviewEvent, Opportunity,
)

logger = logging.getLogger("karnex.crm.calendar")

#: Source markers the UI colour-codes by.
SOURCE_AI = "ai_l1"
SOURCE_MANUAL = "manual_round"

#: Whose round each kind is, for the calendar chip and title (2 Sep 2026).
_ROUND_TEAM = {
    "L1_Interview": "Technical", "L2_F2F": "Technical",
    "L3_Interview": "Technical", "L4_Interview": "Technical",
    "HR_Interview": "HR",
    "Customer_Interview": "Customer", "Customer_L2": "Customer",
}


def _round_display(kind: str | None, category: str | None, stage: str | None) -> str:
    """"Technical · L2 - Interview" / "HR · HR - Interview" / "Customer · Customer L1 - Interview".
    Falls back to whatever the row does carry, so an imported round never
    renders blank."""
    from services.interview_rounds import round_label
    if kind and kind in _ROUND_TEAM:
        return f"{_ROUND_TEAM[kind]} · {round_label(kind)}"
    return category or stage or kind or "Interview"

#: Default when a round does not say how long it runs. An hour is what the
#: scheduling modal offers and what most rounds are booked for.
DEFAULT_DURATION_MINUTES = 60
#: AI L1 sessions are shorter in practice; used only when nothing else is known.
DEFAULT_AI_DURATION_MINUTES = 30

#: Guard against a client asking for ten years of data in one request.
MAX_RANGE_DAYS = 92


@dataclass
class CalendarRange:
    start: datetime
    end: datetime

    @property
    def days(self) -> int:
        return max(1, (self.end - self.start).days)


def parse_range(start: str | None, end: str | None) -> CalendarRange:
    """Resolve the requested window, defaulting to the current week.

    Both bounds are naive local wall time — the grid is drawn in the viewer's
    local time and the legacy schedule strings have no zone, so introducing UTC
    here would only create off-by-one-day bugs at the week boundaries.
    """
    now = datetime.now()

    def _parse(value: str | None) -> datetime | None:
        text = (value or "").strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
        try:  # ISO with a zone — drop it, we work in local wall time
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    start_dt = _parse(start)
    end_dt = _parse(end)

    if start_dt is None:
        # Monday of the current week.
        start_dt = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
    if end_dt is None:
        end_dt = start_dt + timedelta(days=7)
    if end_dt <= start_dt:
        end_dt = start_dt + timedelta(days=1)
    if (end_dt - start_dt).days > MAX_RANGE_DAYS:
        end_dt = start_dt + timedelta(days=MAX_RANGE_DAYS)
    return CalendarRange(start=start_dt, end=end_dt)


def _to_local_naive(value: datetime | None) -> datetime | None:
    """Strip the zone so tz-aware rounds and zone-less legacy rows compare.

    "Local" is IST explicitly (2 Sep 2026) — every time the team enters is
    IST, and the AI-interview rows are IST wall time already. `astimezone()`
    with no argument used the SERVER's zone, so a UTC-hosted deploy placed
    every manual round 5h30 early on the grid.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    try:
        from zoneinfo import ZoneInfo
        return value.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None)
    except Exception:
        return value.astimezone().replace(tzinfo=None)


def parse_legacy_local(value) -> datetime | None:
    """Parse the legacy `scheduled_at_local` TEXT column.

    It is written by several code paths over the years, so the format varies.
    Returns None rather than raising — an unparseable time means the event shows
    in the "no time set" list instead of at a wrong position on the grid.
    """
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%d/%m/%Y %H:%M", "%m/%d/%Y %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logger.debug("Unparseable legacy schedule time: %r", text)
        return None


def _organiser_names(db: Session, user_ids) -> dict[int, str]:
    """Resolve scheduler user ids to display names in ONE query.

    The users table (`registration_data`) is owned by the legacy platform and
    has no ORM model, so it cannot be joined in the SQLAlchemy selects above.
    Batching it here keeps the calendar at a fixed number of queries regardless
    of how many interviews the week contains.
    """
    ids = sorted({int(i) for i in user_ids if i})
    if not ids:
        return {}
    try:
        rows = db.execute(
            sa.text(f"SELECT id, full_name, username FROM {USERS_TABLE} "
                    f"WHERE id IN :ids").bindparams(sa.bindparam("ids", expanding=True)),
            {"ids": ids},
        ).all()
    except Exception as exc:
        logger.debug("Could not resolve organiser names: %s", exc)
        return {}
    return {
        int(row[0]): (str(row[1] or "").strip() or str(row[2] or "").strip() or f"User #{row[0]}")
        for row in rows
    }


def _candidate_name(candidate: Candidate | None, fallback_id) -> str:
    if candidate is None:
        return f"Candidate #{fallback_id or '?'}"
    name = " ".join(
        p for p in (candidate.first_name, getattr(candidate, "middle_name", None),
                    candidate.last_name) if p
    ).strip()
    return name or f"Candidate #{candidate.id}"


def _event(**kwargs) -> dict:
    """Common calendar-event shape. Every source produces exactly this."""
    return {
        "id": kwargs["id"],
        "source": kwargs["source"],
        "title": kwargs["title"],
        "starts_at": kwargs["starts_at"],
        "ends_at": kwargs.get("ends_at"),
        "duration_minutes": kwargs.get("duration_minutes"),
        "raw_when": kwargs.get("raw_when"),
        "candidate_id": kwargs.get("candidate_id"),
        "candidate_name": kwargs.get("candidate_name"),
        "candidate_email": kwargs.get("candidate_email"),
        "profile_id": kwargs.get("profile_id"),
        "opportunity_id": kwargs.get("opportunity_id"),
        "opportunity_title": kwargs.get("opportunity_title"),
        "opportunity_code": kwargs.get("opportunity_code"),
        "customer_name": kwargs.get("customer_name"),
        "kind": kwargs.get("kind"),
        "round_label": kwargs.get("round_label"),
        "mode": kwargs.get("mode"),
        "status": kwargs.get("status"),
        "result": kwargs.get("result"),
        "meeting_link": kwargs.get("meeting_link"),
        "note": kwargs.get("note"),
        "organiser": kwargs.get("organiser"),
        "organiser_user_id": kwargs.get("organiser_user_id"),
        "panel": kwargs.get("panel") or [],
        "cv_url": kwargs.get("cv_url"),
        "detail_path": kwargs.get("detail_path"),
        "report_link": kwargs.get("report_link"),
        "can_modify": bool(kwargs.get("can_modify")),
    }


def _manual_round_events(db: Session, window: CalendarRange) -> list[dict]:
    """Manual interview rounds whose scheduled_at falls inside the window."""
    Cand = Candidate
    Panelist = Employee

    rows = db.execute(
        select(InterviewEvent, Cand, CandidateProfile, Opportunity, Customer, Panelist)
        .join(Cand, Cand.id == InterviewEvent.candidate_id, isouter=True)
        .join(CandidateProfile, CandidateProfile.id == InterviewEvent.profile_id, isouter=True)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id, isouter=True)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        .join(Panelist, Panelist.id == InterviewEvent.employee_id, isouter=True)
        .where(
            InterviewEvent.scheduled_at.isnot(None),
            # scheduled_at is tz-aware; the window is local wall time. Compare in
            # UTC with a day of slack on each side, then filter exactly in Python
            # once both are naive-local. Slack covers the zone offset.
            InterviewEvent.scheduled_at >= (window.start - timedelta(days=1)).replace(tzinfo=timezone.utc),
            InterviewEvent.scheduled_at <= (window.end + timedelta(days=1)).replace(tzinfo=timezone.utc),
        )
        .order_by(InterviewEvent.scheduled_at.asc())
    ).all()

    organisers = _organiser_names(db, [ev.created_by for ev, *_ in rows])

    out: list[dict] = []
    for ev, cand, profile, opp, customer, panelist in rows:
        starts = _to_local_naive(ev.scheduled_at)
        if starts is None or not (window.start <= starts < window.end):
            continue
        ends = _to_local_naive(ev.scheduled_end)
        duration = ev.duration_minutes or (
            int((ends - starts).total_seconds() // 60) if ends else DEFAULT_DURATION_MINUTES
        )
        name = _candidate_name(cand, ev.candidate_id)
        # WHICH round and WHOSE (2 Sep 2026, user report: the popup said only
        # "Internal"). "Technical · L2 - Interview", "HR · HR - Interview",
        # "Customer · Customer L1 - Interview".
        round_label = _round_display(ev.kind, ev.interview_category, ev.stage)
        # The panel: the linked employee, else the typed interviewer name.
        panel_names = [p for p in (
            getattr(panelist, "name", None) if panelist is not None else None,
            (ev.interviewer or "").strip() or None,
        ) if p]
        out.append(_event(
            id=f"manual:{ev.id}",
            source=SOURCE_MANUAL,
            title=f"{round_label} || {name}",
            starts_at=starts.isoformat(),
            ends_at=(ends or (starts + timedelta(minutes=duration))).isoformat(),
            duration_minutes=duration,
            raw_when=ev.raw_when,
            candidate_id=ev.candidate_id,
            candidate_name=name,
            candidate_email=getattr(cand, "email", None),
            profile_id=ev.profile_id,
            opportunity_id=getattr(opp, "id", None),
            opportunity_title=getattr(opp, "title", None),
            opportunity_code=getattr(opp, "opp_id", None),
            customer_name=getattr(customer, "name", None),
            kind=ev.kind,
            round_label=round_label,
            mode=ev.mode,
            status=ev.status,
            result=ev.result,
            meeting_link=ev.meeting_link,
            note=ev.note,
            organiser=organisers.get(ev.created_by),
            organiser_user_id=ev.created_by,
            panel=list(dict.fromkeys(panel_names)),
            cv_url=getattr(cand, "cv_url", None),
            detail_path=f"profiles/{ev.profile_id}" if ev.profile_id else None,
            can_modify=True,
        ))
    return out


def _ai_interview_events(db: Session, window: CalendarRange) -> list[dict]:
    """AI L1 sessions, with their time read from the legacy schedule rows."""
    rows = db.execute(
        select(AiInterviewLink, Candidate, Opportunity, Customer)
        .join(Candidate, Candidate.id == AiInterviewLink.candidate_id, isouter=True)
        .join(Opportunity, Opportunity.id == AiInterviewLink.opportunity_id, isouter=True)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        # No date filter here: the time is not on this table. Bound it by the
        # created_at window instead, generously, then filter exactly below.
        .where(AiInterviewLink.created_at >= (window.start - timedelta(days=180)).replace(tzinfo=timezone.utc))
        .order_by(AiInterviewLink.id.desc())
    ).all()
    if not rows:
        return []

    schedules = _legacy_schedules([link.invite_token for link, *_ in rows])
    organisers = _organiser_names(db, [link.scheduled_by for link, *_ in rows])

    out: list[dict] = []
    for link, cand, opp, customer in rows:
        row = schedules.get(str(link.invite_token or "").strip()) or {}
        starts = parse_legacy_local(row.get("scheduled_at_local"))
        if starts is None or not (window.start <= starts < window.end):
            continue
        name = (str(row.get("candidate_name") or "").strip()
                or _candidate_name(cand, link.candidate_id))
        started = bool(row.get("interview_started_at") or row.get("verified_at"))
        out.append(_event(
            id=f"ai:{link.id}",
            source=SOURCE_AI,
            title=f"AI {link.level or 'L1'} Interview || {name}",
            starts_at=starts.isoformat(),
            ends_at=(starts + timedelta(minutes=DEFAULT_AI_DURATION_MINUTES)).isoformat(),
            duration_minutes=DEFAULT_AI_DURATION_MINUTES,
            candidate_id=link.candidate_id,
            candidate_name=name,
            candidate_email=(str(row.get("candidate_email") or "").strip()
                             or getattr(cand, "email", None)),
            profile_id=link.profile_id,
            opportunity_id=link.opportunity_id,
            opportunity_title=getattr(opp, "title", None),
            opportunity_code=getattr(opp, "opp_id", None),
            customer_name=getattr(customer, "name", None),
            kind="AI_L1",
            round_label=f"AI {link.level or 'L1'}",
            mode="AI",
            status=str(row.get("session_status") or row.get("status") or "").strip() or None,
            # effective_result surfaces a recruiter override when one exists,
            # so the calendar agrees with the candidate profile.
            result=link.effective_result,
            note=str(row.get("notes") or "").strip() or None,
            organiser=organisers.get(link.scheduled_by),
            organiser_user_id=link.scheduled_by,
            cv_url=getattr(cand, "cv_url", None),
            detail_path=f"profiles/{link.profile_id}" if link.profile_id else None,
            report_link=(
                f"/admin?view=candidateReport&cid={(getattr(cand, 'email', '') or '').lower()}"
                f"&iid={link.interview_record_id}"
                if link.interview_record_id and getattr(cand, "email", None) else None
            ),
            # A session the candidate has already opened must not be silently moved.
            can_modify=bool(link.result == "Pending" and not started),
        ))
    return out


def _legacy_schedules(tokens) -> dict[str, dict]:
    """Bulk-read the legacy schedule rows. Never raises."""
    try:
        from auth_db import get_schedules_by_tokens
        from crm_db import crm_database_url

        target = crm_database_url().replace("postgresql+psycopg2://", "postgresql://", 1)
        return get_schedules_by_tokens(target, tokens)
    except Exception as exc:
        logger.warning("Calendar could not read legacy schedules: %s", exc)
        return {}


def calendar_events(db: Session, window: CalendarRange, *,
                    sources: set[str] | None = None) -> list[dict]:
    """Every scheduled interview in the window, soonest first."""
    wanted = sources or {SOURCE_AI, SOURCE_MANUAL}
    events: list[dict] = []
    if SOURCE_MANUAL in wanted:
        events.extend(_manual_round_events(db, window))
    if SOURCE_AI in wanted:
        events.extend(_ai_interview_events(db, window))
    events.sort(key=lambda e: (e["starts_at"], e["title"]))
    return events


def undated_rounds(db: Session, limit: int = 50) -> list[dict]:
    """Rounds with a free-form 'when' but no real timestamp.

    They cannot be placed on the grid, but dropping them entirely would hide
    work that exists — the old dashboard widget listed them under "Date TBC".
    """
    rows = db.execute(
        select(InterviewEvent, Candidate)
        .join(Candidate, Candidate.id == InterviewEvent.candidate_id, isouter=True)
        .where(InterviewEvent.scheduled_at.is_(None))
        .order_by(InterviewEvent.id.desc())
        .limit(limit)
    ).all()
    return [
        _event(
            id=f"manual:{ev.id}",
            source=SOURCE_MANUAL,
            title=f"{_round_display(ev.kind, ev.interview_category, ev.stage)} || "
                  f"{_candidate_name(cand, ev.candidate_id)}",
            starts_at=None,
            raw_when=ev.raw_when,
            candidate_id=ev.candidate_id,
            candidate_name=_candidate_name(cand, ev.candidate_id),
            profile_id=ev.profile_id,
            kind=ev.kind,
            round_label=_round_display(ev.kind, ev.interview_category, ev.stage),
            panel=[ev.interviewer] if (ev.interviewer or "").strip() else [],
            status=ev.status,
            meeting_link=ev.meeting_link,
            note=ev.note,
            detail_path=f"profiles/{ev.profile_id}" if ev.profile_id else None,
        )
        for ev, cand in rows
    ]
