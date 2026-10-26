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
#: every item, not the Dashboard's first 50. 20 Oct 2026: 3000 (was 500) —
#: "Tax invoices issued" lists EVERY invoice, and past 500 the oldest ones
#: silently fell off before the month / customer filter ever saw them.
FULL_MAX_ITEMS = 3000

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
    # The panel member's own technical rounds (7 Oct 2026) — opens My Interviews.
    "panel_feedback": ("My interview feedback",
                       "Technical rounds you took that still need your verdict — record it on My Interviews."),
    "panel_upcoming": ("My upcoming interviews",
                       "Technical rounds booked with you as the interviewer — the CV and the AI interview are on My Interviews."),
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
                     "Every original invoice from the data start month, with its payment status."),
    "fin_awaiting_customer": ("Awaiting customer approval",
                              "Original invoices with Sales for the customer's approval — the Invoices ▸ "
                              "Awaiting customer list, item for item."),
    "fin_customer_approved": ("Pending e-invoices",
                              "The customer accepted these invoices — add the e-invoice IRN and "
                              "Acknowledgement No. so payments can be recorded."),
    "fin_payment_pending": ("Unpaid — record payment",
                            "E-invoices with nothing received yet — record the payment or the TDS when it "
                            "arrives; overdue first."),
    "fin_partially_paid": ("Partially paid — balance due",
                           "Part received (cash or TDS), the balance still due from the customer — map the "
                           "customer's next payment advice to it; overdue first."),
    "fin_tds_pending": ("TDS pending from client",
                        "TDS the customer withheld and has not paid over yet — mark it received with the "
                        "Form 16A."),
    "fin_receipts": ("Receipts to map",
                     "Money received that is still on account or has no branch — map it to invoices and "
                     "name the branch."),
    "inv_confirm": ("Confirm with customer",
                    "Original invoices Finance generated — send them to the customer and confirm their "
                    "approval so Finance can add the IRN."),
    # The Hiring Manager (22 Oct 2026): their three decisions + their positions.
    "hm_jd": ("JD & skills to confirm",
              "Positions RMG approved — confirm the JD & skills so TA can start sourcing, or send them back."),
    "hm_review": ("Candidates to review",
                  "Candidates RMG handed to Sales — approve before the profile goes to the customer, or reject."),
    "hm_signoff": ("Selections to sign off",
                   "The customer selected these candidates and Sales submitted the terms — sign off before "
                   "the Sales Head approves."),
    "hm_positions": ("My positions", "The positions you are Hiring Manager for — open, filled and their status."),
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
             "fin_awaiting_customer": ("With Sales", True), "fin_payment_pending": ("Your move", False),
             "fin_partially_paid": ("Follow up", False),
             "fin_tds_pending": ("Follow up", False), "fin_receipts": ("Your move", False),
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
             "hr_joined": ("Done", True), "hr_exits": ("Coming up", True),
             "panel_feedback": ("Your move", False), "panel_upcoming": ("Coming up", True),
             "hm_jd": ("Your approval", False), "hm_review": ("Your approval", False),
             "hm_signoff": ("Your approval", False), "hm_positions": ("Yours", True)}


#: Tabs whose tile opens a page of its own rather than My Tasks.
TAB_PAGE = {"panel_feedback": "my-interviews?scope=pending", "panel_upcoming": "my-interviews?scope=upcoming"}


def _tab(key: str, items: list[dict], count: int | None = None) -> dict:
    label, hint = TABS[key]
    n = len(items) if count is None else count
    out = {"key": key, "label": label, "hint": hint, "count": n, "items": items,
           "link": TAB_PAGE.get(key) or tab_link(key)}
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


def _panel_tabs(db: Session, user) -> list[dict]:
    """The login's OWN technical rounds (`services.panel_interviews`): the ones
    waiting for their verdict, then the ones booked ahead. Empty when nobody
    linked an employee to this login — the tabs then do not appear."""
    from services.panel_interviews import my_rounds

    data = my_rounds(db, user, "all")
    if not data["linked"]:
        return []
    out: list[dict] = []
    for key, phase, action in (("panel_feedback", "pending", "Record feedback"),
                               ("panel_upcoming", "upcoming", "Open")):
        items = []
        for r in data["rows"]:
            if r["phase"] != phase:
                continue
            pos = r["position"]
            items.append(_item(
                f"panel:{r['id']}", r["candidate"]["name"],
                f"{r['round_label']}" + (f" · {pos['opp_id']} — {pos['title']}" if pos.get("title") else ""),
                chip=r["raw_when"] or ("Time not set" if not r["scheduled_at"] else None),
                tone="warn" if phase == "pending" else "info", when=r["scheduled_at"],
                path=f"my-interviews?focus={r['id']}", action=action, profile_id=r["profile_id"]))
        if items or key == "panel_feedback":
            out.append(_tab(key, items))
    return out


def _hm_tabs(db: Session, user) -> list[dict]:
    """The Hiring Manager's day (22 Oct 2026, services/hiring_managers): the
    JD & skills to confirm, candidates to review before the customer, selections
    to sign off before the Sales Head — on the positions they are on only —
    then the positions themselves. Empty for a login on no position."""
    from models import RequirementStatus
    from services import hiring_managers as hm
    from services.requirements import positions_by_opportunity, requirement_label

    req_ids = hm.requirement_ids_for(db, user.id)
    if not req_ids:
        # The tiles still show (reading "All clear"): an HM on no position yet
        # must not get an empty Dashboard (23 Oct 2026).
        return [_tab(k, []) for k in ("hm_jd", "hm_review", "hm_signoff", "hm_positions")]
    reqs = db.execute(select(Requirement, Opportunity)
                      .join(Opportunity, Opportunity.id == Requirement.opportunity_id)
                      .where(Requirement.id.in_(req_ids))).all()
    opp_ids = [o.id for _, o in reqs]
    jd = [_item(f"hmjd:{r.id}", f"{requirement_label(r)} — {r.title}",
                "RMG approved — confirm the JD & skills", chip="JD & skills", tone="warn",
                when=r.engineering_reviewed_at.isoformat() if r.engineering_reviewed_at else None, path=f"opportunities/{o.id}",
                action="Review JD & skills")
          for r, o in reqs if r.status == RequirementStatus.PENDING_HM_REVIEW]
    out = [_tab("hm_jd", jd)]
    for key, col, stage, action in (
            ("hm_review", CandidateProfile.hm_review_status, PS.SALES_SCREENING.value, "Review candidate"),
            ("hm_signoff", CandidateProfile.hm_final_status, PS.CUSTOMER_APPROVAL.value, "Sign off")):
        items = []
        for p, cand, opp, _rid in _profiles(db, [stage], owner_id=None,
                                             where=(col == hm.PENDING,
                                                    CandidateProfile.opportunity_id.in_(opp_ids))):
            name = " ".join(x for x in (cand.first_name, cand.last_name) if x) or f"Candidate #{cand.id}"
            items.append(_item(f"{key}:{p.id}", name, f"{opp.opp_id} — {opp.title}",
                               chip="Waiting for you", tone="warn", when=p.updated_at.isoformat() if p.updated_at else None,
                               path=f"profiles/{p.id}", action=action, profile_id=p.id))
        out.append(_tab(key, items))
    heads = positions_by_opportunity(db, opp_ids)
    pos = []
    for r, o in reqs:
        h = heads.get(o.id, {})
        pos.append(_item(f"hmpos:{r.id}", f"{requirement_label(r)} — {r.title}",
                         f"{h.get('positions_open', 0)} open of {h.get('positions_total', r.no_of_positions)}",
                         chip=h.get("requirement_display_status") or getattr(r.status, "value", r.status),
                         tone="info", path=f"opportunities/{o.id}", action="Open"))
    out.append(_tab("hm_positions", pos))
    return out


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


