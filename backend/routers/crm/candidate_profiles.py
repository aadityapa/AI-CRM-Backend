"""Candidate profiles (candidate x opportunity): pipeline, evaluations, offers, activity log.

Pipeline transitions are validated server-side in services/candidate_profiles.py
(transition map + per-stage role authority). Reads: any CRM role.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, PageParams, any_crm_role, gated_create, get_crm_db, page_params, role_required, gated_write, gated_write_action
from routers.crm.candidates import _reject_impossible_ctc
from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, Customer,
    InterviewEvent, OfferHistory, OfferStatus, Opportunity, PipelineStatus,
)
from pydantic import BaseModel, Field

from schemas.candidate_profiles import (
    OfferCreate, OfferUpdate, ProfileCreate, ProfileStatusTransitionIn, ProfileUpdate,
    SkillEvaluationItem,
)
from schemas.common import envelope
from services.candidate_profiles import (
    applied_candidates_link,
    REJECTED_BUCKET, user_may_transition_from, backfill_profile_commercials, compute_hike_percent, enrich_profiles_list,
    get_profile_or_404, interview_event_to_dict, interview_events_for_profile,
    offer_to_dict, perform_transition, profile_detail, profile_to_dict, upsert_skill_evaluations,
    visible_statuses_for,
)
from services.candidates import candidate_search_clause
from services.crm_common import log_activity, paginate
from services.interview_rounds import (
    WRITE_ROLES as INTERVIEW_ROUND_WRITE_ROLES, ensure_may_write_round, get_round_or_404,
    options as interview_round_options_data, round_label, validate_round,
)

router = APIRouter(prefix="/api/candidate-profiles", tags=["CRM: Candidate Profiles"])
logger = logging.getLogger("karnex.crm.candidate_profiles")

create_roles = gated_write("profiles", "TA", "Sales", "RMG")
create_profiles_gate = gated_create("profiles", "TA", "Sales", "RMG")
evaluation_roles = gated_write("profiles", "RMG", "TA", "Sales")
#: Roles that may record an offer — on the Offers tab or alongside the status
#: change that requires one.
OFFER_WRITE_ROLES = ("Sales", "Sales_Head", "HR")
offer_roles = gated_write("profiles", *OFFER_WRITE_ROLES)
rmg_roles = gated_write_action("profile.rmg_screening", "profiles", "RMG")
#: Interview feedback is recorded by RMG (Admin/CEO are implicit in role_required).
interview_round_roles = gated_write("profiles", *INTERVIEW_ROUND_WRITE_ROLES)


def _status_val(status) -> str:
    return status.value if hasattr(status, "value") else str(status)



#: Multi-level sort: ?sort=customer:asc,pipeline_status:desc,expected_ctc:desc
#: Keys map to a SQL expression; anything not listed here cannot be ordered on
#: (the computed columns — CTC-slab budget, latest interview — have no column).
#: Latest AI L1 score for a profile, as a correlated scalar subquery.
#:
#: The score lives on ai_interview_links, one row per session, so it cannot be
#: a plain column reference — a profile can have several sessions and only the
#: most recent one is the answer. Ordering matches services.candidate_profiles
#: .latest_ai_interviews(): completed first, then newest, so a finished
#: interview always outranks a later-scheduled one that has not happened.
#:
#: This exists because the directory sorts by score by default. Without it the
#: sort key was silently dropped by _parse_sort and the table claimed an order
#: it did not have.
_LATEST_AI_SCORE = (
    select(AiInterviewLink.overall_score_percent)
    .where(AiInterviewLink.profile_id == CandidateProfile.id)
    .order_by(
        AiInterviewLink.completed_at.desc().nullslast(),
        AiInterviewLink.id.desc(),
    )
    .limit(1)
    .correlate(CandidateProfile)
    .scalar_subquery()
)

_SORTABLE = {
    "candidate_name": (Candidate.first_name, Candidate.last_name),
    "email": (Candidate.email,),
    "experience_years": (Candidate.experience_years,),
    "notice_period": (Candidate.notice_period,),
    "ai_interview": (_LATEST_AI_SCORE,),
    "opportunity": (Opportunity.title,),
    "customer": (Customer.name,),
    "pipeline_status": (CandidateProfile.pipeline_status,),
    "current_ctc": (CandidateProfile.current_ctc,),
    "expected_ctc": (CandidateProfile.expected_ctc,),
    "hike_percent": (CandidateProfile.hike_percent,),
    "ctc_approval_amount": (CandidateProfile.ctc_approval_amount,),
    "customer_submission_date": (CandidateProfile.customer_submission_date,),
    "customer_onboarding_date": (CandidateProfile.customer_onboarding_date,),
    "karnex_onboarding_date": (CandidateProfile.karnex_onboarding_date,),
    "ta_owner_name": (CandidateProfile.ta_owner_name,),
    "applied_on": (CandidateProfile.applied_on,),
    "created_at": (CandidateProfile.created_at,),
}
#: Sort keys needing a join, so it is added once and only when used.
_SORT_NEEDS_CANDIDATE = {"candidate_name", "email", "experience_years", "notice_period"}
_SORT_NEEDS_OPPORTUNITY = {"opportunity", "customer"}
MAX_SORT_LEVELS = 4


def _parse_sort(raw: str | None) -> list[tuple[str, bool]]:
    """"customer:asc,expected_ctc:desc" -> [("customer", False), ("expected_ctc", True)].

    Unknown keys are ignored rather than rejected: a saved layout referencing a
    column that has since been removed should degrade, not 400.
    """
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, direction = part.partition(":")
        key = key.strip()
        if key in _SORTABLE and key not in seen:
            seen.add(key)
            out.append((key, direction.strip().lower() == "desc"))
        if len(out) >= MAX_SORT_LEVELS:
            break
    return out


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_profiles(pp: PageParams = Depends(page_params),
                  pipeline_status: str | None = None,
                  opportunity_id: str | None = None,
                  candidate_id: int | None = None,
                  ta_owner_id: int | None = None,
                  bucket: str | None = None,
                  source: str | None = None,
                  include_hidden: bool = False,
                  sort: str | None = None,
                  # Per-column header filters (Aug 2026) — every one narrows the
                  # SERVER query, so they cover the whole dataset, not the page.
                  ai_min: float | None = None,
                  ai_max: float | None = None,
                  exp_min: float | None = None,
                  exp_max: float | None = None,
                  notice: str | None = None,
                  applied_from: date | None = None,
                  applied_to: date | None = None,
                  submitted_by: str | None = None,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    stmt = select(CandidateProfile)
    joined_candidate = False
    if pp.search:
        # Search candidate name / email / phone without N+1 (join once for the filter).
        # Full-name aware: "anand kumar" matches first_name + last_name together.
        stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        joined_candidate = True
        stmt = stmt.where(candidate_search_clause(pp.search))
    # Candidate-column filters share ONE join with search.
    if exp_min is not None or exp_max is not None or (notice and notice.strip()):
        if not joined_candidate:
            stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            joined_candidate = True
        if exp_min is not None:
            stmt = stmt.where(Candidate.experience_years >= exp_min)
        if exp_max is not None:
            stmt = stmt.where(Candidate.experience_years <= exp_max)
        if notice and notice.strip():
            stmt = stmt.where(Candidate.notice_period.ilike(f"%{notice.strip()}%"))
    if ai_min is not None:
        stmt = stmt.where(_LATEST_AI_SCORE >= ai_min)
    if ai_max is not None:
        stmt = stmt.where(_LATEST_AI_SCORE <= ai_max)
    if applied_from is not None:
        stmt = stmt.where(sa.func.date(CandidateProfile.applied_on) >= applied_from)
    if applied_to is not None:
        stmt = stmt.where(sa.func.date(CandidateProfile.applied_on) <= applied_to)
    if submitted_by and submitted_by.strip():
        stmt = stmt.where(CandidateProfile.created_by_name.ilike(f"%{submitted_by.strip()}%"))
    if pipeline_status:
        # Accepts one status or a comma-separated set, so a reviewer can watch
        # several stages at once ("everything waiting on me") rather than
        # paging through them one at a time.
        valid = {m.value for m in PipelineStatus}
        wanted = [s.strip() for s in pipeline_status.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in valid]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown pipeline_status {', '.join(repr(u) for u in unknown)}. "
                       f"Valid values: {', '.join(sorted(valid))}",
            )
        if wanted:
            stmt = stmt.where(
                CandidateProfile.pipeline_status.in_([PipelineStatus(s) for s in wanted])
            )
    if opportunity_id is not None and str(opportunity_id).strip():
        # One id or a comma-separated set (Aug 2026) — the profiles directory
        # lets a recruiter watch several roles at once.
        try:
            opp_ids = [int(x) for x in str(opportunity_id).split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="opportunity_id must be an id or a comma-separated list of ids")
        if opp_ids:
            stmt = stmt.where(CandidateProfile.opportunity_id.in_(opp_ids))
    if candidate_id is not None:
        stmt = stmt.where(CandidateProfile.candidate_id == candidate_id)
    if ta_owner_id is not None:
        # "Show only the applicants I submitted" — the Applicants tab filter.
        stmt = stmt.where(CandidateProfile.ta_owner_id == ta_owner_id)
    if source:
        # e.g. ?source=zoho_import to show only imported rows.
        stmt = stmt.where(CandidateProfile.source == source.strip())
    if not include_hidden:
        # is_hidden is NOT NULL with a false default, so this never drops rows
        # that predate the column.
        stmt = stmt.where(CandidateProfile.is_hidden.is_(False))
    if bucket:
        bucket = bucket.strip().lower()
        rejected_enums = [PipelineStatus(v) for v in sorted(REJECTED_BUCKET)]
        if bucket == "rejected":
            stmt = stmt.where(CandidateProfile.pipeline_status.in_(rejected_enums))
        elif bucket == "active":
            stmt = stmt.where(CandidateProfile.pipeline_status.not_in(rejected_enums))
        else:
            raise HTTPException(status_code=400, detail="bucket must be 'active' or 'rejected'")

    # Scope the list to the stages a role actually owns. Sales previously saw
    # every profile from Sourcing onward, including candidates still being
    # screened by TA and RMG — so the list was mostly other people's in-progress
    # work and the genuinely actionable rows were buried.
    visible = visible_statuses_for(user)
    if visible is not None:
        stmt = stmt.where(
            CandidateProfile.pipeline_status.in_([PipelineStatus(v) for v in sorted(visible)])
        )
    # ---- ordering -------------------------------------------------------
    levels = _parse_sort(sort)
    if levels:
        keys = {k for k, _ in levels}
        # Join only what the chosen sort keys actually need. `search` may have
        # joined Candidate already, so guard against joining it twice.
        if keys & _SORT_NEEDS_CANDIDATE and not joined_candidate:
            stmt = stmt.outerjoin(Candidate, Candidate.id == CandidateProfile.candidate_id)
        if keys & _SORT_NEEDS_OPPORTUNITY:
            stmt = stmt.outerjoin(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            if "customer" in keys:
                stmt = stmt.outerjoin(Customer, Customer.id == Opportunity.customer_id)
        clauses = []
        for key, desc in levels:
            for col in _SORTABLE[key]:
                clauses.append(col.desc().nullslast() if desc else col.asc().nullslast())
        # Stable tiebreak so pagination never repeats or drops a row.
        stmt = stmt.order_by(*clauses, CandidateProfile.id.desc())
    else:
        stmt = stmt.order_by(
            CandidateProfile.id.asc() if pp.sort_dir == "asc" else CandidateProfile.id.desc()
        )
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope(data=enrich_profiles_list(db, items), meta=meta)


# NOTE (route order): these literal routes MUST stay above GET /{profile_id},
# or FastAPI binds profile_id="ta-owners"/"export" and 404s them.

@router.get("/ta-owners")
def ta_owner_options(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(any_crm_role)):
    """Distinct TA owners for the profiles-list filter dropdown (Aug 2026).

    Sourced from the profiles themselves rather than /api/users (admin-only),
    so any CRM role can populate the filter.
    """
    rows = db.execute(
        select(CandidateProfile.ta_owner_id, CandidateProfile.ta_owner_name)
        .where(CandidateProfile.ta_owner_id.isnot(None))
        .distinct()
    ).all()
    best: dict[int, str] = {}
    for uid, name in rows:
        if uid not in best or (name and not best[uid]):
            best[uid] = (name or "").strip() or f"user:{uid}"
    return envelope(data=sorted(
        ({"id": uid, "name": name} for uid, name in best.items()),
        key=lambda r: r["name"].lower(),
    ))


_EXPORT_COLUMNS: list[tuple[str, str]] = [
    ("candidate_name", "Candidate"), ("email", "Email"), ("phone", "Phone"),
    ("opportunity_opp_id", "Opportunity"), ("opportunity_title", "Role"),
    ("customer_name", "Customer"), ("pipeline_status", "Status"),
    ("withdrawn_from_status", "Withdrew From"),
    ("ai_overall_score_percent", "AI Score %"), ("ai_result", "AI Result"),
    ("experience_years", "Exp (yrs)"), ("notice_period", "Notice"),
    ("current_ctc", "Current CTC"), ("expected_ctc", "Expected CTC"),
    ("approved_ctc", "Approved CTC"), ("ta_owner_name", "TA Owner"),
    ("created_by_name", "Submitted By"), ("applied_on", "Applied On"),
]
_EXPORT_FORMATS = ("csv", "tsv", "json", "xml", "html", "xlsx", "pdf")


@router.get("/export")
def export_profiles(format: str = "csv",
                    pipeline_status: str | None = None,
                    opportunity_id: str | None = None,
                    ta_owner_id: int | None = None,
                    bucket: str | None = None,
                    search: str | None = None,
                    ai_min: float | None = None,
                    ai_max: float | None = None,
                    exp_min: float | None = None,
                    exp_max: float | None = None,
                    notice: str | None = None,
                    applied_from: date | None = None,
                    applied_to: date | None = None,
                    submitted_by: str | None = None,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    """Export the (filtered) profiles list — CSV/TSV/JSON/XML/HTML/XLSX/PDF.

    Mirrors list_profiles' filters; capped at 5000 rows so an unfiltered export
    of a huge dataset cannot exhaust memory. Same role-visibility scoping as
    the list, so nobody exports rows they cannot see.
    """
    from fastapi import Response

    fmt = (format or "csv").strip().lower()
    if fmt not in _EXPORT_FORMATS:
        raise HTTPException(status_code=400,
                            detail=f"format must be one of: {', '.join(_EXPORT_FORMATS)}")

    stmt = select(CandidateProfile)
    joined_candidate = False
    if search and search.strip():
        stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        joined_candidate = True
        stmt = stmt.where(candidate_search_clause(search.strip()))
    if exp_min is not None or exp_max is not None or (notice and notice.strip()):
        if not joined_candidate:
            stmt = stmt.join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            joined_candidate = True
        if exp_min is not None:
            stmt = stmt.where(Candidate.experience_years >= exp_min)
        if exp_max is not None:
            stmt = stmt.where(Candidate.experience_years <= exp_max)
        if notice and notice.strip():
            stmt = stmt.where(Candidate.notice_period.ilike(f"%{notice.strip()}%"))
    if ai_min is not None:
        stmt = stmt.where(_LATEST_AI_SCORE >= ai_min)
    if ai_max is not None:
        stmt = stmt.where(_LATEST_AI_SCORE <= ai_max)
    if applied_from is not None:
        stmt = stmt.where(sa.func.date(CandidateProfile.applied_on) >= applied_from)
    if applied_to is not None:
        stmt = stmt.where(sa.func.date(CandidateProfile.applied_on) <= applied_to)
    if submitted_by and submitted_by.strip():
        stmt = stmt.where(CandidateProfile.created_by_name.ilike(f"%{submitted_by.strip()}%"))
    if pipeline_status:
        valid = {m.value for m in PipelineStatus}
        wanted = [s.strip() for s in pipeline_status.split(",") if s.strip() and s.strip() in valid]
        if wanted:
            stmt = stmt.where(CandidateProfile.pipeline_status.in_([PipelineStatus(s) for s in wanted]))
    if opportunity_id is not None and str(opportunity_id).strip():
        try:
            opp_ids = [int(x) for x in str(opportunity_id).split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="opportunity_id must be an id or a comma-separated list of ids")
        if opp_ids:
            stmt = stmt.where(CandidateProfile.opportunity_id.in_(opp_ids))
    if ta_owner_id is not None:
        stmt = stmt.where(CandidateProfile.ta_owner_id == ta_owner_id)
    stmt = stmt.where(CandidateProfile.is_hidden.is_(False))
    if bucket:
        rejected_enums = [PipelineStatus(v) for v in sorted(REJECTED_BUCKET)]
        if bucket.strip().lower() == "rejected":
            stmt = stmt.where(CandidateProfile.pipeline_status.in_(rejected_enums))
        elif bucket.strip().lower() == "active":
            stmt = stmt.where(CandidateProfile.pipeline_status.not_in(rejected_enums))
    visible = visible_statuses_for(user)
    if visible is not None:
        stmt = stmt.where(
            CandidateProfile.pipeline_status.in_([PipelineStatus(v) for v in sorted(visible)]))
    stmt = stmt.order_by(CandidateProfile.id.desc()).limit(5000)

    items = db.execute(stmt).scalars().all()
    data = enrich_profiles_list(db, items)
    rows = [[("" if r.get(k) is None else r.get(k)) for k, _ in _EXPORT_COLUMNS] for r in data]
    headers = [label for _, label in _EXPORT_COLUMNS]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    fname = f"candidate_profiles_{stamp}.{fmt}"
    disp = {"Content-Disposition": f'attachment; filename="{fname}"'}

    if fmt in ("csv", "tsv"):
        import csv as _csv
        import io as _io
        buf = _io.StringIO()
        w = _csv.writer(buf, delimiter="," if fmt == "csv" else "\t")
        w.writerow(headers)
        w.writerows(rows)
        media = "text/csv" if fmt == "csv" else "text/tab-separated-values"
        return Response(buf.getvalue(), media_type=media, headers=disp)

    if fmt == "json":
        import json as _json
        payload = [dict(zip([k for k, _ in _EXPORT_COLUMNS], row)) for row in rows]
        return Response(_json.dumps(payload, indent=2, default=str),
                        media_type="application/json", headers=disp)

    if fmt == "xml":
        from xml.sax.saxutils import escape as _esc
        parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<candidate_profiles>"]
        for row in rows:
            parts.append("  <profile>")
            for (key, _), value in zip(_EXPORT_COLUMNS, row):
                parts.append(f"    <{key}>{_esc(str(value))}</{key}>")
            parts.append("  </profile>")
        parts.append("</candidate_profiles>")
        return Response("\n".join(parts), media_type="application/xml", headers=disp)

    if fmt == "html":
        from xml.sax.saxutils import escape as _esc
        head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{_esc(str(v))}</td>" for v in row) + "</tr>" for row in rows)
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<title>Candidate Profiles</title>"
            "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px}"
            "h1{font-size:18px}table{border-collapse:collapse;width:100%;font-size:12px}"
            "th,td{border:1px solid #cbd5e1;padding:6px 8px;text-align:left}"
            "th{background:#f1f5f9}tr:nth-child(even){background:#f8fafc}</style></head>"
            f"<body><h1>Candidate Profiles ({len(rows)})</h1>"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"
        )
        return Response(html, media_type="text/html", headers=disp)

    if fmt == "xlsx":
        import io as _io
        from openpyxl import Workbook
        from openpyxl.styles import Font
        wb = Workbook()
        ws = wb.active
        ws.title = "Candidate Profiles"
        ws.append(headers)
        for c in ws[1]:
            c.font = Font(bold=True)
        for row in rows:
            ws.append([str(v) if not isinstance(v, (int, float)) else v for v in row])
        buf = _io.BytesIO()
        wb.save(buf)
        return Response(
            buf.getvalue(),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers=disp)

    # pdf — landscape table, trimmed to the columns that fit a page.
    import io as _io
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    pdf_cols = [0, 3, 4, 5, 6, 8, 10, 15, 17]  # the columns that fit landscape A4
    p_headers = [headers[i] for i in pdf_cols]
    p_rows = [[str(row[i])[:40] for i in pdf_cols] for row in rows]
    buf = _io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=10 * mm, rightMargin=10 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    styles = getSampleStyleSheet()
    tbl = Table([p_headers] + p_rows, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e40af")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    doc.build([Paragraph(f"Candidate Profiles ({len(rows)})", styles["Heading2"]),
               Spacer(1, 4 * mm), tbl])
    return Response(buf.getvalue(), media_type="application/pdf", headers=disp)


@router.post("")
def create_profile(payload: ProfileCreate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(create_profiles_gate)):
    if not db.get(Candidate, payload.candidate_id):
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not db.get(Opportunity, payload.opportunity_id):
        raise HTTPException(status_code=404, detail="Opportunity not found")
    duplicate = db.execute(
        select(CandidateProfile).where(
            CandidateProfile.candidate_id == payload.candidate_id,
            CandidateProfile.opportunity_id == payload.opportunity_id)
    ).scalars().first()
    if duplicate:
        # Structured 409 (user decision, 25 Aug 2026): another TA hitting this
        # must see WHO already applied the candidate and get a link — a bare
        # "already exists" left them re-typing the same person repeatedly.
        cand = db.get(Candidate, payload.candidate_id)
        cname = " ".join(p for p in (getattr(cand, "first_name", None),
                                     getattr(cand, "last_name", None)) if p)
        raise HTTPException(status_code=409, detail={
            "message": (f"{cname or 'This candidate'} was already applied to this opportunity"
                        + (f" by {duplicate.ta_owner_name}" if duplicate.ta_owner_name else "")
                        + "."),
            "duplicate_profile": {
                "profile_id": duplicate.id,
                "candidate_name": cname,
                "applied_by": duplicate.ta_owner_name,
                "applied_on": duplicate.applied_on.isoformat() if duplicate.applied_on else None,
                "pipeline_status": getattr(duplicate.pipeline_status, "value",
                                           duplicate.pipeline_status),
            },
        })
    values = payload.model_dump(exclude_unset=True)
    values.pop("commercial_approved", None)
    notes = (values.pop("notes", None) or "").strip()
    _reject_impossible_ctc(values)
    profile = CandidateProfile(**values)
    if payload.commercial_approved is not None:
        profile.commercial_approved = payload.commercial_approved
    profile.hike_percent = compute_hike_percent(payload.current_ctc, payload.expected_ctc)
    # Attribute the application to the TA acting now, so the Applicants list and
    # the TA dashboard can show "who added this candidate". Imports set
    # ta_owner_id explicitly, so we only stamp when it isn't already set.
    if profile.ta_owner_id is None:
        profile.ta_owner_id = user.id
        profile.ta_owner_name = user.full_name or user.username
    if getattr(profile, "applied_on", None) is None:
        profile.applied_on = datetime.now(timezone.utc)
    if getattr(profile, "source", None) is None:
        profile.source = "app"
    # RMG screening gate (25 Aug 2026): a new applicant starts Pending and the
    # AI-L1 actions stay locked until RMG shortlists. An RMG (or Admin) doing
    # the apply themselves IS the screening — auto-shortlist, no notification.
    from services.candidate_profiles import (
        RMG_SCREENING_PENDING, RMG_SCREENING_SHORTLISTED, notify_rmg_new_applicant,
        rmg_gate_enabled,
    )
    if not rmg_gate_enabled():
        pass  # gate switched off in Settings: no stamping, no notification
    elif user.has_any("RMG") or user.is_admin:
        profile.rmg_screening_status = RMG_SCREENING_SHORTLISTED
        profile.rmg_screening_by = user.id
        profile.rmg_screening_at = datetime.now(timezone.utc)
    else:
        profile.rmg_screening_status = RMG_SCREENING_PENDING
    db.add(profile)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "CREATED",
                 f"Candidate profile created (status Sourcing) by {user.full_name or user.username}")
    if notes:
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "NOTE", notes)
    if profile.rmg_screening_status == RMG_SCREENING_PENDING:
        notify_rmg_new_applicant(db, profile, actor=user)
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message="Candidate profile created")


@router.get("/{profile_id}")
def get_profile(profile_id: int,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    # Pre-fill commercials from Candidate (TA) + opportunity CTC slab (Sales) when
    # still empty, so RMG sees them without re-keying. Only touches NULL fields.
    changed = backfill_profile_commercials(db, profile)
    # Self-heal: profiles whose AI L1 passed BEFORE the RMG hand-off flow existed
    # are still parked in Technical_Screening — advance them to RMG_Review on
    # open so the RMG decision actions appear.
    if _status_val(profile.pipeline_status) == PipelineStatus.TECHNICAL_SCREENING.value:
        from models import AiInterviewLink
        passed = db.execute(
            select(AiInterviewLink.id).where(
                AiInterviewLink.profile_id == profile.id,
                AiInterviewLink.result == "Passed",
            ).limit(1)
        ).first()
        if passed:
            profile.pipeline_status = PipelineStatus.RMG_REVIEW
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                         "STATUS_CHANGE",
                         "Technical_Screening -> RMG_Review: AI L1 already passed — "
                         "auto-forwarded for RMG review")
            changed = True
    if changed:
        db.commit()
        db.refresh(profile)
    return envelope(data=profile_detail(db, profile, user))


#: The internal, human interview ladder RMG runs with the candidate.
#:
#: L1 here is the MANUAL alternative to the AI L1 (1 Sep 2026, user decision):
#: when RMG decides a machine screen is not the right read on someone, the same
#: rounds run with a person on the call — manual L1, then L2 — before the
#: candidate is submitted to Sales and meets the customer. One spec table so a
#: round can never be half-added (scheduled under one name, logged under
#: another, emailed as a third).
MANUAL_ROUNDS: dict[str, dict[str, str]] = {
    "L1": {
        "kind": "L1_Interview",          # services/interview_rounds.ROUNDS
        "label": "L1",
        "owner_role": "RMG",             # who takes the call / who is told
        "stage": PipelineStatus.RMG_REVIEW.value,   # the round belongs to this stage
        "activity": "L1_FACE_TO_FACE",
        "request_activity": "L1_REQUESTED",
        "template_key": "candidate.l1_manual_invite",
        "requested_event": "candidate.l1_requested",
        "scheduled_event": "candidate.l1_scheduled",
        "invite_event": "candidate.l1_manual_invite",
    },
    "L2": {
        "kind": "L2_F2F",
        "label": "L2",
        "owner_role": "RMG",
        "stage": PipelineStatus.RMG_REVIEW.value,
        "activity": "L2_FACE_TO_FACE",
        "request_activity": "L2_REQUESTED",
        "template_key": "candidate.l2_invite",
        "requested_event": "candidate.l2_requested",
        "scheduled_event": "candidate.l2_scheduled",
        "invite_event": "candidate.l2_invite",
    },
    # HR's own round (2 Sep 2026): after Sales Head approves, TA schedules the
    # HR interview and HR records the verdict, then moves to Pre Onboarding.
    "HR": {
        "kind": "HR_Interview",
        "label": "HR",
        "owner_role": "HR",
        "stage": PipelineStatus.HR_SCREENING.value,
        "activity": "HR_FACE_TO_FACE",
        "request_activity": "HR_REQUESTED",
        "template_key": "candidate.hr_invite",
        "requested_event": "candidate.hr_requested",
        "scheduled_event": "candidate.hr_scheduled",
        "invite_event": "candidate.hr_invite",
    },
}


def manual_round_spec(value: str | None) -> dict[str, str]:
    """Round spec for "L1" / "L2" / "HR". Defaults to L2 — the only round that
    existed before the manual path, so old clients keep working unchanged."""
    spec = MANUAL_ROUNDS.get((value or "L2").strip().upper())
    if spec is None:
        raise HTTPException(status_code=400, detail="round must be 'L1', 'L2' or 'HR'")
    return spec


class L2FaceToFaceIn(BaseModel):
    """Schedules an internal face-to-face round (e.g. a Teams call)."""
    scheduled_at: str | None = Field(default=None, max_length=64)   # free-form date/time
    meeting_link: str | None = Field(default=None, max_length=1024)  # Teams/Meet URL
    note: str | None = Field(default=None, max_length=1000)
    #: Who from RMG takes the call. Blank = derive it (28 Aug 2026): the RMG
    #: scheduling, or — when TA schedules on their behalf — the RMG who asked.
    #: The round card and the candidate's invite both name them.
    interviewer: str | None = Field(default=None, max_length=200)
    #: "L1" (the manual replacement for the AI round) or "L2". Default keeps
    #: every pre-1-Sep-2026 caller on the L2 behaviour they were written for.
    round: str | None = Field(default="L2", max_length=4)


class RmgScreeningIn(BaseModel):
    """RMG's screening decision on a fresh applicant (25 Aug 2026)."""
    decision: str = Field(pattern="^(Shortlisted|Rejected)$")
    note: str | None = Field(default=None, max_length=1000)


