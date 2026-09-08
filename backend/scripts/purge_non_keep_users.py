"""Keep only the listed Karnex login accounts; purge every other HR/CRM user.

Candidate accounts (registration_data.role = 'candidate') are left alone.
HR/CRM login accounts whose email is not in KEEP_EMAILS are hard-deleted when
safe; if FK history blocks deletion they are deactivated, stripped of CRM roles,
and their password is scrambled so they cannot log in.

Usage (from repo root, with .env loaded by the process / shell):
    python backend/scripts/purge_non_keep_users.py
    python backend/scripts/purge_non_keep_users.py --apply
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

# Load .env from repo root if present.
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import sqlalchemy as sa
from sqlalchemy import create_engine, text

from password_hashing import hash_password

KEEP_EMAILS = {
    "balasaheb.suryawanshi@karnex.in",
    "vishal.harjani@karnex.in",
    "pavan.majeti@karnex.in",
    "srinivasa.chakravarthyperala@karnex.in",
    "gargee.joshi@karnex.in",
    "karan.singh@karnex.in",
    "pavan.sanap@karnex.in",
}


def _dsn() -> str:
    if os.getenv("USE_LOCAL_DB", "").lower() in {"1", "true", "yes"}:
        host = os.environ["DB_HOST"]
        port = os.environ.get("DB_PORT", "5432")
        name = os.environ["DB_NAME"]
        user = os.environ["DB_USER"]
        password = os.environ["DB_PASSWORD"]
        return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
    url = os.getenv("AUTH_DB_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("No database URL configured (DB_* or AUTH_DB_URL).")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


# Nullable FKs we can clear before delete.
_NULLABLE_FK_UPDATES = [
    ("candidate_profiles", "ta_owner_id"),
    ("timesheets", "approved_by"),
    ("timesheet_uploads", "uploaded_by"),
    ("interview_events", "created_by"),
    ("interview_round_feedback", "created_by"),
    ("leave_applications", "decided_by"),
    ("requirements", "sales_head_approved_by"),
    ("requirements", "engineering_reviewed_by"),
    ("requirement_documents", "uploaded_by"),
    ("resumes", "screened_by"),
    ("ai_interview_links", "scheduled_by"),
    ("invoices", "approved_by"),
    ("opportunity_documents", "uploaded_by"),
    ("opportunities", "sales_head_approved_by"),
    ("template_requests", "fulfilled_by"),
    ("template_requests", "prepared_by"),
    ("employees", "user_id"),
]

# Owned rows that can be deleted with the user.
_OWNED_DELETES = [
    ("user_roles", "user_id"),
    ("user_profiles", "user_id"),
    ("notifications", "user_id"),
    ("user_table_preferences", "user_id"),
    ("timesheet_drafts", "user_id"),
    ("requirement_watchers", "user_id"),
    ("opportunity_watchers", "user_id"),
    ("invoice_watchers", "user_id"),
    ("login_history", "user_id"),
]


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
            {"t": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _reassign_required(conn, table: str, column: str, old_id: int, new_id: int) -> int:
    if not _table_exists(conn, table) or not _column_exists(conn, table, column):
        return 0
    return conn.execute(
        text(f"UPDATE {table} SET {column} = :new WHERE {column} = :old"),
        {"new": new_id, "old": old_id},
    ).rowcount or 0


def purge_user(conn, user_id: int, reassign_to: int) -> str:
    """Try hard-delete; on residual FK failure lock the account instead."""
    for table, col in _NULLABLE_FK_UPDATES:
        if _table_exists(conn, table) and _column_exists(conn, table, col):
            conn.execute(
                text(f"UPDATE {table} SET {col} = NULL WHERE {col} = :i"),
                {"i": user_id},
            )

    for table, col in _OWNED_DELETES:
        if _table_exists(conn, table) and _column_exists(conn, table, col):
            conn.execute(text(f"DELETE FROM {table} WHERE {col} = :i"), {"i": user_id})

    # Required created_by / posted_by style columns — reassign to a keep user.
    for table, col in [
        ("requirements", "created_by"),
        ("requirement_comments", "posted_by"),
        ("opportunities", "created_by"),
        ("invoices", "created_by"),
        ("template_requests", "requested_by"),
    ]:
        _reassign_required(conn, table, col, user_id, reassign_to)

    try:
        with conn.begin_nested():
            conn.execute(text("DELETE FROM registration_data WHERE id = :i"), {"i": user_id})
        return "deleted"
    except Exception:
        # Lock so they cannot log in; keep the row for audit FKs we could not move.
        scrambled = hash_password(secrets.token_urlsafe(48))
        conn.execute(
            text(
                "UPDATE registration_data "
                "SET is_active = FALSE, password_hash = :h, password_salt = '' "
                "WHERE id = :i"
            ),
            {"h": scrambled, "i": user_id},
        )
        if _table_exists(conn, "user_roles"):
            conn.execute(text("DELETE FROM user_roles WHERE user_id = :i"), {"i": user_id})
        return "locked"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    args = parser.parse_args()

    engine = create_engine(_dsn())
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, full_name, email, username, role, "
                "COALESCE(is_active, TRUE) AS is_active "
                "FROM registration_data ORDER BY id"
            )
        ).mappings().all()

        keep = [r for r in rows if (r["email"] or "").lower() in KEEP_EMAILS]
        # Application login accounts only (legacy role hr) not in keep list.
        purge = [
            r
            for r in rows
            if (r["role"] or "").lower() == "hr"
            and (r["email"] or "").lower() not in KEEP_EMAILS
        ]
        candidates = [r for r in rows if (r["role"] or "").lower() == "candidate"]
        other = [
            r
            for r in rows
            if r not in keep and r not in purge and r not in candidates
        ]

        print(f"Total registration_data rows: {len(rows)}")
        print(f"Keep ({len(keep)}):")
        for r in keep:
            print(f"  KEEP  id={r['id']}  {r['email']}  ({r['username']})  active={r['is_active']}")
        print(f"Purge HR logins ({len(purge)}):")
        for r in purge:
            print(f"  PURGE id={r['id']}  {r['email']}  ({r['username']})")
        print(f"Candidates left alone: {len(candidates)}")
        if other:
            print(f"Other non-hr rows left alone ({len(other)}):")
            for r in other:
                print(f"  SKIP  id={r['id']}  role={r['role']}  {r['email']}")

        if not keep:
            raise SystemExit("Refusing to purge: none of the KEEP_EMAILS exist in the DB.")

        reassign_to = int(keep[0]["id"])
        # Prefer pavan.sanap or karan as reassignment target when present.
        for preferred in ("pavan.sanap@karnex.in", "karan.singh@karnex.in", "vishal.harjani@karnex.in"):
            hit = next((r for r in keep if (r["email"] or "").lower() == preferred), None)
            if hit:
                reassign_to = int(hit["id"])
                break

        if not args.apply:
            print("\nDry-run only. Re-run with --apply to write changes.")
            return 0

        results = {"deleted": 0, "locked": 0}
        for r in purge:
            outcome = purge_user(conn, int(r["id"]), reassign_to)
            results[outcome] = results.get(outcome, 0) + 1
            print(f"  {outcome.upper():7} id={r['id']}  {r['email']}")

        print(f"\nDone. deleted={results['deleted']} locked={results['locked']} reassign_to={reassign_to}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
