"""Interview slot-booking flow (automated candidate pipeline).

TA side (auth):
  GET/POST /api/requirements/{id}/slots      list upcoming / bulk-create slots
  DELETE   /api/slots/{slot_id}              remove a slot (400 if confirmed bookings)
  GET      /api/requirements/{id}/bookings   list slot bookings for a requirement
  POST     /api/resumes/{id}/send-slot-invite manual (re)send of the booking link

Public (no auth, follows apply.py's token-page pattern):
  GET  /book/{token}                          HTML page: pick + confirm a slot
  POST /api/book/{token}/confirm {slot_id}    confirm; schedules the AI interview

Confirming a slot runs the same pipeline as TA's schedule-ai-interview: the
candidate + profile are found-or-created, a real AI L1 session is scheduled and
the invite link/access key is returned (and emailed/WhatsApped best-effort).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, get_crm_db, role_required, gated_read, gated_write
from models import (
    AiInterviewStatus, InterviewSlot, Requirement, RequirementActivityLog, Resume, SlotBooking,
)
from routers.crm.apply import _base_url, _page
from schemas.common import envelope
from services.ai_interview_bridge import schedule_l1_interview
from services.candidate_comms import interview_link_message, notify_candidate
from services.crm_common import log_activity
from services.notify import notify_role
from services.slot_booking import (
    booking_url_for, find_or_create_candidate_from_resume, get_or_create_profile,
    send_slot_invite, upcoming_open_slots,
)

router = APIRouter(tags=["CRM: Interview Slots"])


def sa_false():
    from sqlalchemy import false
    return false()


def _now():
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


try:
    from zoneinfo import ZoneInfo
    _DISPLAY_TZ = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing on a bare Windows install
    _DISPLAY_TZ = timezone.utc


def _from_ist(dt: datetime) -> datetime:
    """The booking page's datetime-local input is IST (the page says so) — a
    naive value from it must be read as IST, NOT as UTC like _as_utc does."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_DISPLAY_TZ)
    return dt.astimezone(timezone.utc)


def _when_text(dt: datetime | None) -> str:
    """Candidate-facing time, in IST — the timezone the TA entered it in.

    The DB stores UTC; showing candidates "09:10 UTC" for a slot the TA
    created as 2:40 PM made every invite read as the wrong time (seen live
    25 Aug 2026). Everyone in this hiring flow is in India, so wall-clock IST
    with an explicit label is the honest rendering.
    """
    if dt is None:
        return ""
    return _as_utc(dt).astimezone(_DISPLAY_TZ).strftime("%A, %d %B %Y at %I:%M %p IST")