class SkipAiL1In(BaseModel):
    note: str | None = None
    #: Ask TA to arrange a MANUAL L1 straight away (1 Sep 2026, user flow).
    #: False = skip the AI round and review the CV alone before deciding.
    request_manual_l1: bool = True


@router.post("/{profile_id}/skip-ai-l1")
def skip_ai_l1(
    profile_id: int,
    payload: SkipAiL1In,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(rmg_roles),
):
    """RMG chooses the MANUAL interview route instead of the AI L1.

    The AI round is optional (user decision, 28 Aug 2026): some candidates are
    known quantities, some roles are better judged face to face, and waiting
    for a machine interview that nobody wants only stalls the pipeline. This
    moves the profile to RMG Review — the same stage a passed AI L1 reaches.

    With `request_manual_l1` (the default, and what the "Go manual" button
    sends) the TA owner is asked in the same breath to arrange a human L1, so
    the candidate never lands in RMG Review with nobody knowing whose move it
    is. The rest of the ladder is unchanged: L1 → L2 → submit to Sales →
    customer rounds.
    """
    from services.candidate_profiles import hand_off_to_rmg_review

    profile = get_profile_or_404(db, profile_id)
    note = (payload.note or "").strip()
    reason = "AI L1 skipped by RMG — manual interview route" + (f" — {note}" if note else "")
    moved = hand_off_to_rmg_review(db, profile, user, reason)
    already_here = moved is None
    candidate = db.get(Candidate, profile.candidate_id)
    cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
             if candidate else f"Candidate #{profile.candidate_id}")

    # Asking for the manual L1 is the same request TA already knows how to
    # answer for an L2 — logged under the round's own activity type so the
    # scheduler can name the RMG who asked, and so "L1 requested" is checkable.
    if payload.request_manual_l1:
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     MANUAL_ROUNDS["L1"]["request_activity"],
                     "RMG requested a MANUAL L1 round instead of the AI interview"
                     + (f" — note: {note}" if note else ""))

    try:
        from services.notify import notify_role, notify_user
        if payload.request_manual_l1:
            title = f"Schedule manual L1: {cname}"
            message = (f"RMG is skipping the AI interview for {cname} and wants a human L1 "
                       "round. Please agree a time with the candidate and enter the schedule "
                       "(date/time + meeting link)."
                       + (f" Note: {note}" if note else ""))
            event = MANUAL_ROUNDS["L1"]["requested_event"]
        else:
            title = f"AI L1 skipped: {cname}"
            message = (f"RMG will review {cname} directly"
                       + (f" — {note}" if note else "")
                       + ". No AI interview is needed for this candidate.")
            event = "profile.ai_l1_skipped"
        link = applied_candidates_link(db, profile)
        if profile.ta_owner_id:
            notify_user(db, profile.ta_owner_id, title, message, link,
                        event=event, actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "TA", title, message, link, exclude_user_id=user.id,
                        event=event, actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass  # notification must never cost the decision
    db.commit()
    db.refresh(profile)
    if already_here and not payload.request_manual_l1:
        return envelope(data=profile_to_dict(profile),
                        message="This candidate is already with RMG for review")
    return envelope(
        data=profile_to_dict(profile),
        message=("Manual route chosen — TA notified to schedule the L1 interview"
                 if payload.request_manual_l1
                 else "AI L1 skipped — the candidate is now with RMG for review"),
    )


@router.post("/{profile_id}/rmg-screening")
def rmg_screening_decision(
    profile_id: int,
    payload: RmgScreeningIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(rmg_roles),
):
    """RMG clears (or rejects) a TA-applied candidate for the AI L1 interview.

    Shortlisted → the TA owner is notified (bell + email) to proceed: contact
    the candidate, agree an AI L1 slot, send the invitation link. Rejected →
    a note of at least 5 characters is required and the TA is told why.
    Separate from the pipeline: this is the screening gate, not a stage move.
    """
    from services.candidate_profiles import (
        RMG_SCREENING_REJECTED, RMG_SCREENING_SHORTLISTED,
    )

    profile = get_profile_or_404(db, profile_id)
    note = (payload.note or "").strip()
    if payload.decision == RMG_SCREENING_REJECTED and len(note) < 5:
        raise HTTPException(status_code=400,
                            detail="A rejection note of at least 5 characters is required")
    profile.rmg_screening_status = payload.decision
    profile.rmg_screening_note = note or None
    profile.rmg_screening_by = user.id
    profile.rmg_screening_at = datetime.now(timezone.utc)

    candidate = db.get(Candidate, profile.candidate_id)
    cname = " ".join(p for p in (getattr(candidate, "first_name", None),
                                 getattr(candidate, "last_name", None)) if p) or f"#{profile.candidate_id}"
    opp = db.get(Opportunity, profile.opportunity_id)
    opp_label = f"{getattr(opp, 'opp_id', '')} — {getattr(opp, 'title', '')}".strip(" —")
    shortlisted = payload.decision == RMG_SCREENING_SHORTLISTED
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "RMG_SCREENING",
                 (f"RMG screening: {payload.decision} by {user.full_name or user.username}"
                  + (f" — {note}" if note else "")))

    # Tell the TA who applied this candidate (fall back to the whole TA role
    # when the profile has no owner, e.g. an imported row).
    from services.notify import notify_role, notify_user
    title = (f"RMG shortlisted: {cname}" if shortlisted else f"RMG rejected: {cname}")
    message = (
        f"{cname} on {opp_label} — cleared for the AI L1 interview. Contact the "
        f"candidate, agree a slot, and send the AI L1 invitation link."
        if shortlisted else
        f"{cname} on {opp_label} — rejected at RMG screening"
        + (f": {note}" if note else "") + "."
    )
    link = applied_candidates_link(db, profile)
    try:
        if profile.ta_owner_id:
            notify_user(db, profile.ta_owner_id, title, message, link,
                        event="profile.rmg_screening_decided", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "TA", title, message, link,
                        event="profile.rmg_screening_decided", actor=user,
                        exclude_user_id=user.id,
                        related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile),
                    message=("Candidate shortlisted — TA notified to proceed with the AI L1"
                             if shortlisted else "Candidate rejected at RMG screening — TA notified"))


