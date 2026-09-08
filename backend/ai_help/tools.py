"""Ask AI — whitelisted READ-ONLY query tools.

`crm_data.py` already gives the assistant a snapshot of the user's data, but it
is keyword-routed and fixed: it guesses what might be relevant from the wording
of the question and packs it into the prompt before the model has seen it. That
works for "which POs expire soon" and fails for anything the guesser did not
anticipate, because the model cannot ask a second question.

These tools invert that. The model asks for exactly what it needs, by name,
after reading the question — and gets structured rows back instead of prose.

THE RULES, none of which are negotiable:

1. **SELECT only.** Every function here issues reads. There is no code path in
   this module that writes, and `run_tool` is the only entry point, so adding a
   write means editing this docstring first.
2. **The caller's roles gate every tool.** Permissions are checked here, on the
   server, against the same role names the REST API uses — never in the prompt.
   A model cannot be talked out of a check it never sees.
3. **Everything is capped.** Row limits are clamped and payloads truncated, so
   a vague question cannot drag the whole database into a prompt.
4. **Names, not identifiers.** Tools resolve ids to names before returning, so
   the model never has to invent a label for a number.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any, Callable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

logger = logging.getLogger("karnex.ai_help.tools")

#: Roles allowed to see money. Same set `crm_data.py` uses — kept identical on
#: purpose: two different answers to "may this person see revenue" is a bug
#: waiting to happen.
FINANCE_ROLES = {"Finance", "Sales_Head", "Admin", "CEO"}
#: Roles allowed to see individual employee HR data (balances, leave).
HR_ROLES = {"HR", "Admin", "CEO"}

MAX_ROWS = 25
_MAX_RESULT_CHARS = 4000


def _clamp(limit: Any, default: int = 10) -> int:
    try:
        return max(1, min(MAX_ROWS, int(limit)))
    except (TypeError, ValueError):
        return default


def _ev(value: Any) -> Any:
    """Enum -> its value, everything else unchanged."""
    return getattr(value, "value", value)


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _num(value: Any) -> float | None:
    return float(value) if value is not None else None


def _person_name(obj: Any) -> str:
    """Readable name for a Candidate or Employee.

    Neither table stores a full name — `services/employees.py::full_name` does
    the same join for Employee. Candidates have no equivalent, so both are
    handled here rather than leaving the model to render "(unnamed)" for people
    who very much have names.
    """
    if obj is None:
        return "(unnamed)"
    display = (getattr(obj, "display_name", "") or "").strip()
    if display:
        return display
    parts = [
        (getattr(obj, "first_name", "") or "").strip(),
        (getattr(obj, "middle_name", "") or "").strip(),
        (getattr(obj, "last_name", "") or "").strip(),
    ]
    return " ".join(p for p in parts if p) or "(unnamed)"


def _name_filter(model: Any, term: str):
    """Case-insensitive match across the first/last name columns."""
    like = f"%{term}%"
    return or_(model.first_name.ilike(like), model.last_name.ilike(like))


# =========================================================================
# Tool implementations. Each takes (db, args, ctx) and returns a JSON-able dict.
# =========================================================================


def _search_requirements(db: Session, args: dict, ctx: dict) -> dict:
    from models import Customer, Requirement, RequirementSkill, RequirementStatus, Skill

    stmt = select(Requirement, Customer.name).join(
        Customer, Customer.id == Requirement.customer_id, isouter=True)
    status = (args.get("status") or "").strip()
    if status:
        try:
            stmt = stmt.where(Requirement.status == RequirementStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in RequirementStatus)
            return {"error": f"Unknown status '{status}'. Valid values: {valid}"}
    if args.get("customer_name"):
        stmt = stmt.where(Customer.name.ilike(f"%{args['customer_name']}%"))
    if args.get("query"):
        term = f"%{args['query']}%"
        stmt = stmt.where(or_(Requirement.title.ilike(term),
                              Requirement.req_number.ilike(term)))
    if args.get("skill"):
        stmt = stmt.where(Requirement.id.in_(
            select(RequirementSkill.requirement_id)
            .join(Skill, Skill.id == RequirementSkill.skill_id)
            .where(Skill.name.ilike(f"%{args['skill']}%"))
        ))

    rows = db.execute(
        stmt.order_by(Requirement.id.desc()).limit(_clamp(args.get("limit")))
    ).all()
    return {"requirements": [
        {
            "req_number": r.req_number,
            # ONE id rule (18 Aug 2026): quote the opp_id to users, not REQ-xxxx.
            "opp_id": _req_opp_id(db, r),
            "title": r.title,
            "customer": customer_name,
            "status": _ev(r.status),
            "positions": r.no_of_positions,
            "work_mode": _ev(r.work_mode),
            "priority": _ev(r.priority),
            "target_closure_date": _iso(r.target_closure_date),
        }
        for r, customer_name in rows
    ]}


def _req_opp_id(db: Session, req) -> str | None:
    """The human-visible id: the parent opportunity's OPP-xxxx (None when unlinked)."""
    from models import Opportunity
    if not getattr(req, "opportunity_id", None):
        return None
    opp = db.get(Opportunity, req.opportunity_id)
    return getattr(opp, "opp_id", None)


