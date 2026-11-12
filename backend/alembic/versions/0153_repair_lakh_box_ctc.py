"""Repair CTC figures typed in rupees into a lakh box (6 Nov 2026).

The careers form added " LPA" to whatever the candidate typed, and people type
rupees: "700000 LPA" was stored as ₹70,000 crore, so Applied Candidates read
"Over budget · ₹1000000.0 L vs ₹25.4 L". The code now reads such a figure as
rupees (services/ctc.parse_ctc_to_rupees); this repairs what is already stored.

* candidates / candidate_profiles current_ctc + expected_ctc above ₹50 crore are
  divided by 1,00,000 when the result is a sane rupee figure (≥ ₹10,000).
* resumes from the careers page / apply link: application_details current_ctc /
  expected_ctc are rewritten as "7 LPA" (frozen copy of the parse rule below).

Downgrade is a no-op (the old values were wrong).

Revision ID: 0153
Revises: 0152
"""
from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "0153"
down_revision = "0152"
branch_labels = None
depends_on = None

MAX_RUPEES = 500_000_000.0
RUPEES_FROM = 10_000.0
PORTALS = ("Careers Page", "Apply Link")


def _repair(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if num <= MAX_RUPEES:
        return None
    fixed = num / 100_000
    return round(fixed, 2) if RUPEES_FROM <= fixed <= MAX_RUPEES else None


def _rupees(text: str):
    """Frozen copy of services/ctc.parse_ctc_to_rupees (6 Nov 2026)."""
    text = str(text or "").strip().lower()
    m = re.search(r"(\d+(?:[.,]\d+)*)", text.replace(" ", ""))
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if num <= 0:
        return None
    if re.search(r"\bcr\b|crore", text):
        num *= 10_000_000
    elif re.search(r"lpa|lakh|lacs?\b|\bl\b|\dl\b", text):
        if num >= RUPEES_FROM:
            pass
        elif num >= 1_000:
            return None
        else:
            num *= 100_000
    elif re.search(r"\bk\b|thousand|\dk\b", text):
        num *= 1_000
    elif num < 1_000:
        num *= 100_000
    return None if num > MAX_RUPEES else num


def _display(raw):
    rupees = _rupees(raw)
    if rupees is None:
        return None
    text = f"{round(rupees / 100_000, 2):.2f}".rstrip("0").rstrip(".")
    return f"{text} LPA"


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("candidates", "candidate_profiles"):
        for col in ("current_ctc", "expected_ctc"):
            rows = bind.execute(sa.text(
                f"SELECT id, {col} FROM {table} WHERE {col} > :cap"), {"cap": MAX_RUPEES}).all()
            fixed = 0
            for rid, value in rows:
                new = _repair(value)
                if new is not None:
                    bind.execute(sa.text(f"UPDATE {table} SET {col} = :v WHERE id = :i"),
                                 {"v": new, "i": rid})
                    fixed += 1
            if rows:
                print(f"[0153] {table}.{col}: {fixed} of {len(rows)} repaired")

    resumes = sa.table("resumes", sa.column("id", sa.Integer),
                       sa.column("source_portal", sa.String),
                       sa.column("application_details", sa.JSON))
    rows = bind.execute(sa.select(resumes.c.id, resumes.c.application_details)
                        .where(resumes.c.source_portal.in_(PORTALS))).all()
    changed = 0
    for rid, details in rows:
        if not isinstance(details, dict):
            continue
        new = dict(details)
        for key in ("current_ctc", "expected_ctc"):
            if new.get(key):
                shown = _display(new[key])
                if shown and shown != new[key]:
                    new[key] = shown
        if new != details:
            bind.execute(resumes.update().where(resumes.c.id == rid)
                         .values(application_details=new))
            changed += 1
    if changed:
        print(f"[0153] resumes: {changed} application CTC figures rewritten in lakhs")


def downgrade() -> None:
    pass
