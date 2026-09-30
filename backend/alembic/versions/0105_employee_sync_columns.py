"""employees: the fields HR fills at Joined, plus updated_at (22 Sep 2026).

Reported: an internal employee clears the customer rounds, HR fills the Workflow
section and sets Joined. `_sync_employee_from_joined_profile` already updated the
EXISTING employee record — but only the fields that had somewhere to go. Offer
letter reference, customer onboarding date, relocation and the resignation
certificate had no column, so HR's work stayed on the candidate profile and was
lost to anyone looking at the employee.

`updated_at` exists so "who changed recently" is answerable at all. `employees`
predates `TimestampMixin` and had neither timestamp, which is why the list could
only ever sort by `date_of_joining` — and a 2022 internal employee sorts near the
bottom the moment they are updated, which is what the user actually noticed.

`created_at` is added alongside it: adding one half of the pair would leave the
table inconsistent with every other CRM table for no reason.

Revision ID: 0105
Revises: 0104
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None

TABLE = "employees"

#: New columns, in the order they are added. `work_location` is deliberately
#: absent — it already exists and simply was never populated.
_COLUMNS: list[tuple[str, sa.types.TypeEngine]] = [
    ("offer_letter_reference", sa.String(255)),
    ("resignation_certificate_url", sa.String(1024)),
    ("customer_onboarding_date", sa.Date()),
    ("relocation_applicable", sa.Boolean()),
]


def _existing(bind) -> set[str]:
    insp = sa.inspect(bind)
    if not insp.has_table(TABLE):
        return set()
    return {c["name"] for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    if not have:
        return

    for name, type_ in _COLUMNS:
        if name not in have:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # Timestamps are NOT NULL with a server default, so existing rows get a
    # value without a backfill pass. `onupdate` is ORM-side (TimestampMixin),
    # which is where every other CRM table maintains it too.
    for name in ("created_at", "updated_at"):
        if name not in have:
            op.add_column(TABLE, sa.Column(
                name, sa.DateTime(timezone=True),
                server_default=sa.func.now(), nullable=False))


def downgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for name in ("updated_at", "created_at", *[c for c, _ in reversed(_COLUMNS)]):
        if name in have:
            op.drop_column(TABLE, name)
