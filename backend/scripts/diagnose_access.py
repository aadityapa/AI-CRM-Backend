#!/usr/bin/env python3
"""Why can (or can't) this user see a tab? Read-only access diagnosis.

Prints, for each user given: their CRM roles, the assigned access template,
the per-user legacy override, and the EFFECTIVE access exactly as the server
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
from models import AccessTemplate, Role, UserProfile, UserRole  # noqa: E402
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

    profile = db.execute(
        select(UserProfile).where(UserProfile.user_id == uid)
    ).scalars().first()
    if profile is None:
        print("  profile: none (no template, no override)")
    else:
        if profile.access_template_id:
            t = db.get(AccessTemplate, profile.access_template_id)
            if t is None:
                print(f"  template: ✗ id={profile.access_template_id} points at a DELETED template")
            else:
                print(f"  template: '{t.name}' (id={t.id}) is_active={t.is_active}")
                if not t.is_active:
                    print("    ✗ INACTIVE template still RESTRICTS: the user gets its id but")
                    print("      zero tabs from it — every non-overridden tab is hidden.")
                print(f"    tab_access: {t.tab_access}")
        else:
            print("  template: none assigned")
        print(f"  per-user override (user_profiles.tab_access): {profile.tab_access or 'none'}")

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
            print("      Fix: add it to the template (or the user's override) and re-save.")
        else:
            print(f"    view ✓ / edit {'✓' if mode == 'edit' else '✗ (view-only)'}")
        print("\n  Remember: templates only NARROW. Role gates still apply on top —")
        print("  e.g. project WRITES require Sales_Head/Finance regardless of template.")


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
