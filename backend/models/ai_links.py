"""Link table joining AI-interview sessions (legacy interview_schedule /
interview_records, joined by invite_token) to CRM entities. Keeps the legacy
tables untouched while scoping results to candidate + opportunity."""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK


class AiInterviewLink(Base):
    __tablename__ = "ai_interview_links"
    id = sa.Column(sa.Integer, primary_key=True)
    invite_token = sa.Column(sa.String(64), nullable=False, unique=True)  # join key to legacy tables
    schedule_id = sa.Column(sa.String(64), nullable=True)                 # interview_schedule.id (uuid)
    interview_record_id = sa.Column(sa.String(64), nullable=True)         # interview_records.id (set on completion)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id"), nullable=False, index=True)
    opportunity_id = sa.Column(sa.Integer, sa.ForeignKey("opportunities.id"), nullable=False, index=True)
    profile_id = sa.Column(sa.Integer, sa.ForeignKey("candidate_profiles.id"), nullable=False, index=True)
    requirement_id = sa.Column(sa.Integer, sa.ForeignKey("requirements.id"), nullable=True)
    resume_id = sa.Column(sa.Integer, sa.ForeignKey("resumes.id"), nullable=True)
    scheduled_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    level = sa.Column(sa.String(8), nullable=False, server_default="L1")
    overall_score_percent = sa.Column(sa.Numeric(5, 2), nullable=True)
    result = sa.Column(sa.String(16), nullable=False, server_default="Pending")  # Pending/Passed/Failed

    # --- Recruiter override (migration 0062) --------------------------------
    # `result` above is the AI's own verdict: score vs the pass threshold, written
    # once at completion. A recruiter can disagree — on the interview report page
    # they press Shortlist / On Hold / Reject. That decision used to live only in
    # the legacy auth DB, so the CRM never saw it and kept showing "Failed" next
    # to a candidate the recruiter had already selected.
    #
    # These columns carry the decision across. `result` is deliberately NOT
    # overwritten: the AI verdict and the human verdict are both facts, and a
    # score that was recorded as 57.2% Failed should stay on the record even
    # after someone selects the candidate anyway.
    hr_decision = sa.Column(sa.String(16), nullable=True)   # selected/rejected/on_hold/pending_review
    hr_decision_by = sa.Column(sa.String(255), nullable=True)   # who (email/username)
    hr_decision_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    completed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    profile = relationship("CandidateProfile")
    candidate = relationship("Candidate")

    #: Canonical decision vocabulary. The report page speaks two dialects —
    #: candidate-level ("shortlist"/"reject"/"on_hold") and per-interview
    #: ("selected"/"rejected"/"on_hold"/"pending_review"). Both normalise here.
    HR_DECISIONS = ("selected", "rejected", "on_hold", "pending_review")

    @property
    def effective_result(self) -> str:
        """What a human should act on: the override when present, else the AI verdict."""
        return _DECISION_LABEL.get(self.hr_decision or "", None) or self.result


#: Display labels for the override, so the UI and the API agree on wording.
_DECISION_LABEL = {
    "selected": "Selected",
    "rejected": "Rejected",
    "on_hold": "On Hold",
    "pending_review": "Pending Review",
}


def normalize_hr_decision(raw: str | None) -> str | None:
    """Map either dialect of decision onto the canonical vocabulary.

    Returns None for "cleared" (the report page toggles a decision off by
    re-clicking it), and raises nothing on junk — unknown values become None so
    a bad payload can never write a status nobody can interpret.
    """
    value = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not value or value in {"none", "null", "clear", "cleared"}:
        return None
    # candidate-level dialect -> per-interview dialect
    alias = {"shortlist": "selected", "shortlisted": "selected", "reject": "rejected",
             "hold": "on_hold", "onhold": "on_hold", "pending": "pending_review",
             "review": "pending_review"}
    value = alias.get(value, value)
    return value if value in AiInterviewLink.HR_DECISIONS else None


def hr_decision_label(raw: str | None) -> str | None:
    return _DECISION_LABEL.get(normalize_hr_decision(raw) or "")
