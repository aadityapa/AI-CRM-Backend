"""The candidate-facing interview invitation email.

Replaces the short "here is your link" note with a proper invitation: greeting,
a details table, a confirmation request, and a signature block for the recruiter
who actually sent it.

Two rules drive the whole module.

**Never render an empty row.** A details table with "Venue Address: —" in it
looks broken, and an AI interview has no venue at all. `_rows()` drops anything
without a value, so the table only ever shows facts we hold. That is what makes
the same template work for an online AI screening and an in-person L2 round.

**The signature is the person, not the product.** `sender_details()` resolves
the acting user's name, designation and phone from their profile, falling back
to company defaults. Combined with the Reply-To the outbox already sets, a
candidate can reply to the recruiter handling them rather than a shared inbox.
"""
from __future__ import annotations

import os
from datetime import datetime
from html import escape

# --------------------------------------------------------------- company block


#: env var -> org-settings key: values edited on the Settings page win over
#: .env, which stays as the fallback (see services.org_settings).
_SETTING_FOR_ENV = {
    "COMPANY_NAME": "org.company_name",
    "COMPANY_SHORT_NAME": "org.company_short_name",
    "COMPANY_PHONE": "org.company_phone",
    "COMPANY_EMAIL": "org.company_email",
    "COMPANY_WEBSITE": "org.company_website",
    "INTERVIEW_DEFAULT_DURATION": "interview.default_duration",
}


def _env(name: str, default: str) -> str:
    key = _SETTING_FOR_ENV.get(name)
    if key:
        try:
            from services.org_settings import setting

            return setting(key, default=default) or default
        except Exception:
            pass
    return (os.getenv(name) or "").strip() or default


def company_details() -> dict[str, str]:
    """Signature footer. Env-overridable so a rebrand is a config change.

    Defaults mirror services/company_invoice_config.py so the invitation and the
    tax invoice do not disagree about who Karnex is.
    """
    return {
        "name": _env("COMPANY_NAME", "Karnex Software Solutions PVT LTD"),
        "short_name": _env("COMPANY_SHORT_NAME", "Karnex"),
        "phone": _env("COMPANY_PHONE", _env("INVOICE_SELLER_PHONE", "")),
        "email": _env("COMPANY_EMAIL", _env("INVOICE_SELLER_CONTACT_EMAIL", "info@karnex.in")),
        "website": _env("COMPANY_WEBSITE", "https://karnex.in/"),
    }


def sender_details(db, user) -> dict[str, str]:
    """Who signs the email — the logged-in recruiter, not a generic mailbox.

    `user` is the CurrentUser. Name and email come from the login record; the
    designation, department and phone come from their UserProfile, which is
    optional — so every field degrades to a company default rather than a blank.
    """
    company = company_details()
    name = (getattr(user, "full_name", "") or getattr(user, "username", "") or "").strip()
    email = (getattr(user, "email", "") or "").strip()
    designation, department, phone = "", "", ""

    try:
        from models import UserProfile

        profile = db.query(UserProfile).filter(UserProfile.user_id == getattr(user, "id", None)).first()
        if profile is not None:
            designation = (profile.job_title or "").strip()
            department = (profile.department or "").strip()
            phone = (profile.phone or "").strip()
    except Exception:
        # A missing profile must never stop an invitation going out.
        pass

    return {
        "name": name or company["short_name"] + " Recruitment Team",
        "designation": designation,
        # Their template signs off "Human Resources"; use the sender's real
        # department when we know it, since TA and RMG also send these.
        "department": department or "Human Resources",
        "phone": phone or company["phone"],
        "email": email or company["email"],
    }


# ------------------------------------------------------------------ formatting


def format_when(raw: str | None) -> str:
    """"2026-08-10 16:24" -> "Monday, 10 August 2026 at 16:24".

    Falls back to whatever the recruiter typed if it does not parse — a slightly
    odd date in the email beats an empty one.
    """
    value = (raw or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).strftime("%A, %d %B %Y at %H:%M")
        except ValueError:
            continue
    return value