def _requirement_or_404(db: Session, requirement_id: int) -> Requirement:
    req = db.get(Requirement, requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    return req


def _serialize_slot(slot: InterviewSlot) -> dict:
    return {
        "id": slot.id,
        "requirement_id": slot.requirement_id,
        "slot_at": slot.slot_at.isoformat() if slot.slot_at else None,
        "slot_at_text": _when_text(slot.slot_at),
        "capacity": slot.capacity,
        "booked_count": slot.booked_count,
        "available": max(0, (slot.capacity or 0) - (slot.booked_count or 0)),
        "created_by": slot.created_by,
        "created_at": slot.created_at.isoformat() if slot.created_at else None,
    }


def _serialize_booking(booking: SlotBooking, resume: Resume | None,
                       slot: InterviewSlot | None, base_url: str) -> dict:
    return {
        "id": booking.id,
        "token": booking.token,
        "booking_url": booking_url_for(base_url, booking),
        "resume_id": booking.resume_id,
        "candidate_name": resume.candidate_name if resume else None,
        "email": resume.email if resume else None,
        "phone": resume.phone if resume else None,
        "requirement_id": booking.requirement_id,
        "candidate_id": booking.candidate_id,
        "slot_id": booking.slot_id,
        "slot_at": slot.slot_at.isoformat() if slot and slot.slot_at else None,
        "slot_at_text": _when_text(slot.slot_at) if slot else "",
        "status": booking.status,
        "invite_url": booking.invite_url,
        "created_at": booking.created_at.isoformat() if booking.created_at else None,
        "confirmed_at": booking.confirmed_at.isoformat() if booking.confirmed_at else None,
    }


# ------------------------------------------------------------------ TA: slots

class SlotIn(BaseModel):
    slot_at: datetime
    capacity: int = 1


@router.get("/api/requirements/{requirement_id}/slots")
def list_slots(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    # Read widened 25 Aug 2026: RMG/Sales_Head VIEW the calendar TA runs.
    user: CurrentUser = Depends(gated_read("requirements", "TA", "RMG", "Sales_Head")),
):
    _requirement_or_404(db, requirement_id)
    slots = db.execute(
        select(InterviewSlot)
        .where(InterviewSlot.requirement_id == requirement_id, InterviewSlot.slot_at > func.now())
        .order_by(InterviewSlot.slot_at.asc())
    ).scalars().all()
    return envelope([_serialize_slot(s) for s in slots])


@router.post("/api/requirements/{requirement_id}/slots")
def create_slots(
    requirement_id: int,
    slots_in: list[SlotIn],
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    req = _requirement_or_404(db, requirement_id)
    if not slots_in:
        raise HTTPException(status_code=400, detail="Provide at least one slot ({slot_at, capacity?})")
    now = _now()
    created: list[InterviewSlot] = []
    for item in slots_in:
        slot_at = _as_utc(item.slot_at)
        if slot_at <= now:
            raise HTTPException(status_code=400,
                                detail=f"Slot datetime must be in the future: {item.slot_at.isoformat()}")
        if item.capacity < 1:
            raise HTTPException(status_code=400, detail="Slot capacity must be at least 1")
        slot = InterviewSlot(requirement_id=req.id, slot_at=slot_at,
                             capacity=item.capacity, created_by=user.id)
        db.add(slot)
        created.append(slot)
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SLOTS_CREATED", f"{len(created)} interview slot(s) published")
    db.commit()
    for slot in created:
        db.refresh(slot)
    return envelope([_serialize_slot(s) for s in created],
                    message=f"{len(created)} slot(s) created")


@router.delete("/api/slots/{slot_id}")
def delete_slot(
    slot_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    slot = db.get(InterviewSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="Slot not found")
    confirmed = db.execute(
        select(func.count()).select_from(SlotBooking)
        .where(SlotBooking.slot_id == slot.id, SlotBooking.status == "Confirmed")
    ).scalar() or 0
    if confirmed:
        raise HTTPException(status_code=400,
                            detail=f"Cannot delete: {confirmed} confirmed booking(s) on this slot")
    db.delete(slot)
    db.commit()
    return envelope({"deleted": True, "slot_id": slot_id}, message="Slot deleted")


@router.get("/api/requirements/{requirement_id}/bookings")
def list_bookings(
    requirement_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("requirements", "TA", "RMG", "Sales_Head")),
):
    _requirement_or_404(db, requirement_id)
    base = _base_url(request)
    rows = db.execute(
        select(SlotBooking, Resume)
        .join(Resume, Resume.id == SlotBooking.resume_id)
        .where(SlotBooking.requirement_id == requirement_id)
        .order_by(SlotBooking.created_at.desc(), SlotBooking.id.desc())
    ).all()
    out = []
    for booking, resume in rows:
        slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
        out.append(_serialize_booking(booking, resume, slot, base))
    return envelope(out)


def _reset_missed_booking(db: Session, booking, resume) -> bool:
    """Missed-slot recovery (25 Aug 2026): a booking whose chosen slot is in
    the PAST reopens when the TA re-sends the invite — same link, fresh pick.

    A confirmed FUTURE booking is deliberately untouchable from here: resetting
    it would silently cancel a live interview.
    """
    if booking.status != "Confirmed":
        return False
    slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
    if slot is not None and _as_utc(slot.slot_at) > _now():
        raise HTTPException(
            status_code=400,
            detail=(f"This candidate already confirmed {_when_text(slot.slot_at)} — the booking "
                    "link is still valid. Re-invite becomes available after the slot has passed."),
        )
    booking.status = "Pending"
    booking.slot_id = None
    booking.confirmed_at = None
    booking.invite_token = None
    booking.invite_url = None
    resume.ai_interview_status = AiInterviewStatus.NOT_SCHEDULED
    resume.ai_interview_scheduled_at = None
    return True


@router.get("/api/resumes/{resume_id}/slot-invite-preview")
def slot_invite_preview(
    resume_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    """EXACTLY what the candidate will receive, before anything is sent.

    Powers the confirm dialog (user decision, 25 Aug 2026): TA reads the real
    subject/body — built by the same slot_invite_message the send path uses —
    and only then confirms. Also reports how many open slots exist, because an
    invite to an empty booking page is worse than no invite. Creating the
    booking token here is deliberate: it is idempotent and sends nothing.
    """
    from services.slot_booking import (
        booking_url_for, get_or_create_booking, upcoming_open_slots,
    )
    from services.candidate_comms import slot_invite_message

    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not (resume.email or "").strip() and not (resume.phone or "").strip():
        raise HTTPException(status_code=400,
                            detail="Resume has no email or phone — add contact info before sending an invite")
    req = _requirement_or_404(db, resume.requirement_id)
    from services.requirements import ensure_not_on_hold
    ensure_not_on_hold(req)
    # Same RMG gate as the send — the preview must not promise what the send
    # will refuse.
    if resume.candidate_id is not None:
        from models import CandidateProfile
        from services.candidate_profiles import rmg_screening_blocks_l1
        profile = db.execute(
            select(CandidateProfile).where(
                CandidateProfile.candidate_id == resume.candidate_id,
                CandidateProfile.opportunity_id == req.opportunity_id,
            )
        ).scalars().first()
        blocked = rmg_screening_blocks_l1(profile) if profile is not None else None
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)
    # Manual route (3 Sep 2026): RMG chose a human L1 — no AI booking link.
    from services.slot_booking import slot_invite_blocked
    blocked = slot_invite_blocked(db, resume, req)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)
    booking = get_or_create_booking(db, resume)
    re_invite = _reset_missed_booking(db, booking, resume)
    db.commit()
    url = booking_url_for(_base_url(request), booking)
    msg = slot_invite_message(resume.candidate_name, req.title, url, db=db)
    open_slots = upcoming_open_slots(db, req.id)
    return envelope(data={
        "to_email": resume.email,
        "to_phone": resume.phone,
        "subject": msg["subject"],
        "text": msg["text"],
        "booking_url": url,
        "open_slots": len(open_slots),
        "next_slot_at": open_slots[0].slot_at.isoformat() if open_slots else None,
        "re_invite": re_invite,
    }, message="Slot invite preview")


@router.post("/api/resumes/{resume_id}/send-slot-invite")
def send_slot_invite_manual(
    resume_id: int,
    request: Request,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_write("requirements", "TA")),
):
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume not found")
    if not (resume.email or "").strip() and not (resume.phone or "").strip():
        raise HTTPException(status_code=400,
                            detail="Resume has no email or phone — add contact info before sending an invite")
    req = _requirement_or_404(db, resume.requirement_id)
    from services.requirements import ensure_not_on_hold
    ensure_not_on_hold(req)
    # RMG screening gate (25 Aug 2026): the slot invite leads straight to a
    # booked AI L1, so it is gated exactly like scheduling one.
    if resume.candidate_id is not None:
        from models import CandidateProfile
        from services.candidate_profiles import rmg_screening_blocks_l1
        profile = db.execute(
            select(CandidateProfile).where(
                CandidateProfile.candidate_id == resume.candidate_id,
                CandidateProfile.opportunity_id == req.opportunity_id,
            )
        ).scalars().first()
        blocked = rmg_screening_blocks_l1(profile) if profile is not None else None
        if blocked:
            raise HTTPException(status_code=400, detail=blocked)
    # Manual route (3 Sep 2026): RMG chose a human L1 — no AI booking link.
    from services.slot_booking import slot_invite_blocked
    blocked = slot_invite_blocked(db, resume, req)
    if blocked:
        raise HTTPException(status_code=400, detail=blocked)
    from services.slot_booking import get_or_create_booking as _gocb
    _reset_missed_booking(db, _gocb(db, resume), resume)
    base = _base_url(request)
    booking, results = send_slot_invite(db, resume, req, base, actor=user)
    channels = [ch for ch, res in results.items() if res.get("sent")]
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "SLOT_INVITE_SENT",
                 f"SLOT_INVITE_SENT ({'/'.join(channels) if channels else 'no channel delivered'}) "
                 f"to {resume.candidate_name} — booking #{booking.id}")
    db.commit()

    # HONEST DELIVERY (26 Aug 2026). "queued": True above only means the mail
    # reached the durable outbox — the SMTP send happens on the drain worker,
    # and until now its failures (bad SMTP config, rejection, suppressed event)
    # were invisible: the TA saw "sent", the candidate got nothing. Drain
    # synchronously and report the row's REAL status + error back to the TA.
    email_delivery = None
    if results.get("email", {}).get("queued"):
        try:
            from models import EmailOutbox
            from services.email_outbox import drain_once
            summary = drain_once(limit=10)
            row = db.execute(
                select(EmailOutbox)
                .where(EmailOutbox.to_email == resume.email,
                       EmailOutbox.event == "candidate.slot_invite")
                .order_by(EmailOutbox.id.desc())
            ).scalars().first()
            if row is not None:
                db.refresh(row)
                status = row.status.value if hasattr(row.status, "value") else str(row.status)
                # A drain that could not run at all (smtp_not_configured,
                # db_unavailable) leaves the row Queued with NO last_error —
                # report the drain's own error so "stuck at Queued" explains itself.
                email_delivery = {"status": status, "attempts": row.attempts,
                                  "error": row.last_error or summary.get("error")}
        except Exception:
            pass  # visibility must never break the send itself

    db.refresh(booking)
    slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
    if email_delivery and email_delivery["status"] == "Sent":
        msg = "Slot invite emailed to the candidate"
    elif email_delivery and email_delivery["status"] in ("Failed", "Skipped"):
        msg = f"Email NOT delivered ({email_delivery.get('error') or email_delivery['status']})"
    elif channels:
        msg = "Slot invite queued — delivery is retried automatically"
    else:
        msg = "Booking link created (no channel delivered)"
    return envelope(
        {"booking": _serialize_booking(booking, resume, slot, base),
         "notified": results, "email_delivery": email_delivery},
        message=msg,
    )


