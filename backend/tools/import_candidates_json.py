"""Import the Zoho NEXUS candidates export (candidates.json).

This is the richest candidate source available — a superset of
all_candidates_clean.csv. It covers every candidate referenced by
candidate_applied_opportunities.json, so running it FIRST makes the application
linkage resolve completely.

Upsert order (never guesses):
    zoho_candidate_id  ->  email  ->  new candidate

Candidates already in the database that are absent from this file are left
untouched — this importer never deletes.

Data handling worth knowing about:
  * 4,691 records carry the whole name in `first_name` with `last_name` blank;
    the name is split on whitespace (salutation stripped from `prefix`).
  * `experience_years` occasionally holds a pasted phone number — anything
    outside 0..60 is dropped rather than stored.
  * CTC is in lakhs for all but a handful of rows; <= 500 is multiplied by
    100000, larger values are taken as rupees already.
  * `notice_period` is numeric ("15.00") and becomes "15 days".
  * Duplicate/blank emails get a deterministic placeholder
    (<name>.<sha8>@import.karnex.in) so no candidate is lost — they stay
    linkable by zoho_candidate_id.
  * skills / preferred_locations / domains are upserted into their masters.

CV files: the export names them <candidate_id>.<ext>. Point --cv-dir at the
folder holding them — loose files, .zip archives, or both. ZIPs are read in
place, nothing is extracted. Files are copied into data/crm_uploads/cv and
linked; without --cv-dir only the original filename is recorded, and re-running
later with it fills in the links.

Dry run is the default; nothing is written without --apply.

Usage:
    python tools/import_candidates_json.py [candidates.json]
        [--apply] [--cv-dir <folder of PDFs>] [--report <skipped.csv>]
"""
from __future__ import annotations

import collections
import csv
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import Candidate, CandidateSkill, Designation, Location, Skill
from services.crm_common import CRM_UPLOAD_DIR
from tools.import_candidate_profiles import norm, numf, parse_date, slugify, squash

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "import_templates"
DEFAULT_SRC = TEMPLATES / "candidates.json"
EMAIL_DOMAIN = "import.karnex.in"
SALUTATIONS = {"mr", "mrs", "ms", "miss", "dr", "prof"}
#: Anything outside this range is a pasted phone number, not a career length.
MAX_EXPERIENCE_YEARS = 60


def split_name(record: dict) -> dict:
    """Zoho puts the whole name in first_name for most rows — split it out.

    "Mr." + "SHASHANK TYAGI" + ""  ->  Mr / SHASHANK / None / TYAGI
    """
    first = squash(record.get("first_name"))
    last = squash(record.get("last_name"))
    if not first and not last:
        first = squash(record.get("full_name"))
    tokens = [t for t in f"{first} {last}".split() if t]
    if tokens and tokens[0].strip(".").lower() in SALUTATIONS:
        tokens = tokens[1:]
    salutation = squash(record.get("prefix")).strip(".") or None
    if not tokens:
        return {"salutation": salutation, "first_name": "Unknown",
                "middle_name": None, "last_name": None}
    return {
        "salutation": salutation[:10] if salutation else None,
        "first_name": tokens[0][:120],
        "middle_name": (" ".join(tokens[1:-1])[:120] or None) if len(tokens) > 2 else None,
        "last_name": (tokens[-1][:120] if len(tokens) > 1 else None),
    }


def experience_years(raw):
    val = numf(raw)
    if val is None or not (0 <= val <= MAX_EXPERIENCE_YEARS):
        return None
    return round(val, 1)


def ctc_rupees(raw):
    """Lakhs -> rupees. Values above 500 are already rupees."""
    val = numf(raw)
    if val is None or val <= 0:
        return None
    return round(val * 100000, 2) if val <= 500 else round(val, 2)


def notice_period(raw) -> str | None:
    text = squash(raw)
    if not text:
        return None
    num = numf(text)
    if num is None:
        return text[:60]
    return "Immediate" if num == 0 else f"{int(num)} days"


def truthy(raw) -> bool:
    return norm(raw) in {"true", "yes", "1", "y"}


def placeholder_email(name: dict, zoho_id: str) -> str:
    base = slugify(name.get("first_name"), name.get("last_name"))
    digest = hashlib.sha256((zoho_id or base).encode()).hexdigest()[:8]
    return f"{base}.{digest}@{EMAIL_DOMAIN}"


