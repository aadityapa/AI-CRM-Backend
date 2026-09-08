"""Human interview rounds on a candidate profile — vocabulary, validation, CRUD.

The dropdown values live HERE and are served to the UI via
GET /api/candidate-profiles/{id}/interview-rounds/options, so the form and the
server can never disagree about what is selectable.

Values mirror the Zoho Interview_Round subform so imported history and rounds
entered in the app are directly comparable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Employee, InterviewEvent

try:
    from zoneinfo import ZoneInfo
    _IST = ZoneInfo("Asia/Kolkata")
except Exception:  # tzdata missing on a bare Windows install
    _IST = timezone.utc


def read_as_ist(dt: datetime | None) -> datetime | None:
    """A NAIVE datetime from the round form is IST — the timezone the TA typed
    it in — and must be stored as such (2 Sep 2026 bug report).

    The form's datetime-local input arrives without a zone. Handed straight to
    a timestamptz column it was read as UTC, so every round landed 5h30 late:
    a 7:00 pm interview showed as 12:30 am the next day on the Interviews tab
    and sat in the wrong day (or off-grid) on the Interview Calendar. Same rule
    as slots._from_ist for the candidate booking page.
    """
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=_IST).astimezone(timezone.utc)

#: Interviewer_Category
CATEGORIES = ["Internal", "External"]

#: Interview_Round -> stored in interview_events.kind
ROUNDS: list[tuple[str, str]] = [
    ("L1_Interview", "L1 - Interview"),
    ("L2_F2F", "L2 - Interview"),
    ("L3_Interview", "L3 - Interview"),
    ("L4_Interview", "L4 - Interview"),
    # HR's own round at HR_Screening (2 Sep 2026): TA schedules it, HR records
    # the verdict. Zoho history already used this kind for imported rows.
    ("HR_Interview", "HR - Interview"),
    # The customer's own rounds, recorded after the client interviews the
    # candidate. Customer_Interview is the customer's first round (Customer L1);
    # Customer_L2 (added Aug 2026) is a second customer round for clients who run
    # two. Zoho-imported history uses Customer_Interview and the display ordering
    # ranks these last.
    ("Customer_Interview", "Customer L1 - Interview"),
    ("Customer_L2", "Customer L2 - Interview"),
]
ROUND_VALUES = [value for value, _ in ROUNDS]

#: Which roles may write which round.
#:
#: A single WRITE_ROLES tuple could not express this: RMG owns the technical
#: ladder and Sales owns the customer conversation, and neither should be able
#: to write the other's rounds. Sales recording an L2 result would be inventing
#: an engineering opinion; RMG recording customer feedback would be inventing
#: the client's.
#: TA coordinates the whole interview process, so (Aug 2026) TA may record any
#: round's feedback alongside the round's natural owner — RMG for the technical
#: ladder, Sales for the customer's rounds. Admin/CEO are implicit everywhere.
ROUND_WRITE_ROLES: dict[str, tuple[str, ...]] = {
    "L1_Interview": ("RMG", "TA"),
    "L2_F2F": ("RMG", "TA"),
    "L3_Interview": ("RMG", "TA"),
    "L4_Interview": ("RMG", "TA"),
    "HR_Interview": ("HR", "TA"),
    "Customer_Interview": ("Sales", "Sales_Head", "TA"),
    "Customer_L2": ("Sales", "Sales_Head", "TA"),
}

#: Interview_Duration, in minutes.
DURATIONS = [15, 30, 45, 60, 90, 120, 180]

#: Interview_Status
STATUSES = [
    "Cancelled",
    "Completed",
    "In-Progress",
    "No Show",
    "Pending",
    "Rescheduled Requested By Candidate",
    "Rescheduled Requested By Panel",
    "Scheduled",
]

#: Result — ordered worst to best, matching the Zoho scale.
RESULTS = ["No Hire", "Leaning No", "Leaning Hire", "Hire", "Strong Hire"]

#: The HR round's verdict (3 Sep 2026, user decision): Hire, or Not Recommend
#: (= HR expects a CTC / joining-date problem). Either moves the profile to
#: Pre-Onboarding; the second also raises the budget flag. "Drop" (4 Sep 2026,
#: user request) is the candidate walking away — a better offer elsewhere —
#: and closes the profile as Self Withdrawn. Legacy rows carrying the
#: five-step scale still read back fine.
HR_RESULTS = ["Hire", "Not Recommend", "Drop"]


def results_for(kind: str | None) -> list[str]:
    return HR_RESULTS if (kind or "") == "HR_Interview" else RESULTS

#: UserRole — who conducted the round.
USER_ROLES = ["Customer", "HR", "Interviewer", "RMG", "Sales", "TA"]

#: Any role that may write SOME round. Used as the coarse endpoint gate; the
#: per-kind check below is what actually decides. (Admin/CEO are implicit.)
WRITE_ROLES = tuple(sorted({role for roles in ROUND_WRITE_ROLES.values() for role in roles}))


def roles_for_round(kind: str | None) -> tuple[str, ...]:
    """Roles permitted to write this round kind. Unknown kinds permit nobody."""
    return ROUND_WRITE_ROLES.get(str(kind or "").strip(), ())


def rounds_writable_by(user) -> list[str]:
    """Round kinds this user may create or edit.

    Drives both the server-side guard and the form's dropdown, so a user is
    never offered a round the save would reject.
    """
    if getattr(user, "is_admin", False):
        return list(ROUND_VALUES)
    user_roles = set(getattr(user, "roles", []) or [])
    return [kind for kind in ROUND_VALUES if user_roles & set(ROUND_WRITE_ROLES.get(kind, ()))]


def ensure_may_write_round(user, kind: str | None) -> None:
    """403 unless this user owns this round kind."""
    allowed = roles_for_round(kind)
    if getattr(user, "is_admin", False):
        return
    if not allowed:
        raise HTTPException(status_code=400, detail=f"Unknown interview round '{kind}'")
    if not (set(getattr(user, "roles", []) or []) & set(allowed)):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Your role cannot record a '{kind}' round. "
                f"That round is owned by: {', '.join(allowed)}."
            ),
        )


def _norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).lower()


def _match(value: str | None, allowed: list[str], field: str, *, required: bool = False):
    """Case-insensitive match against the vocabulary, returning the canonical value.

    Imported Zoho rows carry variants like "ReScheduled Requested By Candidate";
    matching on a normalised key keeps those editable instead of rejecting them.
    """
    if value is None or not str(value).strip():
        if required:
            raise HTTPException(status_code=400, detail=f"{field} is required")
        return None
    hit = next((a for a in allowed if _norm(a) == _norm(value)), None)
    if hit is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} '{value}'. Allowed: {', '.join(allowed)}",
        )
    return hit


def options(db: Session, user=None) -> dict:
    """Everything the feedback form needs to render its dropdowns.

    When `user` is supplied the round list is narrowed to the kinds they may
    actually write, so Sales is never offered an L2 that the save would reject
    and RMG is never offered a customer round.
    """
    employees = db.execute(
        select(Employee.id, Employee.first_name, Employee.middle_name, Employee.last_name,
               Employee.email, Employee.employee_code)
        .where(Employee.is_active.is_(True))
        .order_by(Employee.first_name, Employee.last_name)
    ).all()
    writable = set(rounds_writable_by(user)) if user is not None else set(ROUND_VALUES)
    return {
        "categories": CATEGORIES,
        # Every round is listed so existing rows still render with a label;
        # `writable` tells the form which may be chosen.
        "rounds": [{"value": v, "label": l, "writable": v in writable} for v, l in ROUNDS],
        "writable_rounds": [v for v in ROUND_VALUES if v in writable],
        "durations": DURATIONS,
        "statuses": STATUSES,
        "results": RESULTS,
        "hr_results": HR_RESULTS,
        "user_roles": USER_ROLES,
        "employees": [
            {
                "id": e_id,
                "full_name": " ".join(p for p in (first, middle, last) if p),
                "email": email,
                "employee_code": code,
            }
            for e_id, first, middle, last, email, code in employees
        ],
    }


def validate_round(db: Session, payload, *, partial: bool = False,
                   kind_hint: str | None = None) -> dict:
    """Payload -> validated column values. Raises 400 with a readable message.

    `partial=True` (PUT) only validates the fields actually supplied, so a caller
    can change one field without resending the whole round. `kind_hint` is the
    stored kind on an update, so the result vocabulary can be picked (the HR
    round has its own two-way verdict) even when the payload omits `kind`.
    """
    data: dict = {}
    given = payload.model_fields_set if partial else set(payload.model_fields.keys())

    if not partial or "kind" in given:
        data["kind"] = _match(payload.kind, ROUND_VALUES, "Interview Round", required=True)
    if not partial or "interview_category" in given:
        data["interview_category"] = _match(
            payload.interview_category, CATEGORIES, "Interview Category")
    if not partial or "status" in given:
        data["status"] = _match(payload.status, STATUSES, "Interview Status")
    if not partial or "result" in given:
        effective_kind = data.get("kind") or kind_hint
        # Legacy HR rows may carry the five-step scale; keep them editable.
        allowed = results_for(effective_kind)
        if effective_kind == "HR_Interview":
            allowed = HR_RESULTS + [r for r in RESULTS if r not in HR_RESULTS]
        data["result"] = _match(payload.result, allowed, "Result")
    if not partial or "user_role" in given:
        data["user_role"] = _match(payload.user_role, USER_ROLES, "User Role")

    if not partial or "duration_minutes" in given:
        dur = payload.duration_minutes
        if dur is not None and dur not in DURATIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Interview Duration '{dur}'. "
                       f"Allowed: {', '.join(str(d) for d in DURATIONS)}",
            )
        data["duration_minutes"] = dur

    if not partial or "employee_id" in given:
        emp_id = payload.employee_id
        if emp_id is not None:
            emp = db.get(Employee, emp_id)
            if emp is None:
                raise HTTPException(status_code=404, detail="Employee not found")
            data["employee_id"] = emp.id
            data["interviewer"] = " ".join(
                p for p in (emp.first_name, emp.middle_name, emp.last_name) if p)[:200]
        else:
            data["employee_id"] = None

    # External panellists have no employee row — accept a typed name instead.
    if "interviewer" in given and payload.interviewer is not None:
        typed = " ".join(str(payload.interviewer).split())
        if typed:
            data["interviewer"] = typed[:200]

    if not partial or "scheduled_at" in given:
        data["scheduled_at"] = read_as_ist(payload.scheduled_at)
        # raw_when keeps the wall-clock text the TA typed (IST), for display.
        data["raw_when"] = (payload.scheduled_at.strftime("%Y-%m-%d %H:%M")
                            if payload.scheduled_at else None)
    if not partial or "feedback" in given:
        data["feedback"] = (payload.feedback or "").strip() or None
    if not partial or "meeting_link" in given:
        data["meeting_link"] = ((payload.meeting_link or "").strip() or None)
    if not partial or "stage" in given:
        data["stage"] = ((payload.stage or "").strip()[:120] or None)
    if not partial or "mode" in given:
        data["mode"] = ((payload.mode or "").strip()[:60] or None)
    return data


def get_round_or_404(db: Session, profile_id: int, event_id: int) -> InterviewEvent:
    event = db.get(InterviewEvent, event_id)
    if event is None or event.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Interview round not found")
    return event


def round_label(kind: str | None) -> str:
    return next((l for v, l in ROUNDS if v == kind), kind or "Interview")
