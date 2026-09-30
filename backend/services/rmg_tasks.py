"""RMG / GM pending work — ONE list, shown in two places (28 Sep 2026).

User ask: "all RMG & GM pending tasks in the Screening Desk, each opening the
exact place to act — L1 schedule, L2 schedule, feedback pending, template
request … and the same on the Dashboard, so they see it two ways", plus
"when any interview is done, RMG gets told and it stays highlighted until
they have looked".

`screener_tasks(db, user)` is the list. The Screening Desk header renders it
as a task board (`GET /api/screening-desk/tasks`) and the Dashboard work desk
(`services/work_desk.py`) turns the same categories into its tabs — so the
two can never disagree. No rule is invented here: the candidate categories
come from the desk's own `next_step` (`services/screening_desk.py`), feedback
from `interview_followups`, approvals from `screening_desk.approvals_queue`.

Every item is `{key, title, subtitle, chip, tone, when, path, action,
profile_id}` — `path` is a CRM path. A candidate on the desk opens the desk
AT that candidate (`screening-desk?task=<cat>&focus=<id>`), where the exact
button (Shortlist, Choose route, Book L1, Record feedback, Submit to Sales)
is in the detail pane; anything else opens its own page.

"Interview results to review" — the highlight. An interview result becomes a
row the moment it exists (AI L1 completed, or a round's verdict recorded by
someone who is not a screener) and stays until a screener presses "Mark
reviewed" (`mark_reviewed`). Both facts live in the profile's activity log
(`RESULT_RECORDED`, `RESULT_REVIEWED`, comment = the result key `ai:<link>` /
`round:<event>`), so no migration and a full audit trail. A screener who
records a verdict themselves has, by definition, seen it: it is logged as
reviewed at once and nobody is notified.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import (
    AiInterviewLink, Candidate, CandidateProfile, CandidateProfileActivityLog, InterviewEvent,
    Opportunity, PipelineStatus, Requirement, RequirementStatus,
)

logger = logging.getLogger("karnex.crm.rmg_tasks")
PS = PipelineStatus

RESULT_RECORDED = "RESULT_RECORDED"
RESULT_REVIEWED = "RESULT_REVIEWED"
#: How far back a finished interview still asks to be looked at.
REVIEW_WINDOW_DAYS = 21
#: Candidate categories read at most this many desk rows (a to-do list).
MAX_DESK_ROWS = 1000
MAX_ITEMS = 50
#: Rounds whose result a screener is told about. HR's verdict is HR's business.
REVIEWED_KINDS = ("L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview",
                  "Customer_Interview", "Customer_L2")
#: Stages where nothing is left to do with a result.
_CLOSED = tuple(s.value for s in (PS.SALES_REJECTED, PS.RMG_REJECTED, PS.CUSTOMER_REJECTED,
                                   PS.SELF_WITHDRAWN, PS.REJECTED, PS.JOINED))

#: key → (label, hint, icon, on_desk). `on_desk` categories filter the desk's
#: queue (`?task=`); the others open their own page. Reading order = urgency.
CATEGORIES: dict[str, tuple[str, str, str, bool]] = {
    "feedback": ("Feedback due", "The interview time has passed and no verdict is recorded.",
                 "message", True),
    "results": ("Results to review", "An interview finished — open the report, then mark it reviewed.",
                "sparkles", True),
    "screening": ("To screen", "New applicants waiting for your Shortlist / Reject.", "inbox", True),
    "route": ("Choose L1 route", "Shortlisted — pick the AI L1 or a manual L1.", "route", True),
    "booking": ("L1 / L2 to book", "A round was asked for and is not booked yet — TA books it, or book it yourself.",
                "calendar", True),
    "decide": ("Submit to Sales", "Rounds are judged — submit to Sales, ask for an L2, or reject. "
               "The desk also lists who you handed over lately and where they are now.",
               "send", True),
    "ai_failed": ("AI L1 not cleared", "Reject, or take a manual L1 when the AI read looks wrong.",
                  "alert", True),
    "approvals": ("Positions to approve", "New positions waiting for your JD & skills approval.",
                  "check", False),
    "jd": ("JD / skills missing", "Live positions with no RMG JD and no skills — ATS cannot score them.",
           "file", False),
    "headcount": ("Headcount changes", "Sales asked to change the number of positions.", "users", False),
    "templates": ("Template requests", "TA needs an interview template for a position.", "layout", False),
    # The GM's billing chain (29 Sep 2026, user ask: "timesheets pending for approval,
    # Proforma and original invoices on the GM's Dashboard and Screening Desk").
    # Present only for a login that may approve timesheets / raise the Proforma.
    "ts_approve": ("Timesheets to approve", "Sales submitted these sheets — review, approve or reject.",
                   "clock", False),
    "proforma_raise": ("Proformas to raise", "Approved sheets with no invoice yet, and Proformas Finance "
                       "returned — raise or reissue the Proforma.", "receipt", False),
    "proforma_finance": ("Proformas with Finance", "Raised and waiting for Finance to issue the tax invoice.",
                         "hourglass", False),
    "invoices_issued": ("Tax invoices issued", "Original invoices Finance issued in the last 30 days.",
                        "rupee", False),
}
#: Billing categories → (the approval that brings them, the `billing_chain` list).
BILLING_CATEGORIES: dict[str, tuple[str, str]] = {
    "ts_approve": ("timesheet.approve", "submitted"),
    "proforma_raise": ("timesheet.generate_invoice", "awaiting"),
    "proforma_finance": ("timesheet.generate_invoice", "proformas"),
    "invoices_issued": ("timesheet.generate_invoice", "issued"),
}
#: Information, not work: never counted in "N pending", and the tile says "None"
#: at zero instead of "All clear".
INFO_CATEGORIES = frozenset({"proforma_finance", "invoices_issued"})
#: next_step key (screening_desk.next_step) → category.
_STEP_CATEGORY = {
    "screen": "screening", "route": "route", "decide": "decide", "ai_failed": "ai_failed",
    "l1_book": "booking", "l2_book": "booking", "ai_book": "booking",
}
_BOOK_CHIP = {"l1_book": "Technical L1", "l2_book": "Technical L2", "ai_book": "AI L1"}


def _item(key, title, subtitle="", *, chip=None, tone="info", when=None, path="", action=None,
          profile_id=None) -> dict:
    return {"key": key, "title": title, "subtitle": subtitle, "chip": chip, "tone": tone,
            "when": when, "path": path, "action": action, "profile_id": profile_id}


def desk_path(category: str, profile_id: int) -> str:
    return f"screening-desk?task={category}&focus={profile_id}"


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _aware(dt):
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _name(c) -> str:
    return " ".join(x for x in (c.first_name, c.last_name) if x) or f"Candidate #{c.id}"


# ------------------------------------------------------------------ results to review


def result_key(kind: str, ident) -> str:
    return f"{kind}:{ident}"


def record_round_result(db: Session, profile: CandidateProfile, event: InterviewEvent, user,
                        previous_result: str | None) -> None:
    """Called when a round is saved: a NEW or CHANGED verdict becomes a result.

    Logs `RESULT_RECORDED`; a screener recording it is marked as having seen
    it; anyone else (Sales for a customer round, a panel interviewer) sends
    every screener a bell + email. Best-effort — never fails the save."""
    if not event.result or event.result == previous_result or event.kind not in REVIEWED_KINDS:
        return
    from services.action_permissions import screens_as_rmg
    from services.crm_common import log_activity
    from services.interview_rounds import round_label

    key = result_key("round", event.id)
    try:
        with db.begin_nested():
            log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                         RESULT_RECORDED, f"{key} {round_label(event.kind)} — {event.result}")
            if screens_as_rmg(db, user):
                log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                             RESULT_REVIEWED, key)
                return
            _notify_result(db, profile, f"{round_label(event.kind)} — {event.result}",
                           f"{getattr(user, 'full_name', None) or 'Someone'} recorded the verdict"
                           f"{f': {event.feedback[:200]}' if event.feedback else '.'}",
                           exclude=user.id, dedupe=f"result:{key}:{event.result}")
    except Exception:
        logger.warning("could not record round result for profile %s", profile.id, exc_info=True)


def _notify_result(db: Session, profile: CandidateProfile, headline: str, message: str,
                   *, exclude: int | None = None, dedupe: str | None = None) -> None:
    from services.candidate_profiles import screening_notify_user_ids
    from services.notify import notify_role

    cand = db.get(Candidate, profile.candidate_id)
    stage = getattr(profile.pipeline_status, "value", profile.pipeline_status)
    from services.screening_desk import DESK_STAGES
    path = (desk_path("results", profile.id) if stage in DESK_STAGES
            else f"profiles/{profile.id}?tab=interviews")
    notify_role(db, "RMG", f"Interview done — {_name(cand) if cand else 'candidate'}: {headline}",
                message, f"/admin/?view=crm&p={path.replace('?', '&', 1)}",
                exclude_user_id=exclude, event="interview.result_recorded",
                dedupe_prefix=dedupe, user_ids=screening_notify_user_ids(db))


def unreviewed_results(db: Session, profile_ids=None, *, now: datetime | None = None) -> dict[int, list[dict]]:
    """{profile_id: [result, …]} newest first — finished interviews no screener
    has marked reviewed. `profile_ids=None` = every live profile."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=REVIEW_WINDOW_DAYS)
    ids = None if profile_ids is None else [int(p) for p in set(profile_ids) if p]
    if ids is not None and not ids:
        return {}
    live = (select(CandidateProfile.id)
            .where(func.coalesce(CandidateProfile.is_hidden, False).is_(False),
                   CandidateProfile.pipeline_status.notin_([PS(s) for s in _CLOSED])))
    if ids is not None:
        live = live.where(CandidateProfile.id.in_(ids))
    live_ids = set(db.execute(live).scalars().all())
    if not live_ids:
        return {}

    reviewed = {(pid, (c or "").split(" ")[0]) for pid, c in db.execute(
        select(CandidateProfileActivityLog.profile_id, CandidateProfileActivityLog.comment)
        .where(CandidateProfileActivityLog.action_type == RESULT_REVIEWED,
               CandidateProfileActivityLog.profile_id.in_(live_ids))).all()}
    out: dict[int, list[dict]] = {}

    for link in db.execute(
        select(AiInterviewLink).where(AiInterviewLink.profile_id.in_(live_ids),
                                      AiInterviewLink.completed_at.isnot(None),
                                      AiInterviewLink.completed_at >= since)
    ).scalars().all():
        key = result_key("ai", link.id)
        if (link.profile_id, key) in reviewed:
            continue
        score = float(link.overall_score_percent) if link.overall_score_percent is not None else None
        out.setdefault(link.profile_id, []).append({
            "key": key, "kind": "ai", "label": "AI L1", "result": link.effective_result,
            "score": score, "when": _iso(_aware(link.completed_at)), "by": "AI interview",
            "passed": link.effective_result in ("Passed", "Selected"),
        })

    recorded = db.execute(
        select(CandidateProfileActivityLog)
        .where(CandidateProfileActivityLog.action_type == RESULT_RECORDED,
               CandidateProfileActivityLog.profile_id.in_(live_ids),
               CandidateProfileActivityLog.timestamp >= since)
        .order_by(CandidateProfileActivityLog.id.desc())
    ).scalars().all()
    latest: dict[str, CandidateProfileActivityLog] = {}
    for row in recorded:
        key = (row.comment or "").split(" ")[0]
        if key.startswith("round:") and key not in latest:
            latest[key] = row
    event_ids = [int(k.split(":")[1]) for k in latest if k.split(":")[1].isdigit()]
    events = {e.id: e for e in db.execute(
        select(InterviewEvent).where(InterviewEvent.id.in_(event_ids))).scalars().all()} if event_ids else {}
    from services.interview_rounds import round_label
    for key, row in latest.items():
        ev = events.get(int(key.split(":")[1])) if key.split(":")[1].isdigit() else None
        if ev is None or not ev.result or (row.profile_id, key) in reviewed:
            continue
        out.setdefault(row.profile_id, []).append({
            "key": key, "kind": "round", "label": round_label(ev.kind).replace(" - Interview", ""),
            "result": ev.result, "score": None, "when": _iso(_aware(row.timestamp)),
            "by": ev.interviewer or None,
            "passed": ev.result in ("Hire", "Strong Hire", "Leaning Hire"),
        })
    for rows in out.values():
        rows.sort(key=lambda r: r["when"] or "", reverse=True)
    return out


