r"""Attach the NEXUS CVs and resignation certificates to the imported candidates.

Inputs (7 Sep 2026 export, user request), all in one folder:
    Karnex_TA_Active_Candidates_files.csv     one row per file: Record_ID (Zoho profile id),
                                              Candidate_ID (Zoho candidate id), Source
                                              (Profile | CandidateMaster), Field (CV |
                                              Resignation_Certificate), ZIP_Part, Path_In_ZIP,
                                              Status (OK | SKIPPED - identical ...)
    Karnex_TA_Active_Candidates_part1..7.zip  the files themselves

Run AFTER import_zoho_candidate_profiles_sept2026.py — files are matched to the
profile by `candidate_profiles.zoho_profile_id`, falling back to
`candidates.zoho_candidate_id`.

Per file (Status OK only):
  Profile CV              stored under crm_uploads/resumes/ and registered as a
                          Resume on the profile's requirement (candidate, name,
                          email, phone, source "NEXUS", sha256, size, received
                          date = applied date) so it shows on the Resumes /
                          Applied Candidates tabs and can be ATS-scanned. Also
                          becomes the candidate's CV when they have none.
  CandidateMaster CV      stored under crm_uploads/cv/ and set as the candidate's
                          master CV (candidates.cv_url) — the Nexus master wins
                          over the per-profile copy.
  Resignation certificate stored under crm_uploads/cv/, set on
                          candidates.resignation_certificate_url, resignation
                          flag switched on.
Identical bytes (same sha256) already on the candidate are never stored twice,
so the script is safe to re-run. `--ats` runs the deterministic ATS scan on
each new resume (no OpenAI; failures are skipped, not fatal).

Usage (from backend/, same venv as the app):
    python tools\import_zoho_candidate_files_sept2026.py                     # DRY RUN
    python tools\import_zoho_candidate_files_sept2026.py --apply
    python tools\import_zoho_candidate_files_sept2026.py --apply --ats
    python tools\import_zoho_candidate_files_sept2026.py --dir "D:\nexus_files" --apply
"""
from __future__ import annotations

import csv
import hashlib
import logging
import sys
import uuid
import zipfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import Candidate, CandidateProfile, Requirement, Resume  # noqa: E402
from services.crm_common import ALLOWED_UPLOAD_EXTENSIONS, CRM_UPLOAD_DIR  # noqa: E402

FILES_CSV = "Karnex_TA_Active_Candidates_files.csv"
EXT_ALIAS = {".jfif": ".jpg", ".jpe": ".jpg", ".tif": ".png"}


def squash(s) -> str:
    return " ".join(str(s or "").split()).strip()