class L2RequestIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)
    #: "L1" (the manual round that replaces the AI screen) or "L2".
    round: str | None = Field(default="L2", max_length=4)


@router.post("/{profile_id}/l2-request")
def request_l2_face_to_face(
    profile_id: int,
    payload: L2RequestIn,
    db: Session = Depends(get_crm_db),
    # RMG asks for L1/L2; HR asks for the HR round (3 Sep 2026). The owner
    # check below keeps each role on the round that is theirs.
    user: CurrentUser = Depends(gated_write("profiles", "RMG", "HR")),
):
    """The round's OWNER asks TA to arrange a human interview round (27 Aug 2026).

    The flow the team actually runs: RMG (or HR, for the HR round) decides a
    round is wanted and TELLS TA — TA coordinates the time with the candidate
    and enters the schedule; the owner is informed once it is set. `round` is
    "L1" for the manual round that stands in for the AI screen, "L2" for the
    round that follows it, or "HR" for HR's own round after Sales Head's
    approval (HR reviews the candidate's details first, then requests)."""
    spec = manual_round_spec(payload.round)
    rl = spec["label"]
    profile = get_profile_or_404(db, profile_id)
    if not getattr(user, "is_admin", False) and spec["owner_role"] not in (user.roles or set()):
        raise HTTPException(status_code=403,
                            detail=f"Only {spec['owner_role']} can request the {rl} round.")
    if _status_val(profile.pipeline_status) != spec["stage"]:
        raise HTTPException(
            status_code=400,
            detail=f"An {rl} round can be requested only while the profile is in "
                   f"{spec['stage'].replace('_', ' ')}")
    owner = spec["owner_role"]
    note = (payload.note or "").strip()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 spec["request_activity"],
                 f"{owner} requested a face-to-face {rl} round"
                 + (f" — note: {note}" if note else ""))
    candidate = db.get(Candidate, profile.candidate_id)
    cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
             if candidate else f"Candidate #{profile.candidate_id}")
    from services.notify import notify_role, notify_user
    title = f"Schedule {rl} round: {cname}"
    message = (f"{owner} wants a face-to-face {rl} with {cname}. Please agree a time with the "
               "candidate and enter the schedule (date/time + meeting link)."
               + (f" Note: {note}" if note else ""))
    link = applied_candidates_link(db, profile)
    try:
        if profile.ta_owner_id:
            notify_user(db, profile.ta_owner_id, title, message, link,
                        event=spec["requested_event"], actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "TA", title, message, link, exclude_user_id=user.id,
                        event=spec["requested_event"], actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass
    db.commit()
    return envelope({"requested": True, "round": rl},
                    message=f"TA notified — they will schedule the {rl} with the candidate")


def _derive_l2_interviewer(db: Session, profile, user: CurrentUser,
                           request_activity: str = "L2_REQUESTED",
                           owner_role: str = "RMG") -> str:
    """The panel member taking the call, when the form did not name one.

    The owning role (RMG for L1/L2, HR for the HR round) scheduling it →
    themselves. TA scheduling per a request → whoever asked (latest request
    row for this round), looked up in the legacy users table.
    """
    roles = user.roles or set()
    if owner_role in roles:
        return (getattr(user, "full_name", "") or getattr(user, "username", "") or "").strip()[:200]
    try:
        req_row = db.execute(
            select(CandidateProfileActivityLog)
            .where(CandidateProfileActivityLog.profile_id == profile.id,
                   CandidateProfileActivityLog.action_type == request_activity)
            .order_by(CandidateProfileActivityLog.id.desc())
        ).scalars().first()
        if req_row is not None and req_row.user_id:
            from services.requirements import usernames_for
            who = usernames_for(db, [req_row.user_id]).get(req_row.user_id) or {}
            return (who.get("full_name") or who.get("username") or "").strip()[:200]
    except Exception:
        pass
    return ""


@router.post("/{profile_id}/l2-face-to-face")
def schedule_l2_face_to_face(
    profile_id: int,
    payload: L2FaceToFaceIn,
    db: Session = Depends(get_crm_db),
    # HR joins the gate for the HR round (2 Sep 2026); the spec's stage check
    # keeps each role on the round that is actually theirs.
    user: CurrentUser = Depends(gated_write("profiles", "TA", "RMG", "HR")),
):
    """Record an internal face-to-face round (candidate + RMG on a Teams call).

    `round` picks L1 (the manual replacement for the AI screen) or L2. The
    profile stays in RMG_Review while the rounds happen; RMG decides after the
    ladder (submit to Sales / reject). Logs the round, notifies TA to
    coordinate, and best-effort emails the candidate the invite."""
    spec = manual_round_spec(payload.round)
    rl = spec["label"]
    owner = spec["owner_role"]
    profile = get_profile_or_404(db, profile_id)
    # The HR round may also be (re)booked once the profile is already at HR
    # Interviewing (3 Sep 2026) — a reschedule must not be refused.
    allowed_stages = {spec["stage"]}
    if spec["kind"] == "HR_Interview":
        allowed_stages.add(PipelineStatus.HR_INTERVIEWING.value)
    if _status_val(profile.pipeline_status) not in allowed_stages:
        raise HTTPException(
            status_code=400,
            detail=f"The {rl} round can be scheduled only while the profile is in "
                   f"{spec['stage'].replace('_', ' ')}")
    when = (payload.scheduled_at or "").strip()
    link = (payload.meeting_link or "").strip()
    note = (payload.note or "").strip()
    # WHO the candidate is meeting. Named explicitly, else derived: the RMG
    # scheduling it, or the RMG who requested it when TA is arranging.
    interviewer = " ".join((payload.interviewer or "").split())[:200]
    if not interviewer:
        interviewer = _derive_l2_interviewer(db, profile, user, spec["request_activity"], owner)

    # Structured event → powers the interview calendar + the .ics invite.
    from datetime import datetime as _dt
    from models import InterviewEvent
    event_dt = None
    if when:
        try:
            # datetime-local "YYYY-MM-DDTHH:MM" is IST — store it as such, not
            # as UTC (2 Sep 2026: rounds showed 5h30 late and missed the calendar).
            from services.interview_rounds import read_as_ist
            event_dt = read_as_ist(_dt.fromisoformat(when))
        except ValueError:
            event_dt = None
    db.add(InterviewEvent(
        profile_id=profile.id,
        candidate_id=profile.candidate_id,
        kind=spec["kind"],
        scheduled_at=event_dt,
        raw_when=when or None,
        meeting_link=link or None,
        note=note or None,
        interviewer=interviewer or None,
        interview_category="Internal",
        user_role=owner,
        status="Scheduled",
        created_by=user.id,
    ))

    scheduled_by = "TA" if ("TA" in (user.roles or set()) and owner not in (user.roles or set())) else owner
    parts = [f"{rl} face-to-face round scheduled by {scheduled_by}"]
    if interviewer:
        parts.append(f"interviewer: {interviewer}")
    if when:
        parts.append(f"when: {when}")
    if link:
        parts.append(f"meeting: {link}")
    if note:
        parts.append(f"note: {note}")
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 spec["activity"], " — ".join(parts))

    candidate = db.get(Candidate, profile.candidate_id)
    cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
             if candidate else f"Candidate #{profile.candidate_id}")
    from services.notify import notify_role, notify_user
    _sched_title = f"{rl} face-to-face scheduled: {cname}"
    _sched_body = " — ".join(parts[1:]) or f"Face-to-face {rl} round scheduled."
    _sched_link = applied_candidates_link(db, profile)
    if scheduled_by == "TA":
        # TA scheduled per the owner's request → inform whoever asked (latest
        # request entry for this round), falling back to the owning role.
        req_row = db.execute(
            select(CandidateProfileActivityLog)
            .where(CandidateProfileActivityLog.profile_id == profile.id,
                   CandidateProfileActivityLog.action_type == spec["request_activity"])
            .order_by(CandidateProfileActivityLog.id.desc())
        ).scalars().first()
        if req_row is not None:
            notify_user(db, req_row.user_id, _sched_title, _sched_body, _sched_link,
                        event=spec["scheduled_event"], actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, owner, _sched_title, _sched_body, _sched_link,
                        exclude_user_id=user.id, event=spec["scheduled_event"], actor=user)
    else:
        # The owner scheduled directly (still allowed) → TA coordinates, as before.
        notify_role(db, "TA", _sched_title, _sched_body, _sched_link,
                    exclude_user_id=user.id, event=spec["scheduled_event"], actor=user)

    # Best-effort email to the candidate with the call details + calendar invite.
    email_sent = False
    if candidate is not None and (candidate.email or "").strip() and "@noemail" not in candidate.email:
        try:
            from services.candidate_comms import (
                build_ics_invite, candidate_template_override, internal_round_invite_message,
                send_candidate_email,
            )
            team = "HR" if owner == "HR" else "engineering"
            # Built-in wording lives in candidate_comms so Settings → Email
            # Drafts can show exactly what goes out (3 Sep 2026).
            l2_subject, body = internal_round_invite_message(
                cname, rl, team, interviewer, when, link, note)
            # Admin-authored draft (Settings → Email Drafts) wins when set.
            l2_subject, body, _ = candidate_template_override(
                db, spec["template_key"], l2_subject, body,
                {"candidate": cname, "round": rl, "team": team,
                 "interviewer": interviewer or "", "when": when or "",
                 "link": link or "", "note": note or ""})
            attachments = None
            if event_dt is not None:
                ics = build_ics_invite(
                    summary=f"{rl} Interview — {cname}",
                    starts_at=event_dt,
                    description=(note or f"{rl} interview round with the "
                                 f"{'HR' if owner == 'HR' else 'engineering'} team."),
                    location=link or "",
                    uid=f"karnex-{rl.lower()}-{profile.id}",
                )
                attachments = [("interview-invite.ics", ics.encode("utf-8"), "text/calendar")]
            res = send_candidate_email(candidate.email, l2_subject, body, actor=user,
                                       attachments=attachments)
            email_sent = bool(res.get("sent"))
            # Inline send (the .ics can't ride the text-only outbox) — record
            # it so the candidate's Emails-tab thread includes the L2 round.
            from services.email_outbox import record_inline_email
            record_inline_email(
                db, to_email=candidate.email, to_name=cname,
                subject=l2_subject, body_text=body,
                event=spec["invite_event"], actor=user,
                related_type="candidate", related_id=profile.candidate_id,
                sent=email_sent, error=None if email_sent else str(res.get("error") or "send_failed"),
            )
        except Exception:
            email_sent = False

    # Outlook calendar invites for the INTERVIEW SIDE (27 Aug 2026): the TA who
    # scheduled, the RMG who requested (or will conduct) and the TA owner each
    # get the .ics by email, so the round lands in their own calendars — not
    # only the in-app Interview Calendar (which reads the InterviewEvent row).
    if event_dt is not None:
        try:
            from services.candidate_comms import build_ics_invite, send_candidate_email
            recipient_ids: set[int] = {user.id}
            req_row = db.execute(
                select(CandidateProfileActivityLog)
                .where(CandidateProfileActivityLog.profile_id == profile.id,
                       CandidateProfileActivityLog.action_type == spec["request_activity"])
                .order_by(CandidateProfileActivityLog.id.desc())
            ).scalars().first()
            if req_row is not None:
                recipient_ids.add(req_row.user_id)
            if profile.ta_owner_id:
                recipient_ids.add(profile.ta_owner_id)
            ics = build_ics_invite(
                f"{rl} face-to-face — {cname}", event_dt,
                description=(note or f"Face-to-face {rl} round with {cname}."),
                location=link or "", uid=f"karnex-{rl.lower()}-{profile.id}")
            staff_body = (
                f"{rl} face-to-face round scheduled with {cname}.\n"
                + (f"When: {when}\n" if when else "")
                + (f"Meeting link: {link}\n" if link else "")
                + (f"\n{note}\n" if note else "")
                + "\nThe calendar invite is attached.\n")
            rows = db.execute(
                sa.text("SELECT id, email FROM registration_data WHERE id IN :ids")
                .bindparams(sa.bindparam("ids", expanding=True)),
                {"ids": list(recipient_ids)},
            ).all()
            for _uid, em in rows:
                if (em or "").strip():
                    send_candidate_email(em, f"{rl} scheduled: {cname}", staff_body, actor=user,
                                         attachments=[("invite.ics", ics.encode(), "text/calendar")])
        except Exception:
            pass  # calendar mail is a convenience; the round is already recorded

    moved = None
    if spec["kind"] == "HR_Interview":
        # Booking the HR round IS the move to HR Interviewing (3 Sep 2026).
        from services.candidate_profiles import advance_on_hr_round_scheduled
        moved = advance_on_hr_round_scheduled(db, profile, user, when)

    db.commit()
    return envelope(
        data={"profile_id": profile.id, "round": rl, "email_sent": email_sent,
              "pipeline_status": _status_val(profile.pipeline_status)},
        message=f"{rl} face-to-face recorded"
                + (" — candidate emailed" if email_sent else "")
                + (f" — status moved to {moved.replace('_', ' ')}" if moved else ""),
    )