def pending_offers(db: Session, ids: list[int]) -> dict[int, dict]:
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
#: Derived statuses that park a candidacy — not Sales' move (27 Oct 2026).
_PARKED_STATUSES = frozenset({"deal_customer_hold", "deal_sales_hold", "candidate_hold", "hm_review"})


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
    offers = pending_offers(db, ids)
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
        parked = st.get("key") in _PARKED_STATUSES
        if parked and stage in (PS.SALES_SCREENING.value, PS.CUSTOMER_SCREENING.value):
            # 27 Oct 2026: a candidate on a held deal, on hold by request, or with
            # the Hiring Manager is NOT Sales' move — the move would be refused.
            # It stays in its tab under its real status (no move buttons, not
            # counted), so the Status filter never groups it with ready ones.
            tab = "sales_submit" if stage == PS.SALES_SCREENING.value else "sales_response"
            item(tab, st.get("label") or "On hold", "warn", "Open", moves=False)
            tabs[tab][-1].update(status=st.get("label") or "On hold", parked=True)
        elif stage == PS.SALES_SCREENING.value:
            item("sales_submit", f"Cleared by RMG · {age}", waiting_tone, "Submit to customer")
            tabs["sales_submit"][-1]["status"] = "Ready to submit"
        elif stage == PS.CUSTOMER_SCREENING.value:
            item("sales_response", f"With the customer · {age}", waiting_tone, "Record the response")
            tabs["sales_response"][-1]["status"] = "Waiting for the customer"
        elif st.get("key") in _SALES_DECIDE:
            failed = st["key"].endswith("_failed")
            item("sales_decide", st.get("label"), "bad" if failed else "ok", "Decide the next step")
            tabs["sales_decide"][-1]["status"] = st.get("label")
        elif stage == PS.SHORTLISTED.value:
            back = st.get("key") == "terms_sent_back"
            item("sales_terms", "Sent back by Sales Head" if back else f"Customer shortlisted · {age}",
                 "warn" if back else "ok", "Resubmit terms" if back else "Submit terms", moves=False)
            tabs["sales_terms"][-1]["section"] = TERMS_SENT_BACK if back else TERMS_TO_SUBMIT
            tabs["sales_terms"][-1]["status"] = TERMS_SENT_BACK if back else TERMS_TO_SUBMIT
        elif stage == PS.CUSTOMER_APPROVAL.value:
            item("sales_approval", f"Waiting {age}",
                 "bad" if days > 2 * CUSTOMER_WAIT_DAYS else "warn" if days > 1 else "info",
                 "Review terms" if approver else "Open", moves=False)
            tabs["sales_approval"][-1]["status"] = "Waiting for approval"
            # …and the same candidacy on the submitter's side, as "sent, waiting"
            # (29 Sep 2026: everything Sales sends Sales Head lives in ONE tab).
            tabs["sales_terms"].append(dict(tabs["sales_approval"][-1], key=f"sales_terms_wait:{p.id}",
                                            chip=f"With Sales Head · {age}", tone="info", action="Open",
                                            section=TERMS_WAITING, status=TERMS_WAITING))
        elif p.budget_status == BUDGET_OUT and may_reply:
            item("sales_budget", "Out of budget", "bad", "Reply to HR", moves=False)
            tabs["sales_budget"][-1]["hr_note"] = p.budget_note
    terms = sorted(tabs["sales_terms"], key=lambda it: TERMS_SECTIONS.index(it.get("section", TERMS_TO_SUBMIT)))
    out = [_tab(k, tabs[k], count=sum(1 for it in tabs[k] if not it.get("parked")))
           for k in ("sales_submit", "sales_response", "sales_decide")]
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
    * Invoices generated — every Tax invoice of the scope (8 Oct 2026; was 90 days).
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

    from services.billing_rules import invoice_window_clause, work_month_clause

    # 1 — timesheets not yet approved (from the data start month, 23 Oct 2026).
    this_month = today.year * 12 + today.month
    ts_rows = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.project_id.in_(pids or [-1]),
               Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.SUBMITTED, TimesheetStatus.REJECTED]),
               Timesheet.year * 12 + Timesheet.month <= this_month,
               work_month_clause(Timesheet.year, Timesheet.month))
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
        it = _item(f"sts:{ts.id}", f"{_employee_name(emp)} · {_sheet_month(ts)}", pname,
                   chip=chip, tone=tone, when=_when(ts.submitted_at or ts.updated_at),
                   path=f"timesheets/{ts.id}", action=action)
        it.update(section=cname, _order=order, employee=_employee_name(emp),
                  **_facets(cname, pname, ts.year, ts.month))
        ts_items.append(it)

    # 2 — approved sheets waiting for an issued invoice.
    approved = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.project_id.in_(inv_pids or [-1]), Timesheet.status == TimesheetStatus.APPROVED,
               work_month_clause(Timesheet.year, Timesheet.month))).all()
    sdocs = _sheet_docs(db, [ts.id for ts, _ in approved])
    pend_items = []
    for ts, emp in approved:
        state = _sheet_bill_state(ts, sdocs.get(ts.id) or [])
        if state is None:
            continue
        pname, cname = where_of(ts.project_id, inv_projects)
        returned = state[0] == "returned"
        chip = ("Proforma returned by Finance" if returned
                else f"Part billed — {rupees(state[1])} still to bill" if state[0] == "partial"
                else "Approved — waiting for GM's Proforma")
        it = _item(f"sip:{ts.id}", f"{_employee_name(emp)} · {_sheet_month(ts)}", pname,
                   chip=chip,
                   tone="warn" if returned or state[0] == "partial" else "info",
                   when=_when(ts.approved_at or ts.updated_at),
                   path=f"timesheets/{ts.id}", action="Open timesheet")
        it.update(section=cname, _order=0, employee=_employee_name(emp),
                  **_facets(cname, pname, ts.year, ts.month))
        pend_items.append(it)

    # 3 + 4 + 5 — Proformas with Finance; tax invoices issued recently; past due
    # and unpaid. The employee comes through the invoice's timesheet — ONE query
    # for every invoice on the page; a manual invoice names nobody.
    # 8 Oct 2026: EVERY tax invoice of the scope (was the last 90 days) — the
    # tab filters by customer / employee / month instead of hiding older ones.
    inv_q = select(Invoice).where(
        invoice_window_clause(),
        or_((Invoice.kind == proforma) & Invoice.returned_at.is_(None), Invoice.kind == tax))
    if not invoices_everyone:
        inv_q = inv_q.where(Invoice.project_id.in_(inv_pids or [-1]))
    # 27 Oct 2026: the whole of Sales sees EVERY invoice — a manual invoice with
    # no project used to be missing here while "Confirm with customer" listed it.
    invoices = db.execute(inv_q.order_by(Invoice.invoice_date.desc(), Invoice.id.desc())).scalars().all()
    inv_nm = _invoice_names(db, invoices)
    inv_state = invoice_states(db, invoices, today)
    from services.billing_rules import COLLECTABLE_STAGES
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
        # ONE customer per invoice (`billing_rules.invoice_customer_id`).
        pname, cname = inv_nm.get(inv.id, (None, None))
        pname, cname = pname or "No project", cname or "No customer"
        who = emp_of_sheet.get(inv.timesheet_id or 0)
        subtitle = f"{pname}{' · ' + who if who else ''}"
        if inv.kind == proforma:
            age = _age_days(inv.invoice_date, today) or 0
            it = _item(f"spi:{inv.id}", f"{inv.proforma_number or inv.invoice_number} · {rupees(inv.grand_total)}",
                       subtitle, chip=f"With Finance · {age} d" if age else "With Finance",
                       tone="warn" if age > PROFORMA_WAIT_DAYS else "info", when=_when(inv.invoice_date),
                       path=f"invoices/{inv.id}", action="Open Proforma")
            it.update(section=cname, _order=-age, status="With Finance", stage="proforma",
                      **facets_of(inv, cname, pname))
            pi_items.append(it)
            continue
        # 27 Oct 2026: the invoice's STAGE (the Invoices tabs) refined by its
        # status — never `payment_status`, which read "Unpaid" for an invoice
        # still waiting for the customer or its IRN.
        stage, status, late = inv_state[inv.id]
        fields = _invoice_status_fields(stage, status, late)
        it = _item(f"siv:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}", subtitle,
                   chip=fields["chip"], tone=fields["tone"],
                   when=_when(inv.invoice_date), path=f"invoices/{inv.id}", action="Open invoice")
        it.update(section=cname, status=fields["status"], stage=stage, status_key=status,
                  **facets_of(inv, cname, pname))
        iv_items.append(it)
        # Payments to chase: money ours to collect (Unpaid / Partially paid —
        # payment is refused before approval + IRN), past the EFFECTIVE due date
        # (own, else invoice date + 30), in this user's own scope.
        if (stage in COLLECTABLE_STAGES and status == "overdue" and float(inv.balance_amount or 0) > 0
                and (everyone or inv.project_id in projects)):
            from services.billing_rules import effective_due
            it = _item(f"sdue:{inv.id}", f"{inv.invoice_number} · {rupees(inv.balance_amount)} due",
                       subtitle, chip=f"{late} day{'s' if late != 1 else ''} overdue",
                       tone="bad" if late > 30 else "warn",
                       when=_when(effective_due(inv.due_date, inv.invoice_date)),
                       path=f"invoices/{inv.id}", action="Open invoice")
            it.update(section=cname, _order=-late, status=fields["status"], stage=stage,
                      **facets_of(inv, cname, pname))
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
        period = " → ".join(d.strftime("%d %b %Y") for d in (po.start_date, po.end_date) if d)
        it = _item(f"{prefix}po:{po.id}", f"PO {po.po_number} · {rupees(po.balance_value)} left",
                   f"Purchase order · {period}" if period else "Purchase order",
                   chip=chip, tone="bad" if days <= 7 else "warn" if days <= 21 else "info",
                   when=po.end_date.isoformat(), path=f"pos/{po.id}", action="Open PO")
        it.update(section=cname, **_facets(cname, None, day=po.end_date),
                  po_start=po.start_date.isoformat() if po.start_date else None,
                  po_end=po.end_date.isoformat())
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
    from services.billing_rules import work_month_clause

    ts_raw = []
    for ts, emp in db.execute(
            select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
            .where(Timesheet.status.in_([TimesheetStatus.DRAFT, TimesheetStatus.REJECTED]),
                   Timesheet.year * 12 + Timesheet.month < this_month,
                   # 27 Oct 2026: from the data start month, like Sales' own tab.
                   work_month_clause(Timesheet.year, Timesheet.month))
            .order_by(Timesheet.year, Timesheet.month)).all():
        pname, cname, owner = proj.get(ts.project_id, ("Project", "No customer", None))
        month_end = date(ts.year + (ts.month // 12), ts.month % 12 + 1, 1) - timedelta(days=1)
        late = (today - month_end).days
        rejected = getattr(ts.status, "value", ts.status) == TimesheetStatus.REJECTED.value
        it = _item(f"tts:{ts.id}", f"{_employee_name(emp)} · {_sheet_month(ts)}",
                   f"{cname} · {pname}",
                   chip=("Sent back · " if rejected else "Not submitted · ") + f"{late} d late",
                   tone="bad" if late > SHEET_LATE_DAYS else "warn", when=_when(ts.updated_at),
                   path=f"timesheets/{ts.id}", action="Open timesheet")
        it.update(status="Sent back" if rejected else "Not submitted",
                  **_facets(cname, pname, ts.year, ts.month, employee=_employee_name(emp)))
        ts_raw.append((owner, -late, it))
    # 3 — overdue collections: `collectable_overdue` (27 Oct 2026) — Unpaid /
    # Partially paid, past the effective due date, inside the data window.
    from services.billing_rules import days_late as _late, effective_due
    due_raw = []
    due_invs = collectable_overdue(db)
    due_nm = _invoice_names(db, due_invs)
    for inv in due_invs:
        pname, _pc, owner = proj.get(inv.project_id, ("Project", "No customer", None))
        cname = due_nm.get(inv.id, (None, None))[1] or "No customer"
        late = _late(inv.due_date, inv.invoice_date, today)
        it = _item(f"tdue:{inv.id}", f"{inv.invoice_number} · {rupees(inv.balance_amount)} due",
                   f"{cname} · {pname}", chip=f"{late} day{'s' if late != 1 else ''} overdue",
                   tone="bad" if late > 30 else "warn",
                   when=_when(effective_due(inv.due_date, inv.invoice_date)),
                   path=f"invoices/{inv.id}", action="Open invoice")
        it.update(status=_collect_stage(inv), **_facets(cname, pname, day=inv.invoice_date, amount=inv.balance_amount))
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
    # 27 Oct 2026: `collectable_overdue` (the ONE collections rule), biggest
    # balance first, and the WHOLE list — My Tasks filters it; the tile's hint
    # names the top ones. It was cut to 10 on the server.
    from services.billing_rules import days_late as _late, effective_due
    due = []
    due_invs = sorted(collectable_overdue(db), key=lambda i: -float(i.balance_amount or 0))
    due_nm = _invoice_names(db, due_invs)
    for inv in due_invs:
        pname, cname = due_nm.get(inv.id, (None, None))
        cname = cname or "No customer"
        late = _late(inv.due_date, inv.invoice_date, today)
        it = _item(f"hdue:{inv.id}", f"{cname} · {rupees(inv.balance_amount)}",
                   f"{inv.invoice_number} · {pname or ''}", chip=f"{late} day{'s' if late != 1 else ''} overdue",
                   tone="bad" if late > 30 else "warn",
                   when=_when(effective_due(inv.due_date, inv.invoice_date)),
                   path=f"invoices/{inv.id}", action="Open invoice")
        it.update(status=_collect_stage(inv), top=len(due) < HEAD_TOP_COLLECTIONS,
                  **_facets(cname, pname, day=inv.invoice_date, amount=inv.balance_amount))
        due.append(it)
    out.append(_tab("head_collections", due))

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
#: 20 Oct 2026: 180 (was 90) — an approved sheet waiting three months for its
#: Proforma was dropping off Finance's list; the Dashboard's billing tracker
#: (`services/billing_tracker`) lists every one with no window at all.
FIN_TIMESHEET_DAYS = 180
#: A Proforma waiting longer than this is flagged amber, twice as long red.
PROFORMA_WAIT_DAYS = 3


def rupees(value) -> str:
    """₹ with Indian grouping, no paise (a tile, not an invoice)."""
    from services.tax_invoice import format_inr

    return "₹" + format_inr(round(float(value or 0))).replace("INR ", "").rsplit(".", 1)[0]


def _month_label(year: int, month: int) -> str:
    from calendar import month_abbr

    return f"{month_abbr[month]} {year}"


def _sheet_month(ts) -> str:
    """"May 2026", or "May 2026 (21 Apr – 30 May 2026)" for a sheet whose
    period crosses a month (20 Oct 2026, user ask: "see which month a
    timesheet is without opening it")."""
    from services.billing_tracker import period_label

    label = _month_label(ts.year, ts.month)
    period = period_label(ts.year, ts.month, getattr(ts, "period_start_date", None),
                          getattr(ts, "period_end_date", None))
    return f"{label} ({period})" if period else label


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
    * `issued` — every tax invoice, newest first (8 Oct 2026).

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
            f"{key}:{ts.id}", f"{_employee_name(emp)} · {_sheet_month(ts)}",
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

    from services.billing_rules import work_month_clause as _wmc

    # 0 — submitted sheets: the approver's move (oldest first — they have waited longest).
    # 27 Oct 2026: from the data start work month, like every other billing list.
    submitted = db.execute(
        select(Timesheet, Employee).outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.SUBMITTED, _wmc(Timesheet.year, Timesheet.month))
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
    # 23 Oct 2026: windowed by the WORK month (Settings ▸ Billing ▸ Data starts
    # from), the same rule as the billing tracker and the lists — not by "approved
    # in the last 180 days", which made the tile and the list disagree.
    from services.billing_rules import invoice_window_clause, work_month_clause

    sheets = db.execute(
        select(Timesheet, Employee)
        .outerjoin(Employee, Employee.id == Timesheet.employee_id)
        .where(Timesheet.status == TimesheetStatus.APPROVED,
               work_month_clause(Timesheet.year, Timesheet.month))
        .order_by(func.coalesce(Timesheet.approved_at, Timesheet.updated_at).desc())
    ).all()
    sdocs = _sheet_docs(db, [ts.id for ts, _ in sheets])
    states = {ts.id: _sheet_bill_state(ts, sdocs.get(ts.id) or []) for ts, _ in sheets}
    waiting = [(ts, emp) for ts, emp in sheets if states.get(ts.id) is not None]
    nm = names({ts.project_id for ts, _ in waiting})
    ts_items = []
    for ts, emp in waiting:
        state = states[ts.id]
        chip, tone, action = words["returned" if state[0] == "returned" else "awaiting"]
        if state[0] == "partial":
            # 0141 — a sheet split across POs with a part still to raise.
            chip, tone = f"Part billed — {rupees(state[1])} still to bill", "warn"
        ts_items.append(sheet_item("ts", ts, emp, nm, chip, tone, action, ts.approved_at or ts.updated_at))

    # 2 — Proformas raised and not returned: Finance's move.
    pis = db.execute(select(Invoice).where(Invoice.kind == proforma, Invoice.returned_at.is_(None),
                                           invoice_window_clause())
                     .order_by(Invoice.id)).scalars().all()
    nm = _invoice_names(db, pis)
    emp_of = employees_of(pis)
    pi_chip, pi_action = words["proforma"]
    pi_items = []
    for inv in pis:
        pname, cname = nm.get(inv.id, ("Project", None))
        who = emp_of.get(inv.timesheet_id or 0)
        age = _age_days(inv.invoice_date, today) or 0
        tone = "bad" if age > 2 * PROFORMA_WAIT_DAYS else "warn" if age > PROFORMA_WAIT_DAYS else "info"
        pi_items.append(_item(
            f"pi:{inv.id}", f"{inv.proforma_number or inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, who) if x) + (f" · waiting {age} day{'s' if age != 1 else ''}" if age else ""),
            chip=pi_chip, tone=tone, when=_when(inv.invoice_date),
            path=f"invoices/{inv.id}", action=pi_action) | {"section": cname or "No customer"}
            | _facets(cname, pname, day=inv.invoice_date, employee=who, amount=inv.grand_total))

    # 3 — EVERY tax invoice issued (done), newest first. 8 Oct 2026, user
    # report: "Tax invoices issued is not showing all invoices" — the old
    # 30-day window hid the older ones; the tab's customer / employee / month
    # filters narrow it instead.
    issued = db.execute(select(Invoice).where(Invoice.kind == tax, invoice_window_clause())
                        .order_by(Invoice.invoice_date.desc(), Invoice.id.desc())).scalars().all()
    nm = _invoice_names(db, issued)
    emp_of = employees_of(issued)
    # 27 Oct 2026: the chip is the invoice's STAGE (the Invoices tabs) refined by
    # its status — it printed `payment_status`, so awaiting customer, pending
    # e-invoice and unpaid all read "Unpaid" and filtered as one.
    states = invoice_states(db, issued, today)
    iv_items = []
    for inv in issued:
        pname, cname = nm.get(inv.id, ("Project", None))
        who = emp_of.get(inv.timesheet_id or 0)
        from_pi = f"from {inv.proforma_number}" if inv.proforma_number else None
        fields = _invoice_status_fields(*states[inv.id])
        iv_items.append(_item(
            f"iv:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, who, from_pi) if x),
            chip=fields["chip"], tone=fields["tone"],
            when=_when(inv.invoice_date), path=f"invoices/{inv.id}", action="Open invoice")
            | {"section": cname or "No customer", "status": fields["status"], "stage": fields["stage"],
               "status_key": fields["status_key"]}
            | _facets(cname, pname, day=inv.invoice_date, employee=who, amount=inv.grand_total))

    return {"submitted": sub_items, "awaiting": ts_items, "proformas": pi_items, "issued": iv_items}


def _finance_tabs(db: Session) -> list[dict]:
    """Finance's desk, in the order the money moves (29 Sep 2026; 23 Oct 2026
    user ask: "Pending e-invoices, Pending record payment and whatever else
    Finance needs daily"): approved sheets → Proformas to convert → awaiting
    the customer → e-invoices to add → payments to record → TDS to collect →
    receipts to map → every tax invoice issued. Each tab is built in its own
    savepoint, so one failing never blanks the others."""
    import logging

    def safe(fn):
        try:
            with db.begin_nested():
                return fn()
        except Exception:
            logging.getLogger("karnex.crm.work_desk").warning("finance tab failed", exc_info=True)
            return None

    chain = safe(lambda: billing_chain(db, audience="finance")) or {}
    tabs = [
        _tab("fin_timesheets", chain["awaiting"]) if "awaiting" in chain else None,
        _tab("fin_proformas", chain["proformas"]) if "proformas" in chain else None,
        safe(lambda: _awaiting_customer_tab(db, "fin_awaiting_customer")),
        safe(lambda: _customer_approved_tab(db)),
        safe(lambda: _payment_pending_tab(db)),
        safe(lambda: _partially_paid_tab(db)),
        safe(lambda: _tds_pending_tab(db)),
        safe(lambda: _receipts_to_map_tab(db)),
        _tab("fin_invoices", chain["issued"]) if "issued" in chain else None,
    ]
    return [t for t in tabs if t is not None]


#: "Pending e-invoices" keeps an invoice whose IRN is in for this long (done section).
FIN_IRN_DONE_DAYS = 30
#: An invoice waiting longer than this for the customer's approval turns amber, twice as long red.
CONFIRM_WAIT_DAYS = 7
#: Credit terms when an invoice has no due date — the Revenue report's rule.
DEFAULT_TERMS_DAYS = 30


def _awaiting_customer_tab(db: Session, key: str) -> dict:
    """Tax invoices still waiting for the customer's approval — `inv_confirm`
    for the Sales Manager / Sales Head (their move) and `fin_awaiting_customer`
    for Finance (information). 23 Oct 2026, user report: the Invoices list read
    101 while the Sales Manager's tile read 25 — the tile had its own 90-day
    window. Both now use `billing_rules.awaiting_customer_clause()`."""
    from datetime import date

    from models import Invoice
    from services.billing_rules import awaiting_customer_clause

    today = date.today()
    rows = db.execute(select(Invoice).where(awaiting_customer_clause())
                      .order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).scalars().all()
    nm = _invoice_names(db, rows)
    confirm = key == "inv_confirm"
    items = []
    for inv in rows:
        pname, cname = nm.get(inv.id, (None, None))
        age = _age_days(inv.invoice_date, today) or 0
        items.append(_item(
            f"confirm:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname) if x),
            chip=f"Issued {age} d ago" if age else "Issued today",
            tone="bad" if age > 2 * CONFIRM_WAIT_DAYS else "warn" if age > CONFIRM_WAIT_DAYS else "info",
            when=_when(inv.invoice_date), path=f"invoices/{inv.id}",
            action="Confirm customer approval" if confirm else "Open invoice")
            | {"section": cname or "No customer", "status": "Awaiting customer", "stage": "awaiting_customer"}
            | _facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))
    return _tab(key, items)


def _effective_due(inv):
    from datetime import timedelta

    return inv.due_date or (inv.invoice_date + timedelta(days=DEFAULT_TERMS_DAYS))


def _payment_pending_tab(db: Session) -> dict:
    """UNPAID tax invoices (24 Oct 2026: the `unpaid` stage — IRN in, nothing
    received yet). Finance records the payment / TDS here. Overdue first."""
    return _open_balance_tab(db, "unpaid", "fin_payment_pending", "pay", "Record payment")


def _partially_paid_tab(db: Session) -> dict:
    """PARTIALLY PAID tax invoices — part received (cash or TDS), the balance
    still due from the customer (24 Oct 2026, user ask). Mapping the customer's
    next payment advice to the balance clears it."""
    return _open_balance_tab(db, "partially_paid", "fin_partially_paid", "bal", "Collect balance")


def _open_balance_tab(db: Session, stage: str, key: str, prefix: str, action: str) -> dict:
    """One Finance tile over one invoice stage (`billing_rules.invoice_stage_case`
    — the Invoices tabs use the same rule), overdue first by days late."""
    from datetime import date

    from models import Invoice
    from services.billing_rules import invoice_stage_case, invoice_window_clause

    today = date.today()
    rows = db.execute(select(Invoice).where(
        invoice_stage_case() == stage, Invoice.balance_amount > 0,
        invoice_window_clause())).scalars().all()
    rows.sort(key=lambda i: (_effective_due(i), i.id))
    nm = _invoice_names(db, rows)
    items = []
    for inv in rows:
        pname, cname = nm.get(inv.id, (None, None))
        late = (today - _effective_due(inv)).days
        chip = f"{late} d late" if late > 0 else ("Due today" if late == 0 else f"Due in {-late} d")
        tone = "bad" if late > 30 else "warn" if late > 0 else "info"
        received = float(inv.paid_amount or 0) + float(getattr(inv, "tds_deducted", 0) or 0)
        sub = [cname, pname, f"total {rupees(inv.grand_total)}"]
        if received > 0:
            sub.append(f"received {rupees(received)}")
        items.append(_item(
            f"{prefix}:{inv.id}", f"{inv.invoice_number} · {rupees(inv.balance_amount)} due",
            " · ".join(x for x in sub if x),
            chip=chip, tone=tone, when=_when(_effective_due(inv)), path=f"invoices/{inv.id}",
            action=action)
            | {"section": "Overdue" if late > 0 else "Not yet due",
               "status": "Overdue" if late > 0 else "Not yet due", "stage": stage}
            | _facets(cname, pname, day=inv.invoice_date, amount=inv.balance_amount))
    return _tab(key, items)


def _tds_pending_tab(db: Session) -> dict:
    """TDS the customer withheld and has not yet paid over (Form 16A pending)."""
    from datetime import date

    from models import Invoice
    from models.finance import TdsRecord, TdsStatus
    from services.billing_rules import invoice_window_clause

    today = date.today()
    rows = db.execute(select(TdsRecord, Invoice).join(Invoice, Invoice.id == TdsRecord.invoice_id).where(
        TdsRecord.tds_status.in_([TdsStatus.PENDING, TdsStatus.PARTIALLY_PAID]),
        invoice_window_clause()).order_by(Invoice.invoice_date.asc(), Invoice.id.asc())).all()
    nm = _invoice_names(db, [inv for _, inv in rows])
    items = []
    for rec, inv in rows:
        pname, cname = nm.get(inv.id, (None, None))
        since = rec.created_at.date() if getattr(rec, "created_at", None) else inv.invoice_date
        days = (today - since).days
        owed = rec.tds_balance if rec.tds_balance is not None else rec.tds_amount
        items.append(_item(
            f"tds:{rec.id}", f"{inv.invoice_number} · TDS {rupees(owed)}",
            " · ".join(x for x in (cname, pname, rec.section) if x),
            chip=f"Pending {days} d" if days else "Pending", tone="bad" if days > 90 else "warn" if days > 30 else "info",
            when=_when(since), path=f"invoices/{inv.id}", action="Mark received")
            | {"section": cname or "No customer"} | _facets(cname, pname, day=inv.invoice_date, amount=owed))
    return _tab("fin_tds_pending", items)


def _receipts_to_map_tab(db: Session) -> dict:
    """Money received but not fully placed: amount still on account (map it to
    invoices) or no branch named (say which branch paid). 23 Oct 2026."""
    from sqlalchemy import or_

    from models import Customer
    from models.finance import CustomerReceipt

    rows = db.execute(select(CustomerReceipt, Customer.name)
                      .outerjoin(Customer, Customer.id == CustomerReceipt.customer_id)
                      .where(or_(CustomerReceipt.unallocated_amount > 0, CustomerReceipt.branch_id.is_(None)))
                      .order_by(CustomerReceipt.received_date.asc(), CustomerReceipt.id.asc())).all()
    items = []
    for rc, cname in rows:
        on_account = float(rc.unallocated_amount or 0) > 0
        no_branch = rc.branch_id is None
        bits = []
        if on_account:
            bits.append(f"{rupees(rc.unallocated_amount)} on account")
        if no_branch:
            bits.append("no branch")
        items.append(_item(
            f"rcpt:{rc.id}", f"{rupees(rc.amount)} received · {rc.received_date.strftime('%d %b %Y')}",
            " · ".join(x for x in (cname, rc.reference_number, rc.payment_mode) if x),
            chip=" · ".join(bits), tone="warn",
            when=_when(rc.received_date), path=f"received-amounts?receipt={rc.id}",
            action="Map invoices" if on_account else "Set branch")
            | {"section": "Not mapped to invoices" if on_account else "Branch missing"}
            | _facets(cname, None, day=rc.received_date, amount=rc.amount))
    return _tab("fin_receipts", items)


def _invoice_names(db: Session, invoices) -> dict[int, tuple[str | None, str | None]]:
    """INVOICE id → (project name, customer name) — three small queries.

    27 Oct 2026: the customer is `billing_rules.invoice_customer_id` (the PO's,
    else the project's) — the Invoices list's and the Billing Tracker's rule —
    so an invoice carries the same customer on every screen and in every
    customer filter. It used to be keyed by project, i.e. the project's customer."""
    from models import Customer, Project
    from models.finance import PurchaseOrder
    from services.billing_rules import invoice_customer_id

    invoices = list(invoices)
    pids = {i.project_id for i in invoices if i.project_id}
    po_ids = {i.po_id for i in invoices if getattr(i, "po_id", None)}
    proj = {pid: (pn, cid) for pid, pn, cid in db.execute(
        select(Project.id, Project.name, Project.customer_id).where(Project.id.in_(pids))).all()} if pids else {}
    po_cust = dict(db.execute(select(PurchaseOrder.id, PurchaseOrder.customer_id)
                              .where(PurchaseOrder.id.in_(po_ids))).all()) if po_ids else {}
    out: dict[int, tuple] = {}
    cids: set[int] = set()
    for inv in invoices:
        pname, pcid = proj.get(inv.project_id, (None, None))
        cid = invoice_customer_id(pcid, po_cust.get(getattr(inv, "po_id", None)))
        out[inv.id] = (pname, cid)
        if cid:
            cids.add(cid)
    names = dict(db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(cids))).all()) if cids else {}
    return {iid: (pname, names.get(cid)) for iid, (pname, cid) in out.items()}


