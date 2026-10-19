"""Take deactivated logins off every position's sourcing team (14 Oct 2026).

Reported: Sweety Falke left, her account was deactivated, and she still read as
a sourcing TA on C-2026-00101 (and the Assign TAs dialog counted her). From now
on `users_admin.set_user_active` releases a deactivated login at once
(`requirement_assignments.release_user`); this migration does the same for the
accounts deactivated before that, writing the same `TA_ASSIGNED` activity row on
each position. Downgrade is a no-op (nothing to put back — they cannot sign in).

Revision ID: 0134
Revises: 0133
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0134"
down_revision = "0133"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(sa.text(
        "SELECT a.id, a.requirement_id, a.user_id, a.user_name, a.assigned_by "
        "FROM requirement_ta_assignments a "
        "JOIN registration_data u ON u.id = a.user_id "
        "WHERE u.is_active IS FALSE"
    )).all()
    for aid, req_id, uid, name, by in rows:
        who = name or f"User #{uid}"
        bind.execute(sa.text(
            "INSERT INTO requirement_activity_log (requirement_id, user_id, action_type, comment) "
            "VALUES (:r, :u, 'TA_ASSIGNED', :c)"
        ), {"r": req_id, "u": by or uid,
            "c": f"TA removed {who} — account deactivated (clean-up)"})
        bind.execute(sa.text("DELETE FROM requirement_ta_assignments WHERE id = :i"), {"i": aid})


def downgrade() -> None:
    pass