# --------------------------------------------------------------- public: page

def _booking_by_token(db: Session, token: str) -> SlotBooking | None:
    if not token or len(token) > 64:
        return None
    return db.execute(select(SlotBooking).where(SlotBooking.token == token)).scalars().first()


@router.get("/book/{token}", response_class=HTMLResponse)
def booking_page(token: str, db: Session = Depends(get_crm_db)):
    booking = _booking_by_token(db, token)
    if booking is None:
        return _page("Interview booking", '<div class="card"><h1>Link not found</h1>'
                     '<p class="sub">This booking link is invalid or has expired.</p></div>', status=404)
    resume = db.get(Resume, booking.resume_id)
    req = db.get(Requirement, booking.requirement_id)
    title = req.title if req else "Interview"
    name = resume.candidate_name if resume else "Candidate"

    if booking.status == "Confirmed":
        slot = db.get(InterviewSlot, booking.slot_id) if booking.slot_id else None
        when = _when_text(slot.slot_at) if slot else ""
        invite = escape(booking.invite_url or "")
        body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="sub">Hello {escape(name)}, your interview slot is confirmed.</div>
    {f'<div class="msg ok"><strong>When:</strong> {escape(when)}</div>' if when else ''}
    <div class="msg ok">Your AI interview link:<br/>
      <a href="{invite}" style="word-break:break-all">{invite or 'Check your email for the link'}</a></div>
    <div class="note">Your secure access key was sent to your email/WhatsApp along with this link.
    You will need your registered email and the access key to enter the interview.
    If you can't find it, please contact the Karnex recruitment team.</div>
  </div>"""
        return _page(title, body)

    if booking.status in ("Expired", "Cancelled"):
        body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="msg err">This booking link is no longer active ({escape(booking.status.lower())}).
    Our recruitment team will contact you.</div>
  </div>"""
        return _page(title, body)

    # No published slots is no longer a dead end (26 Aug 2026): the candidate
    # proposes their own time and the recruiter is notified of the choice.
    slots = upcoming_open_slots(db, booking.requirement_id)

    options = "".join(
        f'<label style="display:flex;align-items:center;gap:10px;font-weight:600;padding:12px;'
        f'border:1px solid var(--line);border-radius:12px;margin:10px 0;cursor:pointer;">'
        f'<input type="radio" name="slot_id" value="{slot.id}" style="width:auto"/>'
        f'{escape(_when_text(slot.slot_at))}'
        f'<span style="margin-left:auto;color:var(--muted);font-size:12px;font-weight:500">'
        f'{max(0, (slot.capacity or 0) - (slot.booked_count or 0))} seat(s) left</span></label>'
        for slot in slots
    )
    resume_row = db.get(Resume, booking.resume_id)
    pre_email = escape((getattr(resume_row, "email", None) or "").strip())
    pre_phone = escape((getattr(resume_row, "phone", None) or "").strip())
    pre_loc = escape(str(((getattr(resume_row, "application_details", None) or {})
                          .get("preferred_location") or "")).strip())
    field_css = ('style="width:100%;padding:10px 12px;border:1px solid var(--line);'
                 'border-radius:10px;font:inherit;margin:4px 0 12px"')
    label_css = 'style="font-size:12px;font-weight:600;color:var(--muted)"'
    body = f"""
  <div class="card">
    <h1>{escape(title)}</h1>
    <div class="sub">Hello {escape(name)}, confirm your details, then pick a listed slot or
    choose your own interview time. All times are in IST (Indian Standard Time).</div>
    <form id="f">
      <label {label_css}>Full name *</label>
      <input name="cand_name" value="{escape(name)}" required {field_css}/>
      <label {label_css}>Email *</label>
      <input name="cand_email" type="email" value="{pre_email}" required {field_css}/>
      <label {label_css}>Mobile number *</label>
      <input name="cand_phone" value="{pre_phone}" required {field_css}/>
      <label {label_css}>Current location *</label>
      <input name="cand_location" value="{pre_loc}" placeholder="e.g. Bengaluru" required {field_css}/>
      <label {label_css}>Updated resume (optional — PDF/DOC/DOCX, max 10 MB)</label>
      <input name="cand_resume" type="file" accept=".pdf,.doc,.docx,.txt" {field_css}/>
      {options and f'<div style="margin:14px 0 6px;font-size:12px;font-weight:700;color:var(--muted)">OR CHOOSE YOUR OWN TIME</div>' or f'<div style="margin:14px 0 6px;font-size:13px;font-weight:600">Choose the interview time that works for you (IST):</div>'}
      <input name="custom_time" type="datetime-local" {field_css}/>
      <div class="note" style="margin:-6px 0 10px">Pick any time in the next 30 days — at least
      5 minutes from now. Your recruiter will see the time you chose.</div>
      <button id="btn" type="submit">Confirm my slot</button>
      <div id="out"></div>
      <div class="note">After confirming, your AI interview link and secure access key appear here
      and are also sent to your email/WhatsApp.</div>
    </form>
  </div>
  <script>
    var f=document.getElementById('f'),btn=document.getElementById('btn'),out=document.getElementById('out');
    f.addEventListener('submit',async function(e){{
      e.preventDefault(); out.innerHTML=''; btn.disabled=true; btn.textContent='Confirming…';
      try {{
        var sel=f.querySelector('input[name=slot_id]:checked');
        var custom=(f.custom_time&&f.custom_time.value)||'';
        if(!sel&&!custom){{throw new Error('Please choose a slot or enter your own time');}}
        var pick=custom?{{custom_time:custom}}:{{slot_id:parseInt(sel.value,10)}};
        if(f.cand_resume&&f.cand_resume.files&&f.cand_resume.files.length){{
          btn.textContent='Uploading resume…';
          var fd=new FormData(); fd.append('file',f.cand_resume.files[0]);
          var up=await fetch('/api/book/{token}/resume',{{method:'POST',body:fd}});
          var upData=await up.json().catch(function(){{return null;}});
          if(!up.ok){{throw new Error((upData&&upData.detail)||'Resume upload failed');}}
          btn.textContent='Confirming…';
        }}
        var res=await fetch('/api/book/{token}/confirm',{{method:'POST',
          headers:{{'Content-Type':'application/json'}},
          body:JSON.stringify(Object.assign(pick,{{
            name:f.cand_name.value.trim(),email:f.cand_email.value.trim(),
            phone:f.cand_phone.value.trim(),location:f.cand_location.value.trim()}}))}});
        var data=await res.json();
        if(!res.ok){{throw new Error((data&&data.detail)||'Confirmation failed');}}
        var d=(data&&data.data)||{{}};
        f.style.display='none';
        out.innerHTML='<div class="msg ok"><strong>Slot confirmed!</strong><br/>'
          +'Your AI interview link: <a href="'+d.invite_url+'" style="word-break:break-all">'+d.invite_url+'</a>'
          +(d.access_key?('<br/><strong>Secure access key:</strong> <code>'+d.access_key+'</code>'
          +'<br/><small>Keep this key safe — you need it (with your registered email) to enter the interview.</small>'):'')
          +'</div>';
      }} catch(err) {{
        out.innerHTML='<div class="msg err">'+(err.message||'Something went wrong')+'</div>';
        btn.disabled=false; btn.textContent='Confirm my slot';
      }}
    }});
  </script>"""
    return _page(title, body)


