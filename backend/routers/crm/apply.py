"""Public candidate application form + link (CRM-hosted).

TA generates a public application link for a requirement that is open for
sourcing. Candidates open the link (no login), fill name / email / phone /
experience and upload a resume; the submission lands in the CRM as a Resume row
against that requirement (source_portal = "Apply Link"), exactly like a manual
upload — so ATS scan / shortlist / AI L1 all work unchanged.

Paths:
  GET  /api/requirements/{id}/apply-link  (auth: TA/Admin) -> signed link
  GET  /apply/{token}                     (public)         -> HTML form
  POST /api/apply/{token}                 (public)         -> submit application

The token is an HMAC of the requirement id (no DB table needed). A link stays
valid only while the requirement is open for sourcing; closing/fulfilling the
requirement closes the form.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from html import escape

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required
from models import Requirement, RequirementActivityLog, RequirementStatus, Resume
from schemas.common import envelope
from services.crm_common import log_activity, save_upload_hashed
from auth_secret import auth_secret as _shared_auth_secret

router = APIRouter(tags=["CRM: Apply"])

# Requirement states in which the public form accepts applications.
_OPEN_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)

_ALLOWED_EXT = (".pdf", ".doc", ".docx", ".rtf", ".odt")
_MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB


# --------------------------------------------------------------------------- token
def _secret() -> str:
    """Same signing key as the rest of the app — see auth_secret.py.

    Public apply links are HMAC'd with this, so a guessable default meant anyone
    could mint a link for any requirement.
    """
    return _shared_auth_secret()


def make_apply_token(requirement_id: int) -> str:
    sig = hmac.new(_secret().encode("utf-8"), f"apply:{requirement_id}".encode("utf-8"),
                   hashlib.sha256).hexdigest()[:24]
    return f"{requirement_id}-{sig}"


def parse_apply_token(token: str) -> int | None:
    try:
        rid_str, sig = str(token).rsplit("-", 1)
        rid = int(rid_str)
    except (ValueError, AttributeError):
        return None
    expected = hmac.new(_secret().encode("utf-8"), f"apply:{rid}".encode("utf-8"),
                        hashlib.sha256).hexdigest()[:24]
    return rid if hmac.compare_digest(sig, expected) else None


def _base_url(request: Request) -> str:
    pub = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if pub:
        return pub
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------- TA: get link
@router.get("/api/requirements/{requirement_id}/apply-link")
def get_apply_link(
    requirement_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(role_required("TA")),
):
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    token = make_apply_token(req.id)
    apply_path = f"/apply/{token}"
    return envelope(
        data={
            "token": token,
            "apply_path": apply_path,
            "apply_url": f"{_base_url(request)}{apply_path}",
            "is_open": req.status in _OPEN_STATUSES,
            "requirement_status": req.status.value if hasattr(req.status, "value") else str(req.status),
        },
        message="Application link ready",
    )


# ------------------------------------------------------------------- public: form
def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{escape(title)} — Karnex Careers</title>
<style>
  :root {{ --brand1:#0ea5e9; --brand2:#4f46e5; --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --bg:#f8fafc; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         background:var(--bg); color:var(--ink); }}
  .wrap {{ max-width:640px; margin:0 auto; padding:24px 18px 48px; }}
  .brand {{ font-weight:800; font-size:20px; letter-spacing:-.02em; }}
  .brand span {{ background:linear-gradient(90deg,var(--brand1),var(--brand2)); -webkit-background-clip:text;
                 background-clip:text; color:transparent; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:18px; padding:22px; margin-top:18px;
           box-shadow:0 10px 30px rgba(15,23,42,.06); }}
  h1 {{ font-size:22px; margin:6px 0 2px; letter-spacing:-.02em; }}
  .sub {{ color:var(--muted); font-size:14px; margin-bottom:6px; }}
  label {{ display:block; font-size:13px; font-weight:700; margin:14px 0 6px; }}
  .req {{ color:#e11d48; }}
  input,select {{ width:100%; padding:11px 12px; border:1px solid var(--line); border-radius:12px; font-size:15px;
                  background:#fff; }}
  input[type=file] {{ padding:9px; }}
  button {{ margin-top:20px; width:100%; padding:13px; border:0; border-radius:12px; color:#fff; font-weight:800;
            font-size:15px; cursor:pointer; background:linear-gradient(90deg,var(--brand1),var(--brand2)); }}
  button:disabled {{ opacity:.6; cursor:not-allowed; }}
  .note {{ color:var(--muted); font-size:12px; margin-top:10px; }}
  .msg {{ padding:12px 14px; border-radius:12px; font-size:14px; margin-top:14px; }}
  .err {{ background:#fef2f2; color:#b91c1c; border:1px solid #fecaca; }}
  .ok {{ background:#ecfdf5; color:#047857; border:1px solid #a7f3d0; }}
</style></head>
<body><div class="wrap">
  <div class="brand">KARNEX <span>Careers</span></div>
  {body}
</div></body></html>"""
    return HTMLResponse(content=html, status_code=status)


