"""The work desk — every role's daily tasks as tabs on the Dashboard (28 Sep 2026).

User ask: "give tabs like the CEO dashboard, but each role's daily task, separate,
so the Dashboard is rich — for TA and for every role". One call,
`GET /api/dashboard/desk`, returns the tabs THIS login has, each a list of
things to do today. Nothing here computes a rule of its own — every tab is an
existing answer, reshaped into one item shape:

    feedback   interviews that are over with no verdict  (`interview_followups`)
    hr_*       HR Discussion · HR interviews · Pre-Onboarding · Joining soon ·
               Joined recently · Exits & notice                (`_hr_tabs`)
    sales_*    Sales' stages: submit to customer · customer's response · customer
               decision · submit terms · approve terms / with Sales Head · budget
               flags — each item carries the moves it allows   (`_sales_tabs`)
    schedule_customer · schedule_internal · schedule_hr
               rounds asked for but not booked (TA), one tab each for the customer's
               L1 / L2, the AI / Technical L1 / L2 and the HR round
                                                           (`resumes.manual_round_state`)
    sourcing   candidates at Sourcing waiting on TA        (`candidate_status` stage)
    RMG / GM   results to review · to screen · choose route · to book ·
               submit to Sales · AI L1 not cleared · positions to approve ·
               JD missing · headcount · template requests — the Screening
               Desk's task board (`services/rmg_tasks.screener_tasks`)
    upcoming   the next 7 days                             (`dashboard_desk.upcoming`)
    queues     approvals and hand-offs by count            (`dashboards.my_work`)

Item shape: {key, title, subtitle, chip, tone, when, path, action, profile_id}.
`path` is a CRM path (`requirements/12?tab=resumes&q=…`); `profile_id` lets the
UI open the interviews pop-up in place. TA's lists are the candidates the TA
works (`ta_works_clause`: applied OR sent for screening); Admin / CEO see everyone's. Read-only, batched (no query per row).

Every tab also carries `link` — where its Dashboard tile opens: the Screening
Desk on that task for RMG / GM categories, else `my-tasks?tab=<key>` (the My
Tasks page renders the same tabs with their items and actions). The Dashboard
itself shows the tiles only.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Candidate, CandidateProfile, Opportunity, PipelineStatus, Requirement

PS = PipelineStatus
#: Items per tab — a to-do list, not a report; the count still says the total.
MAX_ITEMS = 50
#: The My Tasks page asks for the whole list (`?full=true`) so its filters see
#: every item, not the Dashboard's first 50.
FULL_MAX_ITEMS = 500

TABS = {
    "feedback": ("Feedback due", "Interviews that are over with no verdict — record it so the candidate moves on."),
    "schedule_customer": ("Customer interviews",
                          "Customer L1 / L2 to book — Sales passed the customer's slots; agree one with the candidate."),
    "schedule_internal": ("L1 / L2 interviews",
                          "AI L1 and Technical L1 / L2 rounds RMG / GM asked for — agree a time and book them."),
    "schedule_hr": ("HR interviews", "HR rounds HR asked for — agree a time with the candidate and book them."),
    "sourcing": ("Awaiting your call", "Your candidates at Sourcing: send them for Technical Screening, hold or reject."),
    "ta_pending": ("Pending activities",
                   "Everything waiting on you — interviews to schedule and candidates awaiting your call — "
                   "filter by activity, round, customer or position."),
    "screening": ("To screen", "Applicants waiting for your Shortlist / Reject on the Screening Desk."),
    "route": ("Choose route", "Shortlisted candidates waiting for you to pick the AI or manual L1."),
    # Finance (29 Sep 2026): the billing chain in reading order — what is coming
    # (the GM still has to raise the Proforma), what is Finance's move, what is done.
    # Sales (29 Sep 2026): the customer half of the flow, in order — each tab
    # is one stage Sales owns, and the move is made from the item itself.
    "sales_submit": ("Submit to customer", "RMG cleared these candidates — submit them to the customer or reject."),
    "sales_response": ("Customer's response",
                       "Profiles with the customer — record whether they shortlist the candidate for an interview."),
    "sales_decide": ("Customer decision",
                     "The customer's interview verdict is in — move to the next round, shortlist or reject."),
    "sales_terms": ("Submit to Sales Head",
                    "Everything Sales sends Sales Head — terms to submit, terms sent back, and terms waiting "
                    "for approval, in sections."),
    "sales_approval": ("Approve terms", "Terms Sales submitted — approve, send back or reject."),
    # Admin / CEO (30 Sep 2026): the desk is ONLY what the CEO decides.
    "opp_approvals": ("Approve opportunities",
                      "Deals Sales raised that wait for the Sales Head's approval — yours to approve or reject."),
    "sales_waiting": ("With Sales Head", "Terms you submitted, waiting for Sales Head's approval."),
    "sales_timesheets": ("Timesheets pending",
                         "Sheets to fill and submit, sent back for correction, or waiting for approval — by customer."),
    "sales_invoices_pending": ("Invoices pending",
                               "Approved timesheets with no Proforma yet, or whose Proforma Finance returned — "
                               "by customer or employee."),
    "sales_proformas": ("Proforma invoices",
                        "Proformas the GM raised, with Finance for the original invoice — by customer or employee."),
    "sales_invoices": ("Invoices generated",
                       "Tax invoices issued in the last 90 days with their payment status — by customer or employee."),
    "sales_collections": ("Payments to chase", "Tax invoices past their due date and not fully paid — by customer."),
    "sales_budget": ("Budget flags", "HR flagged these out of budget — talk to the customer and reply to HR."),
    # Sales ladder extras (29 Sep 2026): Sales, Sales Manager (team) and Sales Head.
    "renewals": ("Renewals", "POs ending in 45 days and people whose cover ends in 90 — ask for the next PO or "
                             "extend."),
    "joining_soon": ("Joining soon", "Your candidates joining the customer in the next 30 days — tell the "
                                     "customer, line up access."),
    "team_stuck": ("Team stuck points", "Candidates with Sales more than 2 days or with the customer more than "
                                        "3 — by salesperson."),
    "team_timesheets": ("Team timesheets not submitted",
                        "Sheets for months that have ended, still to submit or sent back — by salesperson."),
    "team_collections": ("Team overdue collections", "Tax invoices past due and not fully paid — by salesperson."),
    "head_pace": ("Pace vs target", "Positions brought in and onboardings this quarter against the targets."),
    "head_stuck": ("Stuck across the team", "Where the company is stuck, and each salesperson's stalled deals."),
    "head_lost": ("Lost this month", "Deals closed lost or rejected, and candidates the customer rejected, "
                                     "this month."),
    "head_collections": ("Top overdue collections", "The biggest overdue balances — who to chase first."),
    "head_po_renewals": ("PO renewals", "Active POs ending in the next 45 days, or already past their end date."),
    # HR (29 Sep 2026): the candidate's tail and the people HR looks after.
    "hr_discussion": ("HR Discussion", "Approved by Sales Head — review the details and request the HR round."),
    "hr_interviews": ("HR interviews", "HR rounds booked from today — the candidate, the time and the link."),
    "hr_onboarding": ("Pre-Onboarding",
                      "HR round done — check CTC and joining date, then complete onboarding and mark Joined."),
    "hr_joining": ("Joining soon", "Candidates and employees starting in the next 30 days."),
    "hr_joined": ("Joined recently", "Employees who joined in the last 30 days — welcome, documents, induction."),
    "hr_exits": ("Exits & notice", "Employees serving notice — last working days in the next 60 days."),
    "hr_leave": ("Leave to approve", "Leave applications waiting for HR — approve or reject before the day comes."),
    "hr_records": ("Records to complete",
                   "Active employees missing what payroll and reports need — CTC, Emp ID, joining date, manager…"),
    "hr_bench": ("On the bench", "Active employees with no live project today — the people to place first."),
    "hr_celebrations": ("Birthdays & anniversaries", "The next 14 days — a message from HR goes a long way."),
    "fin_timesheets": ("Approved timesheets",
                       "Approved sheets on their way to you — the GM raises the Proforma, or has it back after a return."),
    "fin_proformas": ("Proformas to convert",
                      "Proformas the GM raised — review, then generate the original tax invoice or return it."),
    "fin_invoices": ("Tax invoices issued",
                     "Original invoices generated in the last 30 days, with how much has been received."),
    "fin_customer_approved": ("Customer approved invoices",
                              "Invoices the customer accepted (confirmed by the Sales Manager / Sales Head) — "
                              "add the e-invoice IRN and Acknowledgement No."),
    "inv_confirm": ("Confirm with customer",
                    "Original invoices Finance generated — send them to the customer and confirm their "
                    "approval so Finance can add the IRN."),
    "upcoming": ("Upcoming", "Interviews, joinings, roll-offs and due dates in the next 7 days."),
    "queues": ("My queues", "Approvals and hand-offs waiting on you, by count."),
}
_TONE = {"danger": "bad", "warning": "warn", "normal": "info"}


def _item(key, title, subtitle="", *, chip=None, tone="info", when=None, path="", action=None,
          profile_id=None) -> dict:
    return {"key": key, "title": title, "subtitle": subtitle, "chip": chip, "tone": tone,
            "when": when, "path": path, "action": action, "profile_id": profile_id}


#: Where a Dashboard tile opens (28 Sep 2026, user ask: "the Dashboard just
#: shows the buttons; clicking one opens the page where the work is done").
#: RMG / GM categories open the Screening Desk on that task; every other tab
#: opens the My Tasks page on that tab.
TASKS_PAGE = "my-tasks"


def tab_link(key: str, *, screener: bool = False) -> str:
    return f"screening-desk?task={key}" if screener else f"{TASKS_PAGE}?tab={key}"


#: Where a tab sits in its flow — printed as a small caption on the tile.
#: `info` tabs are information (what is coming / what is done), never counted
#: as "waiting on you".
TAB_STAGE = {"fin_timesheets": ("Coming up", True), "fin_proformas": ("Your move", False),
             "fin_invoices": ("Done", True), "fin_customer_approved": ("Your move", False),
             "inv_confirm": ("Your move", False),
             "sales_submit": ("Your move", False), "sales_response": ("With the customer", False),
             "sales_decide": ("Your move", False), "sales_terms": ("Your move", False),
             "sales_approval": ("Your approval", False), "sales_waiting": ("Waiting", True),
             "opp_approvals": ("Your approval", False),
             "sales_timesheets": ("Your move", False), "sales_invoices_pending": ("With GM", True),
             "sales_proformas": ("With Finance", True),
             "sales_invoices": ("Done", True), "sales_collections": ("Follow up", False),
             "sales_budget": ("Your move", False),
             "renewals": ("Coming up", False), "joining_soon": ("Coming up", True),
             "team_stuck": ("Team", False), "team_timesheets": ("Team", False),
             "team_collections": ("Team", False), "head_pace": ("This quarter", True),
             "head_stuck": ("Team", False), "head_lost": ("This month", True),
             "head_collections": ("Follow up", False), "head_po_renewals": ("Coming up", False),
             "hr_discussion": ("Your move", False), "hr_interviews": ("Booked", True),
             "hr_leave": ("Your move", False), "hr_records": ("Your move", False),
             "hr_bench": ("To place", True), "hr_celebrations": ("Coming up", True),
             "hr_onboarding": ("Your move", False), "hr_joining": ("Coming up", True),
             "hr_joined": ("Done", True), "hr_exits": ("Coming up", True)}


def _tab(key: str, items: list[dict], count: int | None = None) -> dict:
    label, hint = TABS[key]
    n = len(items) if count is None else count
    out = {"key": key, "label": label, "hint": hint, "count": n, "items": items,
           "link": tab_link(key)}
    if key in TAB_STAGE:
        out["stage"], out["info"] = TAB_STAGE[key]
    return out


def _profiles(db: Session, stages, *, owner_id: int | None, where=()) -> list[tuple]:
    """(profile, candidate, opportunity, latest requirement id) for live profiles."""
    latest = (select(Requirement.opportunity_id, func.max(Requirement.id).label("rid"))
              .group_by(Requirement.opportunity_id).subquery())
    stmt = (select(CandidateProfile, Candidate, Opportunity, latest.c.rid)
            .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
            .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            .outerjoin(latest, latest.c.opportunity_id == CandidateProfile.opportunity_id)
            .where(CandidateProfile.pipeline_status.in_([PS(s) for s in stages]),
                   func.coalesce(CandidateProfile.is_hidden, False).is_(False), *where)
            .order_by(CandidateProfile.id.desc()).limit(500))
    if owner_id is not None:
        stmt = stmt.where(ta_works_clause(owner_id))
    return db.execute(stmt).all()


def ta_works_clause(user_id: int):
    """The candidacies a TA works (29 Sep 2026, SQL twin of
    `candidate_profiles.ta_user_ids`): the ones they applied (owner) AND the
    ones they sent for Technical Screening — often a colleague's candidate,
    whose customer round then waited on a list its TA never saw."""
    from sqlalchemy import or_

    from models import CandidateProfileActivityLog as Log
    from services.candidate_profiles import SENT_FOR_SCREENING
    sent = (select(Log.id).where(Log.profile_id == CandidateProfile.id,
                                 Log.action_type == SENT_FOR_SCREENING, Log.user_id == user_id)
            .correlate(CandidateProfile).exists())
    return or_(CandidateProfile.ta_owner_id == user_id, sent)


def _name(c) -> str:
    return " ".join(x for x in (c.first_name, c.last_name) if x) or f"Candidate #{c.id}"


def _opp(o) -> str:
    return " — ".join(x for x in (o.opp_id, o.title) if x)


def _applied_path(rid, cand) -> str:
    """The requirement's Applied Candidates row, search prefilled."""
    if not rid:
        return ""
    from urllib.parse import quote
    q = (cand.email or "").strip()
    if not q or q.lower().endswith("@import.karnex.in"):
        q = _name(cand)
    return f"requirements/{rid}?tab=resumes&q={quote(q)}"


