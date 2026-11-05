"""How busy each TA / Hiring Manager is — the hover card of the Assign dialog
(5 Nov 2026, user ask: "when RMG assigns, hovering a name shows which TA / Hiring
Manager is occupied, suggests one — not mandatory — and shows the last activity
with the 5 most recent opportunities").

`with_workload(db, options, kind, exclude)` dresses the picker's choices
(`ta_options` / `hm_options`) with a `workload` block and marks ONE
`suggested` choice. Nothing here decides anything: the suggestion is advice and
the RMG / GM may tick anyone. Batched — six queries for the whole list, never
per person — and it never raises (a broken lookup leaves the plain list).

Definitions the card prints (keep them in step with the UI hint):

* **Active positions** — positions the person is on whose requirement is not
  settled (`SETTLED_REQUIREMENT_STATUSES`: Fulfilled · Closed · Cancelled and the
  two rejections) — what they are expected to work today.
* **Live candidates** — TA: candidacies they own (`ta_owner_id`) that are neither
  closed (`REJECTED_BUCKET`) nor Joined, nor hidden. Hiring Manager: candidates on
  their positions waiting for THEIR review or sign-off.
* **Last activity** — the newest row they wrote on a candidacy or a position
  activity log, with its action words.
* **Recent** — the 5 positions they were assigned most recently (any status).
* **Suggested** — the lowest load (`load_score`), ties broken by who was given a
  position least recently, then by name; people already on the team are never
  suggested, and nothing is suggested when fewer than two can be picked.
"""
from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

RECENT_LIMIT = 5
SETTLED_REQUIREMENT_VALUES = {
    "Fulfilled", "Closed", "Cancelled", "Sales_Head_Rejected", "Engineering_Rejected",
}
#: Positions → level words. Tuned for a TA working 2–5 positions at a time.
LIGHT_MAX = 2
MODERATE_MAX = 5


def workload_level(active_positions: int) -> str:
    """light · moderate · heavy. PURE."""
    if active_positions <= LIGHT_MAX:
        return "light"
    if active_positions <= MODERATE_MAX:
        return "moderate"
    return "heavy"


def load_score(active_positions: int, live_candidates: int) -> int:
    """One number to compare people: a position weighs as much as 10 candidates. PURE."""
    return int(active_positions) * 10 + int(live_candidates)


def pick_suggestion(rows: list[dict], exclude: set[int] | None = None) -> int | None:
    """The id to suggest among `rows` (`{id, name, workload}`), or None. PURE."""
    exclude = exclude or set()
    pool = [r for r in rows if int(r["id"]) not in exclude and r.get("workload")]
    if len(pool) < 2:
        return None

    def key(r):
        w = r["workload"]
        last = w.get("last_assigned_at") or ""
        return (load_score(w.get("active_positions", 0), w.get("live_candidates", 0)), last, r["name"].lower())

    return int(min(pool, key=key)["id"])


def _words(action: str | None) -> str:
    if not action:
        return ""
    return action.replace("_", " ").strip().capitalize()


def _iso(v) -> str | None:
    return v.isoformat() if v is not None and hasattr(v, "isoformat") else None


def _last_activity(db: Session, model, ids: list[int]) -> dict[int, tuple]:
    """`{user_id: (timestamp, action_type)}` — the newest row each wrote."""
    sub = (select(model.user_id.label("uid"), sa.func.max(model.timestamp).label("m"))
           .where(model.user_id.in_(ids)).group_by(model.user_id).subquery())
    rows = db.execute(
        select(model.user_id, model.action_type, model.timestamp)
        .join(sub, sa.and_(model.user_id == sub.c.uid, model.timestamp == sub.c.m))
    ).all()
    return {int(u): (ts, act) for u, act, ts in rows}


