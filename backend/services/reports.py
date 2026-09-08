"""Read-only queries behind /api/reports/* — flat row dicts, CSV-friendly.

Every function returns list[dict] with scalar values only, so the router can
hand the same rows to schemas.common.envelope or services.crm_common.rows_to_csv.
No commits anywhere in this module.
"""
from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AtsStatus,
    Candidate,
    CandidateProfile,
    CandidateProfileActivityLog,
    Customer,
    Opportunity,
    PipelineStage,
    PipelineStatus,
    Resume,
    Role,
    RoleName,
    UserRole,
)


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
                         status: str | None = None) -> list[dict]:
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
        )
        .join(Customer, Customer.id == Opportunity.customer_id)
        .order_by(Opportunity.created_at.desc())
    )
    stages = _resolve_opp_stages(status)
    if stages is not None:
        stmt = stmt.where(Opportunity.pipeline_stage.in_(stages))
    if team and team.strip():
        stmt = stmt.where(Opportunity.created_by.in_(_team_user_ids_select(team)))

    rows = db.execute(stmt).all()
    usernames = _usernames(db, (row[6] for row in rows))
    return [
        {
            "opp_id": opp_id,
            "title": title,
            "customer": customer_name,
            "stage": _ev(stage),
            "opp_type": _ev(opp_type),
            "rfi_value": _fnum(rfi_value),
            "created_by_username": usernames.get(created_by, f"user:{created_by}"),
            "created_at": _iso(created_at),
        }
        for opp_id, title, customer_name, stage, opp_type, rfi_value, created_by, created_at in rows
    ]


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
                              status: str | None = None) -> list[dict]:
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
        )
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .order_by(CandidateProfile.created_at.desc())
    )
    statuses = _resolve_profile_statuses(status)
    if statuses is not None:
        stmt = stmt.where(CandidateProfile.pipeline_status.in_(statuses))
    if team and team.strip():
        creator = _profile_creator_subquery()
        stmt = stmt.join(creator, creator.c.profile_id == CandidateProfile.id).where(
            creator.c.creator_id.in_(_team_user_ids_select(team))
        )

    rows = db.execute(stmt).all()
    return [
        {
            "candidate_name": " ".join(part for part in (first_name, last_name) if part),
            "opportunity": opp_title,
            "pipeline_status": _ev(pipeline_status),
            "current_ctc": _fnum(current_ctc),
            "expected_ctc": _fnum(expected_ctc),
            "hike_percent": _fnum(hike_percent),
            "created_at": _iso(created_at),
        }
        for (first_name, last_name, opp_title, pipeline_status,
             current_ctc, expected_ctc, hike_percent, created_at) in rows
    ]


# ---------------------------------------------------------------------------
# Recruiter productivity report
# ---------------------------------------------------------------------------

def recruiter_productivity_report(db: Session, date_from: date | None = None,
                                  date_to: date | None = None) -> list[dict]:
    resume_conds = []
    profile_conds = []
    if date_from is not None:
        resume_conds.append(sa.cast(Resume.created_at, sa.Date) >= date_from)
        profile_conds.append(sa.cast(CandidateProfile.created_at, sa.Date) >= date_from)
    if date_to is not None:
        resume_conds.append(sa.cast(Resume.created_at, sa.Date) <= date_to)
        profile_conds.append(sa.cast(CandidateProfile.created_at, sa.Date) <= date_to)

    # CAVEAT: resumes has no dedicated uploader column, so screened_by is used
    # as the "uploader" proxy — resumes not yet screened are unattributed.
    resume_rows = db.execute(
        select(
            Resume.screened_by,
            func.count(Resume.id),  # resumes_uploaded (screened_by proxy, see caveat)
            func.count(Resume.id).filter(Resume.ats_score.is_not(None)),  # scans_run
            func.count(Resume.id).filter(Resume.ats_status == AtsStatus.SHORTLISTED),
        )
        .where(Resume.screened_by.is_not(None), *resume_conds)
        .group_by(Resume.screened_by)
    ).all()

    # Profiles "created" by a user = earliest activity-log entry attribution
    # (candidate_profiles has no created_by column).
    creator = _profile_creator_subquery()
    profile_rows = db.execute(
        select(creator.c.creator_id, func.count(creator.c.profile_id))
        .join(CandidateProfile, CandidateProfile.id == creator.c.profile_id)
        .where(*profile_conds)
        .group_by(creator.c.creator_id)
    ).all()
    profiles_by_user = {user_id: count for user_id, count in profile_rows}

    stats: dict[int, dict] = {}
    for user_id, uploaded, scans, shortlisted in resume_rows:
        stats[user_id] = {"resumes_uploaded": uploaded, "scans_run": scans,
                          "shortlisted": shortlisted}
    for user_id, created in profiles_by_user.items():
        stats.setdefault(user_id, {"resumes_uploaded": 0, "scans_run": 0, "shortlisted": 0})
        stats[user_id]["profiles_created"] = created
    for entry in stats.values():
        entry.setdefault("profiles_created", 0)

    # Per-day average window: explicit range when given, otherwise from the
    # earliest resume on record through today.
    today = date.today()
    end = date_to or today
    start = date_from
    if start is None:
        start = db.execute(select(func.min(sa.cast(Resume.created_at, sa.Date)))).scalar() or end
    days = max((end - start).days + 1, 1)

    usernames = _usernames(db, stats.keys())
    rows = [
        {
            "username": usernames.get(user_id, f"user:{user_id}"),
            "resumes_uploaded": entry["resumes_uploaded"],
            "scans_run": entry["scans_run"],
            "shortlisted": entry["shortlisted"],
            "profiles_created": entry["profiles_created"],
            "per_day_avg": round((entry["resumes_uploaded"] + entry["profiles_created"]) / days, 2),
        }
        for user_id, entry in stats.items()
    ]
    rows.sort(key=lambda r: (r["resumes_uploaded"], r["profiles_created"]), reverse=True)
    return rows
