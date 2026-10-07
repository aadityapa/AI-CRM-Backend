"""Read-only queries behind /api/reports/* — flat row dicts, CSV-friendly.

Every function returns list[dict] with scalar values only, so the router can
hand the same rows to schemas.common.envelope or services.crm_common.rows_to_csv.
No commits anywhere in this module.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink,
    Candidate,
    CandidateProfile,
    CandidateProfileActivityLog,
    Customer,
    Opportunity,
    PipelineStage,
    PipelineStatus,
    Role,
    RoleName,
    UserRole,
)
from services.candidate_profiles import REJECTED_BUCKET
from services.candidate_status import _STATUS_CHANGE_RE
from services.ist import IST, to_ist


def _ev(value):
    """Enum -> spec string; anything else passes through."""
    return value.value if hasattr(value, "value") else value


def _fnum(value) -> float | None:
    return float(value) if value is not None else None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _usernames(db: Session, user_ids) -> dict[int, str]:
    """id -> username from the legacy registration_data table (raw SQL — the
    ORM stub for that table only maps the id column)."""
    ids = sorted({i for i in user_ids if i is not None})
    if not ids:
        return {}
    rows = db.execute(
        sa.text("SELECT id, username FROM registration_data WHERE id IN :ids")
        .bindparams(sa.bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# Team filter: users holding a given CRM role
# ---------------------------------------------------------------------------

_TEAM_ROLES = {"sales": RoleName.SALES, "rmg": RoleName.RMG, "ta": RoleName.TA}


def _team_user_ids_select(team: str):
    """Subquery of registration_data ids that hold the given CRM role."""
    role = _TEAM_ROLES.get(team.strip().lower())
    if role is None:
        raise HTTPException(status_code=400, detail="team must be one of: Sales, RMG, TA")
    return (
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.name == role)
    )


# ---------------------------------------------------------------------------
# Opportunities report
# ---------------------------------------------------------------------------

# UI status groups -> pipeline stages (normalized keys: lowercase, spaces->underscores)
_OPP_STATUS_GROUPS: dict[str, list[PipelineStage]] = {
    "active": [PipelineStage.NEW, PipelineStage.ACTIVE],
    "on_hold": [PipelineStage.ON_HOLD, PipelineStage.SALES_HOLD],
    "rejected": [PipelineStage.REJECTED],
    "closed": [PipelineStage.CLOSED_WON, PipelineStage.CLOSED_LOST, PipelineStage.CLOSED_PARTIAL],
    "archived": [PipelineStage.ARCHIVED],
}


def _resolve_opp_stages(status: str | None) -> list[PipelineStage] | None:
    if not status or not status.strip():
        return None
    key = status.strip().lower().replace(" ", "_")
    if key in _OPP_STATUS_GROUPS:
        return _OPP_STATUS_GROUPS[key]
    for stage in PipelineStage:  # exact enum value (case-insensitive)
        if stage.value.lower() == key:
            return [stage]
    raise HTTPException(status_code=400, detail=f"Unknown opportunity status filter: {status}")


def opportunities_report(db: Session, team: str | None = None,
                         status: str | None = None, *, customer_id: int | None = None,
                         date_from: date | None = None, date_to: date | None = None) -> list[dict]:
    """Every deal, with the figures the Opportunities list prints (7 Oct 2026):
    the creator's NAME (the username used to be the only thing shown), the
    approval status, the position's status, open / total positions and the
    Joined count — the same `positions_by_opportunity` the list reads, so the
    report can never disagree with the page. `customer_id` and a created-at
    window narrow it; the CSV gets the same columns."""
    stmt = (
        select(
            Opportunity.opp_id,
            Opportunity.title,
            Customer.name,
            Opportunity.pipeline_stage,
            Opportunity.opp_type,
            Opportunity.rfi_value,
            Opportunity.created_by,
            Opportunity.created_at,
            Opportunity.id,
            Opportunity.approval_status,
            Customer.id,
        )
        .join(Customer, Customer.id == Opportunity.customer_id)
        .order_by(Opportunity.created_at.desc())
    )
    stages = _resolve_opp_stages(status)
    if stages is not None:
        stmt = stmt.where(Opportunity.pipeline_stage.in_(stages))
    if team and team.strip():
        stmt = stmt.where(Opportunity.created_by.in_(_team_user_ids_select(team)))
    if customer_id:
        stmt = stmt.where(Opportunity.customer_id == customer_id)
    for cond in _in_window(Opportunity.created_at, date_from, date_to):
        stmt = stmt.where(cond)

    rows = db.execute(stmt).all()
    names = _display_names(db, (row[6] for row in rows))
    from services.requirements import positions_by_opportunity
    positions = positions_by_opportunity(db, [row[8] for row in rows])
    out = []
    for opp_id, title, customer_name, stage, opp_type, rfi_value, created_by, created_at, oid, approval, cid in rows:
        pos = positions.get(oid) or {}
        who = names.get(created_by) or {}
        out.append({
            "opp_id": opp_id,
            "title": title,
            "customer": customer_name,
            "customer_id": cid,
            "stage": _ev(stage),
            "approval_status": _ev(approval),
            "opp_type": _ev(opp_type),
            "rfi_value": _fnum(rfi_value),
            "positions_total": pos.get("positions_total"),
            "positions_open": pos.get("positions_open"),
            "positions_joined": pos.get("positions_joined"),
            "position_status": pos.get("requirement_display_status") or pos.get("requirement_status"),
            "created_by": who.get("name") or (f"user:{created_by}" if created_by else "—"),
            "created_by_username": who.get("username") or "",
            "created_at": _iso(created_at),
        })
    return out


def opportunities_summary(rows: list[dict]) -> dict:
    """The strip above the table: count, RFI total, by stage, by type,
    positions open / total / joined. PURE."""
    by_stage: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in rows:
        by_stage[r.get("stage") or "—"] = by_stage.get(r.get("stage") or "—", 0) + 1
        by_type[r.get("opp_type") or "—"] = by_type.get(r.get("opp_type") or "—", 0) + 1
    return {
        "count": len(rows),
        "rfi_total": round(sum(r.get("rfi_value") or 0 for r in rows), 2),
        "rfi_known": sum(1 for r in rows if r.get("rfi_value") is not None),
        "positions_total": sum(r.get("positions_total") or 0 for r in rows),
        "positions_open": sum(r.get("positions_open") or 0 for r in rows),
        "positions_joined": sum(r.get("positions_joined") or 0 for r in rows),
        "by_stage": by_stage,
        "by_type": by_type,
    }


# ---------------------------------------------------------------------------
# Candidate profiles report
# ---------------------------------------------------------------------------

_PROFILE_TERMINAL_REJECTED = [
    PipelineStatus.SALES_REJECTED,
    PipelineStatus.RMG_REJECTED,
    PipelineStatus.CUSTOMER_REJECTED,
    PipelineStatus.CUSTOMER_SCREEN_REJECTED,
    PipelineStatus.CUSTOMER_L1_REJECTED,
    PipelineStatus.CUSTOMER_L2_REJECTED,
    PipelineStatus.SELF_WITHDRAWN,
    PipelineStatus.REJECTED,
]
_PROFILE_ACTIVE = [
    s for s in PipelineStatus
    if s not in _PROFILE_TERMINAL_REJECTED and s is not PipelineStatus.JOINED
]
_PROFILE_STATUS_GROUPS: dict[str, list[PipelineStatus]] = {
    "active": _PROFILE_ACTIVE,
    "rejected": _PROFILE_TERMINAL_REJECTED,
    "joined": [PipelineStatus.JOINED],
}


def _resolve_profile_statuses(status: str | None) -> list[PipelineStatus] | None:
    if not status or not status.strip():
        return None
    key = status.strip().lower().replace(" ", "_")
    if key in _PROFILE_STATUS_GROUPS:
        return _PROFILE_STATUS_GROUPS[key]
    for member in PipelineStatus:  # exact enum value (case-insensitive)
        if member.value.lower() == key:
            return [member]
    raise HTTPException(status_code=400, detail=f"Unknown profile status filter: {status}")


def _profile_creator_subquery():
    """candidate_profiles has no created_by column, so the pragmatic 'creator'
    is the user on each profile's EARLIEST activity-log row (min log id)."""
    first_log = (
        select(
            CandidateProfileActivityLog.profile_id.label("profile_id"),
            func.min(CandidateProfileActivityLog.id).label("min_log_id"),
        )
        .group_by(CandidateProfileActivityLog.profile_id)
        .subquery()
    )
    return (
        select(
            CandidateProfileActivityLog.profile_id.label("profile_id"),
            CandidateProfileActivityLog.user_id.label("creator_id"),
        )
        .join(first_log, CandidateProfileActivityLog.id == first_log.c.min_log_id)
        .subquery()
    )


