"""Merge candidates that Zoho stored twice for the same person.

The Zoho export contains 7 pairs where both records share an email address. The
importer keeps both (giving the second a `<name>.<hash>@import.karnex.in`
placeholder, since email is unique on the table) so no data is silently lost —
this script is the deliberate second step that merges them back together.

    Nishant Bansal   bansal.nishant2000@gmail.com          <- kept
    Nishant Bansal   nishant.bansal.4d0ad410@import...     <- merged in, deleted

**A placeholder email is not by itself a duplicate.** Candidates who simply had
no email in Zoho also carry one, and they are real, distinct people. This script
only merges when a *counterpart candidate is actually found*, so those are left
alone.

Matching (strongest first):
  1. identical normalised phone number (digits only, last 10 kept — so
     "+919513150706" and "+91919513150706" match)
  2. identical normalised full name

The keeper is the candidate with a real email; ties go to the lower id. Every
child row is moved to the keeper before the duplicate is deleted:

    candidate_profiles · resumes · candidate_skills · education · experience
    outreach · ai_interview_links · interview_events · slot_bookings

Empty fields on the keeper are filled from the duplicate, so a merge never loses
a value the keeper did not already have.

Dry run by default.

Usage:
    python scripts/merge_duplicate_candidates.py
    python scripts/merge_duplicate_candidates.py --apply
    python scripts/merge_duplicate_candidates.py --apply --by name
        --by phone|name|both   matching strategy (default: both)
        --domain <d>           placeholder domain (default: import.karnex.in)
"""
from __future__ import annotations

import collections
import csv
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from crm_db import get_session_factory
from models import (
    AiInterviewLink, Candidate, CandidateEducation, CandidateExperience, CandidateOutreach,
    CandidateProfile, CandidateSkill, InterviewEvent, Resume, SlotBooking,
)

DEFAULT_DOMAIN = "import.karnex.in"

#: Fields copied from the duplicate when the keeper has nothing there.
FILLABLE = (
    "salutation", "middle_name", "phone", "date_of_birth", "gender", "experience_years",
    "notice_period", "current_address", "permanent_address", "technical_domain", "roles",
    "designation_id", "cv_url", "cv_original_filename", "linkedin_url", "last_working_day",
    "resignation_certificate_url", "current_ctc", "expected_ctc", "preferred_location_id",
    "city", "preferred_locations", "recruiter_email", "source_created_date",
)


def norm_phone(value: str | None) -> str:
    """Digits only, last 10 — tolerates +91 / 0 prefixes and a doubled country code."""
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else ""


def norm_name(cand: Candidate) -> str:
    parts = (cand.first_name, cand.middle_name, cand.last_name)
    return " ".join(" ".join(p for p in parts if p).lower().split())


def is_placeholder(cand: Candidate, domain: str) -> bool:
    return (cand.email or "").lower().endswith(f"@{domain}")


