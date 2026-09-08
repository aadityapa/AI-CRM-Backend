"""Why does a TA see no requirements?

TA visibility is a STATUS whitelist, not an assignment or ownership rule
(services/requirements.py TA_VISIBLE_STATUSES). A requirement only becomes
visible to TA once RMG runs engineering-approve, which moves it to
Open_For_Sourcing.

The usual confusion: "Sales Head approved the opportunity" CREATES a requirement,
but at status Pending_Engineering_Review — which TA cannot see. RMG still has to
approve the requirement itself.

This script prints every requirement's status so you can see where they are stuck.
Read-only.

Usage:
    python scripts/diagnose_requirement_visibility.py
    python scripts/diagnose_requirement_visibility.py --req REQ-2026-001
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select

from crm_db import get_session_factory
from models import Opportunity, Requirement, RequirementStatus
from services.requirements import TA_VISIBLE_STATUSES

TA_VALUES = {s.value for s in TA_VISIBLE_STATUSES}


def _v(status) -> str:
    return status.value if hasattr(status, "value") else str(status)


def main() -> int:
    argv = sys.argv[1:]
    one = None
    if "--req" in argv:
        i = argv.index("--req")
        if i + 1 < len(argv):
            one = argv[i + 1]

    db = get_session_factory()()
    try:
        if one:
            req = db.execute(
                select(Requirement).where(Requirement.req_number == one)
            ).scalars().first()
            if req is None:
                print(f"No requirement with req_number {one!r}")
                return 1
            status = _v(req.status)
            print(f"{req.req_number} — {req.title}")
            print(f"  status            : {status}")
            print(f"  visible to TA     : {'YES' if status in TA_VALUES else 'NO'}")
            print(f"  opportunity_id    : {req.opportunity_id}")
            print(f"  customer_id       : {req.customer_id}")
            print(f"  rmg_jd_text set   : {bool((req.rmg_jd_text or '').strip())}")
            if status not in TA_VALUES:
                print("\n  -> TA cannot see this. Next step:")
                if status == RequirementStatus.PENDING_ENGINEERING_REVIEW.value:
                    print("     RMG must open it and click Approve")
                    print("     (POST /api/requirements/{id}/engineering-approve).")
                    print("     That endpoint REFUSES with 400 unless an RMG JD is")
                    print("     attached — text or file. If RMG hit that error, the")
                    print("     status never changed.")
                elif status in {RequirementStatus.DRAFT.value,
                                RequirementStatus.PENDING_SALES_HEAD_APPROVAL.value}:
                    print("     It has not been approved by Sales Head yet.")
                else:
                    print(f"     Status {status} is outside the TA workflow.")
            return 0

        total = int(db.execute(select(func.count()).select_from(Requirement)).scalar() or 0)
        print("=" * 66)
        print(f" REQUIREMENTS: {total:,} total")
        print("=" * 66)
        if total == 0:
            print("\n No requirements exist at all.")
            print(" One is created when a Sales Head APPROVES an opportunity.")
            opp_pending = int(db.execute(
                select(func.count()).select_from(Opportunity)
                .where(Opportunity.approval_status == "Pending_Sales_Head_Approval")
            ).scalar() or 0)
            print(f" Opportunities awaiting Sales Head approval: {opp_pending:,}")
            return 0

        rows = db.execute(
            select(Requirement.status, func.count())
            .group_by(Requirement.status).order_by(func.count().desc())
        ).all()
        visible = 0
        print(f"\n{'status':<32}{'count':>8}   TA sees?")
        print("-" * 66)
        for status, n in rows:
            s = _v(status)
            ok = s in TA_VALUES
            visible += n if ok else 0
            print(f"{s:<32}{int(n):>8}   {'YES' if ok else 'no'}")
        print("-" * 66)
        print(f"{'VISIBLE TO A TA':<32}{visible:>8}")

        if visible == 0:
            print("\n  This is why the TA list is empty — nothing has reached sourcing.")
            stuck = next((int(n) for s, n in rows
                          if _v(s) == RequirementStatus.PENDING_ENGINEERING_REVIEW.value), 0)
            if stuck:
                print(f"\n  {stuck} requirement(s) sit at Pending_Engineering_Review.")
                print("  They are waiting for RMG, not for TA. An RMG user must open")
                print("  each one and click Approve. Note that approval is REFUSED")
                print("  unless an RMG JD (text or file) is attached first.")
                print("\n  Requirements waiting on RMG:")
                for r in db.execute(
                    select(Requirement)
                    .where(Requirement.status == RequirementStatus.PENDING_ENGINEERING_REVIEW)
                    .order_by(Requirement.id.desc()).limit(15)
                ).scalars().all():
                    jd = "JD attached" if (r.rmg_jd_text or "").strip() else "NO JD YET"
                    print(f"    {r.req_number:<16} {(r.title or '')[:38]:<40} {jd}")
        else:
            print("\n  A TA should see the rows counted above. If the page is still")
            print("  empty, confirm the signed-in user really has the TA role")
            print("  (GET /api/me) and is not filtered by the status dropdown.")
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
