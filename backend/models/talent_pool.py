"""Talent Pool, Phases 2 and 3 (17 Oct 2026) — see services/talent_pool*.py.

* `SkillSynonym`     — admin-kept "these names are the same skill" pairs
                        (AUTOSAR CP ≈ AUTOSAR Classic). Built-in groups live in
                        code; these rows add to them.
* `TalentSkillIndex` — one row per (candidate, skill, evidence): the four
                        Phase 1 sources flattened so a search reads one small
                        indexed table instead of every CV's JSON. DERIVED data —
                        rebuilt by the `talent_pool_index` job, never edited.
* `TalentSavedSearch`— a recruiter's saved search + its daily alert state.

No FK on user / candidate columns (the `work_desk_marks` rule): a deleted
login or a merged candidate must never block anything here.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base


class SkillSynonym(Base):
    __tablename__ = "skill_synonyms"
    __table_args__ = (sa.UniqueConstraint("synonym_key", name="uq_skill_synonyms_key"),)

    id = sa.Column(sa.Integer, primary_key=True)
    canonical = sa.Column(sa.String(120), nullable=False)     # the name people search for
    synonym = sa.Column(sa.String(120), nullable=False)       # another name for it
    synonym_key = sa.Column(sa.String(120), nullable=False)   # talent_pool.skill_key(synonym)
    canonical_key = sa.Column(sa.String(120), nullable=False, index=True)
    created_by = sa.Column(sa.Integer, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class TalentSkillIndex(Base):
    __tablename__ = "talent_skill_index"
    __table_args__ = (sa.UniqueConstraint("candidate_id", "skill_key", "evidence",
                                          name="uq_talent_skill_index_row"),)

    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, nullable=False, index=True)
    skill_key = sa.Column(sa.String(120), nullable=False, index=True)
    skill_name = sa.Column(sa.String(120), nullable=False)
    evidence = sa.Column(sa.String(16), nullable=False)
    rating = sa.Column(sa.Integer, nullable=True)
    indexed_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


class TalentSavedSearch(Base):
    __tablename__ = "talent_saved_searches"

    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, nullable=False, index=True)
    name = sa.Column(sa.String(120), nullable=False)
    filters = sa.Column(JSONB, nullable=False)                # the search's query parameters
    alerts_enabled = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    #: Entry keys ("c12" / "e5") the owner has already seen or been told about.
    seen_keys = sa.Column(JSONB, nullable=True)
    #: Entry keys that appeared since the owner last looked (cleared by "seen").
    new_keys = sa.Column(JSONB, nullable=True)
    last_checked_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    last_alert_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    last_result_count = sa.Column(sa.Integer, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(),
                           onupdate=sa.func.now())