def mark_reviewed(db: Session, profile: CandidateProfile, user, keys: list[str] | None = None) -> int:
    """Mark this candidate's open results (or just `keys`) reviewed. Returns how many."""
    from services.crm_common import log_activity
    open_keys = [r["key"] for r in unreviewed_results(db, [profile.id]).get(profile.id, [])]
    wanted = [k for k in open_keys if keys is None or k in set(keys)]
    for key in wanted:
        log_activity(db, CandidateProfileActivityLog, "profile_id", profile.id, user.id,
                     RESULT_REVIEWED, key)
    return len(wanted)


# ------------------------------------------------------------------ the task list


def _desk_rows(db: Session) -> list[tuple]:
    """(profile, candidate, opportunity, requirement) for every desk row that is
    not screened out — the same `_base` the desk's queue uses."""
    from services.screening_desk import DeskFilters, _base, _latest_requirement

    joined, where = _base(DeskFilters(screening="all"))
    ids = db.execute(joined.where(*where, func.coalesce(CandidateProfile.rmg_screening_status, "")
                                  != "Rejected")
                     .order_by(CandidateProfile.id.desc()).limit(MAX_DESK_ROWS)).scalars().all()
    if not ids:
        return []
    lr = _latest_requirement()
    return db.execute(
        select(CandidateProfile, Candidate, Opportunity, Requirement)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .join(lr, lr.c.opportunity_id == CandidateProfile.opportunity_id)
        .join(Requirement, Requirement.id == lr.c.requirement_id)
        .where(CandidateProfile.id.in_(ids))
        .order_by(CandidateProfile.id.desc())
    ).all()