def workload_for(db: Session, kind: str, ids: list[int]) -> dict[int, dict]:
    """`{user_id: workload}` for TAs (`kind="ta"`) or Hiring Managers (`"hm"`)."""
    from models import (CandidateProfile, CandidateProfileActivityLog, Customer, Opportunity,
                        PipelineStatus, Requirement, RequirementActivityLog, RequirementHiringManager,
                        RequirementTaAssignment)
    from services.candidate_profiles import REJECTED_BUCKET

    ids = sorted({int(i) for i in ids if i})
    if not ids:
        return {}
    team = RequirementHiringManager if kind == "hm" else RequirementTaAssignment

    rows = db.execute(
        select(team.user_id, team.assigned_at, Requirement.id, Requirement.req_number, Requirement.title,
               Requirement.status, Requirement.no_of_positions, Requirement.opportunity_id,
               Opportunity.opp_id, Opportunity.title, Customer.name)
        .join(Requirement, Requirement.id == team.requirement_id)
        .join(Opportunity, Opportunity.id == Requirement.opportunity_id, isouter=True)
        .join(Customer, Customer.id == Requirement.customer_id, isouter=True)
        .where(team.user_id.in_(ids))
        .order_by(team.assigned_at.desc(), team.id.desc())
    ).all()

    out: dict[int, dict] = {i: {"active_positions": 0, "open_headcount": 0, "live_candidates": 0,
                                "last_assigned_at": None, "last_activity_at": None, "last_activity": "",
                                "recent": []} for i in ids}
    my_opps: dict[int, set[int]] = {i: set() for i in ids}
    for uid, at, rid, rno, rtitle, status, npos, oid, opp_no, opp_title, cust in rows:
        w = out[int(uid)]
        sv = getattr(status, "value", status)
        if w["last_assigned_at"] is None:
            w["last_assigned_at"] = _iso(at)
        if sv not in SETTLED_REQUIREMENT_VALUES:
            w["active_positions"] += 1
            w["open_headcount"] += int(npos or 0)
            if oid:
                my_opps[int(uid)].add(int(oid))
        if len(w["recent"]) < RECENT_LIMIT:
            w["recent"].append({
                "requirement_id": int(rid), "req_number": rno, "opportunity_id": oid,
                "opp_id": opp_no, "title": opp_title or rtitle, "customer": cust,
                "status": sv, "active": sv not in SETTLED_REQUIREMENT_VALUES, "assigned_at": _iso(at),
            })

    closed = [m for m in PipelineStatus if m.value in REJECTED_BUCKET or m.value == "Joined"]
    if kind == "hm":
        all_opps = set().union(*my_opps.values()) if my_opps else set()
        if all_opps:
            waiting = db.execute(
                select(CandidateProfile.opportunity_id, sa.func.count(CandidateProfile.id))
                .where(CandidateProfile.opportunity_id.in_(sorted(all_opps)),
                       CandidateProfile.is_hidden.is_(False),
                       sa.or_(CandidateProfile.hm_review_status == "Pending",
                              CandidateProfile.hm_final_status == "Pending"))
                .group_by(CandidateProfile.opportunity_id)
            ).all()
            per_opp = {int(o): int(n) for o, n in waiting}
            for uid, opps in my_opps.items():
                out[uid]["live_candidates"] = sum(per_opp.get(o, 0) for o in opps)
    else:
        owned = db.execute(
            select(CandidateProfile.ta_owner_id, sa.func.count(CandidateProfile.id))
            .where(CandidateProfile.ta_owner_id.in_(ids), CandidateProfile.is_hidden.is_(False),
                   CandidateProfile.pipeline_status.notin_(closed))
            .group_by(CandidateProfile.ta_owner_id)
        ).all()
        for uid, n in owned:
            out[int(uid)]["live_candidates"] = int(n)

    latest: dict[int, tuple] = {}
    for model in (CandidateProfileActivityLog, RequirementActivityLog):
        for uid, (ts, act) in _last_activity(db, model, ids).items():
            if ts is not None and (uid not in latest or ts > latest[uid][0]):
                latest[uid] = (ts, act)
    for uid, (ts, act) in latest.items():
        out[uid]["last_activity_at"] = _iso(ts)
        out[uid]["last_activity"] = _words(act)

    for w in out.values():
        w["level"] = workload_level(w["active_positions"])
        w["score"] = load_score(w["active_positions"], w["live_candidates"])
    return out


def with_workload(db: Session, options: list[dict], kind: str, exclude=None) -> list[dict]:
    """The picker's choices with `workload` + `suggested` on each. Never raises."""
    if not options:
        return options
    try:
        with db.begin_nested():
            loads = workload_for(db, kind, [o["id"] for o in options])
    except Exception:  # noqa: BLE001 — the hover card is advice; the picker must still open
        log.warning("team workload lookup failed", exc_info=True)
        return options
    rows = [{**o, "workload": loads.get(int(o["id"]))} for o in options]
    best = pick_suggestion(rows, {int(i) for i in (exclude or ())})
    for r in rows:
        r["suggested"] = best is not None and int(r["id"]) == best
    return rows
