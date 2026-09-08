r"""Create the missing Employee record for candidates already marked Joined.

Until 31 Aug 2026 a JOINED candidate never became an Employee, so anyone who
joined before that fix is missing from the Employees tab — which blocks project
mapping, timesheets, PO consumption and invoicing for them.

    cd backend
    python scripts/backfill_joined_employees.py --dry-run   # list only
    python scripts/backfill_joined_employees.py             # create them

Idempotent: profiles that already have an employee (by link or by email) are
skipped, so it is safe to re-run.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be created, write nothing")
    args = ap.parse_args()

    from sqlalchemy import select

    from crm_db import get_session_factory
    from models import Candidate, CandidateProfile, Employee, PipelineStatus
    from services.candidate_profiles import ensure_employee_for_joined_profile

    db = get_session_factory()()
    try:
        joined = db.execute(
            select(CandidateProfile)
            .where(CandidateProfile.pipeline_status == PipelineStatus.JOINED)
            .order_by(CandidateProfile.id)
        ).scalars().all()
        print(f"{len(joined)} profile(s) at Joined")

        created = skipped = failed = 0
        for prof in joined:
            linked = db.execute(
                select(Employee.id).where(Employee.candidate_profile_id == prof.id)
            ).first()
            cand = db.get(Candidate, prof.candidate_id)
            who = (" ".join(p for p in [getattr(cand, "first_name", None),
                                        getattr(cand, "last_name", None)] if p)
                   or f"candidate #{prof.candidate_id}")
            if linked:
                # A link to an employee with a DIFFERENT name is the shared-
                # mailbox mislink (2 Sep 2026: a candidate on it_support@ was
                # attached to the IT Support employee). Unlink and create the
                # real person under a placeholder address.
                emp_row = db.get(Employee, linked[0])
                cand_first = (getattr(cand, "first_name", "") or "").strip().lower()
                emp_first = (getattr(emp_row, "first_name", "") or "").strip().lower()
                if emp_row is not None and cand_first and emp_first and cand_first != emp_first:
                    print(f"  ! {who}: linked to employee #{emp_row.id} '{emp_row.first_name}' "
                          f"(shared email) — {'unlinking + creating' if not args.dry_run else 'WOULD unlink + create'}")
                    if not args.dry_run:
                        emp_row.candidate_profile_id = None
                        db.flush()
                        emp = ensure_employee_for_joined_profile(db, prof)
                        if emp is None:
                            failed += 1
                        else:
                            created += 1
                            print(f"  + {who}: employee #{emp.id} ({emp.email})")
                    else:
                        created += 1
                    continue
                skipped += 1
                print(f"  = {who}: already an employee (#{linked[0]})")
                continue
            if args.dry_run:
                created += 1
                print(f"  + {who}: WOULD create an employee")
                continue
            emp = ensure_employee_for_joined_profile(db, prof)
            if emp is None:
                failed += 1
                print(f"  ! {who}: could not create (see logs)")
            else:
                created += 1
                print(f"  + {who}: employee #{emp.id} ({emp.email})")

        if args.dry_run:
            db.rollback()
            print(f"\nDRY RUN — {created} would be created, {skipped} already existed")
        else:
            db.commit()
            print(f"\nDone — {created} created, {skipped} skipped, {failed} failed")
            if created:
                print("Next: Projects → map each employee to a project (rate + onboarding date), "
                      "then their timesheets can be created.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