def _find_requirement_by_number(db: Session, number: str):
    """Accept either the public OPP-xxxx id or the internal REQ-xxxx key."""
    from models import Opportunity, Requirement
    req = db.execute(
        select(Requirement).where(Requirement.req_number.ilike(number))
    ).scalars().first()
    if req is not None:
        return req
    opp = db.execute(
        select(Opportunity).where(Opportunity.opp_id.ilike(number))
    ).scalars().first()
    if opp is not None:
        return db.execute(
            select(Requirement).where(Requirement.opportunity_id == opp.id)
        ).scalars().first()
    return None


def _get_requirement(db: Session, args: dict, ctx: dict) -> dict:
    from models import (
        AtsStatus, Customer, Location, Requirement, RequirementSkill, Resume, Skill,
    )

    number = (args.get("req_number") or "").strip()
    if not number:
        return {"error": "req_number is required"}
    req = _find_requirement_by_number(db, number)
    if req is None:
        return {"error": f"No requirement numbered {number}"}

    customer = db.get(Customer, req.customer_id) if req.customer_id else None
    location = db.get(Location, req.location_id) if req.location_id else None
    skills = db.execute(
        select(Skill.name).join(RequirementSkill, RequirementSkill.skill_id == Skill.id)
        .where(RequirementSkill.requirement_id == req.id)
    ).scalars().all()
    by_status = dict(db.execute(
        select(Resume.ats_status, func.count(Resume.id))
        .where(Resume.requirement_id == req.id).group_by(Resume.ats_status)
    ).all())

    return {
        "req_number": req.req_number,
        "opp_id": _req_opp_id(db, req),
        "title": req.title,
        "customer": customer.name if customer else None,
        "status": _ev(req.status),
        "positions": req.no_of_positions,
        "experience_min": req.experience_min,
        "experience_max": req.experience_max,
        "work_mode": _ev(req.work_mode),
        "location": ", ".join(
            p for p in [getattr(location, "city", None), getattr(location, "state", None)] if p
        ) or None,
        "priority": _ev(req.priority),
        "skills": list(skills),
        "has_jd": bool((req.rmg_jd_text or "").strip()),
        "resumes": {
            "total": sum(by_status.values()),
            **{str(_ev(k)): v for k, v in by_status.items()},
        },
        "shortlisted": by_status.get(AtsStatus.SHORTLISTED, 0),
    }


def _top_resumes_for_requirement(db: Session, args: dict, ctx: dict) -> dict:
    from models import Candidate, Requirement, Resume

    number = (args.get("req_number") or "").strip()
    req = _find_requirement_by_number(db, number)
    if req is None:
        return {"error": f"No requirement numbered {number}"}

    rows = db.execute(
        select(Resume, Candidate)
        .join(Candidate, Candidate.id == Resume.candidate_id, isouter=True)
        .where(Resume.requirement_id == req.id)
        .order_by(Resume.ats_score.desc().nullslast())
        .limit(_clamp(args.get("limit"), default=8))
    ).all()
    return {"req_number": req.req_number, "candidates": [
        {
            # Resume carries its own candidate_name for applications that came
            # in before a Candidate row existed.
            "name": _person_name(candidate) if candidate else (resume.candidate_name or "(unnamed)"),
            "ats_score": _num(resume.ats_score),
            "ats_status": _ev(resume.ats_status),
            "ai_interview_status": _ev(resume.ai_interview_status),
            "experience_years": getattr(candidate, "experience_years", None),
            "notice_period": getattr(candidate, "notice_period", None),
        }
        for resume, candidate in rows
    ]}