def _when(dt) -> str | None:
    return dt.isoformat() if dt else None


# ------------------------------------------------------------------ tabs


def _feedback_tab(db: Session, user) -> dict | None:
    from services.interview_followups import areas_for, feedback_due_for
    roles = set(user.roles or ())
    if areas_for(db, user) == set() and "TA" not in roles:
        return None
    data = feedback_due_for(db, user)
    items = [_item(
        f"fb:{it['event_id']}", it["candidate_name"],
        " · ".join(x for x in (it["opportunity_ref"], it["opportunity_title"], it["area_label"]) if x),
        chip=it["round_label"], tone="bad" if it["overdue_hours"] >= 48 else "warn",
        when=it["scheduled_at"], path=f"profiles/{it['profile_id']}?tab=interviews",
        action="Record feedback", profile_id=it["profile_id"],
    ) for it in data["items"]]
    return _tab("feedback", items)


#: The rounds TA books, grouped into the three "to schedule" tabs (29 Sep 2026,
#: user ask: "one tab for customer interviews, one for L1 & L2 — manual or AI —
#: and one for the HR interview"). label → (tab, round kind the UI books).
#: The AI L1 has no interview-round kind — it is an AI link.
SCHEDULE_ROUNDS = {
    "Customer L1": ("schedule_customer", "Customer_Interview"),
    "Customer L2": ("schedule_customer", "Customer_L2"),
    "AI L1": ("schedule_internal", "AI_L1"),
    "Technical L1": ("schedule_internal", "L1_Interview"),
    "Technical L2": ("schedule_internal", "L2_F2F"),
    "HR round": ("schedule_hr", "HR_Interview"),
}
SCHEDULE_TABS = ("schedule_customer", "schedule_internal", "schedule_hr")


#: Stages at which TA books the customer's L2 (mirror: F-V2 Applied Candidates).
CUSTOMER_L2_STAGES = (PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value)


def rounds_to_schedule(r: dict, stage: str, has_ai_link: bool) -> list[str]:
    """The rounds waiting for TA to book on ONE candidacy. PURE — the same
    rules as TA's buttons on Applied Candidates."""
    wanted = []
    if r.get("ai_l1_requested") and not has_ai_link:
        wanted.append("AI L1")
    if r.get("l1_manual_requested") and not r.get("l1_manual_scheduled"):
        wanted.append("Technical L1")
    if r.get("l2_requested") and not r.get("l2_scheduled"):
        wanted.append("Technical L2")
    if stage == PS.CUSTOMER_INTERVIEW.value and not r.get("cust_l1_scheduled"):
        wanted.append("Customer L1")
    # Sales lines the L2 up at "Customer L1 Interview" OR "Customer L2
    # Interview" (29 Sep 2026: the move to L2 is how Sales asks for it).
    if stage in CUSTOMER_L2_STAGES and not r.get("cust_l2_scheduled"):
        wanted.append("Customer L2")
    if r.get("hr_requested") and not r.get("hr_scheduled"):
        wanted.append("HR round")
    return wanted


def _schedule_tabs(db: Session, user, owner_id: int | None) -> list[dict]:
    """Rounds asked for (by RMG / GM / HR / the stage) but not booked, as the
    three tabs of `SCHEDULE_TABS`. Every item carries `round_kind` (what the
    UI books in place) and, for a customer round, the customer's slots Sales
    passed on (`customer_slots`, only when the offer is for THAT round)."""
    from services.candidate_profiles import latest_ai_interviews
    from services.resumes import manual_round_state

    rows = _profiles(db, (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value, PS.RMG_REVIEW.value,
                          PS.CUSTOMER_INTERVIEW.value, *CUSTOMER_L2_STAGES, PS.HR_SCREENING.value,
                          PS.HR_INTERVIEWING.value), owner_id=owner_id)
    ids = [p.id for p, *_ in rows]
    rounds = manual_round_state(db, ids)
    ai = latest_ai_interviews(db, [p for p, *_ in rows])
    items: dict[str, list[dict]] = {key: [] for key in SCHEDULE_TABS}
    for p, cand, opp, rid in rows:
        r = rounds.get(p.id, {})
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        for label in rounds_to_schedule(r, stage, p.id in ai):
            tab, kind = SCHEDULE_ROUNDS[label]
            offer = r.get("customer_slots")
            offer = offer if offer and offer.get("kind") == kind else None
            item = _item(f"sch:{p.id}:{label}", _name(cand), _opp(opp), chip=label, tone="warn",
                         path=_applied_path(rid, cand), action=f"Schedule {label}", profile_id=p.id)
            item["round_kind"] = kind
            item["customer_slots"] = offer
            # The AI L1 invite needs the candidate's address (placeholders never).
            email = (cand.email or "").strip()
            item["candidate_email"] = None if email.lower().endswith("@import.karnex.in") else email or None
            if offer:
                slots = offer["slots"]
                item["subtitle"] += (f" · customer offered {slots[0]['label']}"
                                     + (f" (+{len(slots) - 1} more)" if len(slots) > 1 else ""))
            items[tab].append(item)
    return [_tab(key, items[key]) for key in SCHEDULE_TABS]


def _ta_tabs(db: Session, user, owner_id: int | None) -> list[dict]:
    """TA's tabs: ONE "Pending activities" tab first (30 Sep 2026, user ask:
    "after Feedback due, his pending activities like schedule interviews, with
    filters") — every schedule item and every awaiting-your-call item together,
    each carrying `activity` (which list it came from), `section` (the position,
    so My Tasks groups by it) and `round_kind` — followed by the three schedule
    tabs and the sourcing tab, built ONCE (no second query set)."""
    import logging

    # Each half in its own savepoint: one failing must not take the other down
    # (the desk rule — a broken tab is logged and left out).
    schedule: list[dict] = []
    sourcing: dict | None = None
    try:
        with db.begin_nested():
            schedule = _schedule_tabs(db, user, owner_id)
    except Exception:
        logging.getLogger("karnex.crm.work_desk").warning("desk tab schedule failed", exc_info=True)
    try:
        with db.begin_nested():
            sourcing = _sourcing_tab(db, user, owner_id)
    except Exception:
        logging.getLogger("karnex.crm.work_desk").warning("desk tab sourcing failed", exc_info=True)
    pending: list[dict] = []
    for tab in (*schedule, *([sourcing] if sourcing else [])):
        for it in tab["items"]:
            row = dict(it)
            row["key"] = f"pend:{it['key']}"
            row["activity"] = tab["label"]
            # The subtitle is the position (+ "· customer offered …" on a customer round).
            row["section"] = (it.get("subtitle") or "").split(" · customer offered")[0] or "No position"
            pending.append(row)
    pending.sort(key=lambda r: (r.get("when") or "9999", r["title"]))
    return [_tab("ta_pending", pending), *schedule, *([sourcing] if sourcing else [])]


def _sourcing_tab(db: Session, user, owner_id: int | None) -> dict:
    """TA's own candidates still at the derived Sourcing phase."""
    from services.candidate_status import SOURCING_STAGE, TA_HOLD, statuses_for

    rows = _profiles(db, (PS.SOURCING.value, PS.TECHNICAL_SCREENING.value), owner_id=owner_id)
    stages = statuses_for(db, [p for p, *_ in rows])
    items = []
    for p, cand, opp, rid in rows:
        st = stages.get(p.id) or {}
        if (st.get("stage") or {}).get("key") != SOURCING_STAGE:
            continue
        held = p.budget_status == TA_HOLD
        items.append(_item(f"src:{p.id}", _name(cand), _opp(opp),
                           chip="On hold" if held else "Not sent for screening",
                           tone="warn" if held else "info",
                           when=_when(p.applied_on or p.created_at), path=_applied_path(rid, cand),
                           action="Release or decide" if held else "Technical Screening",
                           profile_id=p.id))
    return _tab("sourcing", items)


def _upcoming_tab(db: Session, user) -> dict:
    from services.dashboard_desk import upcoming

    tone = {"info": "info", "ok": "ok", "warn": "warn", "bad": "bad"}
    items = [_item(f"up:{i}", it["title"], it["subtitle"], chip=it["kind"].replace("_", " ").title(),
                   tone=tone.get(it["state"], "info"), when=it["when"], path=it["path"])
             for i, it in enumerate(upcoming(db, user, 7)["items"])]
    return _tab("upcoming", items)


def _queues_tab(db: Session, user) -> dict:
    from services.dashboards import my_work

    work = my_work(db, user)["items"]
    items = [_item(f"q:{it['key']}", f"{it['count']} {it['label']}", "", chip=str(it["count"]),
                   tone=_TONE.get(it["urgency"], "info"), path=it["path"], action="Open")
             for it in work]
    return _tab("queues", items, count=sum(it["count"] for it in work))


# ------------------------------------------------------------------ Sales

#: A profile with the customer longer than this is flagged amber, twice as long red.
CUSTOMER_WAIT_DAYS = 3
#: Customer-stage statuses that are a VERDICT waiting for Sales' next move
#: (a No Hire closes the profile by itself; "Leaning No" does not).
_SALES_DECIDE = {"customer_l1_passed", "customer_l1_failed", "customer_l1_review",
                 "customer_l2_passed", "customer_l2_failed", "customer_l2_review"}
#: Moves the desk never offers: the Customer Approval step is the terms dialog,
#: and a withdrawal is recorded on the profile.
_DESK_EXCLUDED_MOVES = {PS.CUSTOMER_APPROVAL.value, PS.SELF_WITHDRAWN.value}


def sales_scope(user_id: int):
    """What a plain Sales user works (29 Sep 2026): the deals they raised, and
    any candidacy they have acted on (a colleague's deal they carried). Sales
    Head / Sales Manager / Admin see every deal."""
    from sqlalchemy import or_

    from models import CandidateProfileActivityLog as Log
    acted = (select(Log.id).where(Log.profile_id == CandidateProfile.id, Log.user_id == user_id)
             .correlate(CandidateProfile).exists())
    return or_(Opportunity.created_by == user_id, acted)


