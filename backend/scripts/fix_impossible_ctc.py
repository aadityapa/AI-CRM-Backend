r"""Find (and optionally repair) CTC values that are 10^5 too big.

2 Sep 2026 bug report: a candidate showed **Current CTC ₹1,00,00,00,000** on the
Applicants tab. CTC is stored in RUPEES while several inputs are labelled
"(Lac)" and multiply by 100,000 on save — a rupee figure typed (or prefilled)
into one of those is off by exactly 100,000. The write-time guard now refuses
such values; this repairs the rows written before it existed.

    cd backend
    python scripts/fix_impossible_ctc.py                 # report only (default)
    python scripts/fix_impossible_ctc.py --apply          # divide by 100,000
    python scripts/fix_impossible_ctc.py --apply --null   # blank them instead

Only values ABOVE the sanity ceiling are touched. `--apply` divides by 100,000
(the exact inverse of the bug) and re-checks the result — if the corrected value
is still implausible the row is reported and left alone, never guessed at twice.
Nothing is written without --apply.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LAKH = 100_000


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the corrections")
    ap.add_argument("--null", action="store_true",
                    help="with --apply: blank the value instead of dividing by 100,000")
    args = ap.parse_args()

    from sqlalchemy import or_, select

    from crm_db import get_session_factory
    from models import Candidate, CandidateProfile
    from services.ctc import MAX_CTC_RUPEES, ctc_looks_wrong

    db = get_session_factory()()
    fixed = flagged = 0
    try:
        targets = [
            (Candidate, ["current_ctc", "expected_ctc"], "candidate"),
            (CandidateProfile, ["current_ctc", "expected_ctc", "ctc_approval_amount"],
             "profile"),
        ]
        for model, fields, label in targets:
            conds = [getattr(model, f) > MAX_CTC_RUPEES for f in fields]
            rows = db.execute(select(model).where(or_(*conds))).scalars().all()
            for row in rows:
                for field in fields:
                    val = getattr(row, field, None)
                    if val is None or float(val) <= MAX_CTC_RUPEES:
                        continue
                    old = float(val)
                    corrected = round(old / LAKH, 2)
                    still_wrong = ctc_looks_wrong(corrected)
                    where = f"{label} #{row.id}.{field}"
                    if args.null:
                        print(f"  {where}: ₹{old:,.0f} -> NULL")
                        if args.apply:
                            setattr(row, field, None)
                            fixed += 1
                    elif still_wrong:
                        flagged += 1
                        print(f"  {where}: ₹{old:,.0f} -> ₹{corrected:,.0f} is STILL "
                              f"implausible — left alone, fix by hand")
                    else:
                        print(f"  {where}: ₹{old:,.0f} -> ₹{corrected:,.0f}")
                        if args.apply:
                            setattr(row, field, corrected)
                            fixed += 1

        if args.apply:
            db.commit()
            print(f"\nDone — {fixed} value(s) corrected"
                  + (f", {flagged} left for manual review" if flagged else ""))
        else:
            db.rollback()
            print(f"\nDRY RUN — nothing written. Re-run with --apply to correct them."
                  + (f" ({flagged} would need manual review.)" if flagged else ""))
    finally:
        db.close()


if __name__ == "__main__":
    main()
