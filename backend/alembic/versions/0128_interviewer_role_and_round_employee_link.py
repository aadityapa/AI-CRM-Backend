"""The Interviewer role + rounds linked to the panel's employee row (7 Oct 2026).

User ask: eight new logins for the people who take the technical interviews;
each sees only the candidates whose interview they took and records that
round's feedback — nothing else. Two things make that true for data already
on file:

1. The **Interviewer** custom role is SEEDED (name, description, the single
   grant `my-interviews: edit`, no approvals) so Admin creates a login, ticks
   "Interviewer", links the Employees record, done. Skipped when a role of that
   name exists already (any case) — a name Admin may have taken for something
   else is never overwritten.

2. `interview_events.employee_id` is BACKFILLED for every technical round that
   still carries only the typed `interviewer` name: when exactly ONE active
   employee bears that full name (with or without the middle name, case- and
   space-insensitive) the round is linked to them; an ambiguous or unknown
   name is left alone — a guess would put a round on the wrong login.
   Postgres only (the CRM is Postgres only; the test SQLite never holds rows).

Revision ID: 0128
Revises: 0127
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None

ROLE_NAME = "Interviewer"
ROLE_DESCRIPTION = ("Panel member: sees only the candidates whose technical interview they take "
                    "and records that round's feedback. Nothing else.")
TAB_ACCESS = {"my-interviews": "edit"}
PANEL_KINDS = ("L1_Interview", "L2_F2F", "L3_Interview", "L4_Interview")

# One name per active employee, in both spellings, lower-cased and squeezed.
_NAMES = """
    SELECT id,
           lower(regexp_replace(btrim(concat_ws(' ', first_name, middle_name, last_name)), '\\s+', ' ', 'g')) AS full_name,
           lower(regexp_replace(btrim(concat_ws(' ', first_name, last_name)), '\\s+', ' ', 'g')) AS short_name
    FROM employees WHERE is_active
"""


def upgrade() -> None:
    bind = op.get_bind()
    exists = bind.execute(
        sa.text("SELECT id FROM custom_roles WHERE lower(name) = lower(:n)"), {"n": ROLE_NAME}
    ).scalar()
    if exists is None:
        bind.execute(
            sa.text("INSERT INTO custom_roles (name, description, is_active, tab_access, field_access, "
                    "action_access, created_at, updated_at) "
                    "VALUES (:n, :d, :active, :tabs, :fields, :actions, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"),
            {"n": ROLE_NAME, "d": ROLE_DESCRIPTION, "active": True,
             "tabs": json.dumps(TAB_ACCESS), "fields": json.dumps({}), "actions": json.dumps([])},
        )
    if bind.dialect.name != "postgresql":
        return
    names: dict[str, list[int]] = {}
    for emp_id, full, short in bind.execute(sa.text(_NAMES)).fetchall():
        for key in {full, short}:
            if key:
                names.setdefault(key, []).append(emp_id)
    unique = {k: v[0] for k, v in names.items() if len(v) == 1}
    rows = bind.execute(sa.text(
        "SELECT id, lower(regexp_replace(btrim(interviewer), '\\s+', ' ', 'g')) FROM interview_events "
        "WHERE employee_id IS NULL AND COALESCE(interviewer, '') <> '' AND kind IN :kinds"
    ).bindparams(sa.bindparam("kinds", expanding=True)), {"kinds": list(PANEL_KINDS)}).fetchall()
    for event_id, name in rows:
        emp_id = unique.get(name or "")
        if emp_id is None:
            continue
        bind.execute(sa.text("UPDATE interview_events SET employee_id = :e WHERE id = :i"),
                     {"e": emp_id, "i": event_id})


def downgrade() -> None:
    # The seeded role stays (members may hold it); the links are facts.
    pass
