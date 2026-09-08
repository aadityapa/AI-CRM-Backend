"""Read-only snapshot of what is actually in the CRM tables right now.

Written to settle a specific question: the Zoho import committed 7,983 candidate
profiles, but the app was reported as holding 3,535. Nothing is decided — least
of all anything destructive — until we can see the real numbers.

**This script writes nothing.** It opens a session, reads counts, and rolls back.

Usage:
    python scripts/report_crm_state.py
    python scripts/report_crm_state.py --json      # machine-readable
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select, text

from crm_db import get_session_factory
from models import (
    Candidate, CandidateProfile, InterviewEvent, Opportunity,
)


def _count(db, stmt) -> int:
    return int(db.execute(stmt).scalar() or 0)


def _has_column(db, table: str, column: str) -> bool:
    return bool(db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c LIMIT 1"
    ), {"t": table, "c": column}).first())


def main() -> int:
    as_json = "--json" in sys.argv
    db = get_session_factory()()
    out: dict = {}
    try:
        # ---- candidate_profiles -------------------------------------------
        total = _count(db, select(func.count()).select_from(CandidateProfile))
        has_zoho_col = _has_column(db, "candidate_profiles", "zoho_profile_id")
        with_zoho = app_made = 0
        if has_zoho_col:
            with_zoho = _count(db, select(func.count()).select_from(CandidateProfile)
                               .where(CandidateProfile.zoho_profile_id.isnot(None)))
            app_made = total - with_zoho
        out["candidate_profiles"] = {
            "total": total,
            "zoho_profile_id_column_exists": has_zoho_col,
            "with_zoho_id": with_zoho,
            "app_created_no_zoho_id": app_made,
            "source_column_exists": _has_column(db, "candidate_profiles", "source"),
            "is_hidden_column_exists": _has_column(db, "candidate_profiles", "is_hidden"),
        }

        # ---- candidates ----------------------------------------------------
        c_total = _count(db, select(func.count()).select_from(Candidate))
        c_zoho = 0
        if _has_column(db, "candidates", "zoho_candidate_id"):
            c_zoho = _count(db, select(func.count()).select_from(Candidate)
                            .where(Candidate.zoho_candidate_id.isnot(None)))
        with_cv = _count(db, select(func.count()).select_from(Candidate)
                         .where(Candidate.cv_url.isnot(None), Candidate.cv_url != ""))
        out["candidates"] = {
            "total": c_total,
            "with_zoho_id": c_zoho,
            "with_cv_linked": with_cv,
        }

        # ---- interviews + opportunities ------------------------------------
        out["interview_events"] = {
            "total": _count(db, select(func.count()).select_from(InterviewEvent)),
            "zoho_interview_id_column_exists":
                _has_column(db, "interview_events", "zoho_interview_id"),
        }
        opp_total = _count(db, select(func.count()).select_from(Opportunity))
        opp_with_applicants = _count(db, select(func.count(func.distinct(
            CandidateProfile.opportunity_id))).select_from(CandidateProfile))
        out["opportunities"] = {
            "total": opp_total,
            "with_at_least_one_applicant": opp_with_applicants,
        }

        # ---- pipeline spread, to sanity-check the import --------------------
        rows = db.execute(
            select(CandidateProfile.pipeline_status, func.count())
            .group_by(CandidateProfile.pipeline_status)
            .order_by(func.count().desc())
        ).all()
        out["pipeline_status"] = {
            (s.value if hasattr(s, "value") else str(s)): int(n) for s, n in rows
        }

        if as_json:
            print(json.dumps(out, indent=2))
            return 0

        print("=" * 62)
        print(" CRM STATE — read-only, nothing was written")
        print("=" * 62)
        cp = out["candidate_profiles"]
        print("\nCANDIDATE PROFILES")
        print(f"   total rows                 : {cp['total']:>8,}")
        print(f"   carrying a Zoho profile id : {cp['with_zoho_id']:>8,}")
        print(f"   created in the app (no id) : {cp['app_created_no_zoho_id']:>8,}")
        print(f"   `source` column exists     : {cp['source_column_exists']}")
        print(f"   `is_hidden` column exists  : {cp['is_hidden_column_exists']}")

        c = out["candidates"]
        print("\nCANDIDATES")
        print(f"   total rows                 : {c['total']:>8,}")
        print(f"   carrying a Zoho id         : {c['with_zoho_id']:>8,}")
        print(f"   with a CV linked           : {c['with_cv_linked']:>8,}")

        ie = out["interview_events"]
        print("\nINTERVIEW ROUNDS")
        print(f"   total rows                 : {ie['total']:>8,}")

        o = out["opportunities"]
        print("\nOPPORTUNITIES")
        print(f"   total                      : {o['total']:>8,}")
        print(f"   with >=1 applicant         : {o['with_at_least_one_applicant']:>8,}")

        print("\nPIPELINE STATUS SPREAD")
        for status, n in out["pipeline_status"].items():
            print(f"   {status:<26} {n:>8,}")

        print("\n" + "-" * 62)
        expected = 7983
        if cp["total"] == 0:
            print(" The table is EMPTY — the Zoho import is not in this database.")
        elif abs(cp["total"] - expected) <= 50:
            print(f" Matches the {expected:,} the Zoho import committed.")
        else:
            print(f" NOTE: {cp['total']:,} rows, but the Zoho import committed {expected:,}.")
            print(" Check you are pointed at the same database (see DB_NAME in .env).")
        print("-" * 62)
    finally:
        db.rollback()
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