def _since_status_change(db: Session, ids: list[int]) -> dict[int, datetime]:
    """profile id → when it last changed stage (ONE grouped query)."""
    from models import CandidateProfileActivityLog as Log
    if not ids:
        return {}
    return {pid: ts for pid, ts in db.execute(
        select(Log.profile_id, func.max(Log.timestamp))
        .where(Log.profile_id.in_(ids), Log.action_type == "STATUS_CHANGE")
        .group_by(Log.profile_id)).all()}


def _pending_offers(db: Session, ids: list[int]) -> dict[int, dict]:
    """profile id → the newest PENDING offer's terms (what Sales submitted)."""
    from models import OfferHistory, OfferStatus
    out: dict[int, dict] = {}
    if not ids:
        return out
    for o in db.execute(select(OfferHistory).where(OfferHistory.profile_id.in_(ids),
                                                   OfferHistory.status == OfferStatus.PENDING)
                        .order_by(OfferHistory.id)).scalars():
        out[o.profile_id] = {
            "ctc": float(o.ctc) if o.ctc is not None else None,
            "rate_unit": o.rate_unit,
            "rate_value": float(o.rate_value) if o.rate_value is not None else None,
            "joining_date": o.joining_date.isoformat() if o.joining_date else None,
            "offer_date": o.offer_date.isoformat() if o.offer_date else None,
            "status": getattr(o.status, "value", o.status),
        }
    return out


#: Sections of "Submit to Sales Head", in reading order.
TERMS_SENT_BACK = "Sent back — revise and resubmit"
TERMS_TO_SUBMIT = "Ready to submit"
TERMS_WAITING = "With Sales Head — waiting for approval"
TERMS_SECTIONS = (TERMS_SENT_BACK, TERMS_TO_SUBMIT, TERMS_WAITING)


def _sales_tabs(db: Session, user, *, is_admin: bool) -> list[dict]:
    """Sales' desk (29 Sep 2026, user ask: "after Feedback due, tabs for all the
    work Sales has to do — without opening the candidate profile"). One tab per
    stage Sales owns; every item carries what the UI needs to make the move in
    place: `current_status` + `allowed` (the SAME `allowed_next_statuses_for_user`
    the profile's Next-step bar uses), the pending `offer`, the CTC figures and
    the approved budget. Five batched queries for the whole desk."""
    from services.action_permissions import user_may
    from services.candidate_profiles import (
        BUDGET_OUT, allowed_next_statuses_for_user, approved_ctc_budgets)
    from services.candidate_status import statuses_for

    roles = set(user.roles or ())
    from services.role_implications import sees_team
    everyone = is_admin or "Sales_Head" in roles or sees_team(roles)
    where = () if everyone else (sales_scope(user.id),)
    stages = (PS.SALES_SCREENING.value, PS.CUSTOMER_SCREENING.value, PS.CUSTOMER_INTERVIEW.value,
              PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value, PS.SHORTLISTED.value,
              PS.CUSTOMER_APPROVAL.value, PS.PREBOARDING.value, PS.HR_INTERVIEWING.value)
    rows = _profiles(db, stages, owner_id=None, where=where)
    profiles = [p for p, *_ in rows]
    ids = [p.id for p in profiles]
    status = statuses_for(db, profiles)
    moved = _since_status_change(db, ids)
    offers = _pending_offers(db, ids)
    budgets = approved_ctc_budgets(db, profiles, {c.id: c for _, c, *_ in rows})
    approver = user_may(db, user, "profile.sales_head_decision")
    may_reply = user_may(db, user, "profile.budget_resolve")
    now = datetime.now(timezone.utc)

    tabs: dict[str, list[dict]] = {k: [] for k in (
        "sales_submit", "sales_response", "sales_decide", "sales_terms",
        "sales_approval", "sales_budget")}
    for p, cand, opp, rid in rows:
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        st = status.get(p.id) or {}
        since = moved.get(p.id) or getattr(p, "updated_at", None)
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        days = (now - since).days if since else 0

        def item(tab, chip, tone, action, *, moves=True):
            it = _item(f"{tab}:{p.id}", _name(cand), _opp(opp), chip=chip, tone=tone,
                       when=_when(since), path=f"profiles/{p.id}", action=action, profile_id=p.id)
            it.update({
                "current_status": stage,
                "status_label": st.get("label"),
                "allowed": ([s for s in allowed_next_statuses_for_user(stage, user, p, db)
                             if s not in _DESK_EXCLUDED_MOVES] if moves else []),
                "offer": offers.get(p.id),
                "expected_ctc": float(p.expected_ctc) if p.expected_ctc is not None else None,
                "current_ctc": float(p.current_ctc) if p.current_ctc is not None else None,
                "opportunity_label": _opp(opp),
                "days_waiting": days,
                **budgets.get(p.id, {"approved_ctc_budget": None, "ctc_slab_band": None}),
            })
            tabs[tab].append(it)

        age = f"{days} day{'s' if days != 1 else ''}"
        waiting_tone = "bad" if days > 2 * CUSTOMER_WAIT_DAYS else "warn" if days > CUSTOMER_WAIT_DAYS else "info"
        if stage == PS.SALES_SCREENING.value:
            item("sales_submit", f"Cleared by RMG · {age}", waiting_tone, "Submit to customer")
        elif stage == PS.CUSTOMER_SCREENING.value:
            item("sales_response", f"With the customer · {age}", waiting_tone, "Record the response")
        elif st.get("key") in _SALES_DECIDE:
            failed = st["key"].endswith("_failed")
            item("sales_decide", st.get("label"), "bad" if failed else "ok", "Decide the next step")
        elif stage == PS.SHORTLISTED.value:
            back = st.get("key") == "terms_sent_back"
            item("sales_terms", "Sent back by Sales Head" if back else f"Customer shortlisted · {age}",
                 "warn" if back else "ok", "Resubmit terms" if back else "Submit terms", moves=False)
            tabs["sales_terms"][-1]["section"] = TERMS_SENT_BACK if back else TERMS_TO_SUBMIT
        elif stage == PS.CUSTOMER_APPROVAL.value:
            item("sales_approval", f"Waiting {age}",
                 "bad" if days > 2 * CUSTOMER_WAIT_DAYS else "warn" if days > 1 else "info",
                 "Review terms" if approver else "Open", moves=False)
            # …and the same candidacy on the submitter's side, as "sent, waiting"
            # (29 Sep 2026: everything Sales sends Sales Head lives in ONE tab).
            tabs["sales_terms"].append(dict(tabs["sales_approval"][-1], key=f"sales_terms_wait:{p.id}",
                                            chip=f"With Sales Head · {age}", tone="info", action="Open",
                                            section=TERMS_WAITING))
        elif p.budget_status == BUDGET_OUT and may_reply:
            item("sales_budget", "Out of budget", "bad", "Reply to HR", moves=False)
            tabs["sales_budget"][-1]["hr_note"] = p.budget_note
    terms = sorted(tabs["sales_terms"], key=lambda it: TERMS_SECTIONS.index(it.get("section", TERMS_TO_SUBMIT)))
    out = [_tab(k, tabs[k]) for k in ("sales_submit", "sales_response", "sales_decide")]
    # The count is what Sales still has to SEND — waiting items are information.
    # A real Sales Head APPROVES terms (their own tab below); "Submit to Sales
    # Head" is for Sales / Sales Manager only (29 Sep 2026, user ask).
    head = "Sales_Head" in set(getattr(user, "held_roles", None) or roles) and not is_admin
    if not head:
        out.append(_tab("sales_terms", terms,
                        count=sum(1 for it in terms if it.get("section") != TERMS_WAITING)))
    # Customer Approval: the approver's own tab (the submitter sees it inside Submit to Sales Head).
    if approver:
        out.append(_tab("sales_approval", tabs["sales_approval"]))
    if may_reply:
        out.append(_tab("sales_budget", tabs["sales_budget"]))
    return out


# ------------------------------------------------------------------ Sales billing

#: "Invoices generated" looks back this far.
SALES_INVOICES_DAYS = 90
#: A Draft sheet for a month that ended more than this many days ago is red.
SHEET_LATE_DAYS = 5


