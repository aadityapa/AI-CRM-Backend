"""Backfill branch links for customer holidays missing branch_id / calendar year.

Links orphan Holidays-tab rows into branch_holiday_years so they appear in the
branch Holiday Billing Policy panel.

- If holiday already has branch_id: ensure year header + holiday_calendar_id.
- If customer_id set and branch_id NULL and that customer has exactly one
  active branch: assign that branch.
- Otherwise print SKIP for manual assignment.

Usage:
    python backend/scripts/backfill_holiday_branch_links.py
    python backend/scripts/backfill_holiday_branch_links.py --apply
"""
from __future__ import annotations

import argparse
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

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from models import BranchHolidayYear, CustomerBranch, Holiday  # noqa: E402
from services.holidays import link_holiday_to_branch_calendar  # noqa: E402


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
        raise SystemExit("No database URL configured")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return url


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    engine = create_engine(_dsn())
    with Session(engine) as db:
        orphans = db.execute(
            select(Holiday).where(
                Holiday.is_active.is_(True),
                Holiday.customer_id.is_not(None),
            ).order_by(Holiday.id)
        ).scalars().all()

        linked = 0
        assigned = 0
        skipped = 0
        for h in orphans:
            if h.branch_id is None:
                branches = db.execute(
                    select(CustomerBranch).where(
                        CustomerBranch.customer_id == h.customer_id,
                    )
                ).scalars().all()
                if len(branches) != 1:
                    print(
                        f"SKIP id={h.id} {h.name} {h.holiday_date} "
                        f"customer={h.customer_id} branches={len(branches)} "
                        f"(pick branch manually in Holidays tab)"
                    )
                    skipped += 1
                    continue
                h.branch_id = branches[0].id
                assigned += 1
                print(
                    f"ASSIGN id={h.id} {h.name} {h.holiday_date} "
                    f"-> branch {branches[0].id} ({branches[0].branch_name})"
                )

            needs_link = h.holiday_calendar_id is None
            if needs_link or True:
                before = h.holiday_calendar_id
                link_holiday_to_branch_calendar(db, h, check_freeze=False)
                if h.holiday_calendar_id != before or needs_link:
                    linked += 1
                    print(
                        f"LINK  id={h.id} {h.name} {h.holiday_date} "
                        f"branch={h.branch_id} calendar={h.holiday_calendar_id}"
                    )

        print(f"\nassigned={assigned} linked={linked} skipped={skipped}")
        if args.apply:
            db.commit()
            print("Applied.")
        else:
            db.rollback()
            print("Dry-run only. Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