def candidate_profiles_report(db: Session, team: str | None = None,
                              status: str | None = None, *, customer_id: int | None = None,
                              date_from: date | None = None, date_to: date | None = None) -> list[dict]:
    """Every candidacy with the words the Candidate Profiles list prints
    (derived status), the deal's id, the customer and the TA. `customer_id`
    and an applied-on window (falls back to the created date) narrow it."""
    stmt = (
        select(
            Candidate.first_name,
            Candidate.last_name,
            Opportunity.title,
            CandidateProfile.pipeline_status,
            CandidateProfile.current_ctc,
            CandidateProfile.expected_ctc,
            CandidateProfile.hike_percent,
            CandidateProfile.created_at,
            CandidateProfile.id,
            CandidateProfile.rmg_screening_status,
            CandidateProfile.withdrawn_from_status,
            CandidateProfile.ta_owner_name,
            CandidateProfile.applied_on,
            Customer.name.label("customer"),
            Opportunity.opp_id,
            Customer.id.label("customer_id"),
            Candidate.email,
        )
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        .order_by(CandidateProfile.created_at.desc())
    )
    statuses = _resolve_profile_statuses(status)
    if statuses is not None:
        stmt = stmt.where(CandidateProfile.pipeline_status.in_(statuses))
    if customer_id:
        stmt = stmt.where(Opportunity.customer_id == customer_id)
    # Applied date when stamped, else the day the candidacy was created —
    # `applied_on` is a DATE and `created_at` a timestamp, so each gets its
    # own predicate and the OR picks whichever the row carries.
    if date_from is not None or date_to is not None:
        on_applied = _in_window(CandidateProfile.applied_on, date_from, date_to, is_date=True)
        on_created = _in_window(CandidateProfile.created_at, date_from, date_to)
        stmt = stmt.where(or_(and_(CandidateProfile.applied_on.isnot(None), *on_applied),
                              and_(CandidateProfile.applied_on.is_(None), *on_created)))
    if team and team.strip():
        if team.strip().lower() == "ta":
            # The TA team's candidacies are the ones they OWN (ta_owner_id is
            # stamped at apply time) — the earliest activity row would credit
            # whoever touched the profile first (7 Oct 2026).
            stmt = stmt.where(CandidateProfile.ta_owner_id.in_(_team_user_ids_select(team)))
        else:
            creator = _profile_creator_subquery()
            stmt = stmt.join(creator, creator.c.profile_id == CandidateProfile.id).where(
                creator.c.creator_id.in_(_team_user_ids_select(team))
            )

    rows = db.execute(stmt).all()
    # The same status words the Candidate Profiles list shows.
    from services.candidate_status import statuses_for
    statuses = statuses_for(db, rows)
    return [
        {
            "candidate_name": " ".join(part for part in (row.first_name, row.last_name) if part),
            "candidate_email": row.email,
            "opportunity": row.title,
            "opp_id": row.opp_id,
            "customer": row.customer,
            "customer_id": row.customer_id,
            "ta_owner": row.ta_owner_name,
            "applied_on": _iso(row.applied_on),
            "pipeline_status": _ev(row.pipeline_status),
            "candidate_status": statuses.get(row.id),
            "current_ctc": _fnum(row.current_ctc),
            "expected_ctc": _fnum(row.expected_ctc),
            "hike_percent": _fnum(row.hike_percent),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def candidate_profiles_summary(rows: list[dict]) -> dict:
    """The strip above the table: count, live / closed / joined, by stage
    (the derived stage every screen shows), average hike. PURE."""
    by_stage: dict[str, int] = {}
    by_group: dict[str, int] = {}
    hikes = [r["hike_percent"] for r in rows if r.get("hike_percent") is not None]
    for r in rows:
        st = r.get("candidate_status") or {}
        stage = ((st.get("stage") or {}).get("label")) or (r.get("pipeline_status") or "—")
        by_stage[stage] = by_stage.get(stage, 0) + 1
        grp = st.get("group") or "—"
        by_group[grp] = by_group.get(grp, 0) + 1
    joined = sum(1 for r in rows if r.get("pipeline_status") == PipelineStatus.JOINED.value)
    closed = sum(1 for r in rows if r.get("pipeline_status") in {s.value for s in _PROFILE_TERMINAL_REJECTED})
    return {
        "count": len(rows),
        "joined": joined,
        "closed": closed,
        "live": len(rows) - joined - closed,
        "avg_hike_percent": round(sum(hikes) / len(hikes), 1) if hikes else None,
        "by_stage": by_stage,
        "by_group": by_group,
    }


# ---------------------------------------------------------------------------
# Recruiter productivity report (rewritten 7 Oct 2026)
# ---------------------------------------------------------------------------
# The old report counted `resumes.screened_by` (whoever ran the ATS — RMG as
# often as the uploader), "shortlisted" was the ATS status, "profiles created"
# was the earliest activity-log row (a Zoho import credited the importing
# admin with 628 profiles) and the per-day average divided by every calendar
# day since the first resume on record. None of that is a number a TA can be
# shown in a meeting. Every figure below is attributed by the column the
# application stamps for exactly that act, dated by when the act happened,
# and counted INSIDE the window:
#
#   candidates_added    candidates.created_by_id / created_at (0101)
#   applied             candidate_profiles.ta_owner_id, dated applied_on
#                       (else created_at) — the candidacy the TA raised
#   opening_emails      OPENING_MAIL_SENT activity rows by user
#   sent_for_screening  SENT_FOR_SCREENING activity rows by user (distinct
#                       candidacies — a resend is not a second send)
#   interviews_scheduled  AI L1 links `scheduled_by` + manual L1/L2/HR rounds
#                       the user booked (`L1_FACE_TO_FACE` …) + rounds added
#                       WITHOUT a verdict (a schedule, not a feedback record)
#   rmg_shortlisted     the TA's candidacies RMG/GM shortlisted, dated
#                       rmg_screening_at
#   submitted_to_sales / to_customer   the TA's candidacies, dated by the
#                       workflow stamps sales_submission_date /
#                       customer_submission_date
#   selected · joined · rejected   the TA's candidacies, dated by the
#                       STATUS_CHANGE row that moved them ("<from> -> <to>")
#   days_active         distinct days the TA added / applied / sent / booked
#   per_day_avg         applied ÷ working days (Mon–Fri) in the window
#
# Rows are every login holding the TA role (zeros included — a quiet week is
# a fact) plus anyone else with attributed work, flagged `is_ta = False`.
# ---------------------------------------------------------------------------

#: Activity rows that mean "the user booked an interview" (plus AI links).
_BOOKED_ROUND_ACTIONS = ("L1_FACE_TO_FACE", "L2_FACE_TO_FACE", "HR_FACE_TO_FACE", "AI_L1_SCHEDULED",
                         "AI_INTERVIEW_SCHEDULED", "SLOT_INVITE_SENT")
#: `create_interview_round` logs this for a schedule AND for a recorded
#: verdict; only the comment tells them apart ("<round> recorded — <result>").
_ROUND_ADDED_ACTION = "INTERVIEW_ROUND_ADDED"
_SENT_FOR_SCREENING_ACTION = "SENT_FOR_SCREENING"
_OPENING_MAIL_ACTION = "OPENING_MAIL_SENT"
_STATUS_CHANGE_ACTION = "STATUS_CHANGE"
_SELECTED_STATUS = PipelineStatus.SHORTLISTED.value   # "Customer Shortlisted"
_JOINED_STATUS = PipelineStatus.JOINED.value

PRODUCTIVITY_COLUMNS: list[dict] = [
    {"key": "candidates_added", "label": "Added", "group": "sourcing",
     "hint": "Candidate records this recruiter created (upload, bulk ZIP, Candidates page)."},
    {"key": "applied", "label": "Applied", "group": "sourcing",
     "hint": "Candidacies raised on a position — the recruiter is the TA owner, dated by the apply date."},
    {"key": "opening_emails", "label": "Emails", "group": "sourcing",
     "hint": "'Are you interested?' mails sent to candidates."},
    {"key": "sent_for_screening", "label": "Screening", "group": "sourcing",
     "hint": "Candidacies the recruiter pushed to Technical Screening (RMG / GM)."},
    {"key": "rmg_shortlisted", "label": "Shortlisted", "group": "pipeline",
     "hint": "Of the recruiter's candidacies, how many RMG / GM shortlisted in the window."},
    {"key": "interviews_scheduled", "label": "Interviews", "group": "pipeline",
     "hint": "AI L1 links, manual L1 / L2 / HR rounds and slot invites the recruiter booked."},
    {"key": "submitted_to_sales", "label": "To Sales", "group": "pipeline",
     "hint": "The recruiter's candidacies that reached Sales Screening."},
    {"key": "to_customer", "label": "To customer", "group": "pipeline",
     "hint": "The recruiter's candidacies submitted to the customer."},
    {"key": "selected", "label": "Selected", "group": "outcome",
     "hint": "The recruiter's candidacies the customer shortlisted."},
    {"key": "joined", "label": "Joined", "group": "outcome",
     "hint": "The recruiter's candidacies that joined."},
    {"key": "rejected", "label": "Closed", "group": "outcome",
     "hint": "The recruiter's candidacies closed at any stage (rejections and self-withdrawals)."},
    {"key": "days_active", "label": "Active days", "group": "pace",
     "hint": "Distinct days with at least one sourcing action."},
    {"key": "per_day_avg", "label": "Per day", "group": "pace",
     "hint": "Applied ÷ working days (Mon–Fri) in the window."},
]
_COUNT_KEYS = [c["key"] for c in PRODUCTIVITY_COLUMNS if c["key"] not in ("per_day_avg",)]


def working_days(start: date, end: date) -> int:
    """Mon–Fri days in [start, end] (holidays ignored on purpose — the same
    rule as the hiring tower's pace)."""
    if end < start:
        return 0
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def _display_names(db: Session, user_ids) -> dict[int, dict]:
    """id -> {name, username} from the legacy users table (raw SQL in a
    savepoint — the ORM stub and the test fixtures carry only `id`)."""
    ids = sorted({int(i) for i in user_ids if i is not None})
    if not ids:
        return {}
    try:
        with db.begin_nested():
            rows = db.execute(
                sa.text("SELECT id, full_name, username FROM registration_data WHERE id IN :ids")
                .bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": ids},
            ).all()
        return {int(r[0]): {"name": (r[1] or r[2] or f"User #{r[0]}"), "username": r[2] or ""}
                for r in rows}
    except Exception:  # noqa: BLE001 — names are cosmetic
        return {}


def _in_window(col, start: date | None, end: date | None, *, is_date: bool = False):
    """Window predicate on a date column, or on a timestamp compared against
    IST midnight bounds — no DB-side date maths, so SQLite and Postgres agree
    (``CAST(ts AS DATE)`` is a number on SQLite)."""
    conds = []
    if is_date:
        if start is not None:
            conds.append(col >= start)
        if end is not None:
            conds.append(col <= end)
        return conds
    if start is not None:
        conds.append(col >= datetime.combine(start, time.min, tzinfo=IST))
    if end is not None:
        conds.append(col < datetime.combine(end + timedelta(days=1), time.min, tzinfo=IST))
    return conds


def _owner_counts(db: Session, when_col, conds, *, is_date=False, extra=()) -> dict[int, int]:
    """ta_owner_id -> count of the TA's candidacies whose `when_col` falls in the window."""
    rows = db.execute(
        select(CandidateProfile.ta_owner_id, func.count(CandidateProfile.id))
        .where(CandidateProfile.ta_owner_id.is_not(None), *extra,
               *_in_window(when_col, *conds, is_date=is_date))
        .group_by(CandidateProfile.ta_owner_id)
    ).all()
    return {int(uid): int(n) for uid, n in rows}


def _add(stats: dict[int, dict], uid: int | None, key: str, n: int = 1) -> None:
    if uid is None:
        return
    entry = stats.setdefault(int(uid), {k: 0 for k in _COUNT_KEYS})
    entry[key] += n


def recruiter_productivity_report(db: Session, date_from: date | None = None,
                                  date_to: date | None = None) -> tuple[list[dict], dict]:
    """Rows (one per recruiter) + meta {window, working_days, columns}."""
    today = date.today()
    end = date_to or today
    Log = CandidateProfileActivityLog
    window = (date_from, end)
    stats: dict[int, dict] = {}
    active_days: dict[int, set[date]] = defaultdict(set)

    def touch(uid, when) -> None:
        if uid is not None and when is not None:
            active_days[int(uid)].add(to_ist(when).date() if isinstance(when, datetime) else when)

    # TA role holders — listed even at zero.
    ta_ids = {int(r[0]) for r in db.execute(
        select(UserRole.user_id).join(Role, Role.id == UserRole.role_id).where(Role.name == RoleName.TA)
    ).all()}
    for uid in ta_ids:
        stats.setdefault(uid, {k: 0 for k in _COUNT_KEYS})

    # Sourcing — candidates added (0101 stamps created_by_id).
    for uid, when in db.execute(
        select(Candidate.created_by_id, Candidate.created_at)
        .where(Candidate.created_by_id.is_not(None), *_in_window(Candidate.created_at, *window))
    ).all():
        _add(stats, uid, "candidates_added")
        touch(uid, when)

    # Applied — the candidacy the TA raised, dated by the apply date.
    applied_at = func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at)
    for uid, when in db.execute(
        select(CandidateProfile.ta_owner_id, applied_at)
        .where(CandidateProfile.ta_owner_id.is_not(None), *_in_window(applied_at, *window))
    ).all():
        _add(stats, uid, "applied")
        touch(uid, when)

    # Activity-log acts by the user: opening mails, sends to screening, bookings.
    log_rows = db.execute(
        select(Log.user_id, Log.action_type, Log.profile_id, Log.comment, Log.timestamp)
        .where(Log.action_type.in_((_OPENING_MAIL_ACTION, _SENT_FOR_SCREENING_ACTION,
                                    _ROUND_ADDED_ACTION, *_BOOKED_ROUND_ACTIONS)),
               *_in_window(Log.timestamp, *window))
    ).all()
    sent_seen: set[tuple[int, int]] = set()
    for uid, action, pid, comment, when in log_rows:
        if action == _OPENING_MAIL_ACTION:
            _add(stats, uid, "opening_emails")
        elif action == _SENT_FOR_SCREENING_ACTION:
            if (int(uid), int(pid)) in sent_seen:
                continue
            sent_seen.add((int(uid), int(pid)))
            _add(stats, uid, "sent_for_screening")
        elif action == _ROUND_ADDED_ACTION:
            # "<round> recorded — <result>" is a verdict, not a booking.
            if " — " in (comment or ""):
                continue
            _add(stats, uid, "interviews_scheduled")
        else:
            _add(stats, uid, "interviews_scheduled")
        touch(uid, when)
    for uid, when in db.execute(
        select(AiInterviewLink.scheduled_by, AiInterviewLink.created_at)
        .where(AiInterviewLink.scheduled_by.is_not(None),
               *_in_window(AiInterviewLink.created_at, *window))
    ).all():
        _add(stats, uid, "interviews_scheduled")
        touch(uid, when)

    # Outcomes on the TA's candidacies, dated by the stamp of that milestone.
    for uid, n in _owner_counts(db, CandidateProfile.rmg_screening_at, window,
                                extra=(CandidateProfile.rmg_screening_status == "Shortlisted",)).items():
        _add(stats, uid, "rmg_shortlisted", n)
    for uid, n in _owner_counts(db, CandidateProfile.sales_submission_date, window, is_date=True).items():
        _add(stats, uid, "submitted_to_sales", n)
    for uid, n in _owner_counts(db, CandidateProfile.customer_submission_date, window, is_date=True).items():
        _add(stats, uid, "to_customer", n)

    # Selected · joined · rejected: the STATUS_CHANGE row that made the move
    # ("<from> -> <to>: <reason>", the shape perform_transition always writes).
    moves = db.execute(
        select(CandidateProfile.ta_owner_id, Log.comment)
        .join(CandidateProfile, CandidateProfile.id == Log.profile_id)
        .where(Log.action_type == _STATUS_CHANGE_ACTION, CandidateProfile.ta_owner_id.is_not(None),
               *_in_window(Log.timestamp, *window))
    ).all()
    for uid, comment in moves:
        m = _STATUS_CHANGE_RE.match(comment or "")
        if not m:
            continue
        target = m.group(2)
        if target == _SELECTED_STATUS:
            _add(stats, uid, "selected")
        elif target == _JOINED_STATUS:
            _add(stats, uid, "joined")
        elif target in REJECTED_BUCKET:
            _add(stats, uid, "rejected")

    # Window + pace. With no explicit start the window opens on the earliest
    # day anyone was active (never "since the first resume on record").
    start = date_from
    if start is None:
        firsts = [min(days) for days in active_days.values() if days]
        start = min(firsts) if firsts else end
    wdays = max(working_days(start, end), 1)

    names = _display_names(db, stats.keys())
    rows = []
    for uid, entry in stats.items():
        who = names.get(uid, {})
        rows.append({
            "user_id": uid,
            "name": who.get("name") or f"User #{uid}",
            "username": who.get("username") or "",
            "is_ta": uid in ta_ids,
            **entry,
            "days_active": len(active_days.get(uid, ())),
            "per_day_avg": round(entry["applied"] / wdays, 2),
        })
    # Anyone outside the TA role with nothing attributed is noise, not a row.
    rows = [r for r in rows if r["is_ta"] or any(r[k] for k in _COUNT_KEYS)]
    rows.sort(key=lambda r: (-r["applied"], -r["candidates_added"], -r["joined"], r["name"].lower()))
    meta = {
        "window": {"from": start.isoformat(), "to": end.isoformat(), "explicit_from": date_from is not None},
        "working_days": wdays,
        "columns": PRODUCTIVITY_COLUMNS,
    }
    return rows, meta
