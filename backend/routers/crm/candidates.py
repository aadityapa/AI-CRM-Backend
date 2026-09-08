"""Candidate master CRUD: candidates + education + experience + skills + CV upload.

Write access: TA / RMG / Sales / HR (Admin implicit). Reads: any CRM role.
"""
from __future__ import annotations

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pydantic import BaseModel, Field

from crm_deps import CurrentUser, PageParams, any_crm_role, gated_create, gated_write, get_crm_db, page_params, role_required
from models import (
    Candidate, CandidateEducation, CandidateExperience, CandidateSkill,
    Requirement, RequirementActivityLog, RequirementStatus, Resume, Skill,
)
from schemas.candidates import (
    CandidateCreate, CandidateUpdate, EducationCreate, EducationUpdate, ExperienceCreate,
    ExperienceUpdate, SkillSetIn,
)
from schemas.common import envelope
from services.candidates import (
    apply_cv_profile_to_candidate, candidate_detail, candidate_search_clause, candidate_to_dict,
    education_to_dict, ensure_skills_exist, experience_to_dict, get_candidate_or_404,
)
from services.crm_common import log_activity, paginate, save_upload
from services.requirements import requirement_label

router = APIRouter(prefix="/api/candidates", tags=["CRM: Candidates"])

write_roles = role_required("TA", "RMG", "Sales", "Sales_Head", "HR")
create_candidates = gated_create("candidates", "TA", "RMG", "Sales", "Sales_Head", "HR")


def _email_taken(db: Session, email: str, exclude_id: int | None = None) -> bool:
    stmt = select(Candidate.id).where(func.lower(Candidate.email) == email.lower())
    if exclude_id is not None:
        stmt = stmt.where(Candidate.id != exclude_id)
    return db.execute(stmt).first() is not None


# ---------------------------------------------------------------------------
# Candidate CRUD
# ---------------------------------------------------------------------------

@router.get("")
def list_candidates(pp: PageParams = Depends(page_params),
                    skill_id: int | None = None,
                    technical_domain: str | None = None,
                    has_cv: bool | None = None,
                    db: Session = Depends(get_crm_db),
                    user: CurrentUser = Depends(any_crm_role)):
    stmt = select(Candidate)
    if pp.search:
        # Full-name aware: "anand kumar" matches first_name + last_name together.
        stmt = stmt.where(candidate_search_clause(pp.search))
    if skill_id is not None:
        stmt = stmt.where(Candidate.id.in_(
            select(CandidateSkill.candidate_id).where(CandidateSkill.skill_id == skill_id)))
    if technical_domain:
        stmt = stmt.where(Candidate.technical_domain.ilike(f"%{technical_domain.strip()}%"))
    if has_cv is not None:
        # Only ~30% of imported candidates have a CV on file (the rest still need
        # re-downloading from Zoho), so being able to filter to them matters.
        attached = sa.and_(Candidate.cv_url.isnot(None), Candidate.cv_url != "")
        stmt = stmt.where(attached if has_cv else sa.not_(attached))
    order = Candidate.id.asc() if pp.sort_dir == "asc" else Candidate.id.desc()
    stmt = stmt.order_by(order)
    items, meta = paginate(db, stmt, pp.page, pp.limit)
    return envelope(data=[candidate_to_dict(c) for c in items], meta=meta)


#: The Emails tab shows ONLY mail addressed to the candidate (3 Sep 2026,
#: user decision). Many staff notifications also carry the `candidate.`
#: prefix ("candidate.joined" to TA, "candidate.offer_submitted" to HR…) and
#: were landing in the candidate's thread — that was the "mixed" look. This
#: is the allow-list; label is what the tab shows instead of the raw key.
CANDIDATE_MAIL_EVENTS: dict[str, str] = {
    "candidate.slot_invite": "Shortlisted — pick a slot",
    "candidate.ai_invite": "AI L1 interview scheduled",
    "candidate.interview_link": "AI L1 interview link",
    "candidate.l1_manual_invite": "L1 interview scheduled",
    "candidate.l2_invite": "L2 interview scheduled",
    "candidate.hr_invite": "HR interview scheduled",
    "candidate.round_invite": "Interview scheduled",
    "candidate.hiring_interest": "Hiring interest",
    "candidate.direct_message": "Direct message",
}


# NOTE: literal route — MUST stay above GET /{candidate_id}.
@router.get("/email-tags")
def email_tags(db: Session = Depends(get_crm_db),
               user: CurrentUser = Depends(any_crm_role)):
    """Distinct event tags on the candidate mailbox, with counts — the Tags
    rail of the Emails tab (27 Aug 2026 redesign)."""
    from sqlalchemy import func as sa_func
    from models import EmailOutbox
    rows = db.execute(
        select(EmailOutbox.event, sa_func.count(EmailOutbox.id))
        .where(EmailOutbox.event.in_(list(CANDIDATE_MAIL_EVENTS)))
        .group_by(EmailOutbox.event)
        .order_by(sa_func.count(EmailOutbox.id).desc())
    ).all()
    return envelope(data=[{"event": e, "label": CANDIDATE_MAIL_EVENTS.get(e, e), "count": int(c)}
                          for e, c in rows])