def invoice_states(db: Session, invoices, today=None) -> dict[int, tuple[str, str, int]]:
    """invoice id → (stage, status, days late) — `billing_rules`' ONE stage and
    status rule (the Invoices tabs and Status filter), with TDS pending read in
    one query. 27 Oct 2026: every desk chip on an invoice comes from here."""
    from datetime import date

    from models.finance import TdsRecord, TdsStatus
    from services import billing_rules

    today = today or date.today()
    invoices = list(invoices)
    ids = [i.id for i in invoices]
    tds_open = set(db.execute(select(TdsRecord.invoice_id).where(
        TdsRecord.invoice_id.in_(ids), TdsRecord.tds_status != TdsStatus.PAID)).scalars()) if ids else set()
    out = {}
    for inv in invoices:
        stage = billing_rules.stage_of_invoice(inv)
        status = billing_rules.invoice_status(stage=stage, returned=bool(inv.returned_at),
                                              tds_pending=inv.id in tds_open, due=inv.due_date,
                                              invoice_date=inv.invoice_date, today=today)
        out[inv.id] = (stage, status, billing_rules.days_late(inv.due_date, inv.invoice_date, today))
    return out


def _collect_stage(inv) -> str:
    """The stage words of an invoice on a collections list (every one there is
    overdue): "Partially paid" once any cash or TDS is in, else "Unpaid" — the
    Invoices tabs' words, so the Status filter splits them the same way."""
    money_in = float(getattr(inv, "paid_amount", 0) or 0) + float(getattr(inv, "tds_deducted", 0) or 0)
    return "Partially paid" if money_in > 0 else "Unpaid"


