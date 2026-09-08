"""Karnex — why didn't X get the timesheet email?

Answers it from the actual data rather than guessing:
  - which roles the running code notifies (read out of timesheets.py)
  - who holds those roles right now, and who is active
  - the exact recipient list a submit would produce
  - what the outbox actually queued for the last few submits
  - for any address you name, why it is or isn't on the list

    cd F:\\AI-Interview-Model-B-V2
    python karnex_check_recipients.py
    python karnex_check_recipients.py balasaheb.suryawanshi@karnex.in karan.singh@karnex.in

Read-only. Changes nothing, sends nothing.
"""
from __future__ import annotations

import io
import os
import re
import sys

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


def repo_root() -> str:
    here = os.path.abspath(os.getcwd())
    if os.path.isfile(os.path.join(here, "backend", "main.py")):
        return here
    parent = os.path.dirname(here)
    if os.path.isfile(os.path.join(parent, "backend", "main.py")):
        return parent
    print("ERROR: run this from F:\\AI-Interview-Model-B-V2 (or its backend folder).")
    raise SystemExit(2)


def notified_roles(root: str) -> tuple[list[str], str]:
    """Read the role tuple straight out of the source, so we report what the code
    ACTUALLY does — not what anyone believes it does."""
    path = os.path.join(root, "backend", "routers", "crm", "timesheets.py")
    if not os.path.exists(path):
        return [], "(timesheets.py not found)"
    src = io.open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"^(TS_(?:APPROVER|NOTIFY)_ROLES)\s*=\s*\(([^)]*)\)", src, re.M)
    if not m:
        return [], "(no TS_APPROVER_ROLES / TS_NOTIFY_ROLES found — is the change applied?)"
    roles = re.findall(r'"([^"]+)"', m.group(2))
    return roles, m.group(1)


def load_env(root: str) -> None:
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8", errors="replace").read().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    wanted = [a.strip().lower() for a in sys.argv[1:] if "@" in a]
    root = repo_root()
    print("Karnex — timesheet notification recipients\n")
    print(f"Repo: {root}\n")

    roles, varname = notified_roles(root)
    print("=== 1. What the code notifies ===")
    if roles:
        print(f"{OK} {varname} = {tuple(roles)}")
    else:
        print(f"{BAD} {varname}")
        print("       Without that tuple the submit notification cannot run at all.")

    load_env(root)
    sys.path.insert(0, os.path.join(root, "backend"))
    os.chdir(os.path.join(root, "backend"))

    try:
        import sqlalchemy as sa
        from crm_db import get_session_factory
    except Exception as exc:
        print(f"\n{BAD} cannot load the backend: {type(exc).__name__}: {exc}")
        return 1

    try:
        db = get_session_factory()()
    except Exception as exc:
        print(f"\n{BAD} cannot reach the database: {exc}")
        return 1

    try:
        _run_checks(db, roles, wanted)
    except Exception as exc:
        print(f"\n{BAD} database error: {type(exc).__name__}: {str(exc)[:200]}")
        print("       A SQLAlchemy session connects lazily, so this is the first real")
        print("       contact with Postgres. Check CRM_DATABASE_URL / AUTH_DB_URL in .env")
        print("       and that the database is running.")
        return 1
    finally:
        db.close()

    print("\n" + "=" * 66)
    print("Send this whole output back to Claude and it will tell you the fix.")
    return 0