#: What HR may change on a profile, and WHEN (2 Sep 2026, user request).
#:
#: HR owns the onboarding paperwork — the confirmed CTCs, the references, the
#: two onboarding dates, the verified experience and the relocation answer —
#: and confirms it with the candidate at Pre Onboarding. Outside that stage
#: the profile is another team's (TA/Sales/RMG), so HR stays read-only; and
#: even inside it HR never touches the approval figure, which is Sales Head's.
HR_EDIT_STATUSES = frozenset({PipelineStatus.HR_SCREENING.value, PipelineStatus.HR_INTERVIEWING.value,
                              PipelineStatus.PREBOARDING.value})
HR_EDITABLE_FIELDS = frozenset({
    "current_ctc", "expected_ctc", "offer_letter_reference", "employee_ref",
    "customer_onboarding_date", "karnex_onboarding_date",
    "total_experience_years", "relocation_applicable", "official_email",
    "department_id", "designation_id",
})
_PROFILE_OWNER_ROLES = {"TA", "Sales", "RMG"}


def enforce_hr_edit_window(profile, user: CurrentUser, updates: dict) -> None:
    """403 when an HR-only user edits outside their window or their fields.

    Only HR-ONLY users are constrained: someone who is also TA/Sales/RMG (or
    Admin/CEO, who never reach here) keeps that role's full rights.
    """
    roles = set(user.roles or ())
    if getattr(user, "is_admin", False) or "HR" not in roles or roles & _PROFILE_OWNER_ROLES:
        return
    stage = _status_val(profile.pipeline_status)
    if stage not in HR_EDIT_STATUSES:
        raise HTTPException(
            status_code=403,
            detail="HR can edit these details once the candidate reaches HR Screening / Pre Onboarding "
                   f"(currently {stage.replace('_', ' ')}).")
    outside = sorted(set(updates) - HR_EDITABLE_FIELDS)
    if outside:
        raise HTTPException(
            status_code=403,
            detail=f"HR cannot change: {', '.join(outside)}. "
                   "The approval amount is Sales Head's decision.")


@router.put("/{profile_id}")
def update_profile(profile_id: int, payload: ProfileUpdate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(gated_write("profiles", "TA", "Sales", "RMG", "HR"))):
    profile = get_profile_or_404(db, profile_id)
    updates = payload.model_dump(exclude_unset=True)
    enforce_hr_edit_window(profile, user, updates)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), "profiles", updates, {
        "current_ctc": "current_ctc",
        "expected_ctc": "expected_ctc",
        "ctc_approval_amount": "approved_ctc",
        "commercial_approved": "approved_ctc",
        # Everything in the Workflow block rides ONE field grant, `workflow`
        # (3 Sep 2026) — Admin/CEO decide per template which logins see the
        # block, and a template that hides it must hide every field in it.
        "offer_letter_reference": "workflow",
        "employee_ref": "workflow",
        "customer_onboarding_date": "workflow",
        "karnex_onboarding_date": "workflow",
        "total_experience_years": "workflow",
        "relocation_applicable": "workflow",
        "official_email": "workflow",
        "department_id": "workflow",
        "designation_id": "workflow",
    })
    _reject_impossible_ctc(updates)
    for field, value in updates.items():
        setattr(profile, field, value)
    if "current_ctc" in updates or "expected_ctc" in updates:
        profile.hike_percent = compute_hike_percent(profile.current_ctc, profile.expected_ctc)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "UPDATED", f"Profile fields updated: {', '.join(sorted(updates)) or 'none'}")
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message="Candidate profile updated")