# --------------------------------------------------------------------------
# CV files
#
# The export names each file <candidate_id>.<ext> and carries the real extension
# — 5,477 .pdf, 954 .docx, 62 .doc, plus an .html and a .pptx.
#
# The folder may hold loose files, .zip archives, or both. ZIPs are read in
# place — nothing is extracted to disk. Both are indexed by filename AND by
# stem, so a file whose extension was changed, or whose case differs, still
# links. Subfolders (and folders inside a ZIP) are searched.
# --------------------------------------------------------------------------

CV_SUBDIR = "cv"
#: Files a resume export sometimes carries that are never a CV.
_IGNORED_NAMES = {"thumbs.db", ".ds_store"}


#: Open ZipFile handles, keyed by archive path. Reopening a 27 MB archive for
#: every member turns 6,500 reads into minutes of work; keeping the handle makes
#: it seconds. Closed by close_archives() when the import finishes.
_OPEN_ARCHIVES: dict[Path, zipfile.ZipFile] = {}


def _archive(path: Path) -> zipfile.ZipFile:
    zf = _OPEN_ARCHIVES.get(path)
    if zf is None:
        zf = zipfile.ZipFile(path)
        _OPEN_ARCHIVES[path] = zf
    return zf


def close_archives() -> None:
    for zf in _OPEN_ARCHIVES.values():
        try:
            zf.close()
        except OSError:
            pass
    _OPEN_ARCHIVES.clear()


class CvFile:
    """One resume, either loose on disk or a member of a ZIP archive.

    Gives both sources the same tiny interface (`name`, `stem`, `suffix`,
    `read()`), so the matching and copying code does not care which it is.
    """

    __slots__ = ("path", "archive", "member", "name")

    def __init__(self, *, path: Path | None = None,
                 archive: Path | None = None, member: str | None = None):
        self.path = path
        self.archive = archive
        self.member = member
        self.name = path.name if path is not None else PurePosixPath(member or "").name

    @property
    def stem(self) -> str:
        return Path(self.name).stem

    @property
    def suffix(self) -> str:
        return Path(self.name).suffix

    @property
    def origin(self) -> str:
        return str(self.path) if self.path is not None else f"{self.archive}!{self.member}"

    def read(self) -> bytes:
        if self.path is not None:
            return self.path.read_bytes()
        return _archive(self.archive).read(self.member)


def build_cv_index(folder: Path) -> tuple[dict[str, CvFile], dict[str, CvFile]]:
    """Index every resume under `folder`, including inside .zip archives.

    Returns (by_filename, by_stem), both lower-cased. Loose files are indexed
    first so an extracted copy wins over the same name inside an archive.
    """
    by_name: dict[str, CvFile] = {}
    by_stem: dict[str, CvFile] = {}

    def add(entry: CvFile) -> None:
        low = entry.name.lower()
        if not low or low in _IGNORED_NAMES:
            return
        by_name.setdefault(low, entry)
        by_stem.setdefault(entry.stem.lower(), entry)

    archives: list[Path] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() == ".zip":
            archives.append(path)
            continue
        add(CvFile(path=path))

    for archive in archives:
        try:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    # Skip directories and macOS resource-fork noise.
                    if info.is_dir() or PurePosixPath(info.filename).name.startswith("._"):
                        continue
                    if info.filename.startswith("__MACOSX/"):
                        continue
                    add(CvFile(archive=archive, member=info.filename))
        except (zipfile.BadZipFile, OSError) as err:
            print(f"  WARNING: could not read {archive.name}: {err}")

    return by_name, by_stem


def count_archives(folder: Path) -> int:
    return sum(1 for p in folder.rglob("*") if p.is_file() and p.suffix.lower() == ".zip")


def find_cv(record: dict, by_name: dict[str, CvFile],
            by_stem: dict[str, CvFile]) -> CvFile | None:
    """Locate this candidate's CV: exact filename, then <candidate_id>.<anything>."""
    resume_file = squash(record.get("resume_file"))
    if resume_file:
        hit = by_name.get(PurePosixPath(resume_file).name.lower())
        if hit is not None:
            return hit
    zoho_id = squash(record.get("candidate_id"))
    if zoho_id:
        hit = by_stem.get(zoho_id.lower())
        if hit is not None:
            return hit
    return None


