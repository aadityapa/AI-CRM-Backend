"""Admin email administration: routing flows, per-user pause, and invitations.

This is the Users-tab backend for "never edit code when people change":

* /api/email-flows          — which roles receive which application email.
* /api/users/{id}/email-pause — stop mailing a leaver without touching data.
* /api/users/invite         — add a person by email alone; they get an
  invitation mail with a set-password link and finish their own profile.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from crm_deps import CurrentUser, any_crm_role, get_crm_db, role_required
from models import NotificationRoute, RoleName, UserNotifyPref
from schemas.common import envelope
from services import users_admin as users_svc
from services.email_outbox import app_url, queue_email, render_html, render_text
from services.notify import paused_user_ids

router = APIRouter(prefix="/api", tags=["CRM: Email flows (Admin)"])

admin_only = role_required()

#: Every routable event, with the DEFAULT the code falls back to when the
#: admin has not saved a row. Adding a new notify_roles(..., event=...) call
#: site only needs a line here to become admin-editable.
EVENTS: list[dict] = [
    {"event": "timesheet.submitted",
     "label": "Timesheet submitted for approval",
     "description": "Sent when an employee submits a monthly timesheet.",
     "default_roles": ["HR", "RMG", "Sales", "CEO"]},
    {"event": "leave.submitted",
     "label": "Leave application submitted",
     "description": "Sent when an employee applies for leave.",
     "default_roles": ["HR"]},
    {"event": "leave.credit_repaired",
     "label": "Leave accrual repaired a missed month",
     "description": "Sent when the monthly leave-credit job finds a month that never ran and "
                    "credits it late. Balances are corrected automatically; this tells you which "
                    "months were affected, because anything already settled from them was wrong.",
     "default_roles": ["HR", "CEO"]},
    {"event": "invoice.generated",
     "label": "Invoice generated from a timesheet",
     "description": "Sent when a reviewer generates the invoice for an approved timesheet.",
     "default_roles": ["Finance"]},
    {"event": "opportunity.submitted",
     "label": "Opportunity awaiting approval",
     "description": "Sent when an opportunity is created or resubmitted for Sales Head approval.",
     "default_roles": ["Sales_Head"]},
    {"event": "opportunity.approved",
     "label": "Opportunity approved — engineering review",
     "description": "Sent when an approved opportunity auto-creates a requirement that needs engineering review.",
     "default_roles": ["RMG"]},
    {"event": "requirement.submitted",
     "label": "Requirement submitted for approval",
     "description": "Sent when a requirement is submitted for Sales Head approval.",
     "default_roles": ["Sales_Head"]},
    {"event": "requirement.sales_approved",
     "label": "Requirement approved by Sales Head",
     "description": "Sent when Sales Head approves a requirement and it needs engineering review.",
     "default_roles": ["RMG"]},
    {"event": "requirement.engineering_approved",
     "label": "Requirement open for sourcing",
     "description": "Sent when engineering approves a requirement and sourcing can start.",
     "default_roles": ["TA"]},
    # Candidate stage arrivals — ONE ROW PER STAGE (2 Sep 2026). A single
    # "candidate.stage_arrival" row used to cover every stage, so customising
    # it for one team redirected every other team's hand-off too: HR stopped
    # hearing about Pre Onboarding the day someone saved a route for Sales.
    # Generated from the pipeline's own owner map, so the list here can never
    # drift from the roles the code actually notifies — appended below.
    {"event": "candidate.joined",
     "label": "Candidate joined",
     "description": "Sent when HR marks a candidate Joined — to every team that carried them "
                    "through the pipeline, and to leadership.",
     "default_roles": ["TA", "RMG", "Sales", "Sales_Head", "Admin", "CEO"]},
    {"event": "candidate.notice_period_requested",
     "label": "Collect the candidate's notice period",
     "description": "Sent to the TA owner when RMG submits a candidate to Sales and ticks "
                    "'ask TA to collect the notice period'.",
     "default_roles": ["TA"]},
    {"event": "candidate.offer_submitted",
     "label": "Terms submitted for Sales Head approval",
     "description": "Sent to TA, RMG and HR when Sales submits a candidate's rate and onboarding "
                    "date for Sales Head approval (Sales Head is told by the stage-arrival flow).",
     "default_roles": ["TA", "RMG", "HR"]},
    {"event": "candidate.hr_screening",
     "label": "Approved — HR round next",
     "description": "Sent to TA, RMG and HR when Sales Head approves the terms: the candidate is in "
                    "HR Screening and TA schedules the HR round.",
     "default_roles": ["TA", "RMG", "HR"]},
    {"event": "candidate.round_scheduled",
     "label": "Interview round scheduled — panel notified",
     "description": "Sent to the team whose round it is (RMG for L1–L4, HR for the HR round, Sales for "
                    "the customer's) when TA books a round from the Applied Candidates tab or the "
                    "Interviews tab. Roles here are the DEFAULT; the round decides the recipient.",
     "default_roles": []},
    {"event": "candidate.hr_requested",
     "label": "HR round requested",
     "description": "Sent to the TA owner when HR (at HR Screening) asks for the HR round to be booked.",
     "default_roles": ["TA"]},
    {"event": "candidate.round_rejected",
     "label": "Rejected at an interview round",
     "description": "Sent to the TA who sourced the candidate (and to Sales for customer rounds) when a "
                    "customer or RMG round is recorded as No Hire and the candidacy closes.",
     "default_roles": ["TA", "Sales"]},
    {"event": "candidate.out_of_budget",
     "label": "Out of budget — HR flag at Pre-Onboarding",
     "description": "Sent to Sales Head and the Sales person who submitted the terms when HR finds the "
                    "candidate's CTC / joining date does not fit at Pre-Onboarding.",
     "default_roles": ["Sales_Head", "Sales"]},
    {"event": "candidate.budget_resolved",
     "label": "Budget reply from Sales",
     "description": "Sent to HR when Sales / Sales Head reply to the out-of-budget flag after talking to "
                    "the customer (optionally with revised terms).",
     "default_roles": ["HR"]},
    {"event": "candidate.hr_scheduled",
     "label": "HR round scheduled",
     "description": "Sent to HR (or the TA owner, when HR scheduled it directly) once the HR round "
                    "is booked.",
     "default_roles": ["HR"]},
    {"event": "candidate.offer_approve",
     "label": "Offer approved by Sales Head",
     "description": "Sent to the Sales person who submitted the terms when Sales Head approves them "
                    "(the candidate moves to Pre Onboarding; HR is told by the stage-arrival flow).",
     "default_roles": ["Sales"]},
    {"event": "candidate.offer_send_back",
     "label": "Offer sent back to Sales",
     "description": "Sent to the submitting Sales person when Sales Head sends the rate / onboarding "
                    "date back to be redone.",
     "default_roles": ["Sales"]},
    {"event": "candidate.offer_reject",
     "label": "Candidate rejected at Sales Head approval",
     "description": "Sent to the submitting Sales person when Sales Head rejects the candidate.",
     "default_roles": ["Sales"]},
    {"event": "candidate.l2_scheduled",
     "label": "L2 face-to-face scheduled",
     "description": "Sent when an L2 face-to-face round is scheduled for a candidate.",
     "default_roles": ["TA"]},
    {"event": "ai_interview.completed",
     "label": "AI interview completed",
     "description": "Sent when a candidate finishes the AI interview, with the score.",
     "default_roles": ["TA"]},
    {"event": "ai_interview.passed_review",
     "label": "AI L1 passed — needs review",
     "description": "Sent when a candidate passes the AI L1 threshold and the report needs a decision.",
     "default_roles": ["RMG"]},
    {"event": "slot.confirmed",
     "label": "Interview slot confirmed",
     "description": "Sent when a candidate confirms an interview slot and AI L1 is scheduled.",
     "default_roles": ["TA"]},
    {"event": "slot.manual_followup",
     "label": "Shortlisted — manual follow-up needed",
     "description": "Sent when a resume clears the auto-shortlist threshold but has no email/phone for the invite.",
     "default_roles": ["TA"]},
    {"event": "timesheet.invoice_undone",
     "label": "Invoice undone by Admin/CEO",
     "description": "Sent to Finance when Admin/CEO rejects an already-invoiced timesheet and the "
                    "invoice is deleted with it (payments reversed, PO balance restored).",
     "default_roles": ["Finance"]},
    {"event": "timesheet.due_reminder",
     "label": "Timesheet due — reminder to the employee",
     "description": "Sent by the daily scheduler to employees whose previous-month timesheet is not submitted.",
     "default_roles": []},
    {"event": "timesheet.due_digest",
     "label": "Timesheet due — digest to approvers",
     "description": "Daily scheduler digest listing everyone still due for the previous month.",
     "default_roles": ["HR", "RMG"]},
    {"event": "po.expiry_warning",
     "label": "Purchase order expiring / expired",
     "description": "Milestone notices before expiry (45/30/15/5/1 days), on the expiry day, and while overdue.",
     "default_roles": ["Finance", "Sales_Head"]},
    {"event": "invoice.auto_drafted",
     "label": "Invoice auto-generated (recurring billing)",
     "description": "Sent when the scheduler raises an invoice for a recurring-billing project, or when one needs manual attention.",
     "default_roles": ["Finance"]},
    {"event": "template_request.created",
     "label": "Interview template requested",
     "description": "Sent when TA raises a request for an interview template.",
     "default_roles": ["RMG"]},
    # ---- CANDIDATE-FACING DRAFTS (28 Aug 2026, user request) --------------
    # kind="candidate": the recipient is the candidate, not a role — editing
    # the draft here changes the wording with no code change. Tokens use
    # single braces {token}; unknown tokens pass through visibly.
    {"event": "candidate.slot_invite", "kind": "candidate",
     "label": "Candidate — pick your interview slot",
     "description": "Sent to the candidate with the public booking link after shortlisting.",
     "default_roles": [],
     "tokens": ["candidate", "role", "link", "company"]},
    {"event": "candidate.ai_invite", "kind": "candidate",
     "label": "Candidate — AI L1 interview invitation (link + access key)",
     "description": "Sent to the candidate when the AI L1 interview is scheduled — by a recruiter, or "
                    "automatically when the candidate confirms a slot.",
     "default_roles": [],
     "tokens": ["candidate", "role", "level", "when", "duration", "link", "access_key",
                "sender", "sender_designation", "sender_department", "sender_phone",
                "sender_email", "company", "company_name", "company_website"]},
    {"event": "candidate.l1_manual_invite", "kind": "candidate",
     "label": "Candidate — L1 interview invitation (manual route)",
     "description": "Sent to the candidate when TA schedules the human L1 round that replaces the AI "
                    "interview (calendar invite attached).",
     "default_roles": [],
     "tokens": ["candidate", "round", "team", "interviewer", "when", "link", "note", "company"]},
    {"event": "candidate.l2_invite", "kind": "candidate",
     "label": "Candidate — L2 interview invitation",
     "description": "Sent to the candidate when the L2 round is scheduled (calendar invite attached).",
     "default_roles": [],
     "tokens": ["candidate", "round", "team", "interviewer", "when", "link", "note", "company"]},
    {"event": "candidate.hr_invite", "kind": "candidate",
     "label": "Candidate — HR interview invitation",
     "description": "Sent to the candidate when the HR round is scheduled (calendar invite attached).",
     "default_roles": [],
     "tokens": ["candidate", "round", "team", "interviewer", "when", "link", "note", "company"]},
    {"event": "candidate.round_invite", "kind": "candidate",
     "label": "Candidate — customer interview invitation (Customer L1 / L2)",
     "description": "Sent to the candidate when a customer-side round is scheduled with a time and "
                    "meeting link (calendar invite attached).",
     "default_roles": [],
     "tokens": ["candidate", "round", "interviewer", "when", "link", "note", "company"]},
    {"event": "candidate.hiring_interest", "kind": "candidate",
     "label": "Candidate — we're hiring, interested?",
     "description": "The DEFAULT draft the 'Email selected' composer prefills on Suggested Candidates. "
                    "Keep the {{double-brace}} placeholders — they are filled per candidate at send time.",
     "default_roles": [],
     "tokens": ["{{first_name}}", "{{full_name}}", "{{role}}", "{{customer}}", "{{sender}}"]},
]


def _stage_arrival_events() -> list[dict]:
    """One routable event per pipeline stage that has an owner to notify.

    Built from `_ARRIVAL_NOTIFY_ROLE` — the map the transition code actually
    reads — so the Users tab always lists the stages that fire, with the role
    the code defaults to. A route saved for one stage touches that stage only.
    """
    from services.candidate_profiles import (
        _ARRIVAL_ACTION, _ARRIVAL_NOTIFY_ROLE, stage_arrival_event,
    )
    out = []
    for status, role in _ARRIVAL_NOTIFY_ROLE.items():
        pretty = status.replace("_", " ")
        out.append({
            "event": stage_arrival_event(status),
            "label": f"Candidate reaches {pretty}",
            "description": (f"Sent to {role.replace('_', ' ')} when a candidate profile arrives at "
                            f"{pretty}. {_ARRIVAL_ACTION.get(status, '')}").strip(),
            "default_roles": [role],
        })
    return out


EVENTS.extend(_stage_arrival_events())

ALL_ROLES = [m.value for m in RoleName]


# Deliberately NOT pydantic's EmailStr: that type needs the optional
# email-validator package, and importing this module without it installed
# raises — which silently unregisters this whole router (the registry
# isolates failures) and turns every endpoint here into a 405 from the SPA
# catch-all. A plain regex has no such failure mode.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_email(value: str) -> str:
    addr = (value or "").strip().lower()
    if not _EMAIL_RE.match(addr):
        raise ValueError(f"'{value}' is not a valid email address")
    return addr


class FlowIn(BaseModel):
    roles: list[str] = Field(default_factory=list)
    extra_emails: list[str] = Field(default_factory=list)
    enabled: bool = True
    #: Admin-authored wording (0071). None/empty = code-composed text.
    #: Placeholders: {subject} {body} {recipient} {company}.
    subject_template: str | None = Field(default=None, max_length=255)
    body_template: str | None = Field(default=None, max_length=8000)
    #: Custom drafts only (0093): rename / re-describe on save.
    label: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("extra_emails")
    @classmethod
    def _valid_extras(cls, v):
        return [_clean_email(e) for e in (v or [])]


class PauseIn(BaseModel):
    paused: bool


class InviteIn(BaseModel):
    email: str
    full_name: str = ""
    roles: list[str] = Field(default_factory=list)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v):
        return _clean_email(v)


#: Placeholders an admin-created draft may use. Filled by the composer that
#: offers the draft (it knows the candidate and the sender); anything else is
#: left literally for the writer to fill in.
CUSTOM_DRAFT_TOKENS = ["candidate", "first_name", "email", "role", "customer", "sender", "company"]

#: Placeholders every internal notification draft may use (see
#: email_outbox._apply_event_template).
INTERNAL_TOKENS = ["subject", "title", "message", "details", "action", "link",
                   "recipient", "company", "body"]


def _last_sent_examples(db: Session, events: list[str]) -> dict[str, dict]:
    """The most recent outbox row per event — the "format we actually sent",
    shown under each internal draft (3 Sep 2026, user request). One query."""
    from sqlalchemy import func as sa_func
    from models import EmailOutbox
    if not events:
        return {}
    try:
        latest = (
            select(EmailOutbox.event, sa_func.max(EmailOutbox.id).label("mid"))
            .where(EmailOutbox.event.in_(events))
            .group_by(EmailOutbox.event)
        ).subquery("l")
        rows = db.execute(
            select(EmailOutbox).join(latest, latest.c.mid == EmailOutbox.id)
        ).scalars().all()
        return {
            r.event: {
                "subject": r.subject,
                "body": r.body_text,
                "to": r.to_name or r.to_email,
                "sent_at": (r.sent_at or r.created_at).isoformat() if (r.sent_at or r.created_at) else None,
                "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            }
            for r in rows
        }
    except Exception:  # pragma: no cover — examples are a nicety, never a blocker
        return {}


def _custom_flow(row: NotificationRoute) -> dict:
    return {
        "event": row.event,
        "label": row.label or row.event.split(".", 1)[-1].replace("-", " ").title(),
        "description": row.description or "Admin-created draft — offered in the email composer as “Use a draft”.",
        "kind": "custom",
        "tokens": list(CUSTOM_DRAFT_TOKENS),
        "roles": [], "extra_emails": [], "enabled": True, "default_roles": [],
        "subject_template": row.subject_template,
        "body_template": row.body_template,
        "default_subject": None, "default_body": None,
        "customized": True,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/email-flows")
def list_email_flows(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    from models.notify_routes import CUSTOM_DRAFT_PREFIX
    from services.candidate_comms import builtin_candidate_draft
    from services.email_outbox import INTERNAL_DEFAULT_BODY, INTERNAL_DEFAULT_SUBJECT
    all_rows = db.execute(select(NotificationRoute).order_by(NotificationRoute.id)).scalars().all()
    rows = {r.event: r for r in all_rows}
    internal_events = [s["event"] for s in EVENTS if s.get("kind", "internal") == "internal"]
    examples = _last_sent_examples(db, internal_events)
    flows = []
    for spec in EVENTS:
        row = rows.get(spec["event"])
        kind = spec.get("kind", "internal")
        # The CURRENT built-in wording (3 Sep 2026, user request): the editor
        # prefills with it so an admin sees what goes out today and changes
        # exactly that, instead of guessing from an empty box. Internal
        # notifications show the layout every one of them is composed with,
        # plus the last one actually sent as a worked example.
        if kind == "candidate":
            default = builtin_candidate_draft(spec["event"])
        else:
            default = {"subject": INTERNAL_DEFAULT_SUBJECT, "body": INTERNAL_DEFAULT_BODY}
        flows.append({
            **spec,
            "kind": kind,
            "tokens": spec.get("tokens", INTERNAL_TOKENS if kind == "internal" else ["company"]),
            "roles": [str(x) for x in (row.roles or [])] if row else list(spec["default_roles"]),
            "extra_emails": [str(x) for x in (row.extra_emails or [])] if row else [],
            "enabled": bool(row.enabled) if row else True,
            "subject_template": row.subject_template if row else None,
            "body_template": row.body_template if row else None,
            "default_subject": (default or {}).get("subject"),
            "default_body": (default or {}).get("body"),
            "last_sent": examples.get(spec["event"]),
            "customized": row is not None,
        })
    # Admin-created drafts (0093) come after the code-defined ones.
    for row in all_rows:
        if row.event.startswith(CUSTOM_DRAFT_PREFIX):
            flows.append(_custom_flow(row))
    return envelope(data={
        "flows": flows,
        "all_roles": ALL_ROLES,
        "paused_user_ids": sorted(paused_user_ids(db)),
        "custom_tokens": list(CUSTOM_DRAFT_TOKENS),
    })


@router.get("/email-drafts/custom")
def list_custom_drafts(db: Session = Depends(get_crm_db),
                       user: CurrentUser = Depends(any_crm_role)):
    """The admin-created drafts, for the composers' "Use a draft" picker —
    readable by anyone who can write an email, not only Admin."""
    from models.notify_routes import CUSTOM_DRAFT_PREFIX
    rows = db.execute(
        select(NotificationRoute)
        .where(NotificationRoute.event.like(f"{CUSTOM_DRAFT_PREFIX}%"))
        .order_by(NotificationRoute.label, NotificationRoute.id)
    ).scalars().all()
    return envelope(data=[{
        "key": r.event, "label": r.label or r.event,
        "description": r.description or "",
        "subject": r.subject_template or "", "body": r.body_template or "",
    } for r in rows], meta={"tokens": list(CUSTOM_DRAFT_TOKENS)})


class CustomDraftIn(BaseModel):
    label: str = Field(min_length=2, max_length=160)
    description: str | None = Field(default=None, max_length=500)
    subject: str = Field(min_length=1, max_length=255)
    body: str = Field(min_length=1, max_length=8000)


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return s[:40] or "draft"


@router.post("/email-flows/custom")
def create_custom_draft(body: CustomDraftIn, db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(admin_only)):
    """A new admin-authored draft (3 Sep 2026, user request: "if I want any
    another email in Email Drafts I also do"). Keyed `custom.<slug>`; a
    duplicate label gets a numeric suffix rather than a 409 — two drafts can
    legitimately share a name across teams."""
    from models.notify_routes import CUSTOM_DRAFT_PREFIX
    base = f"{CUSTOM_DRAFT_PREFIX}{_slug(body.label)}"
    event, n = base, 2
    while db.execute(select(NotificationRoute.id).where(NotificationRoute.event == event)).first():
        event = f"{base}-{n}"[:64]
        n += 1
    row = NotificationRoute(
        event=event, roles=[], extra_emails=[], enabled=True,
        subject_template=body.subject.strip(), body_template=body.body.strip(),
        label=body.label.strip(), description=(body.description or "").strip() or None,
        updated_by=user.id,
    )
    db.add(row)
    db.commit()
    return envelope(data=_custom_flow(row), message=f"Draft '{row.label}' created")


@router.put("/email-flows/{event}")
def save_email_flow(event: str, body: FlowIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    from models.notify_routes import CUSTOM_DRAFT_PREFIX
    spec = next((s for s in EVENTS if s["event"] == event), None)
    is_custom = spec is None and event.startswith(CUSTOM_DRAFT_PREFIX)
    if spec is None and not is_custom:
        raise HTTPException(status_code=404, detail="Unknown email flow")
    bad = [r for r in body.roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {', '.join(bad)}")
    if (spec is not None and spec.get("kind", "internal") != "candidate"
            and body.enabled and not body.roles and not body.extra_emails):
        raise HTTPException(
            status_code=400,
            detail="An enabled flow needs at least one role or extra email — or disable it instead",
        )
    row = db.execute(
        select(NotificationRoute).where(NotificationRoute.event == event)
    ).scalars().first()
    if row is None:
        if is_custom:
            raise HTTPException(status_code=404, detail="Unknown draft")
        row = NotificationRoute(event=event)
        db.add(row)
    # Empty string = clear the template (back to code-composed text).
    new_subject = (body.subject_template or "").strip() or None
    new_body = (body.body_template or "").strip() or None
    if is_custom and (not new_subject or not new_body):
        raise HTTPException(status_code=400, detail="A custom draft needs both a subject and a body")
    row.roles = list(body.roles)
    row.extra_emails = [str(e) for e in body.extra_emails]
    row.enabled = body.enabled
    row.subject_template = new_subject
    row.body_template = new_body
    if is_custom:
        if body.label is not None and body.label.strip():
            row.label = body.label.strip()
        if body.description is not None:
            row.description = body.description.strip() or None
    row.updated_by = user.id
    db.commit()
    label = spec["label"] if spec else (row.label or event)
    return envelope(
        data={"event": event, "roles": row.roles, "extra_emails": row.extra_emails,
              "enabled": row.enabled, "subject_template": row.subject_template,
              "body_template": row.body_template, "label": row.label,
              "description": row.description},
        message=f"{'Draft' if is_custom else 'Email flow'} '{label}' saved",
    )


@router.delete("/email-flows/{event}")
def reset_email_flow(event: str, db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Back to the code default (removes the customization row) — or, for an
    admin-created draft, deletes it."""
    from models.notify_routes import CUSTOM_DRAFT_PREFIX
    row = db.execute(
        select(NotificationRoute).where(NotificationRoute.event == event)
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()
    if event.startswith(CUSTOM_DRAFT_PREFIX):
        return envelope(data={"event": event}, message="Draft deleted")
    return envelope(data={"event": event}, message="Flow reset to default")


# ------------------------------------------------------------- email pause


@router.post("/users/{user_id}/email-pause")
def set_email_pause(user_id: int, body: PauseIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    """Pause / resume application email for one user (leavers, long absences).

    Deliberately separate from deactivation: pausing keeps their login and
    history intact while making sure nothing is sent to a dead mailbox.
    """
    pref = db.get(UserNotifyPref, user_id)
    if pref is None:
        pref = UserNotifyPref(user_id=user_id)
        db.add(pref)
    pref.email_paused = body.paused
    pref.updated_by = user.id
    db.commit()
    return envelope(
        data={"user_id": user_id, "email_paused": pref.email_paused},
        message="Email paused for this user" if body.paused else "Email resumed for this user",
    )


# ------------------------------------------------------------ daily scheduler


@router.get("/scheduler/status")
def scheduler_status(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Is the scheduler alive, and what did each job do last time?

    This exists so "the reminders stopped" is answerable in one look instead of
    by reading server logs.
    """
    from datetime import datetime, timedelta, timezone

    from services.scheduler import DEFAULTS, JOBS, last_runs
    from services.scheduler import _flag as job_enabled

    labels = {"timesheet_reminders": "Timesheet due reminders",
              "po_expiry": "Purchase order expiry notices",
              "recurring_invoices": "Recurring invoice drafts",
              "pe_leave_credit": "Monthly leave credit"}
    runs = last_runs()

    def _stale(entry: dict) -> bool:
        """No run in 48h. Every job is daily, so a two-day silence means the
        worker is not running — which for leave credit means balances are
        quietly drifting, the exact failure this panel exists to surface."""
        stamp = (entry or {}).get("at")
        if not stamp:
            return True
        try:
            when = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - when > timedelta(hours=48)

    return envelope(data={
        "jobs": [
            {"job": job, "setting": flag_key,
             "label": labels.get(job, job),
             "last_run": runs.get(job, {}),
             "enabled": job_enabled(flag_key),
             "stale": _stale(runs.get(job, {}))}
            for job, (flag_key, _fn) in JOBS.items()
        ],
        "defaults": DEFAULTS,
    })


@router.get("/backup/status")
def backup_status(user: CurrentUser = Depends(admin_only)):
    """Age and outcome of the last backup, so silent failure is impossible.

    Reads the status file `scripts/backup_karnex.py` writes. Backups that stop
    running are the classic silent disaster: everything looks fine until the
    day you need a restore. `stale` goes true after 48h without a good run.
    """
    import json as _json
    import os as _os
    from datetime import datetime, timezone
    from pathlib import Path

    configured = (_os.getenv("BACKUP_DIR") or "").strip()
    root = Path(__file__).resolve().parents[3]
    candidates = [Path(configured)] if configured else []
    candidates += [root.parent / "KarnexBackups", root / "KarnexBackups"]
    for folder in candidates:
        status_file = folder / "backup-status.json"
        if status_file.is_file():
            try:
                data = _json.loads(status_file.read_text(encoding="utf-8"))
            except Exception as exc:
                return envelope(data={"configured": True, "readable": False,
                                      "error": str(exc)[:120]},
                                message="Backup status file is unreadable")
            age_hours = None
            stamp = data.get("finished_at") or data.get("started_at")
            if stamp:
                try:
                    then = datetime.fromisoformat(stamp)
                    if then.tzinfo is None:
                        then = then.replace(tzinfo=timezone.utc)
                    age_hours = round((datetime.now(timezone.utc) - then).total_seconds() / 3600, 1)
                except Exception:
                    pass
            stale = data.get("ok") is not True or (age_hours is not None and age_hours > 48)
            return envelope(data={
                "configured": True, "readable": True, "folder": str(folder),
                "last": data, "age_hours": age_hours, "stale": stale,
            }, message="Backup status")
    return envelope(
        data={"configured": False, "stale": True,
              "hint": "Schedule backup_karnex.bat daily in Windows Task Scheduler."},
        message="No backup has run yet",
    )


@router.post("/scheduler/run")
def scheduler_run_now(job: str | None = None,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(admin_only)):
    """Run the daily jobs immediately (all, or one by name).

    Safe to press repeatedly: every message the jobs send carries a milestone
    dedupe key, so a forced run cannot re-send anything already sent.
    """
    from services.scheduler import JOBS, run_due_jobs

    if job and job not in JOBS:
        raise HTTPException(status_code=404, detail=f"Unknown job '{job}'")
    results = run_due_jobs(force=True, only=job)
    return envelope(data=results, message="Scheduler run complete")


# -------------------------------------------------------- action permissions


class ActionPermissionIn(BaseModel):
    roles: list[str] = Field(default_factory=list)


@router.get("/action-permissions")
def list_action_permissions(db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    """Which roles may perform each configurable write action. Admin/CEO
    always pass regardless — an empty role list means admins only."""
    from services.action_permissions import effective as ap_effective

    return envelope(data={"actions": ap_effective(db), "all_roles": ALL_ROLES})


@router.put("/action-permissions/{action}")
def save_action_permission(action: str, body: ActionPermissionIn,
                           db: Session = Depends(get_crm_db),
                           user: CurrentUser = Depends(admin_only)):
    from models import ActionPermission
    from services.action_permissions import ACTIONS, invalidate as ap_invalidate

    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown action")
    bad = [r for r in body.roles if r not in ALL_ROLES]
    if bad:
        raise HTTPException(status_code=400, detail=f"Unknown role(s): {', '.join(bad)}")
    row = db.execute(
        select(ActionPermission).where(ActionPermission.action == action)
    ).scalars().first()
    if row is None:
        row = ActionPermission(action=action)
        db.add(row)
    row.roles = list(dict.fromkeys(body.roles))
    row.updated_by = user.id
    db.commit()
    ap_invalidate()
    label = ACTIONS[action][0]
    who = ", ".join(row.roles) if row.roles else "Admin/CEO only"
    return envelope(data={"action": action, "roles": row.roles},
                    message=f"'{label}' can now be done by: {who}")


@router.delete("/action-permissions/{action}")
def reset_action_permission(action: str, db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(admin_only)):
    """Back to the code default (removes the customization row)."""
    from models import ActionPermission
    from services.action_permissions import invalidate as ap_invalidate

    row = db.execute(
        select(ActionPermission).where(ActionPermission.action == action)
    ).scalars().first()
    if row is not None:
        db.delete(row)
        db.commit()
        ap_invalidate()
    return envelope(data={"action": action}, message="Permission reset to default")


# -------------------------------------------------------- organisation settings


class OrgSettingsIn(BaseModel):
    """Bulk upsert: {key: value}. Only known org-settings keys are accepted."""

    values: dict[str, str] = Field(default_factory=dict)


@router.get("/org-settings")
def get_org_settings(db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    """Company identity, email/link and interview settings with provenance —
    each value says whether it comes from the Settings page, the server
    environment, or the code default."""
    from services.org_settings import KEYS, effective

    return envelope(data={"settings": effective(db), "keys": sorted(KEYS)})


@router.put("/org-settings")
def put_org_settings(body: OrgSettingsIn,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(admin_only)):
    from models import AppSetting
    from services.org_settings import KEYS, invalidate

    unknown = [k for k in body.values if k not in KEYS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown setting(s): {', '.join(unknown)}")
    url = (body.values.get("email.public_base_url") or "").strip()
    if url and not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400,
                            detail="Public base URL must start with http:// or https://")
    for key, value in body.values.items():
        row = db.get(AppSetting, key)
        value = (value or "").strip()
        if row is None:
            if value:
                db.add(AppSetting(key=key, value=value,
                                  description="Organisation setting (Settings page)"))
        elif value:
            row.value = value
        else:
            # Cleared in the UI -> back to the environment/code fallback.
            db.delete(row)
    db.commit()
    invalidate()
    return envelope(data={"saved": sorted(body.values)}, message="Organisation settings saved")


class TestEmailIn(BaseModel):
    to: str

    @field_validator("to")
    @classmethod
    def _valid_to(cls, v):
        return _clean_email(v)


@router.post("/org-settings/test-email")
def send_test_email(body: TestEmailIn,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(admin_only)):
    """Queue a test email through the real outbox so the admin can verify the
    whole chain — settings, sender identity, SMTP — with one click."""
    from services.org_settings import setting

    base = setting("email.public_base_url") or "(no public base URL set)"
    text = render_text(
        "Karnex test email",
        f"This is a test email sent from the Settings page by "
        f"{user.full_name or user.username}. If you can read this, SMTP and the "
        f"email outbox are working. Links currently point at: {base}",
        action_label="Open Karnex" if base.startswith("http") else "",
        action_url=base if base.startswith("http") else "",
    )
    html = render_html(
        "Karnex test email",
        f"This is a test email sent from the Settings page by "
        f"{user.full_name or user.username}. If you can read this, SMTP and the "
        f"email outbox are working. Links currently point at: {base}",
        action_label="Open Karnex" if base.startswith("http") else "",
        action_url=base if base.startswith("http") else "",
    )
    row = queue_email(
        db,
        to_email=body.to,
        to_name="",
        subject="Karnex test email",
        body_text=text,
        body_html=html,
        event="settings.test_email",
        actor=user,
    )
    db.commit()
    if row is None:
        raise HTTPException(status_code=400,
                            detail="Email is disabled in settings — the test was not queued")
    return envelope(
        data={"queued": True, "outbox_id": row.id},
        message=f"Test email queued to {body.to} — check the inbox (and Email Outbox for status)",
    )


# -------------------------------------------------------------- invitations


@router.post("/users/invite")
def invite_user(body: InviteIn,
                db: Session = Depends(get_crm_db),
                user: CurrentUser = Depends(admin_only)):
    """Create an account from an email address and send an invitation.

    The account is created with a random unusable password; the invitation
    email carries a set-password link (the existing reset-token flow), so the
    new person chooses their own password and fills their own details. The
    admin only types an email and picks roles.
    """
    from auth_db import create_password_reset

    email = str(body.email).strip().lower()
    full_name = (body.full_name or "").strip() or email.split("@", 1)[0].replace(".", " ").title()
    username = email  # unique, memorable, and what they will type at login

    created = users_svc.create_user(
        db,
        full_name=full_name,
        email=email,
        username=username,
        password=secrets.token_urlsafe(24),  # unusable until they set their own
        legacy_role="hr",
        role_names=body.roles,
    )
    uid = int(created["id"])

    # Set-password link via the existing single-use reset-token flow, but with
    # a longer window than a forgot-password (the invitee may open it tomorrow).
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    create_password_reset(users_svc._legacy_db_target(), uid, token_hash, expires_at)

    base = app_url("") or ""
    link = f"{base}/?reset_token={token}" if base else f"/?reset_token={token}"
    roles_txt = ", ".join(created["roles"]) or "none"
    title = "You're invited to Karnex"
    message = (
        f"{user.full_name or user.username} invited you to the Karnex application "
        f"with the role(s): {roles_txt}. Set your password to activate your account; "
        f"the link works for 7 days. After signing in you can complete your own profile."
    )
    text = render_text(title, message, action_label="Set your password", action_url=link)
    html = render_html(title, message, action_label="Set your password", action_url=link)
    queue_email(
        db,
        to_email=email,
        to_name=full_name,
        subject="Invitation to Karnex — set your password",
        body_text=text,
        body_html=html,
        event="user.invited",
        actor=user,
        dedupe_key=f"user.invited:{uid}:{token_hash[:12]}",
    )
    db.commit()
    return envelope(
        data={**created, "invited": True},
        message=f"Invitation sent to {email} (roles: {roles_txt})",
    )