class ComposeEmailIn(BaseModel):
    candidate_id: int
    subject: str = Field(min_length=2, max_length=300)
    body: str = Field(min_length=2, max_length=20000)


@router.post("/email-compose")
def compose_candidate_email(payload: ComposeEmailIn,
                            db: Session = Depends(get_crm_db),
                            user: CurrentUser = Depends(
                                gated_write("emails", "TA", "RMG", "Sales",
                                            "Sales_Head", "HR"))):
    """New Email (27 Aug 2026): a free-form mail to one candidate, queued on
    the durable outbox with the sender's identity — so replies go to the
    author, and the mail shows in the candidate's thread like every other."""
    candidate = get_candidate_or_404(db, payload.candidate_id)
    email_addr = (candidate.email or "").strip()
    if not email_addr or email_addr.lower().endswith("@import.karnex.in"):
        raise HTTPException(status_code=400,
                            detail="This candidate has no real email address on file")
    from services.candidate_comms import notify_candidate
    cname = f"{candidate.first_name} {candidate.last_name or ''}".strip()
    sent = notify_candidate(email_addr, None, payload.subject.strip(),
                            payload.body.strip(), None, db=db,
                            event="candidate.direct_message", actor=user,
                            to_name=cname, candidate_id=candidate.id)
    db.commit()
    return envelope({"queued": True, "channels": sent},
                    message=f"Email queued to {cname}")


# NOTE: literal route — MUST stay above GET /{candidate_id} or FastAPI tries
# to coerce "email-conversations" into an int.
@router.get("/email-conversations")
def email_conversations(pp: PageParams = Depends(page_params),
                        status: str | None = None,
                        candidate_id: int | None = None,
                        to_email: str | None = None,
                        folder: str | None = None,
                        event: str | None = None,
                        db: Session = Depends(get_crm_db),
                        user: CurrentUser = Depends(any_crm_role)):
    """The Emails tab (Aug 2026): every email in a candidate's story, newest
    first, grouped like a mailbox.

    WHO an email belongs to (reworked 26 Aug 2026): the stamped
    `related_type='candidate'` / `related_id` wins; address match against the
    candidate table is only the fallback for rows queued before stamping
    existed. Matching by address alone merged different candidates who share
    a (test) address, and dropped candidate-ABOUT mails sent to the TA (e.g.
    "Slot confirmed"). `?candidate_id=` returns one candidate's full thread —
    including those TA-side mails.

    OUTBOUND ONLY — candidate replies go to the sending recruiter's mailbox
    (Reply-To); Karnex has no inbound mail receiver. Search covers candidate
    name, email and subject.
    """
    from sqlalchemy import func as sa_func, or_ as sa_or, and_ as sa_and
    from models import EmailOutbox

    is_stamped = sa_and(EmailOutbox.related_type == "candidate",
                        EmailOutbox.related_id.isnot(None))
    # Outer join by address for legacy/unstamped rows; the stamp wins when present.
    stmt = (
        select(EmailOutbox, Candidate.id)
        .outerjoin(Candidate, sa_func.lower(Candidate.email) == sa_func.lower(EmailOutbox.to_email))
    )
    # Mail TO THE CANDIDATE only (user decision, 3 Sep 2026; was candidate.*
    # + slot.confirmed, which pulled staff notifications into the thread).
    stmt = stmt.where(EmailOutbox.event.in_(list(CANDIDATE_MAIL_EVENTS)))
    if candidate_id is not None:
        stmt = stmt.where(sa_or(
            sa_and(is_stamped, EmailOutbox.related_id == candidate_id),
            sa_and(~is_stamped, Candidate.id == candidate_id),
        ))
    elif to_email and to_email.strip():
        # The "mail:<address>" thread — rows that matched no candidate at all.
        stmt = stmt.where(sa_func.lower(EmailOutbox.to_email) == to_email.strip().lower())
    else:
        # A row belongs on this tab when it is stamped OR its address matches a candidate.
        stmt = stmt.where(sa_or(is_stamped, Candidate.id.isnot(None)))
    if status and status.strip():
        stmt = stmt.where(EmailOutbox.status == status.strip())
    # `folder` is accepted for compatibility; everything here is sent mail.
    if event and event.strip():
        stmt = stmt.where(EmailOutbox.event == event.strip())
    if pp.search:
        like = f"%{pp.search}%"
        stmt = stmt.where(sa_or(
            EmailOutbox.subject.ilike(like),
            EmailOutbox.to_email.ilike(like),
            Candidate.first_name.ilike(like),
            Candidate.last_name.ilike(like),
        ))
    stmt = stmt.order_by(EmailOutbox.created_at.desc())
    # paginate() uses .scalars() which keeps only EmailOutbox from the tuple —
    # resolve the owning candidate per row afterwards, batched.
    rows, meta = paginate(db, stmt, pp.page, pp.limit)

    emails = {(r.to_email or "").lower() for r in rows}
    stamped_ids = {r.related_id for r in rows
                   if r.related_type == "candidate" and r.related_id is not None}
    cand_by_email: dict[str, tuple[int, str]] = {}
    cand_by_id: dict[int, str] = {}
    if emails or stamped_ids:
        for cid, cfirst, clast, cemail in db.execute(
            select(Candidate.id, Candidate.first_name, Candidate.last_name, Candidate.email)
            .where(sa_or(
                sa_func.lower(Candidate.email).in_(emails or {""}),
                Candidate.id.in_(stamped_ids or {0}),
            ))
        ).all():
            name = " ".join(p for p in [cfirst, clast] if p)
            cand_by_id[cid] = name
            cand_by_email.setdefault((cemail or "").lower(), (cid, name))

    def _iso(v):
        return v.isoformat() if v else None
    data = []
    for outbox in rows:
        if outbox.related_type == "candidate" and outbox.related_id in cand_by_id:
            cand_id, cand_name = outbox.related_id, cand_by_id[outbox.related_id]
        else:
            cand_id, cand_name = cand_by_email.get((outbox.to_email or "").lower(), (None, ""))
        data.append({
            "id": outbox.id,
            "candidate_id": cand_id,
            "candidate_name": cand_name,
            "to_email": outbox.to_email,
            "to_name": outbox.to_name,
            "event": outbox.event,
            "event_label": CANDIDATE_MAIL_EVENTS.get(outbox.event, outbox.event),
            "subject": outbox.subject,
            "body_text": outbox.body_text,
            "status": outbox.status.value if hasattr(outbox.status, "value") else str(outbox.status),
            "from_name": outbox.from_name,
            "reply_to_email": outbox.reply_to_email,
            "reply_to_name": outbox.reply_to_name,
            "attempts": outbox.attempts,
            "last_error": outbox.last_error,
            "created_at": _iso(outbox.created_at),
            "sent_at": _iso(outbox.sent_at),
        })
    return envelope(data=data, meta=meta)


