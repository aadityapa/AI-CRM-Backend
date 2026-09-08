"""Candidate-facing communications: email (SMTP) + WhatsApp (stub) + templates.

Everything here is BEST-EFFORT and never raises: pipeline flows (ATS auto-invite,
public slot confirmation, manual scheduling) must complete even when no channel
is configured. Each send returns {"sent": bool, "error": str|None} so callers
can surface per-channel results in API responses and activity logs.
"""
from __future__ import annotations

import logging
import os
from html import escape

from email_smtp import send_email, smtp_configured

logger = logging.getLogger("karnex.crm.candidate_comms")


# --------------------------------------------------------------------- email

def send_candidate_email(to: str, subject: str, text: str, html: str | None = None,
                         attachments: list[tuple[str, bytes, str]] | None = None,
                         *, db=None, event: str = "candidate", actor=None,
                         to_name: str = "", candidate_id: int | None = None) -> dict:
    """Send one candidate email. Never raises. Sent as the logged-in user when
    the caller passes no `actor` (services/actor_context.py).

    DURABLE PATH: pass `db` (the caller's session) and the message is QUEUED on
    the email outbox instead of sent inline — it gets the same five retries
    with backoff and the same audit row in the Email Outbox screen that staff
    notifications get, and it only goes out if the caller's transaction
    commits. This closed the old gap where a ZeptoMail hiccup at the wrong
    moment silently lost a candidate's interview invitation with no record.

    INLINE PATH (no `db`, or `attachments` present): the original direct SMTP
    send. Attachments stay inline because the outbox table stores text bodies
    only — today that is exactly one email, the L2 calendar invite (.ics).

    Returns {"sent": bool, "error": str|None} either way; the durable path
    adds {"queued": True} so activity logs can say which road it took.
    """
    if actor is None:
        from services.actor_context import current_actor
        actor = current_actor()
    try:
        to = (to or "").strip()
        if not to:
            return {"sent": False, "error": "no_email"}
        if db is not None and not attachments:
            try:
                from services.email_outbox import queue_email

                row = queue_email(
                    db,
                    to_email=to,
                    to_name=to_name,
                    subject=subject,
                    body_text=text,
                    body_html=html,
                    event=event or "candidate",
                    actor=actor,
                    # WHO this mail is about (26 Aug 2026) — the Emails tab
                    # groups conversations by candidate id, not by address:
                    # shared/test addresses were merging different candidates.
                    related_type="candidate" if candidate_id else None,
                    related_id=candidate_id,
                )
                if row is not None:
                    return {"sent": True, "error": None, "queued": True}
                # Queueing declined (e.g. email disabled in settings) — report
                # honestly rather than silently double-sending inline.
                return {"sent": False, "error": "email_disabled", "queued": False}
            except Exception as exc:
                # The outbox must never take a candidate flow down with it —
                # fall back to the old inline send.
                logger.warning("candidate email queue failed for %s (%s); sending inline", to, exc)
        if not smtp_configured():
            return {"sent": False, "error": "smtp_disabled"}
        # Inline sends carry the SAME sender identity as outbox mail (7 Sep
        # 2026, user request: the candidate's invite must come from the TA
        # who booked it): From shows "<TA name> (Karnex)" and Reply-To is the
        # TA's own address, so a reply lands in their mailbox. The envelope
        # sender stays SMTP_FROM — Office 365 refuses Send-As otherwise.
        from services.recipients import is_real_email as _real
        actor_name = (getattr(actor, "full_name", "") or getattr(actor, "username", "") or "").strip()
        actor_email = (getattr(actor, "email", "") or "").strip()
        result = send_email(
            to, subject, text, html, attachments=attachments,
            from_name=f"{actor_name} (Karnex)" if actor_name else None,
            reply_to=actor_email if _real(actor_email) else None,
            reply_to_name=actor_name or None,
        )
        if result.get("ok"):
            return {"sent": True, "error": None}
        return {"sent": False, "error": str(result.get("error") or "send_failed")}
    except Exception as exc:  # defensive: comms must never break the pipeline
        logger.warning("candidate email failed for %s: %s", to, exc)
        return {"sent": False, "error": str(exc)}


# ----------------------------------------------------------------- ics invite