def store(data: bytes, original_name: str, subdir: str) -> tuple[str, str, int] | None:
    """Write bytes under CRM_UPLOAD_DIR/<subdir>/ with a random name; return
    (serving_url, sha256, size) or None when the extension is not allowed."""
    ext = Path(original_name).suffix.lower()[:10]
    ext = EXT_ALIAS.get(ext, ext)
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return None
    target = CRM_UPLOAD_DIR / subdir
    target.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (target / name).write_bytes(data)
    return f"/api/crm-files/{subdir}/{name}", hashlib.sha256(data).hexdigest(), len(data)


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    ats = "--ats" in argv
    # pypdf repairs Naukri PDFs with a broken xref table and logs one
    # "Ignoring wrong pointing object" line per object — thousands per run.
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    logging.getLogger("PyPDF2").setLevel(logging.ERROR)

    def take(flag):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return None

    src_dir = Path(take("--dir") or Path(__file__).resolve().parent.parent.parent / "import_templates" / "nexus_files")
    user_arg = take("--user")
    csv_path = src_dir / FILES_CSV
    if not csv_path.exists():
        print(f"Missing {csv_path}")
        return 2
    with csv_path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if squash(r.get("Status")).upper() == "OK"]
    print(f"Folder: {src_dir} · files to attach: {len(rows)}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}{' + ATS scan' if ats else ''} · upload dir: {CRM_UPLOAD_DIR}")

    zips: dict[str, zipfile.ZipFile] = {}

    def read_member(zip_name: str, member: str) -> bytes | None:
        z = zips.get(zip_name)
        if z is None:
            p = src_dir / zip_name
            if not p.exists():
                return None
            z = zips[zip_name] = zipfile.ZipFile(p)
        try:
            return z.read(member)
        except KeyError:
            return None

    db = get_session_factory()()
    written: list[Path] = []
    try:
        if user_arg:
            row = db.execute(text("SELECT id FROM registration_data WHERE lower(username)=:u OR lower(email)=:u "
                                  "ORDER BY id LIMIT 1"), {"u": user_arg.lower()}).first()
        else:
            row = db.execute(text("SELECT id FROM registration_data ORDER BY id LIMIT 1")).first()
        user_id = row[0] if row else None

        profiles = {p.zoho_profile_id: p for p in db.execute(
            select(CandidateProfile).where(CandidateProfile.zoho_profile_id.isnot(None))).scalars().all()}
        cands = {c.zoho_candidate_id: c for c in db.execute(
            select(Candidate).where(Candidate.zoho_candidate_id.isnot(None))).scalars().all()}
        req_by_opp = {r.opportunity_id: r for r in db.execute(select(Requirement)).scalars().all()}
        # sha256 already attached per candidate (resume rows + master CV files we can hash)
        seen: set[tuple[int, str]] = set()
        for cid, sha in db.execute(select(Resume.candidate_id, Resume.file_sha256)
                                   .where(Resume.file_sha256.isnot(None))).all():
            if cid:
                seen.add((cid, sha))

        def sha_of_url(url: str | None) -> str | None:
            """sha256 of a file already stored under CRM_UPLOAD_DIR (re-run safety)."""
            if not url or not url.startswith("/api/crm-files/"):
                return None
            p = CRM_UPLOAD_DIR / url.removeprefix("/api/crm-files/")
            try:
                return hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                return None

        n_resume = n_master = n_resign = n_skip = n_missing = n_dup = n_ats = 0
        # Profile CVs first so a master CV can override cv_url afterwards.
        order = {("Profile", "CV"): 0, ("CandidateMaster", "CV"): 1,
                 ("Profile", "Resignation_Certificate"): 2, ("CandidateMaster", "Resignation_Certificate"): 3}
        rows.sort(key=lambda r: order.get((squash(r["Source"]), squash(r["Field"])), 9))
        for r in rows:
            rec_id, cand_zid = squash(r["Record_ID"]), squash(r["Candidate_ID"])
            src, field = squash(r["Source"]), squash(r["Field"])
            prof = profiles.get(rec_id)
            cand = db.get(Candidate, prof.candidate_id) if prof else cands.get(cand_zid)
            if cand is None:
                n_missing += 1
                print(f"  ! no candidate in the tool for {r['Candidate_Name']} (profile {rec_id}) — import profiles first")
                continue
            data = read_member(squash(r["ZIP_Part"]), squash(r["Path_In_ZIP"]))
            if data is None:
                n_skip += 1
                print(f"  ! file not found in zip: {r['ZIP_Part']} :: {r['Path_In_ZIP']}")
                continue
            sha = hashlib.sha256(data).hexdigest()
            original = squash(r["Original_Filename"]) or Path(r["Path_In_ZIP"]).name
            key = (cand.id, sha)

            if field == "CV" and src == "Profile":
                if key in seen:
                    n_dup += 1
                    if not cand.cv_url:
                        existing = db.execute(select(Resume).where(Resume.candidate_id == cand.id,
                                                                   Resume.file_sha256 == sha)).scalars().first()
                        if existing:
                            cand.cv_url = existing.resume_file_url
                    continue
                stored = store(data, original, "resumes")
                if stored is None:
                    n_skip += 1
                    print(f"  ! unsupported file type skipped: {original}")
                    continue
                url, _, size = stored
                written.append(CRM_UPLOAD_DIR / url.removeprefix("/api/crm-files/"))
                req = req_by_opp.get(prof.opportunity_id) if prof else None
                if req is not None:
                    resume = Resume(
                        requirement_id=req.id, candidate_id=cand.id,
                        candidate_name=" ".join(x for x in (cand.first_name, cand.last_name) if x)[:255],
                        email=cand.email, phone=cand.phone, source_portal="NEXUS",
                        resume_file_url=url, file_sha256=sha, file_size=size,
                        received_date=(prof.applied_on.date() if prof and prof.applied_on else date.today()),
                    )
                    db.add(resume)
                    db.flush()
                    n_resume += 1
                    if ats and user_id:
                        try:
                            from services.resumes import run_ats_scan
                            with db.begin_nested():
                                run_ats_scan(db, resume, req, user_id)
                            n_ats += 1
                        except Exception:
                            pass
                if not cand.cv_url:
                    cand.cv_url = url
                    cand.cv_original_filename = original[:255]
                seen.add(key)

            elif field == "CV":  # CandidateMaster
                if sha_of_url(cand.cv_url) == sha:
                    n_dup += 1
                    continue
                stored = store(data, original, "cv")
                if stored is None:
                    n_skip += 1
                    print(f"  ! unsupported file type skipped: {original}")
                    continue
                url, _, _ = stored
                written.append(CRM_UPLOAD_DIR / url.removeprefix("/api/crm-files/"))
                cand.cv_url = url
                cand.cv_original_filename = original[:255]
                seen.add(key)
                n_master += 1

            else:  # resignation certificate (either source)
                if sha_of_url(cand.resignation_certificate_url) == sha:
                    n_dup += 1
                    continue
                stored = store(data, original, "cv")
                if stored is None:
                    n_skip += 1
                    print(f"  ! unsupported file type skipped: {original}")
                    continue
                url, _, _ = stored
                written.append(CRM_UPLOAD_DIR / url.removeprefix("/api/crm-files/"))
                if not cand.resignation_certificate_url or src == "CandidateMaster":
                    cand.resignation_certificate_url = url
                cand.resignation_status = True
                seen.add(key)
                n_resign += 1

        print(f"RESUMES: {n_resume} attached to profiles · MASTER CVs: {n_master} · "
              f"RESIGNATION CERTIFICATES: {n_resign} · already present {n_dup} · "
              f"skipped {n_skip} · no candidate {n_missing}" + (f" · ATS scored {n_ats}" if ats else ""))
        if apply:
            db.commit()
            print("\nAPPLIED.")
        else:
            db.rollback()
            for p in written:      # dry run leaves no files behind
                try:
                    p.unlink()
                except OSError:
                    pass
            print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0
    finally:
        db.close()
        for z in zips.values():
            z.close()


if __name__ == "__main__":
    raise SystemExit(main())