def _subtitle(opp, req, extra=None) -> str:
    return " · ".join(x for x in (opp.opp_id, req.title if req is not None else opp.title, extra) if x)


def _candidate_categories(db: Session, now: datetime) -> tuple[dict[str, list[dict]], dict[int, tuple]]:
    from services.candidate_profiles import latest_ai_interviews
    from services.resumes import manual_round_state
    from services.screening_desk import next_step

    rows = _desk_rows(db)
    profiles = [r[0] for r in rows]
    ai = latest_ai_interviews(db, profiles)
    rounds = manual_round_state(db, [p.id for p in profiles])
    out: dict[str, list[dict]] = {}
    by_id: dict[int, tuple] = {}
    for p, cand, opp, req in rows:
        by_id[p.id] = (p, cand, opp, req)
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        step = next_step(screening=p.rmg_screening_status, stage=stage, ai=ai.get(p.id),
                         rounds=rounds.get(p.id))
        cat = _STEP_CATEGORY.get(step["key"])
        if cat is None:
            continue
        applied = _aware(p.applied_on or p.created_at)
        waited = (now - applied).days if applied else None
        chip = _BOOK_CHIP.get(step["key"]) or (f"{waited} d waiting" if waited is not None else None)
        tone = "bad" if cat == "ai_failed" or (waited or 0) >= 5 else "warn"
        out.setdefault(cat, []).append(_item(
            f"{cat}:{p.id}:{step['key']}", _name(cand), _subtitle(opp, req, p.ta_owner_name),
            chip=chip, tone=tone, when=_iso(applied), path=desk_path(cat, p.id),
            action=CATEGORIES[cat][0] if cat != "booking" else "Book it", profile_id=p.id))
    return out, by_id


