"""The "we have an opening — are you interested?" email (1 Oct 2026, user ask).

When TA bulk-uploads CVs onto an opportunity, every candidate whose CV gave a
real email address gets ONE professional note: the role, the experience band,
where and how the work happens, the key skills — and a request to reply with
their interest, CTC, notice period, location and a time for a call. The reply
goes to the TA's own mailbox (Reply-To = the TA who uploaded; the company
mailbox when the login has no address).

TA then confirms the details on the Applied Candidates row and presses
"Interested" — which records the reply and hands the candidate to RMG / GM for
Technical Screening — or "Not interested", which closes the candidacy as Self
Withdrawn. Every step is an activity row (`SENT_ACTION`, `INTERESTED_ACTION`,
`NOT_INTERESTED_ACTION`), so the state needs no migration and stays audited.

The wording is an admin-editable draft (Settings ▸ Email Drafts, event
`EVENT`, single-brace {tokens}); the outbox gives the usual five retries and
the Email Outbox audit row. Placeholder / malformed addresses are never mailed
(`mailable_email`). Nothing here raises into the caller: a mail that cannot go
returns a status the upload reports.
"""
from __future__ import annotations

import logging
from html import escape
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CandidateProfileActivityLog
from services.crm_common import log_activity

logger = logging.getLogger("karnex.crm.opening_interest")

#: Outbox / Email Drafts event (listed in `routers/crm/email_flows.EVENTS`).
EVENT = "candidate.opening_interest"
SENT_ACTION = "OPENING_MAIL_SENT"
INTERESTED_ACTION = "CANDIDATE_INTERESTED"
NOT_INTERESTED_ACTION = "CANDIDATE_NOT_INTERESTED"
_STATE_BY_ACTION = {
    SENT_ACTION: "sent",
    INTERESTED_ACTION: "interested",
    NOT_INTERESTED_ACTION: "not_interested",
}
#: The tokens an admin draft may use (Settings ▸ Email Drafts chips).
TOKENS = ("candidate", "first_name", "role", "experience", "location", "work_mode", "skills",
          "sender", "sender_designation", "sender_phone", "sender_email", "company",
          "company_name", "company_website")
#: Key skills printed in the mail — enough to judge the fit, short enough to read.
MAX_SKILLS = 6


def mailable_email(raw) -> str:
    """The address this mail may go to, or "" (blank, placeholder, malformed)."""
    from services.resume_parse import clean_email
    return clean_email(raw)


# ------------------------------------------------------------------ the facts

def _band(lo, hi) -> str:
    def fmt(v) -> str:
        f = float(v)
        return str(int(f)) if f.is_integer() else f"{f:g}"
    if lo is not None and hi is not None:
        return f"{fmt(lo)}–{fmt(hi)} years"
    if lo is not None:
        return f"{fmt(lo)}+ years"
    if hi is not None:
        return f"Up to {fmt(hi)} years"
    return ""


def opening_facts(db: Session, req) -> dict:
    """What the candidate is told about the position. The customer is NOT
    named — a first-touch mail about a client's opening never discloses the
    client (standard practice; TA shares it on the call)."""
    from models import Location, RequirementSkill, Skill

    location = ""
    if getattr(req, "location_id", None):
        loc = db.get(Location, req.location_id)
        if loc is not None:
            location = ", ".join(x for x in (loc.city, loc.state) if x)
    if not location:
        opp = getattr(req, "opportunity", None)
        location = str(((getattr(opp, "details", None) or {}).get("tm_work_location")) or "").strip()
    mode = getattr(req, "work_mode", None)
    mode = (getattr(mode, "value", mode) or "").replace("_", " ")
    skills = [name for name, _ in db.execute(
        select(Skill.name, RequirementSkill.is_mandatory)
        .join(RequirementSkill, RequirementSkill.skill_id == Skill.id)
        .where(RequirementSkill.requirement_id == req.id)
        .order_by(RequirementSkill.is_mandatory.desc(), Skill.name)
    ).all()][:MAX_SKILLS]
    return {
        "role": (req.title or "").strip() or "an open position",
        "experience": _band(req.experience_min, req.experience_max),
        "location": location,
        "work_mode": mode,
        "skills": ", ".join(skills),
    }


