"""Resume / ATS pipeline router: upload, ATS scan, shortlist, AI L1 scheduling.

Paths span two prefixes (/api/requirements/{id}/resumes and /api/resumes/{id}),
so the router carries no prefix of its own.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm_deps import (
    CurrentUser, PageParams, any_crm_role, get_crm_db, page_params,
    role_required, gated_write,
)
from models import (
    AiInterviewStatus, AtsStatus, Candidate, CandidateProfile,
    CandidateProfileActivityLog, PipelineStatus, Requirement,
    RequirementActivityLog, RequirementStatus, Resume,
)

logger = logging.getLogger("karnex.crm.resumes")
from pydantic import BaseModel, Field

from routers.crm.apply import _base_url
from schemas.common import envelope
from schemas.resumes import AI_INTERVIEW_STATUS_VALUES, ATS_STATUS_VALUES, ResumeScanResult
from services.ai_interview_bridge import ai_interview_autosend_enabled, schedule_l1_interview
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity, paginate, save_upload_hashed
from services.requirements import get_requirement_or_404
from services.resumes import enrich_resumes_with_ai, run_ats_scan, serialize_resume
from services.slot_booking import (
    auto_pipeline_after_scan, find_or_create_candidate_from_resume, get_or_create_profile,
)

router = APIRouter(tags=["CRM: Resumes"])

_UPLOAD_ALLOWED_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


def _now():
    return datetime.now(timezone.utc)


def _get_resume_or_404(db: Session, resume_id: int) -> Resume:
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    return resume


# ---------------------------------------------------------------- upload + list

@router.post("/api/requirements/{requirement_id}/resumes")
def upload_resume(
    requirement_id: int,
    file: UploadFile = File(...),
    candidate_name: str = Form(..., min_length=1, max_length=255),
    email: str | None = Form(None),
    phone: str | None = Form(None),
    source_portal: str | None = Form(None),
    # Same applicant details as the public apply-link form, so TA-entered
    # uploads carry identical data onto the Resume + Candidate.
    experience: str = Form("", max_length=64),
    education: str = Form("", max_length=120),
    technical_domain: str = Form("", max_length=120),
    skills: str = Form("", max_length=500),
    notice_period: str = Form("", max_length=60),
    current_ctc: str = Form("", max_length=40),
    expected_ctc: str = Form("", max_length=40),
    preferred_location: str = Form("", max_length=120),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    req = get_requirement_or_404(db, requirement_id)
    if req.status not in _UPLOAD_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Resumes can only be uploaded when the requirement is in "
                   f"{', '.join(s.value for s in _UPLOAD_ALLOWED_STATUSES)} "
                   f"(current: {req.status.value})",
        )
    existing = db.execute(
        select(func.count()).select_from(Resume).where(Resume.requirement_id == req.id)
    ).scalar() or 0

    file_url, file_sha256, file_size = save_upload_hashed(file, "resumes")

    # Dedupe: identical file (same bytes) already on this requirement → return it
    # instead of creating a duplicate application.
    dup = db.execute(
        select(Resume).where(Resume.requirement_id == req.id,
                             Resume.file_sha256 == file_sha256,
                             # Dismissed CVs never block a retry (0087).
                             Resume.duplicate_dismissed.is_not(True))
    ).scalars().first()
    if dup is not None:
        # Same file re-uploaded: still make sure a Candidate exists for it (the
        # original upload may predate immediate candidate creation), and a
        # profile too, so pre-existing rows also surface in Applicants.
        try:
            if dup.candidate_id is None:
                cand = find_or_create_candidate_from_resume(db, dup)
                dup.candidate_id = cand.id
            from services.slot_booking import ensure_sourcing_profile
            ensure_sourcing_profile(db, dup, req, ta_user=user)
            db.commit()
        except Exception:
            db.rollback()
        return envelope(serialize_resume(dup), message="Duplicate resume — already on this requirement")

    details = {
        "education": (education or "").strip() or None,
        "technical_domain": (technical_domain or "").strip() or None,
        "skills": (skills or "").strip() or None,
        "notice_period": (notice_period or "").strip() or None,
        "current_ctc": (current_ctc or "").strip() or None,
        "expected_ctc": (expected_ctc or "").strip() or None,
        "preferred_location": (preferred_location or "").strip() or None,
    }
    details = {k: v for k, v in details.items() if v}
    resume = Resume(
        requirement_id=req.id,
        candidate_name=candidate_name.strip(),
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        source_portal=(source_portal or "").strip() or None,
        applicant_experience=(experience or "").strip() or None,
        application_details=details or None,
        resume_file_url=file_url,
        file_sha256=file_sha256,
        file_size=file_size,
    )
    db.add(resume)
    db.flush()  # assign resume.id before deriving the candidate
    # Create/enrich the Candidate now so the applicant appears in the Candidate
    # tab immediately (name/email/phone/CV; apply-form details when present) —
    # and a Sourcing profile so they appear in the APPLICANTS tab too. Every
    # resume is an application; before this, a profile only existed once an AI
    # interview was scheduled, so the Applicants tab silently missed everyone
    # who came in through Resumes.
    try:
        from services.slot_booking import ensure_sourcing_profile, find_or_create_candidate_from_resume
        # Savepoint: this block is best-effort, and on Postgres a failed
        # statement without a savepoint rollback poisons the transaction —
        # the log_activity/commit below would then 500 the whole upload.
        with db.begin_nested():
            cand = find_or_create_candidate_from_resume(db, resume)
            resume.candidate_id = cand.id
        ensure_sourcing_profile(db, resume, req, ta_user=user)
    except Exception:
        logger.warning("candidate bootstrap failed for resume %s", resume.id, exc_info=True)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUME_UPLOADED", f"Resume uploaded for {resume.candidate_name}")
    # Quick ATS on upload (2 Sep 2026, user request): the row arrives scored
    # so TA and RMG see a fit score without a "Scan" click, exactly as the
    # bulk-ZIP path already does. Best-effort in a savepoint — a JD-less
    # requirement or a parser failure must never fail the upload. The
    # auto-threshold pipeline (auto-shortlist / slot invite) is deliberately
    # NOT run here: RMG decides on the Applicants tab.
    try:
        with db.begin_nested():
            run_ats_scan(db, resume, req, user.id)
    except Exception:
        logger.info("auto ATS scan skipped for resume %s", resume.id, exc_info=True)
    if req.status == RequirementStatus.POSTED_ON_PORTALS and existing == 0:
        req.status = RequirementStatus.IN_PROGRESS
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                     "STATUS_CHANGED", "Auto-moved Posted_On_Portals -> In_Progress (first resume received)")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume uploaded")


# ------------------------------------------------------------ parse (quick apply)

@router.post("/api/resumes/parse")
def parse_resume(
    file: UploadFile = File(...),
    opportunity_id: int | None = Form(None),
    db: Session = Depends(get_crm_db),
    # Gated by CANDIDATES (31 Aug 2026): the resume-first New Candidate form
    # uses this too, and its users are everyone who may create candidates —
    # not only requirement-editing TAs. Zero DB writes, so read-shaped risk.
    user: CurrentUser = Depends(gated_write("candidates", "TA", "Sales", "Sales_Head", "RMG")),
):
    """Extract candidate details from a resume WITHOUT creating anything.

    Powers quick apply: TA drops the file first, the upload form prefills
    itself from the response, and a `duplicate` block (when present) shows the
    existing candidate — with their profile on this opportunity if they already
    applied — so the TA decides before a duplicate entry exists.
    """
    from services.crm_common import read_upload_capped, safe_upload_extension
    from services.resume_parse import (
        duplicate_summary, find_duplicate_candidate, find_name_match,
        name_match_summary, parse_resume_bytes,
    )

    import hashlib

    ext = safe_upload_extension(file.filename)
    if ext not in (".pdf", ".docx", ".txt"):
        raise HTTPException(status_code=400,
                            detail=f"Cannot read '{ext}' resumes. Supported: .pdf, .docx, .txt")
    data = read_upload_capped(file)
    sha = hashlib.sha256(data).hexdigest()
    parsed = parse_resume_bytes(data, ext, db=db, sha256=sha)
    dup = find_duplicate_candidate(db, parsed.get("email"), parsed.get("phone"))
    # Soft signal, only when there is no hard match: same full name, different
    # contact details. Informational — it never holds anything.
    name_match = None
    if dup is None:
        nm = find_name_match(db, parsed.get("name"))
        if nm is not None:
            name_match = name_match_summary(nm)
    db.commit()  # persist the parse-cache write (a no-op on a cache hit)
    return envelope(data={
        "parsed": parsed,
        "duplicate": duplicate_summary(db, dup, opportunity_id) if dup is not None else None,
        "name_match": name_match,
    }, message="Resume parsed")


# ------------------------------------------------------------ bulk ZIP upload

#: Bulk caps: each parse is one model call, and one request should not be able
#: to queue hundreds of them.
_BULK_MAX_FILES = 50
_BULK_MAX_MEMBER_BYTES = 10 * 1024 * 1024

#: In-process job store for background ZIP runs. Process-local is correct here:
#: UVICORN_WORKERS must stay 1 (see backend CLAUDE.md §6), the same constraint
#: the interview session dict already relies on. Bounded — see _bulk_jobs_put.
_BULK_JOBS: dict[str, dict] = {}
_BULK_JOBS_LOCK = threading.Lock()
_BULK_JOBS_MAX = 30


def _bulk_jobs_put(job_id: str, patch: dict) -> None:
    with _BULK_JOBS_LOCK:
        job = _BULK_JOBS.setdefault(job_id, {})
        job.update(patch)
        # Evict the oldest FINISHED jobs beyond the cap; running jobs are kept.
        if len(_BULK_JOBS) > _BULK_JOBS_MAX:
            finished = [k for k, v in _BULK_JOBS.items()
                        if v.get("status") in ("done", "error") and k != job_id]
            for k in finished[: len(_BULK_JOBS) - _BULK_JOBS_MAX]:
                _BULK_JOBS.pop(k, None)


def _bulk_jobs_get(job_id: str) -> dict | None:
    with _BULK_JOBS_LOCK:
        job = _BULK_JOBS.get(job_id)
        return dict(job) if job is not None else None


def _run_bulk_zip_job(job_id: str, requirement_id: int, user_id: int,
                      user_name: str, files: list[tuple[str, str, bytes]]) -> None:
    """The worker. Own DB session (we are on a thread, not in a request).

    Progress is written to the job store after every file so the UI can show
    "23 / 50". Every terminal path sets status done/error — a job must never
    hang in "running" forever.
    """
    import hashlib
    import uuid as _uuid
    from types import SimpleNamespace

    from crm_db import get_session_factory
    from services.crm_common import CRM_UPLOAD_DIR, log_activity as _log
    from services.resume_parse import (
        duplicate_summary, find_duplicate_candidate, find_name_match,
        name_match_summary, parse_resume_bytes,
    )
    from services.resumes import run_ats_scan
    from services.slot_booking import (
        ensure_sourcing_profile, find_or_create_candidate_from_resume,
    )

    applied: list[dict] = []
    held: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    db = get_session_factory()()
    try:
        req = db.get(Requirement, requirement_id)
        if req is None:
            _bulk_jobs_put(job_id, {"status": "error", "error": "Requirement not found"})
            return
        ta_user = SimpleNamespace(id=user_id, full_name=user_name, username=user_name)
        target_dir = CRM_UPLOAD_DIR / "resumes"
        target_dir.mkdir(parents=True, exist_ok=True)

        for i, (short, ext, data) in enumerate(files):
            _bulk_jobs_put(job_id, {"done": i, "current": short})
            try:
                sha = hashlib.sha256(data).hexdigest()
                # Dismissed CVs (0087) are hidden rows — they must NOT block a
                # re-upload: uploading the same file again is exactly how a TA
                # retries after dismissing. Only a LIVE identical row skips.
                if db.execute(select(Resume.id).where(
                        Resume.requirement_id == req.id,
                        Resume.file_sha256 == sha,
                        Resume.duplicate_dismissed.is_not(True))).first():
                    skipped.append({"file": short, "reason": "identical file already on this requirement"})
                    continue

                parsed = parse_resume_bytes(data, ext, db=db, sha256=sha)
                name = (parsed.get("name") or "").strip() or short.rsplit(".", 1)[0][:255]
                dup = find_duplicate_candidate(db, parsed.get("email"), parsed.get("phone"))

                # SAVEPOINT per file: a broken resume must cost only its own
                # rows, never the earlier files' uncommitted work.
                with db.begin_nested():
                    stored = f"{_uuid.uuid4().hex}{ext}"  # never trust the zip's filename
                    (target_dir / stored).write_bytes(data)
                    details = {k: v for k, v in {
                        "education": parsed.get("education"),
                        "technical_domain": parsed.get("technical_domain"),
                        "skills": ", ".join(parsed.get("skills") or []) or None,
                        "notice_period": parsed.get("notice_period"),
                        "current_ctc": parsed.get("current_ctc"),
                        "expected_ctc": parsed.get("expected_ctc"),
                        "preferred_location": parsed.get("location"),
                        "current_company": parsed.get("current_company"),
                        "designation": parsed.get("designation"),
                        "linkedin_url": parsed.get("linkedin_url"),
                        "certifications": parsed.get("certifications"),
                        "summary": parsed.get("summary"),
                    }.items() if v}
                    resume = Resume(
                        requirement_id=req.id,
                        candidate_name=name[:255],
                        email=(parsed.get("email") or "").strip() or None,
                        phone=(parsed.get("phone") or "").strip() or None,
                        source_portal="Bulk ZIP",
                        applicant_experience=(parsed.get("experience") or "").strip() or None,
                        application_details=details or None,
                        resume_file_url=f"/api/crm-files/resumes/{stored}",
                        file_sha256=sha,
                        file_size=len(data),
                    )
                    db.add(resume)
                    db.flush()

                    # Auto ATS-scan (improvement #2): the score is ready before
                    # the TA ever sees the row — no "Scan all pending" step.
                    # Non-fatal, in its OWN savepoint: a DB-level failure here
                    # must not poison the file's savepoint and lose the resume.
                    ats_score = None
                    try:
                        with db.begin_nested():
                            scan = run_ats_scan(db, resume, req, user_id)
                        ats_score = scan.get("ats_score") if isinstance(scan, dict) else None
                    except Exception:
                        pass

                    if dup is not None:
                        # HOLD (persisted, improvement #1): the flag lives on
                        # the row, so the review queue survives the dialog.
                        resume.candidate_id = dup.id
                        resume.possible_duplicate_of = dup.id
                        held.append({"file": short, "resume_id": resume.id,
                                     "extracted_name": name, "ats_score": ats_score,
                                     "duplicate": duplicate_summary(db, dup, req.opportunity_id)})
                    else:
                        # Soft signal: same full name as an existing candidate
                        # but different email/phone. Computed BEFORE the create
                        # so it can't match the row we are about to make.
                        name_note = None
                        try:
                            nm = find_name_match(db, name)
                            if nm is not None:
                                name_note = name_match_summary(nm)
                        except Exception:
                            pass
                        cand = find_or_create_candidate_from_resume(db, resume)
                        resume.candidate_id = cand.id
                        profile = ensure_sourcing_profile(db, resume, req, ta_user=ta_user,
                                                          notify_rmg=False)
                        if name_note and name_note.get("candidate_id") == cand.id:
                            name_note = None  # matched by name and REUSED — not a second record
                        applied.append({"file": short, "resume_id": resume.id,
                                        "candidate_id": cand.id, "name": name,
                                        "ats_score": ats_score,
                                        "name_match": name_note,
                                        "profile_id": getattr(profile, "id", None)})
            except Exception:
                logger.warning("bulk zip: file %s failed", short, exc_info=True)
                failed.append({"file": short, "reason": "could not process this file"})

        _log(db, RequirementActivityLog, "requirement_id", req.id, user_id,
             "RESUME_UPLOADED",
             f"Bulk ZIP: {len(applied)} applied, {len(held)} held as possible "
             f"duplicates, {len(skipped)} skipped, {len(failed)} failed")
        # ONE summary notification to RMG for the whole batch — fifty separate
        # bell rows and emails for one zip would be noise, not signal.
        if applied:
            try:
                from services.notify import notify_role
                names = ", ".join(a["name"] for a in applied[:5])
                more = f" and {len(applied) - 5} more" if len(applied) > 5 else ""
                notify_role(
                    db, "RMG",
                    f"{len(applied)} new applicant(s) awaiting RMG screening",
                    f"{names}{more} — applied to '{req.title}' via bulk upload by "
                    f"{user_name}. Review and Shortlist to enable AI L1 interviews.",
                    f"/admin?view=crm&p=requirements/{req.id}&tab=resumes",
                    event="profile.rmg_screening_requested",
                    dedupe_prefix=f"rmg_screen_bulk:{job_id}",
                )
            except Exception:
                pass
        if req.status == RequirementStatus.POSTED_ON_PORTALS and applied:
            req.status = RequirementStatus.IN_PROGRESS
            _log(db, RequirementActivityLog, "requirement_id", req.id, user_id,
                 "STATUS_CHANGED", "Auto-moved Posted_On_Portals -> In_Progress (first resume received)")
        db.commit()

        parts = [f"{len(applied)} applied"]
        if held:
            parts.append(f"{len(held)} possible duplicate(s) held for review")
        if skipped:
            parts.append(f"{len(skipped)} already uploaded")
        if failed:
            parts.append(f"{len(failed)} failed")
        _bulk_jobs_put(job_id, {
            "status": "done", "done": len(files), "current": None,
            "message": ", ".join(parts),
            "result": {"applied": applied, "held": held, "skipped": skipped,
                       "failed": failed, "total": len(files)},
        })
    except Exception:
        logger.exception("bulk zip job %s crashed", job_id)
        db.rollback()
        _bulk_jobs_put(job_id, {"status": "error",
                                "error": "Bulk processing failed — see server logs"})
    finally:
        db.close()


@router.post("/api/requirements/{requirement_id}/resumes/bulk-zip")
def bulk_zip_upload(
    requirement_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """Start a background bulk-resume job from a ZIP; returns a job_id to poll.

    Validation (zip readable, member count, requirement status) happens HERE so
    the TA gets an immediate 400 for a bad upload; the slow part — one AI parse
    per resume, up to 50 — runs on a worker thread. Synchronous processing
    took 1-3 minutes for a full zip, which is proxy-timeout territory and a
    frozen button for the TA. Poll GET /api/resumes/bulk-jobs/{job_id}.
    """
    import io as _io
    import uuid as _uuid
    import zipfile

    from services.crm_common import read_upload_capped

    req = get_requirement_or_404(db, requirement_id)
    if req.status not in _UPLOAD_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Resumes can only be uploaded when the requirement is in "
                   f"{', '.join(s.value for s in _UPLOAD_ALLOWED_STATUSES)} "
                   f"(current: {req.status.value})",
        )
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip file of resumes")
    blob = read_upload_capped(file)
    try:
        zf = zipfile.ZipFile(_io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="That file is not a valid ZIP archive")

    members = [m for m in zf.infolist()
               if not m.is_dir()
               and not m.filename.startswith("__MACOSX")
               and m.filename.rsplit(".", 1)[-1].lower() in ("pdf", "docx", "txt")]
    if not members:
        raise HTTPException(status_code=400,
                            detail="No readable resumes (.pdf/.docx/.txt) found in the ZIP")
    if len(members) > _BULK_MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"Too many resumes in one ZIP (max {_BULK_MAX_FILES}, got {len(members)})")

    # Materialise every member NOW — the zip handle dies with this request.
    files: list[tuple[str, str, bytes]] = []
    oversize: list[dict] = []
    for m in members:
        short = m.filename.rsplit("/", 1)[-1]
        if m.file_size > _BULK_MAX_MEMBER_BYTES:
            oversize.append({"file": short, "reason": "file too large (max 10 MB)"})
            continue
        files.append((short, "." + m.filename.rsplit(".", 1)[-1].lower(), zf.read(m)))

    job_id = _uuid.uuid4().hex
    _bulk_jobs_put(job_id, {
        "status": "running", "total": len(files), "done": 0, "current": None,
        "requirement_id": req.id, "user_id": user.id, "oversize": oversize,
    })
    threading.Thread(
        target=_run_bulk_zip_job,
        args=(job_id, req.id, user.id, user.full_name or user.username, files),
        daemon=True,
        name=f"bulk-zip-{job_id[:8]}",
    ).start()
    return envelope(
        data={"job_id": job_id, "total": len(files), "oversize": oversize},
        message=f"Processing {len(files)} resume(s) in the background",
    )


# ------------------------------------------------- candidates bulk ZIP (talent pool)

def _run_candidate_zip_job(job_id: str, user_id: int, user_name: str,
                           files: list[tuple[str, str, bytes]]) -> None:
    """Worker for the Candidates-tab bulk ZIP (31 Aug 2026, user request).

    Same parse/dedupe engine as the requirement bulk upload, but NO
    requirement and NO application: every resume becomes (or enriches) a
    CANDIDATE in the talent pool. TA applies them to an opportunity later
    from the candidate record.

    Per file, in its OWN savepoint: parse → duplicate check (email OR
    last-10-digit phone) → create candidate + store the CV + fill every field
    the parser found (skills/education/experience via the shared CV applier),
    or enrich the EXISTING candidate's empty fields when it is a duplicate.
    """
    import hashlib
    import uuid as _uuid

    from crm_db import get_session_factory
    from services.candidates import apply_cv_profile_to_candidate
    from services.ctc import parse_ctc_to_rupees
    from services.crm_common import CRM_UPLOAD_DIR
    from services.resume_parse import (
        find_duplicate_candidate, find_name_match, name_match_summary,
        parse_resume_bytes,
    )
    from services.slot_booking import split_candidate_name

    db = get_session_factory()()
    created: list[dict] = []
    enriched: list[dict] = []
    failed: list[dict] = []
    try:
        target_dir = CRM_UPLOAD_DIR / "cv"
        target_dir.mkdir(parents=True, exist_ok=True)

        for i, (short, ext, data) in enumerate(files):
            _bulk_jobs_put(job_id, {"done": i, "current": short})
            try:
                sha = hashlib.sha256(data).hexdigest()
                parsed = parse_resume_bytes(data, ext, db=db, sha256=sha)
                name = (parsed.get("name") or "").strip() or short.rsplit(".", 1)[0][:255]
                email = (parsed.get("email") or "").strip()
                phone = (parsed.get("phone") or "").strip()
                dup = find_duplicate_candidate(db, email, phone)

                with db.begin_nested():
                    stored = f"{_uuid.uuid4().hex}{ext}"   # never trust the zip's filename
                    (target_dir / stored).write_bytes(data)
                    cv_url = f"/api/crm-files/cv/{stored}"

                    # The parser's field names -> the shared CV-applier's shape,
                    # so pool candidates get exactly what an uploaded CV gives.
                    def _num(v):
                        s = str(v or "").replace(",", "").strip()
                        try:
                            return float(s) if s else None
                        except ValueError:
                            return None

                    profile = {
                        "technical_domain": parsed.get("technical_domain"),
                        "linkedin_url": parsed.get("linkedin_url"),
                        "experience_years": _num(parsed.get("experience")),
                        # Unit-aware (2 Sep 2026): a CV writes "12 LPA",
                        # "12,00,000" or "1.2 Cr" for the same salary. Storing
                        # the raw number put wildly different scales in one
                        # rupee column.
                        "current_ctc": parse_ctc_to_rupees(parsed.get("current_ctc")),
                        "expected_ctc": parse_ctc_to_rupees(parsed.get("expected_ctc")),
                        "notice_period": parsed.get("notice_period"),
                        "skills": parsed.get("skills") or [],
                    }

                    if dup is not None:
                        # Known person: never a second record. Fill the gaps and
                        # attach the CV when they had none.
                        if not dup.cv_url:
                            dup.cv_url = cv_url
                        if not dup.phone and phone:
                            dup.phone = phone
                        filled = apply_cv_profile_to_candidate(db, dup, profile)
                        enriched.append({
                            "file": short, "candidate_id": dup.id,
                            "name": " ".join(p for p in [dup.first_name, dup.last_name] if p),
                            "email": dup.email,
                            "filled": len(filled.get("fields") or []) + int(filled.get("skills") or 0),
                        })
                    else:
                        note = None
                        try:
                            nm = find_name_match(db, name)
                            if nm is not None:
                                note = name_match_summary(nm)
                        except Exception:
                            pass
                        first, last = split_candidate_name(name)
                        cand = Candidate(
                            first_name=first or name[:120],
                            last_name=last or None,
                            # candidates.email is NOT NULL + unique — synthesize
                            # a placeholder exactly like the resume path does.
                            email=email.lower() or f"pool-{sha[:12]}@noemail.karnex.local",
                            phone=phone or None,
                            city=(parsed.get("location") or "").strip()[:120] or None,
                            preferred_locations=(parsed.get("location") or "").strip()[:255] or None,
                            cv_url=cv_url,
                        )
                        db.add(cand)
                        db.flush()
                        apply_cv_profile_to_candidate(db, cand, profile)
                        created.append({
                            "file": short, "candidate_id": cand.id, "name": name,
                            "email": cand.email, "name_match": note,
                            "no_email": not email,
                        })
            except Exception:
                logger.warning("candidate zip: file %s failed", short, exc_info=True)
                failed.append({"file": short, "reason": "could not process this file"})

        db.commit()
        parts = [f"{len(created)} candidate(s) added"]
        if enriched:
            parts.append(f"{len(enriched)} already existed (details topped up)")
        if failed:
            parts.append(f"{len(failed)} failed")
        _bulk_jobs_put(job_id, {
            "status": "done", "done": len(files), "current": None,
            "message": " · ".join(parts),
            "result": {"created": created, "enriched": enriched,
                       "failed": failed, "total": len(files)},
        })
    except Exception as exc:
        logger.exception("candidate zip job %s crashed", job_id)
        try:
            db.rollback()
        except Exception:
            pass
        _bulk_jobs_put(job_id, {"status": "error", "error": str(exc) or "Bulk processing failed"})
    finally:
        db.close()


@router.post("/api/candidates/bulk-zip")
def candidates_bulk_zip(
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("candidates", "TA", "Sales", "Sales_Head", "RMG")),
):
    """Add MANY candidates to the talent pool from one ZIP of resumes.

    Validates synchronously (immediate 400s), then runs on a worker thread and
    returns a job_id — poll GET /api/resumes/bulk-jobs/{job_id}. No
    requirement, no application: this fills the Candidates tab only.
    """
    import io as _io
    import uuid as _uuid
    import zipfile

    from services.crm_common import read_upload_capped

    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload a .zip containing the resumes")
    blob = read_upload_capped(file)
    try:
        zf = zipfile.ZipFile(_io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="That file is not a valid ZIP archive")

    members = [m for m in zf.infolist()
               if not m.is_dir()
               and not m.filename.startswith("__MACOSX")
               and m.filename.rsplit(".", 1)[-1].lower() in ("pdf", "docx", "txt")]
    if not members:
        raise HTTPException(status_code=400,
                            detail="No readable resumes (.pdf/.docx/.txt) found in the ZIP")
    if len(members) > _BULK_MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"Too many resumes in one ZIP (max {_BULK_MAX_FILES}, got {len(members)})")

    files: list[tuple[str, str, bytes]] = []
    oversize: list[dict] = []
    for m in members:
        short = m.filename.rsplit("/", 1)[-1]
        if m.file_size > _BULK_MAX_MEMBER_BYTES:
            oversize.append({"file": short, "reason": "file too large (max 10 MB)"})
            continue
        files.append((short, "." + m.filename.rsplit(".", 1)[-1].lower(), zf.read(m)))

    job_id = _uuid.uuid4().hex
    _bulk_jobs_put(job_id, {
        "status": "running", "total": len(files), "done": 0, "current": None,
        "user_id": user.id, "oversize": oversize,
    })
    threading.Thread(
        target=_run_candidate_zip_job,
        args=(job_id, user.id, user.full_name or user.username, files),
        daemon=True,
        name=f"cand-zip-{job_id[:8]}",
    ).start()
    return envelope(
        data={"job_id": job_id, "total": len(files), "oversize": oversize},
        message=f"Processing {len(files)} resume(s) in the background",
    )


@router.get("/api/resumes/bulk-jobs/{job_id}")
def bulk_zip_job_status(
    job_id: str,
    db: Session = Depends(get_crm_db),
    # Serves BOTH bulk jobs (requirement resumes + candidate pool), and only
    # ever returns the caller's OWN job — see the ownership check below.
    user: CurrentUser = Depends(any_crm_role),
):
    """Progress/result for a bulk ZIP job. `status`: running | done | error."""
    job = _bulk_jobs_get(job_id)
    if job is None:
        raise HTTPException(status_code=404,
                            detail="Unknown or expired job (results are kept in memory only)")
    # The uploader's own job (Admin/CEO pass everywhere as usual).
    if job.get("user_id") != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Not your bulk upload")
    out = {k: job.get(k) for k in ("status", "total", "done", "current",
                                   "message", "result", "error", "oversize")}
    return envelope(data=out, message=job.get("message") or job.get("status") or "")


class InterviewInviteIn(BaseModel):
    """TA-reviewed AI-L1 invite email (25 Aug 2026): the frontend prefills the
    standard wording (link + access key + time), the TA edits, THIS sends."""
    subject: str = Field(min_length=3, max_length=300)
    message: str = Field(min_length=10, max_length=8000)


@router.post("/api/resumes/{resume_id}/send-interview-invite")
def send_interview_invite(
    resume_id: int,
    payload: InterviewInviteIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """Send (or re-send) the AI-L1 interview invite to the candidate, with the
    TA's edited wording. Goes through the durable outbox; the TA's identity
    rides as From-name/Reply-To (their own From address when the domain is in
    email.sender_domains)."""
    from services.candidate_comms import send_candidate_email

    resume = _get_resume_or_404(db, resume_id)
    to = (resume.email or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="Resume has no email address on file")
    subject = " ".join(payload.subject.split())  # header-safe: no CR/LF
    res = send_candidate_email(
        to, subject, payload.message.strip(), db=db,
        event="candidate.interview_link", actor=user, to_name=resume.candidate_name,
        candidate_id=resume.candidate_id,
    )
    if not res.get("sent"):
        raise HTTPException(status_code=502,
                            detail=f"Send failed: {res.get('error') or 'unknown error'}")
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "AI_L1_INVITE_SENT", f"AI L1 invite email sent to {resume.candidate_name} ({to})")
    db.commit()
    return envelope(data={"sent": True, "to": to}, message="AI L1 invite sent to the candidate")


@router.post("/api/resumes/{resume_id}/apply-duplicate")
def apply_held_duplicate(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """Resolve a held possible-duplicate: apply the EXISTING candidate to this
    requirement's opportunity and clear the flag. One atomic action, so no
    duplicate record can ever be created from the review queue."""
    from services.slot_booking import ensure_sourcing_profile, profile_from_resume_application

    resume = _get_resume_or_404(db, resume_id)
    dup_id = resume.possible_duplicate_of
    if dup_id is None:
        raise HTTPException(status_code=400, detail="This resume is not held as a possible duplicate")
    req = get_requirement_or_404(db, resume.requirement_id)
    resume.candidate_id = dup_id
    profile = ensure_sourcing_profile(db, resume, req, ta_user=user)
    resume.possible_duplicate_of = None
    # Enrich the EXISTING candidate from the new resume's extracted details —
    # fill-only-empty, so nothing the recruiter already recorded is touched.
    # This is what maps the parsed skills onto CandidateSkill rows (when the
    # candidate has none), which is what the Suggested Candidates matcher reads.
    try:
        from models import Candidate as _Cand
        from services.candidates import apply_cv_profile_to_candidate
        with db.begin_nested():  # savepoint: enrichment is best-effort
            cand = db.get(_Cand, dup_id)
            if cand is not None:
                apply_cv_profile_to_candidate(db, cand, profile_from_resume_application(resume))
    except Exception:
        logger.warning("apply-duplicate enrichment failed for resume %s", resume_id, exc_info=True)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUME_UPLOADED",
                 f"Duplicate hold resolved for {resume.candidate_name}: existing candidate applied")
    db.commit()
    db.refresh(resume)
    data = serialize_resume(resume)
    data["profile_id"] = getattr(profile, "id", None)
    return envelope(data, message="Existing candidate applied to this opportunity")


@router.post("/api/resumes/{resume_id}/dismiss-duplicate")
def dismiss_held_duplicate(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """The other resolution: keep the resume on file but do NOT apply — clears
    the flag so the row stops showing in the review queue."""
    resume = _get_resume_or_404(db, resume_id)
    if resume.possible_duplicate_of is None:
        raise HTTPException(status_code=400, detail="This resume is not held as a possible duplicate")
    resume.possible_duplicate_of = None
    # Persisted (0087): a dismissed CV leaves the requirement's applicant list
    # entirely (default filter) — "kept on file" must not look like "applied".
    resume.duplicate_dismissed = True
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_UPLOADED",
                 f"Duplicate hold dismissed for {resume.candidate_name}: not applied")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Hold dismissed — candidate not applied")


@router.post("/api/resumes/{resume_id}/reparse")
def reparse_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG", "Sales_Head")),
):
    """Fresh AI read of the stored resume file (28 Aug 2026, user request):
    bypasses the parse cache (which may predate newer PARSE_FIELDS and hold a
    thin result) and fills EMPTY detail keys on the resume row. Existing
    values — including TA corrections — are never overwritten."""
    import hashlib as _hashlib

    from services.crm_common import CRM_UPLOAD_DIR
    from services.resume_parse import parse_resume_bytes

    resume = _get_resume_or_404(db, resume_id)
    url = resume.resume_file_url or ""
    prefix = "/api/crm-files/"
    if not url.startswith(prefix):
        raise HTTPException(status_code=400, detail="This resume has no stored file to re-read")
    path = (CRM_UPLOAD_DIR / url[len(prefix):]).resolve()
    if not str(path).startswith(str(CRM_UPLOAD_DIR.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail="Stored resume file not found")
    data = path.read_bytes()
    sha = resume.file_sha256 or _hashlib.sha256(data).hexdigest()
    parsed = parse_resume_bytes(data, path.suffix.lower(), db=db, sha256=sha,
                                use_cache=False)

    def _s(v) -> str:
        return str(v).strip() if v is not None else ""

    if not resume.email and _s(parsed.get("email")):
        resume.email = _s(parsed.get("email"))[:255]
    if not resume.phone and _s(parsed.get("phone")):
        resume.phone = _s(parsed.get("phone"))[:32]
    if not resume.applicant_experience and _s(parsed.get("experience")):
        resume.applicant_experience = _s(parsed.get("experience"))[:64]
    details = dict(resume.application_details or {})
    mapping = {  # details key -> parsed key
        "education": "education", "technical_domain": "technical_domain",
        "notice_period": "notice_period", "current_ctc": "current_ctc",
        "expected_ctc": "expected_ctc", "preferred_location": "location",
        "current_company": "current_company", "designation": "designation",
        "linkedin_url": "linkedin_url", "certifications": "certifications",
        "summary": "summary",
    }
    for dkey, pkey in mapping.items():
        v = _s(parsed.get(pkey))
        if v and not _s(details.get(dkey)):
            details[dkey] = v
    skills = parsed.get("skills")
    if skills and not _s(details.get("skills")):
        details["skills"] = ", ".join(skills) if isinstance(skills, list) else _s(skills)
    resume.application_details = details or None
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume),
                    message="Resume re-read" if parsed.get("text_extracted") is not False
                    else "The file has no readable text — fill the details manually")


@router.get("/api/resumes/{resume_id}/review")
def resume_review(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG", "Sales_Head")),
):
    """Side-by-side verify payload (28 Aug 2026, user request): after a bulk
    ZIP upload the TA steps through each applied resume — the FILE on one
    side, the candidate record the parser created on the other — and confirms
    or corrects before moving on. One call returns both halves; edits go
    through the normal PUT /api/candidates/{id}."""
    from services.candidates import candidate_detail

    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    cand = db.get(Candidate, resume.candidate_id) if resume.candidate_id else None
    return envelope(data={
        "resume": serialize_resume(resume),
        "candidate": candidate_detail(db, cand) if cand is not None else None,
    })


@router.get("/api/requirements/{requirement_id}/resumes")
def list_resumes(
    requirement_id: int,
    ats_status: str | None = None,
    ai_interview_status: str | None = None,
    applied_by: str | None = None,
    dismissed: bool = False,
    stage: str | None = None,
    p: PageParams = Depends(page_params),
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG", "Sales_Head")),
):
    req = get_requirement_or_404(db, requirement_id)
    stmt = select(Resume).where(Resume.requirement_id == req.id)
    # Dismissed duplicates are hidden by default (0087); ?dismissed=1 lists
    # ONLY them, so the decision stays reviewable and reversible by re-upload.
    if dismissed:
        stmt = stmt.where(Resume.duplicate_dismissed.is_(True))
    else:
        stmt = stmt.where(or_(Resume.duplicate_dismissed.is_(None),
                              Resume.duplicate_dismissed.is_(False)))
    # Stage pills (28 Aug 2026, user request): filter by the candidate's LIVE
    # pipeline stage on this opportunity. CSV so one pill can cover several
    # statuses ("Customer Interviewing" = interview + feedback + shortlist).
    # Server-side — the list is paginated, a client filter would miss pages.
    wanted_stages: list[PipelineStatus] = []
    if stage:
        for s in stage.split(","):
            s = s.strip()
            if not s:
                continue
            try:
                wanted_stages.append(PipelineStatus(s))
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Invalid stage '{s}'")
        if wanted_stages:
            stmt = (
                stmt.join(CandidateProfile,
                          (CandidateProfile.candidate_id == Resume.candidate_id)
                          & (CandidateProfile.opportunity_id == req.opportunity_id))
                .where(CandidateProfile.pipeline_status.in_(wanted_stages))
            )
    if ats_status:
        if ats_status not in ATS_STATUS_VALUES:
            raise HTTPException(status_code=400, detail=f"Invalid ats_status filter '{ats_status}'")
        stmt = stmt.where(Resume.ats_status == AtsStatus(ats_status))
    if ai_interview_status:
        if ai_interview_status not in AI_INTERVIEW_STATUS_VALUES:
            raise HTTPException(
                status_code=400, detail=f"Invalid ai_interview_status filter '{ai_interview_status}'"
            )
        stmt = stmt.where(Resume.ai_interview_status == AiInterviewStatus(ai_interview_status))
    if applied_by:
        # "Show only what <TA> submitted" — attribution lives on the linked
        # candidate profile (ta_owner_name), stamped when the resume created
        # its Sourcing profile. Filter must be SERVER-side: the list is
        # paginated, so a client-side filter would silently miss other pages.
        stmt = stmt.where(Resume.candidate_id.in_(
            select(CandidateProfile.candidate_id).where(
                CandidateProfile.opportunity_id == req.opportunity_id,
                CandidateProfile.ta_owner_name == applied_by.strip(),
            )
        ))
    if p.search:
        like = f"%{p.search}%"
        stmt = stmt.where(or_(Resume.candidate_name.ilike(like), Resume.email.ilike(like),
                              Resume.phone.ilike(like)))
    stmt = stmt.order_by(Resume.created_at.desc(), Resume.id.desc())

    # RMG-CLEARED CANDIDATES WITH NO RESUME ROW (user decision, 28 Aug 2026).
    # A candidate added from the Candidates page and applied to the opportunity
    # has a profile but no resume, so this tab — where every interview is
    # scheduled — could never see them. They belong in the SAME list, so they
    # are merged into the ordering and counted in the total (1 Sep 2026): they
    # used to be appended to page 1 only, which is why the header said
    # "4 resumes" over a table showing 6 rows.
    extra = _profile_only_applied_rows(
        db, req, search=p.search, applied_by=applied_by,
        stages=wanted_stages, dismissed=dismissed,
    )
    items, extra_page, order, meta = _paginate_merged(db, stmt, extra, p.page, p.limit)
    data = enrich_resumes_with_ai(db, items)
    # Attach the linked profile's attribution + RMG screening state — batched,
    # one query for the whole page. Powers the Applied-by column and the RMG
    # screening badge/decisions on this tab.
    cand_ids = [r.candidate_id for r in items if r.candidate_id]
    prof: dict[int, tuple] = {}
    if cand_ids:
        for cid, owner, pid, screening, stage, budget in db.execute(
            select(CandidateProfile.candidate_id, CandidateProfile.ta_owner_name,
                   CandidateProfile.id, CandidateProfile.rmg_screening_status,
                   CandidateProfile.pipeline_status, CandidateProfile.budget_status).where(
                CandidateProfile.opportunity_id == req.opportunity_id,
                CandidateProfile.candidate_id.in_(cand_ids),
            )
        ).all():
            prof[cid] = (owner, pid, screening, getattr(stage, "value", stage), budget)
    # Manual L1 / L2 round state for EVERY row that has a profile — not only
    # the ones with an AI link (1 Sep 2026). A candidate RMG took down the
    # manual route never has a link, and keying this off one left their row
    # with no buttons at all.
    from services.resumes import manual_round_state
    rounds = manual_round_state(db, [p[1] for p in prof.values() if p[1]])
    for row, item in zip(data, items):
        owner, pid, screening, stage, budget = (
            prof.get(item.candidate_id, (None, None, None, None, None))
            if item.candidate_id else (None, None, None, None, None))
        row["applied_by"] = owner
        row["rmg_screening_status"] = screening
        # profile_id may already be set by the AI-link enrichment; the applied
        # profile is the same record, so only fill the gap.
        if row.get("profile_id") is None:
            row["profile_id"] = pid
        if row.get("profile_pipeline_status") is None:
            row["profile_pipeline_status"] = stage
        row["budget_status"] = budget   # the Pre-Onboarding budget hold (3 Sep 2026)
        if pid:
            row.update(rounds.get(pid, {}))

    # Interleave the profile-only rows back into their date position — the two
    # kinds of row are one list to the recruiter, so "newest first" has to hold
    # across both.
    by_resume = {item.id: row for item, row in zip(items, data)}
    by_profile = {row["id"]: row for row in extra_page}
    merged = [(by_resume if kind == "r" else by_profile).get(key) for kind, key in order]
    return envelope([row for row in merged if row is not None], meta=meta)


def _naive_utc(dt: datetime | None) -> datetime:
    """Comparable sort key across tz-aware (Postgres) and naive (SQLite) rows."""
    if dt is None:
        return datetime.min
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _paginate_merged(db: Session, stmt, extra: list[dict], page: int, limit: int):
    """Page over resumes + profile-only rows as ONE newest-first list.

    Returns (resume objects on this page, profile-only rows on this page, the
    page's ("r"|"p", key) order, meta) — the order comes back with the data
    rather than living in module state, so concurrent requests can't cross.

    Only the id + timestamp of every matching resume is read to build the
    ordering; the full rows (and their AI enrichment) are loaded for the page
    alone, so the extra query stays cheap on a requirement with many CVs.
    """
    if not extra:
        items, meta = paginate(db, stmt, page, limit)
        return items, [], [("r", r.id) for r in items], meta

    keys = db.execute(
        stmt.with_only_columns(Resume.id, Resume.created_at, maintain_column_froms=True)
    ).all()
    ordered = [("r", rid, _naive_utc(created)) for rid, created in keys]
    ordered += [("p", row["id"], _naive_utc(row.pop("_sort_at", None))) for row in extra]
    # id descending as the tiebreaker, so rows created in the same second (a
    # bulk ZIP) keep a stable, newest-first order instead of shuffling per page.
    ordered.sort(key=lambda t: (t[2], abs(t[1])), reverse=True)

    total = len(ordered)
    pages = (total + limit - 1) // limit if limit else 1
    start = max(page - 1, 0) * limit
    window = ordered[start:start + limit]

    wanted = [key for kind, key, _ in window if kind == "r"]
    by_id = {}
    if wanted:
        by_id = {r.id: r for r in db.execute(
            select(Resume).where(Resume.id.in_(wanted))).scalars().all()}
    items = [by_id[key] for kind, key, _ in window if kind == "r" and key in by_id]
    extra_by_id = {row["id"]: row for row in extra}
    extra_page = [extra_by_id[key] for kind, key, _ in window if kind == "p"]
    order = [(kind, key) for kind, key, _ in window]
    return items, extra_page, order, {"page": page, "limit": limit,
                                      "total": total, "pages": pages}


def _profile_only_applied_rows(db: Session, req, *, search: str | None,
                               applied_by: str | None,
                               stages: list | None = None,
                               dismissed: bool = False) -> list[dict]:
    """RMG-shortlisted profiles on this opportunity that have no resume row.

    Filtered by the same search / applied-by / stage the caller asked for, and
    empty when the caller wants dismissed CVs only (a profile-only row is not
    a CV and can never have been dismissed).
    """
    if dismissed:
        return []
    from services.candidate_profiles import RMG_SCREENING_SHORTLISTED
    try:
        rows = db.execute(
            select(CandidateProfile, Candidate)
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .where(
                CandidateProfile.opportunity_id == req.opportunity_id,
                CandidateProfile.rmg_screening_status == RMG_SCREENING_SHORTLISTED,
                ~CandidateProfile.candidate_id.in_(
                    select(Resume.candidate_id).where(
                        Resume.requirement_id == req.id,
                        Resume.candidate_id.isnot(None),
                    )
                ),
            )
            .order_by(CandidateProfile.id.desc())
        ).all()
    except Exception:  # pragma: no cover — never break the main list
        logger.warning("profile-only applied rows failed for requirement %s", req.id, exc_info=True)
        return []

    # The AI L1 for a profile-only candidate is linked by PROFILE, not resume
    # (there is no resume) — so the resume-keyed enrichment misses it entirely
    # and the row read "Not Scheduled" forever, hiding the L2 button with it
    # (user report, 28 Aug 2026). Same fields, sourced per profile.
    ai_by_profile = _ai_state_by_profile(db, [p.id for p, _ in rows])
    # A PASSED AI L1 hands the candidate to RMG — the stage the Schedule L2
    # button keys off. The resume path already self-heals this; profile-only
    # rows were stuck at Technical_Screening with no way forward.
    try:
        healed = False
        for profile, _cand in rows:
            state = ai_by_profile.get(profile.id) or {}
            passed = (state.get("ai_effective_result") or state.get("ai_interview_result")) == "Passed"
            here = getattr(profile.pipeline_status, "value", profile.pipeline_status)
            if passed and here in (PipelineStatus.SOURCING.value,
                                   PipelineStatus.TECHNICAL_SCREENING.value):
                profile.pipeline_status = PipelineStatus.RMG_REVIEW
                log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, None,
                             "STATUS_CHANGE",
                             f"{here} -> RMG_Review: AI L1 passed — "
                             "auto-forwarded for RMG review")
                healed = True
        if healed:
            db.commit()
    except Exception:
        db.rollback()
        logger.warning("profile-only RMG hand-off heal failed", exc_info=True)

    needle = (search or "").strip().lower()
    want_ta = (applied_by or "").strip()
    # Stage pills apply here too — filtered in Python, and AFTER the heal above,
    # so a candidate the heal just moved to RMG Review answers the pill the
    # recruiter is actually looking at.
    want_stages = {getattr(s, "value", s) for s in (stages or [])}
    out: list[dict] = []
    for profile, cand in rows:
        name = " ".join(p for p in [cand.first_name, cand.last_name] if p) or f"Candidate #{cand.id}"
        email = (cand.email or "").strip()
        if needle and needle not in f"{name} {email} {cand.phone or ''}".lower():
            continue
        if want_ta and (profile.ta_owner_name or "") != want_ta:
            continue
        here = getattr(profile.pipeline_status, "value", profile.pipeline_status)
        if want_stages and here not in want_stages:
            continue
        out.append({
            # Sort key for the merged page — popped before the row is returned.
            "_sort_at": profile.applied_on or profile.created_at,
            # NEGATIVE id: the row still needs a stable key for the table, and
            # a negative one can never collide with a real resume id — while
            # making "this is not a resume" obvious to any caller.
            "id": -profile.id,
            "profile_id": profile.id,
            "candidate_id": cand.id,
            "requirement_id": req.id,
            "is_profile_only": True,
            "candidate_name": name,
            "email": email,
            "phone": cand.phone,
            "source_portal": profile.source,
            "applicant_experience": (str(cand.experience_years) if cand.experience_years is not None else None),
            "resume_file_url": cand.cv_url,
            "ats_score": None,
            "ats_status": None,
            "ai_interview_status": None,
            "applied_by": profile.ta_owner_name,
            "rmg_screening_status": profile.rmg_screening_status,
            "profile_pipeline_status": getattr(profile.pipeline_status, "value", profile.pipeline_status),
            "budget_status": getattr(profile, "budget_status", None),
            "received_date": profile.applied_on.isoformat() if profile.applied_on else None,
            "created_at": profile.created_at.isoformat() if profile.created_at else None,
            **ai_by_profile.get(profile.id, {}),
        })
    return out


def _ai_state_by_profile(db: Session, profile_ids: list[int]) -> dict[int, dict]:
    """AI L1 + L2 state for profiles, keyed by profile id.

    The resume-based enrichment finds an interview through `resume_id`; a
    candidate applied from the Candidates page has none, so the same facts are
    gathered here through `profile_id`. Field names match exactly — the row
    renders through the same UI. Best-effort: an enrichment failure must not
    remove the candidate from the list.
    """
    if not profile_ids:
        return {}
    try:
        import os
        from models import AiInterviewLink, CandidateProfileActivityLog, InterviewEvent
        from models.ai_links import hr_decision_label

        latest: dict[int, AiInterviewLink] = {}
        for link in db.execute(
            select(AiInterviewLink)
            .where(AiInterviewLink.profile_id.in_(profile_ids))
            .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
        ).scalars().all():
            if link.profile_id not in latest:
                latest[link.profile_id] = link

        # One source of truth for the human rounds — the same helper the
        # resume rows use, so both kinds of row can never disagree.
        from services.resumes import manual_round_state
        rounds = manual_round_state(db, profile_ids)

        emails: dict[int, str] = {}
        cand_ids = {link.candidate_id for link in latest.values() if link.candidate_id}
        if cand_ids:
            for c in db.execute(select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all():
                if c.email:
                    emails[c.id] = c.email.strip().lower()
        base = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")

        out: dict[int, dict] = {}
        for pid in profile_ids:
            link = latest.get(pid)
            d: dict = dict(rounds.get(pid, {}))
            if link is not None:
                email = emails.get(link.candidate_id, "")
                token = (link.invite_token or "").strip()
                d.update({
                    "ai_interview_status": link.result,
                    "ai_interview_result": link.result,
                    "ai_overall_score_percent": (float(link.overall_score_percent)
                                                 if link.overall_score_percent is not None else None),
                    "ai_hr_decision": link.hr_decision,
                    "ai_hr_decision_label": hr_decision_label(link.hr_decision),
                    "ai_effective_result": link.effective_result,
                    "ai_is_overridden": bool(link.hr_decision) and link.effective_result != link.result,
                    "ai_interview_record_id": link.interview_record_id,
                    "ai_report_link": (
                        f"/admin?view=candidateReport&cid={email}&iid={link.interview_record_id}"
                        if link.interview_record_id and email else None
                    ),
                    "ai_interview_scheduled_at": link.created_at.isoformat() if link.created_at else None,
                })
                if token:
                    d["ai_invite_token"] = token
                    d["ai_invite_url"] = f"{base}/?invite={token}" if base else f"/?invite={token}"
            out[pid] = d
        return out
    except Exception:  # pragma: no cover — enrichment is never worth a 500
        logger.warning("profile-only AI enrichment failed", exc_info=True)
        return {}


# --------------------------------------------------------------- edit + delete

class ResumeUpdateIn(BaseModel):
    """Editable applicant details on a resume row (all optional; None = untouched)."""
    candidate_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    source_portal: str | None = Field(default=None, max_length=64)
    experience: str | None = Field(default=None, max_length=64)
    education: str | None = Field(default=None, max_length=120)
    technical_domain: str | None = Field(default=None, max_length=120)
    skills: str | None = Field(default=None, max_length=500)
    notice_period: str | None = Field(default=None, max_length=60)
    current_ctc: str | None = Field(default=None, max_length=40)
    expected_ctc: str | None = Field(default=None, max_length=40)
    preferred_location: str | None = Field(default=None, max_length=120)
    current_company: str | None = Field(default=None, max_length=160)
    designation: str | None = Field(default=None, max_length=160)
    linkedin_url: str | None = Field(default=None, max_length=255)
    certifications: str | None = Field(default=None, max_length=400)
    summary: str | None = Field(default=None, max_length=500)

_DETAIL_KEYS = ("education", "technical_domain", "skills", "notice_period",
                "current_ctc", "expected_ctc", "preferred_location",
                "current_company", "designation", "linkedin_url",
                "certifications", "summary")


@router.put("/api/resumes/{resume_id}")
def update_resume(
    resume_id: int,
    payload: ResumeUpdateIn,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="No fields to update")
    if "candidate_name" in changes and changes["candidate_name"]:
        resume.candidate_name = changes["candidate_name"].strip()
    if "email" in changes:
        resume.email = (changes["email"] or "").strip() or None
    if "phone" in changes:
        resume.phone = (changes["phone"] or "").strip() or None
    if "source_portal" in changes:
        resume.source_portal = (changes["source_portal"] or "").strip() or None
    if "experience" in changes:
        resume.applicant_experience = (changes["experience"] or "").strip() or None
    details = dict(resume.application_details or {})
    detail_changed = False
    for k in _DETAIL_KEYS:
        if k in changes:
            v = (changes[k] or "").strip()
            if v:
                details[k] = v
            else:
                details.pop(k, None)
            detail_changed = True
    if detail_changed:
        resume.application_details = details or None
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_UPDATED", f"Resume details updated for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume updated")


@router.delete("/api/resumes/{resume_id}")
def delete_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """Delete a resume/application. Unlinks AI-interview rows and removes slot
    bookings for this resume; the Candidate record (if created) is kept."""
    from models import AiInterviewLink, SlotBooking

    resume = _get_resume_or_404(db, resume_id)
    name = resume.candidate_name
    req_id = resume.requirement_id
    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.resume_id == resume.id)
    ).scalars().all():
        link.resume_id = None
    for booking in db.execute(
        select(SlotBooking).where(SlotBooking.resume_id == resume.id)
    ).scalars().all():
        db.delete(booking)
    db.delete(resume)
    log_activity(db, RequirementActivityLog, "requirement_id", req_id, user.id,
                 "RESUME_DELETED", f"Resume deleted for {name}")
    db.commit()
    return envelope(data={"id": resume_id}, message="Resume deleted")


# ---------------------------------------------------------------- ATS scanning

@router.post("/api/resumes/{resume_id}/ats-scan")
def ats_scan(
    resume_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    # RMG runs the same deterministic scan while screening (25 Aug 2026): the
    # score is shared — what differs between the roles is the decision.
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG")),
):
    resume = _get_resume_or_404(db, resume_id)
    req = get_requirement_or_404(db, resume.requirement_id)
    result = run_ats_scan(db, resume, req, user.id)
    # Auto-threshold pipeline (auto-shortlist + slot invite) — never raises.
    auto = auto_pipeline_after_scan(db, resume, req, user.id, _base_url(request))
    db.commit()
    db.refresh(resume)
    data = serialize_resume(resume)
    data["auto_shortlisted"] = bool(auto.get("auto_shortlisted"))
    data["slot_invite_sent"] = bool(auto.get("slot_invite_sent"))
    data["auto_action"] = auto
    return envelope(data, message=f"ATS scan complete: {result['ats_score']}/100")


def ensure_resume_for_profile(db: Session, profile: CandidateProfile, req) -> Resume:
    """The Resume row a profile-only applicant needs before anything CV-shaped
    can happen to them — built from the CV on the candidate's own record.

    A candidate applied from the Candidates page (or "Apply to Opportunity")
    has a profile but no `resumes` row, so ATS, slot invites and the AI L1
    all had nothing to work on (user report, 2 Sep 2026). Once this row
    exists the applicant is an ordinary resume row everywhere. Idempotent:
    an existing (requirement, candidate) row is reused, never duplicated.
    """
    existing = db.execute(
        select(Resume).where(Resume.requirement_id == req.id,
                             Resume.candidate_id == profile.candidate_id)
        .order_by(Resume.id.desc())
    ).scalars().first()
    if existing is not None:
        return existing
    cand = db.get(Candidate, profile.candidate_id)
    if cand is None:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not (cand.cv_url or "").strip():
        raise HTTPException(
            status_code=422,
            detail="No CV on file for this candidate — upload one on their record "
                   "(Candidates → their profile → CV), then run the ATS scan.")
    resume = Resume(
        requirement_id=req.id,
        candidate_id=cand.id,
        candidate_name=(" ".join(p for p in (cand.first_name, cand.last_name) if p)
                        or f"Candidate #{cand.id}")[:255],
        email=cand.email,
        phone=cand.phone,
        source_portal=(profile.source or "app")[:64],
        applicant_experience=(str(cand.experience_years)
                              if cand.experience_years is not None else None),
        resume_file_url=cand.cv_url,
    )
    # Stamp the application date the profile carries, not today — the row is
    # catching up with an application that already happened. Left unset (so
    # the column's server default applies) when the profile has no date.
    applied = profile.applied_on or profile.created_at
    if applied is not None:
        resume.received_date = applied.date()
    db.add(resume)
    db.flush()
    return resume


def _requirement_for_profile(db: Session, profile, requirement_id: int | None):
    """The requirement to score against: the one named, else the opportunity's
    own (latest) — the Applicants tab works at opportunity level and has no
    requirement id to hand (2 Sep 2026)."""
    if requirement_id is not None:
        req = get_requirement_or_404(db, requirement_id)
        if req.opportunity_id != profile.opportunity_id:
            raise HTTPException(status_code=400,
                                detail="That requirement belongs to a different opportunity")
        return req
    req = db.execute(
        select(Requirement).where(Requirement.opportunity_id == profile.opportunity_id)
        .order_by(Requirement.id.desc())
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=400,
                            detail="This opportunity has no requirement yet — nothing to score against")
    return req


@router.post("/api/candidate-profiles/{profile_id}/ats-scan")
def ats_scan_profile(
    profile_id: int,
    request: Request,
    requirement_id: int | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG")),
):
    """ATS-scan a profile-only applicant against a requirement (TA or RMG).

    Materialises the resume row from the candidate's CV first, then runs the
    same scan as `POST /api/resumes/{id}/ats-scan` — same score, same
    auto-threshold pipeline. The row comes back as a normal resume row, so the
    Applied Candidates tab shows every standard action from the next load.
    """
    from services.candidate_profiles import get_profile_or_404
    profile = get_profile_or_404(db, profile_id)
    req = _requirement_for_profile(db, profile, requirement_id)
    resume = ensure_resume_for_profile(db, profile, req)
    result = run_ats_scan(db, resume, req, user.id)
    auto = auto_pipeline_after_scan(db, resume, req, user.id, _base_url(request))
    db.commit()
    db.refresh(resume)
    data = serialize_resume(resume)
    data["auto_shortlisted"] = bool(auto.get("auto_shortlisted"))
    data["slot_invite_sent"] = bool(auto.get("slot_invite_sent"))
    data["auto_action"] = auto
    return envelope(data, message=f"ATS scan complete: {result['ats_score']}/100")


@router.post("/api/requirements/{requirement_id}/resumes/scan-all")
def scan_all_resumes(
    requirement_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG")),
):
    req = get_requirement_or_404(db, requirement_id)
    base_url = _base_url(request)
    pending = db.execute(
        select(Resume)
        .where(Resume.requirement_id == req.id, Resume.ats_status == AtsStatus.PENDING_SCAN)
        .order_by(Resume.id.asc())
    ).scalars().all()
    results: list[dict] = []
    scored = failed = 0
    for resume in pending:
        try:
            outcome = run_ats_scan(db, resume, req, user.id)
            # Auto-threshold pipeline (auto-shortlist + slot invite) — never raises.
            auto = auto_pipeline_after_scan(db, resume, req, user.id, base_url)
            scored += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Scored", ats_score=outcome["ats_score"],
                auto_shortlisted=bool(auto.get("auto_shortlisted")),
                slot_invite_sent=bool(auto.get("slot_invite_sent")),
            ).model_dump())
        except HTTPException as exc:
            failed += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Failed", error=str(exc.detail),
            ).model_dump())
        except Exception as exc:  # keep going on unexpected per-file errors
            failed += 1
            results.append(ResumeScanResult(
                resume_id=resume.id, candidate_name=resume.candidate_name,
                status="Failed", error=f"Unexpected error: {exc}",
            ).model_dump())
    db.commit()
    return envelope(
        {"total_pending": len(pending), "scored": scored, "failed": failed, "results": results},
        message=f"Scanned {scored} of {len(pending)} pending resume(s); {failed} failed",
    )


# ---------------------------------------------------------------- shortlist / reject

@router.post("/api/resumes/{resume_id}/shortlist")
def shortlist_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    if resume.ats_score is None:
        raise HTTPException(status_code=400, detail="Resume must be ATS-scanned before shortlisting")
    resume.ats_status = AtsStatus.SHORTLISTED
    resume.screened_by = resume.screened_by or user.id
    # Legacy resumes (uploaded before profiles were created at upload time) get
    # their Applicants-tab row here, the first time TA acts on them.
    try:
        from services.slot_booking import ensure_sourcing_profile, find_or_create_candidate_from_resume
        if resume.candidate_id is None:
            with db.begin_nested():  # savepoint: best-effort must not poison the txn
                resume.candidate_id = find_or_create_candidate_from_resume(db, resume).id
        ensure_sourcing_profile(db, resume, get_requirement_or_404(db, resume.requirement_id),
                                ta_user=user)
    except Exception:
        logger.warning("shortlist bootstrap failed for resume %s", resume.id, exc_info=True)
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_SHORTLISTED", f"Resume shortlisted for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume shortlisted")


@router.post("/api/resumes/{resume_id}/reject")
def reject_resume(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    resume = _get_resume_or_404(db, resume_id)
    resume.ats_status = AtsStatus.REJECTED
    resume.screened_by = resume.screened_by or user.id
    # Keep the Applicants tab honest: an ATS-rejected resume whose applicant is
    # still sitting at Sourcing becomes Rejected there too. ONLY from Sourcing —
    # anyone already moved along the pipeline was advanced deliberately, and a
    # resume decision must not clobber a pipeline decision.
    try:
        if resume.candidate_id is not None:
            with db.begin_nested():  # savepoint: best-effort must not poison the txn
                req = get_requirement_or_404(db, resume.requirement_id)
                profile = db.execute(
                    select(CandidateProfile).where(
                        CandidateProfile.candidate_id == resume.candidate_id,
                        CandidateProfile.opportunity_id == req.opportunity_id,
                    )
                ).scalars().first()
                cur = getattr(profile.pipeline_status, "value", profile.pipeline_status) if profile else None
                if profile is not None and cur == PipelineStatus.SOURCING.value:
                    profile.pipeline_status = PipelineStatus.REJECTED
                    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                                 "STATUS_CHANGE",
                                 "Sourcing -> Rejected: resume rejected in ATS screening")
    except Exception:
        logger.warning("reject cascade failed for resume %s", resume.id, exc_info=True)
    log_activity(db, RequirementActivityLog, "requirement_id", resume.requirement_id, user.id,
                 "RESUME_REJECTED", f"Resume rejected for {resume.candidate_name}")
    db.commit()
    db.refresh(resume)
    return envelope(serialize_resume(resume), message="Resume rejected")


# ---------------------------------------------------------------- AI L1 scheduling
# Candidate/profile find-or-create now lives in services/slot_booking.py — it is
# shared with the public slot-confirmation flow (routers/crm/slots.py).

@router.post("/api/resumes/{resume_id}/schedule-ai-interview")
def schedule_ai_interview(
    resume_id: int,
    db: Session = Depends(get_crm_db),
    # RMG added 2 Sep 2026 — RMG picks the route (AI vs manual L1) from the row.
    user: CurrentUser = Depends(gated_write("requirements", "TA", "RMG")),
):
    resume = _get_resume_or_404(db, resume_id)
    if resume.ats_status != AtsStatus.SHORTLISTED:
        raise HTTPException(
            status_code=400,
            detail=f"Only Shortlisted resumes can be scheduled for AI interview "
                   f"(current ats_status: {resume.ats_status.value})",
        )
    req = get_requirement_or_404(db, resume.requirement_id)
    from services.requirements import ensure_not_on_hold
    ensure_not_on_hold(req)

    candidate = find_or_create_candidate_from_resume(db, resume)
    profile = get_or_create_profile(db, candidate, req)

    # RMG screening gate (25 Aug 2026): a Pending/Rejected applicant cannot be
    # sent into the AI L1 — RMG must Shortlist first. Server-side on purpose.
    from services.candidate_profiles import rmg_screening_blocks_l1
    blocked = rmg_screening_blocks_l1(profile)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)

    bridge = schedule_l1_interview(db, candidate, req, profile, resume=resume, scheduled_by=user.id)
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502, detail=f"AI interview scheduling failed: {bridge.get('error')}")

    resume.candidate_id = candidate.id
    resume.ai_interview_status = AiInterviewStatus.SCHEDULED
    resume.ai_interview_scheduled_at = _now()
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "AI_L1_SCHEDULED", f"AI_L1_SCHEDULED for {resume.candidate_name}")

    # Best-effort notify only when AI_INTERVIEW_AUTOSEND is enabled (default: off).
    notified = {"email": False, "whatsapp": False}
    if ai_interview_autosend_enabled():
        # Candidate-facing time in IST — same rendering as the slot pages.
        from routers.crm.slots import _when_text as _slot_when_text
        when_text = _slot_when_text(resume.ai_interview_scheduled_at)
        msg = interview_link_message(resume.candidate_name, req.title, when_text,
                                     bridge.get("invite_url", ""), bridge.get("access_key", ""))
        notified = notify_candidate(resume.email, resume.phone, msg["subject"], msg["text"], msg["html"],
                                    db=db, event="candidate.interview_link", actor=user,
                                    to_name=resume.candidate_name,
                                    candidate_id=resume.candidate_id)

    db.commit()
    db.refresh(resume)
    return envelope(
        {
            "resume_id": resume.id,
            "candidate_id": candidate.id,
            "profile_id": profile.id,
            "candidate_name": resume.candidate_name,
            "candidate_email": resume.email,
            "ai_interview_status": resume.ai_interview_status.value,
            "scheduled": bool(bridge.get("scheduled")),
            "session_ref": bridge.get("session_ref"),
            "invite_url": bridge.get("invite_url", ""),
            "access_key": bridge.get("access_key", ""),
            "job_id": bridge.get("job_id", ""),
            "notified": notified,
            "autosend": ai_interview_autosend_enabled(),
            "resume": serialize_resume(resume),
        },
        message=(
            f"AI L1 interview ready for {resume.candidate_name} — copy the invite link to share"
            if not ai_interview_autosend_enabled()
            else f"AI L1 interview scheduled for {resume.candidate_name}"
        ),
    )