def _sales_billing_tabs(db: Session, user, *, everyone: bool, invoices_everyone: bool | None = None) -> list[dict]:
    """Sales' billing chain (29 Sep 2026, user ask: "Timesheet pending, Invoices
    pending, Generated invoices — customer-wise sections; think as Sales").
    Every item carries `section` = the customer AND the `employee` facet
    (30 Sep 2026, user ask: "customer wise & employee wise"), so the page groups
    by either and filters by both.

    * Timesheets pending — Draft (fill & submit) / Rejected (fix & resubmit) /
      Submitted (with the GM) for months up to the current one.
    * Invoices pending — Approved sheets with no issued invoice: waiting for the
      GM's Proforma, or the Proforma was returned by Finance.
    * Proforma invoices — Proformas the GM raised, with Finance.
    * Invoices generated — Tax invoices of the last `SALES_INVOICES_DAYS` days.
    * Payments to chase — Tax invoices past due and not fully paid (Sales owns
      the customer relationship, so the reminder is theirs).

    Scope: `everyone` covers the timesheet / collections tabs — a plain Sales
    user sees the projects of the deals they raised plus any sheet they acted
    on; `invoices_everyone` (defaults to `everyone`) covers the three invoice
    tabs — the whole of Sales (Sales Manager, Sales Head) sees every customer's
    invoices (30 Sep 2026, user ask). Six batched queries."""
    from datetime import date, timedelta

    from sqlalchemy import or_

    from models import (
        Customer, Employee, Invoice, Project, Timesheet, TimesheetActivityLog, TimesheetStatus)
    from models.finance import InvoiceKind, PaymentStatus

    today = date.today()
    tax, proforma = InvoiceKind.TAX.value, InvoiceKind.PROFORMA.value
    if invoices_everyone is None:
        invoices_everyone = everyone

    def project_map(all_of_them: bool) -> dict:
        q = (select(Project.id, Project.name, Customer.id, func.coalesce(Customer.name, "No customer"))
             .outerjoin(Customer, Customer.id == Project.customer_id))
        if not all_of_them:
            acted = (select(TimesheetActivityLog.id)
                     .join(Timesheet, Timesheet.id == TimesheetActivityLog.timesheet_id)
                     .where(Timesheet.project_id == Project.id, TimesheetActivityLog.user_id == user.id)
                     .correlate(Project).exists())
            owned = (select(Opportunity.id).where(Opportunity.id == Project.opportunity_id,
                                                  Opportunity.created_by == user.id)
                     .correlate(Project).exists())
            q = q.where(or_(owned, acted))
        return {pid: (pname, cid, cname) for pid, pname, cid, cname in db.execute(q).all()}

    projects = project_map(everyone)
    inv_projects = projects if invoices_everyone == everyone else project_map(invoices_everyone)
    if not projects and not inv_projects:
        return [_tab(k, []) for k in ("sales_timesheets", "sales_invoices_pending", "sales_proformas",
                                      "sales_invoices", "sales_collections")]
    pids = list(projects)
    inv_pids = list(inv_projects)

    def where_of(pid, scope=None):
        pname, _cid, cname = (scope or projects).get(pid, ("Project", None, "No customer"))
        return pname, cname

    def by_section(items):
        return sorted(items, key=lambda it: (it["section"].lower(), it.get("_order", 0)))

    def clean(items):
        for it in items:
            it.pop("_order", None)
        return items

    # 1 — timesheets not yet approved.
    this_month = today.year * 12 + today.month
    ts_rows = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.project_id.in_(pids or [-1]),
               Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.SUBMITTED, TimesheetStatus.REJECTED]),
               Timesheet.year * 12 + Timesheet.month <= this_month)
        .order_by(Timesheet.year, Timesheet.month)).all()
    ts_items, ts_move = [], 0
    for ts, emp in ts_rows:
        st = getattr(ts.status, "value", ts.status)
        pname, cname = where_of(ts.project_id)
        month_end = date(ts.year + (ts.month // 12), ts.month % 12 + 1, 1) - timedelta(days=1)
        late = (today - month_end).days
        if st == TimesheetStatus.REJECTED.value:
            chip, tone, action, order = "Sent back — fix & resubmit", "bad", "Fix timesheet", 0
        elif st == TimesheetStatus.DRAFT.value:
            chip = "To submit" if late <= 0 else f"To submit · month ended {late} d ago"
            tone = "bad" if late > SHEET_LATE_DAYS else "warn" if late > 0 else "info"
            action, order = "Fill & submit", 1
        else:
            chip, tone, action, order = "With GM for approval", "info", "Open", 2
        if order < 2:
            ts_move += 1
        it = _item(f"sts:{ts.id}", f"{_employee_name(emp)} · {_month_label(ts.year, ts.month)}", pname,
                   chip=chip, tone=tone, when=_when(ts.submitted_at or ts.updated_at),
                   path=f"timesheets/{ts.id}", action=action)
        it.update(section=cname, _order=order, employee=_employee_name(emp),
                  **_facets(cname, pname, ts.year, ts.month))
        ts_items.append(it)

    # 2 — approved sheets waiting for an issued invoice.
    since = datetime.now(timezone.utc) - timedelta(days=FIN_TIMESHEET_DAYS)
    approved = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.project_id.in_(inv_pids or [-1]), Timesheet.status == TimesheetStatus.APPROVED,
               func.coalesce(Timesheet.approved_at, Timesheet.updated_at) >= since)).all()
    docs: dict[int, Invoice] = {}
    if approved:
        for inv in db.execute(select(Invoice).where(
                Invoice.timesheet_id.in_([ts.id for ts, _ in approved])).order_by(Invoice.id)).scalars():
            docs[inv.timesheet_id] = inv
    pend_items = []
    for ts, emp in approved:
        doc = docs.get(ts.id)
        if doc is not None and not (doc.kind == proforma and doc.returned_at):
            continue
        pname, cname = where_of(ts.project_id, inv_projects)
        returned = doc is not None
        it = _item(f"sip:{ts.id}", f"{_employee_name(emp)} · {_month_label(ts.year, ts.month)}", pname,
                   chip="Proforma returned by Finance" if returned else "Approved — waiting for GM's Proforma",
                   tone="warn" if returned else "info", when=_when(ts.approved_at or ts.updated_at),
                   path=f"timesheets/{ts.id}", action="Open timesheet")
        it.update(section=cname, _order=0, employee=_employee_name(emp),
                  **_facets(cname, pname, ts.year, ts.month))
        pend_items.append(it)

    # 3 + 4 + 5 — Proformas with Finance; tax invoices issued recently; past due
    # and unpaid. The employee comes through the invoice's timesheet — ONE query
    # for every invoice on the page; a manual invoice names nobody.
    cutoff = today - timedelta(days=SALES_INVOICES_DAYS)
    unpaid = (PaymentStatus.UNPAID, PaymentStatus.PARTIALLY_PAID)
    invoices = db.execute(select(Invoice).where(
        Invoice.project_id.in_(inv_pids or [-1]),
        or_((Invoice.kind == proforma) & Invoice.returned_at.is_(None),
            (Invoice.kind == tax) & or_(Invoice.invoice_date >= cutoff,
                                        Invoice.payment_status.in_(unpaid) & (Invoice.due_date < today))))
        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())).scalars().all()
    emp_of_sheet: dict[int, str] = {}
    sheet_ids = [inv.timesheet_id for inv in invoices if inv.timesheet_id]
    if sheet_ids:
        for tsid, emp in db.execute(
                select(Timesheet.id, Employee).join(Employee, Employee.id == Timesheet.employee_id)
                .where(Timesheet.id.in_(sheet_ids))).all():
            emp_of_sheet[tsid] = _employee_name(emp)

    def facets_of(inv, cname, pname):
        return dict(employee=emp_of_sheet.get(inv.timesheet_id or 0),
                    **_facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))

    pi_items, iv_items, due_items = [], [], []
    for inv in invoices:
        pname, cname = where_of(inv.project_id, inv_projects)
        who = emp_of_sheet.get(inv.timesheet_id or 0)
        subtitle = f"{pname}{' · ' + who if who else ''}"
        if inv.kind == proforma:
            age = _age_days(inv.invoice_date, today) or 0
            it = _item(f"spi:{inv.id}", f"{inv.proforma_number or inv.invoice_number} · {rupees(inv.grand_total)}",
                       subtitle, chip=f"With Finance · {age} d" if age else "With Finance",
                       tone="warn" if age > PROFORMA_WAIT_DAYS else "info", when=_when(inv.invoice_date),
                       path=f"invoices/{inv.id}", action="Open Proforma")
            it.update(section=cname, _order=-age, **facets_of(inv, cname, pname))
            pi_items.append(it)
            continue
        status = str(getattr(inv.payment_status, "value", inv.payment_status) or "Unpaid")
        paid = status == PaymentStatus.PAID.value
        overdue = (not paid) and inv.due_date is not None and inv.due_date < today
        if inv.invoice_date >= cutoff:
            it = _item(f"siv:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}", subtitle,
                       chip=("Overdue" if overdue else status.replace("_", " ")),
                       tone="bad" if overdue else "ok" if paid else "info",
                       when=_when(inv.invoice_date), path=f"invoices/{inv.id}", action="Open invoice")
            it.update(section=cname, **facets_of(inv, cname, pname))
            iv_items.append(it)
        if overdue and inv.project_id in projects:
            late = (today - inv.due_date).days
            it = _item(f"sdue:{inv.id}", f"{inv.invoice_number} · {rupees(inv.balance_amount or inv.grand_total)} due",
                       subtitle, chip=f"{late} day{'s' if late != 1 else ''} overdue",
                       tone="bad" if late > 30 else "warn", when=inv.due_date.isoformat(),
                       path=f"invoices/{inv.id}", action="Open invoice")
            it.update(section=cname, _order=-late, **facets_of(inv, cname, pname))
            due_items.append(it)

    return [_tab("sales_timesheets", clean(by_section(ts_items)), count=ts_move),
            _tab("sales_invoices_pending", clean(by_section(pend_items))),
            _tab("sales_proformas", clean(by_section(pi_items))),
            _tab("sales_invoices", clean(sorted(iv_items, key=lambda it: it["section"].lower()))),
            _tab("sales_collections", clean(by_section(due_items)))]


# ------------------------------------------------------------------ Sales ladder extras

#: Renewals: POs ending this soon, and people whose cover ends this soon.
RENEWAL_PO_DAYS = 45
RENEWAL_ROLLOFF_DAYS = 90
#: "Joining soon" on the Sales desk looks this far ahead.
SALES_JOINING_DAYS = 30
#: Team stuck points: with Sales longer than this (days), with the customer longer than CUSTOMER_WAIT_DAYS.
TEAM_SALES_WAIT_DAYS = 2
#: Sales Head's "Top overdue collections" shows this many, biggest balance first.
HEAD_TOP_COLLECTIONS = 10
_CUSTOMER_STAGES = (PS.CUSTOMER_SCREENING.value, PS.CUSTOMER_INTERVIEW.value,
                    PS.L1_FEEDBACK.value, PS.L2_FEEDBACK.value)
#: Sales-family rungs (29 Sep 2026, user ask): the tabs each rung ADDS.
SALES_RUNG_TABS = {
    "sales": ("renewals", "joining_soon"),
    "manager": ("renewals", "joining_soon", "team_stuck", "team_timesheets", "team_collections"),
    "head": ("head_pace", "head_stuck", "head_lost", "head_collections", "head_po_renewals"),
}


def sales_rung(user) -> str | None:
    """"head" | "manager" | "sales" | None for a login's Sales rung, from the roles
    it HOLDS (a Sales Manager also carries the implied "Sales")."""
    from services.role_implications import sees_team

    roles = set(user.roles or ())
    held = set(getattr(user, "held_roles", None) or roles)
    if roles & {"Admin", "CEO"}:
        return None
    if "Sales_Head" in held:
        return "head"
    if sees_team(held | roles):
        return "manager"
    if "Sales" in roles:
        return "sales"
    return None


def _owned_customers(db: Session, user_id: int) -> set[int]:
    return {cid for (cid,) in db.execute(
        select(Opportunity.customer_id).where(Opportunity.created_by == user_id).distinct()).all()}


def _renewal_items(db: Session, *, customers: set[int] | None, po_days=RENEWAL_PO_DAYS,
                   rolloff_days=RENEWAL_ROLLOFF_DAYS, prefix="rn") -> list[dict]:
    """POs ending within `po_days` (ask the customer for the next one) and people
    whose cover ends within `rolloff_days` (extend, or place elsewhere) — one
    section per customer. `customers=None` = every customer."""
    from datetime import date, timedelta

    from models import Customer
    from models.finance import POStatus, PurchaseOrder
    from services.dashboards import bench_rolloffs

    today = date.today()
    items = []
    q = (select(PurchaseOrder, func.coalesce(Customer.name, "No customer"))
         .outerjoin(Customer, Customer.id == PurchaseOrder.customer_id)
         .where(PurchaseOrder.status == POStatus.ACTIVE, PurchaseOrder.end_date.is_not(None),
                PurchaseOrder.end_date <= today + timedelta(days=po_days))
         .order_by(PurchaseOrder.end_date))
    if customers is not None:
        if not customers:
            return []
        q = q.where(PurchaseOrder.customer_id.in_(customers))
    for po, cname in db.execute(q).all():
        days = (po.end_date - today).days
        chip = f"Expired {-days} d ago" if days < 0 else "Ends today" if days == 0 else f"Ends in {days} d"
        it = _item(f"{prefix}po:{po.id}", f"PO {po.po_number} · {rupees(po.balance_value)} left", "Purchase order",
                   chip=chip, tone="bad" if days <= 7 else "warn" if days <= 21 else "info",
                   when=po.end_date.isoformat(), path=f"pos/{po.id}", action="Open PO")
        it.update(section=cname, **_facets(cname, None, day=po.end_date))
        items.append(it)
    if rolloff_days:
        names = None
        if customers is not None:
            names = {n for (n,) in db.execute(select(Customer.name).where(Customer.id.in_(customers))).all()}
        for r in bench_rolloffs(db, rolloff_days):
            if names is not None and r.get("customer_name") not in names:
                continue
            days = r.get("days_left")
            why = "project closes" if r.get("ends_by") == "project_close" else "PO cover ends"
            chip = (f"{why} · {days} d" if days is not None and days >= 0 else f"{why} — ended")
            cname = r.get("customer_name") or "No customer"
            it = _item(f"{prefix}ro:{r['project_employee_id']}", r["employee_name"],
                       f"{r.get('project_name') or ''}{' · ' + r['role_title'] if r.get('role_title') else ''}",
                       chip=chip, tone="bad" if (days or 0) <= 15 else "warn" if (days or 0) <= 45 else "info",
                       when=r.get("po_end_date"), path=f"project-employees/{r['project_employee_id']}",
                       action="Open")
            it.update(section=cname, customer=cname, project=r.get("project_name"))
            items.append(it)
    items.sort(key=lambda it: (it["section"].lower(), it.get("when") or ""))
    return items


def _joining_items(db: Session, user, *, everyone: bool) -> list[dict]:
    """Candidates of Sales' deals joining the customer in the next
    `SALES_JOINING_DAYS` days — tell the customer, line up access."""
    from datetime import date, timedelta

    today = date.today()
    stages = (PS.CUSTOMER_APPROVAL.value, PS.HR_SCREENING.value, PS.HR_INTERVIEWING.value,
              PS.PREBOARDING.value, PS.JOINED.value)
    where = [CandidateProfile.customer_onboarding_date >= today,
             CandidateProfile.customer_onboarding_date <= today + timedelta(days=SALES_JOINING_DAYS)]
    if not everyone:
        where.append(sales_scope(user.id))
    items = []
    for p, cand, opp, _rid in _profiles(db, stages, owner_id=None, where=where):
        cod = p.customer_onboarding_date
        days = (cod - today).days
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        it = _item(f"js:{p.id}", _name(cand), _opp(opp),
                   chip=("Today" if days == 0 else f"In {days} day{'s' if days != 1 else ''}")
                   + ("" if stage == PS.JOINED.value else " · not joined yet"),
                   tone="warn" if days <= 3 else "info", when=cod.isoformat(), path=f"profiles/{p.id}",
                   action="Open", profile_id=p.id)
        items.append(it)
    items.sort(key=lambda it: it["when"] or "")
    return items


