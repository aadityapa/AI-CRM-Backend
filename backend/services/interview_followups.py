"""Interviews whose time is over but whose verdict is missing (28 Sep 2026).

User ask: "when a candidate's interview date & time is over, remind the
responsible person on the Dashboard so the profile moves to the next step."

A round is FEEDBACK-DUE once `scheduled_at + duration` (default
`DEFAULT_DURATION_MIN`) has passed, it has no result, it was not cancelled /
no-show / rescheduled, and the candidacy is still live. Who must act is the
round's owner — the same split `interview_rounds.ROUND_WRITE_ROLES` enforces:

    L1–L4 (technical)     → whoever SCREENS (RMG · GM · a template's Approvals)
    HR round              → HR
    Customer L1 / L2      → Sales (they carry the customer's verdict back)

TA coordinates every round, so TA sees the rounds of the candidates they own.

One reader (`feedback_due`) feeds the Dashboard panel
(the "Feedback due" tab of `GET /api/dashboard/desk`, `services/work_desk.py`) and the reminder job
(`run_feedback_due_reminders`, at most one notice per round a day).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from models import Candidate, CandidateProfile, InterviewEvent, Opportunity
from services.interview_rounds import NOT_HELD_STATUSES as _NOT_HELD

logger = logging.getLogger("karnex.crm.interview_followups")

#: Assumed length of a round with no duration recorded.
DEFAULT_DURATION_MIN = 60
#: Rounds older than this are history, not a reminder (imported Zoho rows).
LOOKBACK_DAYS = 45

SCREENING, HR, SALES = "screening", "hr", "sales"
#: Who carries the customer's verdict back (29 Sep 2026: + the Sales Manager
#: custom role). A non-built-in name routes to the custom-role members.
CUSTOMER_ROUND_ROLES = ("Sales", "Sales_Head", "Sales Manager")
AREAS = {
    SCREENING: "RMG / GM",
    HR: "HR",
    SALES: "Sales",
}
_AREA_OF_KIND = {
    "L1_Interview": SCREENING, "L2_F2F": SCREENING, "L3_Interview": SCREENING,
    "L4_Interview": SCREENING, "HR_Interview": HR,
    "Customer_Interview": SALES, "Customer_L2": SALES,
}
_ROUND_LABEL = {
    "L1_Interview": "Technical L1", "L2_F2F": "Technical L2", "L3_Interview": "Technical L3",
    "L4_Interview": "Technical L4", "HR_Interview": "HR round",
    "Customer_Interview": "Customer L1", "Customer_L2": "Customer L2",
}
FEEDBACK_DUE_EVENT = "interview.feedback_due"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def areas_for(db: Session, user) -> set[str] | None:
    """The rounds this user answers for. None = every round (Admin / CEO)."""
    if getattr(user, "is_admin", False):
        return None
    from services.action_permissions import screens_as_rmg

    roles = set(getattr(user, "roles", None) or ())
    areas: set[str] = set()
    if screens_as_rmg(db, user):
        areas.add(SCREENING)
    if "HR" in roles:
        areas.add(HR)
    if roles & set(CUSTOMER_ROUND_ROLES):
        areas.add(SALES)
    return areas


def feedback_due(db: Session, *, now: datetime | None = None, areas: set[str] | None = None,
                 ta_owner_id: int | None = None, limit: int = 200) -> list[dict]:
    """Every held-but-unrecorded round, oldest first.

    `areas` narrows to the rounds of those owners (None = all); `ta_owner_id`
    adds the rounds of that TA's own candidates whatever the area.
    """
    from services.candidate_profiles import TERMINAL_STATUSES

    now = _aware(now) or datetime.now(timezone.utc)
    kinds = [k for k, a in _AREA_OF_KIND.items() if areas is None or a in areas]
    scope = [InterviewEvent.kind.in_(kinds)] if kinds else []
    if ta_owner_id is not None:
        scope.append(CandidateProfile.ta_owner_id == ta_owner_id)
    if not scope:
        return []
    rows = db.execute(
        select(InterviewEvent, CandidateProfile, Candidate, Opportunity)
        .join(CandidateProfile, CandidateProfile.id == InterviewEvent.profile_id)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .outerjoin(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(
            InterviewEvent.kind.in_(tuple(_AREA_OF_KIND)),
            or_(*scope),
            InterviewEvent.scheduled_at.isnot(None),
            InterviewEvent.scheduled_at <= now,
            InterviewEvent.scheduled_at >= now - timedelta(days=LOOKBACK_DAYS),
            func.coalesce(InterviewEvent.result, "") == "",
            func.coalesce(InterviewEvent.status, "").notin_(_NOT_HELD),
            func.coalesce(CandidateProfile.is_hidden, False).is_(False),
            CandidateProfile.pipeline_status.notin_(tuple(TERMINAL_STATUSES)),
        )
        .order_by(InterviewEvent.scheduled_at.asc())
        .limit(limit)
    ).all()
    out: list[dict] = []
    for ev, profile, cand, opp in rows:
        when = _aware(ev.scheduled_at)
        ends = when + timedelta(minutes=int(ev.duration_minutes or DEFAULT_DURATION_MIN))
        if ends > now:
            continue  # still running
        area = _AREA_OF_KIND[ev.kind]
        out.append({
            "event_id": ev.id,
            "profile_id": profile.id,
            "candidate_name": " ".join(x for x in (cand.first_name, cand.last_name) if x)
                              or f"Candidate #{cand.id}",
            "opportunity_id": getattr(opp, "id", None),
            "opportunity_ref": getattr(opp, "opp_id", None),
            "opportunity_title": getattr(opp, "title", None),
            "round_kind": ev.kind,
            "round_label": _ROUND_LABEL[ev.kind],
            "scheduled_at": when.isoformat(),
            "overdue_hours": round((now - ends).total_seconds() / 3600, 1),
            "area": area,
            "area_label": AREAS[area],
            "interviewer": ev.interviewer,
            "employee_id": ev.employee_id,
            "ta_owner_id": profile.ta_owner_id,
            "ta_owner_name": profile.ta_owner_name,
        })
    return out


def feedback_due_for(db: Session, user, *, now: datetime | None = None) -> dict:
    """The Dashboard panel: the rounds THIS user must record, plus the counts."""
    areas = areas_for(db, user)
    ta_id = getattr(user, "id", None) if "TA" in (getattr(user, "roles", None) or ()) else None
    if areas is not None and not areas and ta_id is None:
        return {"items": [], "total": 0, "by_area": {}}
    items = feedback_due(db, now=now, areas=areas, ta_owner_id=ta_id)
    by_area: dict[str, int] = {}
    for it in items:
        by_area[it["area"]] = by_area.get(it["area"], 0) + 1
    return {"items": items, "total": len(items), "by_area": by_area,
            "areas": {k: AREAS[k] for k in by_area}}


def _link(db: Session, item: dict) -> str:
    if item["area"] == SALES:  # Sales has no route to the requirement page
        return f"/admin/?view=crm&p=profiles/{item['profile_id']}&tab=interviews"
    from services.candidate_profiles import applied_candidates_link
    profile = db.get(CandidateProfile, item["profile_id"])
    return applied_candidates_link(db, profile)


REMINDER_ACTION = "FEEDBACK_REMINDER"
#: A reminder repeats at most once per this many hours while the verdict is missing.
REMIND_EVERY_HOURS = 24


def _reminded_recently(db: Session, items: list[dict], now: datetime) -> set[int]:
    """Round ids reminded within `REMIND_EVERY_HOURS` — read from the profile's
    activity log (one query), because a bell row is written on every call and
    the outbox dedupe only guards the email."""
    import re

    from models import CandidateProfileActivityLog as Log
    if not items:
        return set()
    rows = db.execute(
        select(Log.comment).where(
            Log.profile_id.in_({i["profile_id"] for i in items}),
            Log.action_type == REMINDER_ACTION,
            Log.timestamp >= now - timedelta(hours=REMIND_EVERY_HOURS),
        )
    ).scalars().all()
    return {int(m.group(1)) for d in rows for m in [re.search(r"round #(\d+)", d or "")] if m}


def run_feedback_due_reminders(db: Session, *, now: datetime | None = None) -> dict:
    """Scheduler job (every pass): a bell + email per overdue round to its
    owner, repeated once a day until the verdict is recorded. Screening rounds
    reach everyone who may screen, not just the role called RMG; a customer
    round reaches Sales, Sales Head, the Sales Manager AND the candidate's TAs
    (29 Sep 2026 — TA coordinates the round and chases the verdict)."""
    from services.candidate_profiles import screening_notify_user_ids, ta_user_ids
    from services.crm_common import log_activity
    from models import CandidateProfileActivityLog
    from services.notify import notify_roles

    now = _aware(now) or datetime.now(timezone.utc)
    items = feedback_due(db, now=now)
    done = _reminded_recently(db, items, now)
    items = [i for i in items if i["event_id"] not in done]
    screeners = screening_notify_user_ids(db) if any(i["area"] == SCREENING for i in items) else set()
    roles = {SCREENING: ["RMG"], HR: ["HR"], SALES: list(CUSTOMER_ROUND_ROLES)}
    sent = 0
    from services.panel_interviews import panel_user_ids
    for it in items:
        extra = screeners if it["area"] == SCREENING else None
        if it["area"] == SCREENING and it.get("employee_id"):
            # The panel member themselves (7 Oct 2026) — the login behind the
            # round's employee hears it on their My Interviews page too.
            extra = set(extra or ()) | set(panel_user_ids(db, it["employee_id"]))
        if it["area"] == SALES:
            profile = db.get(CandidateProfile, it["profile_id"])
            extra = set(ta_user_ids(db, profile)) if profile is not None else None
        title = f"Feedback due: {it['round_label']} — {it['candidate_name']}"
        message = (f"The {it['round_label']} interview with {it['candidate_name']}"
                   + (f" ({it['opportunity_ref']} — {it['opportunity_title']})"
                      if it["opportunity_ref"] else "")
                   + " is over. Record the verdict so the candidate moves to the next step.")
        try:
            with db.begin_nested():
                n = notify_roles(db, roles[it["area"]], title, message, _link(db, it),
                                 event=FEEDBACK_DUE_EVENT,
                                 user_ids=extra or None,
                                 dedupe_prefix=f"fb_due:{it['event_id']}:{now.date().isoformat()}")
                log_activity(db, CandidateProfileActivityLog, "profile_id", it["profile_id"], None,
                             REMINDER_ACTION,
                             f"Feedback reminder sent for the {it['round_label']} (round #{it['event_id']})")
            sent += 1 if n else 0
        except Exception:
            logger.warning("feedback-due notice failed for event %s", it["event_id"], exc_info=True)
    db.commit()
    return {"overdue": len(items) + len(done), "notified": sent}
