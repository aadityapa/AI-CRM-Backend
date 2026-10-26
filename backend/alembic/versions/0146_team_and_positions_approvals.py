"""Change positions / Change TAs / Assign Hiring Manager are RMG / GM decisions
(23 Oct 2026, user report with a screenshot of a TA login).

* `requirement.positions.request` was a MANAGE button implied by Requirements:
  Edit (0124 backfilled it into every list whose template had that grant) — a
  TA needs Requirements: Edit to work a position, so every TA saw "Change
  positions". It is an APPROVAL now (defaults RMG · GM · Sales · Sales Head).
* New APPROVAL `requirement.team.assign` (RMG · GM): Change TAs + Assign Hiring
  Manager. It used to be `gated_write("requirements", …)`, the same TA grant.

Backfill, like 0111: every SAVED Approvals list of a template TAGGED / custom
role NAMED with one of the default roles gets the keys it lacks (the Sales
Manager role, which acts as Sales, also gets the positions key). And the
positions key is TAKEN OUT of the saved lists of templates tagged TA / HR /
Finance and of the Interviewer role — the only removal, because those lists
received it from the 0124 button backfill, never from an admin's choice.
NULL lists need nothing: they resolve to the code defaults.

Snapshots below are frozen at this revision on purpose (see 0108).

Revision ID: 0146
Revises: 0145
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None

#: approval action -> roles whose saved list gets it (registry @ 0146; only the
#: entries this revision changed or added).
_APPROVAL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "requirement.positions.request": ("RMG", "GM", "Sales", "Sales_Head"),
    "requirement.team.assign": ("RMG", "GM"),
}
#: names beyond the defaults that act as one of those roles.
_ALSO: dict[str, tuple[str, ...]] = {
    "requirement.positions.request": ("Sales Manager", "Hiring Manager"),
}
#: role tags / role names whose saved list loses the positions key.
_STRIP_FROM: tuple[str, ...] = ("TA", "HR", "Finance", "Interviewer")
_STRIP_KEYS: tuple[str, ...] = ("requirement.positions.request",)


def _loads(raw):
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def plan(name: str | None, current: list) -> list:
    """The new list for one row (PURE — the test runs it)."""
    nm = (name or "").strip()
    low = nm.lower()
    out = list(current)
    if low in {s.lower() for s in _STRIP_FROM}:
        out = [k for k in out if k not in _STRIP_KEYS]
    for key, roles in _APPROVAL_DEFAULTS.items():
        names = {r.lower() for r in roles + _ALSO.get(key, ())}
        if low in names and key not in out:
            out.append(key)
    return out


def _member_roles(bind, insp) -> dict[int, set[str]]:
    """template id -> built-in roles of the people assigned to it (0124's rule)."""
    out: dict[int, set[str]] = {}
    if not (insp.has_table("user_profiles") and insp.has_table("user_roles") and insp.has_table("roles")):
        return out
    if "access_template_id" not in {c["name"] for c in insp.get_columns("user_profiles")}:
        return out
    for tid, role in bind.execute(sa.text(
            "SELECT p.access_template_id, r.name FROM user_profiles p "
            "JOIN user_roles ur ON ur.user_id = p.user_id "
            "JOIN roles r ON r.id = ur.role_id "
            "WHERE p.access_template_id IS NOT NULL")).all():
        out.setdefault(int(tid), set()).add(str(role))
    return out


def untagged_name(members: set[str] | None) -> str | None:
    """An UNTAGGED template used only by TA / HR / Finance people is treated as
    theirs for the strip (PURE). Anything else keeps its list."""
    if members and all(m in _STRIP_FROM for m in members):
        return "TA"
    return None


def _backfill(bind, table: str, name_col: str) -> None:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return
    if "action_access" not in {c["name"] for c in insp.get_columns(table)}:
        return
    members = _member_roles(bind, insp) if table == "access_templates" else {}
    rows = bind.execute(sa.text(
        f"SELECT id, {name_col}, action_access FROM {table} "
        "WHERE action_access IS NOT NULL")).all()
    for row_id, name, raw in rows:
        current = _loads(raw)
        if current is None:
            continue
        if table == "access_templates" and not (name or "").strip():
            name = untagged_name(members.get(int(row_id)))
        new = plan(name, current)
        if new != current:
            bind.execute(sa.text(f"UPDATE {table} SET action_access = :a WHERE id = :id"),
                         {"a": json.dumps(new), "id": row_id})


def upgrade() -> None:
    bind = op.get_bind()
    _backfill(bind, "access_templates", "role")
    _backfill(bind, "custom_roles", "name")


def downgrade() -> None:
    # Nothing is put back: a downgrade must never silently hand a TA a button.
    pass
