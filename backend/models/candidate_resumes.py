"""A candidate's resume library (9 Oct 2026).

One candidate, several resumes — an "AUTOSAR version", a "BLE version" — each
scored against every open position so TA can see which positions the
candidate fits and apply them with the best version in one go
(`services/candidate_resumes.py`).

`file_url` is the stored file (`/api/crm-files/...`), the same kind of URL
`candidates.cv_url` and `resumes.resume_file_url` hold. The PRIMARY version is
mirrored to `candidates.cv_url`, so every existing CV path keeps reading the
same field. `extracted_text` caches the parsed text (a PDF is read once, not on
every match); `ai_reviews` keeps the AI fit reviews TA asked for, keyed by
requirement id, so a second look costs nothing.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base, TimestampMixin


class CandidateResume(Base, TimestampMixin):
    __tablename__ = "candidate_resumes"
    __table_args__ = (sa.UniqueConstraint("candidate_id", "file_url", name="uq_candidate_resumes_file"),)

    id = sa.Column(sa.Integer, primary_key=True)
    candidate_id = sa.Column(sa.Integer, sa.ForeignKey("candidates.id", ondelete="CASCADE"),
                             nullable=False, index=True)
    file_url = sa.Column(sa.String(1024), nullable=False)
    original_filename = sa.Column(sa.String(255), nullable=True)
    label = sa.Column(sa.String(120), nullable=True)
    #: cv (the record's CV) · application (uploaded for a position) · upload (added here)
    source = sa.Column(sa.String(32), nullable=True)
    file_sha256 = sa.Column(sa.String(64), nullable=True, index=True)
    file_size = sa.Column(sa.Integer, nullable=True)
    is_primary = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    extracted_text = sa.Column(sa.Text, nullable=True)
    ai_reviews = sa.Column(JSONB, nullable=True)
    uploaded_by_id = sa.Column(sa.Integer, nullable=True)
    uploaded_by_name = sa.Column(sa.String(255), nullable=True)
