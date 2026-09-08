"""Copy notice period from apply-form data onto the candidate record.

The public apply form has always asked for notice period and stored it in
`Resume.application_details["notice_period"]`, which is why it shows on the
Opportunity > Resumes tab. But the code that creates/updates the Candidate from
an application never copied it across, so `candidates.notice_period` stayed NULL
and the Notice Period column on Candidate Profiles was blank for everyone who
applied online.

The forward path is fixed. This backfills the candidates who applied before it was.

Only fills EMPTY values — a notice period a recruiter typed by hand always wins.

    python scripts/backfill_notice_period.py            # dry run, prints a summary
    python scripts/backfill_notice_period.py --apply    # write
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

try:
    from dotenv import load_dotenv
    for candidate in (BACKEND / ".env", BACKEND.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
except ImportError:
    pass


def notice_from_resume(resume) -> str | None:
    details = getattr(resume, "application_details", None) or {}
    if not isinstance(details, dict):
        return None
    value = str(details.get("notice_period") or "").strip()
    return value[:60] or None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    args = ap.parse_args()

    from sqlalchemy import or_, select

    from crm_db import get_session_factory
    from models import Candidate, Resume

    stats: Counter[str] = Counter()
    session = get_session_factory()()
    try:
        # Candidates missing a notice period, newest application first so the
        # most recent answer wins when someone applied more than once.
        blank = select(Candidate).where(
            or_(Candidate.notice_period.is_(None), Candidate.notice_period == "")
        )
        candidates = session.execute(blank).scalars().all()
        stats["candidates_without_notice"] = len(candidates)
        if not candidates:
            print("Every candidate already has a notice period. Nothing to do.")
            return 0

        by_id = {c.id: c for c in candidates}
        resumes = session.execute(
            select(Resume)
            .where(Resume.candidate_id.in_(list(by_id.keys())))
            .order_by(Resume.id.desc())
        ).scalars().all()

        seen: set[int] = set()
        examples: list[str] = []
        for resume in resumes:
            cid = resume.candidate_id
            if cid in seen or cid not in by_id:
                continue
            notice = notice_from_resume(resume)
            if not notice:
                stats["resume_had_no_notice"] += 1
                continue
            seen.add(cid)
            candidate = by_id[cid]
            if len(examples) < 8:
                name = " ".join(p for p in (candidate.first_name, candidate.last_name) if p)
                examples.append(f"  {name or candidate.email:<32} -> {notice}")
            if args.apply:
                candidate.notice_period = notice
            stats["filled"] += 1

        stats["still_blank"] = len(candidates) - stats["filled"]

        print("=" * 62)
        print(" Notice period backfill" + ("" if args.apply else "  (DRY RUN)"))
        print("=" * 62)
        for key in ("candidates_without_notice", "filled", "still_blank", "resume_had_no_notice"):
            print(f"  {key:28} {stats[key]}")
        if examples:
            print("\n  Examples:")
            print("\n".join(examples))

        if args.apply:
            session.commit()
            print("\n  Committed.")
        else:
            print("\n  Dry run — re-run with --apply to write.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
