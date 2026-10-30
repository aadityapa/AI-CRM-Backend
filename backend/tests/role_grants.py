"""Test helper — give a user a role that carries permissions.

Access Templates were merged into Roles on 27 Oct 2026; the tests that used to
assign a template now give the user a custom role carrying the same grants
(`give_role`) or set a built-in role's own permissions (`set_builtin`).
"""
from __future__ import annotations

import itertools

_seq = itertools.count(1)


def give_role(db, uid: int, tab_access: dict, field_access: dict | None = None,
              action_access: list[str] | None = None, *, name: str | None = None,
              is_active: bool = True):
    from models.custom_roles import CustomRole, UserCustomRole
    role = CustomRole(name=name or f"Perm {next(_seq)}", is_active=is_active,
                      tab_access=dict(tab_access), field_access=dict(field_access or {}),
                      action_access=action_access)
    db.add(role)
    db.flush()
    db.add(UserCustomRole(user_id=uid, custom_role_id=role.id))
    db.commit()
    return role


def set_builtin(db, uid: int, role_name: str, tab_access: dict | None,
                field_access: dict | None = None, action_access: list[str] | None = None):
    """Give `uid` the built-in role and set that role's own permissions."""
    from sqlalchemy import select
    from models.rbac import Role, RoleName, UserRole
    member = next(r for r in RoleName if r.value == role_name)
    role = db.execute(select(Role).where(Role.name == member)).scalars().first()
    if role is None:
        role = Role(name=member)
        db.add(role)
        db.flush()
    role.tab_access = tab_access
    role.field_access = field_access
    role.action_access = action_access
    if not db.execute(select(UserRole).where(UserRole.user_id == uid, UserRole.role_id == role.id)).first():
        db.add(UserRole(user_id=uid, role_id=role.id))
    db.commit()
    return role