#: Magic bytes for the formats a real resume can be in.
_RESUME_SIGNATURES = (
    b"%PDF",            # pdf
    b"PK",              # docx / pptx (zip container)
    b"\xd0\xcf\x11\xe0",  # legacy .doc / OLE compound file
    b"{\\rtf",          # rtf
)
#: Smaller than this and it cannot be a CV — the Zoho stubs are exactly 120 bytes.
_MIN_RESUME_BYTES = 512


def resume_problem(data: bytes) -> str | None:
    """Return why this file is not a usable resume, or None if it looks fine.

    The Zoho bulk download saves the API response body whatever it contains, so a
    rate-limited run produces thousands of 120-byte files like:

        {"code": 4000, "message": "Account's Developer API limit has been
         reached. Please upgrade to execute more REST API calls."}

    They carry a .pdf/.docx extension and would otherwise be linked as real CVs,
    giving every affected candidate a "View CV" button that opens an error.
    """
    if not data:
        return "empty file"
    head = data.lstrip()[:400]
    if head[:1] == b"{":
        try:
            payload = json.loads(data.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and {"code", "message"} <= set(payload):
            return f"Zoho API error: {str(payload.get('message'))[:80]}"
        return "JSON payload, not a document"
    if data.startswith(_RESUME_SIGNATURES):
        return None
    lowered = head[:64].lower()
    if lowered.startswith((b"<!doctype html", b"<html")):
        return None  # Naukri exports HTML resumes with a .doc extension
    if len(data) < _MIN_RESUME_BYTES:
        return f"suspiciously small ({len(data)} bytes)"
    return None  # unrecognised but substantial — let it through


def store_cv(source: CvFile, dest_dir: Path, *, apply: bool) -> str:
    """Write a CV into the CRM upload folder, keeping its real extension.

    The stored name is a hash of the source filename, so re-running is idempotent
    (the same CV overwrites itself rather than accumulating copies). Works the
    same whether the source is a loose file or a ZIP member.
    """
    ext = source.suffix.lower() or ".pdf"
    stored = f"{hashlib.sha256(source.name.encode()).hexdigest()[:32]}{ext}"
    if apply:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / stored
        if source.path is not None:
            shutil.copy2(source.path, target)
        else:
            target.write_bytes(source.read())
    return f"/api/crm-files/{CV_SUBDIR}/{stored}"


def main() -> int:  # noqa: C901 - a linear import script reads better in one piece
    argv = sys.argv[1:]
    apply = "--apply" in argv

    def take_flag(flag: str):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    cv_dir = take_flag("--cv-dir")
    report_arg = take_flag("--report")
    positional = [a for i, a in enumerate(argv)
                  if not a.startswith("--")
                  and (i == 0 or argv[i - 1] not in {"--cv-dir", "--report"})]
    src = Path(positional[0]) if positional else DEFAULT_SRC
    if not src.exists():
        print(f"ERROR: source not found: {src}")
        return 2
    cv_path = Path(cv_dir) if cv_dir else None
    if cv_path and not cv_path.is_dir():
        print(f"ERROR: --cv-dir is not a folder: {cv_path}")
        return 2
    report_path = Path(report_arg) if report_arg else src.parent / "import_candidates_report.csv"

    records = json.loads(src.read_text(encoding="utf-8"))
    print(f"Source  : {src}")
    print(f"Records : {len(records):,}")
    print(f"CV files: {cv_path or 'not supplied — filenames recorded only'}")
    print(f"Mode    : {'APPLY' if apply else 'DRY RUN'}\n")

    db = get_session_factory()()
    # Counter, not dict: a counter key added later (email_conflict, cvs_invalid,
    # the per-extension cv_* tallies) must not blow up on first increment.
    stats: collections.Counter[str] = collections.Counter()
    notes: list[dict] = []

    try:
        by_zoho: dict[str, Candidate] = {}
        by_email: dict[str, Candidate] = {}
        for c in db.execute(select(Candidate)).scalars().all():
            zid = getattr(c, "zoho_candidate_id", None)
            if zid:
                by_zoho[zid] = c
            if c.email:
                by_email[norm(c.email)] = c
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        locs_by_city = {norm(l.city): l for l in db.execute(select(Location)).scalars().all()}
        desigs = {norm(d.name): d for d in db.execute(select(Designation)).scalars().all()}
        skill_links = {(l.candidate_id, l.skill_id)
                       for l in db.execute(select(CandidateSkill)).scalars().all()}
        print(f"Existing: {len(by_email):,} candidates "
              f"({len(by_zoho):,} already carry a Zoho id)\n")

        cv_out = CRM_UPLOAD_DIR / CV_SUBDIR
        cv_by_name: dict[str, Path] = {}
        cv_by_stem: dict[str, Path] = {}
        matched_cv_files: set[Path] = set()
        if cv_path:
            zips = count_archives(cv_path)
            cv_by_name, cv_by_stem = build_cv_index(cv_path)
            print(f"CV folder: {len(cv_by_name):,} resumes indexed under {cv_path}"
                  + (f"  (from {zips} zip archive{'s' if zips != 1 else ''}"
                     " — read in place, nothing extracted)" if zips else ""))
            kinds = collections.Counter(p.suffix.lower() or "(none)" for p in cv_by_name.values())
            print("           " + "  ".join(f"{ext} {n:,}" for ext, n in kinds.most_common()) + "\n")

        used_emails = set(by_email)
        #: candidate.id -> the Zoho id that took it during THIS run.
        claimed: dict[int, str] = {}
        for rec in records:
            zoho_id = squash(rec.get("candidate_id"))
            name = split_name(rec)
            email = squash(rec.get("email")).lower() or None

            # Two records may legitimately share an email (7 pairs in this export).
            # Without the `claimed` guard the second one matches the row the first
            # just took and overwrites it, silently losing a candidate — so a row
            # already claimed by a DIFFERENT Zoho id this run is not a match.
            cand = by_zoho.get(zoho_id) if zoho_id else None
            if cand is not None and claimed.get(cand.id, zoho_id) != zoho_id:
                cand = None
            if cand is not None:
                stats["matched_zoho"] += 1
            elif email and email in by_email:
                hit = by_email[email]
                if claimed.get(hit.id, zoho_id) == zoho_id:
                    cand = hit
                    stats["matched_email"] += 1
                else:
                    stats["email_conflict"] += 1

            # A blank email, or one already taken by a DIFFERENT candidate, gets a
            # deterministic placeholder so this record is still imported.
            if cand is None:
                if not email or email in used_emails:
                    email = placeholder_email(name, zoho_id)
                    stats["placeholder"] += 1
                    notes.append({"candidate_id": zoho_id, "name": rec.get("full_name"),
                                  "email": squash(rec.get("email")),
                                  "reason": "duplicate or blank email — placeholder assigned",
                                  "assigned_email": email})
            elif email and norm(email) != norm(cand.email or "") and email in used_emails:
                email = cand.email  # keep the existing address rather than collide

            if cand is None:
                cand = Candidate(email=email)
                db.add(cand)
                stats["created"] += 1
            else:
                stats["updated"] += 1
                if email:
                    cand.email = email
            used_emails.add(norm(email or ""))

            for field, value in name.items():
                setattr(cand, field, value)
            cand.zoho_candidate_id = (zoho_id or None) and zoho_id[:32]
            cand.phone = squash(rec.get("phone"))[:32] or None
            cand.gender = squash(rec.get("gender"))[:20] or None
            cand.date_of_birth = parse_date(rec.get("date_of_birth"))
            cand.city = squash(rec.get("city"))[:120] or None
            cand.experience_years = experience_years(rec.get("experience_years"))
            cand.notice_period = notice_period(rec.get("notice_period"))
            cand.current_address = squash(rec.get("current_address")) or None
            cand.permanent_address = squash(rec.get("permanent_address")) or None
            cand.roles = squash(rec.get("roles"))[:255] or None
            cand.current_ctc = ctc_rupees(rec.get("current_ctc_lac"))
            cand.expected_ctc = ctc_rupees(rec.get("expected_ctc_lac"))
            cand.resignation_status = truthy(rec.get("is_resigned"))
            cand.last_working_day = parse_date(rec.get("last_working_day"))
            cand.source_created_date = parse_date(rec.get("created_date"))
            cand.recruiter_email = squash(rec.get("recruiter"))[:255] or None
            cand.cv_original_filename = squash(rec.get("cv_original_filename"))[:255] or None

            domains = [squash(d) for d in (rec.get("domains") or []) if squash(d)]
            if domains:
                cand.technical_domain = ", ".join(dict.fromkeys(domains))[:120]

            # Designation from the free-text role, so the master stays populated.
            role = squash(rec.get("roles"))
            if role:
                desig = desigs.get(norm(role))
                if desig is None:
                    desig = Designation(name=role[:120], is_active=True)
                    db.add(desig)
                    db.flush()
                    desigs[norm(role)] = desig
                cand.designation_id = desig.id

            locations = [squash(x) for x in (rec.get("preferred_locations") or []) if squash(x)]
            locations = list(dict.fromkeys(locations))
            if locations:
                cand.preferred_locations = ", ".join(locations)[:500]
                primary = locations[0]
                loc = locs_by_city.get(norm(primary))
                if loc is None:
                    loc = Location(city=primary[:120], country="India")
                    db.add(loc)
                    db.flush()
                    locs_by_city[norm(primary)] = loc
                cand.preferred_location_id = loc.id

            db.flush()
            claimed[cand.id] = zoho_id
            if zoho_id:
                by_zoho[zoho_id] = cand
            if cand.email:
                by_email.setdefault(norm(cand.email), cand)

            for raw_skill in (rec.get("skills") or []):
                sname = squash(raw_skill)
                if not sname:
                    continue
                skill = skills_by_name.get(norm(sname))
                if skill is None:
                    skill = Skill(name=sname[:120], is_active=True)
                    db.add(skill)
                    db.flush()
                    skills_by_name[norm(sname)] = skill
                if (cand.id, skill.id) not in skill_links:
                    db.add(CandidateSkill(candidate_id=cand.id, skill_id=skill.id))
                    skill_links.add((cand.id, skill.id))
                    stats["skills"] += 1

            if cv_path and squash(rec.get("resume_file")):
                source = find_cv(rec, cv_by_name, cv_by_stem)
                if source is None:
                    stats["cvs_missing"] += 1
                    notes.append({"candidate_id": zoho_id, "name": rec.get("full_name"),
                                  "email": cand.email, "reason": "CV file not found in --cv-dir",
                                  "assigned_email": squash(rec.get("resume_file"))})
                else:
                    problem = resume_problem(source.read())
                    if problem:
                        # Never link a stub — the candidate is better off with no CV
                        # than a button that opens a Zoho error page.
                        stats["cvs_invalid"] += 1
                        notes.append({"candidate_id": zoho_id, "name": rec.get("full_name"),
                                      "email": cand.email,
                                      "reason": f"not a resume — {problem}",
                                      "assigned_email": squash(rec.get("resume_file"))})
                    else:
                        cand.cv_url = store_cv(source, cv_out, apply=apply)
                        matched_cv_files.add(source)
                        stats["cvs_copied"] += 1
                        stats[f"cv_{source.suffix.lower().lstrip('.') or 'none'}"] += 1

        db.flush()

        if notes:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            with report_path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(
                    fh, fieldnames=["candidate_id", "name", "email", "reason", "assigned_email"])
                w.writeheader()
                w.writerows(notes)

        print(f"Created        : {stats['created']:>6,}")
        print(f"Updated        : {stats['updated']:>6,}   "
              f"(matched on Zoho id {stats['matched_zoho']:,}, on email {stats['matched_email']:,})")
        print(f"Skill links    : {stats['skills']:>6,} added")
        print(f"Placeholder    : {stats['placeholder']:>6,} emails synthesised "
              f"(duplicate or blank)")
        if cv_path:
            by_type = "  ".join(
                f"{k[3:]} {v:,}" for k, v in sorted(stats.items())
                if k.startswith("cv_") and v)
            print(f"CVs linked     : {stats['cvs_copied']:>6,}   {by_type}")
            print(f"CVs missing    : {stats['cvs_missing']:>6,}   "
                  f"(no file for that candidate in --cv-dir)")
            if stats.get("cvs_invalid"):
                print(f"CVs REJECTED   : {stats['cvs_invalid']:>6,}   "
                      f"(not real documents — see the report; re-download these)")
            orphans = len(cv_by_name) - len(matched_cv_files)
            if orphans > 0:
                print(f"Unused files   : {orphans:>6,}   "
                      f"(in the folder but no candidate references them)")
        if notes:
            print(f"Report         : {report_path}  ({len(notes):,} rows)")

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
