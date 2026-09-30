"""candidate_profiles: the CTC HR offers at Pre-Onboarding (30 Sep 2026).

User rule: after the HR discussion / HR round, at Pre-Onboarding, HR records the
CTC actually OFFERED to the candidate. It is HR's own figure — distinct from the
candidate's expected CTC (TA), the Sales Head's approved terms (`offer_history`)
and the slab budget — and it is what the Employees record takes as `current_ctc`
at Joined. Only HR (and Admin/CEO) can see or set it; `services/hr_offer.py` is
the one place that reads and writes these four columns.

Revision ID: 0116
Revises: 0115
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None

TABLE = "candidate_profiles"

_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("hr_offered_ctc", sa.Numeric(14, 2)),
    ("hr_offered_at", sa.DateTime(timezone=True)),
    ("hr_offered_by", sa.Integer()),
    ("hr_offer_note", sa.Text()),
]


def _existing(bind) -> set[str]:
    insp = sa.inspect(bind)
    if not insp.has_table(TABLE):
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    have = _existing(op.get_bind())
    if not have:
        return
    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    have = _existing(op.get_bind())
    for name, _ in _COLUMNS:
        if name in have:
            op.drop_column(TABLE, name)
