"""CLI bootstrap: assign a CRM role to an existing user.

Usage (from backend/):
    python assign_crm_role.py <username> <RoleName>

Example — create the first Admin:
    python assign_crm_role.py pavan Admin

Looks up registration_data by username (case-insensitive), upserts the
roles/user_roles rows. Requires CRM_DATABASE_URL or AUTH_DB_URL (PostgreSQL).
"""
from __future__ import annotations

import sys

import sqlalchemy as sa
from sqlalchemy import select

from crm_db import CrmNotConfiguredError, get_session_factory
from models import Role, RoleName, UserRole

VALID_ROLE_NAMES = [m.value for m in RoleName]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python assign_crm_role.py <username> <RoleName>")
        print(f"Valid roles: {', '.join(VALID_ROLE_NAMES)}")
        return 2

    username, role_name = argv[0].strip(), argv[1].strip()

    member = next((m for m in RoleName if m.value.lower() == role_name.lower()), None)
    if member is None:
        print(f"ERROR: Unknown role '{role_name}'. Valid roles: {', '.join(VALID_ROLE_NAMES)}")
        return 1

    try:
        session = get_session_factory()()
    except CrmNotConfiguredError as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        row = session.execute(
            sa.text("SELECT id, username FROM registration_data WHERE LOWER(username) = LOWER(:u)"),
            {"u": username},
        ).first()
        if not row:
            print(f"ERROR: No user found in registration_data with username '{username}'.")
            return 1
        user_id, real_username = int(row[0]), str(row[1])

        role = session.execute(select(Role).where(Role.name == member)).scalar_one_or_none()
        if role is None:
            role = Role(name=member)
            session.add(role)
            session.flush()

        existing = session.execute(
            select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role.id)
        ).scalar_one_or_none()
        if existing is not None:
            print(f"OK: User '{real_username}' (id={user_id}) already has role '{member.value}'. Nothing to do.")
            return 0

        session.add(UserRole(user_id=user_id, role_id=role.id))
        session.commit()
        print(f"SUCCESS: Assigned CRM role '{member.value}' to user '{real_username}' (id={user_id}).")
        return 0
    except Exception as exc:  # pragma: no cover - CLI safety net
        session.rollback()
        print(f"ERROR: {exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