def build_ics_invite(summary: str, starts_at, description: str = "", location: str = "",
                     uid: str = "karnex-interview", duration_minutes: int = 60) -> str:
    """Minimal RFC-5545 VCALENDAR for one interview event (floating local time,
    matching how RMG typed it). Attach as ("invite.ics", bytes, "text/calendar")."""
    from datetime import timedelta

    def _fmt(dt) -> str:
        return dt.strftime("%Y%m%dT%H%M%S")

    def _esc(v: str) -> str:
        return (v or "").replace("\\", "\\\\").replace(";", r"\;").replace(",", r"\,").replace("\n", r"\n")

    ends_at = starts_at + timedelta(minutes=duration_minutes)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Karnex//AI HR Suite//EN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{uid}@karnex",
        f"DTSTAMP:{_fmt(starts_at)}",
        f"DTSTART:{_fmt(starts_at)}",
        f"DTEND:{_fmt(ends_at)}",
        f"SUMMARY:{_esc(summary)}",
    ]
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    lines += ["STATUS:CONFIRMED", "END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


# ------------------------------------------------------------------ whatsapp

def send_candidate_whatsapp(phone: str, message: str) -> dict:
    """STUB WhatsApp channel — provider integration point.

    Provider interface (drop keys in, implement one branch, done):

    * Meta WhatsApp Cloud API — set WHATSAPP_PROVIDER=meta plus
      WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN, then POST
      https://graph.facebook.com/v19.0/{WHATSAPP_PHONE_NUMBER_ID}/messages
      with JSON {"messaging_product": "whatsapp", "to": <E.164 phone>,
      "type": "text", "text": {"body": message}} and header
      "Authorization: Bearer {WHATSAPP_ACCESS_TOKEN}".

    * Twilio WhatsApp — set WHATSAPP_PROVIDER=twilio plus TWILIO_ACCOUNT_SID,
      TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM (e.g. "whatsapp:+14155238886"),
      then POST https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json
      (basic auth SID:token) with form fields From=TWILIO_WHATSAPP_FROM,
      To=f"whatsapp:{phone}", Body=message.

    Until a provider is wired, this returns
    {"sent": False, "error": "whatsapp_not_configured"} (env unset) or
    {"sent": False, "error": "whatsapp_provider_<name>_not_implemented"}.
    Never raises.
    """
    try:
        phone = (phone or "").strip()
        if not phone:
            return {"sent": False, "error": "no_phone"}
        provider = (os.getenv("WHATSAPP_PROVIDER") or "").strip().lower()
        if not provider:
            return {"sent": False, "error": "whatsapp_not_configured"}
        # Provider branches go here (see docstring). Stubbed on purpose.
        logger.info("whatsapp stub: provider=%s to=%s (not implemented)", provider, phone)
        return {"sent": False, "error": f"whatsapp_provider_{provider}_not_implemented"}
    except Exception as exc:
        return {"sent": False, "error": str(exc)}


# ------------------------------------------------------------- multi-channel

def notify_candidate(email: str | None, phone: str | None, subject: str,
                     message_text: str, message_html: str | None = None,
                     *, db=None, event: str = "candidate", actor=None,
                     to_name: str = "", candidate_id: int | None = None) -> dict:
    """Try email first, then WhatsApp. Returns per-channel results:
    {"email": {"sent": bool, "error": ...}, "whatsapp": {"sent": bool, "error": ...}}.

    Pass `db` to route the email through the durable outbox (retries + audit)
    — see send_candidate_email. WhatsApp stays inline either way.
    """
    email_result = (
        send_candidate_email(email, subject, message_text, message_html,
                             db=db, event=event, actor=actor, to_name=to_name,
                             candidate_id=candidate_id)
        if (email or "").strip() else {"sent": False, "error": "no_email"}
    )
    whatsapp_result = (
        send_candidate_whatsapp(phone, message_text)
        if (phone or "").strip() else {"sent": False, "error": "no_phone"}
    )
    return {"email": email_result, "whatsapp": whatsapp_result}


# ----------------------------------------------------------------- templates

def _branded_html(title: str, body_html: str) -> str:
    """Simple branded wrapper (inline styles only — email-client safe)."""
    return f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#1e293b;max-width:560px;">
      <div style="padding:14px 0;border-bottom:2px solid #e2e8f0;margin-bottom:16px;">
        <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#0f172a;">KARNEX</span>
        <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#4f46e5;"> Careers</span>
      </div>
      <p style="font-size:16px;font-weight:700;margin:0 0 10px;">{escape(title)}</p>
      {body_html}
      <p style="color:#64748b;font-size:13px;margin-top:20px;">— Karnex Recruitment Team</p>
    </div>
    """


def candidate_template_override(db, event: str, subject: str, text: str,
                                tokens: dict) -> tuple[str, str, bool]:
    """Admin-authored draft (Settings → Email Drafts) for a candidate email.

    Returns (subject, text, overridden). Single-brace {token} replace — plain
    replace of known tokens only, so a typo'd token shows up literally instead
    of swallowing the mail. Falls back to the code-composed text on ANY error;
    savepoint-protected so a lookup failure cannot poison the transaction.
    """
    if db is None or not event:
        return subject, text, False
    try:
        from sqlalchemy import text as _sql
        with db.begin_nested():
            row = db.execute(
                _sql("SELECT subject_template, body_template FROM notification_routes "
                     "WHERE event = :e"), {"e": event}).first()
        if not row or (not row[0] and not row[1]):
            return subject, text, False
        subj = row[0] or subject
        body = row[1] or text
        try:
            from services.org_settings import get_setting
            tokens = {**tokens, "company": tokens.get("company")
                      or get_setting(db, "org.company_name") or "Karnex"}
        except Exception:
            tokens = {**tokens, "company": tokens.get("company") or "Karnex"}
        for k, v in tokens.items():
            rep = "" if v is None else str(v)
            key = k if k.startswith("{") else "{" + k + "}"
            subj = subj.replace(key, rep)
            body = body.replace(key, rep)
        return subj, body, True
    except Exception:
        return subject, text, False


def _plain_html(heading: str, text: str) -> str:
    """Branded HTML from an admin-authored plain-text draft (nl2br, escaped)."""
    paragraphs = "".join(
        f"<p>{escape(part).replace(chr(10), '<br>')}</p>"
        for part in text.split("\n\n") if part.strip()
    )
    return _branded_html(heading, paragraphs)


def slot_invite_message(candidate_name: str, role_title: str, booking_url: str,
                        db=None) -> dict:
    """Invite the candidate to pick an interview slot via the public booking link.

    Returns {"subject": str, "text": str, "html": str}.
    """
    subject = f"Karnex — pick your interview slot for {role_title}"
    text = (
        f"Hello {candidate_name},\n\n"
        f"Congratulations! Your application for \"{role_title}\" has been shortlisted, "
        f"and we would like to invite you to the next step: a short AI-led interview.\n\n"
        f"Please choose an interview slot that works for you:\n"
        f"{booking_url}\n\n"
        f"Once you confirm a slot you will receive your interview link and a secure "
        f"access key by email. Please join from a quiet place with a working webcam "
        f"and microphone.\n\n"
        f"— Karnex Recruitment Team\n"
    )
    # The button is the link (2 Sep 2026, user request) — the raw URL under it
    # was noise and looked like a second, different link. The plain-text
    # fallback above still carries the URL for clients that strip HTML.
    html = _branded_html(
        "You have been shortlisted!",
        f"""
      <p>Hello <strong>{escape(candidate_name)}</strong>,</p>
      <p>Congratulations — your application for <strong>{escape(role_title)}</strong> has been
         shortlisted, and we would like to invite you to the next step: a short AI-led
         interview.</p>
      <p>Please choose an interview slot that works for you:</p>
      <p><a href="{escape(booking_url)}" style="display:inline-block;padding:12px 18px;background:#4f46e5;color:#ffffff;border-radius:10px;text-decoration:none;font-weight:700;">Choose my interview slot</a></p>
      <p style="font-size:13px;color:#64748b;">Once you confirm a slot you will receive your
         interview link and a secure access key by email. Please join from a quiet place with a
         working webcam and microphone.</p>
        """,
    )
    subject, text, overridden = candidate_template_override(
        db, "candidate.slot_invite", subject, text,
        {"candidate": candidate_name, "role": role_title, "link": booking_url})
    if overridden:
        html = _plain_html(subject, text)
    return {"subject": subject, "text": text, "html": html}


def interview_link_message(candidate_name: str, role_title: str, when_text: str,
                           invite_url: str, access_key: str, db=None) -> dict:
    """Deliver the AI interview link (+ access key) to the candidate.

    Returns {"subject": str, "text": str, "html": str}.
    """
    subject = f"Karnex — your AI interview link for {role_title}"
    text = (
        f"Hello {candidate_name},\n\n"
        f"Your AI interview for \"{role_title}\" is scheduled.\n"
        f"When: {when_text or 'See your recruiter'}\n\n"
        f"Open this link to start your interview:\n{invite_url}\n\n"
    )
    if access_key:
        text += (
            f"Your Secure Access Key: {access_key}\n"
            f"You will need your registered email and this key to enter the interview.\n"
            f"Do NOT share these credentials with anyone.\n\n"
        )
    text += "— Karnex Recruitment Team\n"

    key_html = ""
    if access_key:
        key_html = f"""
      <div style="margin:16px 0;padding:14px 18px;background:#f1f5f9;border:2px dashed #4f46e5;border-radius:12px;">
        <p style="margin:0 0 4px;font-size:12px;text-transform:uppercase;letter-spacing:0.05em;color:#64748b;font-weight:700;">Secure Access Key</p>
        <p style="margin:0;font-size:20px;font-weight:900;letter-spacing:0.15em;color:#1e293b;font-family:monospace;">{escape(access_key)}</p>
      </div>
      <p style="font-size:13px;color:#ef4444;font-weight:600;">Do NOT share your access key or interview link with anyone.</p>
        """
    html = _branded_html(
        "Your AI interview is scheduled",
        f"""
      <p>Hello <strong>{escape(candidate_name)}</strong>,</p>
      <p>Your AI interview for <strong>{escape(role_title)}</strong> is scheduled.</p>
      <p><strong>When:</strong> {escape(when_text or 'See your recruiter')}</p>
      {key_html}
      <p><a href="{escape(invite_url)}" style="display:inline-block;padding:12px 18px;background:#2563eb;color:#ffffff;border-radius:10px;text-decoration:none;font-weight:700;">Open my interview</a></p>
      <p style="word-break:break-all;font-size:13px;color:#64748b;">{escape(invite_url)}</p>
        """,
    )
    subject, text, overridden = candidate_template_override(
        db, "candidate.ai_invite", subject, text,
        {"candidate": candidate_name, "role": role_title,
         "when": when_text or "See your recruiter", "link": invite_url,
         "access_key": access_key or "", **ai_invite_signature_tokens(None)})
    if overridden:
        html = _plain_html(subject, text)
    return {"subject": subject, "text": text, "html": html}


def ai_invite_signature_tokens(sender: dict | None) -> dict:
    """The {sender…} / {company…} placeholders the AI-invite draft may use,
    filled from the acting recruiter (or the company defaults when there is
    no acting user — public slot confirmation, legacy paths)."""
    try:
        from services.interview_invite_email import company_details
        company = company_details()
    except Exception:
        company = {"name": "Karnex", "short_name": "Karnex", "phone": "", "email": "", "website": ""}
    s = sender or {"name": f"{company['short_name']} Recruitment Team", "designation": "",
                   "department": "Human Resources", "phone": company["phone"],
                   "email": company["email"]}
    return {
        "level": "L1 — AI Screening Interview",
        "duration": "",
        "sender": s.get("name") or "",
        "sender_designation": s.get("designation") or "",
        "sender_department": s.get("department") or "",
        "sender_phone": s.get("phone") or "",
        "sender_email": s.get("email") or "",
        "company": company.get("short_name") or "Karnex",
        "company_name": company.get("name") or "Karnex",
        "company_website": company.get("website") or "",
    }


# ------------------------------------------------ human interview rounds

def internal_round_invite_message(candidate_name: str, round_label: str, team_label: str,
                                  interviewer: str | None, when: str | None,
                                  link: str | None, note: str | None) -> tuple[str, str]:
    """Built-in wording for the internal L1 / L2 / HR round invitation
    (subject, plain text). Lifted verbatim from the scheduling endpoint so the
    Email Drafts tab can show the same words the candidate receives."""
    subject = f"Interview invitation — {round_label} round"
    body = (
        f"Hello {candidate_name},\n\n"
        f"Your interview round ({round_label}) has been scheduled with our "
        f"{team_label} team.\n"
        + (f"Interviewer: {interviewer}\n" if interviewer else "")
        + (f"When: {when}\n" if when else "")
        + (f"Meeting link: {link}\n" if link else "")
        + (f"\n{note}\n" if note else "")
        + "\n— Karnex Recruitment Team\n"
    )
    return subject, body


def customer_round_invite_message(candidate_name: str, round_label: str, when: str,
                                  link: str, note: str | None) -> tuple[str, str]:
    """Built-in wording for a customer-side round (subject, plain text)."""
    subject = f"Interview invitation — {round_label}"
    body = (
        f"Hello {candidate_name},\n\n"
        f"Your {round_label} has been scheduled.\n"
        f"When: {when}\n"
        f"Meeting link: {link}\n"
        + (f"\n{note}\n" if note else "")
        + "\nPlease join a few minutes early. Reply to this email if the time does not work.\n"
        "\n— Karnex Recruitment Team\n"
    )
    return subject, body


# ------------------------------------------------ the built-in drafts, on show

def builtin_candidate_draft(event: str) -> dict | None:
    """The CURRENT built-in subject/body for a candidate email, placeholders
    left as `{token}` — what Settings → Email Drafts shows in the editor so an
    admin edits real text instead of a blank box (3 Sep 2026, user request).

    Built by running the very same builders the send paths use, with the
    placeholder names as the values, so this can never drift from what is
    actually sent. Returns None for events with no fixed wording (e.g. the
    free-form direct message)."""
    try:
        if event == "candidate.slot_invite":
            m = slot_invite_message("{candidate}", "{role}", "{link}", db=None)
            return {"subject": m["subject"], "body": m["text"]}
        if event == "candidate.ai_invite":
            from services.interview_invite_email import _env, interview_invite_message
            # The Duration row only exists when INTERVIEW_DEFAULT_DURATION is
            # set (the builder drops empty rows) — mirror that in the draft.
            duration = "{duration}" if _env("INTERVIEW_DEFAULT_DURATION", "") else ""
            m = interview_invite_message(
                candidate_name="{candidate}", position="{role}", interview_level="{level}",
                interview_date="{when}", duration=duration,
                interview_mode="Online — AI Interview (browser based)",
                meeting_link="{link}", venue="", access_key="{access_key}",
                sender={"name": "{sender}", "designation": "{sender_designation}",
                        "department": "{sender_department}", "phone": "{sender_phone}",
                        "email": "{sender_email}"},
                company={"name": "{company_name}", "short_name": "{company}",
                         "phone": "", "email": "", "website": "{company_website}"},
            )
            return {"subject": m["subject"], "body": m["text"]}
        if event in ("candidate.l1_manual_invite", "candidate.l2_invite"):
            label = "L1" if event.endswith("l1_manual_invite") else "L2"
            s, b = internal_round_invite_message("{candidate}", label, "engineering",
                                                 "{interviewer}", "{when}", "{link}", "{note}")
            return {"subject": s, "body": b}
        if event == "candidate.hr_invite":
            s, b = internal_round_invite_message("{candidate}", "HR", "HR",
                                                 "{interviewer}", "{when}", "{link}", "{note}")
            return {"subject": s, "body": b}
        if event == "candidate.round_invite":
            s, b = customer_round_invite_message("{candidate}", "{round}", "{when}", "{link}", "{note}")
            return {"subject": s, "body": b}
        if event == "candidate.hiring_interest":
            from routers.crm.opportunities import CANDIDATE_EMAIL_BODY, CANDIDATE_EMAIL_SUBJECT
            return {"subject": CANDIDATE_EMAIL_SUBJECT, "body": CANDIDATE_EMAIL_BODY}
    except Exception:  # pragma: no cover — the editor degrades to an empty box
        logger.warning("builtin draft for %s failed", event, exc_info=True)
    return None