def _sales_rung_tabs(db: Session, user, rung: str) -> list[dict]:
    """The extra tiles a Sales-family rung gets (29 Sep 2026, user ask):
    Sales — Renewals · Joining soon; Sales Manager — those for the team plus Team
    stuck points · Team timesheets not submitted · Team overdue collections; Sales
    Head — Pace vs target · Stuck deals across the team · Lost this month · Top
    overdue collections · PO renewals."""
    out = []
    if rung in ("sales", "manager"):
        team = rung == "manager"
        customers = None if team else _owned_customers(db, user.id)
        out.append(_tab("renewals", _renewal_items(db, customers=customers)))
        out.append(_tab("joining_soon", _joining_items(db, user, everyone=team)))
    if rung == "manager":
        out.extend(_team_tabs(db))
    if rung == "head":
        out.extend(_head_tabs(db, user))
    return out


def _deal_owner_names(db: Session, owner_ids) -> dict[int, str]:
    from services.revenue_report import _user_names

    return _user_names(db, set(owner_ids))


def _team_tabs(db: Session) -> list[dict]:
    """Sales Manager's team view — every item's `section` is the SALESPERSON who
    owns the deal (`Opportunity.created_by`)."""
    from datetime import date, timedelta

    from models import Customer, Employee, Invoice, Project, Timesheet, TimesheetStatus
    from models.finance import InvoiceKind, PaymentStatus

    today = date.today()
    now = datetime.now(timezone.utc)

    # 1 — stuck: with Sales > 2 days, with the customer > 3 days.
    rows = _profiles(db, (PS.SALES_SCREENING.value,) + _CUSTOMER_STAGES, owner_id=None)
    moved = _since_status_change(db, [p.id for p, *_ in rows])
    stuck_raw = []
    for p, cand, opp, _rid in rows:
        since = moved.get(p.id) or p.updated_at
        if since is None:
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        days = (now - since).days
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        with_sales = stage == PS.SALES_SCREENING.value
        limit = TEAM_SALES_WAIT_DAYS if with_sales else CUSTOMER_WAIT_DAYS
        if days <= limit:
            continue
        where = "With Sales" if with_sales else "With the customer"
        stuck_raw.append((opp.created_by, days, _item(
            f"tst:{p.id}", _name(cand), _opp(opp), chip=f"{where} · {days} d",
            tone="bad" if days > 2 * limit else "warn", when=_when(since), path=f"profiles/{p.id}",
            action="Open", profile_id=p.id)))

    # 2 — timesheets not submitted for months that have ended; 3 — overdue collections.
    this_month = today.year * 12 + today.month
    proj = {pid: (pname, cname, owner) for pid, pname, cname, owner in db.execute(
        select(Project.id, Project.name, func.coalesce(Customer.name, "No customer"),
               Opportunity.created_by)
        .outerjoin(Customer, Customer.id == Project.customer_id)
        .outerjoin(Opportunity, Opportunity.id == Project.opportunity_id)).all()}
    ts_raw = []
    for ts, emp in db.execute(
            select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.REJECTED]),
                   Timesheet.year * 12 + Timesheet.month < this_month)
            .order_by(Timesheet.year, Timesheet.month)).all():
        pname, cname, owner = proj.get(ts.project_id, ("Project", "No customer", None))
        month_end = date(ts.year + (ts.month // 12), ts.month % 12 + 1, 1) - timedelta(days=1)
        late = (today - month_end).days
        rejected = getattr(ts.status, "value", ts.status) == TimesheetStatus.REJECTED.value
        it = _item(f"tts:{ts.id}", f"{_employee_name(emp)} · {_month_label(ts.year, ts.month)}",
                   f"{cname} · {pname}",
                   chip=("Sent back · " if rejected else "Not submitted · ") + f"{late} d late",
                   tone="bad" if late > SHEET_LATE_DAYS else "warn", when=_when(ts.updated_at),
                   path=f"timesheets/{ts.id}", action="Open timesheet")
        it.update(**_facets(cname, pname, ts.year, ts.month))
        ts_raw.append((owner, -late, it))
    due_raw = []
    for inv in db.execute(select(Invoice).where(
            Invoice.kind == InvoiceKind.TAX.value,
            Invoice.payment_status.in_((PaymentStatus.UNPAID, PaymentStatus.PARTIALLY_PAID)),
            Invoice.due_date.is_not(None), Invoice.due_date < today)).scalars():
        pname, cname, owner = proj.get(inv.project_id, ("Project", "No customer", None))
        late = (today - inv.due_date).days
        it = _item(f"tdue:{inv.id}", f"{inv.invoice_number} · {rupees(inv.balance_amount or inv.grand_total)} due",
                   f"{cname} · {pname}", chip=f"{late} day{'s' if late != 1 else ''} overdue",
                   tone="bad" if late > 30 else "warn", when=inv.due_date.isoformat(),
                   path=f"invoices/{inv.id}", action="Open invoice")
        it.update(**_facets(cname, pname, day=inv.invoice_date))
        due_raw.append((owner, -late, it))

    names = _deal_owner_names(db, {o for o, *_ in stuck_raw + ts_raw + due_raw if o})

    def by_owner(raw, desc_key=True):
        out = []
        for owner, key, it in raw:
            it["section"] = names.get(owner, "No owner") if owner else "No owner"
            it["_k"] = key
            out.append(it)
        out.sort(key=lambda it: (it["section"].lower(), -it["_k"] if desc_key else it["_k"]))
        for it in out:
            it.pop("_k", None)
        return out

    return [_tab("team_stuck", by_owner(stuck_raw)),
            _tab("team_timesheets", by_owner(ts_raw, desc_key=False)),
            _tab("team_collections", by_owner(due_raw, desc_key=False))]


def _head_tabs(db: Session, user) -> list[dict]:
    """Sales Head: pace vs target, where the team is stuck, what was lost this
    month, the biggest overdue balances and the POs to renew."""
    from datetime import date

    from models import Customer, Invoice, Project
    from models.finance import InvoiceKind, PaymentStatus
    from models.opportunities import PipelineStage
    from services.dashboard_desk import STALLED_DAYS, team_overview
    from services.hiring_dashboard import hiring_dashboard

    today = date.today()
    tone_of = {"ok": "ok", "warn": "warn", "bad": "bad"}
    out = []

    # 1 — pace vs target (the hiring targets the Sales Head sets; quarter zoom).
    pace_items = []
    try:
        pace = hiring_dashboard(db, None, "quarter", today=today)["pace"]
    except Exception:  # noqa: BLE001 — one tile never blanks the desk
        pace = {}
    for key, label, what in (("sales", "Sales pace", "positions brought in"),
                             ("fulfilment", "Fulfilment pace", "onboardings")):
        p = pace.get(key) or {}
        if not p:
            continue
        target = p.get("target")
        actual = p.get("actual") or 0
        if target is None:
            chip, sub = "No target set", f"{actual:g} {what} this quarter — set a target on the Dashboard"
        else:
            chip = f"{p.get('attainment_pct') or 0:g}% of target"
            need = p.get("required_per_week")
            sub = (f"{actual:g} of {target:g} {what} this quarter · {p.get('current_per_week') or 0:g}/week"
                   + (f", need {need:g}/week" if need is not None else ""))
        pace_items.append(_item(f"hp:{key}", label, sub, chip=chip,
                                tone=tone_of.get(p.get("state"), "info"), path="", action="Open Dashboard"))
    pace_tab = _tab("head_pace", pace_items, count=sum(1 for it in pace_items if it["tone"] in ("warn", "bad")))
    out.append(pace_tab)

    # 2 — stuck across the team (the company stuck points + stalled deals per owner).
    stuck_items = []
    try:
        team = team_overview(db, user)
    except Exception:  # noqa: BLE001
        team = {"stuck": [], "sales": []}
    for i, s in enumerate(team.get("stuck") or []):
        it = _item(f"hs:{i}", s["title"], s.get("detail") or "", chip=f"{s['count']}",
                   tone=tone_of.get(s.get("state"), "info"), path=s.get("path") or "", action="Open")
        it["section"] = s.get("area") or "Company"
        stuck_items.append(it)
    for r in team.get("sales") or []:
        if not r.get("stalled"):
            continue
        it = _item(f"hso:{r['user_id']}", f"{r['name']} — {r['stalled']} deal{'s' if r['stalled'] != 1 else ''} stalled",
                   f"{r['open']} open · {r.get('pending_approval', 0)} waiting for approval",
                   chip=f"No update > {STALLED_DAYS} d", tone="warn", path="opportunities", action="Open")
        it["section"] = "Stalled deals by salesperson"
        stuck_items.append(it)
    out.append(_tab("head_stuck", stuck_items))

    # 3 — lost this month: deals closed lost / rejected, candidates the customer rejected.
    month_start = today.replace(day=1)
    lost = []
    for o, cname in db.execute(
            select(Opportunity, func.coalesce(Customer.name, "No customer"))
            .outerjoin(Customer, Customer.id == Opportunity.customer_id)
            .where(Opportunity.pipeline_stage.in_((PipelineStage.CLOSED_LOST, PipelineStage.REJECTED)),
                   func.date(Opportunity.updated_at) >= month_start)
            .order_by(Opportunity.updated_at.desc())).all():
        stage = getattr(o.pipeline_stage, "value", o.pipeline_stage)
        it = _item(f"hlo:{o.id}", _opp(o), cname, chip=stage.replace("_", " "), tone="bad",
                   when=_when(o.updated_at), path=f"opportunities/{o.id}", action="Open")
        it.update(section="Deals lost", customer=cname)
        lost.append(it)
    rej = _profiles(db, (PS.CUSTOMER_REJECTED.value,), owner_id=None)
    moved = _since_status_change(db, [p.id for p, *_ in rej])
    for p, cand, opp, _rid in rej:
        since = moved.get(p.id) or p.updated_at
        if since is None or since.date() < month_start:
            continue
        it = _item(f"hlc:{p.id}", _name(cand), _opp(opp), chip="Customer rejected", tone="warn",
                   when=_when(since), path=f"profiles/{p.id}", action="Open", profile_id=p.id)
        it["section"] = "Candidates the customer rejected"
        lost.append(it)
    out.append(_tab("head_lost", lost))

    # 4 — top overdue collections, biggest balance first.
    due = []
    for inv, pname, cname in db.execute(
            select(Invoice, Project.name, func.coalesce(Customer.name, "No customer"))
            .outerjoin(Project, Project.id == Invoice.project_id)
            .outerjoin(Customer, Customer.id == Project.customer_id)
            .where(Invoice.kind == InvoiceKind.TAX.value,
                   Invoice.payment_status.in_((PaymentStatus.UNPAID, PaymentStatus.PARTIALLY_PAID)),
                   Invoice.due_date.is_not(None), Invoice.due_date < today)
            .order_by(func.coalesce(Invoice.balance_amount, Invoice.grand_total).desc())).all():
        late = (today - inv.due_date).days
        it = _item(f"hdue:{inv.id}", f"{cname} · {rupees(inv.balance_amount or inv.grand_total)}",
                   f"{inv.invoice_number} · {pname or ''}", chip=f"{late} day{'s' if late != 1 else ''} overdue",
                   tone="bad" if late > 30 else "warn", when=inv.due_date.isoformat(),
                   path=f"invoices/{inv.id}", action="Open invoice")
        it.update(**_facets(cname, pname, day=inv.invoice_date))
        due.append(it)
    out.append(_tab("head_collections", due[:HEAD_TOP_COLLECTIONS], count=len(due)))

    # 5 — PO renewals across the company.
    out.append(_tab("head_po_renewals", _renewal_items(db, customers=None, rolloff_days=0, prefix="hpo")))
    return out


# ------------------------------------------------------------------ HR

#: "Joining soon" / "Joined recently" windows, and how far ahead notice is shown.
HR_JOINING_DAYS = 30
HR_JOINED_DAYS = 30
HR_EXIT_DAYS = 60
#: Birthdays and work anniversaries this far ahead.
HR_CELEBRATION_DAYS = 14
#: What an active employee record must hold (label → test). Payroll needs the
#: CTC, every report the Emp ID and the joining date, leave the manager.
HR_REQUIRED_FIELDS = (
    ("CTC", lambda e: e.current_ctc is not None),
    ("Emp ID", lambda e: bool((e.employee_code or "").strip())),
    ("Joining date", lambda e: e.date_of_joining is not None),
    ("Designation", lambda e: bool(e.designation_id or (e.role_title or "").strip())),
    ("Reporting manager", lambda e: e.reporting_manager_id is not None),
    ("Date of birth", lambda e: e.date_of_birth is not None),
)


def _hr_tabs(db: Session) -> list[dict]:
    """HR's desk (29 Sep 2026, user ask: "only HR-related tabs — upcoming
    interviews, future joiners, already joined; think as HR"). The candidate's
    tail (HR Discussion → HR round → Pre-Onboarding) from the profiles, the
    people side (joining soon · joined recently · on notice) from Employees.
    Batched: one profile query, the round state, one events query, two
    employee queries."""
    from datetime import date, timedelta

    from models import Employee, InterviewEvent
    from services.candidate_profiles import BUDGET_CONCERN, BUDGET_OUT, BUDGET_RESOLVED
    from services.interview_rounds import NOT_HELD_STATUSES
    from services.resumes import manual_round_state

    today = date.today()
    now = datetime.now(timezone.utc)
    rows = _profiles(db, (PS.HR_SCREENING.value, PS.HR_INTERVIEWING.value, PS.PREBOARDING.value), owner_id=None)
    rounds = manual_round_state(db, [p.id for p, *_ in rows])
    tabs: dict[str, list[dict]] = {k: [] for k in ("hr_discussion", "hr_onboarding", "hr_joining")}
    by_id = {}
    for p, cand, opp, _rid in rows:
        by_id[p.id] = (p, cand, opp)
        stage = getattr(p.pipeline_status, "value", p.pipeline_status)
        r = rounds.get(p.id, {})
        path = f"profiles/{p.id}"
        if stage == PS.HR_SCREENING.value:
            requested = bool(r.get("hr_requested"))
            it = _item(f"hrd:{p.id}", _name(cand), _opp(opp),
                       chip="Waiting for TA to book" if requested else "Request the HR round",
                       tone="info" if requested else "warn", when=_when(p.updated_at), path=path,
                       action="Open" if requested else "Request HR round", profile_id=p.id)
            it["hr_requested"] = requested
            tabs["hr_discussion"].append(it)
        elif stage == PS.PREBOARDING.value:
            chip, tone = {
                BUDGET_OUT: ("Out of budget — with Sales", "warn"),
                BUDGET_RESOLVED: ("Sales replied — your call", "bad"),
                BUDGET_CONCERN: ("Budget concern — check terms", "warn"),
            }.get(p.budget_status or "", ("Complete onboarding", "ok"))
            tabs["hr_onboarding"].append(_item(
                f"hro:{p.id}", _name(cand), _opp(opp), chip=chip, tone=tone,
                when=p.customer_onboarding_date.isoformat() if p.customer_onboarding_date else None,
                path=path, action="Open", profile_id=p.id))
        cod = p.customer_onboarding_date
        if cod and today <= cod <= today + timedelta(days=HR_JOINING_DAYS):
            days = (cod - today).days
            tabs["hr_joining"].append(_item(
                f"hrj:{p.id}", _name(cand), _opp(opp),
                chip="Today" if days == 0 else f"In {days} day{'s' if days != 1 else ''}",
                tone="warn" if days <= 3 else "info", when=cod.isoformat(), path=path, action="Open",
                profile_id=p.id))

    # HR rounds booked from today on (the feedback tab has the ones already held).
    interviews = []
    for ev in db.execute(
            select(InterviewEvent).where(
                InterviewEvent.kind == "HR_Interview", InterviewEvent.scheduled_at.is_not(None),
                InterviewEvent.scheduled_at >= now - timedelta(hours=1),
                func.coalesce(InterviewEvent.status, "").notin_(NOT_HELD_STATUSES),
                func.coalesce(InterviewEvent.result, "") == "")
            .order_by(InterviewEvent.scheduled_at)).scalars():
        p, cand, opp = by_id.get(ev.profile_id, (None, None, None))
        if p is None:
            continue
        when = ev.scheduled_at if ev.scheduled_at.tzinfo else ev.scheduled_at.replace(tzinfo=timezone.utc)
        hours = (when - now).total_seconds() / 3600
        it = _item(f"hri:{ev.id}", _name(cand), _opp(opp),
                   chip="Starting now" if hours <= 1 else "Today" if when.date() == now.date() else "Booked",
                   tone="warn" if hours <= 24 else "info", when=_when(when), path=f"profiles/{p.id}?tab=interviews",
                   action="Open", profile_id=p.id)
        it["meeting_link"] = ev.meeting_link
        it["interviewer"] = ev.interviewer
        interviews.append(it)

    def emp_name(e) -> str:
        return (getattr(e, "display_name", None)
                or " ".join(x for x in (e.first_name, e.last_name) if x) or f"Employee #{e.id}")

    for e in db.execute(select(Employee).where(
            Employee.date_of_joining > today,
            Employee.date_of_joining <= today + timedelta(days=HR_JOINING_DAYS))
            .order_by(Employee.date_of_joining)).scalars():
        days = (e.date_of_joining - today).days
        tabs["hr_joining"].append(_item(
            f"hre:{e.id}", emp_name(e), e.role_title or e.work_location or "",
            chip=f"In {days} day{'s' if days != 1 else ''}", tone="warn" if days <= 3 else "info",
            when=e.date_of_joining.isoformat(), path=f"employees/{e.id}", action="Open"))
    tabs["hr_joining"].sort(key=lambda it: it["when"] or "")

    joined = [_item(f"hrn:{e.id}", emp_name(e), e.role_title or e.work_location or "",
                    chip=f"Joined {(today - e.date_of_joining).days} day{'s' if (today - e.date_of_joining).days != 1 else ''} ago"
                    if e.date_of_joining != today else "Joined today",
                    tone="ok", when=e.date_of_joining.isoformat(), path=f"employees/{e.id}", action="Open")
              for e in db.execute(select(Employee).where(
                  Employee.is_active.is_(True),
                  Employee.date_of_joining <= today,
                  Employee.date_of_joining >= today - timedelta(days=HR_JOINED_DAYS))
                  .order_by(Employee.date_of_joining.desc())).scalars()]

    exits = []
    for e in db.execute(select(Employee).where(
            Employee.last_working_day.is_not(None), Employee.last_working_day >= today,
            Employee.last_working_day <= today + timedelta(days=HR_EXIT_DAYS))
            .order_by(Employee.last_working_day)).scalars():
        days = (e.last_working_day - today).days
        exits.append(_item(f"hrx:{e.id}", emp_name(e), e.role_title or e.work_location or "",
                           chip="Last day today" if days == 0 else f"Last day in {days} day{'s' if days != 1 else ''}",
                           tone="bad" if days <= 7 else "warn", when=e.last_working_day.isoformat(),
                           path=f"employees/{e.id}", action="Open"))

    people = _hr_people_tabs(db, today, emp_name)
    return [_tab("hr_discussion", tabs["hr_discussion"]), _tab("hr_interviews", interviews),
            _tab("hr_onboarding", tabs["hr_onboarding"]), people["hr_leave"], people["hr_records"],
            _tab("hr_joining", tabs["hr_joining"]), _tab("hr_joined", joined), _tab("hr_exits", exits),
            people["hr_bench"], people["hr_celebrations"]]


def _next_occurrence(d, today):
    """The next date (today or later) with d's month and day; 29 Feb → 28 Feb. PURE."""
    for year in (today.year, today.year + 1):
        try:
            nxt = d.replace(year=year)
        except ValueError:
            nxt = d.replace(year=year, day=28)
        if nxt >= today:
            return nxt
    return None


def _hr_people_tabs(db: Session, today, emp_name) -> dict[str, dict]:
    """The people side of HR's day (29 Sep 2026, user ask "think what HR needs"):
    leave to approve · records to complete · the bench · birthdays and
    anniversaries. One query per tab; the bench reuses `deployment_by_employee`."""
    from datetime import timedelta

    from models import Employee
    from models.leave import LeaveApplication
    from services.project_closure import BENCH, deployment_by_employee

    leave = []
    for la in db.execute(select(LeaveApplication).where(LeaveApplication.status == "Pending")
                         .order_by(LeaveApplication.from_date)).scalars():
        e = la.employee
        days = float(la.days or 0)
        starts = (la.from_date - today).days
        kind = getattr(la.leave_type, "name", None) or "Leave"
        span = la.from_date.strftime("%d %b") + (f" – {la.to_date.strftime('%d %b')}" if la.to_date != la.from_date else "")
        leave.append(_item(
            f"hrl:{la.id}", emp_name(e) if e else f"Employee #{la.employee_id}",
            f"{kind} · {span} · {days:g} day{'s' if days != 1 else ''}",
            chip="Already started" if starts < 0 else "Starts today" if starts == 0 else f"Starts in {starts} d",
            tone="bad" if starts <= 0 else "warn" if starts <= 3 else "info",
            when=la.from_date.isoformat(), path="leave-applications", action="Review"))

    active = db.execute(select(Employee).where(
        Employee.is_active.is_(True),
        (Employee.last_working_day.is_(None)) | (Employee.last_working_day >= today))).scalars().all()

    records = []
    for e in active:
        missing = [label for label, ok in HR_REQUIRED_FIELDS if not ok(e)]
        if missing:
            it = _item(f"hrr:{e.id}", emp_name(e), e.employee_code or e.role_title or "",
                       chip="Missing: " + ", ".join(missing[:3]) + (f" +{len(missing) - 3}" if len(missing) > 3 else ""),
                       tone="bad" if "CTC" in missing or "Emp ID" in missing else "warn",
                       when=None, path=f"employees/{e.id}", action="Complete")
            it["missing"] = missing
            records.append(it)
    records.sort(key=lambda it: -len(it["missing"]))

    deploy = deployment_by_employee(db, {e.id for e in active}, today)
    bench = [_item(f"hrb:{e.id}", emp_name(e), e.role_title or e.work_location or "",
                   chip="No CTC on record" if e.current_ctc is None else "On the bench",
                   tone="warn", when=None, path=f"employees/{e.id}", action="Open")
             for e in active if deploy.get(e.id, {}).get("status") == BENCH]

    horizon = today + timedelta(days=HR_CELEBRATION_DAYS)
    celebrations = []
    for e in active:
        for label, d in (("Birthday", e.date_of_birth), ("Work anniversary", e.date_of_joining)):
            if d is None or (label == "Work anniversary" and d.year >= today.year):
                continue
            nxt = _next_occurrence(d, today)
            if nxt is None or nxt > horizon:
                continue
            days = (nxt - today).days
            years = nxt.year - d.year
            what = label if label == "Birthday" else f"{years} year{'s' if years != 1 else ''} with us"
            celebrations.append(_item(
                f"hrc:{label[0]}:{e.id}", emp_name(e), what,
                chip="Today" if days == 0 else "Tomorrow" if days == 1 else f"In {days} days",
                tone="ok" if days == 0 else "info", when=nxt.isoformat(),
                path=f"employees/{e.id}", action="Open"))
    celebrations.sort(key=lambda it: it["when"] or "")

    return {"hr_leave": _tab("hr_leave", leave), "hr_records": _tab("hr_records", records),
            "hr_bench": _tab("hr_bench", bench), "hr_celebrations": _tab("hr_celebrations", celebrations)}


# ------------------------------------------------------------------ Finance

#: Approved sheets older than this are not "coming up" any more — one invoiced
#: by hand (no timesheet link) would otherwise sit on the list for ever.
FIN_TIMESHEET_DAYS = 90
#: "Tax invoices issued" looks back this far.
FIN_ISSUED_DAYS = 30
#: A Proforma waiting longer than this is flagged amber, twice as long red.
PROFORMA_WAIT_DAYS = 3


def rupees(value) -> str:
    """₹ with Indian grouping, no paise (a tile, not an invoice)."""
    from services.tax_invoice import format_inr

    return "₹" + format_inr(round(float(value or 0))).replace("INR ", "").rsplit(".", 1)[0]


def _month_label(year: int, month: int) -> str:
    from calendar import month_abbr

    return f"{month_abbr[month]} {year}"


def _employee_name(e) -> str:
    if e is None:
        return "Employee"
    return " ".join(x for x in (e.first_name, getattr(e, "last_name", None)) if x) or "Employee"


def _age_days(dt, today) -> int | None:
    if dt is None:
        return None
    d = dt.date() if hasattr(dt, "date") else dt
    return (today - d).days


def _facets(customer=None, project=None, year=None, month=None, *, day=None,
            employee=None, amount=None) -> dict:
    """The filter facets of a billing item (29 Sep 2026, user ask: "filters in
    every tab — customer wise, month wise, search"): the customer, the project
    and the month the work belongs to — a timesheet's PERIOD, an invoice's date
    (never the day it was touched). 6 Oct 2026: the employee the sheet / invoice
    is for and the invoice's amount (the "Largest first" sort), when known."""
    if day is not None:
        year, month = day.year, day.month
    out = {"customer": customer or None, "project": project or None}
    if year and month:
        out["month"] = f"{int(year):04d}-{int(month):02d}"
    if employee:
        out["employee"] = employee
    if amount is not None:
        out["amount"] = float(amount)
    return out


#: The words each side of the billing chain reads (29 Sep 2026): Finance waits on
#: the GM's Proforma and converts it; the GM raises / reissues it and watches it
#: with Finance. One query set, two vocabularies — `billing_chain(audience=)`.
_CHAIN_WORDS = {
    "finance": {"awaiting": ("Waiting for GM's Proforma", "info", "Open timesheet"),
                "returned": ("Returned to GM", "warn", "Open timesheet"),
                "proforma": ("Ready to convert", "Generate tax invoice")},
    "gm": {"awaiting": ("Approved — raise the Proforma", "warn", "Raise Proforma"),
           "returned": ("Returned by Finance — reissue", "bad", "Reissue Proforma"),
           "proforma": ("With Finance", "Open Proforma")},
}


def billing_chain(db: Session, *, audience: str = "finance") -> dict[str, list[dict]]:
    """The timesheet → Proforma → tax-invoice chain as item lists, for Finance's
    desk and the GM's (29 Sep 2026). Keys:

    * `submitted` — sheets waiting for the approver (the GM's "to approve");
    * `awaiting` — approved sheets with no issued invoice, or whose Proforma
      Finance returned (last `FIN_TIMESHEET_DAYS`);
    * `proformas` — Proformas with Finance (not returned);
    * `issued` — tax invoices of the last `FIN_ISSUED_DAYS`.

    Five batched queries; every item carries the customer / project / month
    facets. The WORDING follows `audience` ("finance" | "gm")."""
    from datetime import date, timedelta

    from models import Customer, Employee, Invoice, Project, Timesheet, TimesheetStatus
    from models.finance import InvoiceKind

    words = _CHAIN_WORDS[audience]
    today = date.today()
    tax, proforma = InvoiceKind.TAX.value, InvoiceKind.PROFORMA.value

    def names(project_ids):
        rows = db.execute(select(Project.id, Project.name, Customer.name)
                          .outerjoin(Customer, Customer.id == Project.customer_id)
                          .where(Project.id.in_(project_ids or [0]))).all()
        return {pid: (pname, cname) for pid, pname, cname in rows}

    def sheet_item(key, ts, emp, nm, chip, tone, action, when):
        pname, cname = nm.get(ts.project_id, ("Project", None))
        return _item(
            f"{key}:{ts.id}", f"{_employee_name(emp)} · {_month_label(ts.year, ts.month)}",
            " · ".join(x for x in (pname, cname) if x), chip=chip, tone=tone, when=_when(when),
            path=f"timesheets/{ts.id}", action=action) | {"section": cname or "No customer"} \
            | _facets(cname, pname, ts.year, ts.month, employee=_employee_name(emp))

    def employees_of(invoices):
        """The employee each invoice bills, through its timesheet — ONE query."""
        sheet_ids = [i.timesheet_id for i in invoices if i.timesheet_id]
        if not sheet_ids:
            return {}
        return {tsid: _employee_name(emp) for tsid, emp in db.execute(
            select(Timesheet.id, Employee).join(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.id.in_(sheet_ids))).all()}

    # 0 — submitted sheets: the approver's move (oldest first — they have waited longest).
    submitted = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.SUBMITTED)
        .order_by(func.coalesce(Timesheet.submitted_at, Timesheet.updated_at).asc())).all()
    nm = names({ts.project_id for ts, _ in submitted})
    sub_items = []
    for ts, emp in submitted:
        age = _age_days(ts.submitted_at or ts.updated_at, today) or 0
        sub_items.append(sheet_item(
            "tsa", ts, emp, nm, f"Submitted {age} d ago" if age else "Submitted today",
            "bad" if age > 2 * PROFORMA_WAIT_DAYS else "warn" if age > PROFORMA_WAIT_DAYS else "info",
            "Review & approve", ts.submitted_at or ts.updated_at))

    # 1 — approved sheets with no issued tax invoice (the GM's Proforma is next).
    since = datetime.now(timezone.utc) - timedelta(days=FIN_TIMESHEET_DAYS)
    sheets = db.execute(
        select(Timesheet, Employee)
        .outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.APPROVED,
               func.coalesce(Timesheet.approved_at, Timesheet.updated_at) >= since)
        .order_by(func.coalesce(Timesheet.approved_at, Timesheet.updated_at).desc())
    ).all()
    docs: dict[int, Invoice] = {}
    if sheets:
        for inv in db.execute(select(Invoice).where(
                Invoice.timesheet_id.in_([ts.id for ts, _ in sheets])).order_by(Invoice.id)).scalars():
            docs[inv.timesheet_id] = inv          # latest document per sheet wins
    waiting = [(ts, emp) for ts, emp in sheets
               if docs.get(ts.id) is None or (docs[ts.id].kind == proforma and docs[ts.id].returned_at)]
    nm = names({ts.project_id for ts, _ in waiting})
    ts_items = []
    for ts, emp in waiting:
        chip, tone, action = words["returned" if docs.get(ts.id) is not None else "awaiting"]
        ts_items.append(sheet_item("ts", ts, emp, nm, chip, tone, action, ts.approved_at or ts.updated_at))

    # 2 — Proformas raised and not returned: Finance's move.
    pis = db.execute(select(Invoice).where(Invoice.kind == proforma, Invoice.returned_at.is_(None))
                     .order_by(Invoice.id)).scalars().all()
    nm = names({i.project_id for i in pis})
    emp_of = employees_of(pis)
    pi_chip, pi_action = words["proforma"]
    pi_items = []
    for inv in pis:
        pname, cname = nm.get(inv.project_id, ("Project", None))
        who = emp_of.get(inv.timesheet_id or 0)
        age = _age_days(inv.invoice_date, today) or 0
        tone = "bad" if age > 2 * PROFORMA_WAIT_DAYS else "warn" if age > PROFORMA_WAIT_DAYS else "info"
        pi_items.append(_item(
            f"pi:{inv.id}", f"{inv.proforma_number or inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, who) if x) + (f" · waiting {age} day{'s' if age != 1 else ''}" if age else ""),
            chip=pi_chip, tone=tone, when=_when(inv.invoice_date),
            path=f"invoices/{inv.id}", action=pi_action) | {"section": cname or "No customer"}
            | _facets(cname, pname, day=inv.invoice_date, employee=who, amount=inv.grand_total))

    # 3 — tax invoices issued in the window (done), newest first.
    cutoff = today - timedelta(days=FIN_ISSUED_DAYS)
    issued = db.execute(select(Invoice).where(Invoice.kind == tax, Invoice.invoice_date >= cutoff)
                        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())).scalars().all()
    nm = names({i.project_id for i in issued})
    emp_of = employees_of(issued)
    iv_items = []
    for inv in issued:
        pname, cname = nm.get(inv.project_id, ("Project", None))
        who = emp_of.get(inv.timesheet_id or 0)
        status = getattr(inv.payment_status, "value", inv.payment_status) or "Unpaid"
        paid = str(status).lower() == "paid"
        from_pi = f"from {inv.proforma_number}" if inv.proforma_number else None
        iv_items.append(_item(
            f"iv:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, who, from_pi) if x),
            chip=str(status).replace("_", " "), tone="ok" if paid else "info",
            when=_when(inv.invoice_date), path=f"invoices/{inv.id}", action="Open invoice")
            | {"section": cname or "No customer"}
            | _facets(cname, pname, day=inv.invoice_date, employee=who, amount=inv.grand_total))

    return {"submitted": sub_items, "awaiting": ts_items, "proformas": pi_items, "issued": iv_items}


