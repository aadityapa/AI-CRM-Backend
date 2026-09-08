"""Attach resume files to candidates already in the database.

Use this when the candidates were imported before the CV folder was available —
it only touches `cv_url` and `cv_original_filename`, leaving every other field
alone. Safe and quick to re-run.

Matching, in order:
  1. `resume_file` from candidates.json  ->  exact filename in the folder
  2. `<zoho_candidate_id>.<any extension>`
  3. candidates with no Zoho id: `<candidate id>.<any extension>`

The export names files `<candidate_id>.<ext>` and carries the real extension —
5,477 .pdf, 954 .docx, 62 .doc, 1 .html, 1 .pptx. All are served and previewed:
PDF renders via pdf.js, DOCX via mammoth, and .doc offers a download (the old
binary Word format has no in-browser renderer).

Dry run by default.

`--cv-dir` may hold loose resume files, .zip archives, or a mix. ZIP archives are
read in place — nothing is extracted to disk, so a folder of 15 zips works
directly.

Usage (Windows — keep each command on ONE line):

    # files already placed in data/crm_uploads/cv — just update the database
    python scripts\\link_candidate_resumes.py --manifest
    python scripts\\link_candidate_resumes.py --manifest --apply

    # link from a folder of resumes and/or .zip archives
    python scripts\\link_candidate_resumes.py --cv-dir "F:\\resumes"
    python scripts\\link_candidate_resumes.py --cv-dir "F:\\resumes" --apply

        --manifest [path]  use a pre-built manifest; defaults to
                           import_templates/resume_manifest.csv
        --relink           also replace CV links that are already set
        --json <path>      candidates.json (default: import_templates/candidates.json)
"""
from __future__ import annotations

import collections
import csv
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import select

from crm_db import get_session_factory
from models import Candidate
from services.crm_common import CRM_UPLOAD_DIR
from tools.import_candidates_json import (
    CV_SUBDIR, DEFAULT_SRC, TEMPLATES, build_cv_index, close_archives, count_archives,
    resume_problem, squash, store_cv,
)

#: Written by the resume placement step; used when --manifest is given no path.
DEFAULT_MANIFEST = TEMPLATES / "resume_manifest.csv"


