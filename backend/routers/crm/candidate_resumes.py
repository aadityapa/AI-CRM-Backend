"""Resume library, matching positions and multi-apply (9 Oct 2026).

  GET    /api/candidates/{id}/resume-library                list the versions
  POST   /api/candidates/{id}/resume-library                add one (multipart)
  PATCH  /api/candidates/{id}/resume-library/{rid}          rename / make main
  DELETE /api/candidates/{id}/resume-library/{rid}          remove (not the main one)
  GET    /api/candidates/{id}/matching-positions            every live position, scored
  POST   /api/candidates/{id}/matching-positions/{req}/ai-review   the paid AI look, on TA's click
  POST   /api/candidates/{id}/multi-apply                   apply to several positions at once

Multi-apply and the AI review are TA's (user decision: "TA only"; Admin/CEO
implicit). Every application goes through `create_profile_core`, the Apply
button's own path, so the sourcing, duplicate and ATS rules are the same.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, gated_read, role_required
from models import Requirement
from schemas.candidate_profiles import ProfileCreate
from schemas.common import envelope
from services import candidate_resumes as lib
from services.candidates import get_candidate_or_404

router = APIRouter(prefix="/api/candidates", tags=["CRM: Resume library"])

read_gate = gated_read("candidates", "TA", "RMG", "Sales", "Sales_Head", "HR")
write_gate = role_required("TA", "RMG", "Sales", "Sales_Head", "HR")
ta_gate = role_required("TA")

#: Positions one multi-apply may cover.
MAX_APPLY = 15


class VersionUpdateIn(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    primary: bool | None = None


class ApplyItem(BaseModel):
    requirement_id: int
    resume_id: int | None = None


class MultiApplyIn(BaseModel):
    items: list[ApplyItem] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)


class AiReviewIn(BaseModel):
    resume_id: int


@router.get("/{candidate_id}/resume-library")
def list_library(candidate_id: int, db: Session = Depends(get_crm_db), user: CurrentUser = Depends(read_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    rows = lib.sync_library(db, cand)
    db.commit()
    return envelope(data=[lib.serialize_version(v) for v in rows], meta={"max_versions": lib.MAX_VERSIONS})


@router.post("/{candidate_id}/resume-library")
def add_to_library(candidate_id: int,
                   file: UploadFile = File(...),
                   label: str = Form(""),
                   make_primary: bool = Form(False),
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(write_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    v, created = lib.add_version(db, cand, file, label=label, user=user, make_primary=make_primary)
    lib.version_text(db, v)  # read once now, so matching is instant
    db.commit()
    return envelope(data=lib.serialize_version(v),
                    message="Resume added" if created else "This file is already in the library")


@router.patch("/{candidate_id}/resume-library/{resume_id}")
def update_version(candidate_id: int, resume_id: int, body: VersionUpdateIn,
                   db: Session = Depends(get_crm_db), user: CurrentUser = Depends(write_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    lib.sync_library(db, cand)
    v = lib.version_for_application(db, cand, resume_id)
    if body.label is not None:
        lib.rename_version(db, cand, v, body.label)
    if body.primary:
        lib.set_primary(db, cand, v)
    db.commit()
    return envelope(data=lib.serialize_version(v), message="Resume updated")


@router.delete("/{candidate_id}/resume-library/{resume_id}")
def delete_version(candidate_id: int, resume_id: int,
                   db: Session = Depends(get_crm_db), user: CurrentUser = Depends(write_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    lib.sync_library(db, cand)
    v = lib.version_for_application(db, cand, resume_id)
    lib.delete_version(db, cand, v)
    db.commit()
    return envelope(data={"id": resume_id}, message="Resume removed from the library")


@router.get("/{candidate_id}/matching-positions")
def matching_positions(candidate_id: int, db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(read_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    data = lib.matching_positions(db, cand)
    db.commit()  # the library sync + cached resume text
    return envelope(data=data["positions"], meta={k: v for k, v in data.items() if k != "positions"})


@router.post("/{candidate_id}/matching-positions/{requirement_id}/ai-review")
def ai_review(candidate_id: int, requirement_id: int, body: AiReviewIn,
              db: Session = Depends(get_crm_db), user: CurrentUser = Depends(ta_gate)):
    cand = get_candidate_or_404(db, candidate_id)
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Position not found")
    lib.sync_library(db, cand)
    v = lib.version_for_application(db, cand, body.resume_id)
    review = lib.ai_review(db, cand, v, req, user)
    db.commit()
    return envelope(data=review, message="AI review ready")


@router.post("/{candidate_id}/multi-apply")
def multi_apply(candidate_id: int, body: MultiApplyIn,
                db: Session = Depends(get_crm_db), user: CurrentUser = Depends(ta_gate)):
    """Apply the candidate to several positions, each with the resume TA chose.
    Each position stands alone: one refusal (already applied, not open) never
    blocks the others."""
    from routers.crm.candidate_profiles import create_profile_core

    cand = get_candidate_or_404(db, candidate_id)
    items = body.items[:MAX_APPLY]
    if not items:
        raise HTTPException(status_code=400, detail="Pick at least one position.")
    if len({i.requirement_id for i in items}) != len(items):
        raise HTTPException(status_code=400, detail="A position is listed twice.")
    lib.sync_library(db, cand)
    results = []
    applied = 0
    for item in items:
        req = db.get(Requirement, item.requirement_id)
        label = f"{req.req_number} · {req.title}" if req else f"Position #{item.requirement_id}"
        try:
            with db.begin_nested():
                if req is None or not req.opportunity_id:
                    raise HTTPException(status_code=404, detail="Position not found")
                version = lib.version_for_application(db, cand, item.resume_id)
                profile, _missing = create_profile_core(
                    db, ProfileCreate(candidate_id=cand.id, opportunity_id=req.opportunity_id, notes=body.note),
                    user, resume_version=version)
            applied += 1
            results.append({"requirement_id": item.requirement_id, "position": label, "ok": True,
                            "profile_id": profile.id,
                            "resume": (version.label if version else None)})
        except HTTPException as exc:
            detail = exc.detail.get("message") if isinstance(exc.detail, dict) else exc.detail
            results.append({"requirement_id": item.requirement_id, "position": label, "ok": False,
                            "message": str(detail)})
    db.commit()
    msg = f"Applied to {applied} of {len(items)} position{'s' if len(items) != 1 else ''}"
    return envelope(data=results, message=msg)