def _rows(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep only rows we can actually fill. See the module docstring."""
    return [(label, str(value).strip()) for label, value in pairs
            if value is not None and str(value).strip()]


# -------------------------------------------------------------------- template


def interview_invite_message(
    *,
    candidate_name: str,
    position: str,
    interview_level: str = "",
    interview_date: str = "",
    duration: str = "",
    interview_mode: str = "",
    meeting_link: str = "",
    venue: str = "",
    access_key: str = "",
    sender: dict[str, str] | None = None,
    company: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the invitation. Returns {"subject", "text", "html"}."""
    company = company or company_details()
    sender = sender or {
        "name": f"{company['short_name']} Recruitment Team",
        "designation": "",
        "department": "Human Resources",
        "phone": company["phone"],
        "email": company["email"],
    }

    subject = f"Interview Invitation — {position} at {company['short_name']}"

    detail_rows = _rows([
        ("Job Position", position),
        ("Interview Level", interview_level),
        ("Date & Time", interview_date),
        ("Duration", duration),
        ("Interview Mode", interview_mode),
        ("Location / Meeting Link", meeting_link),
        ("Venue Address", venue),
    ])

    # ---------------------------------------------------------------- plain text
    lines = [
        f"Dear {candidate_name},",
        "",
        f"Thank you for your interest in the {position} position at {company['name']}. "
        f"Following a review of your application, we are pleased to invite you to attend "
        f"an interview as part of our selection process.",
        "",
        "INTERVIEW DETAILS",
    ]
    width = max((len(label) for label, _ in detail_rows), default=0)
    for label, value in detail_rows:
        lines.append(f"  {label.ljust(width)}  {value}")
    if access_key:
        lines += ["", f"  {'Secure Access Key'.ljust(width)}  {access_key}",
                  "  You will need your registered email and this key to enter the interview.",
                  "  Do NOT share these credentials with anyone."]
    lines += [
        "",
        "Please confirm your availability by replying to this email. If the proposed "
        "schedule is not convenient, kindly share your availability on this email.",
        "",
        f"We appreciate your interest in {company['short_name']} and look forward to "
        f"speaking with you.",
        "",
        "Please ensure you join the meeting 5 minutes early and have a stable internet "
        "connection in case of an online meeting.",
        "",
        "Kind regards,",
        sender["name"],
    ]
    for extra in (sender.get("designation"), sender.get("department"), company["name"]):
        if (extra or "").strip():
            lines.append(extra)
    if sender.get("phone"):
        lines.append(f"Phone: {sender['phone']}")
    if sender.get("email"):
        lines.append(f"Email: {sender['email']}")
    if company.get("website"):
        lines.append(f"Web:   {company['website']}")
    text = "\n".join(lines) + "\n"

    # --------------------------------------------------------------------- html
    rows_html = "".join(
        '<tr>'
        f'<td style="padding:9px 18px 9px 0;color:#64748b;font-size:13px;'
        f'white-space:nowrap;vertical-align:top;border-bottom:1px solid #f1f5f9;">{escape(label)}</td>'
        f'<td style="padding:9px 0;color:#0f172a;font-size:14px;font-weight:600;'
        f'border-bottom:1px solid #f1f5f9;word-break:break-word;">{_value_html(label, value)}</td>'
        '</tr>'
        for label, value in detail_rows
    )

    key_html = ""
    if access_key:
        key_html = f"""
      <div style="margin:18px 0;padding:14px 18px;background:#f8fafc;border:2px dashed #4f46e5;border-radius:12px;">
        <p style="margin:0 0 4px;font-size:11px;text-transform:uppercase;letter-spacing:0.08em;color:#64748b;font-weight:700;">Secure Access Key</p>
        <p style="margin:0;font-size:20px;font-weight:800;letter-spacing:0.15em;color:#0f172a;font-family:Consolas,monospace;">{escape(access_key)}</p>
        <p style="margin:8px 0 0;font-size:12px;color:#64748b;">You will need your registered email and this key to enter. Please do not share them.</p>
      </div>"""

    sig_lines = "".join(
        f'<div style="font-size:13px;color:#475569;">{escape(v)}</div>'
        for v in (sender.get("designation"), sender.get("department"), company["name"])
        if (v or "").strip()
    )
    contact_lines = ""
    if sender.get("phone"):
        contact_lines += f'<div style="font-size:13px;color:#475569;">&#128222; {escape(sender["phone"])}</div>'
    if sender.get("email"):
        contact_lines += (f'<div style="font-size:13px;color:#475569;">&#128231; '
                          f'<a href="mailto:{escape(sender["email"])}" style="color:#4f46e5;text-decoration:none;">'
                          f'{escape(sender["email"])}</a></div>')
    if company.get("website"):
        contact_lines += (f'<div style="font-size:13px;color:#475569;">&#127760; '
                          f'<a href="{escape(company["website"])}" style="color:#4f46e5;text-decoration:none;">'
                          f'{escape(company["website"])}</a></div>')

    html = f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;line-height:1.6;color:#1e293b;max-width:620px;">
  <div style="padding:0 0 14px;border-bottom:2px solid #e2e8f0;margin-bottom:20px;">
    <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#0f172a;">KARNEX</span>
    <span style="font-weight:800;font-size:20px;letter-spacing:-0.02em;color:#4f46e5;"> Careers</span>
  </div>

  <p style="margin:0 0 14px;font-size:15px;">Dear <strong>{escape(candidate_name)}</strong>,</p>

  <p style="margin:0 0 18px;font-size:14px;color:#334155;">
    Thank you for your interest in the <strong>{escape(position)}</strong> position at
    {escape(company['name'])}. Following a review of your application, we are pleased to
    invite you to attend an interview as part of our selection process.
  </p>

  <p style="margin:0 0 6px;font-size:12px;text-transform:uppercase;letter-spacing:0.08em;color:#4f46e5;font-weight:700;">Interview Details</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="width:100%;border-collapse:collapse;margin:0 0 4px;">
    {rows_html}
  </table>
  {key_html}

  <p style="margin:18px 0 0;font-size:14px;color:#334155;">
    Please confirm your availability by replying to this email. If the proposed schedule is
    not convenient, kindly share your availability on this email.
  </p>
  <p style="margin:12px 0 0;font-size:14px;color:#334155;">
    We appreciate your interest in {escape(company['short_name'])} and look forward to speaking with you.
  </p>
  <p style="margin:12px 0 0;font-size:13px;color:#64748b;">
    Please ensure you join the meeting 5 minutes early and have a stable internet connection
    in case of an online meeting.
  </p>

  <div style="margin-top:26px;padding-top:16px;border-top:1px solid #e2e8f0;">
    <div style="font-size:14px;color:#334155;margin-bottom:6px;">Kind regards,</div>
    <div style="font-size:15px;font-weight:700;color:#0f172a;">{escape(sender['name'])}</div>
    {sig_lines}
    <div style="margin-top:8px;">{contact_lines}</div>
  </div>
</div>
""".strip()

    return {"subject": subject, "text": text, "html": html}


def _value_html(label: str, value: str) -> str:
    """Make the meeting link clickable; everything else is plain escaped text."""
    if label.lower().startswith("location") and value.lower().startswith("http"):
        return (f'<a href="{escape(value)}" style="color:#4f46e5;text-decoration:none;'
                f'word-break:break-all;">{escape(value)}</a>')
    return escape(value)


# ------------------------------------------------------------------- assembling


def build_ai_interview_invite(
    db,
    user,
    *,
    candidate_name: str,
    position: str,
    level: str = "L1",
    scheduled_at_raw: str = "",
    invite_url: str = "",
    access_key: str = "",
) -> dict[str, str]:
    """The AI screening invitation: always online, no venue.

    Duration comes from INTERVIEW_DEFAULT_DURATION when set. The AI bridge runs
    count-mode with `time_limit_sec: 0` — there is no fixed length to report — so
    rather than invent one, an unset value simply drops the row.
    """
    level_label = {"L1": "L1 — AI Screening Interview",
                   "L2": "L2 — Technical Interview"}.get((level or "").upper(), level or "")
    return interview_invite_message(
        candidate_name=candidate_name,
        position=position,
        interview_level=level_label,
        interview_date=format_when(scheduled_at_raw),
        duration=_env("INTERVIEW_DEFAULT_DURATION", ""),
        interview_mode="Online — AI Interview (browser based)",
        meeting_link=invite_url,
        venue="",
        access_key=access_key,
        sender=sender_details(db, user),
    )