def link_from_manifest(manifest: Path, *, apply: bool, relink: bool) -> int:
    """Set cv_url from a pre-built manifest, without needing the source folder.

    Use when the resume files are already sitting in data/crm_uploads/cv — this
    only updates the database. Columns: zoho_candidate_id, cv_url,
    cv_original_filename.
    """
    if not manifest.is_file():
        print(f"ERROR: manifest not found: {manifest}")
        return 2
    rows = list(csv.DictReader(manifest.open(encoding="utf-8-sig")))
    print(f"Manifest  : {manifest}  ({len(rows):,} rows)")
    print(f"Mode      : {'APPLY' if apply else 'DRY RUN'}{' + RELINK' if relink else ''}\n")

    cv_out = CRM_UPLOAD_DIR / CV_SUBDIR
    db = get_session_factory()()
    stats = collections.Counter()
    try:
        by_zoho = {
            squash(getattr(c, "zoho_candidate_id", None)): c
            for c in db.execute(select(Candidate)).scalars().all()
            if squash(getattr(c, "zoho_candidate_id", None))
        }
        print(f"Candidates with a Zoho id: {len(by_zoho):,}\n")

        for row in rows:
            cand = by_zoho.get(squash(row.get("zoho_candidate_id")))
            if cand is None:
                stats["no_candidate"] += 1
                continue
            if cand.cv_url and not relink:
                stats["already_linked"] += 1
                continue
            url = squash(row.get("cv_url"))
            # Refuse to point the UI at a file that is not actually there.
            if not (cv_out / url.rsplit("/", 1)[-1]).is_file():
                stats["file_absent"] += 1
                continue
            cand.cv_url = url
            original = squash(row.get("cv_original_filename"))
            if original:
                cand.cv_original_filename = original[:255]
            stats["linked"] += 1

        print(f"Linked         : {stats['linked']:>6,}")
        print(f"Already linked : {stats['already_linked']:>6,}"
              f"{'' if relink else '   (use --relink to replace)'}")
        print(f"No candidate   : {stats['no_candidate']:>6,}   "
              f"(import candidates.json first)")
        if stats["file_absent"]:
            print(f"File absent    : {stats['file_absent']:>6,}   "
                  f"(listed in the manifest but not in {cv_out})")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
    finally:
        db.close()
    return 0


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    relink = "--relink" in argv

    def take_flag(flag: str):
        """Value after `flag`, or None. A following `--flag` is not a value —
        without this, `--manifest --apply` would read "--apply" as the path."""
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                return argv[i + 1]
        return None

    manifest_arg = take_flag("--manifest")
    cv_dir = take_flag("--cv-dir")
    src = Path(take_flag("--json") or DEFAULT_SRC)

    # --- manifest mode: the files are already in data/crm_uploads/cv ---------
    # `--manifest` with no path falls back to the standard location, so a command
    # that gets wrapped or truncated by the shell still does the right thing.
    if "--manifest" in argv:
        return link_from_manifest(Path(manifest_arg) if manifest_arg else DEFAULT_MANIFEST,
                                  apply=apply, relink=relink)

    if not cv_dir:
        print("ERROR: nothing to do. Pass one of:\n"
              f"  --manifest [path]        (default: {DEFAULT_MANIFEST})\n"
              "  --cv-dir <folder>        folder of resume files and/or .zip archives\n\n"
              "Windows example (all on ONE line):\n"
              r"  python scripts\link_candidate_resumes.py --manifest --apply")
        return 2

    cv_path = Path(cv_dir)
    if not cv_path.is_dir():
        print(f"ERROR: not a folder: {cv_path}")
        return 2

    zips = count_archives(cv_path)
    print(f"CV folder : {cv_path}")
    if zips:
        print(f"Archives  : {zips} zip file{'s' if zips != 1 else ''} — "
              f"read in place, nothing is extracted to disk")
    by_name, by_stem = build_cv_index(cv_path)
    kinds = collections.Counter(f.suffix.lower() or "(none)" for f in by_name.values())
    print(f"Resumes   : {len(by_name):,}   "
          + "  ".join(f"{ext} {n:,}" for ext, n in kinds.most_common()))
    print(f"Mode      : {'APPLY' if apply else 'DRY RUN'}{' + RELINK' if relink else ''}\n")

    # Zoho id -> resume_file / original filename, when the export is available.
    resume_by_zoho: dict[str, dict] = {}
    if src.exists():
        for rec in json.loads(src.read_text(encoding="utf-8")):
            zid = squash(rec.get("candidate_id"))
            if zid:
                resume_by_zoho[zid] = rec
        print(f"Export    : {src}  ({len(resume_by_zoho):,} records)\n")
    else:
        print(f"Export    : {src} not found — matching on candidate id only\n")

    db = get_session_factory()()
    stats = collections.Counter()
    misses: list[dict] = []
    matched: set[Path] = set()
    try:
        candidates = db.execute(select(Candidate)).scalars().all()
        cv_out = CRM_UPLOAD_DIR / CV_SUBDIR
        print(f"Candidates: {len(candidates):,}\n")

        for cand in candidates:
            if cand.cv_url and not relink:
                stats["already_linked"] += 1
                continue

            zid = squash(getattr(cand, "zoho_candidate_id", None))
            rec = resume_by_zoho.get(zid) if zid else None

            source = None
            if rec:
                resume_file = squash(rec.get("resume_file"))
                if resume_file:
                    source = by_name.get(resume_file.lower())
            if source is None and zid:
                source = by_stem.get(zid.lower())
            if source is None:
                source = by_stem.get(str(cand.id).lower())

            if source is None:
                stats["no_file"] += 1
                misses.append({
                    "candidate_id": cand.id,
                    "zoho_candidate_id": zid or "",
                    "name": " ".join(p for p in (cand.first_name, cand.last_name) if p),
                    "email": cand.email or "",
                    "expected_file": (squash(rec.get("resume_file")) if rec else "") or "",
                    "reason": "no matching file in --cv-dir",
                })
                continue

            # A Zoho bulk download that hit the API limit writes the error body to
            # the file. Linking one would give the candidate a "View CV" button
            # that opens an error page, so reject it and report it instead.
            problem = resume_problem(source.read())
            if problem:
                stats["invalid"] += 1
                misses.append({
                    "candidate_id": cand.id,
                    "zoho_candidate_id": zid or "",
                    "name": " ".join(p for p in (cand.first_name, cand.last_name) if p),
                    "email": cand.email or "",
                    "expected_file": source.name,
                    "reason": f"not a resume — {problem}",
                })
                continue

            cand.cv_url = store_cv(source, cv_out, apply=apply)
            if rec and squash(rec.get("cv_original_filename")):
                cand.cv_original_filename = squash(rec.get("cv_original_filename"))[:255]
            elif not getattr(cand, "cv_original_filename", None):
                cand.cv_original_filename = source.name[:255]
            matched.add(source)
            stats["linked"] += 1
            stats[source.suffix.lower() or "(none)"] += 1

        print(f"Linked        : {stats['linked']:>6,}")
        for ext, n in sorted((k, v) for k, v in stats.items() if k.startswith(".")):
            print(f"   {ext:<10} {n:>6,}")
        print(f"Already linked: {stats['already_linked']:>6,}"
              f"{'' if relink else '   (use --relink to replace)'}")
        print(f"No file found : {stats['no_file']:>6,}")
        if stats["invalid"]:
            print(f"REJECTED      : {stats['invalid']:>6,}   not real documents — "
                  f"re-download these from Zoho (see the report)")
        orphans = len(by_name) - len(matched)
        if orphans > 0:
            print(f"Unused files  : {orphans:>6,}   (in the folder, no candidate references them)")

        if misses:
            report = cv_path.parent / "candidates_without_resume.csv"
            with report.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(misses[0].keys()))
                w.writeheader()
                w.writerows(misses)
            print(f"Report        : {report}  ({len(misses):,} rows)")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to commit.")
    finally:
        db.close()
        close_archives()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
