"""Karnex Careers — the public page's API + RMG's careers-link card (3 Nov 2026).

PUBLIC (no login; the page is `frontend/careers.html`, served at `/careers`):

    GET  /api/public/careers                          every listed position + location counts
    GET  /api/public/careers/positions/{id}           one position with its RMG JD and skills
    POST /api/public/careers/positions/{id}/apply     apply (multipart, résumé ≤ 10 MB)
    POST /api/public/careers/general-apply            "Didn't find your role?" — joins the talent pool

STAFF:

    GET  /api/careers/overview                        the link, the post, every live position
    PUT  /api/careers/positions/{requirement_id}      {listed} — RMG / GM / Sales Head switch

What is listed, what the JD is and how an application lands: services/careers.py.
The customer is never named on the public side.
"""
from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

import rate_limit as _rl
from crm_deps import CurrentUser, gated_read, gated_write, get_crm_db, screener_or
from models import Requirement
from schemas.common import envelope
from services import careers
from services.crm_common import save_upload_hashed

router = APIRouter(tags=["Public: Careers"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^[+()\-\s\d]{7,20}$")

CAREERS_READ = screener_or(gated_read("requirements", "TA", "RMG", "Sales", "Sales_Head"))
CAREERS_WRITE = screener_or(gated_write("requirements", "RMG", "Sales_Head"))


# ------------------------------------------------------------------ public

@router.get("/api/public/careers")
@_rl.limit("120/minute")
def public_listing(request: Request, db: Session = Depends(get_crm_db)):
    return envelope(data=careers.careers_listing(db))


@router.get("/api/public/careers/positions/{requirement_id}")
@_rl.limit("120/minute")
def public_position(request: Request, requirement_id: int, db: Session = Depends(get_crm_db)):
    item = careers.careers_position(db, requirement_id)
    if item is None:
        raise HTTPException(status_code=404, detail="This position is no longer open. "
                                                    "Have a look at the other openings.")
    return envelope(data=item)


def _check_person(name: str, email: str, phone: str) -> tuple[str, str, str]:
    name, email, phone = (name or "").strip(), (email or "").strip(), (phone or "").strip()
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Please enter your full name.")
    if not _EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if not _PHONE_RE.match(phone):
        raise HTTPException(status_code=400, detail="Please enter a valid mobile number.")
    return name, email.lower(), phone


def _save_resume(file: UploadFile) -> tuple[str, str, int]:
    filename = (file.filename or "").lower()
    if not any(filename.endswith(ext) for ext in careers.ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="Your résumé must be a PDF or Word file.")
    url, sha, size = save_upload_hashed(file, "resumes")
    if int(size or 0) > careers.MAX_RESUME_BYTES:
        raise HTTPException(status_code=400, detail="Your résumé is larger than 10 MB.")
    return url, sha, size


def _details(**kw) -> dict:
    return {k: v.strip() for k, v in kw.items() if isinstance(v, str) and v.strip()}


@router.post("/api/public/careers/positions/{requirement_id}/apply")
@_rl.limit("10/minute")
def public_apply(
    request: Request,
    requirement_id: int,
    candidate_name: str = Form(..., max_length=255),
    email: str = Form(..., max_length=255),
    phone: str = Form(..., max_length=32),
    experience: str = Form(..., max_length=64),
    notice_period: str = Form("", max_length=60),
    last_working_day: str = Form("", max_length=20),
    current_location: str = Form("", max_length=120),
    preferred_location: str = Form("", max_length=120),
    current_ctc: str = Form("", max_length=40),
    expected_ctc: str = Form("", max_length=40),
    skills: str = Form("", max_length=500),
    linkedin_url: str = Form("", max_length=300),
    consent: str = Form(""),
    src: str = Form("", max_length=40),
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
):
    req = db.get(Requirement, requirement_id)
    if not careers.is_listed(db, req):
        raise HTTPException(status_code=404, detail="This position is no longer open — "
                                                    "have a look at the other openings.")
    if str(consent).lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=400, detail="Please agree to share your details with Karnex.")
    name, email, phone = _check_person(candidate_name, email, phone)
    if not (experience or "").strip():
        raise HTTPException(status_code=400, detail="Please enter your total experience.")
    lwd, lwd_err = careers.lwd_problem(notice_period, last_working_day, date.today())
    if lwd_err:
        raise HTTPException(status_code=400, detail=lwd_err)
    url, sha, size = _save_resume(file)
    channel = careers.channel_label(src)
    details = _details(notice_period=notice_period, current_location=current_location,
                       preferred_location=preferred_location, current_ctc=current_ctc,
                       expected_ctc=expected_ctc, skills=skills, linkedin_url=linkedin_url,
                       last_working_day=lwd.isoformat() if lwd else "")
    res = careers.record_application(
        db, req, name=name, email=email, phone=phone, experience=experience, details=details,
        file_url=url, file_sha256=sha, file_size=size, portal=careers.CAREERS_PORTAL,
        via="careers", channel=channel)
    return envelope(data={"received": True, "position": req.title},
                    message="We already have this application" if res.get("duplicate")
                    else "Application received")


@router.post("/api/public/careers/general-apply")
@_rl.limit("5/minute")
def public_general_apply(
    request: Request,
    candidate_name: str = Form(..., max_length=255),
    email: str = Form(..., max_length=255),
    phone: str = Form(..., max_length=32),
    experience: str = Form("", max_length=64),
    current_location: str = Form("", max_length=120),
    preferred_location: str = Form("", max_length=120),
    skills: str = Form("", max_length=500),
    consent: str = Form(""),
    src: str = Form("", max_length=40),
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
):
    """No matching opening: the person joins the talent pool (a Candidate record
    with the CV), so TA finds them in Talent Pool search for the next position."""
    if str(consent).lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=400, detail="Please agree to share your details with Karnex.")
    name, email, phone = _check_person(candidate_name, email, phone)
    url, _sha, _size = _save_resume(file)
    details = _details(current_location=current_location, preferred_location=preferred_location,
                       skills=skills)
    shim = SimpleNamespace(id=None, email=email, candidate_name=name, phone=phone,
                           resume_file_url=url, application_details=details,
                           applicant_experience=(experience or "").strip() or None)
    from services.slot_booking import find_or_create_candidate_from_resume
    channel = careers.channel_label(src)
    try:
        cand = find_or_create_candidate_from_resume(db, shim)
        # Who added them: the careers page (never overwrites a TA who added them first),
        # so the Candidates list reads "by Careers page · LinkedIn" and can be filtered.
        if not cand.created_by_id and not (cand.created_by_name or "").strip():
            cand.created_by_name = f"Careers page · {channel}"[:255]
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=500, detail="We could not save your résumé — please try again.")
    try:
        from services.notify import notify_role
        notify_role(db, "TA", f"Résumé from the careers page: {name}",
                    f"{name} sent a résumé from the careers page ({channel}) without picking a position. "
                    "Open Candidates (or Talent Pool search) to see the résumé and apply them to a position.",
                    f"/admin/?view=crm&p=candidates/{cand.id}", event="careers.general_application",
                    dedupe_prefix=f"careers_general:{cand.id}",
                    related_type="candidate", related_id=cand.id)
        db.commit()
    except Exception:
        db.rollback()
    return envelope(data={"received": True}, message="Résumé received")