def _invoice_status_fields(stage: str, status: str, days: int) -> dict:
    """The chip, tone and FILTER status of an issued invoice item: the stage
    words (Awaiting customer · Pending e-invoice · Unpaid · Partially paid ·
    Paid) — the Invoices tabs' — refined by overdue / due soon."""
    from services import billing_rules

    chip = billing_rules.invoice_chip(stage, status, days if status == "overdue" else 0)
    tone = ("ok" if stage == "paid" and status == "paid" else "warn" if stage == "paid"
            else "bad" if status == "overdue" else "warn" if status == "due_soon" else "info")
    return {"chip": chip, "tone": tone, "status": billing_rules.STAGE_LABELS.get(stage, stage),
            "stage": stage, "status_key": status}


def collectable_overdue(db: Session, *, project_ids=None):
    """Tax invoices whose money is ours to chase and late: stage Unpaid /
    Partially paid (payments are refused before the customer's approval and the
    IRN), a balance left, past the EFFECTIVE due date (own, else invoice date +
    30), inside the data window. 27 Oct 2026: the ONE rule of every
    "collections" tab (Sales, Sales Manager team, Sales Head)."""
    from models import Invoice
    from services.billing_rules import (
        COLLECTABLE_STAGES, invoice_stage_case, invoice_window_clause, overdue_clause)

    q = select(Invoice).where(invoice_stage_case().in_(COLLECTABLE_STAGES),
                              func.coalesce(Invoice.balance_amount, 0) > 0,
                              overdue_clause(), invoice_window_clause())
    if project_ids is not None:
        q = q.where(Invoice.project_id.in_(list(project_ids) or [-1]))
    return db.execute(q).scalars().all()