def sender_for(db: Session, user) -> dict:
    """Who signs the mail: the TA (name, designation, phone, email), with the
    company's details where the login has none. Savepointed — the profile read
    must never poison an upload's transaction."""
    from services.interview_invite_email import company_details, sender_details

    company = company_details()
    try:
        with db.begin_nested():
            sender = sender_details(db, user)
    except Exception:
        sender = {"name": getattr(user, "full_name", "") or company["short_name"] + " Recruitment Team",
                  "designation": "", "department": "", "phone": company["phone"],
                  "email": getattr(user, "email", "") or company["email"]}
    return {**sender, "company": company["short_name"], "company_name": company["name"],
            "company_website": company["website"]}


# ------------------------------------------------------------------ the mail

def opening_message(candidate_name: str, facts: dict, sender: dict, db=None) -> dict:
    """{"subject", "text", "html"} — the built-in wording, or the admin's draft
    from Settings ▸ Email Drafts when one is saved. PURE apart from that read."""
    from services.candidate_comms import _branded_html, _plain_html, candidate_template_override

    first = (candidate_name or "").strip().split(" ")[0] or "there"
    company = sender.get("company") or "Karnex"
    rows = [(label, facts.get(key) or "") for label, key in (
        ("Position", "role"), ("Experience", "experience"), ("Location", "location"),
        ("Work mode", "work_mode"), ("Key skills", "skills"))]
    rows = [(label, value) for label, value in rows if value]
    asks = [
        "Whether you are interested (Yes / No)",
        "Your current and expected CTC",
        "Your notice period (or last working day, if you are serving notice)",
        "Your current location and preferred work location",
        "A convenient time for a short call",
    ]
    signature = [sender.get("name") or f"{company} Recruitment Team"]
    if sender.get("designation"):
        signature.append(sender["designation"])
    signature.append(f"Talent Acquisition, {sender.get('company_name') or company}")
    contact = " | ".join(x for x in (sender.get("phone"), sender.get("email")) if x)
    if contact:
        signature.append(contact)
    if sender.get("company_website"):
        signature.append(sender["company_website"])

    subject = f"Job opportunity: {facts.get('role') or 'an open position'} — are you interested?"
    width = max((len(label) for label, _ in rows), default=0) + 2
    text = (
        f"Dear {first},\n\n"
        "I hope you are doing well.\n\n"
        f"I am {sender.get('name') or 'writing'} from the Talent Acquisition team at {company}. "
        "We came across your profile and believe it is a good fit for a current opening "
        "with one of our clients:\n\n"
        + "".join(f"  {(label + ':').ljust(width)}{value}\n" for label, value in rows)
        + "\nIf this opportunity interests you, please reply to this email with:\n"
        + "".join(f"  {i}. {a}\n" for i, a in enumerate(asks, start=1))
        + "\nIf the timing is not right, a short \"not interested\" reply is equally welcome "
          "and we will not contact you about this role again.\n\n"
          "Looking forward to hearing from you.\n\n"
          "Kind regards,\n" + "\n".join(signature) + "\n"
    )
    table = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#64748b;font-size:13px;white-space:nowrap;'
        f'vertical-align:top;">{escape(label)}</td>'
        f'<td style="padding:6px 0;font-weight:600;color:#0f172a;">{escape(value)}</td></tr>'
        for label, value in rows)
    html = _branded_html(
        "A role that matches your profile",
        f"""
      <p>Dear <strong>{escape(first)}</strong>,</p>
      <p>I hope you are doing well. I am {escape(sender.get('name') or '')} from the Talent
         Acquisition team at {escape(company)}. We came across your profile and believe it is a
         good fit for a current opening with one of our clients:</p>
      <table style="border-collapse:collapse;margin:8px 0 14px;padding:10px 14px;background:#f8fafc;
                    border:1px solid #e2e8f0;border-radius:10px;">{table}</table>
      <p>If this opportunity interests you, please <strong>reply to this email</strong> with:</p>
      <ol style="margin:0 0 12px;padding-left:20px;">{''.join(f'<li>{escape(a)}</li>' for a in asks)}</ol>
      <p style="font-size:13px;color:#64748b;">If the timing is not right, a short "not interested"
         reply is equally welcome and we will not contact you about this role again.</p>
      <p>Kind regards,<br>{'<br>'.join(escape(s) for s in signature)}</p>
        """,
    )
    tokens = {
        "candidate": candidate_name or first, "first_name": first,
        "role": facts.get("role") or "", "experience": facts.get("experience") or "",
        "location": facts.get("location") or "", "work_mode": facts.get("work_mode") or "",
        "skills": facts.get("skills") or "", "sender": sender.get("name") or "",
        "sender_designation": sender.get("designation") or "",
        "sender_phone": sender.get("phone") or "", "sender_email": sender.get("email") or "",
        "company": company, "company_name": sender.get("company_name") or company,
        "company_website": sender.get("company_website") or "",
    }
    subject, text, overridden = candidate_template_override(db, EVENT, subject, text, tokens)
    if overridden:
        html = _plain_html(subject, text)
    return {"subject": subject, "text": text, "html": html}