def _results_items(db: Session, desk_ids: set[int], now: datetime) -> list[dict]:
    res = unreviewed_results(db, now=now)
    if not res:
        return []
    rows = db.execute(
        select(CandidateProfile, Candidate, Opportunity)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.id.in_(list(res)))).all()
    items = []
    for p, cand, opp in rows:
        results = res[p.id]
        top = results[0]
        label = f"{top['label']}: {top['result']}" + (f" · {round(top['score'])}%" if top["score"] is not None else "")
        if p.id in desk_ids:
            path = desk_path("results", p.id)
        else:
            path = f"profiles/{p.id}?tab={'ai' if top['kind'] == 'ai' else 'interviews'}"
        items.append(_item(f"results:{p.id}", _name(cand),
                           " · ".join(x for x in (opp.opp_id, opp.title,
                                                  f"+{len(results) - 1} more" if len(results) > 1 else None) if x),
                           chip=label, tone="ok" if top["passed"] else "bad", when=top["when"],
                           path=path, action="Review report", profile_id=p.id))
    items.sort(key=lambda i: i["when"] or "", reverse=True)
    return items


def _feedback_items(db: Session, user, desk_ids: set[int]) -> list[dict]:
    from services.interview_followups import feedback_due_for
    items = []
    for it in feedback_due_for(db, user)["items"]:
        pid = it["profile_id"]
        position = " · ".join(x for x in (it["opportunity_ref"], it["opportunity_title"]) if x)
        row = _item(
            f"feedback:{it['event_id']}", it["candidate_name"], position,
            chip=it["round_label"], tone="bad" if it["overdue_hours"] >= 48 else "warn",
            when=it["scheduled_at"],
            path=desk_path("feedback", pid) if pid in desk_ids else f"profiles/{pid}?tab=interviews",
            action="Record feedback", profile_id=pid)
        # Facets for the desk's Feedback-due panel (30 Sep 2026, user ask: "position
        # wise or day wise"): the position it was for, the round, the interviewer.
        row.update(section=position or "No position", round_kind=it["round_kind"],
                   interviewer=it.get("interviewer"), overdue_hours=it["overdue_hours"])
        items.append(row)
    return items