@router.delete("/{profile_id}")
def delete_profile(profile_id: int,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(create_roles)):
    from models import AiInterviewLink, Employee, InterviewEvent
    from services.crm_common import commit_or_conflict

    profile = get_profile_or_404(db, profile_id)
    # Cascade AI interview links owned by this profile.
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id == profile.id)
    ).scalars().all():
        db.delete(link)
    # Interview rounds (L2 face-to-face, customer interviews) have a NOT NULL
    # profile_id with no DB cascade — delete them or the profile delete fails.
    for ev in db.execute(
        select(InterviewEvent).where(InterviewEvent.profile_id == profile.id)
    ).scalars().all():
        db.delete(ev)
    db.flush()
    for emp in db.execute(
        select(Employee).where(Employee.candidate_profile_id == profile.id)
    ).scalars().all():
        emp.candidate_profile_id = None
    db.delete(profile)
    commit_or_conflict(db, "Cannot delete: candidate profile is still referenced by other records.")
    return envelope(message="Candidate profile deleted")


# ---------------------------------------------------------------------------
# Pipeline status transition
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/status-transition")
def status_transition(profile_id: int, payload: ProfileStatusTransitionIn,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)

    # Moving to Customer Approved requires an offer, so the offer can be
    # supplied with the move. Doing it here rather than making the user visit
    # the Offers tab first keeps both writes in ONE transaction: a rejected
    # transition rolls the offer back, instead of leaving an orphaned offer
    # attached to a profile that never advanced.
    created_offer = None
    if payload.offer is not None:
        if payload.new_status != PipelineStatus.CUSTOMER_APPROVAL.value:
            raise HTTPException(
                status_code=400,
                detail="An offer can only be attached when moving to Customer Approved.",
            )
        _require_offer_authority(user)
        created_offer = OfferHistory(profile_id=profile.id, status=OfferStatus.PENDING,
                                     **payload.offer.model_dump())
        db.add(created_offer)
        db.flush()  # so the entry-requirement check below sees it
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "OFFER_CREATED",
                     f"Offer #{created_offer.id} created with the status change "
                     f"(CTC {payload.offer.ctc}, offered {payload.offer.offer_date.isoformat()})")

    old_status = perform_transition(db, profile, payload.new_status, payload.comment, user)
    notice_asked = False
    if payload.ask_notice_period and payload.new_status == PipelineStatus.SALES_SCREENING.value:
        notice_asked = _ask_ta_for_notice_period(db, profile, user)
    scheduled_round = None
    if payload.schedule is not None and payload.new_status in _CUSTOMER_STAGE_ROUND_KIND:
        scheduled_round = _schedule_customer_round_with_move(
            db, profile, payload.new_status, payload.schedule, (payload.comment or "").strip(), user)
    db.commit()
    db.refresh(profile)
    message = f"Status changed: {old_status} -> {payload.new_status}"
    if created_offer is not None:
        message = "Offer recorded and sent to Sales Head for approval"
    elif notice_asked:
        message = "Submitted to Sales — TA asked to collect the notice period"
    elif scheduled_round is not None:
        message = (f"{round_label(scheduled_round.kind)} scheduled — the candidate is invited "
                   "and TA notified")
    return envelope(data=profile_to_dict(profile), message=message)


#: Moving INTO these stages can carry the customer's slot (2 Sep 2026): the
#: stage says which of the customer's rounds is being lined up.
_CUSTOMER_STAGE_ROUND_KIND: dict[str, str] = {
    PipelineStatus.CUSTOMER_INTERVIEW.value: "Customer_Interview",
    PipelineStatus.L1_FEEDBACK.value: "Customer_Interview",
    PipelineStatus.L2_FEEDBACK.value: "Customer_L2",
}


def _fmt_slot(raw: str) -> str:
    """'2026-09-10T14:30' → '10 Sep 2026, 02:30 PM' for notes and mails."""
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(raw).strftime("%d %b %Y, %I:%M %p")
    except (ValueError, TypeError):
        return raw


def _schedule_customer_round_with_move(db: Session, profile, new_status: str, sched,
                                       comment: str, user: CurrentUser):
    """Record the customer's slot on the round this stage belongs to.

    Reuses the round row the transition recorder just touched (one card per
    customer round — the duplicate-card rule), fills in the slot, and treats
    the move's note as the round's NOTE rather than its feedback: nothing has
    been judged yet. Then the candidate is invited and TA + Sales are told.
    """
    from datetime import datetime as _dt
    from models import InterviewEvent
    from services.interview_rounds import read_as_ist

    kind = _CUSTOMER_STAGE_ROUND_KIND[new_status]
    slots = sched.all_slots()
    if not slots:
        return None
    primary, alternates = slots[0], slots[1:]
    event = db.execute(
        select(InterviewEvent).where(InterviewEvent.profile_id == profile.id,
                                     InterviewEvent.kind == kind)
        .order_by(InterviewEvent.id.desc())
    ).scalars().first()
    if event is None:
        event = InterviewEvent(profile_id=profile.id, candidate_id=profile.candidate_id,
                               kind=kind, created_by=user.id)
        db.add(event)
    try:
        event.scheduled_at = read_as_ist(_dt.fromisoformat(primary.scheduled_at))
    except ValueError:
        event.scheduled_at = None
    event.raw_when = primary.scheduled_at
    if sched.interviewer:
        event.interviewer = sched.interviewer.strip()[:200]
    if primary.meeting_link and primary.meeting_link.strip():
        event.meeting_link = primary.meeting_link.strip()
    elif sched.meeting_link:
        event.meeting_link = sched.meeting_link.strip()
    if sched.duration_minutes:
        event.duration_minutes = sched.duration_minutes
    event.interview_category = event.interview_category or "External"
    event.user_role = event.user_role or "Customer"
    if not event.result:
        event.status = "Scheduled"
        # The transition recorder wrote the move's note as feedback; a slot is
        # not a verdict, so keep it as the note and leave feedback for Sales.
        if comment and (event.feedback or "").strip() == comment:
            event.feedback = None
        note_lines = [comment] if comment else []
        if alternates:
            # Multiple customer slots (7 Sep 2026): the first is booked on the
            # round; the others travel in the note so the candidate's invite
            # and TA's row both show every option the customer offered.
            note_lines.append("Alternative slots offered by the customer:")
            for i, alt in enumerate(alternates, start=2):
                line = f"  {i}. {_fmt_slot(alt.scheduled_at)}"
                if alt.meeting_link and alt.meeting_link.strip():
                    line += f" — {alt.meeting_link.strip()}"
                note_lines.append(line)
        event.note = "\n".join(note_lines) or event.note
    db.flush()
    _email_candidate_round_invite(db, profile, event, user)
    # TA sees the slot on their row and calendar; tell them (and Sales, when
    # someone else booked it) so nobody has to go looking.
    try:
        from services.notify import notify_role, notify_user
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        title = f"{round_label(kind)} scheduled: {cname}"
        body = (f"{round_label(kind)} with {cname} on {_fmt_slot(primary.scheduled_at)}"
                + (f" (+{len(alternates)} alternative slot{'s' if len(alternates) > 1 else ''})" if alternates else "")
                + (f" — panel {event.interviewer}" if event.interviewer else "")
                + ". The candidate has been invited; the round is on the Interviews tab.")
        link = applied_candidates_link(db, profile)
        if profile.ta_owner_id and profile.ta_owner_id != user.id:
            notify_user(db, profile.ta_owner_id, title, body, link,
                        event="candidate.round_scheduled", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "TA", title, body, link, exclude_user_id=user.id,
                        event="candidate.round_scheduled", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        if "Sales" not in (user.roles or set()):
            notify_role(db, "Sales", title, body, link, exclude_user_id=user.id,
                        event="candidate.round_scheduled", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass
    return event


def _ask_ta_for_notice_period(db: Session, profile, user: CurrentUser) -> bool:
    """Tell the TA owner (else the TA role) to collect the candidate's notice
    period, now that RMG has cleared them for Sales (2 Sep 2026). Logged on
    the profile so the ask is visible next to the hand-off it rode on.
    Best-effort: the hand-off must never fail because a notification did."""
    try:
        from services.notify import notify_role, notify_user
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        known = (getattr(candidate, "notice_period", None) or "").strip() if candidate else ""
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     "NOTICE_PERIOD_REQUESTED",
                     "RMG asked TA to collect the candidate's notice period"
                     + (f" (currently on record: {known})" if known else ""))
        title = f"Collect notice period: {cname}"
        body = (f"RMG has submitted {cname} to Sales. Please confirm the candidate's notice "
                "period with them and update it on the candidate record"
                + (f" (currently recorded as '{known}')." if known else " — nothing is on record yet."))
        link = applied_candidates_link(db, profile)
        if profile.ta_owner_id:
            notify_user(db, profile.ta_owner_id, title, body, link,
                        event="candidate.notice_period_requested", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "TA", title, body, link, exclude_user_id=user.id,
                        event="candidate.notice_period_requested", actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        return True
    except Exception:
        return False


def _require_offer_authority(user: CurrentUser) -> None:
    """Attaching an offer needs the same role as creating one on the Offers tab."""
    if getattr(user, "is_admin", False):
        return
    if not (set(getattr(user, "roles", []) or []) & set(OFFER_WRITE_ROLES)):
        raise HTTPException(
            status_code=403,
            detail=f"Recording an offer requires one of: {', '.join(OFFER_WRITE_ROLES)}",
        )


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

@router.get("/{profile_id}/activity-log")
def activity_log(profile_id: int,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(any_crm_role)):
    get_profile_or_404(db, profile_id)
    rows = db.execute(
        sa.text(
            "SELECT l.id, l.user_id, r.username, l.action_type, l.comment, l.timestamp "
            "FROM candidate_profile_activity_log l "
            "LEFT JOIN registration_data r ON r.id = l.user_id "
            "WHERE l.profile_id = :pid "
            "ORDER BY l.timestamp ASC, l.id ASC"
        ),
        {"pid": profile_id},
    ).mappings().all()
    data = [
        {
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"],
            "action_type": row["action_type"],
            "comment": row["comment"],
            "timestamp": row["timestamp"].isoformat() if row["timestamp"] is not None else None,
        }
        for row in rows
    ]
    return envelope(data=data)


# ---------------------------------------------------------------------------
# Skill evaluations
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/skill-evaluation")
def skill_evaluation(profile_id: int, payload: list[SkillEvaluationItem],
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(evaluation_roles)):
    profile = get_profile_or_404(db, profile_id)
    if not payload:
        raise HTTPException(status_code=400, detail="At least one skill evaluation item is required")
    count = upsert_skill_evaluations(db, profile, payload, user)
    db.commit()
    return envelope(data=profile_detail(db, profile, user)["skill_evaluations"],
                    message=f"Skill evaluation saved for {count} skill(s)")


# ---------------------------------------------------------------------------
# The Sales → Sales Head approval gate (2 Sep 2026, user flow)
#
#   customer shortlists  →  Sales submits the RATE + CUSTOMER ONBOARDING DATE
#   →  "Pending Sales Head Approval"  →  Sales Head approves (→ Preboarding,
#   HR notified) / sends the terms back to Sales / rejects.
#
# Both halves existed as pieces (an offer attached to a generic status change;
# a banner for Sales Head) but no one screen offered the step by name, so the
# gate was skipped in practice. These two endpoints ARE the step, and every
# surface that lists a candidate calls them.
# ---------------------------------------------------------------------------

#: Annualisation for the rate units Sales may quote (2 Sep 2026). Hourly uses
#: the CRM's standard billable month — 8 h × 22 working days — over 12 months.
RATE_UNIT_TO_ANNUAL: dict[str, float] = {
    "Hourly": 8 * 22 * 12,   # 2,112 billable hours a year
    "Monthly": 12,
    "Yearly": 1,
}


def annualise_rate(value: float, unit: str) -> float:
    """The annual rupee CTC every downstream calculation reads, from the
    rate as Sales typed it."""
    factor = RATE_UNIT_TO_ANNUAL.get(unit)
    if factor is None:
        raise HTTPException(status_code=400,
                            detail="rate_unit must be Hourly, Monthly or Yearly")
    return round(float(value) * factor, 2)


class SubmitForApprovalIn(BaseModel):
    """Sales' proposal: what the candidate will be paid, and when the customer
    takes them. The offer row and the profile's onboarding date are written
    together, in the transaction that moves the stage.

    The rate is ONE field with a unit (Hourly / Monthly / Yearly, 2 Sep 2026);
    `ctc` — the annualised rupee figure — is derived from it. Older callers
    that send `ctc` alone are still accepted (treated as Yearly)."""
    rate_value: float | None = Field(default=None, gt=0)
    rate_unit: str | None = Field(default=None, pattern="^(Hourly|Monthly|Yearly)$")
    #: Annual CTC in RUPEES. Optional when rate_value/rate_unit are given.
    ctc: float | None = Field(default=None, gt=0)
    customer_onboarding_date: date
    offer_date: date | None = None
    expiry_date: date | None = None
    note: str | None = Field(default=None, max_length=1000)

    def annual_ctc(self) -> float:
        if self.rate_value is not None:
            return annualise_rate(self.rate_value, self.rate_unit or "Yearly")
        if self.ctc is not None:
            return float(self.ctc)
        raise HTTPException(status_code=400, detail="Enter the candidate's rate")


class SalesHeadDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|send_back|reject)$")
    comment: str = Field(min_length=5, max_length=1000)
    #: Sales Head may correct the terms while approving — the offer row and
    #: the onboarding date are updated before the move, so what HR receives
    #: is what was signed off, not what was proposed.
    ctc: float | None = Field(default=None, gt=0)
    #: Or the correction in the unit Sales quoted (2 Sep 2026): ₹1,200 hourly
    #: stays ₹1,200 hourly on Sales Head's screen, not "25.34 L".
    rate_value: float | None = Field(default=None, gt=0)
    rate_unit: str | None = Field(default=None, pattern="^(Hourly|Monthly|Yearly)$")
    customer_onboarding_date: date | None = None


def _latest_pending_offer(db: Session, profile) -> OfferHistory | None:
    return db.execute(
        select(OfferHistory)
        .where(OfferHistory.profile_id == profile.id,
               OfferHistory.status == OfferStatus.PENDING)
        .order_by(OfferHistory.id.desc())
    ).scalars().first()


def _approval_submitter_id(db: Session, profile) -> int | None:
    """The Sales user who submitted the terms — the person a decision goes back to."""
    row = db.execute(
        select(CandidateProfileActivityLog)
        .where(CandidateProfileActivityLog.profile_id == profile.id,
               CandidateProfileActivityLog.action_type == "SUBMITTED_FOR_APPROVAL")
        .order_by(CandidateProfileActivityLog.id.desc())
    ).scalars().first()
    return row.user_id if row is not None else None


@router.post("/{profile_id}/submit-for-approval")
def submit_for_approval(profile_id: int, payload: SubmitForApprovalIn,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(offer_roles)):
    """Sales proposes the candidate's rate + customer onboarding date and hands
    the profile to Sales Head. Allowed from Shortlisted (the customer's yes)."""
    profile = get_profile_or_404(db, profile_id)
    here = _status_val(profile.pipeline_status)
    if here != PipelineStatus.SHORTLISTED.value:
        raise HTTPException(
            status_code=400,
            detail=("Submit for approval once the customer has shortlisted the candidate "
                    f"(currently {here.replace('_', ' ')})."))

    offer = _latest_pending_offer(db, profile)
    if offer is None:
        offer = OfferHistory(profile_id=profile.id, status=OfferStatus.PENDING,
                             offer_date=payload.offer_date or date.today())
        db.add(offer)
    elif payload.offer_date is not None:
        offer.offer_date = payload.offer_date
    # A resubmission after "send back" reuses the pending row — one offer, its
    # terms corrected, not a trail of superseded rows the Offers tab must explain.
    annual = payload.annual_ctc()
    offer.ctc = annual
    offer.rate_value = payload.rate_value if payload.rate_value is not None else annual
    offer.rate_unit = payload.rate_unit or "Yearly"
    offer.joining_date = payload.customer_onboarding_date
    if payload.expiry_date is not None:
        offer.expiry_date = payload.expiry_date
    profile.customer_onboarding_date = payload.customer_onboarding_date
    db.flush()

    note = (payload.note or "").strip()
    rate_text = f"₹{float(offer.rate_value):,.0f} {offer.rate_unit.lower()}"
    if offer.rate_unit != "Yearly":
        rate_text += f" (≈ ₹{annual:,.0f} a year)"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "SUBMITTED_FOR_APPROVAL",
                 f"Submitted to Sales Head — rate {rate_text}, customer onboarding "
                 f"{payload.customer_onboarding_date.isoformat()}"
                 + (f" — {note}" if note else ""))
    comment = note or "Offer terms submitted for Sales Head approval"
    # perform_transition: Sales owns Shortlisted; the entry requirement (an
    # offer on record) is satisfied by the row flushed above; Sales Head is
    # notified by the stage-arrival route.
    perform_transition(db, profile, PipelineStatus.CUSTOMER_APPROVAL.value, comment, user)
    # Everyone who carried the candidate hears the terms went up (2 Sep 2026):
    # TA, RMG and HR are told, not asked — the decision is Sales Head's.
    try:
        from services.notify import notify_roles
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        notify_roles(db, ["TA", "RMG", "HR"],
                     f"Submitted for Sales Head approval: {cname}",
                     f"Sales submitted {cname}'s terms — rate {rate_text}, customer onboarding "
                     f"{payload.customer_onboarding_date.isoformat()}. Sales Head will approve "
                     "or send them back.",
                     f"/admin?view=crm&p=profiles/{profile.id}",
                     exclude_user_id=user.id, actor=user, event="candidate.offer_submitted",
                     related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile),
                    message="Sent to Sales Head for approval — they have been notified")