def already_sent(db: Session, profile_id: int) -> bool:
    return db.execute(
        select(CandidateProfileActivityLog.id).where(
            CandidateProfileActivityLog.profile_id == profile_id,
            CandidateProfileActivityLog.action_type == SENT_ACTION).limit(1)
    ).first() is not None


def send_opening_mail(db: Session, profile, candidate, req, user, *,
                      facts: dict | None = None, sender: dict | None = None,
                      resend: bool = False, to_email: str | None = None) -> dict:
    """Queue the mail for one candidacy. Returns {"status", "email"} where
    status is sent · no_email · already_sent · not_sent (email off / outbox
    declined). Writes `SENT_ACTION` when queued. Never raises; the caller
    commits (the mail leaves only if that commit happens).

    `to_email` — the address read from THIS CV, which wins over the candidate
    record's (a record reused by name may carry an older address)."""
    from services.candidate_comms import send_candidate_email

    to = mailable_email(to_email) or mailable_email(getattr(candidate, "email", None))
    if not to:
        return {"status": "no_email", "email": None}
    try:
        if not resend and already_sent(db, profile.id):
            return {"status": "already_sent", "email": to}
        facts = facts or opening_facts(db, req)
        sender = sender or sender_for(db, user)
        name = " ".join(p for p in (getattr(candidate, "first_name", None),
                                    getattr(candidate, "last_name", None)) if p).strip()
        msg = opening_message(name, facts, sender, db=db)
        # Reply-To is the actor's address: the TA's, else the company mailbox,
        # so the candidate's "Yes" always lands with a person.
        actor = SimpleNamespace(
            id=getattr(user, "id", None),
            full_name=getattr(user, "full_name", "") or getattr(user, "username", ""),
            username=getattr(user, "username", ""),
            email=sender.get("email") or "",
        )
        res = send_candidate_email(
            to, msg["subject"], msg["text"], msg["html"], db=db, event=EVENT, actor=actor,
            to_name=name, candidate_id=getattr(candidate, "id", None),
            dedupe_key=None if resend else f"opening_interest:{profile.id}",
        )
        if not res.get("sent"):
            return {"status": "not_sent", "email": to, "error": res.get("error")}
        who = actor.full_name or "TA"
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, actor.id, SENT_ACTION,
                     f"{'Re-sent' if resend else 'Sent'} the opening email to {to} ({who}) — "
                     "waiting for the candidate's reply")
        return {"status": "sent", "email": to}
    except Exception:
        logger.warning("opening mail failed for profile %s", getattr(profile, "id", None), exc_info=True)
        return {"status": "not_sent", "email": to, "error": "failed"}


def record_reply(db: Session, profile, interested: bool, note: str, user) -> None:
    """Log the candidate's answer to the opening email. The caller moves the
    candidacy on (Technical Screening / Self Withdrawn)."""
    who = getattr(user, "full_name", None) or getattr(user, "username", None) or "TA"
    log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, getattr(user, "id", None),
                 INTERESTED_ACTION if interested else NOT_INTERESTED_ACTION,
                 f"{who} recorded that the candidate is {'interested' if interested else 'not interested'}"
                 + (f": {note}" if note else ""))


def opening_states(db: Session, profile_ids) -> dict[int, dict]:
    """{profile id: {"state": sent|interested|not_interested, "at", "note"}} —
    the LATEST of the three rows per profile, one query for a page."""
    ids = [int(i) for i in (profile_ids or ()) if i is not None]
    if not ids:
        return {}
    out: dict[int, dict] = {}
    for pid, action, comment, ts in db.execute(
        select(CandidateProfileActivityLog.profile_id, CandidateProfileActivityLog.action_type,
               CandidateProfileActivityLog.comment, CandidateProfileActivityLog.timestamp)
        .where(CandidateProfileActivityLog.profile_id.in_(ids),
               CandidateProfileActivityLog.action_type.in_(tuple(_STATE_BY_ACTION)))
        .order_by(CandidateProfileActivityLog.id.desc())
    ).all():
        if pid not in out:
            out[pid] = {"state": _STATE_BY_ACTION[action],
                        "at": ts.isoformat() if ts else None,
                        "note": comment or ""}
    return out