def _customer_approved_tab(db: Session) -> dict:
    """Finance (5 Oct 2026): every customer-approved tax invoice still without
    an IRN (oldest approval first — the count), then those whose IRN went in
    during the last `FIN_IRN_DONE_DAYS` (done)."""
    from datetime import timedelta

    from models import Invoice
    from models.finance import InvoiceKind
    from services.billing_rules import invoice_window_clause

    from services.billing_rules import invoice_stage_case

    since = datetime.now(timezone.utc) - timedelta(days=FIN_IRN_DONE_DAYS)
    # 27 Oct 2026: the `pending_einvoice` STAGE — the Invoices "Pending e-invoice"
    # tab, item for item. An approved invoice without an IRN that already has
    # money on it is in Partially paid / Paid (its own tile), never here as well.
    pending = db.execute(select(Invoice).where(
        invoice_stage_case() == "pending_einvoice", invoice_window_clause())
        .order_by(Invoice.customer_approved_at.asc())).scalars().all()
    done = db.execute(select(Invoice).where(
        Invoice.kind == InvoiceKind.TAX.value, Invoice.customer_approved_at.isnot(None),
        Invoice.irn_number.isnot(None), Invoice.irn_recorded_at >= since, invoice_window_clause())
        .order_by(Invoice.irn_recorded_at.desc())).scalars().all()
    nm = _invoice_names(db, [*pending, *done])
    now = datetime.now(timezone.utc)
    items = []
    for inv in pending:
        pname, cname = nm.get(inv.id, (None, None))
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
        pname, cname = nm.get(inv.id, (None, None))
        items.append(_item(
            f"irn:{inv.id}", f"{inv.invoice_number} · {rupees(inv.grand_total)}",
            " · ".join(x for x in (cname, pname, f"Ack {inv.ack_number}" if inv.ack_number else None) if x),
            chip="IRN recorded", tone="ok", when=_when(inv.irn_recorded_at),
            path=f"invoices/{inv.id}", action="Open invoice")
            | {"section": "IRN recorded"} | _facets(cname, pname, day=inv.invoice_date, amount=inv.grand_total))
    return _tab("fin_customer_approved", items, count=len(pending))