def _approval_items(db: Session, user) -> list[dict]:
    from services.screening_desk import approvals_queue
    q = approvals_queue(db, user)
    return [_item(f"approvals:{a['requirement_id']}", a["title"],
                  " · ".join(x for x in (a["opp_id"], a["customer_name"],
                                         f"{a['positions']} position(s)") if x),
                  chip="JD missing" if not (a["rmg_jd_text"] or a["has_jd_file"]) else
                  ("Skills missing" if not a["skills"] else "Ready to approve"),
                  tone="warn", when=a["created_at"], path="screening-desk?task=approvals",
                  action="Review & approve") for a in q["items"]]


def _jd_items(db: Session) -> list[dict]:
    from models import RequirementSkill
    live = (RequirementStatus.OPEN_FOR_SOURCING, RequirementStatus.POSTED_ON_PORTALS,
            RequirementStatus.IN_PROGRESS, RequirementStatus.ON_HOLD)
    with_skills = select(RequirementSkill.requirement_id).distinct()
    rows = db.execute(
        select(Requirement, Opportunity)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
        .where(Requirement.status.in_(live),
               func.coalesce(func.trim(Requirement.rmg_jd_text), "") == "",
               Requirement.id.notin_(with_skills))
        .order_by(Requirement.id.desc())).all()
    return [_item(f"jd:{r.id}", r.title, " · ".join(x for x in (o.opp_id, o.title) if x),
                  chip="No JD · no skills", tone="warn", when=_iso(_aware(r.created_at)),
                  path=f"requirements/{r.id}?tab=details", action="Add JD & skills") for r, o in rows]