def _search_candidate_profiles(db: Session, args: dict, ctx: dict) -> dict:
    from models import Candidate, CandidateProfile, Opportunity, PipelineStatus

    stmt = (
        select(CandidateProfile, Candidate, Opportunity.opp_id)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id, isouter=True)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id, isouter=True)
    )
    status = (args.get("pipeline_status") or "").strip()
    if status:
        try:
            stmt = stmt.where(CandidateProfile.pipeline_status == PipelineStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in PipelineStatus)
            return {"error": f"Unknown pipeline_status '{status}'. Valid values: {valid}"}
    if args.get("candidate_name"):
        stmt = stmt.where(_name_filter(Candidate, args["candidate_name"]))
    if args.get("opportunity_id"):
        stmt = stmt.where(Opportunity.opp_id.ilike(f"%{args['opportunity_id']}%"))

    rows = db.execute(
        stmt.order_by(CandidateProfile.id.desc()).limit(_clamp(args.get("limit")))
    ).all()
    return {"profiles": [
        {
            "candidate": _person_name(candidate),
            "opportunity": opp_id,
            "pipeline_status": _ev(profile.pipeline_status),
            "current_ctc": _num(profile.current_ctc),
            "expected_ctc": _num(profile.expected_ctc),
        }
        for profile, candidate, opp_id in rows
    ]}


def _pipeline_counts(db: Session, args: dict, ctx: dict) -> dict:
    """How many profiles sit at each stage — the question every standup asks."""
    from models import CandidateProfile, Opportunity

    stmt = select(CandidateProfile.pipeline_status, func.count(CandidateProfile.id))
    if args.get("opportunity_id"):
        stmt = stmt.join(
            Opportunity, Opportunity.id == CandidateProfile.opportunity_id
        ).where(Opportunity.opp_id.ilike(f"%{args['opportunity_id']}%"))
    rows = db.execute(stmt.group_by(CandidateProfile.pipeline_status)).all()
    counts = {str(_ev(status)): count for status, count in rows}
    return {"by_stage": counts, "total": sum(counts.values())}


def _search_opportunities(db: Session, args: dict, ctx: dict) -> dict:
    from models import Customer, Opportunity, PipelineStage

    stmt = select(Opportunity, Customer.name).join(
        Customer, Customer.id == Opportunity.customer_id, isouter=True)
    stage = (args.get("pipeline_stage") or "").strip()
    if stage:
        try:
            stmt = stmt.where(Opportunity.pipeline_stage == PipelineStage(stage))
        except ValueError:
            valid = ", ".join(s.value for s in PipelineStage)
            return {"error": f"Unknown pipeline_stage '{stage}'. Valid values: {valid}"}
    if args.get("customer_name"):
        stmt = stmt.where(Customer.name.ilike(f"%{args['customer_name']}%"))

    rows = db.execute(
        stmt.order_by(Opportunity.id.desc()).limit(_clamp(args.get("limit")))
    ).all()
    return {"opportunities": [
        {
            "opp_id": o.opp_id,
            "customer": customer_name,
            "type": _ev(o.opp_type),
            "stage": _ev(o.pipeline_stage),
            "approval_status": _ev(o.approval_status),
        }
        for o, customer_name in rows
    ]}