@router.post("/{profile_id}/sales-head-decision")
def sales_head_decision(profile_id: int, payload: SalesHeadDecisionIn,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(gated_write("profiles", "Sales_Head"))):
    """Sales Head approves the terms (→ Preboarding), sends them back to Sales
    (→ Shortlisted) or rejects the candidate. The submitting Sales user is told
    either way — a decision nobody hears about is not a decision."""
    profile = get_profile_or_404(db, profile_id)
    if _status_val(profile.pipeline_status) != PipelineStatus.CUSTOMER_APPROVAL.value:
        raise HTTPException(status_code=400,
                            detail="This candidate is not awaiting Sales Head approval")
    offer = _latest_pending_offer(db, profile)
    if payload.decision == "approve":
        if (payload.ctc is not None or payload.rate_value is not None
                or payload.customer_onboarding_date is not None):
            if offer is None:
                raise HTTPException(status_code=400, detail="No pending offer to correct")
            changes = []
            # A correction in the quoted unit wins over a bare annual figure.
            new_unit = payload.rate_unit or offer.rate_unit or "Yearly"
            new_value = payload.rate_value
            if new_value is None and payload.ctc is not None:
                new_unit, new_value = "Yearly", payload.ctc
            if new_value is not None:
                new_annual = annualise_rate(new_value, new_unit)
                if float(offer.ctc or 0) != new_annual or (offer.rate_unit or "Yearly") != new_unit:
                    old_text = (f"₹{float(offer.rate_value or offer.ctc or 0):,.0f} "
                                f"{(offer.rate_unit or 'Yearly').lower()}")
                    changes.append(f"rate {old_text} → ₹{new_value:,.0f} {new_unit.lower()}")
                    offer.ctc = new_annual
                    offer.rate_unit = new_unit
                    offer.rate_value = new_value
            if (payload.customer_onboarding_date is not None
                    and offer.joining_date != payload.customer_onboarding_date):
                changes.append(f"customer onboarding {offer.joining_date} → "
                               f"{payload.customer_onboarding_date}")
                offer.joining_date = payload.customer_onboarding_date
                profile.customer_onboarding_date = payload.customer_onboarding_date
            if changes:
                log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                             "OFFER_CORRECTED", "Sales Head corrected the terms: " + "; ".join(changes))
        # Approval hands the candidate to HR (HR Screening, 2 Sep 2026). Since
        # 3 Sep 2026 HR reviews the details first and asks TA for the round
        # themselves (POST /{id}/hr-round-request) — no automatic request here.
        target = PipelineStatus.HR_SCREENING.value
        title = "Offer approved by Sales Head"
    elif payload.decision == "send_back":
        target = PipelineStatus.SHORTLISTED.value
        title = "Offer sent back — redo the terms"
    else:
        target = PipelineStatus.CUSTOMER_REJECTED.value
        title = "Candidate rejected by Sales Head"

    perform_transition(db, profile, target, payload.comment, user)

    # Tell the Sales person who submitted (fall back to the whole Sales role).
    try:
        from services.notify import notify_role, notify_user
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        link = f"/admin?view=crm&p=profiles/{profile.id}"
        body = f"{cname}: {payload.comment}"
        event = f"candidate.offer_{payload.decision}"
        submitter = _approval_submitter_id(db, profile)
        if submitter and submitter != user.id:
            notify_user(db, submitter, f"{title}: {cname}", body, link, event=event,
                        actor=user, related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "Sales", f"{title}: {cname}", body, link, exclude_user_id=user.id,
                        event=event, actor=user,
                        related_type="candidate", related_id=profile.candidate_id)
        if payload.decision == "approve":
            # Approval is the hand-off to HR Screening (2 Sep 2026): TA is asked
            # to schedule the HR round, RMG and HR are told the candidate is
            # theirs next. HR also hears through the stage-arrival route; the
            # bell dedupes per person, so nobody is told twice.
            from services.notify import notify_roles
            notify_roles(db, ["TA", "RMG", "HR"],
                         f"Approved — HR Screening next: {cname}",
                         f"Sales Head approved {cname}'s terms. The candidate is in HR "
                         "Screening: HR reviews the details and requests the HR round; TA "
                         "then books it with the candidate; HR records Hire / Not Recommend.",
                         link, exclude_user_id=user.id, actor=user,
                         event="candidate.hr_screening",
                         related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass  # the decision is recorded either way
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message={
        "approve": "Approved — moved to HR Screening; HR will review and request the HR round",
        "send_back": "Sent back to Sales to redo the terms",
        "reject": "Candidate rejected — Sales notified",
    }[payload.decision])


# ---------------------------------------------------------------------------
# The Pre-Onboarding budget hold (3 Sep 2026, user flow)
#
# After the HR round the profile is at Preboarding and HR re-checks the CTCs
# and the customer onboarding date. In budget → HR completes onboarding and
# marks Joined (no approval needed). Not in budget / date mismatch → HR
# corrects the Expected CTC / date, writes what is wrong and FLAGS it; Sales
# Head and the Sales person who submitted the terms are told. Sales talks to
# the customer and REPLIES (optionally with revised terms); HR is told and
# decides. A parallel flag — the stage never moves.
# ---------------------------------------------------------------------------

class BudgetFlagIn(BaseModel):
    note: str = Field(min_length=5, max_length=2000)
    #: Corrected figures, in RUPEES (the UI converts from Lac).
    expected_ctc: float | None = None
    current_ctc: float | None = None
    customer_onboarding_date: date | None = None


class BudgetResolveIn(BaseModel):
    note: str = Field(min_length=5, max_length=2000)
    #: Revised terms agreed with the customer, if any — same shape Sales Head
    #: uses to correct an offer.
    rate_value: float | None = None
    rate_unit: str | None = Field(default=None, pattern="^(Hourly|Monthly|Yearly)$")
    customer_onboarding_date: date | None = None


def _budget_actor_name(user: CurrentUser) -> str:
    return (getattr(user, "full_name", "") or getattr(user, "username", "") or "").strip()


@router.post("/{profile_id}/budget-flag")
def flag_out_of_budget(profile_id: int, payload: BudgetFlagIn,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(gated_write("profiles", "HR"))):
    """HR: the candidate is out of budget / the joining date does not fit.

    Saves HR's corrected figures on the profile, marks the budget hold and
    tells Sales Head + the submitting Sales person. Allowed at Pre-Onboarding
    (and at HR Interviewing, when the verdict already said Not Recommend).
    """
    from services.candidate_profiles import BUDGET_OUT
    profile = get_profile_or_404(db, profile_id)
    status = _status_val(profile.pipeline_status)
    if status not in (PipelineStatus.PREBOARDING.value, PipelineStatus.HR_INTERVIEWING.value):
        raise HTTPException(status_code=400,
                            detail="The budget check happens at Pre-Onboarding — this candidate is at "
                                   f"{status.replace('_', ' ')}.")
    if not getattr(user, "is_admin", False) and "HR" not in (user.roles or set()):
        raise HTTPException(status_code=403, detail="Only HR raises the budget flag.")

    changes: list[str] = []
    updates = {k: v for k, v in {
        "expected_ctc": payload.expected_ctc, "current_ctc": payload.current_ctc,
    }.items() if v is not None}
    if updates:
        _reject_impossible_ctc(updates)
    for field, value in updates.items():
        before = getattr(profile, field)
        if float(before or 0) != float(value):
            changes.append(f"{field.replace('_', ' ')} ₹{float(before or 0):,.0f} → ₹{float(value):,.0f}")
            setattr(profile, field, value)
    if "current_ctc" in updates or "expected_ctc" in updates:
        profile.hike_percent = compute_hike_percent(profile.current_ctc, profile.expected_ctc)
    if (payload.customer_onboarding_date is not None
            and profile.customer_onboarding_date != payload.customer_onboarding_date):
        changes.append(f"customer onboarding {profile.customer_onboarding_date} → "
                       f"{payload.customer_onboarding_date}")
        profile.customer_onboarding_date = payload.customer_onboarding_date

    note = payload.note.strip()
    profile.budget_status = BUDGET_OUT
    profile.budget_note = note
    profile.budget_flagged_by = user.id
    profile.budget_flagged_at = datetime.now(timezone.utc)
    profile.budget_resolution_note = None
    profile.budget_resolved_by = None
    profile.budget_resolved_at = None
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "BUDGET_FLAGGED",
                 "HR flagged OUT OF BUDGET: " + note
                 + (f" — {'; '.join(changes)}" if changes else ""))

    # Sales Head + the Sales person who submitted the terms.
    try:
        from services.notify import notify_role, notify_user
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        link = f"/admin?view=crm&p=profiles/{profile.id}"
        offer = _latest_pending_offer(db, profile)
        rows = [
            ("Expected CTC (annual)", f"₹{float(profile.expected_ctc or 0):,.0f}"),
            ("Current CTC (annual)", f"₹{float(profile.current_ctc or 0):,.0f}"),
            ("Approved rate", (f"₹{float(offer.rate_value or offer.ctc or 0):,.0f} "
                               f"{(offer.rate_unit or 'Yearly').lower()}") if offer else "—"),
            ("Customer onboarding", str(profile.customer_onboarding_date or "—")),
            ("HR", _budget_actor_name(user)),
        ]
        title = f"Out of budget — {cname}"
        message = (f"HR checked {cname} at Pre-Onboarding and the terms do not fit: {note} "
                   "Please discuss with the customer and reply to HR from the candidate's profile.")
        submitter = _approval_submitter_id(db, profile)
        notify_role(db, "Sales_Head", title, message, link, exclude_user_id=user.id,
                    event="candidate.out_of_budget", actor=user, rows=rows,
                    related_type="candidate", related_id=profile.candidate_id)
        if submitter and submitter != user.id:
            notify_user(db, submitter, title, message, link, event="candidate.out_of_budget",
                        actor=user, rows=rows,
                        related_type="candidate", related_id=profile.candidate_id)
        else:
            notify_role(db, "Sales", title, message, link, exclude_user_id=user.id,
                        event="candidate.out_of_budget", actor=user, rows=rows,
                        related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass  # the flag is recorded either way
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile),
                    message="Flagged out of budget — Sales Head and the Sales person notified")