@router.get("/apply/{token}", response_class=HTMLResponse)
def apply_form(token: str, db: Session = Depends(get_crm_db)):
    rid = parse_apply_token(token)
    req = db.get(Requirement, rid) if rid else None
    if req is None:
        return _page("Application", '<div class="card"><h1>Link not found</h1>'
                     '<p class="sub">This application link is invalid or has expired.</p></div>', status=404)
    if req.status not in _OPEN_STATUSES:
        return _page(req.title, f'<div class="card"><h1>{escape(req.title)}</h1>'
                     '<div class="msg err">Applications for this role are currently closed.</div></div>', status=200)

    jd_html = ""
    if (req.description or "").strip():
        jd_html = f'<div class="sub" style="white-space:pre-wrap;margin:8px 0 4px">{escape(req.description.strip())}</div>'
    body = f"""
  <div class="card">
    <h1>{escape(req.title)}</h1>
    {jd_html}
    <div class="sub">Apply below. Fields marked <span class="req">*</span> are required.</div>"""
    body += f"""
    <form id="f" enctype="multipart/form-data">
      <label>Full name <span class="req">*</span></label>
      <input name="candidate_name" required maxlength="255" autocomplete="name"/>
      <label>Email <span class="req">*</span></label>
      <input name="email" type="email" required maxlength="255" autocomplete="email"/>
      <label>Phone <span class="req">*</span></label>
      <input name="phone" required maxlength="32" autocomplete="tel"/>
      <label>Total experience (years) <span class="req">*</span></label>
      <input name="experience" required maxlength="64" placeholder="e.g. 4.5"/>
      <label>Highest education</label>
      <input name="education" maxlength="120" placeholder="e.g. B.Tech, Computer Science"/>
      <label>Technical domain</label>
      <input name="technical_domain" maxlength="120" placeholder="e.g. Backend / Data Engineering"/>
      <label>Key skills <span class="req">*</span></label>
      <input name="skills" required maxlength="500" placeholder="e.g. Python, FastAPI, PostgreSQL, AWS"/>
      <label>Notice period <span class="req">*</span></label>
      <input name="notice_period" required maxlength="60" placeholder="e.g. 30 days / Immediate"/>
      <label>Current CTC <span class="req">*</span></label>
      <input name="current_ctc" required maxlength="40" placeholder="e.g. 12 LPA"/>
      <label>Expected CTC <span class="req">*</span></label>
      <input name="expected_ctc" required maxlength="40" placeholder="e.g. 18 LPA"/>
      <label>Preferred location <span class="req">*</span></label>
      <input name="preferred_location" required maxlength="120" placeholder="e.g. Pune / Remote"/>
      <label>Resume <span class="req">*</span></label>
      <input name="file" type="file" required accept=".pdf,.doc,.docx,.rtf,.odt"/>
      <button id="btn" type="submit">Submit application</button>
      <div id="out"></div>
      <div class="note">Your details are shared only with the Karnex recruitment team for this role.</div>
    </form>
  </div>
  <script>
    var f=document.getElementById('f'),btn=document.getElementById('btn'),out=document.getElementById('out');
    f.addEventListener('submit',async function(e){{
      e.preventDefault(); out.innerHTML=''; btn.disabled=true; btn.textContent='Submitting…';
      try {{
        var res=await fetch('/api/apply/{token}',{{method:'POST',body:new FormData(f)}});
        var data=await res.json();
        if(!res.ok){{throw new Error((data&&data.detail)||'Submission failed');}}
        f.style.display='none';
        out.innerHTML='<div class="msg ok"><strong>Thank you!</strong> Your application has been received. Our team will be in touch.</div>';
      }} catch(err) {{
        out.innerHTML='<div class="msg err">'+(err.message||'Something went wrong')+'</div>';
        btn.disabled=false; btn.textContent='Submit application';
      }}
    }});
  </script>"""
    return _page(req.title, body)