def _headcount_items(db: Session, user) -> list[dict]:
    from models import RequirementPositionRequest
    from services.action_permissions import user_may
    if not user_may(db, user, "requirement.positions.approve"):
        return []
    rows = db.execute(
        select(RequirementPositionRequest, Requirement)
        .join(Requirement, Requirement.id == RequirementPositionRequest.requirement_id)
        .where(RequirementPositionRequest.status == "Pending")
        .order_by(RequirementPositionRequest.id.asc())).all()
    return [_item(f"headcount:{pr.id}", req.title,
                  f"{pr.from_positions} → {pr.to_positions} positions" + (f" · {pr.reason[:80]}" if pr.reason else ""),
                  chip=f"{pr.from_positions} → {pr.to_positions}", tone="warn",
                  when=_iso(_aware(getattr(pr, "created_at", None))),
                  path=f"requirements/{req.id}", action="Approve / reject") for pr, req in rows]


def _template_items(db: Session, user) -> list[dict]:
    from models import TemplateRequest
    from models.template_requests import TemplateRequestStatus
    from services.dashboard_desk import _permits
    _roles, _admin, allowed = _permits(db, user)
    if not allowed("template-requests", "RMG"):
        return []
    rows = db.execute(select(TemplateRequest).where(TemplateRequest.status == TemplateRequestStatus.PENDING_RMG)
                      .order_by(TemplateRequest.id.asc())).scalars().all()
    return [_item(f"templates:{t.id}", t.role_title,
                  " · ".join(x for x in (t.tr_number, t.experience_level) if x),
                  chip=t.tr_number, tone="warn", when=_iso(_aware(t.created_at)),
                  path="template-requests", action="Build template") for t in rows]


def screener_tasks(db: Session, user, *, now: datetime | None = None) -> dict:
    """Every pending task for an RMG / GM login, by category (reading order).

    `{"categories": [{key, label, hint, icon, on_desk, count, items, desk_ids}],
      "total": n, "as_of": iso}` — each category built in its own savepoint;
    one failing is logged and reported empty, never a blank board."""
    now = now or datetime.now(timezone.utc)
    cand, by_id = {}, {}
    try:
        with db.begin_nested():
            cand, by_id = _candidate_categories(db, now)
    except Exception:
        logger.warning("screener task candidates failed", exc_info=True)
    desk_ids = set(by_id)
    builders = {
        "results": lambda: _results_items(db, desk_ids, now),
        "feedback": lambda: _feedback_items(db, user, desk_ids),
        "approvals": lambda: _approval_items(db, user),
        "jd": lambda: _jd_items(db),
        "headcount": lambda: _headcount_items(db, user),
        "templates": lambda: _template_items(db, user),
    }
    billing = _billing(db, user)
    categories = []
    for key, (label, hint, icon, on_desk) in CATEGORIES.items():
        if key in BILLING_CATEGORIES and key not in billing:
            continue
        items = billing.get(key, cand.get(key, []))
        if key in builders:
            try:
                with db.begin_nested():
                    items = builders[key]()
            except Exception:
                logger.warning("screener task %s failed", key, exc_info=True)
                items = []
        categories.append({
            "key": key, "label": label, "hint": hint, "icon": icon, "on_desk": on_desk,
            "count": len(items), "items": items[:MAX_ITEMS],
            "desk_ids": sorted({i["profile_id"] for i in items if i["profile_id"] in desk_ids}),
            "info": key in INFO_CATEGORIES,
            # A permitted billing tab stays on the board at zero: "nothing to approve" is news.
            "always": key in BILLING_CATEGORIES,
        })
    return {"categories": categories,
            "total": sum(c["count"] for c in categories if not c["info"]),
            "as_of": now.isoformat()}