@router.post("/{profile_id}/budget-resolve")
def resolve_budget(profile_id: int, payload: BudgetResolveIn,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(gated_write("profiles", "Sales", "Sales_Head"))):
    """Sales / Sales Head reply to HR's budget flag after talking to the customer.

    Optionally revises the approved terms (rate + unit, onboarding date) on
    the pending offer — the same correction Sales Head may make at approval —
    then tells HR, who decides: Joined if it fits, otherwise leave it.
    """
    from services.candidate_profiles import BUDGET_OUT, BUDGET_RESOLVED, BUDGET_CONCERN
    profile = get_profile_or_404(db, profile_id)
    if profile.budget_status not in (BUDGET_OUT, BUDGET_CONCERN):
        raise HTTPException(status_code=400,
                            detail="HR has not raised a budget flag on this candidate.")
    if not getattr(user, "is_admin", False) and not ({"Sales", "Sales_Head"} & set(user.roles or set())):
        raise HTTPException(status_code=403, detail="Only Sales / Sales Head reply to the budget flag.")

    changes: list[str] = []
    offer = _latest_pending_offer(db, profile)
    if payload.rate_value is not None:
        if offer is None:
            raise HTTPException(status_code=400, detail="No pending offer to revise")
        new_unit = payload.rate_unit or offer.rate_unit or "Yearly"
        new_annual = annualise_rate(payload.rate_value, new_unit)
        old_text = (f"₹{float(offer.rate_value or offer.ctc or 0):,.0f} "
                    f"{(offer.rate_unit or 'Yearly').lower()}")
        changes.append(f"rate {old_text} → ₹{payload.rate_value:,.0f} {new_unit.lower()}")
        offer.ctc = new_annual
        offer.rate_unit = new_unit
        offer.rate_value = payload.rate_value
    if payload.customer_onboarding_date is not None:
        if offer is not None and offer.joining_date != payload.customer_onboarding_date:
            offer.joining_date = payload.customer_onboarding_date
        if profile.customer_onboarding_date != payload.customer_onboarding_date:
            changes.append(f"customer onboarding {profile.customer_onboarding_date} → "
                           f"{payload.customer_onboarding_date}")
            profile.customer_onboarding_date = payload.customer_onboarding_date

    note = payload.note.strip()
    profile.budget_status = BUDGET_RESOLVED
    profile.budget_resolution_note = note
    profile.budget_resolved_by = user.id
    profile.budget_resolved_at = datetime.now(timezone.utc)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "BUDGET_RESOLVED",
                 f"Sales replied to the budget flag: {note}"
                 + (f" — {'; '.join(changes)}" if changes else ""))

    try:
        from services.notify import notify_role, notify_user
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        link = f"/admin?view=crm&p=profiles/{profile.id}"
        rows = ([("Revised terms", "; ".join(changes))] if changes else []) + [
            ("Sales", _budget_actor_name(user)),
        ]
        title = f"Budget reply from Sales — {cname}"
        message = (f"{_budget_actor_name(user)} replied to your budget flag on {cname}: {note} "
                   "If the terms now fit, complete onboarding and mark Joined; otherwise leave the "
                   "candidate as is.")
        if profile.budget_flagged_by and profile.budget_flagged_by != user.id:
            notify_user(db, profile.budget_flagged_by, title, message, link,
                        event="candidate.budget_resolved", actor=user, rows=rows,
                        related_type="candidate", related_id=profile.candidate_id)
        notify_role(db, "HR", title, message, link,
                    exclude_user_id=profile.budget_flagged_by or user.id,
                    event="candidate.budget_resolved", actor=user, rows=rows,
                    related_type="candidate", related_id=profile.candidate_id)
    except Exception:
        pass
    db.commit()
    db.refresh(profile)
    return envelope(data=profile_to_dict(profile), message="Reply sent — HR notified")


# ---------------------------------------------------------------------------
# Offers
# ---------------------------------------------------------------------------

@router.post("/{profile_id}/offer")
def create_offer(profile_id: int, payload: OfferCreate,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(offer_roles)):
    profile = get_profile_or_404(db, profile_id)
    # Offer CTC is typed into a "(Lac)" field too — same 10^5 trap.
    _reject_impossible_ctc(payload.model_dump())
    offer = OfferHistory(profile_id=profile.id, status=OfferStatus.PENDING,
                         **payload.model_dump())
    db.add(offer)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_CREATED",
                 f"Offer #{offer.id} created (CTC {payload.ctc}, date {payload.offer_date.isoformat()})")
    db.commit()
    db.refresh(offer)
    return envelope(data=offer_to_dict(offer), message="Offer created")


@router.put("/{profile_id}/offer/{offer_id}")
def update_offer(profile_id: int, offer_id: int, payload: OfferUpdate,
                 db: Session = Depends(get_crm_db),
                 user: CurrentUser = Depends(offer_roles)):
    profile = get_profile_or_404(db, profile_id)
    offer = db.get(OfferHistory, offer_id)
    if not offer or offer.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Offer not found for this profile")
    updates = payload.model_dump(exclude_unset=True)
    new_status = updates.pop("status", None)
    if new_status is not None:
        valid = {m.value for m in OfferStatus}
        if new_status not in valid:
            raise HTTPException(status_code=400,
                                detail=f"Unknown offer status '{new_status}'. "
                                       f"Valid values: {', '.join(sorted(valid))}")
        offer.status = OfferStatus(new_status)
        if new_status == OfferStatus.ACCEPTED.value and "acceptance_date" not in updates \
                and offer.acceptance_date is None:
            offer.acceptance_date = date.today()
    for field, value in updates.items():
        setattr(offer, field, value)
    changed = sorted(list(updates) + (["status"] if new_status is not None else []))
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_UPDATED", f"Offer #{offer.id} updated: {', '.join(changed) or 'none'}")
    db.commit()
    db.refresh(offer)
    return envelope(data=offer_to_dict(offer), message="Offer updated")


class OfferLetterIn(BaseModel):
    """HR's edits to the generated letter (4 Sep 2026). `fields` overrides
    individual facts; `paragraphs` replaces the body wholesale. Send `reset`
    to go back to the default letter."""
    fields: dict[str, str] | None = None
    paragraphs: list[str] | None = None
    reset: bool = False


def _offer_or_404(db: Session, profile, offer_id: int):
    offer = db.get(OfferHistory, offer_id)
    if not offer or offer.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="Offer not found for this profile")
    return offer


def _letter_payload(db, profile, offer, user) -> dict:
    from services.offer_letter import (
        EDITABLE_FIELDS, build_offer_letter_context, effective_letter, offer_letter_facts,
        offer_letter_paragraphs,
    )
    base = build_offer_letter_context(db, profile, offer, user)
    overrides = getattr(offer, "letter_overrides", None) or None
    ctx, paragraphs = effective_letter(base, overrides)
    return {
        "context": ctx,
        "default_context": base,
        "paragraphs": paragraphs,
        "facts": offer_letter_facts(ctx),
        "default_paragraphs": offer_letter_paragraphs(ctx),
        "editable_fields": list(EDITABLE_FIELDS),
        "overrides": overrides,
        "has_edits": bool(overrides),
    }


@router.get("/{profile_id}/offer/{offer_id}/letter")
def view_offer_letter(profile_id: int, offer_id: int,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(offer_roles)):
    """The letter as data — what the editor shows and what the PDF / Word
    will say, with HR's saved edits applied."""
    profile = get_profile_or_404(db, profile_id)
    offer = _offer_or_404(db, profile, offer_id)
    return envelope(data=_letter_payload(db, profile, offer, user))


@router.put("/{profile_id}/offer/{offer_id}/letter")
def save_offer_letter(profile_id: int, offer_id: int, payload: OfferLetterIn,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(offer_roles)):
    """Save HR's edits on the offer row; the next download uses them."""
    from services.offer_letter import clean_overrides

    profile = get_profile_or_404(db, profile_id)
    offer = _offer_or_404(db, profile, offer_id)
    if payload.reset:
        offer.letter_overrides = None
        note = f"Offer #{offer.id} letter reset to the default wording"
    else:
        offer.letter_overrides = clean_overrides({"fields": payload.fields, "paragraphs": payload.paragraphs})
        note = f"Offer #{offer.id} letter edited"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_LETTER_EDITED", note)
    db.commit()
    db.refresh(offer)
    return envelope(data=_letter_payload(db, profile, offer, user), message="Offer letter saved")