# ---------------------------------------------------------------- public: submit
@router.post("/api/apply/{token}")
def submit_application(
    token: str,
    candidate_name: str = Form(..., min_length=1, max_length=255),
    email: str = Form(..., max_length=255),
    phone: str = Form(..., max_length=32),
    experience: str = Form("", max_length=64),
    education: str = Form("", max_length=120),
    technical_domain: str = Form("", max_length=120),
    skills: str = Form("", max_length=500),
    notice_period: str = Form("", max_length=60),
    current_ctc: str = Form("", max_length=40),
    expected_ctc: str = Form("", max_length=40),
    preferred_location: str = Form("", max_length=120),
    file: UploadFile = File(...),
    db: Session = Depends(get_crm_db),
):
    rid = parse_apply_token(token)
    req = db.get(Requirement, rid) if rid else None
    if req is None:
        raise HTTPException(status_code=404, detail="Invalid or expired application link")
    if req.status not in _OPEN_STATUSES:
        raise HTTPException(status_code=400, detail="Applications for this role are closed")

    filename = (file.filename or "").lower()
    if not any(filename.endswith(ext) for ext in _ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="Resume must be a PDF, DOC, DOCX, RTF or ODT file")

    name = candidate_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Full name is required")

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
    file_url, file_sha256, file_size = save_upload_hashed(file, "resumes")
    # Dedupe identical submissions (e.g. double-clicked apply) by file bytes.
    dup = db.execute(
        select(Resume).where(Resume.requirement_id == req.id, Resume.file_sha256 == file_sha256)
    ).scalars().first()
    if dup is not None:
        # Same file re-submitted: still make sure a Candidate exists for it (the
        # original submission may predate immediate candidate creation).
        if dup.candidate_id is None:
            try:
                from services.slot_booking import find_or_create_candidate_from_resume
                cand = find_or_create_candidate_from_resume(db, dup)
                dup.candidate_id = cand.id
                db.commit()
            except Exception:
                db.rollback()
        return envelope(data={"received": True}, message="Application already received")
    resume = Resume(
        requirement_id=req.id,
        candidate_name=name,
        email=(email or "").strip() or None,
        phone=(phone or "").strip() or None,
        source_portal="Apply Link",
        applicant_experience=(experience or "").strip() or None,
        application_details=details or None,
        resume_file_url=file_url,
        file_sha256=file_sha256,
        file_size=file_size,
    )
    db.add(resume)
    db.flush()  # assign resume.id before deriving the candidate
    # Create/enrich the Candidate immediately so the applicant appears in the
    # Candidate tab right away, with the details they submitted (experience,
    # education, domain, skills, CTC) — and a Sourcing profile so they appear
    # in the opportunity's Applicants tab too. Best-effort — never block the
    # submission.
    try:
        from services.slot_booking import ensure_sourcing_profile, find_or_create_candidate_from_resume
        # Savepoint: best-effort — on Postgres a failed statement here would
        # otherwise poison the transaction and 500 the whole public submission.
        with db.begin_nested():
            cand = find_or_create_candidate_from_resume(db, resume)
            resume.candidate_id = cand.id
        ensure_sourcing_profile(db, resume, req, source="apply_link")
    except Exception:
        pass
    existing = db.execute(
        select(func.count()).select_from(Resume).where(Resume.requirement_id == req.id)
    ).scalar() or 0
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                 "RESUME_UPLOADED", f"Application received via link from {name}")
    if req.status == RequirementStatus.POSTED_ON_PORTALS and existing == 0:
        req.status = RequirementStatus.IN_PROGRESS
        log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                     "STATUS_CHANGED", "Auto-moved Posted_On_Portals -> In_Progress (first application received)")
    db.commit()
    return envelope(data={"received": True}, message="Application received")