def main() -> int:  # noqa: C901
    argv = sys.argv[1:]
    apply = "--apply" in argv

    def take_flag(flag: str, default: str) -> str:
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return default

    by = take_flag("--by", "both").lower()
    domain = take_flag("--domain", DEFAULT_DOMAIN)
    if by not in {"phone", "name", "both"}:
        print("ERROR: --by must be phone, name or both")
        return 2

    print(f"Placeholder domain : @{domain}")
    print(f"Matching on        : {by}")
    print(f"Mode               : {'APPLY' if apply else 'DRY RUN'}\n")

    db = get_session_factory()()
    stats = collections.Counter()
    merges: list[dict] = []
    try:
        candidates = db.execute(select(Candidate)).scalars().all()
        placeholders = [c for c in candidates if is_placeholder(c, domain)]
        print(f"Candidates          : {len(candidates):,}")
        print(f"Placeholder emails  : {len(placeholders):,}\n")
        if not placeholders:
            print("Nothing to do.")
            return 0

        by_phone: dict[str, list[Candidate]] = collections.defaultdict(list)
        by_name: dict[str, list[Candidate]] = collections.defaultdict(list)
        for c in candidates:
            p = norm_phone(c.phone)
            if p:
                by_phone[p].append(c)
            n = norm_name(c)
            if n:
                by_name[n].append(c)

        def counterpart(dup: Candidate) -> Candidate | None:
            """A different candidate that is clearly the same person."""
            pools = []
            if by in {"phone", "both"}:
                pools.append(by_phone.get(norm_phone(dup.phone), []))
            if by in {"name", "both"}:
                pools.append(by_name.get(norm_name(dup), []))
            for pool in pools:
                # Prefer a real-email candidate; never merge two placeholders into
                # each other (both may be genuinely email-less people).
                real = [c for c in pool
                        if c.id != dup.id and not is_placeholder(c, domain)]
                if len(real) == 1:
                    return real[0]
                if len(real) > 1:
                    return None  # ambiguous — leave it for a human
            return None

        for dup in placeholders:
            keeper = counterpart(dup)
            if keeper is None:
                stats["no_counterpart"] += 1
                continue

            # ---- fill gaps on the keeper -------------------------------
            filled = []
            for field in FILLABLE:
                if getattr(keeper, field, None) in (None, "") and \
                        getattr(dup, field, None) not in (None, ""):
                    setattr(keeper, field, getattr(dup, field))
                    filled.append(field)
            if not getattr(keeper, "zoho_candidate_id", None):
                keeper.zoho_candidate_id = getattr(dup, "zoho_candidate_id", None)

            # ---- move children -----------------------------------------
            moved = collections.Counter()

            existing_opps = {
                p.opportunity_id for p in db.execute(
                    select(CandidateProfile).where(CandidateProfile.candidate_id == keeper.id)
                ).scalars().all()
            }
            for prof in db.execute(
                select(CandidateProfile).where(CandidateProfile.candidate_id == dup.id)
            ).scalars().all():
                if prof.opportunity_id in existing_opps:
                    # The keeper already has an application for this opportunity —
                    # uq_profile_candidate_opp forbids a second, so drop this one.
                    db.delete(prof)
                    moved["profile_dropped"] += 1
                else:
                    prof.candidate_id = keeper.id
                    existing_opps.add(prof.opportunity_id)
                    moved["profiles"] += 1

            have_skills = {
                s.skill_id for s in db.execute(
                    select(CandidateSkill).where(CandidateSkill.candidate_id == keeper.id)
                ).scalars().all()
            }
            for link in db.execute(
                select(CandidateSkill).where(CandidateSkill.candidate_id == dup.id)
            ).scalars().all():
                if link.skill_id in have_skills:
                    db.delete(link)
                else:
                    link.candidate_id = keeper.id
                    have_skills.add(link.skill_id)
                    moved["skills"] += 1

            for model, label in (
                (Resume, "resumes"), (CandidateEducation, "education"),
                (CandidateExperience, "experience"), (CandidateOutreach, "outreach"),
                (AiInterviewLink, "ai_links"), (InterviewEvent, "interview_events"),
                (SlotBooking, "slot_bookings"),
            ):
                for row in db.execute(
                    select(model).where(model.candidate_id == dup.id)
                ).scalars().all():
                    row.candidate_id = keeper.id
                    moved[label] += 1

            db.flush()
            db.delete(dup)
            stats["merged"] += 1
            merges.append({
                "kept_id": keeper.id,
                "kept_email": keeper.email,
                "removed_id": dup.id,
                "removed_email": dup.email,
                "name": norm_name(keeper),
                "phone": keeper.phone or "",
                "fields_filled": ",".join(filled),
                "children_moved": ", ".join(f"{k}={v}" for k, v in sorted(moved.items())) or "none",
            })

        print(f"Merged and removed  : {stats['merged']:>6,}")
        print(f"Kept (no duplicate) : {stats['no_counterpart']:>6,}   "
              f"candidates with no email in Zoho — real people, left alone")

        if merges:
            print("\n  kept  <-  removed")
            for m in merges[:12]:
                print(f"   #{m['kept_id']} {m['kept_email'][:38]:38} <- #{m['removed_id']} "
                      f"({m['children_moved']})")
            if len(merges) > 12:
                print(f"   ... and {len(merges) - 12} more")
            report = BACKEND.parent / "import_templates" / "merged_duplicate_candidates.csv"
            report.parent.mkdir(parents=True, exist_ok=True)
            with report.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(merges[0].keys()))
                w.writeheader()
                w.writerows(merges)
            print(f"\nReport              : {report}")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