# ------------------------------------------------------------ public: confirm

@router.post("/api/book/{token}/resume")
def booking_upload_resume(token: str, file: UploadFile = File(...),
                          db: Session = Depends(get_crm_db)):
    """Public: the candidate attaches an UPDATED resume from the booking page
    (27 Aug 2026). Token-gated like the page itself; same allowlist/size
    rules as the public apply form. Replaces the resume file on the record —
    the newest CV is the one the ATS and the interviewers should see."""
    booking = _booking_by_token(db, token)
    if booking is None or booking.status not in ("Pending", "Confirmed"):
        raise HTTPException(status_code=404, detail="This booking link is not active")
    resume = db.get(Resume, booking.resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Resume record not found")
    from services.crm_common import safe_upload_extension, save_upload_hashed
    safe_upload_extension(file.filename)  # 400 on anything but pdf/doc/docx/…
    file_url, file_sha256, file_size = save_upload_hashed(file, "resumes")
    if file_size > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10 MB)")
    resume.resume_file_url = file_url
    resume.file_sha256 = file_sha256
    resume.file_size = file_size
    log_activity(db, RequirementActivityLog, "requirement_id", booking.requirement_id,
                 resume.screened_by or 0,
                 "RESUME_UPDATED",
                 f"Updated resume uploaded by {resume.candidate_name} from the booking page")
    db.commit()
    return envelope({"uploaded": True}, message="Updated resume received")