def _finance_tabs(db: Session) -> list[dict]:
    """Finance's desk (29 Sep 2026, user ask): the timesheet → Proforma →
    tax-invoice chain as three tabs (`billing_chain`, Finance's words), then
    the customer-approved invoices waiting for the IRN (5 Oct 2026)."""
    chain = billing_chain(db, audience="finance")
    return [_tab("fin_timesheets", chain["awaiting"]), _tab("fin_proformas", chain["proformas"]),
            _tab("fin_invoices", chain["issued"]), _customer_approved_tab(db)]


#: "Customer approved invoices" keeps an invoice whose IRN is in for this long.
FIN_IRN_DONE_DAYS = 30
#: "Confirm with customer" looks back this far (invoice date).
CONFIRM_LOOKBACK_DAYS = 90
#: An invoice waiting longer than this for the customer's approval turns amber, twice as long red.
CONFIRM_WAIT_DAYS = 7


def _invoice_names(db: Session, invoices) -> dict[int, tuple[str | None, str | None]]:
    """project id → (project name, customer name) — ONE query."""
    from models import Customer, Project

    ids = {i.project_id for i in invoices if i.project_id}
    if not ids:
        return {}
    return {pid: (pn, cn) for pid, pn, cn in db.execute(
        select(Project.id, Project.name, Customer.name)
        .outerjoin(Customer, Customer.id == Project.customer_id)
        .where(Project.id.in_(ids))).all()}


