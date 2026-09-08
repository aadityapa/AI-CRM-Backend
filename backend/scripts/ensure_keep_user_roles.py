"""One-shot: print CRM roles for keep-list users and ensure CEO/Admin on known accounts."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from sqlalchemy import create_engine, text

KEEP = {
    "balasaheb.suryawanshi@karnex.in",
    "vishal.harjani@karnex.in",
    "pavan.majeti@karnex.in",
    "srinivasa.chakravarthyperala@karnex.in",
    "gargee.joshi@karnex.in",
    "karan.singh@karnex.in",
    "pavan.sanap@karnex.in",
}

# Governance defaults from historical seed (username karan / vishal → email accounts).
ENSURE = {
    "karan.singh@karnex.in": "CEO",
    "vishal.harjani@karnex.in": "Admin",
    "pavan.sanap@karnex.in": "Admin",
}


def dsn() -> str:
    host = os.environ["DB_HOST"]
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ["DB_NAME"]
    user = os.environ["DB_USER"]
    password = os.environ["DB_PASSWORD"]
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def main() -> None:
    eng = create_engine(dsn())
    with eng.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT r.id, r.email, r.username,
                       COALESCE(string_agg(DISTINCT roles.name::text, ','), '') AS crm_roles
                FROM registration_data r
                LEFT JOIN user_roles ur ON ur.user_id = r.id
                LEFT JOIN roles ON roles.id = ur.role_id
                WHERE LOWER(r.email) = ANY(:emails)
                GROUP BY r.id
                ORDER BY r.id
                """
            ),
            {"emails": list(KEEP)},
        ).mappings().all()
        print("Current keep-list roles:")
        for r in rows:
            print(f"  {r['email']}: [{r['crm_roles']}]")

        for email, role_name in ENSURE.items():
            row = next((x for x in rows if (x["email"] or "").lower() == email), None)
            if not row:
                print(f"MISSING account {email}")
                continue
            roles = {x for x in (row["crm_roles"] or "").split(",") if x}
            if role_name in roles:
                print(f"OK {email} already has {role_name}")
                continue
            role_id = conn.execute(
                text("SELECT id FROM roles WHERE name::text = :n"),
                {"n": role_name},
            ).scalar()
            if not role_id:
                print(f"ROLE missing in DB: {role_name}")
                continue
            exists = conn.execute(
                text(
                    "SELECT 1 FROM user_roles WHERE user_id = :u AND role_id = :r"
                ),
                {"u": row["id"], "r": role_id},
            ).scalar()
            if exists:
                print(f"OK {email} already has {role_name}")
                continue
            conn.execute(
                text("INSERT INTO user_roles (user_id, role_id) VALUES (:u, :r)"),
                {"u": row["id"], "r": role_id},
            )
            print(f"ASSIGNED {role_name} -> {email}")


if __name__ == "__main__":
    main()
