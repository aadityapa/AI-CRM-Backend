#!/usr/bin/env python3
"""Why can (or can't) this user see a tab? Read-only access diagnosis.

Prints, for each user given: their CRM roles and which of them carry their
own permissions (Access Templates were merged into Roles on 27 Oct 2026), the
per-user legacy override, and the EFFECTIVE access exactly as the server
resolves it (`services.access_templates.effective_access` — the same function
every `require_access` gate calls, so this cannot drift from enforcement).

Usage:
  cd backend
  python scripts/diagnose_access.py balasaheb.suryawanshi pavan.majeti
  python scripts/diagnose_access.py someone@karnex.in --tab projects
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import Role, UserProfile, UserRole  # noqa: E402
from services.access_templates import _held_role_rows  # noqa: E402
from services.access_templates import effective_access  # noqa: E402
from services.access_registry import TABS  # noqa: E402


def _find_user(db, needle: str) -> tuple[int, str, str, bool] | None:
    row = db.execute(text(
        "SELECT id, username, email, is_active FROM registration_data "
        "WHERE LOWER(username) = LOWER(:n) OR LOWER(email) = LOWER(:n) "
        "OR LOWER(email) LIKE LOWER(:like) LIMIT 1"
    ), {"n": needle, "like": f"{needle}%@%"}).first()
    return tuple(row) if row else None  # type: ignore[return-value]


def diagnose(db, needle: str, tab: str | None) -> None:
    print(f"\n{'=' * 62}\nUSER LOOKUP: {needle}")
    found = _find_user(db, needle)
    if not found:
        print("  ✗ No user in registration_data matches that username/email.")
        return
    uid, username, email, is_active = found
    print(f"  id={uid}  username={username}  email={email}  active={bool(is_active)}")
    if not is_active:
        print("  ✗ USER IS DEACTIVATED — login itself will fail (403).")

    roles = set(db.execute(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == uid)
    ).scalars().all())
    roles = {getattr(r, "value", r) for r in roles}
    print(f"  CRM roles: {sorted(roles) or '✗ NONE — the whole CRM will refuse them'}")

    for name, is_custom, tabs, _fields, actions in _held_role_rows(db, uid):
        kind = "custom" if is_custom else "built-in"
        if tabs:
            print(f"  role '{name}' ({kind}): {len(tabs)} tab(s) {tabs}")
        else:
            print(f"  role '{name}' ({kind}): no permissions set — the product's role rules")
        print(f"    buttons: {'role lists decide' if actions is None else actions}")

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == uid)
    ).scalars().first()
    print(f"  per-user override (user_profiles.tab_access): {(profile.tab_access if profile else None) or 'none'}")

    acc = effective_access(db, uid, roles)
    print(f"  EFFECTIVE (what every require_access gate sees):")
    print(f"    source={acc['source']}  full={acc['full']}")
    if acc["visible_tabs"] is None:
        print("    visible_tabs=None → UNRESTRICTED (role defaults decide everything)")
    else:
        print(f"    visible_tabs={acc['visible_tabs']}")
        hidden = sorted(set(TABS) - set(acc["tabs"]))
        if hidden:
            print(f"    hidden tabs: {hidden}")

    if tab:
        mode = acc["tabs"].get(tab)
        print(f"\n  VERDICT for tab '{tab}':")
        if acc["full"] or acc["visible_tabs"] is None:
            print("    view ✓ / edit ✓ (unrestricted)")
        elif mode is None:
            print(f"    ✗ NOT GRANTED — '{tab}' is not in the effective tab map.")
            print("      Fix: add it to one of their roles (Access Control ▸ Roles) and save.")
        else:
            print(f"    view ✓ / edit {'✓' if mode == 'edit' else '✗ (view-only)'}")
        print("\n  Remember: role permissions only NARROW. Hard role gates still apply on top.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose a user's effective tab access")
    parser.add_argument("users", nargs="+", help="usernames or emails")
    parser.add_argument("--tab", default=None, help="registry tab key, e.g. projects")
    args = parser.parse_args()
    db = get_session_factory()()
    try:
        for needle in args.users:
            diagnose(db, needle, args.tab)
    finally:
        db.close()
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