def _customer_approved_tab(db: Session) -> dict:
    """Finance (5 Oct 2026): every customer-approved tax invoice still without
    an IRN (oldest approval first — the count), then those whose IRN went in
    during the last `FIN_IRN_DONE_DAYS` (done)."""
    from datetime import timedelta

    from models import Invoice
    from models.finance import InvoiceKind

    since = datetime.now(timezone.utc) - timedelta(days=FIN_IRN_DONE_DAYS)
    pending = db.execute(select(Invoice).where(
        Invoice.kind == InvoiceKind.TAX.value, Invoice.customer_approved_at.isnot(None),
        Invoice.irn_number.is_(None)).order_by(Invoice.customer_approved_at.asc())).scalars().all()
    done = db.execute(select(Invoice).where(
        Invoice.kind == InvoiceKind.TAX.value, Invoice.customer_approved_at.isnot(None),
        Invoice.irn_number.isnot(None), Invoice.irn_recorded_at >= since)
        .order_by(Invoice.irn_recorded_at.desc())).scalars().all()
    nm = _invoice_names(db, [*pending, *done])
    now = datetime.now(timezone.utc)
    items = []
    for inv in pending:
        pname, cname = nm.get(inv.project_id, (None, None))
        at = inv.customer_approved_at
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        days = (now - at).days if at else 0
        items.append(_item(
            f"irn:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, f"approved {days} d ago" if days else "approved today") if x),
            chip="Add IRN", tone="bad" if days > 6 else "warn", when=_when(inv.customer_approved_at),
            path=f"invoices/{inv.id}", action="Add IRN & Ack No.")
            | {"section": "IRN to add"} | _facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))
    for inv in done:
        pname, cname = nm.get(inv.project_id, (None, None))
        items.append(_item(
            f"irn:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, f"Ack {inv.ack_number}" if inv.ack_number else None) if x),
            chip="IRN recorded", tone="ok", when=_when(inv.irn_recorded_at),
            path=f"invoices/{inv.id}", action="Open invoice")
            | {"section": "IRN recorded"} | _facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))
    return _tab("fin_customer_approved", items, count=len(pending))