def _search_purchase_orders(db: Session, args: dict, ctx: dict) -> dict:
    from models import Customer, POStatus, PurchaseOrder

    stmt = select(PurchaseOrder, Customer.name).join(
        Customer, Customer.id == PurchaseOrder.customer_id, isouter=True)
    if args.get("customer_name"):
        stmt = stmt.where(Customer.name.ilike(f"%{args['customer_name']}%"))
    status = (args.get("status") or "").strip()
    if status:
        try:
            stmt = stmt.where(PurchaseOrder.status == POStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in POStatus)
            return {"error": f"Unknown status '{status}'. Valid values: {valid}"}
    days = args.get("expiring_within_days")
    if days is not None:
        try:
            horizon = date.today() + timedelta(days=max(0, min(365, int(days))))
        except (TypeError, ValueError):
            return {"error": "expiring_within_days must be a number of days"}
        stmt = stmt.where(PurchaseOrder.end_date.is_not(None),
                          PurchaseOrder.end_date <= horizon)

    rows = db.execute(
        stmt.order_by(PurchaseOrder.end_date.nullslast()).limit(_clamp(args.get("limit")))
    ).all()
    today = date.today()
    return {"purchase_orders": [
        {
            "po_number": po.po_number,
            "customer": customer_name,
            "status": _ev(po.status),
            "end_date": _iso(po.end_date),
            "days_left": (po.end_date - today).days if po.end_date else None,
            "total_value": _num(po.total_value),
            "consumed_value": _num(po.consumed_value),
            "balance_value": _num(po.balance_value),
        }
        for po, customer_name in rows
    ]}


def _search_invoices(db: Session, args: dict, ctx: dict) -> dict:
    from models import Customer, Invoice, PaymentStatus, PurchaseOrder

    # Invoices carry no customer_id — they hang off the PO, which does. The
    # join is outer so an invoice with no PO still lists (with a null customer)
    # instead of vanishing from a report Finance is using to chase money.
    stmt = (
        select(Invoice, Customer.name)
        .join(PurchaseOrder, PurchaseOrder.id == Invoice.po_id, isouter=True)
        .join(Customer, Customer.id == PurchaseOrder.customer_id, isouter=True)
    )
    if args.get("customer_name"):
        stmt = stmt.where(Customer.name.ilike(f"%{args['customer_name']}%"))
    status = (args.get("payment_status") or "").strip()
    if status:
        try:
            stmt = stmt.where(Invoice.payment_status == PaymentStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in PaymentStatus)
            return {"error": f"Unknown payment_status '{status}'. Valid values: {valid}"}
    if args.get("overdue_only"):
        stmt = stmt.where(Invoice.due_date.is_not(None),
                          Invoice.due_date < date.today(),
                          Invoice.payment_status != PaymentStatus.PAID)

    rows = db.execute(
        stmt.order_by(Invoice.invoice_date.desc().nullslast())
        .limit(_clamp(args.get("limit")))
    ).all()
    today = date.today()
    return {"invoices": [
        {
            "invoice_number": inv.invoice_number,
            "customer": customer_name,
            "invoice_date": _iso(inv.invoice_date),
            "due_date": _iso(inv.due_date),
            "days_overdue": (
                (today - inv.due_date).days
                if inv.due_date and inv.due_date < today else 0
            ),
            "grand_total": _num(inv.grand_total),
            "paid_amount": _num(inv.paid_amount),
            "balance_amount": _num(inv.balance_amount),
            "payment_status": _ev(inv.payment_status),
        }
        for inv, customer_name in rows
    ]}


def _timesheet_status(db: Session, args: dict, ctx: dict) -> dict:
    """Who has not submitted or approved for a month — the chase list."""
    from models import Employee, Project, Timesheet, TimesheetStatus

    today = date.today()
    try:
        month = int(args.get("month") or today.month)
        year = int(args.get("year") or today.year)
    except (TypeError, ValueError):
        return {"error": "month and year must be numbers"}
    if not 1 <= month <= 12:
        return {"error": "month must be between 1 and 12"}

    stmt = (
        select(Timesheet, Employee, Project.name)
        .join(Employee, Employee.id == Timesheet.employee_id, isouter=True)
        .join(Project, Project.id == Timesheet.project_id, isouter=True)
        .where(Timesheet.month == month, Timesheet.year == year)
    )
    status = (args.get("status") or "").strip()
    if status:
        try:
            stmt = stmt.where(Timesheet.status == TimesheetStatus(status))
        except ValueError:
            valid = ", ".join(s.value for s in TimesheetStatus)
            return {"error": f"Unknown status '{status}'. Valid values: {valid}"}
    if args.get("project_name"):
        stmt = stmt.where(Project.name.ilike(f"%{args['project_name']}%"))

    rows = db.execute(stmt.limit(_clamp(args.get("limit"), default=15))).all()
    counts: dict[str, int] = {}
    for ts, _emp, _proj in rows:
        key = str(_ev(ts.status))
        counts[key] = counts.get(key, 0) + 1
    return {
        "period": f"{year:04d}-{month:02d}",
        "by_status": counts,
        "note": "Draft means the employee has not submitted; Submitted means it is "
                "waiting on an approver.",
        "timesheets": [
            {
                "employee": _person_name(employee),
                "project": proj_name,
                "status": _ev(ts.status),
            }
            for ts, employee, proj_name in rows
        ],
    }