def _mailbox_scope(stmt, *, status, folder, event, search):
    """The shared WHERE of the Emails tab (candidate ↔ TA correspondence only,
    folder / status / tag / search) — one place so the thread list and the
    per-thread view can never disagree about what belongs on the tab."""
    from sqlalchemy import or_ as sa_or
    from models import EmailOutbox
    stmt = stmt.where(EmailOutbox.event.in_(list(CANDIDATE_MAIL_EVENTS)))
    if status and status.strip():
        stmt = stmt.where(EmailOutbox.status == status.strip())
    # `folder` accepted for compatibility — everything here is sent mail.
    if event and event.strip():
        stmt = stmt.where(EmailOutbox.event == event.strip())
    if search:
        like = f"%{search}%"
        stmt = stmt.where(sa_or(
            EmailOutbox.subject.ilike(like),
            EmailOutbox.to_email.ilike(like),
            Candidate.first_name.ilike(like),
            Candidate.last_name.ilike(like),
        ))
    return stmt


# NOTE: literal route — MUST stay above GET /{candidate_id}.
@router.get("/email-threads")
def email_threads(pp: PageParams = Depends(page_params),
                  status: str | None = None,
                  folder: str | None = None,
                  event: str | None = None,
                  sort: str = "date",
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    """ONE ROW PER CONVERSATION (3 Sep 2026, user report: "all candidate
    emails mixed with each other").

    `email-conversations` pages over individual mails and the UI grouped them
    client-side, so a candidate with mail on two pages showed twice, the
    order was the order of whichever mail happened to land on the page, and
    "50 emails" bore no relation to the number of people in the list. This
    groups SERVER-side — by the stamped candidate, else the address-matched
    candidate, else the bare address — and pages over conversations, newest
    activity first (or by name with `sort=name`). Each row carries the latest
    mail, the count and how many failed, so the list renders without a
    second call; the thread itself still comes from `email-conversations`.
    """
    from sqlalchemy import func as sa_func, and_ as sa_and, case as sa_case, cast as sa_cast
    from models import EmailOutbox, EmailStatus

    is_stamped = sa_and(EmailOutbox.related_type == "candidate",
                        EmailOutbox.related_id.isnot(None))
    cand_key = sa_func.coalesce(
        sa_case((is_stamped, EmailOutbox.related_id), else_=None), Candidate.id)
    # "cand:<id>" or "mail:<address>" — the same key the UI uses, so the
    # selected conversation survives a reload of either list.
    thread_key = sa_func.coalesce(
        sa.literal("cand:") + sa_cast(cand_key, sa.String),
        sa.literal("mail:") + sa_func.lower(EmailOutbox.to_email),
    ).label("thread_key")
    part = [thread_key]
    inner = (
        select(
            EmailOutbox.id.label("mail_id"),
            thread_key,
            sa_func.row_number().over(
                partition_by=part,
                order_by=[EmailOutbox.created_at.desc(), EmailOutbox.id.desc()]).label("rn"),
            sa_func.count().over(partition_by=part).label("cnt"),
            sa_func.sum(sa_case((EmailOutbox.status == EmailStatus.FAILED, 1), else_=0))
                .over(partition_by=part).label("failed"),
            sa_func.sum(sa_case((EmailOutbox.status == EmailStatus.QUEUED, 1), else_=0))
                .over(partition_by=part).label("queued"),
            sa_func.max(EmailOutbox.created_at).over(partition_by=part).label("latest_at"),
            sa_func.min(EmailOutbox.created_at).over(partition_by=part).label("first_at"),
        )
        .outerjoin(Candidate, sa_func.lower(Candidate.email) == sa_func.lower(EmailOutbox.to_email))
        .where(sa.or_(is_stamped, Candidate.id.isnot(None)))
    )
    inner = _mailbox_scope(inner, status=status, folder=folder, event=event, search=pp.search)
    sub = inner.subquery("t")
    stmt = select(sub).where(sub.c.rn == 1)
    if sort == "name":
        # Name lives on the mail row (to_name) — good enough for A→Z without
        # a second join; ties fall back to recency.
        stmt = (stmt.join(EmailOutbox, EmailOutbox.id == sub.c.mail_id)
                .order_by(sa_func.lower(sa_func.coalesce(EmailOutbox.to_name, EmailOutbox.to_email)).asc(),
                          sub.c.latest_at.desc()))
    else:
        stmt = stmt.order_by(sub.c.latest_at.desc(), sub.c.mail_id.desc())

    total = db.execute(select(sa_func.count()).select_from(stmt.order_by(None).subquery())).scalar() or 0
    page_rows = db.execute(stmt.offset((pp.page - 1) * pp.limit).limit(pp.limit)).all()
    meta = {"page": pp.page, "limit": pp.limit, "total": int(total),
            "pages": max(1, -(-int(total) // pp.limit))}

    ids = [r.mail_id for r in page_rows]
    mails = {m.id: m for m in db.execute(
        select(EmailOutbox).where(EmailOutbox.id.in_(ids or [0]))).scalars().all()}
    emails = {(m.to_email or "").lower() for m in mails.values()}
    stamped_ids = {m.related_id for m in mails.values()
                   if m.related_type == "candidate" and m.related_id is not None}
    cand_by_email: dict[str, tuple[int, str, str]] = {}
    cand_by_id: dict[int, tuple[str, str]] = {}
    if emails or stamped_ids:
        for cid, cfirst, clast, cemail in db.execute(
            select(Candidate.id, Candidate.first_name, Candidate.last_name, Candidate.email)
            .where(sa.or_(
                sa_func.lower(Candidate.email).in_(emails or {""}),
                Candidate.id.in_(stamped_ids or {0}),
            ))
        ).all():
            name = " ".join(p for p in [cfirst, clast] if p)
            cand_by_id[cid] = (name, cemail or "")
            cand_by_email.setdefault((cemail or "").lower(), (cid, name, cemail or ""))

    def _iso(v):
        return v.isoformat() if v else None
    data = []
    for r in page_rows:
        m = mails.get(r.mail_id)
        if m is None:
            continue
        if m.related_type == "candidate" and m.related_id in cand_by_id:
            cid = m.related_id
            cname, cemail = cand_by_id[cid]
        else:
            cid, cname, cemail = cand_by_email.get((m.to_email or "").lower(), (None, "", ""))
        snippet = (m.body_text or "").strip()
        data.append({
            "key": r.thread_key,
            "candidate_id": cid,
            "candidate_name": cname or m.to_name or "",
            "candidate_email": cemail or (m.to_email if (m.event or "").startswith("candidate.") else ""),
            "to_email": m.to_email,
            "count": int(r.cnt or 0),
            "failed": int(r.failed or 0),
            "queued": int(r.queued or 0),
            "latest_at": _iso(r.latest_at),
            "first_at": _iso(r.first_at),
            "latest": {
                "id": m.id,
                "event": m.event,
                "event_label": CANDIDATE_MAIL_EVENTS.get(m.event, m.event),
                "subject": m.subject,
                "snippet": " ".join(snippet.split())[:140],
                "status": m.status.value if hasattr(m.status, "value") else str(m.status),
                "from_name": m.from_name,
                "created_at": _iso(m.created_at),
                "sent_at": _iso(m.sent_at),
            },
        })
    return envelope(data=data, meta=meta)


# NOTE: literal route — MUST stay above GET /{candidate_id} or FastAPI tries
# to coerce "check-duplicates" into an int.
@router.get("/check-duplicates")
def check_duplicates(
    phone: str | None = None,
    email: str | None = None,
    name: str | None = None,
    db: Session = Depends(get_crm_db),
    user: CurrentUser = Depends(any_crm_role),
):
    """Possible duplicates BEFORE a candidate is created — a warning, not a wall.

    The same person arrives through the portal, a referral and a Zoho import,
    and untangling two half-populated records after profiles hang off both is
    miserable. The exact-email 409 on create catches only the narrowest case;
    this catches the rest and lets the recruiter decide:

    * **Phone** — last 10 digits, so "+91 81234 56789" matches "8123456789".
      The strongest signal: people re-type numbers, not addresses.
    * **Email** — case-insensitive, EXCLUDING the synthesised
      ``@import.karnex.in`` placeholders (unique hashes; matching them would
      be noise and they are never a real person's address).
    * **Name** — normalised full-name equality. Weakest signal, so it is
      reported last and never on its own for very short names (<6 chars —
      "A Ku" would flag half the database).
    """
    def digits(v: str | None) -> str:
        return "".join(ch for ch in (v or "") if ch.isdigit())[-10:]

    def norm_name(v: str | None) -> str:
        return "".join(ch for ch in (v or "").lower() if ch.isalpha())

    want_phone = digits(phone)
    want_email = (email or "").strip().lower()
    want_name = norm_name(name)

    matches: dict[int, dict] = {}

    def note(c: Candidate, reason: str) -> None:
        entry = matches.setdefault(c.id, {
            "id": c.id,
            "name": " ".join(p for p in [c.first_name, c.last_name] if p),
            "email": c.email,
            "phone": c.phone,
            "created_at": c.source_created_date.isoformat() if c.source_created_date else None,
            "match_on": [],
        })
        if reason not in entry["match_on"]:
            entry["match_on"].append(reason)

    # One pass over a bounded candidate set per signal — each is an indexed-ish
    # narrow query rather than a full-table scan in Python.
    if want_email and not want_email.endswith("@import.karnex.in"):
        for c in db.execute(select(Candidate).where(
                func.lower(Candidate.email) == want_email).limit(5)).scalars():
            note(c, "email")
    if len(want_phone) >= 7:
        # Normalise INSIDE the query so "+91 81234 56789" matches "8123456789":
        # strip the separators people actually type, then suffix-match.
        stripped = Candidate.phone
        for sep in (" ", "-", "+", "(", ")", "."):
            stripped = func.replace(stripped, sep, "")
        for c in db.execute(select(Candidate).where(
                Candidate.phone.isnot(None),
                stripped.like(f"%{want_phone}")).limit(25)).scalars():
            if digits(c.phone) == want_phone:
                note(c, "phone")
    if len(want_name) >= 6:
        for c in db.execute(select(Candidate).where(
                func.lower(func.coalesce(Candidate.first_name, ""))
                .like(f"{(name or '').strip().split(' ')[0].lower()}%")).limit(50)).scalars():
            full = norm_name(" ".join(p for p in [c.first_name, c.middle_name, c.last_name] if p))
            if full == want_name:
                note(c, "name")

    # Strongest evidence first: more signals, then phone > email > name.
    weight = {"phone": 0, "email": 1, "name": 2}
    out = sorted(matches.values(),
                 key=lambda m: (-len(m["match_on"]),
                                min(weight[r] for r in m["match_on"])))
    return envelope(data=out[:5])


def _reject_impossible_ctc(values: dict) -> None:
    """400 on an out-of-scale CTC (2 Sep 2026 bug report: ₹1,00,00,00,000).

    CTC is stored in RUPEES while several UI fields are labelled "(Lac)" and
    multiply by 100,000 — a rupee figure typed into one of those is 10^5 too
    big. Nothing checked, so the number rendered as fact on the Applicants tab.
    """
    from services.ctc import ctc_looks_wrong

    labels = {"current_ctc": "Current CTC", "expected_ctc": "Expected CTC",
              "ctc_approval_amount": "Approved budget", "ctc": "CTC"}
    for key, label in labels.items():
        if key in values:
            reason = ctc_looks_wrong(values[key])
            if reason:
                raise HTTPException(status_code=400, detail=f"{label}: {reason}")


@router.post("")
def create_candidate(payload: CandidateCreate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(create_candidates)):
    if _email_taken(db, payload.email):
        raise HTTPException(status_code=409,
                            detail=f"A candidate with email '{payload.email}' already exists")
    _reject_impossible_ctc(payload.model_dump())
    candidate = Candidate(**payload.model_dump())
    db.add(candidate)
    db.commit()
    db.refresh(candidate)
    return envelope(data=candidate_to_dict(candidate), message="Candidate created")


@router.get("/{candidate_id}")
def get_candidate(candidate_id: int,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(any_crm_role)):
    candidate = get_candidate_or_404(db, candidate_id)
    return envelope(data=candidate_detail(db, candidate))


@router.get("/{candidate_id}/emails")
def candidate_emails(candidate_id: int,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(any_crm_role)):
    """Every email the system sent TO this candidate (Aug 2026).

    Sourced from the durable email outbox, so it covers interview invites,
    hiring-interest mails, slot confirmations — with status, sender identity
    and the recruiter whose inbox receives the reply (Reply-To). NOTE: this is
    the OUTBOUND half only; candidate replies go to the recruiter's mailbox —
    Karnex has no inbound mail receiver.
    """
    from sqlalchemy import func as sa_func
    from models import EmailOutbox

    candidate = get_candidate_or_404(db, candidate_id)
    email = (candidate.email or "").strip().lower()
    if not email or email.endswith("@import.karnex.in"):
        return envelope(data=[], message="Candidate has no real email address")
    rows = db.execute(
        select(EmailOutbox)
        .where(sa_func.lower(EmailOutbox.to_email) == email)
        .order_by(EmailOutbox.created_at.desc())
        .limit(200)
    ).scalars().all()
    def _iso(v):
        return v.isoformat() if v else None
    return envelope(data=[{
        "id": r.id,
        "event": r.event,
        "subject": r.subject,
        "body_text": r.body_text,
        "status": r.status.value if hasattr(r.status, "value") else str(r.status),
        "from_name": r.from_name,
        "reply_to_email": r.reply_to_email,
        "reply_to_name": r.reply_to_name,
        "attempts": r.attempts,
        "last_error": r.last_error,
        "created_at": _iso(r.created_at),
        "sent_at": _iso(r.sent_at),
    } for r in rows])


@router.put("/{candidate_id}")
def update_candidate(candidate_id: int, payload: CandidateUpdate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    updates = payload.model_dump(exclude_unset=True)
    # Field-level template enforcement — the API twin of the greyed inputs.
    from services.access_templates import reject_view_only_fields
    reject_view_only_fields(db, user.id, set(user.roles), "candidates", updates, {
        "salutation": "name", "first_name": "name", "middle_name": "name", "last_name": "name",
        "email": "email", "phone": "phone",
        "experience_years": "experience_years", "notice_period": "notice_period",
        "current_ctc": "current_ctc", "expected_ctc": "expected_ctc",
        "resignation_status": "resignation", "last_working_day": "resignation",
        "resignation_certificate_url": "resignation",
    })
    new_email = updates.get("email")
    if new_email and _email_taken(db, new_email, exclude_id=candidate.id):
        raise HTTPException(status_code=409,
                            detail=f"A candidate with email '{new_email}' already exists")
    _reject_impossible_ctc(updates)
    for field, value in updates.items():
        setattr(candidate, field, value)
    db.commit()
    db.refresh(candidate)
    return envelope(data=candidate_to_dict(candidate), message="Candidate updated")


@router.delete("/{candidate_id}")
def delete_candidate(candidate_id: int,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    from models import CandidateOutreach, Resume
    from services.crm_common import commit_or_conflict
    from services.crm_delete import cascade_candidate_children

    candidate = get_candidate_or_404(db, candidate_id)
    # Cascade profiles, AI interview links, and slot bookings owned by this candidate.
    cascade_candidate_children(db, candidate.id)
    for row in db.execute(
        select(CandidateOutreach).where(CandidateOutreach.candidate_id == candidate.id)
    ).scalars().all():
        db.delete(row)
    for resume in db.execute(
        select(Resume).where(Resume.candidate_id == candidate.id)
    ).scalars().all():
        resume.candidate_id = None
    db.delete(candidate)
    commit_or_conflict(db, "Cannot delete: candidate is still referenced by other records.")
    return envelope(message="Candidate deleted")


# ---------------------------------------------------------------------------
# CV upload
# ---------------------------------------------------------------------------

def _autofill_from_cv(db: Session, candidate: Candidate) -> dict:
    """Parse the candidate's stored CV and fill empty fields + missing child
    records. Best-effort: swallows extraction errors and returns a summary."""
    summary = {"fields": [], "skills": 0, "education": 0, "experience": 0}
    if not candidate.cv_url:
        return summary
    try:
        from ai import parse_cv_profile
        from services.resumes import extract_resume_text
        cv_text = extract_resume_text(candidate.cv_url)
        profile = parse_cv_profile(cv_text)
        summary = apply_cv_profile_to_candidate(db, candidate, profile)
    except Exception:
        pass  # parsing is best-effort — never block the upload / request
    return summary


@router.post("/{candidate_id}/cv")
def upload_cv(candidate_id: int,
              file: UploadFile = File(...),
              db: Session = Depends(get_crm_db),
              user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.cv_url = save_upload(file, "cv")
    db.flush()
    # Auto-populate empty candidate details (domain, experience, skills, education,
    # experience history, CTC, LinkedIn) from the freshly uploaded CV.
    filled = _autofill_from_cv(db, candidate)
    db.commit()
    return envelope(
        data={"cv_url": candidate.cv_url, "autofilled": filled},
        message="CV uploaded",
    )


class ApplyToRequirementIn(BaseModel):
    requirement_id: int


_APPLY_ALLOWED_STATUSES = (
    RequirementStatus.OPEN_FOR_SOURCING,
    RequirementStatus.POSTED_ON_PORTALS,
    RequirementStatus.IN_PROGRESS,
)


@router.post("/{candidate_id}/apply")
def apply_candidate_to_requirement(
    candidate_id: int,
    payload: ApplyToRequirementIn,
    db: Session = Depends(get_crm_db),
    # Sourcing is TA's job — only TA (and Admin/CEO implicitly) may fast-track apply.
    user: CurrentUser = Depends(role_required("TA")),
):
    """TA fast-track: apply an existing candidate directly to a requirement.

    Reuses the candidate's stored CV and profile details to create the
    application (Resume row) — no re-upload/re-typing — then best-effort runs
    the ATS scan so the score shows immediately in the requirement's Resumes tab.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    req = db.get(Requirement, payload.requirement_id)
    if req is None:
        raise HTTPException(status_code=404, detail="Requirement not found")
    if req.status not in _APPLY_ALLOWED_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Candidates can only be applied while the requirement is sourcing "
                   f"(current: {req.status.value})",
        )
    if not candidate.cv_url:
        raise HTTPException(status_code=400,
                            detail="Candidate has no CV on file — upload a CV on the candidate first.")
    dup = db.execute(
        select(Resume.id).where(Resume.requirement_id == req.id,
                                Resume.candidate_id == candidate.id)
    ).first()
    if dup:
        raise HTTPException(status_code=409,
                            detail="This candidate has already been applied to this requirement.")

    # Application details assembled from the candidate's profile.
    skill_names = db.execute(
        select(Skill.name).join(CandidateSkill, CandidateSkill.skill_id == Skill.id)
        .where(CandidateSkill.candidate_id == candidate.id)
    ).scalars().all()
    first_edu = db.execute(
        select(CandidateEducation.course)
        .where(CandidateEducation.candidate_id == candidate.id)
        .order_by(CandidateEducation.id).limit(1)
    ).scalar_one_or_none()
    details = {
        "education": first_edu,
        "technical_domain": candidate.technical_domain,
        "skills": ", ".join(skill_names) or None,
        "current_ctc": (str(candidate.current_ctc) if getattr(candidate, "current_ctc", None) is not None else None),
        "expected_ctc": (str(candidate.expected_ctc) if candidate.expected_ctc is not None else None),
    }
    details = {k: v for k, v in details.items() if v}
    exp_years = getattr(candidate, "experience_years", None)

    full_name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p) or "Candidate"
    resume = Resume(
        requirement_id=req.id,
        candidate_id=candidate.id,
        candidate_name=full_name,
        email=candidate.email,
        phone=candidate.phone,
        source_portal="TA Sourced",
        applicant_experience=(str(exp_years) if exp_years is not None else None),
        application_details=details or None,
        resume_file_url=candidate.cv_url,
    )
    db.add(resume)
    db.flush()
    log_activity(db, RequirementActivityLog, "requirement_id", req.id, user.id,
                 "RESUME_UPLOADED",
                 f"TA applied existing candidate {full_name} directly from the Candidates tab")
    if req.status == RequirementStatus.POSTED_ON_PORTALS:
        req.status = RequirementStatus.IN_PROGRESS

    # Best-effort immediate ATS scan so the score appears without an extra click.
    ats_score = None
    try:
        from services.resumes import run_ats_scan
        result = run_ats_scan(db, resume, req, user.id)
        ats_score = result.get("ats_score")
    except Exception:
        pass  # scan can be run manually from the Resumes tab

    db.commit()
    db.refresh(resume)
    return envelope(
        data={"resume_id": resume.id, "requirement_id": req.id,
              "req_number": req.req_number, "ats_score": ats_score},
        message=f"{full_name} applied to {requirement_label(req)}"
                + (f" — ATS score {ats_score}" if ats_score is not None else ""),
    )


@router.post("/{candidate_id}/resignation-certificate")
def upload_resignation_certificate(candidate_id: int,
                                   file: UploadFile = File(...),
                                   db: Session = Depends(get_crm_db),
                                   user: CurrentUser = Depends(write_roles)):
    """Attach the candidate's resignation / relieving certificate.

    Stored on the CANDIDATE, not the application: a person resigns from one job
    once, whatever number of opportunities they are put forward for. Every
    Candidate Profile for them then shows it, so Sales and Sales Head can see the
    proof without asking TA for the file.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.resignation_certificate_url = save_upload(file, "resignation")
    # Uploading the certificate is itself the statement that they have resigned.
    if not candidate.resignation_status:
        candidate.resignation_status = True
    db.commit()
    db.refresh(candidate)
    return envelope(
        data={
            "resignation_certificate_url": candidate.resignation_certificate_url,
            "resignation_status": bool(candidate.resignation_status),
        },
        message="Resignation certificate uploaded",
    )


@router.delete("/{candidate_id}/resignation-certificate")
def remove_resignation_certificate(candidate_id: int,
                                   db: Session = Depends(get_crm_db),
                                   user: CurrentUser = Depends(write_roles)):
    """Detach the certificate (e.g. the wrong file was uploaded).

    Leaves resignation_status alone — the candidate may still have resigned even
    if the document needs replacing.
    """
    candidate = get_candidate_or_404(db, candidate_id)
    candidate.resignation_certificate_url = None
    db.commit()
    return envelope(data={"resignation_certificate_url": None},
                    message="Resignation certificate removed")


@router.post("/{candidate_id}/parse-cv")
def parse_cv(candidate_id: int,
             db: Session = Depends(get_crm_db),
             user: CurrentUser = Depends(write_roles)):
    """Re-parse the candidate's existing CV and fill any still-empty fields /
    missing child records (skills, education, experience). Existing data is kept."""
    candidate = get_candidate_or_404(db, candidate_id)
    if not candidate.cv_url:
        raise HTTPException(status_code=400, detail="No CV on file to parse. Upload a CV first.")
    filled = _autofill_from_cv(db, candidate)
    db.commit()
    return envelope(data={"autofilled": filled}, message="CV parsed")


# ---------------------------------------------------------------------------
# Education
# ---------------------------------------------------------------------------

def _get_education_or_404(db: Session, candidate_id: int, edu_id: int) -> CandidateEducation:
    edu = db.get(CandidateEducation, edu_id)
    if not edu or edu.candidate_id != candidate_id:
        raise HTTPException(status_code=404, detail="Education record not found for this candidate")
    return edu


@router.post("/{candidate_id}/education")
def add_education(candidate_id: int, payload: EducationCreate,
                  db: Session = Depends(get_crm_db),
                  user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    edu = CandidateEducation(candidate_id=candidate.id, **payload.model_dump())
    db.add(edu)
    db.commit()
    db.refresh(edu)
    return envelope(data=education_to_dict(edu), message="Education added")


@router.put("/{candidate_id}/education/{edu_id}")
def update_education(candidate_id: int, edu_id: int, payload: EducationUpdate,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    edu = _get_education_or_404(db, candidate_id, edu_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(edu, field, value)
    db.commit()
    db.refresh(edu)
    return envelope(data=education_to_dict(edu), message="Education updated")


@router.delete("/{candidate_id}/education/{edu_id}")
def delete_education(candidate_id: int, edu_id: int,
                     db: Session = Depends(get_crm_db),
                     user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    edu = _get_education_or_404(db, candidate_id, edu_id)
    db.delete(edu)
    db.commit()
    return envelope(message="Education deleted")


# ---------------------------------------------------------------------------
# Experience
# ---------------------------------------------------------------------------

def _get_experience_or_404(db: Session, candidate_id: int, exp_id: int) -> CandidateExperience:
    exp = db.get(CandidateExperience, exp_id)
    if not exp or exp.candidate_id != candidate_id:
        raise HTTPException(status_code=404, detail="Experience record not found for this candidate")
    return exp


@router.post("/{candidate_id}/experience")
def add_experience(candidate_id: int, payload: ExperienceCreate,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    exp = CandidateExperience(candidate_id=candidate.id, **payload.model_dump())
    db.add(exp)
    db.commit()
    db.refresh(exp)
    return envelope(data=experience_to_dict(exp), message="Experience added")


@router.put("/{candidate_id}/experience/{exp_id}")
def update_experience(candidate_id: int, exp_id: int, payload: ExperienceUpdate,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(exp, field, value)
    db.commit()
    db.refresh(exp)
    return envelope(data=experience_to_dict(exp), message="Experience updated")


@router.delete("/{candidate_id}/experience/{exp_id}")
def delete_experience(candidate_id: int, exp_id: int,
                      db: Session = Depends(get_crm_db),
                      user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    db.delete(exp)
    db.commit()
    return envelope(message="Experience deleted")


@router.post("/{candidate_id}/experience/{exp_id}/certificate")
def upload_experience_certificate(candidate_id: int, exp_id: int,
                                  file: UploadFile = File(...),
                                  db: Session = Depends(get_crm_db),
                                  user: CurrentUser = Depends(write_roles)):
    get_candidate_or_404(db, candidate_id)
    exp = _get_experience_or_404(db, candidate_id, exp_id)
    exp.certificate_url = save_upload(file, "certificates")
    db.commit()
    return envelope(data={"certificate_url": exp.certificate_url}, message="Certificate uploaded")


# ---------------------------------------------------------------------------
# Skills (replace full set)
# ---------------------------------------------------------------------------

@router.post("/{candidate_id}/skills")
def replace_skills(candidate_id: int, payload: SkillSetIn,
                   db: Session = Depends(get_crm_db),
                   user: CurrentUser = Depends(write_roles)):
    candidate = get_candidate_or_404(db, candidate_id)
    skill_ids = ensure_skills_exist(db, payload.skill_ids)
    db.execute(sa.delete(CandidateSkill).where(CandidateSkill.candidate_id == candidate.id))
    for sid in skill_ids:
        db.add(CandidateSkill(candidate_id=candidate.id, skill_id=sid))
    db.commit()
    return envelope(data={"candidate_id": candidate.id, "skill_ids": skill_ids},
                    message="Candidate skills replaced")
