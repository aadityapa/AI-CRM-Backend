"""Full data backup (Admin / CEO only) — Settings ▸ Backup.

    GET  /api/admin/backup/datasets        the tabs the user can pick
    POST /api/admin/backup                 {datasets: ["customers", …] | ["all"]} → starts a job
    GET  /api/admin/backup/status[?job_id] progress of the latest / given job
    GET  /api/admin/backup/history         finished archives kept on disk
    GET  /api/admin/backup/download/{name} streams one archive

Only one build runs at a time (409 otherwise). See services/data_backup.py.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from crm_db import get_session_factory
from crm_deps import CurrentUser, role_required
from schemas.common import envelope
from services import data_backup as backup

router = APIRouter(prefix="/api/admin/backup", tags=["crm-backup"])

admin_only = role_required()   # Admin / CEO — no other role, ever


class BackupRequest(BaseModel):
    datasets: list[str] = Field(default_factory=list, description='Dataset keys, or ["all"] / empty for everything')
    #: Optional inclusive window. Applies to tables with a date column (records
    #: created / dated inside it); masters, policies and settings are always whole.
    date_from: date | None = None
    date_to: date | None = None


@router.get("/datasets")
def backup_datasets(user: CurrentUser = Depends(admin_only)):
    return envelope(data={
        "datasets": backup.dataset_options(),
        "keep_last": backup.KEEP_LAST,
        "free_space_bytes": backup.free_space_bytes(),
    })


@router.post("")
def start_backup(body: BackupRequest, user: CurrentUser = Depends(admin_only)):
    try:
        datasets = backup.resolve_datasets(body.datasets)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if body.date_from and body.date_to and body.date_from > body.date_to:
        raise HTTPException(status_code=400, detail="'From' date must be on or before 'To' date")
    try:
        session_factory = get_session_factory()
    except Exception as exc:  # noqa: BLE001 — CRM database not configured
        raise HTTPException(status_code=503, detail=f"CRM database unavailable: {exc}")
    try:
        job = backup.start_job(datasets, user.full_name or user.username or f"user:{user.id}",
                               session_factory, date_from=body.date_from, date_to=body.date_to)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    window = (f" for {body.date_from or '…'} to {body.date_to or '…'}" if (body.date_from or body.date_to) else "")
    return envelope(data=job.to_dict(),
                    message=f"Backup started — {len(datasets)} dataset(s){window}. This can take a few minutes.")


@router.get("/status")
def backup_status(job_id: str | None = None, user: CurrentUser = Depends(admin_only)):
    return envelope(data=backup.job_status(job_id))


@router.get("/history")
def backup_history(user: CurrentUser = Depends(admin_only)):
    return envelope(data=backup.list_archives())


@router.get("/download/{name}")
def download_backup(name: str, user: CurrentUser = Depends(admin_only)):
    path = backup.archive_path(name)
    if path is None:
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(path, media_type="application/zip", filename=path.name)