def _billing(db: Session, user) -> dict[str, list[dict]]:
    """The GM's billing categories this login may work, from the SAME chain
    Finance's desk reads (`work_desk.billing_chain`, the GM's words). A category
    is absent when its approval is not the user's; a failure is logged and the
    permitted categories show empty rather than blanking the board."""
    from services.action_permissions import user_may

    allowed = [k for k, (action, _src) in BILLING_CATEGORIES.items() if user_may(db, user, action)]
    if not allowed:
        return {}
    try:
        with db.begin_nested():
            from services.work_desk import billing_chain
            chain = billing_chain(db, audience="gm")
    except Exception:
        logger.warning("GM billing tasks failed", exc_info=True)
        chain = {}
    return {k: chain.get(BILLING_CATEGORIES[k][1], []) for k in allowed}


def task_profile_ids(db: Session, user, task: str) -> list[int]:
    """The desk rows a `?task=` filter keeps (the category's own list)."""
    if task not in CATEGORIES:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"task must be one of {', '.join(CATEGORIES)}")
    for c in screener_tasks(db, user)["categories"]:
        if c["key"] == task:
            return c["desk_ids"]
    return []


# ------------------------------------------------------------------ hand-overs to Sales

#: How far back "recently submitted to Sales" looks.
HANDOVER_DAYS = 30
#: The activity rows that mean "RMG / GM handed this candidate to Sales".
_HANDOVER_PREFIX = f"{PS.RMG_REVIEW.value} -> {PS.SALES_SCREENING.value}"


def recent_handovers(db: Session, *, days: int = HANDOVER_DAYS, limit: int = MAX_ITEMS,
                     now: datetime | None = None) -> list[dict]:
    """Candidates RMG / GM submitted to Sales in the last `days`, newest first
    (29 Sep 2026 report: "Submit to Sales" showed nothing right after a
    submission — the to-do list empties the moment the job is done, so the
    desk also shows what was handed over and where each candidate is NOW).

    Read from the activity log — the `RMG_Review -> Sales_Screening`
    STATUS_CHANGE row the transition writes, and `FAST_TRACKED` for an
    internal candidate sent straight to Sales. Three batched queries; the
    status is the one every screen prints (`candidate_status`)."""
    from services.candidate_status import statuses_for
    from services.revenue_report import _user_names

    now = now or datetime.now(timezone.utc)
    log = CandidateProfileActivityLog
    rows = db.execute(
        select(log.profile_id, log.user_id, log.timestamp, log.action_type, log.comment)
        .where(log.timestamp >= now - timedelta(days=days),
               ((log.action_type == "STATUS_CHANGE") & log.comment.startswith(_HANDOVER_PREFIX))
               | (log.action_type == "FAST_TRACKED"))
        .order_by(log.timestamp.desc(), log.id.desc())
    ).all()
    latest: dict[int, tuple] = {}
    for pid, uid, ts, action, comment in rows:
        latest.setdefault(pid, (uid, ts, action, comment))
    if not latest:
        return []
    ids = list(latest)[:limit]
    found = {p.id: (p, c, o) for p, c, o in db.execute(
        select(CandidateProfile, Candidate, Opportunity)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(CandidateProfile.id.in_(ids),
               func.coalesce(CandidateProfile.is_hidden, False).is_(False))).all()}
    statuses = statuses_for(db, [v[0] for v in found.values()])
    names = _user_names(db, {latest[i][0] for i in ids if latest[i][0]})
    out = []
    for pid in ids:
        if pid not in found:
            continue
        profile, cand, opp = found[pid]
        uid, ts, action, comment = latest[pid]
        note = (comment or "").split(":", 1)[1].strip() if action == "STATUS_CHANGE" and ":" in (comment or "") \
            else (comment or "")
        out.append({
            "profile_id": pid,
            "candidate_name": _name(cand),
            "opportunity_ref": opp.opp_id,
            "opportunity_title": opp.title,
            "submitted_at": _iso(_aware(ts)),
            "submitted_by": names.get(uid) if uid else None,
            "fast_track": action == "FAST_TRACKED",
            "note": note[:400] or None,
            "status": statuses.get(pid),
            "path": f"profiles/{pid}",
        })
    return out
