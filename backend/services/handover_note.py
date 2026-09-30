"""The note RMG / GM hand to Sales with a candidate (28 Sep 2026).

User ask: "when RMG / GM submit to Sales, the message should come by default
from the interviews — no need to type it by hand". The Submit-to-Sales dialog
is prefilled with `handover_note(db, profile)`; RMG / GM may still edit it, and
what they send is what Sales reads (the status-transition comment).

It is built ONLY from facts already recorded, never invented:
  * every technical round that happened (L1–L4): verdict, interviewer, the
    first line(s) of the written feedback;
  * the AI L1, when one was completed: verdict and score;
  * the skill evaluation: where the reviewer rated at or above the required
    level (strengths) and below it (gaps);
  * experience, notice period and expected CTC when on file.
`compose_handover_note` is PURE (no DB) so every branch is testable; the
loader does three small queries for one profile.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

#: Rounds RMG / GM own — the ones whose verdicts they hand over.
TECH_ROUNDS = ("L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview")
#: Feedback is quoted, not pasted whole: Sales reads a summary.
FEEDBACK_CHARS = 220


@dataclass
class RoundFact:
    label: str
    result: str | None
    interviewer: str | None = None
    feedback: str | None = None


@dataclass
class HandoverFacts:
    candidate: str
    position: str | None = None
    customer: str | None = None
    rounds: list[RoundFact] = field(default_factory=list)
    ai_result: str | None = None
    ai_score: float | None = None
    #: (skill, reviewer rating, required level) — ratings 1..5.
    skills: list[tuple[str, int | None, int | None]] = field(default_factory=list)
    experience_years: float | None = None
    notice_period: str | None = None
    expected_ctc: float | None = None


def _clip(text: str | None, limit: int = FEEDBACK_CHARS) -> str:
    one = " ".join((text or "").split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _num(v: float) -> str:
    return f"{v:g}"


def to_lac(v) -> float:
    """CTCs are stored in RUPEES (the profile form converts Lac → rupees); a
    figure under 1,000 is already in Lac (older rows / tests). 29 Sep 2026:
    the note printed "expects 2500000 L" for a ₹25 L expectation."""
    x = float(v)
    return round(x / 100000, 2) if x >= 1000 else x


def compose_handover_note(f: HandoverFacts) -> str:
    """The recommendation text, one fact per line. Empty facts are left out."""
    head = f"Recommending {f.candidate}"
    if f.position:
        head += f" for {f.position}"
    if f.customer:
        head += f" ({f.customer})"
    lines = [head + "."]

    for r in f.rounds:
        who = f" — {r.interviewer}" if r.interviewer else ""
        verdict = r.result or "no verdict recorded"
        line = f"• {r.label}{who}: {verdict}"
        if r.feedback and r.feedback.strip():
            line += f". {_clip(r.feedback)}"
        lines.append(line)

    if f.ai_result or f.ai_score is not None:
        score = f" ({round(f.ai_score)}%)" if f.ai_score is not None else ""
        lines.append(f"• AI L1 interview: {f.ai_result or 'completed'}{score}")

    rated = [(s, got, need) for s, got, need in f.skills if got is not None]
    strong = [f"{s} {got}/5" for s, got, need in rated if need is None or got >= need]
    gaps = [f"{s} {got}/5 (needs {need})" for s, got, need in rated if need is not None and got < need]
    if strong:
        lines.append("Strengths: " + ", ".join(strong) + ".")
    if gaps:
        lines.append("To probe: " + ", ".join(gaps) + ".")

    facts = []
    if f.experience_years is not None:
        facts.append(f"{_num(float(f.experience_years))} yrs experience")
    if f.notice_period:
        facts.append(f"notice {f.notice_period}")
    if f.expected_ctc is not None:
        facts.append(f"expects {_num(to_lac(f.expected_ctc))} L")
    if facts:
        text = " · ".join(facts)
        lines.append(text[0].upper() + text[1:] + ".")
    return "\n".join(lines)


def handover_facts(db: Session, profile) -> HandoverFacts:
    """Load the facts for one profile — three small queries."""
    from models import (
        AiInterviewLink, Candidate, Customer, InterviewEvent, Opportunity, Skill, SkillEvaluation,
    )
    from services.interview_rounds import NOT_HELD_STATUSES, round_label

    cand = db.get(Candidate, profile.candidate_id)
    opp = db.get(Opportunity, profile.opportunity_id) if profile.opportunity_id else None
    customer = db.get(Customer, opp.customer_id) if opp is not None and getattr(opp, "customer_id", None) else None
    name = " ".join(x for x in (getattr(cand, "first_name", None), getattr(cand, "last_name", None)) if x) \
        or f"Candidate #{profile.candidate_id}"

    events = db.execute(
        select(InterviewEvent)
        .where(InterviewEvent.profile_id == profile.id, InterviewEvent.kind.in_(TECH_ROUNDS))
        .order_by(InterviewEvent.scheduled_at.asc().nullslast(), InterviewEvent.id.asc())
    ).scalars().all()
    rounds = [RoundFact(round_label(e.kind), e.result, e.interviewer, e.feedback)
              for e in events if (e.status or "") not in NOT_HELD_STATUSES]

    link = db.execute(
        select(AiInterviewLink)
        .where(AiInterviewLink.profile_id == profile.id, AiInterviewLink.completed_at.isnot(None))
        .order_by(AiInterviewLink.id.desc()).limit(1)
    ).scalar_one_or_none()

    skills = [(n, got, need) for n, got, need in db.execute(
        select(Skill.name, SkillEvaluation.reviewer_rated, SkillEvaluation.required_level)
        .join(Skill, Skill.id == SkillEvaluation.skill_id)
        .where(SkillEvaluation.profile_id == profile.id)
        .order_by(Skill.name)
    ).all()]

    def first(*vals):
        return next((v for v in vals if v not in (None, "")), None)

    return HandoverFacts(
        candidate=name,
        position=getattr(opp, "title", None),
        customer=first(getattr(customer, "legal_entity_name", None), getattr(customer, "name", None)),
        rounds=rounds,
        ai_result=(getattr(link, "effective_result", None) or getattr(link, "result", None)) if link else None,
        ai_score=(float(link.overall_score_percent) if link is not None and link.overall_score_percent is not None
                  else None),
        skills=skills,
        experience_years=first(getattr(profile, "total_experience_years", None),
                               getattr(cand, "experience_years", None)),
        notice_period=first(getattr(profile, "notice_period", None), getattr(cand, "notice_period", None)),
        expected_ctc=first(getattr(profile, "expected_ctc", None), getattr(cand, "expected_ctc", None)),
    )


def handover_note(db: Session, profile) -> str:
    return compose_handover_note(handover_facts(db, profile))


# ------------------------------------------------------------ sales readiness
#
# 29 Sep 2026, user ask: "when GM / RMG submit a candidate to Sales, show every
# detail Sales needs so they can verify it — and add what is missing right
# there". ONE list, read with the note (`GET …/handover-note` → `checks`) and
# written by `PATCH …/sales-details` (`SALES_DETAIL_FIELDS`).

#: field key → (where it lives, column). Only these can be written from the
#: Submit-to-Sales dialog — nothing else on the profile or candidate.
SALES_DETAIL_FIELDS: dict[str, tuple[str, str]] = {
    "current_ctc": ("profile", "current_ctc"),
    "expected_ctc": ("profile", "expected_ctc"),
    "total_experience_years": ("profile", "total_experience_years"),
    "notice_period": ("candidate", "notice_period"),
    "city": ("candidate", "city"),
    "preferred_locations": ("candidate", "preferred_locations"),
    "phone": ("candidate", "phone"),
}


def _real_email(email: str | None) -> str | None:
    e = (email or "").strip()
    low = e.lower()
    if not e or low.endswith("@import.karnex.in") or "@noemail" in low:
        return None
    return e


def sales_readiness(db: Session, profile) -> list[dict]:
    """What Sales needs before they can put the candidate to a customer.

    Each check: `key`, `label`, `value` (display text or None), `ok`,
    `required` (a missing required item is shown red; the submit is NOT
    blocked — RMG may know something the record does not), `field` (the
    `SALES_DETAIL_FIELDS` key to fill it inline, None when it is filled
    elsewhere) and `input` (money_lac · number · text) for the UI.
    """
    from models import AiInterviewLink, Candidate, InterviewEvent, SkillEvaluation
    from services.interview_rounds import NOT_HELD_STATUSES

    cand = db.get(Candidate, profile.candidate_id)

    def first(*vals):
        return next((v for v in vals if v not in (None, "")), None)

    def lac(v):
        return None if v is None else f"{_num(to_lac(v))} L"

    checks: list[dict] = []

    def add(key, label, value, *, required=True, field=None, kind="text", ok=None, hint=None):
        checks.append({"key": key, "label": label, "value": value,
                       "ok": bool(value) if ok is None else ok, "required": required,
                       "field": field, "input": kind, "hint": hint})

    cur = first(profile.current_ctc, getattr(cand, "current_ctc", None))
    exp = first(profile.expected_ctc, getattr(cand, "expected_ctc", None))
    years = first(profile.total_experience_years, getattr(cand, "experience_years", None))
    add("current_ctc", "Current CTC", lac(cur), field="current_ctc", kind="money_lac")
    add("expected_ctc", "Expected CTC", lac(exp), field="expected_ctc", kind="money_lac")
    add("notice_period", "Notice period", first(getattr(cand, "notice_period", None)), field="notice_period")
    add("experience", "Total experience", f"{_num(float(years))} yrs" if years is not None else None,
        field="total_experience_years", kind="number")
    add("city", "Current location", first(getattr(cand, "city", None)), field="city")
    add("preferred_locations", "Preferred location", first(getattr(cand, "preferred_locations", None)),
        field="preferred_locations")
    add("phone", "Phone", first(getattr(cand, "phone", None)), field="phone")
    add("email", "Email", _real_email(getattr(cand, "email", None)),
        hint="Edit it on the candidate record")
    add("cv", "CV on file", "On file" if first(profile.resume_url, getattr(cand, "cv_url", None)) else None,
        hint="Upload it on the candidate record")

    held = [e for e in db.execute(
        select(InterviewEvent.result, InterviewEvent.status)
        .where(InterviewEvent.profile_id == profile.id,
               InterviewEvent.kind.in_(TECH_ROUNDS))
    ).all() if (e.status or "") not in NOT_HELD_STATUSES]
    verdicts = [e.result for e in held if e.result]
    ai = db.execute(
        select(AiInterviewLink.result, AiInterviewLink.overall_score_percent)
        .where(AiInterviewLink.profile_id == profile.id, AiInterviewLink.completed_at.isnot(None))
        .order_by(AiInterviewLink.id.desc()).limit(1)
    ).first()
    parts = [f"{len(verdicts)} round verdict{'s' if len(verdicts) != 1 else ''}"] if verdicts else []
    if ai is not None:
        parts.append(f"AI L1 {ai.result or 'done'}"
                     + (f" {round(float(ai.overall_score_percent))}%" if ai.overall_score_percent is not None else ""))
    add("technical", "Technical verdict", " · ".join(parts) or None,
        hint="Record the round feedback first")
    rated = db.execute(
        select(sa.func.count()).select_from(SkillEvaluation)
        .where(SkillEvaluation.profile_id == profile.id, SkillEvaluation.reviewer_rated.isnot(None))
    ).scalar() or 0
    add("skills", "Skill evaluation", f"{rated} skill{'s' if rated != 1 else ''} rated" if rated else None,
        required=False, hint="Rate the skills in the Skill Evaluation tab")
    return checks


def apply_sales_details(db: Session, profile, values: dict) -> list[str]:
    """Write the whitelisted fields; returns the keys that changed. CTCs arrive
    in Lac (the dialog's unit) and are stored in rupees."""
    from models import Candidate

    cand = db.get(Candidate, profile.candidate_id)
    changed: list[str] = []
    for key, raw in values.items():
        if key not in SALES_DETAIL_FIELDS:
            continue
        where, col = SALES_DETAIL_FIELDS[key]
        target = profile if where == "profile" else cand
        if target is None:
            continue
        if key in ("current_ctc", "expected_ctc"):
            value = None if raw in (None, "") else round(float(raw) * 100000, 2)
        elif key == "total_experience_years":
            value = None if raw in (None, "") else float(raw)
        else:
            value = (str(raw).strip() or None) if raw is not None else None
        if getattr(target, col) != value:
            setattr(target, col, value)
            changed.append(key)
    return changed