def _run_checks(db, roles, wanted) -> None:
    import sqlalchemy as sa
    if True:
        print("\n=== 2. Who holds each CRM role ===")
        rows = db.execute(sa.text("""
            SELECT ro.name::text AS role, r.id, r.full_name, r.email,
                   COALESCE(r.is_active, TRUE) AS active
            FROM user_roles ur
            JOIN roles ro ON ro.id = ur.role_id
            JOIN registration_data r ON r.id = ur.user_id
            ORDER BY ro.name, r.full_name
        """)).all()
        by_role: dict[str, list] = {}
        for role, uid, name, email, active in rows:
            by_role.setdefault(role, []).append((uid, name, email, active))

        all_roles = db.execute(sa.text("SELECT name::text FROM roles ORDER BY name")).scalars().all()
        for role in all_roles:
            holders = by_role.get(role, [])
            mark = " <- notified" if role in roles else ""
            if not holders:
                print(f"  {role:<12} (nobody){mark}")
                continue
            print(f"  {role:<12} {len(holders)}{mark}")
            for uid, name, email, active in holders:
                flag = "" if active else "   [INACTIVE - skipped]"
                print(f"               {(name or '(no name)'):<32} {email}{flag}")

        print("\n=== 3. Exact recipient list for a timesheet submit ===")
        if not roles:
            print(f"{BAD} cannot compute — no role tuple in the source")
        else:
            recips = db.execute(sa.text("""
                SELECT DISTINCT r.full_name, r.email
                FROM user_roles ur
                JOIN roles ro ON ro.id = ur.role_id
                JOIN registration_data r ON r.id = ur.user_id
                WHERE ro.name::text = ANY(:roles) AND COALESCE(r.is_active, TRUE)
                ORDER BY r.full_name
            """), {"roles": roles}).all()
            if recips:
                for name, email in recips:
                    print(f"  {(name or '(no name)'):<32} {email}")
                print(f"\n{OK} {len(recips)} recipient(s). The submitter is excluded from their own.")
            else:
                print(f"{BAD} nobody — a submit would email no one")

        print("\n=== 4. Addresses you asked about ===")
        if not wanted:
            print("  (none given — pass them as arguments to check specific people)")
        for addr in wanted:
            # Only columns guaranteed by auth_db's DDL. A missing optional column
            # must not take the whole diagnostic down.
            row = db.execute(sa.text("""
                SELECT id, full_name, email, COALESCE(is_active, TRUE)
                FROM registration_data WHERE LOWER(email) = :e
            """), {"e": addr}).first()
            print(f"\n  {addr}")
            if row is None:
                print(f"{BAD} no login account with this address")
                print("       Notifications resolve through user_roles -> registration_data, so an")
                print("       address that is not a user account can never be notified.")
                continue
            uid, name, _email, active = row
            try:
                legacy_role = db.execute(sa.text(
                    "SELECT role FROM registration_data WHERE id = :u"), {"u": uid}).scalar()
            except Exception:
                legacy_role = "?"
            print(f"     user id {uid}, name '{name}', legacy role '{legacy_role}', "
                  f"active={active}")
            held = db.execute(sa.text("""
                SELECT ro.name::text FROM user_roles ur
                JOIN roles ro ON ro.id = ur.role_id WHERE ur.user_id = :u
                ORDER BY ro.name
            """), {"u": uid}).scalars().all()
            print(f"     CRM roles: {held or '(NONE)'}")
            if not active:
                print(f"{BAD} account is inactive — skipped by every notification")
            elif not held:
                print(f"{BAD} holds no CRM role row.")
                print("       Being an admin through the legacy login is NOT enough — the")
                print("       notification query joins user_roles, so a role badge must exist")
                print("       in CRM -> Users.")
            elif not set(held) & set(roles):
                print(f"{BAD} holds {held} but the code notifies {roles} — no overlap")
            else:
                print(f"{OK} should receive timesheet submit notifications")

        print("\n=== 5. What the outbox actually queued (last 15) ===")
        try:
            rows = db.execute(sa.text("""
                SELECT id, status::text, to_email, attempts, created_at, last_error
                FROM email_outbox
                WHERE event LIKE 'timesheet%'
                ORDER BY id DESC LIMIT 15
            """)).all()
            if not rows:
                print("  (no timesheet rows in email_outbox)")
                print("  If you have submitted a timesheet since the restart, that means the")
                print("  notification never ran — check sections 1 and 3 above.")
            for rid, status, to_email, attempts, created, err in rows:
                print(f"  #{rid:<5} {status:<8} {to_email:<40} attempts={attempts}  {created}")
                if err:
                    print(f"         error: {str(err)[:120]}")
        except Exception as exc:
            print(f"{WARN} could not read email_outbox: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())