@router.get("/{profile_id}/offer/{offer_id}/letter.{fmt}")
def download_offer_letter(profile_id: int, offer_id: int, fmt: str,
                          db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(offer_roles)):
    """The formatted offer letter (4 Sep 2026, user request) — `letter.pdf`
    or `letter.docx`, built from the profile, offer, candidate, opportunity and
    org settings on file. Nothing is stored; regenerate after any edit."""
    from fastapi.responses import Response

    from services.offer_letter import (
        build_offer_letter_context, effective_letter, offer_letter_filename,
        render_offer_letter_docx, render_offer_letter_pdf,
    )

    fmt = (fmt or "").lower()
    if fmt not in ("pdf", "docx"):
        raise HTTPException(status_code=400, detail="Format must be pdf or docx")
    profile = get_profile_or_404(db, profile_id)
    offer = _offer_or_404(db, profile, offer_id)
    ctx, paragraphs = effective_letter(build_offer_letter_context(db, profile, offer, user),
                                       getattr(offer, "letter_overrides", None))
    if fmt == "pdf":
        body, media = render_offer_letter_pdf(ctx, paragraphs), "application/pdf"
    else:
        body = render_offer_letter_docx(ctx, paragraphs)
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "OFFER_LETTER_DOWNLOADED", f"Offer #{offer.id} letter downloaded as {fmt.upper()}")
    db.commit()
    return Response(content=body, media_type=media, headers={
        "Content-Disposition": f'attachment; filename="{offer_letter_filename(ctx, fmt)}"',
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/{profile_id}/offer-history")
def offer_history(profile_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    return envelope(data=[offer_to_dict(o) for o in profile.offers])


# --------------------------------------------------------------------------
# Interview rounds — RMG records feedback for every round on this application.
# Vocabulary and validation live in services/interview_rounds.py so the form's
# dropdowns and the server's checks come from one place.
# --------------------------------------------------------------------------

class InterviewRoundIn(BaseModel):
    kind: str = Field(description="Interview Round, e.g. L1_Interview")
    interview_category: str | None = None      # Internal / External
    employee_id: int | None = None             # panel member from the Employees tab
    interviewer: str | None = None             # free text for external panellists
    duration_minutes: int | None = None
    status: str | None = None
    scheduled_at: datetime | None = None       # Interview Date/Time From
    result: str | None = None
    feedback: str | None = None                # Overall Feedback
    user_role: str | None = None
    meeting_link: str | None = None
    stage: str | None = None
    mode: str | None = None


class InterviewRoundUpdate(BaseModel):
    kind: str | None = None
    interview_category: str | None = None
    employee_id: int | None = None
    interviewer: str | None = None
    duration_minutes: int | None = None
    status: str | None = None
    scheduled_at: datetime | None = None
    result: str | None = None
    feedback: str | None = None
    user_role: str | None = None
    meeting_link: str | None = None
    stage: str | None = None
    mode: str | None = None


@router.get("/{profile_id}/interview-rounds/options")
def interview_round_options(profile_id: int,
                            db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(any_crm_role)):
    """Dropdown values + the active employee list for the feedback form.

    Served from the router rather than the Employees tab so the picker does not
    depend on that tab's access template.
    """
    get_profile_or_404(db, profile_id)
    # Pass the user so the round list is narrowed to what they may actually
    # save — offering a choice the save would reject is a trap, not a form.
    return envelope(data=interview_round_options_data(db, user))


@router.get("/{profile_id}/interview-rounds")
def list_interview_rounds(profile_id: int,
                          db: Session = Depends(get_crm_db),
                          user: CurrentUser = Depends(any_crm_role)):
    profile = get_profile_or_404(db, profile_id)
    return envelope(data=interview_events_for_profile(db, profile.id))


def _email_candidate_round_invite(db: Session, profile: CandidateProfile,
                                  event: InterviewEvent, user: CurrentUser) -> None:
    """Send the candidate the details of a round that was just SCHEDULED.

    Mirrors the L2 face-to-face path: plain-text body + .ics, wording
    overridable from Settings → Email Drafts (event `candidate.round_invite`),
    recorded on the candidate's Emails thread. Best-effort — a mail failure
    never fails the round.
    """
    try:
        if not event.scheduled_at or not (event.meeting_link or "").strip():
            return  # nothing to invite anyone TO yet
        candidate = db.get(Candidate, profile.candidate_id)
        email = (getattr(candidate, "email", None) or "").strip()
        if not email or "@noemail" in email or "@import.karnex.in" in email:
            return
        from services.candidate_comms import (
            build_ics_invite, candidate_template_override, customer_round_invite_message,
            send_candidate_email,
        )
        from services.email_outbox import record_inline_email
        from services.interview_rounds import round_label

        cname = " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or "Candidate"
        label = round_label(event.kind)
        # Show the time in IST — the zone the TA typed it in.
        try:
            from zoneinfo import ZoneInfo
            when_txt = event.scheduled_at.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
        except Exception:
            when_txt = event.raw_when or str(event.scheduled_at)
        link = event.meeting_link.strip()
        subject, body = customer_round_invite_message(cname, label, when_txt, link, event.note)
        subject, body, _ = candidate_template_override(
            db, "candidate.round_invite", subject, body,
            {"candidate": cname, "round": label, "when": when_txt, "link": link,
             "interviewer": event.interviewer or "", "note": event.note or ""})
        ics = build_ics_invite(summary=f"{label} — {cname}", starts_at=event.scheduled_at,
                               description=event.note or f"{label} with Karnex.",
                               location=link, uid=f"karnex-round-{event.id}")
        res = send_candidate_email(email, subject, body, actor=user,
                                   attachments=[("interview-invite.ics", ics.encode("utf-8"), "text/calendar")])
        sent = bool(res.get("sent"))
        record_inline_email(
            db, to_email=email, to_name=cname, subject=subject, body_text=body,
            event="candidate.round_invite", actor=user,
            related_type="candidate", related_id=profile.candidate_id,
            sent=sent, error=None if sent else str(res.get("error") or "send_failed"),
        )
    except Exception:
        logger.warning("round invite email failed for profile %s", getattr(profile, "id", "?"),
                       exc_info=True)


@router.post("/{profile_id}/interview-rounds")
def create_interview_round(profile_id: int, payload: InterviewRoundIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    values = validate_round(db, payload)
    # The endpoint gate only says "may write SOME round". Which round is decided
    # here: RMG owns the technical ladder, Sales owns the customer conversation.
    ensure_may_write_round(user, values.get("kind"))
    _ensure_hr_verdict_is_hrs(user, values.get("kind"), values)
    event = InterviewEvent(profile_id=profile.id, candidate_id=profile.candidate_id,
                           created_by=user.id, **values)
    db.add(event)
    db.flush()
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_ADDED",
                 f"{round_label(event.kind)} recorded"
                 f"{f' — {event.result}' if event.result else ''}"
                 f"{f' (interviewer: {event.interviewer})' if event.interviewer else ''}")
    # SAVING CUSTOMER FEEDBACK *IS* THE STATUS CHANGE (user decision, 27 Aug
    # 2026). Only once a verdict exists — a round merely SCHEDULED (link
    # recorded, interview still to happen) must not move the pipeline.
    moved = None
    if event.result:
        from services.candidate_profiles import (
            advance_on_hr_verdict, advance_status_for_customer_round, reject_on_round_verdict,
        )
        if event.kind == "HR_Interview":
            # HR's verdict ends the HR stage (3 Sep 2026): Hire or Not
            # Recommend, the profile moves to Pre-Onboarding.
            moved = advance_on_hr_verdict(db, profile, event.result, event.feedback or "", user)
        else:
            # "No Hire" closes the candidacy at this round (7 Sep 2026);
            # anything else moves the stage the round proves.
            moved = reject_on_round_verdict(db, profile, event.kind, event.result, event.feedback or "", user) \
                or advance_status_for_customer_round(db, profile, event.kind, event.feedback or "", user)
    else:
        # A SCHEDULED round (no verdict yet) with a time and a link is an
        # invitation — the candidate must receive it (2 Sep 2026 bug report:
        # customer L1/L2 links were recorded here and never reached anyone).
        # Best-effort, .ics attached, logged to the candidate's Emails thread.
        _email_candidate_round_invite(db, profile, event, user)
        # And tell the panel's own team a round is on the calendar (2 Sep 2026):
        # TA schedules every round, so the people who actually take it — RMG,
        # HR or Sales for the customer's — hear it was booked and can record
        # the outcome afterwards. Nothing to the scheduler themselves.
        _notify_round_owner_scheduled(db, profile, event, user)
        if event.kind == "HR_Interview":
            # Booking the HR round IS the move to HR Interviewing (3 Sep 2026).
            from services.candidate_profiles import advance_on_hr_round_scheduled
            moved = advance_on_hr_round_scheduled(
                db, profile, user,
                event.raw_when or (event.scheduled_at.isoformat() if event.scheduled_at else ""))
    db.commit()
    db.refresh(event)
    return envelope(
        data={**interview_event_to_dict(event), "pipeline_status": _status_val(profile.pipeline_status)},
        message=("Round scheduled — the candidate has been invited" if not event.result
                 else "Interview feedback saved")
                + (f" — status moved to {moved.replace('_', ' ')}" if moved else ""),
    )


def _ensure_hr_verdict_is_hrs(user: CurrentUser, kind: str | None, values: dict) -> None:
    """TA may SCHEDULE the HR round, but its verdict is HR's alone (2 Sep 2026,
    user decision). A result or feedback on an HR_Interview from anyone but
    HR (Admin/CEO aside) is refused."""
    if kind != "HR_Interview" or getattr(user, "is_admin", False):
        return
    if not (values.get("result") or values.get("feedback")):
        return
    if "HR" not in (user.roles or set()):
        raise HTTPException(status_code=403,
                            detail="Only HR records the HR round's feedback. TA schedules it.")


#: Whose round each kind is — the team told when TA books it.
_ROUND_OWNER_ROLE: dict[str, str] = {
    "L1_Interview": "RMG", "L2_F2F": "RMG", "L3_Interview": "RMG", "L4_Interview": "RMG",
    "HR_Interview": "HR",
    "Customer_Interview": "Sales", "Customer_L2": "Sales",
}


def _notify_round_owner_scheduled(db: Session, profile, event, user: CurrentUser) -> None:
    owner = _ROUND_OWNER_ROLE.get(event.kind or "")
    if not owner or owner in (user.roles or set()):
        return
    try:
        from services.notify import notify_role
        candidate = db.get(Candidate, profile.candidate_id)
        cname = (f"{candidate.first_name} {candidate.last_name or ''}".strip()
                 if candidate else f"Candidate #{profile.candidate_id}")
        when = event.raw_when or (event.scheduled_at.isoformat() if event.scheduled_at else "")
        notify_role(
            db, owner,
            f"{round_label(event.kind)} scheduled: {cname}",
            f"{round_label(event.kind)} with {cname}"
            + (f" on {when}" if when else "")
            + (f" — interviewer {event.interviewer}" if event.interviewer else "")
            + ". Record the outcome on the candidate's Interviews tab afterwards.",
            applied_candidates_link(db, profile),
            exclude_user_id=user.id, actor=user, event="candidate.round_scheduled",
            related_type="candidate", related_id=profile.candidate_id,
        )
    except Exception:
        pass


@router.put("/{profile_id}/interview-rounds/{event_id}")
def update_interview_round(profile_id: int, event_id: int, payload: InterviewRoundUpdate,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    event = get_round_or_404(db, profile.id, event_id)
    values = validate_round(db, payload, partial=True, kind_hint=event.kind)
    # Check the round as it stands AND as it would become, so an edit cannot be
    # used to convert someone else's round into one you own, or yours into theirs.
    ensure_may_write_round(user, event.kind)
    if values.get("kind") and values["kind"] != event.kind:
        ensure_may_write_round(user, values["kind"])
    _ensure_hr_verdict_is_hrs(user, values.get("kind") or event.kind, values)
    had_link = bool((event.meeting_link or "").strip())
    for field, value in values.items():
        setattr(event, field, value)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_UPDATED",
                 f"{round_label(event.kind)} updated: {', '.join(sorted(values)) or 'none'}")
    # The meeting link arrived AFTER the slot was booked (7 Sep 2026: Sales
    # books the customer's slot, TA adds the link later) — that is the moment
    # the candidate can actually be invited, so send the invite now.
    if (not had_link and (event.meeting_link or "").strip() and not event.result
            and event.scheduled_at is not None):
        _email_candidate_round_invite(db, profile, event, user)
        _notify_round_owner_scheduled(db, profile, event, user)
    moved = None
    if event.result:
        from services.candidate_profiles import (
            advance_on_hr_verdict, advance_status_for_customer_round, reject_on_round_verdict,
        )
        if event.kind == "HR_Interview":
            moved = advance_on_hr_verdict(db, profile, event.result, event.feedback or "", user)
        else:
            moved = reject_on_round_verdict(db, profile, event.kind, event.result, event.feedback or "", user) \
                or advance_status_for_customer_round(db, profile, event.kind, event.feedback or "", user)
    db.commit()
    db.refresh(event)
    return envelope(
        data={**interview_event_to_dict(event), "pipeline_status": _status_val(profile.pipeline_status)},
        message="Interview feedback updated"
                + (f" — status moved to {moved.replace('_', ' ')}" if moved else ""),
    )


@router.delete("/{profile_id}/interview-rounds/{event_id}")
def delete_interview_round(profile_id: int, event_id: int,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(interview_round_roles)):
    profile = get_profile_or_404(db, profile_id)
    event = get_round_or_404(db, profile.id, event_id)
    ensure_may_write_round(user, event.kind)
    label = round_label(event.kind)
    db.delete(event)
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "INTERVIEW_ROUND_DELETED", f"{label} removed")
    db.commit()
    return envelope(data={"id": event_id}, message="Interview round removed")


# --------------------------------------------------------------------------
# Workflow actions surfaced as buttons on the Candidate Profiles page.
# Each one does the real pipeline work rather than just stamping a field.
# --------------------------------------------------------------------------

class SubmitToCustomerIn(BaseModel):
    #: Defaults to today when omitted.
    submitted_on: date | None = None
    comment: str | None = None


@router.post("/{profile_id}/submit-to-customer")
def submit_to_customer(profile_id: int, payload: SubmitToCustomerIn | None = None,
                       db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(role_required("Sales", "Sales_Head"))):
    """Record that this candidate was submitted to the customer.

    Stamps `customer_submission_date` and, when the profile is at Sales Screening
    AND this user has authority over that stage, advances it to Customer
    Screening — the move this action represents. Authority is checked BEFORE
    anything is written, so the date and the stage always agree.
    """
    body = payload or SubmitToCustomerIn()
    profile = get_profile_or_404(db, profile_id)
    when = body.submitted_on or date.today()

    if profile.customer_submission_date and not body.submitted_on:
        raise HTTPException(
            status_code=409,
            detail=f"Already submitted to the customer on {profile.customer_submission_date}. "
                   f"Send submitted_on to change the date.",
        )

    at_sales_screening = (
        _status_val(profile.pipeline_status) == PipelineStatus.SALES_SCREENING.value
    )
    move = at_sales_screening and user_may_transition_from(profile.pipeline_status, user)

    profile.customer_submission_date = when
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                 "SUBMITTED_TO_CUSTOMER",
                 (body.comment or "").strip() or f"Submitted to the customer on {when}")
    if move:
        perform_transition(db, profile, PipelineStatus.CUSTOMER_SCREENING.value,
                           f"Submitted to the customer on {when}", user)

    db.commit()
    db.refresh(profile)
    return envelope(
        data=profile_detail(db, profile, user),
        message=f"Submitted to the customer on {when}"
                + (" — moved to Customer Screening" if move else ""),
    )
