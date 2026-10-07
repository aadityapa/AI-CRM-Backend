"""Access audit log (7 Oct 2026) — every change to who may do what.

Before this, role edits, the Access dropdown, tab grants, template assignment,
password resets, (de)activation and deletion wrote nothing: "who gave X Finance
access?" had no answer. One row per change, written by `services/access_audit.py`.

Deliberately NO foreign keys on the two user columns: the log must outlive the
account it describes (a deleted user's history is exactly what an auditor asks
for), so the names are snapshotted next to the ids. `subject_*` names what the
change was about when it is not a single user (a custom role, an access template,
an approval rule).
"""
from __future__ import annotations

import sqlalchemy as sa

from models.base import Base


class UserAccessLog(Base):
    __tablename__ = "user_access_log"

    id = sa.Column(sa.Integer, primary_key=True)
    #: The login whose access changed (None for a role / template / rule edit).
    target_user_id = sa.Column(sa.Integer, nullable=True, index=True)
    target_name = sa.Column(sa.String(200), nullable=True)
    actor_id = sa.Column(sa.Integer, nullable=True, index=True)
    actor_name = sa.Column(sa.String(200), nullable=True)
    #: Key from services.access_audit.ACTIONS ("user.roles", "user.deactivated" …).
    action = sa.Column(sa.String(48), nullable=False, index=True)
    #: "role" · "template" · "approval" when the change was not about one user.
    subject_type = sa.Column(sa.String(24), nullable=True)
    subject_id = sa.Column(sa.String(64), nullable=True)
    subject_name = sa.Column(sa.String(200), nullable=True)
    #: One readable line ("Roles: Sales → Sales, Sales Manager").
    summary = sa.Column(sa.Text, nullable=True)
    before = sa.Column(sa.JSON, nullable=True)
    after = sa.Column(sa.JSON, nullable=True)
    reason = sa.Column(sa.Text, nullable=True)
    created_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True)