# ------------------------------------------------------------------- staff

def _may_edit(db: Session, user: CurrentUser) -> bool:
    if getattr(user, "is_admin", False) or user.has_any("RMG", "Sales_Head"):
        return True
    try:
        from services.action_permissions import screens_as_rmg
        return bool(screens_as_rmg(db, user))
    except Exception:
        return False


@router.get("/api/careers/overview")
def careers_overview(request: Request, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(CAREERS_READ)):
    from routers.crm.apply import _base_url
    base = _base_url(request)
    try:
        from services.invite_links import base_reach, base_source, request_origin
        reach, source = base_reach(base), base_source()
        # A local / office address (http://localhost:2020 while the app serves HTTPS,
        # a LAN IP the certificate does not cover …) cannot be posted anyway, so the
        # link uses the exact origin this dashboard was opened on — it opens here.
        # Once a public address is set (Settings ▸ Organisation ▸ Email & links), it wins.
        if reach != "public":
            here = request_origin(request)
            if here:
                base, reach, source = here, base_reach(here), "request"
    except Exception:
        reach, source = "unknown", "request"
    data = careers.staff_overview(db)
    url = f"{base}/careers"
    listed = [p for p in data["positions"] if p["listed"]]
    return envelope(data={
        **data,
        "url": url,
        "base_url": base,
        "base_reach": reach,
        "base_source": source,
        "post_text": careers.post_text(listed, url),
        "channels": [{"key": k, "label": v} for k, v in
                     (("linkedin", "LinkedIn"), ("naukri", "Naukri"), ("whatsapp", "WhatsApp"),
                      ("indeed", "Indeed"))],
        "can_edit": _may_edit(db, user),
    })


class ListingIn(BaseModel):
    listed: bool


@router.put("/api/careers/positions/{requirement_id}")
def set_position_listing(requirement_id: int, body: ListingIn, db: Session = Depends(get_crm_db),
                         user: CurrentUser = Depends(CAREERS_WRITE)):
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Position not found")
    changed = careers.set_listed(db, req, body.listed, user)
    db.commit()
    word = "is on" if body.listed else "is off"
    return envelope(data={"id": req.id, "listed": body.listed, "changed": changed},
                    message=f"{req.title} {word} the careers page")
