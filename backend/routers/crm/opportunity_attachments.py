"""Opportunity attachments — list / upload / delete (matches the Attachments card)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db, role_required, gated_write
from models import Opportunity, OpportunityActivityLog, OpportunityAttachment
from schemas.common import envelope
from services.crm_common import log_activity, save_upload_hashed

router = APIRouter(tags=["CRM: Opportunity Attachments"])

writer = gated_write("opportunities", "Sales", "Sales_Head")

_MAX_BYTES = 15 * 1024 * 1024  # 15 MB
_ALLOWED_KINDS = {"customer_jd", "general"}


def _opp_or_404(db: Session, opportunity_id: int) -> Opportunity:
    opp = db.get(Opportunity, opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    return opp


def _serialize(a: OpportunityAttachment) -> dict:
    return {
        "id": a.id,
        "opportunity_id": a.opportunity_id,
        "file_url": a.file_url,
        "file_name": a.file_name,
        "file_sha256": a.file_sha256,
        "file_size": a.file_size,
        "kind": a.kind or "general",
        "uploaded_by": a.uploaded_by,
        "uploaded_at": a.uploaded_at.isoformat() if a.uploaded_at else None,
    }


@router.get("/api/opportunities/{opportunity_id}/attachments")
def list_attachments(
    opportunity_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    _opp_or_404(db, opportunity_id)
    rows = db.execute(
        select(OpportunityAttachment)
        .where(OpportunityAttachment.opportunity_id == opportunity_id)
        .order_by(OpportunityAttachment.id.desc())
    ).scalars().all()
    return envelope([_serialize(a) for a in rows])


@router.post("/api/opportunities/{opportunity_id}/attachments")
def add_attachment(
    opportunity_id: int,
    file: UploadFile = File(...),
    kind: str | None = Form(None),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(writer),
):
    opp = _opp_or_404(db, opportunity_id)
    data = file.file.read()
    if len(data) > _MAX_BYTES:
        raise HTTPException(status_code=400, detail="File is larger than 15 MB")
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    file.file.seek(0)
    kind_norm = (kind or "general").strip().lower() or "general"
    if kind_norm not in _ALLOWED_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid kind '{kind}'. Allowed: {', '.join(sorted(_ALLOWED_KINDS))}",
        )
    url, sha, size = save_upload_hashed(file, "opportunity_attachments")
    att = OpportunityAttachment(
        opportunity_id=opp.id,
        file_url=url,
        file_name=(file.filename or "")[:255] or None,
        file_sha256=sha,
        file_size=size,
        kind=kind_norm,
        uploaded_by=user.id,
    )
    db.add(att)
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp.id, user.id,
                 "Attachment_Added", f"Attachment added ({kind_norm}): {att.file_name or 'file'}")
    db.commit()
    db.refresh(att)
    return envelope(_serialize(att), message="Attachment added")


@router.delete("/api/opportunities/attachments/{attachment_id}")
def delete_attachment(
    attachment_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(writer),
):
    att = db.get(OpportunityAttachment, attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    opp_id = att.opportunity_id
    db.delete(att)
    log_activity(db, OpportunityActivityLog, "opportunity_id", opp_id, user.id,
                 "Attachment_Removed", f"Attachment #{attachment_id} removed")
    db.commit()
    return envelope({"id": attachment_id}, message="Attachment removed")
