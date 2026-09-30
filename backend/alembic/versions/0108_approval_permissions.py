"""Approval buttons become part of an Access Template / custom role (25 Sep 2026).

Reported: a Sales login filled and submitted a timesheet and was then offered
Approve / Reject on it — the Timesheets: Edit grant it needs to fill the sheet
also satisfied the approve gate. Approvals are now their own grant:

* `access_templates.action_access` / `custom_roles.action_access` (JSON list of
  approval-action keys, `services.action_permissions.APPROVAL_ACTIONS`).
  NULL = never configured → the action's role list decides (old behaviour for
  untagged templates). A custom role left NULL resolves to the approvals whose
  default names it, so "GM" keeps approving timesheets without a backfill.
* Backfill: every template TAGGED with a role gets the approvals whose code
  default names that role — "Default — Finance" converts Proformas, "Default —
  HR" decides leave, "Default — Sales" gets NO timesheet approval.
* The saved Users ▸ Action Permissions rows for the three timesheet actions are
  reset to the code default (GM): 0107 moved the decision to the GM, and a row
  saved before it (RMG + Sales on the dev box) kept Sales approving.

The role map below is a SNAPSHOT of the registry at this revision on purpose —
a migration must not change meaning when the application code moves on.

Revision ID: 0108
Revises: 0107
"""
from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

#: approval action -> roles whose template gets it by default (registry @ 0108).
_APPROVAL_DEFAULTS: dict[str, tuple[str, ...]] = {
    "timesheet.approve": ("GM",),
    "timesheet.reject": ("GM",),
    "timesheet.generate_invoice": ("GM",),
    "invoice.convert_proforma": ("Finance",),
    "invoice.revision.approve": ("Sales", "Sales_Head"),
    "credit_note.approve": ("Finance",),
    "opportunity.approve": ("Sales_Head",),
    "requirement.sales_head_approve": ("Sales_Head",),
    "requirement.engineering_approve": ("RMG",),
    "requirement.positions.approve": ("RMG",),
    "profile.rmg_screening": ("RMG",),
    "profile.sales_head_decision": ("Sales_Head",),
    "profile.budget_resolve": ("Sales", "Sales_Head"),
    "leave.approve": ("HR",),
    "leave.reject": ("HR",),
}

_RESET_ACTIONS = ("timesheet.approve", "timesheet.reject", "timesheet.generate_invoice")


def _columns(bind, table: str) -> set[str]:
    insp = sa.inspect(bind)
    if not insp.has_table(table):
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()

    have = _columns(bind, "access_templates")
    if have and "action_access" not in have:
        op.add_column("access_templates", sa.Column("action_access", sa.JSON(), nullable=True))
        rows = bind.execute(sa.text(
            "SELECT id, role FROM access_templates WHERE role IS NOT NULL AND role <> ''")).all()
        for tid, role in rows:
            actions = [k for k, roles in _APPROVAL_DEFAULTS.items() if role in roles]
            bind.execute(sa.text("UPDATE access_templates SET action_access = :a WHERE id = :id"),
                         {"a": json.dumps(actions), "id": tid})

    have = _columns(bind, "custom_roles")
    if have and "action_access" not in have:
        op.add_column("custom_roles", sa.Column("action_access", sa.JSON(), nullable=True))

    if sa.inspect(bind).has_table("action_permissions"):
        bind.execute(sa.text("DELETE FROM action_permissions WHERE action IN :keys")
                     .bindparams(sa.bindparam("keys", expanding=True)),
                     {"keys": list(_RESET_ACTIONS)})


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("custom_roles", "access_templates"):
        if "action_access" in _columns(bind, table):
            op.drop_column(table, "action_access")
