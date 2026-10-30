"""Fill the Employees records of past joiners with what HR entered before Joined
(28 Oct 2026, user report).

A NEW joiner's Employees row was created with department, designation, joining
date and CTC only — the offer letter reference, customer onboarding date,
relocation and resignation certificate HR filled on the candidate profile were
copied only when the person already had an Employees row. The code now applies
one map on both paths (`candidate_profiles.apply_profile_to_employee`); this
fills the rows created before the fix.

Only BLANK employee columns are filled — a value HR has since typed on the
Employee page is never overwritten. Portable SQL (correlated subqueries), so it
runs on SQLite as well as Postgres. Downgrade is a no-op (nothing to undo
safely: the values are what HR entered).

Revision ID: 0150
Revises: 0149
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None

#: (profile column, employee column) — frozen at this revision.
_PAIRS = (
    ("department_id", "department_id"),
    ("designation_id", "designation_id"),
    ("karnex_onboarding_date", "date_of_joining"),
    ("total_experience_years", "experience_years"),
    ("offer_letter_reference", "offer_letter_reference"),
    ("resignation_certificate_url", "resignation_certificate_url"),
    ("customer_onboarding_date", "customer_onboarding_date"),
    ("relocation_applicable", "relocation_applicable"),
)


def _cols(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)} if insp.has_table(table) else set()


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    emp_cols = _cols(insp, "employees")
    prof_cols = _cols(insp, "candidate_profiles")
    if "candidate_profile_id" not in emp_cols:
        return
    for src, dest in _PAIRS:
        if src not in prof_cols or dest not in emp_cols:
            continue
        result = bind.execute(sa.text(
            f"UPDATE employees SET {dest} = ("
            f"  SELECT p.{src} FROM candidate_profiles p WHERE p.id = employees.candidate_profile_id) "
            f"WHERE {dest} IS NULL AND candidate_profile_id IS NOT NULL "
            f"AND EXISTS (SELECT 1 FROM candidate_profiles p "
            f"            WHERE p.id = employees.candidate_profile_id AND p.{src} IS NOT NULL)"))
        if result.rowcount:
            print(f"[0150] employees.{dest}: filled {result.rowcount} row(s) from the candidate profile")


def downgrade() -> None:
    pass