def _leave_balances(db: Session, args: dict, ctx: dict) -> dict:
    from models import Employee, EmployeeLeaveBalance, LeavePolicyType

    name = (args.get("employee_name") or "").strip()
    if not name:
        return {"error": "employee_name is required"}
    employee = db.execute(
        select(Employee).where(_name_filter(Employee, name))
    ).scalars().first()
    if employee is None:
        return {"error": f"No employee matching '{name}'"}

    year = args.get("year") or date.today().year
    rows = db.execute(
        select(EmployeeLeaveBalance, LeavePolicyType.name)
        .join(LeavePolicyType,
              LeavePolicyType.id == EmployeeLeaveBalance.leave_type_id, isouter=True)
        .where(EmployeeLeaveBalance.employee_id == employee.id,
               EmployeeLeaveBalance.year == int(year))
    ).all()
    return {
        "employee": _person_name(employee),
        "year": int(year),
        "balances": [
            {
                "leave_type": type_name,
                "accrued": _num(bal.accrued),
                "carry_forward": _num(bal.carry_forward),
                "consumed": _num(bal.consumed),
                "balance": _num(bal.balance),
            }
            for bal, type_name in rows
        ],
    }


# =========================================================================
# Registry
# =========================================================================

#: name -> (handler, required_roles or None, OpenAI schema)
#: `required_roles is None` means any user with a CRM role may call it.
_REGISTRY: dict[str, tuple[Callable[[Session, dict, dict], dict], set[str] | None, dict]] = {
    "search_requirements": (_search_requirements, None, {
        "type": "function",
        "function": {
            "name": "search_requirements",
            "description": "Find hiring requirements by status, customer, title/number or skill.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description":
                               "Exact status e.g. Open_For_Sourcing, In_Progress, Fulfilled"},
                    "customer_name": {"type": "string"},
                    "query": {"type": "string", "description": "Matches title or REQ number"},
                    "skill": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "get_requirement": (_get_requirement, None, {
        "type": "function",
        "function": {
            "name": "get_requirement",
            "description": "Full detail for one requirement including skills and resume counts.",
            "parameters": {
                "type": "object",
                "properties": {"req_number": {"type": "string",
                                              "description": "e.g. REQ-2026-016"}},
                "required": ["req_number"],
            },
        },
    }),
    "top_resumes_for_requirement": (_top_resumes_for_requirement, None, {
        "type": "function",
        "function": {
            "name": "top_resumes_for_requirement",
            "description": "Best-scoring candidates submitted against a requirement, "
                           "highest ATS score first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "req_number": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["req_number"],
            },
        },
    }),
    "search_candidate_profiles": (_search_candidate_profiles, None, {
        "type": "function",
        "function": {
            "name": "search_candidate_profiles",
            "description": "Candidates in the delivery pipeline, by stage, name or opportunity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_status": {"type": "string", "description":
                                        "e.g. Sourcing, RMG_Review, Customer_Approval, Joined"},
                    "candidate_name": {"type": "string"},
                    "opportunity_id": {"type": "string", "description": "e.g. OPP-2026-004"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "pipeline_counts": (_pipeline_counts, None, {
        "type": "function",
        "function": {
            "name": "pipeline_counts",
            "description": "How many candidate profiles sit at each pipeline stage.",
            "parameters": {
                "type": "object",
                "properties": {"opportunity_id": {"type": "string"}},
            },
        },
    }),
    "search_opportunities": (_search_opportunities, None, {
        "type": "function",
        "function": {
            "name": "search_opportunities",
            "description": "Sales opportunities by pipeline stage or customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pipeline_stage": {"type": "string", "description":
                                       "e.g. New, Active, On_Hold, Closed_Won"},
                    "customer_name": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "timesheet_status": (_timesheet_status, None, {
        "type": "function",
        "function": {
            "name": "timesheet_status",
            "description": "Timesheet submission and approval state for a month — "
                           "who still has to submit or approve.",
            "parameters": {
                "type": "object",
                "properties": {
                    "month": {"type": "integer", "description": "1-12, defaults to this month"},
                    "year": {"type": "integer"},
                    "status": {"type": "string", "description":
                               "Draft, Submitted, Approved or Rejected"},
                    "project_name": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "search_purchase_orders": (_search_purchase_orders, FINANCE_ROLES, {
        "type": "function",
        "function": {
            "name": "search_purchase_orders",
            "description": "Purchase orders with values and expiry, by customer, status or "
                           "how soon they expire.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "status": {"type": "string", "description": "Active, Exhausted or Cancelled"},
                    "expiring_within_days": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "search_invoices": (_search_invoices, FINANCE_ROLES, {
        "type": "function",
        "function": {
            "name": "search_invoices",
            "description": "Invoices with totals, payments and overdue days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "payment_status": {"type": "string", "description":
                                       "Unpaid, Partially_Paid or Paid"},
                    "overdue_only": {"type": "boolean"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }),
    "leave_balances": (_leave_balances, HR_ROLES, {
        "type": "function",
        "function": {
            "name": "leave_balances",
            "description": "Leave balances for one employee by leave type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "employee_name": {"type": "string"},
                    "year": {"type": "integer"},
                },
                "required": ["employee_name"],
            },
        },
    }),
}


def tool_names() -> list[str]:
    return list(_REGISTRY)


def _permitted(name: str, roles: set[str], is_admin: bool) -> bool:
    entry = _REGISTRY.get(name)
    if entry is None:
        return False
    required = entry[1]
    return required is None or is_admin or bool(roles & required)


def available_tools(
    roles: set[str],
    is_admin: bool,
    whitelist: list[str] | None = None,
) -> list[dict]:
    """OpenAI tool schemas this user may call.

    A tool the user is not allowed to run is not merely refused — it is never
    offered, so the model cannot describe a capability the person does not have
    and cannot be steered into trying.
    """
    allowed = set(whitelist) if whitelist else None
    return [
        schema for name, (_fn, _required, schema) in _REGISTRY.items()
        if _permitted(name, roles, is_admin) and (allowed is None or name in allowed)
    ]


def run_tool(
    db: Session,
    name: str,
    args: dict | None,
    *,
    roles: set[str],
    is_admin: bool,
    whitelist: list[str] | None = None,
) -> dict:
    """Execute one whitelisted read. Never raises — errors come back as data.

    A tool that raises would abort the whole assist call; a tool that returns
    ``{"error": ...}`` lets the model apologise usefully, or try a different
    query, which is what a person would do.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        return {"error": f"Unknown tool '{name}'"}
    if whitelist and name not in whitelist:
        return {"error": f"Tool '{name}' is not enabled for this request"}
    if not _permitted(name, roles, is_admin):
        return {"error": "You do not have permission to see that data."}

    handler = entry[0]
    try:
        result = handler(db, args or {}, {"roles": roles, "is_admin": is_admin})
    except Exception as exc:  # never let a query kill the conversation
        logger.warning("ai_help.tool_failed name=%s: %s", name, exc, exc_info=True)
        return {"error": "That lookup failed. Try narrowing the question."}

    # A tool must never be the reason the prompt blows its budget. Truncating
    # to a note rather than to malformed JSON keeps the model honest: it can
    # say "too many to list" instead of summarising half a result as if it
    # were the whole one.
    if len(json.dumps(result, default=str)) > _MAX_RESULT_CHARS:
        return {
            "error": "That returned too much data to read at once. "
                     "Narrow it down — add a customer, a status, or a smaller limit.",
        }
    return result
