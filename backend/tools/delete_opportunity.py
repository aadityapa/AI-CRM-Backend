r"""Hard-delete one or more opportunities by Opportunity ID, with everything
hanging off them (requirement, applicant profiles, resumes, interview rounds,
AI links, slots, attachments, CTC slab). Refuses when a Project is built on
the opportunity.

Usage (from backend/):
    python tools\delete_opportunity.py C-2025-0033                     # DRY RUN — shows what would go
    python tools\delete_opportunity.py C-2025-0033 C-2025-0048 --apply
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import CandidateProfile, Opportunity, Project, Requirement, Resume  # noqa: E402
from services.crm_delete import hard_delete_opportunities  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    ids = [a for a in argv if not a.startswith("--")]
    if not ids:
        print(__doc__)
        return 2
    db = get_session_factory()()
    try:
        doomed: list[int] = []
        for oid in ids:
            opp = db.execute(select(Opportunity).where(Opportunity.opp_id == oid)).scalars().first()
            if opp is None:
                print(f"  ! {oid}: not found")
                continue
            n_proj = db.execute(select(func.count()).select_from(Project)
                                .where(Project.opportunity_id == opp.id)).scalar() or 0
            if n_proj:
                print(f"  ! {oid} ({opp.title}): {n_proj} project(s) built on it — NOT deleted")
                continue
            reqs = [r.id for r in db.execute(select(Requirement).where(Requirement.opportunity_id == opp.id)).scalars()]
            n_prof = db.execute(select(func.count()).select_from(CandidateProfile)
                                .where(CandidateProfile.opportunity_id == opp.id)).scalar() or 0
            n_res = db.execute(select(func.count()).select_from(Resume)
                               .where(Resume.requirement_id.in_(reqs))).scalar() if reqs else 0
            print(f"  {oid} ({opp.title}): {len(reqs)} requirement(s), {n_prof} candidate profile(s), {n_res} resume(s)")
            doomed.append(opp.id)
        if not doomed:
            print("Nothing to delete.")
            return 0
        hard_delete_opportunities(db, doomed)
        if apply:
            db.commit()
            print(f"\nDELETED {len(doomed)} opportunit{'y' if len(doomed) == 1 else 'ies'}.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing deleted. Re-run with --apply.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
