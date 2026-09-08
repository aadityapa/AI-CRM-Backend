#!/usr/bin/env python3
"""CLI: run Project Employee monthly leave credit (+ year-end carry/expiry).

The app scheduler now runs this daily (`services/scheduler.py`, job
``pe_leave_credit``) and repairs months it missed on its own. This CLI remains
for deliberate operations: repairing a gap older than the scheduler's lookback
window, or crediting a single employee.

Usage:
  cd backend
  python scripts/run_pe_leave_credit.py
  python scripts/run_pe_leave_credit.py --as-of 2026-07-01
  python scripts/run_pe_leave_credit.py --pe-id 12 --as-of 2026-07-01

  # Repair: credit every month from 2026-01 to today that has no ledger row.
  python scripts/run_pe_leave_credit.py --from 2026-01
  python scripts/run_pe_leave_credit.py --from 2026-01 --dry-run
  # Credit a range regardless of what already ran (still idempotent per month).
  python scripts/run_pe_leave_credit.py --from 2026-01 --to 2026-06 --all-periods

Backfill runs each month at its month end, which is also the only date the
year-end carry-forward acts on — so a range spanning December replays that too.
Every credit is keyed `pe_credit:{pe}:{type}:{YYYY-MM}`, so re-running a month
that already ran credits nothing.

Cron example (1st of month 02:00) — only needed if SCHEDULER_WORKER=false:
  0 2 1 * * cd /path/to/backend && python scripts/run_pe_leave_credit.py
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# Allow `python scripts/run_pe_leave_credit.py` from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from crm_db import get_session_factory  # noqa: E402
from services.project_employee_leave_credit import (  # noqa: E402
    missing_credit_periods,
    run_pe_leave_credit,
    run_pe_leave_credit_backfill,
)


def _periods_between(start: str, end: str) -> list[str]:
    """Inclusive YYYY-MM list."""
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = int(end[:4]), int(end[5:7])
    out: list[str] = []
    while (sy, sm) <= (ey, em):
        out.append(f"{sy:04d}-{sm:02d}")
        sy, sm = (sy + 1, 1) if sm == 12 else (sy, sm + 1)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Credit PE leave for the period")
    parser.add_argument("--as-of", type=str, default=None, help="YYYY-MM-DD (default: today)")
    parser.add_argument("--pe-id", type=int, default=None, help="Limit to one project_employee id")
    parser.add_argument("--from", dest="from_period", type=str, default=None,
                        help="YYYY-MM — backfill from this month forward")
    parser.add_argument("--to", dest="to_period", type=str, default=None,
                        help="YYYY-MM — last month to backfill (default: last closed month)")
    parser.add_argument("--all-periods", action="store_true",
                        help="Backfill every month in the range, not only months with no ledger row")
    parser.add_argument("--dry-run", action="store_true",
                        help="List the months that would be credited, then exit")
    args = parser.parse_args()

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    db = get_session_factory()()
    try:
        if args.from_period:
            end = args.to_period or (
                f"{as_of.year:04d}-{as_of.month - 1:02d}" if as_of.month > 1
                else f"{as_of.year - 1:04d}-12"
            )
            wanted = _periods_between(args.from_period, end)
            if not args.all_periods:
                gaps = set(missing_credit_periods(db, as_of=as_of, lookback_months=600))
                wanted = [p for p in wanted if p in gaps]
            if args.dry_run:
                summary = {"dry_run": True, "would_credit": wanted}
            elif not wanted:
                summary = {"periods": [], "message": "Nothing to repair — every month in that "
                                                     "range already has credits."}
            else:
                summary = run_pe_leave_credit_backfill(db, periods=wanted, pe_id=args.pe_id)
        else:
            summary = run_pe_leave_credit(db, as_of=as_of, pe_id=args.pe_id)
    finally:
        db.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