def _customer_confirm_tab(db: Session) -> dict:
    """Sales Manager / Sales Head (5 Oct 2026): every original invoice whose
    customer approval is not confirmed yet — the Invoices ▸ Awaiting customer
    list, item for item (23 Oct 2026: no own window any more)."""
    return _awaiting_customer_tab(db, "inv_confirm")


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
    from services import hiring_managers
    from services.panel_interviews import is_panel_only
    if hiring_managers.is_hiring_manager(user):
        builders.insert(1, ("hiring_manager", lambda: _hm_tabs(db, user)))
    if hiring_managers.hm_scoped(user, db):
        # A Hiring Manager's day is their positions' decisions and their own
        # interviews (22 Oct 2026) — no company-wide Upcoming / queues.
        builders = [("hiring_manager", lambda: _hm_tabs(db, user)),
                    ("panel", lambda: _panel_tabs(db, user))]
    elif is_panel_only(user):
        # A login holding only custom roles (the Interviewer logins, 7 Oct
        # 2026): their day is their own rounds — nothing else is theirs. A
        # Hiring Manager among them keeps the HM tabs (23 Oct 2026: they were
        # dropped here, and the HM's Dashboard read "nothing to show").
        builders = ([("hiring_manager", lambda: _hm_tabs(db, user))]
                    if hiring_managers.is_hiring_manager(user) else [])
        builders += [("panel", lambda: _panel_tabs(db, user))]
    else:
        # Anyone linked to an Employees row and named as a panel member gets
        # their own rounds beside their role's tabs.
        builders.insert(1, ("panel", lambda: _panel_tabs(db, user)))
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
    try:
        with db.begin_nested():
            apply_marks(db, user.id, tabs)
    except Exception:
        logging.getLogger("karnex.crm.work_desk").warning("desk marks failed", exc_info=True)
    for tab in tabs:
        tab["items"] = tab["items"][:max_items]
    return {"tabs": tabs, "as_of": datetime.now(timezone.utc).isoformat()}