class ConfirmIn(BaseModel):
    # Either a published slot OR the candidate's own proposed time (26 Aug
    # 2026, user decision): TA no longer has to pre-create slots — the
    # candidate picks what suits them and TA is told what was chosen.
    slot_id: int | None = None
    custom_time: datetime | None = None
    # Candidate-confirmed contact details (25 Aug 2026): the booking form asks
    # for these before the slot pick, so the record is corrected by the person
    # who knows it best. All optional at the API level — an old page without
    # the fields must still confirm.
    name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=32)
    location: str | None = Field(default=None, max_length=120)


@router.post("/api/book/{token}/confirm")
def confirm_booking(
    token: str,
    payload: ConfirmIn,
    request: Request,
    db: Session = Depends(get_crm_db),
):
    booking = _booking_by_token(db, token)
    if booking is None:
        raise HTTPException(status_code=404, detail="Invalid or expired booking link")
    if booking.status == "Confirmed":
        raise HTTPException(status_code=400, detail="This booking is already confirmed")
    if booking.status != "Pending":
        raise HTTPException(status_code=400,
                            detail=f"This booking is no longer active ({booking.status})")

    candidate_chose_time = False
    if payload.slot_id is not None:
        # Lock the slot row so two candidates can't take the last seat concurrently.
        slot = db.execute(
            select(InterviewSlot).where(InterviewSlot.id == payload.slot_id).with_for_update()
        ).scalars().first()
        if slot is None or slot.requirement_id != booking.requirement_id:
            raise HTTPException(status_code=404, detail="Slot not found for this role")
        if _as_utc(slot.slot_at) <= _now():
            raise HTTPException(status_code=400, detail="This slot is in the past — pick another one")
        if (slot.booked_count or 0) >= (slot.capacity or 0):
            raise HTTPException(status_code=409, detail="This slot just filled up — pick another one")
    elif payload.custom_time is not None:
        when_utc = _from_ist(payload.custom_time)
        if when_utc <= _now() + timedelta(minutes=5):
            raise HTTPException(status_code=400,
                                detail="Pick a time at least 5 minutes from now")
        if when_utc > _now() + timedelta(days=30):
            raise HTTPException(status_code=400,
                                detail="Pick a time within the next 30 days")
        # A capacity-1 slot is minted for the candidate's own time, so every
        # downstream surface (slot tables, bookings, calendars) sees a normal
        # slot; created_by stays NULL to mark it candidate-proposed.
        slot = InterviewSlot(requirement_id=booking.requirement_id, slot_at=when_utc,
                             capacity=1, booked_count=0, created_by=None)
        db.add(slot)
        db.flush()
        candidate_chose_time = True
    else:
        raise HTTPException(status_code=400,
                            detail="Choose a slot or propose your own time")

    resume = db.get(Resume, booking.resume_id)
    if resume is None:
        raise HTTPException(status_code=404, detail="Application not found for this booking")
    req = _requirement_or_404(db, booking.requirement_id)

    # Apply the candidate-confirmed details BEFORE candidate creation, so the
    # interview link and access key go to the address they just verified.
    if (payload.name or "").strip():
        resume.candidate_name = payload.name.strip()[:255]
    if (payload.email or "").strip():
        resume.email = payload.email.strip()[:320]
    if (payload.phone or "").strip():
        resume.phone = payload.phone.strip()[:32]
    if (payload.location or "").strip():
        details = dict(resume.application_details or {})
        details["preferred_location"] = payload.location.strip()[:120]
        resume.application_details = details

    # Reuse the candidate already linked to this resume — matching by the
    # (possibly just-corrected) email would mint a DUPLICATE candidate the
    # moment someone fixes a typo in their address.
    from models import Candidate as _Candidate
    candidate = db.get(_Candidate, resume.candidate_id) if resume.candidate_id else None
    if candidate is None:
        candidate = find_or_create_candidate_from_resume(db, resume)
    elif (payload.phone or "").strip() and not (candidate.phone or "").strip():
        candidate.phone = payload.phone.strip()[:32]
    profile = get_or_create_profile(db, candidate, req)

    # THE SLOT TIME GATES THE LINK (fix, 27 Aug 2026): passing the booked
    # slot as scheduled_at_local makes the invite login hold the candidate on
    # a countdown until 10:00 AM for a 10:00 AM slot, instead of letting them
    # start the moment the confirmation email arrived. IST wall time — the
    # same rendering every candidate-facing "When:" uses.
    slot_local = _as_utc(slot.slot_at).astimezone(_DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    bridge = schedule_l1_interview(db, candidate, req, profile, resume=resume, scheduled_by=None,
                                   scheduled_at_local=slot_local)
    if not bridge.get("scheduled"):
        raise HTTPException(status_code=502,
                            detail=f"AI interview scheduling failed: {bridge.get('error')}")

    now = _now()
    booking.status = "Confirmed"
    booking.confirmed_at = now
    booking.candidate_id = candidate.id
    booking.slot_id = slot.id
    booking.invite_token = bridge.get("session_ref")
    booking.invite_url = bridge.get("invite_url") or None
    slot.booked_count = (slot.booked_count or 0) + 1
    resume.candidate_id = candidate.id
    resume.ai_interview_status = AiInterviewStatus.SCHEDULED
    # The CHOSEN SLOT time, not now(): this field feeds every "When:" the
    # candidate and TA see. Storing the confirmation moment made the invite
    # email announce an interview "at 2:48 PM today" for a slot booked for
    # Thursday 11:41 PM (seen live 25 Aug 2026).
    resume.ai_interview_scheduled_at = slot.slot_at

    when = _when_text(slot.slot_at)
    chosen_note = " (time proposed by the candidate)" if candidate_chose_time else ""
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, req.created_by,
                 "SLOT_CONFIRMED",
                 f"SLOT_CONFIRMED — {resume.candidate_name} booked {when}{chosen_note}; AI L1 scheduled")
    # The TA's email shows the CANDIDATE as the sender identity (From display
    # name + Reply-To), so replying goes straight to them instead of noreply.
    from types import SimpleNamespace
    candidate_actor = SimpleNamespace(full_name=resume.candidate_name,
                                      username=resume.candidate_name,
                                      email=(resume.email or "").strip())
    # ONLY the TA who sent the invite hears about the booking (user decision,
    # 26 Aug 2026) — broadcasting to the whole TA role meant every recruiter
    # got every candidate's confirmation and the actual owner's inbox was
    # indistinguishable from noise. invited_by (0084) is stamped at send time;
    # legacy bookings fall back to the resume's screener, then the role.
    _confirm_title = f"Slot confirmed: {resume.candidate_name}"
    _confirm_body = (f"{resume.candidate_name} confirmed {when}{chosen_note} "
                     f"for '{req.title}' — AI L1 scheduled.")
    _confirm_link = f"/admin?view=crm&p=requirements/{req.id}"
    owner_id = getattr(booking, "invited_by", None) or getattr(resume, "screened_by", None)
    if owner_id:
        from services.notify import notify_user
        notify_user(db, owner_id, _confirm_title, _confirm_body, _confirm_link,
                    event="slot.confirmed", actor=candidate_actor,
                    related_type="candidate", related_id=candidate.id)
    else:
        notify_role(db, "TA", _confirm_title, _confirm_body, _confirm_link,
                    event="slot.confirmed", actor=candidate_actor)

    msg = interview_link_message(resume.candidate_name, req.title, when,
                                 bridge.get("invite_url", ""), bridge.get("access_key", ""), db=db)
    # The confirmation to the candidate goes out AS the TA who sent the invite
    # (7 Sep 2026): From shows their name, Reply-To is their mailbox.
    ta_actor = None
    if owner_id:
        try:
            from services.recipients import user_recipient
            rec = user_recipient(db, owner_id)
            if rec is not None:
                ta_actor = SimpleNamespace(full_name=rec.name, username=rec.name, email=rec.email)
        except Exception:
            ta_actor = None
    notified = notify_candidate(resume.email, resume.phone, msg["subject"], msg["text"], msg["html"],
                                db=db, event="candidate.interview_link", to_name=resume.candidate_name,
                                candidate_id=candidate.id, actor=ta_actor)

    db.commit()
    return envelope(
        {
            "booking_id": booking.id,
            "status": booking.status,
            "slot_id": slot.id,
            "slot_at": slot.slot_at.isoformat() if slot.slot_at else None,
            "slot_at_text": when,
            "invite_url": bridge.get("invite_url", ""),
            "access_key": bridge.get("access_key", ""),
            "notified": notified,
        },
        message="Slot confirmed — AI interview scheduled",
    )