def _customer_confirm_tab(db: Session) -> dict:
    """Sales Manager / Sales Head (5 Oct 2026): original invoices of the last
    `CONFIRM_LOOKBACK_DAYS` whose customer approval is not confirmed yet."""
    from datetime import date, timedelta

    from models import Invoice
    from models.finance import InvoiceKind

    today = date.today()
    rows = db.execute(select(Invoice).where(
        Invoice.kind == InvoiceKind.TAX.value, Invoice.customer_approved_at.is_(None),
        Invoice.invoice_date >= today - timedelta(days=CONFIRM_LOOKBACK_DAYS))
        .order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).scalars().all()
    nm = _invoice_names(db, rows)
    items = []
    for inv in rows:
        pname, cname = nm.get(inv.project_id, (None, None))
        age = _age_days(inv.invoice_date, today) or 0
        items.append(_item(
            f"confirm:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname) if x),
            chip=f"Issued {age} d ago" if age else "Issued today",
            tone="bad" if age > 2 * CONFIRM_WAIT_DAYS else "warn" if age > CONFIRM_WAIT_DAYS else "info",
            when=_when(inv.invoice_date), path=f"invoices/{inv.id}", action="Confirm customer approval")
            | {"section": cname or "No customer"} | _facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))
    return _tab("inv_confirm", items)


# ---------------------------------------------------------- Admin / CEO desk


#: The only tabs an Admin / CEO login gets (30 Sep 2026, user ask: "on the
#: CEO dashboard show just his own work, not every role's"). Each is a
#: decision only the top of the ladder takes; everything else on the desk is
#: another role's day and lives on their Dashboard.
CEO_TABS = ("opp_approvals", "sales_approval")


def _opp_approvals_tab(db: Session) -> dict:
    """Opportunities Sales raised that wait for the Sales Head's approval —
    Admin / CEO approve them (`opportunities.approve`). One query."""
    from models import Customer, OpportunityApprovalStatus

    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(Opportunity, Customer.name)
        .join(Customer, Customer.id == Opportunity.customer_id, isouter=True)
        .where(Opportunity.approval_status == OpportunityApprovalStatus.PENDING_SALES_HEAD_APPROVAL)
        .order_by(Opportunity.updated_at.asc(), Opportunity.id.asc())).all()
    items = []
    for opp, customer in rows:
        since = opp.updated_at or opp.created_at
        if since is not None and since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        days = (now - since).days if since else 0
        it = _item(f"opp_approvals:{opp.id}", _opp(opp), customer or "",
                   chip=f"Waiting {days} day{'s' if days != 1 else ''}",
                   tone="bad" if days > 2 * CUSTOMER_WAIT_DAYS else "warn" if days > 1 else "info",
                   when=_when(since), path=f"opportunities/{opp.id}", action="Review & approve")
        it.update(_facets(customer, None, since.year if since else None, since.month if since else None))
        items.append(it)
    return _tab("opp_approvals", items)


def _ceo_tabs(db: Session, user) -> list[dict]:
    """Admin / CEO: `CEO_TABS` only — the deals and the candidate terms waiting
    for the top approval. The Sales builder makes the terms tab (with the
    Approve / Send back / Reject the UI needs); the rest of its output is
    Sales' work and is dropped."""
    tabs = [_opp_approvals_tab(db)]
    tabs += [t for t in _sales_tabs(db, user, is_admin=True) if t["key"] == "sales_approval"]
    return tabs


# ------------------------------------------------------------------ entry


def desk(db: Session, user, *, max_items: int = MAX_ITEMS) -> dict:
    """The tabs this login gets, in reading order, each best-effort (one tab
    failing must never blank the others — it is logged and left out).

    Every item carries the filter facets `customer` / `month` (and `project`
    on the billing tabs) — `fill_facets` — and each tab is cut to `max_items`
    AFTER its count is taken: the Dashboard tiles need 50, the My Tasks page
    asks for `FULL_MAX_ITEMS` so its filters see the whole list."""
    import logging

    from services.action_permissions import screens_as_rmg

    roles = set(user.roles or ())
    is_admin = bool(roles & {"Admin", "CEO"})
    ta = "TA" in roles or is_admin
    owner_id = None if is_admin else user.id
    screener = screens_as_rmg(db, user)
    # Finance's billing chain: the Finance role, or a template / custom role that
    # converts Proformas. Admin / CEO have the company view instead.
    finance = "Finance" in roles or (not is_admin and _may_convert(db, user))
    # Sales / Sales Head / Sales Manager (implied roles) and Admin / CEO.
    sales = bool(roles & {"Sales", "Sales_Head"}) or is_admin
    from services.role_implications import sees_team
    sales_all = is_admin or "Sales_Head" in roles or sees_team(roles)
    hr_only = "HR" in roles and not is_admin and not ta and not screener and not finance and not sales
    # A Sales-family login (Sales · Sales Manager · Sales Head) works from its own
    # tabs; Upcoming / My queues repeated them (29 Sep 2026, user ask).
    sales_only = sales and not is_admin and not ta and not screener and not finance and "HR" not in roles
    no_generic = hr_only or sales_only
    # The Sales ladder (29 Sep 2026, user ask): each rung drops what is not its
    # work and adds its own tiles (`SALES_RUNG_TABS`). A Sales Manager's billing
    # tabs are their OWN deals; the team view is the team_* tiles.
    rung = sales_rung(user) if sales else None
    billing_everyone = is_admin or rung == "head" or (sales_all and rung is None)
    # The three invoice tabs (30 Sep 2026, user ask): every Sales rung has them,
    # and the Sales Manager / Sales Head see every customer's invoices.
    invoices_everyone = billing_everyone or sales_all
    dropped = set()
    if rung == "head":
        dropped |= {"sales_submit", "sales_response", "sales_collections"}
    builders = [
        # A screener's feedback tab comes from the task list (desk-aware links).
        ("feedback", (lambda: _feedback_tab(db, user)) if not screener else None),
        ("sales", (lambda: _sales_tabs(db, user, is_admin=is_admin)) if sales else None),
        ("sales_billing", (lambda: _sales_billing_tabs(db, user, everyone=billing_everyone,
                                                       invoices_everyone=invoices_everyone)) if sales else None),
        ("sales_rung", (lambda: _sales_rung_tabs(db, user, rung)) if rung else None),
        # 5 Oct 2026: the Sales Manager / Sales Head confirm the customer's
        # approval of each original invoice (Finance adds the IRN next).
        ("inv_confirm", (lambda: _customer_confirm_tab(db)) if _confirms_invoices(user) else None),
        ("hr", (lambda: _hr_tabs(db)) if "HR" in roles else None),
        ("ta", (lambda: _ta_tabs(db, user, owner_id)) if ta else None),
        ("screener", (lambda: _screener_tabs(db, user)) if screener else None),
        ("finance", (lambda: _finance_tabs(db)) if finance else None),
        # An HR-only login's day is the HR tabs above; the generic Upcoming /
        # My queues tiles repeated them in a different shape (29 Sep 2026, user ask).
        ("upcoming", (lambda: _upcoming_tab(db, user)) if not no_generic else None),
        ("queues", (lambda: _queues_tab(db, user)) if not no_generic else None),
    ]
    if is_admin:
        # Admin / CEO: ONLY the decisions that are theirs (`CEO_TABS`). Every
        # other tab is some role's day and lives on THAT role's Dashboard
        # (30 Sep 2026, user ask: "show just his own work, not every role's").
        builders = [("ceo", lambda: _ceo_tabs(db, user))]
    tabs = []
    for key, build in builders:
        if build is None:
            continue
        try:
            with db.begin_nested():
                tab = build()
        except Exception:
            logging.getLogger("karnex.crm.work_desk").warning("desk tab %s failed", key, exc_info=True)
            continue
        if isinstance(tab, list):
            tabs.extend(tab)
        elif tab is not None:
            tabs.append(tab)
    tabs = [t for t in tabs if t.get("key") not in dropped]
    try:
        with db.begin_nested():
            fill_facets(db, tabs)
    except Exception:
        logging.getLogger("karnex.crm.work_desk").warning("desk facets failed", exc_info=True)
    for tab in tabs:
        tab["items"] = tab["items"][:max_items]
    return {"tabs": tabs, "as_of": datetime.now(timezone.utc).isoformat()}


def fill_facets(db: Session, tabs: list[dict]) -> None:
    """Give every item a `customer` and a `month` it does not already carry.
    Candidate items take the customer of their opportunity (ONE query for the
    whole desk); the month falls back to the item's own date (`when`)."""
    from models import Customer

    missing = {it["profile_id"] for tab in tabs for it in tab["items"]
               if it.get("profile_id") and not it.get("customer")}
    names: dict[int, str] = {}
    if missing:
        names = dict(db.execute(
            select(CandidateProfile.id, Customer.name)
            .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
            .join(Customer, Customer.id == Opportunity.customer_id)
            .where(CandidateProfile.id.in_(missing))).all())
    for tab in tabs:
        for it in tab["items"]:
            if not it.get("customer"):
                it["customer"] = names.get(it.get("profile_id")) or None
            if not it.get("month") and isinstance(it.get("when"), str) and len(it["when"]) >= 7:
                it["month"] = it["when"][:7]


def _confirms_invoices(user) -> bool:
    from services.invoice_customer_approval import may_confirm

    return may_confirm(user)


def _may_convert(db: Session, user) -> bool:
    try:
        from services.action_permissions import user_may

        return bool(user_may(db, user, "invoice.convert_proforma"))
    except Exception:
        return False


#: Screener categories that are always a tab (the core of the RMG / GM day);
#: the rest appear only while they hold something.
_ALWAYS_SCREENER_TABS = ("feedback", "results", "screening")
#: The caption over the GM's billing tiles — where each sits in the chain.
_SCREENER_STAGE = {"ts_approve": "Your move", "proforma_raise": "Your move",
                   "proforma_finance": "With Finance", "invoices_issued": "Done"}


def _screener_tabs(db: Session, user) -> list[dict]:
    """RMG / GM (28 Sep 2026): the SAME categories as the Screening Desk's task
    board (`services/rmg_tasks.screener_tasks`) — so the Dashboard and the desk
    can never disagree. Feedback items keep `profile_id`, so the Dashboard
    still records the verdict in a pop-up; empty extras are left out."""
    from services.rmg_tasks import screener_tasks

    out = []
    for c in screener_tasks(db, user)["categories"]:
        if not c["count"] and c["key"] not in _ALWAYS_SCREENER_TABS and not c.get("always"):
            continue
        tab = {"key": c["key"], "label": c["label"], "hint": c["hint"], "icon": c["icon"],
               "count": c["count"], "items": c["items"], "link": tab_link(c["key"], screener=True)}
        if c.get("info"):
            tab["info"] = True
        if c["key"] in _SCREENER_STAGE:
            tab["stage"] = _SCREENER_STAGE[c["key"]]
        out.append(tab)
    return out
