"""`custom_roles.department` — roles shown department-wise (7 Oct 2026).

User ask: "lots of roles now, more logins coming — divide them department
wise." A built-in role's department is fixed in code
(`access_registry.BUILTIN_ROLE_DEPARTMENT`); a custom role carries its own.
Existing roles are placed ONCE by the words in their name
(`access_registry.guess_department`: "Sales Manager" → sales, "GM" →
engineering, "Interviewer" → panel …); Admin can move any of them from the
Roles tab. Unknown names read as "other".

Revision ID: 0129
Revises: 0128
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None

# A frozen copy of access_registry._DEPARTMENT_HINTS at the time of writing —
# a migration must not change its mind when the live rule is edited later.
_HINTS = (
    ("panel", ("interview", "panel")),
    ("sales", ("sales", "account", "bd", "business")),
    ("recruitment", ("recruit", "talent", "sourcing", " ta")),
    ("engineering", ("rmg", "engineer", "gm", "delivery", "tech")),
    ("hr", ("hr", "people", "onboard")),
    ("finance", ("finance", "account", "billing", "invoice")),
    ("leadership", ("ceo", "director", "head", "chief", "founder", "admin")),
)


def _guess(name: str) -> str:
    low = f" {(name or '').strip().lower()} "
    for key, words in _HINTS:
        if any(w in low for w in words):
            return key
    return "other"


def upgrade() -> None:
    op.add_column("custom_roles", sa.Column("department", sa.String(length=40), nullable=True))
    bind = op.get_bind()
    for role_id, name in bind.execute(
            sa.text("SELECT id, name FROM custom_roles WHERE department IS NULL")).fetchall():
        bind.execute(sa.text("UPDATE custom_roles SET department = :d WHERE id = :i"),
                     {"d": _guess(name), "i": role_id})


def downgrade() -> None:
    op.drop_column("custom_roles", "department")