@router.get("/api/requirements/{requirement_id}/interview-history")
def requirement_interview_history(
    requirement_id: int,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(gated_read("requirements", "TA", "RMG", "Sales", "Sales_Head")),
):
    """Candidate-wise interview history for one requirement (28 Aug 2026, user
    request — replaces the slot list as the tab's primary view now that
    candidates propose their own time).

    Per candidate: every AI L1 link with the LEGACY session truth attached —
    session status (verified/active/completed/terminated), start/finish
    times, violation count and the violation breakdown (tab switches etc.) —
    plus the human rounds (L2 face-to-face, customer interviews). Read-only.
    """
    import json as _json

    from auth_db import get_schedules_by_tokens
    from models import AiInterviewLink, Candidate, CandidateProfile, InterviewEvent
    from services.ai_interview_bridge import _legacy_db_target
    from services.requirements import get_requirement_or_404

    req = get_requirement_or_404(db, requirement_id)
    links = db.execute(
        select(AiInterviewLink)
        .where(AiInterviewLink.opportunity_id == req.opportunity_id)
        .order_by(AiInterviewLink.created_at.desc(), AiInterviewLink.id.desc())
    ).scalars().all()

    prof_ids = [p_id for p_id in {l.profile_id for l in links} if p_id]
    profiles = db.execute(
        select(CandidateProfile).where(CandidateProfile.opportunity_id == req.opportunity_id)
    ).scalars().all()
    prof_by_id = {p.id: p for p in profiles}
    cand_ids = ({l.candidate_id for l in links if l.candidate_id}
                | {p.candidate_id for p in profiles if p.candidate_id})
    cand_by_id = {c.id: c for c in db.execute(
        select(Candidate).where(Candidate.id.in_(cand_ids))).scalars().all()} if cand_ids else {}

    sched_by_token = {}
    try:
        sched_by_token = get_schedules_by_tokens(
            _legacy_db_target(), [l.invite_token for l in links if l.invite_token])
    except Exception:
        pass

    def _iso(v):
        if v is None:
            return None
        return v.isoformat() if hasattr(v, "isoformat") else str(v)

    def _violations(raw):
        """violations_log -> {type: count}; tolerate any stored shape."""
        if not raw:
            return {}
        try:
            data = raw if isinstance(raw, list) else _json.loads(raw)
        except Exception:
            return {}
        out: dict[str, int] = {}
        if isinstance(data, list):
            for item in data:
                t = (item.get("type") or item.get("violation_type") or "other"
                     ) if isinstance(item, dict) else str(item)
                t = str(t).strip() or "other"
                out[t] = out.get(t, 0) + 1
        return out

    by_cand: dict[int, dict] = {}

    def _bucket(cand_id: int | None, profile_id: int | None) -> dict:
        key = cand_id or 0
        if key not in by_cand:
            c = cand_by_id.get(cand_id or 0)
            name = " ".join(p for p in [getattr(c, "first_name", None),
                                        getattr(c, "last_name", None)] if p) if c else None
            by_cand[key] = {
                "candidate_id": cand_id, "candidate_name": name or "Unknown candidate",
                "email": getattr(c, "email", None) if c else None,
                "profile_id": profile_id, "entries": [],
            }
        elif profile_id and not by_cand[key]["profile_id"]:
            by_cand[key]["profile_id"] = profile_id
        return by_cand[key]

    for l in links:
        sched = sched_by_token.get((l.invite_token or "").strip()) or {}
        c = cand_by_id.get(l.candidate_id or 0)
        email = (getattr(c, "email", None) or "").strip().lower()
        report = (f"/admin?view=candidateReport&cid={email}&iid={l.interview_record_id}"
                  if l.interview_record_id and email else None)
        _bucket(l.candidate_id, l.profile_id)["entries"].append({
            "type": f"AI {getattr(l, 'level', None) or 'L1'}",
            "scheduled_at": sched.get("scheduled_at_local") or _iso(l.created_at),
            "session_status": sched.get("session_status") or None,
            "status": sched.get("status") or None,
            "started_at": _iso(sched.get("interview_started_at")),
            "completed_at": _iso(sched.get("interview_completed_at")) or _iso(l.completed_at),
            "violation_count": sched.get("violation_count") or 0,
            "violations": _violations(sched.get("violations_log")),
            "score_percent": float(l.overall_score_percent)
                             if l.overall_score_percent is not None else None,
            "result": l.result,
            "effective_result": l.effective_result,
            "hr_decision": l.hr_decision,
            "report_link": report,
        })

    events = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id.in_(list(prof_by_id))
               if prof_by_id else sa_false())
        .order_by(InterviewEvent.scheduled_at.desc().nulls_last(), InterviewEvent.id.desc())
    ).scalars().all() if prof_by_id else []
    for ev in events:
        prof = prof_by_id.get(ev.profile_id)
        cand_id = ev.candidate_id or (prof.candidate_id if prof else None)
        _bucket(cand_id, ev.profile_id)["entries"].append({
            "type": {"L2_F2F": "L2 face-to-face",
                     "Customer_Interview": "Customer interview"}.get(ev.kind, ev.kind),
            "scheduled_at": _iso(ev.scheduled_at) or ev.raw_when,
            "session_status": None,
            "status": ev.status or None,
            "started_at": None, "completed_at": None,
            "violation_count": 0, "violations": {},
            "score_percent": None,
            "result": ev.result or None,
            "effective_result": ev.result or ev.status or None,
            "hr_decision": None,
            "report_link": None,
            "meeting_link": ev.meeting_link or None,
            "note": ev.note or None,
        })

    groups = sorted(by_cand.values(), key=lambda g: (g["candidate_name"] or "").lower())
    return envelope(data=groups)