# ------------------------------------------------------------------ ticks (8 Oct 2026)

#: Most keys one tick request may carry (an employee group of invoices).
MAX_MARK_KEYS = 500


def marked_keys(db: Session, user_id: int) -> set[str]:
    """The item keys this login ticked as done on My Tasks."""
    from models import WorkDeskMark

    return set(db.execute(select(WorkDeskMark.item_key).where(WorkDeskMark.user_id == user_id)).scalars())


def apply_marks(db: Session, user_id: int, tabs: list[dict]) -> None:
    """Stamp `done: True` on the items this login ticked ("work done, tick &
    close" — user ask, 8 Oct 2026) and count them per tab (`done_count`). The
    item stays in the payload (My Tasks hides it by default and can show it
    again); the tile COUNT is unchanged — a tick is personal bookkeeping, not a
    change to the record."""
    marks = marked_keys(db, user_id)
    for tab in tabs:
        n = 0
        for it in tab.get("items") or []:
            if it.get("key") in marks:
                it["done"] = True
                n += 1
        tab["done_count"] = n


def set_marks(db: Session, user_id: int, keys: list[str], done: bool) -> int:
    """Tick (done=True) or untick a batch of item keys for this login.
    Idempotent; returns how many keys changed."""
    from models import WorkDeskMark

    clean = sorted({str(k).strip()[:80] for k in keys if str(k or "").strip()})[:MAX_MARK_KEYS]
    if not clean:
        return 0
    have = set(db.execute(select(WorkDeskMark.item_key).where(
        WorkDeskMark.user_id == user_id, WorkDeskMark.item_key.in_(clean))).scalars())
    if done:
        new = [k for k in clean if k not in have]
        db.add_all([WorkDeskMark(user_id=user_id, item_key=k) for k in new])
        return len(new)
    gone = [k for k in clean if k in have]
    if gone:
        db.execute(WorkDeskMark.__table__.delete().where(
            WorkDeskMark.user_id == user_id, WorkDeskMark.item_key.in_(gone)))
    return len(gone)


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
    _fill_billing_facets(db, tabs)
    for tab in tabs:
        for it in tab["items"]:
            if not it.get("customer"):
                it["customer"] = names.get(it.get("profile_id")) or None
            if not it.get("month") and isinstance(it.get("when"), str) and len(it["when"]) >= 7:
                it["month"] = it["when"][:7]


