r"""Link candidates to opportunities (creates the Candidate Profile rows).

The candidate <-> opportunity relation is NOT present in the Zoho exports we
have (the opportunities' Candidates subform came back empty and the candidate
file's Opportunity_ID column is blank), so it needs one small mapping file.

Expected CSV — any of these column names work (case-insensitive):

    candidate  : Email | Candidate_Email | Candidate Email | Candidate_Name | Name
    opportunity: Opportunity_ID | Opportunity | Opportunity_Title | Opp_ID
    optional   : Status | Candidate_Status | Pipeline_Status   (e.g. Sourcing,
                 Technical_Screening, Shortlisted, Joined, Sales_Rejected...)
    optional   : Current_CTC, Expected_CTC   (lac or rupees, auto-detected)

Minimal example:

    Email,Opportunity_ID,Status
    soniamadabattula328@gmail.com,C-2026-00068,Sourcing

Usage (from the backend folder, same venv as the app):

    python tools\link_candidates_opportunities.py ..\import_templates\candidate_opportunity_links.csv
    python tools\link_candidates_opportunities.py ..\import_templates\candidate_opportunity_links.csv --apply

Matching:
  * Candidate by email (exact, case-insensitive) first, then by full name.
  * Opportunity by Opportunity ID (e.g. C-2026-00068) first, then by exact title
    — a title matching several opportunities is reported, never guessed.
  * Existing links are updated (status/CTC), never duplicated.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import Candidate, CandidateProfile, Opportunity, PipelineStatus

CAND_KEYS = ("email", "candidate_email", "candidateemail", "candidate email")
NAME_KEYS = ("candidate_name", "candidate name", "candidatename", "name")
OPP_KEYS = ("opportunity_id", "opportunity id", "opp_id", "oppid", "opportunity")
TITLE_KEYS = ("opportunity_title", "opportunity title", "title")
STATUS_KEYS = ("status", "candidate_status", "candidate status", "pipeline_status")
CTC_KEYS = ("current_ctc", "current ctc", "ctc")
ECTC_KEYS = ("expected_ctc", "expected ctc", "expected_ctc_lac_annual")


def squash(s) -> str:
    return " ".join(str(s or "").split())


def norm(s) -> str:
    return squash(s).lower()


def pick(row: dict, keys) -> str:
    for k, v in row.items():
        if norm(k) in keys and squash(v):
            return squash(v)
    return ""


def money(v):
    v = squash(v)
    if not v:
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    if x <= 0:
        return None
    return round(x * 100000, 2) if x <= 500 else round(x, 2)


def to_status(raw: str) -> PipelineStatus:
    s = squash(raw).replace(" ", "_")
    if not s:
        return PipelineStatus.SOURCING
    try:
        return PipelineStatus(s)
    except ValueError:
        for st in PipelineStatus:
            if norm(st.value) == norm(raw):
                return st
    return PipelineStatus.SOURCING


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    paths = [a for a in argv if not a.startswith("--")]
    csv_path = Path(paths[0]) if paths else (
        Path(__file__).resolve().parent.parent.parent
        / "import_templates" / "candidate_opportunity_links.csv")
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        print(__doc__)
        return 2

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    print(f"CSV link rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    db = get_session_factory()()
    created = updated = skipped = 0
    try:
        by_email: dict[str, Candidate] = {}
        by_name: dict[str, list[Candidate]] = {}
        for c in db.execute(select(Candidate)).scalars().all():
            if c.email:
                by_email[norm(c.email)] = c
            full = " ".join(x for x in (c.first_name, c.middle_name, c.last_name) if x)
            by_name.setdefault(norm(full), []).append(c)
        by_opp_id = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        by_title: dict[str, list[Opportunity]] = {}
        for o in by_opp_id.values():
            by_title.setdefault(norm(o.title), []).append(o)
        existing = {
            (p.candidate_id, p.opportunity_id): p
            for p in db.execute(select(CandidateProfile)).scalars().all()
        }

        for i, row in enumerate(rows, start=2):
            email = pick(row, CAND_KEYS)
            cname = pick(row, NAME_KEYS)
            opp_ref = pick(row, OPP_KEYS)
            opp_title = pick(row, TITLE_KEYS)

            cand = by_email.get(norm(email)) if email else None
            if cand is None and cname:
                hits = by_name.get(norm(cname), [])
                if len(hits) == 1:
                    cand = hits[0]
                elif len(hits) > 1:
                    print(f"  ! row {i}: candidate name {cname!r} matches {len(hits)} candidates - add an Email column")
                    skipped += 1
                    continue
            if cand is None:
                print(f"  ! row {i}: candidate not found ({email or cname or '-'})")
                skipped += 1
                continue

            opp = by_opp_id.get(opp_ref) if opp_ref else None
            if opp is None and (opp_title or opp_ref):
                hits = by_title.get(norm(opp_title or opp_ref), [])
                if len(hits) == 1:
                    opp = hits[0]
                elif len(hits) > 1:
                    ids = ", ".join(o.opp_id for o in hits)
                    print(f"  ! row {i}: title {(opp_title or opp_ref)!r} matches several opportunities ({ids}) - use Opportunity_ID")
                    skipped += 1
                    continue
            if opp is None:
                print(f"  ! row {i}: opportunity not found ({opp_ref or opp_title or '-'})")
                skipped += 1
                continue

            status = to_status(pick(row, STATUS_KEYS))
            cur_ctc = money(pick(row, CTC_KEYS)) or cand.current_ctc
            exp_ctc = money(pick(row, ECTC_KEYS)) or cand.expected_ctc

            prof = existing.get((cand.id, opp.id))
            label = f"{cand.email} -> {opp.opp_id} ({opp.title})"
            if prof is None:
                db.add(CandidateProfile(
                    candidate_id=cand.id, opportunity_id=opp.id,
                    current_ctc=cur_ctc, expected_ctc=exp_ctc,
                    pipeline_status=status,
                ))
                existing[(cand.id, opp.id)] = True  # type: ignore[assignment]
                created += 1
                print(f"  + {label} [{status.value}]")
            else:
                if prof is not True:
                    prof.pipeline_status = status
                    if cur_ctc is not None:
                        prof.current_ctc = cur_ctc
                    if exp_ctc is not None:
                        prof.expected_ctc = exp_ctc
                updated += 1
                print(f"  ~ {label} [{status.value}]")

        print(f"\nLINKS: {created} created, {updated} updated, {skipped} skipped")
        if apply:
            db.commit()
            print("\nAPPLIED - candidate/opportunity links written.")
        else:
            db.rollback()
            print("\nDry run only - nothing changed. Re-run with --apply to write.")
        return 0
    except Exception as exc:
        db.rollback()
        import traceback
        traceback.print_exc()
        print(f"FAILED - rolled back, nothing changed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