def _fill_billing_facets(db: Session, tabs: list[dict]) -> None:
    """16 Oct 2026 (user ask): every invoice / timesheet item says its customer
    BRANCH, and an invoice the MONTH it bills (the timesheet period — not the
    day it was raised) and its employee, so Finance / Sales filter by branch,
    employee and month without opening anything. Two queries for the desk."""
    import re

    from models import CustomerBranch, Invoice, Project, Timesheet
    from services.invoice_facets import invoice_facets

    inv_ids, ts_ids = set(), set()
    for tab in tabs:
        for it in tab["items"]:
            path = it.get("path") or ""
            if m := re.fullmatch(r"invoices/(\d+)", path):
                inv_ids.add(int(m.group(1)))
            elif m := re.fullmatch(r"timesheets/(\d+)", path):
                ts_ids.add(int(m.group(1)))
    if not inv_ids and not ts_ids:
        return
    try:
        with db.begin_nested():
            invs = db.execute(select(Invoice).where(Invoice.id.in_(inv_ids))).scalars().all() if inv_ids else []
            facets = invoice_facets(db, invs)
            ts_branch = dict(db.execute(
                select(Timesheet.id, CustomerBranch.branch_name)
                .join(Project, Project.id == Timesheet.project_id)
                .join(CustomerBranch, CustomerBranch.id == Project.branch_id)
                .where(Timesheet.id.in_(ts_ids))).all()) if ts_ids else {}
    except Exception:  # noqa: BLE001 — filter aids only
        import logging
        logging.getLogger("karnex.crm.work_desk").warning("work desk billing facets failed", exc_info=True)
        return
    for tab in tabs:
        for it in tab["items"]:
            path = it.get("path") or ""
            if m := re.fullmatch(r"invoices/(\d+)", path):
                f = facets.get(int(m.group(1))) or {}
                if f.get("branch_name"):
                    it["branch"] = f["branch_name"]
                if f.get("month"):
                    it["month"] = f["month"]
                    it["for_month"] = f["month_label"]
                if f.get("employee") and not it.get("employee"):
                    it["employee"] = f["employee"]
                it["invoice_id"] = int(m.group(1))
            elif m := re.fullmatch(r"timesheets/(\d+)", path):
                if ts_branch.get(int(m.group(1))):
                    it["branch"] = ts_branch[int(m.group(1))]


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


def _sheet_docs(db: Session, ids: list[int]) -> dict[int, list]:
    """Every invoice per sheet (0141: a split sheet has one per part) — one query."""
    from models import Invoice
    out: dict[int, list] = {}
    if not ids:
        return out
    for inv in db.execute(select(Invoice).where(Invoice.timesheet_id.in_(ids))
                          .order_by(Invoice.id)).scalars():
        out.setdefault(inv.timesheet_id, []).append(inv)
    return out


def _sheet_bill_state(ts, docs: list) -> tuple[str, float] | None:
    """('new', 0) no document yet · ('returned', 0) a Proforma came back ·
    ('partial', left) split parts leave money to bill · None = nothing to raise."""
    from services.timesheets import billing_progress
    if not docs:
        return ("new", 0.0)
    prog = billing_progress(None, ts, docs)
    if prog.get("returned"):
        return ("returned", 0.0)
    left = prog.get("remaining")
    if prog.get("split") and left is not None and left > 0.01:
        return ("partial", float(left))
    return None
