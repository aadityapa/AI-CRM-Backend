"""Import the candidate master from a CSV export (All Candidates).

Usage (from the backend folder, same venv as the app):

    python tools\import_candidates.py                                  # DRY RUN
    python tools\import_candidates.py --apply                          # write to the DB
    python tools\import_candidates.py --cv-dir "D:\CV Downloads" --apply
    python tools\import_candidates.py ..\import_templates\all_candidates.csv --apply

CSV columns: Name, Roles, Domain, Expereince, Skills, CV, Notice Period,
             CTC (Lac) Annual

Behaviour:
  * The app requires a unique email per candidate; the CSV has none, so a
    deterministic placeholder is generated: <name-slug>.<hash>@import.karnex.in
    (same row always gets the same email, so re-runs UPDATE instead of
    duplicating). Replace with real emails in the UI when known.
  * Name is split into salutation / first / middle / last.
  * Skills are comma-split, upserted into the skills master, and linked.
  * Experience > 60 years and CTC > 500 lac are treated as junk -> left empty.
    Numeric notice periods are stored as "<n> days".
  * --cv-dir: folder (searched recursively) holding the downloaded CV files.
    Matching files are copied into the app's storage (data/crm_uploads/cv) and
    linked so the CV opens in the UI. Without --cv-dir, or when a file is
    missing, the CV is left empty and counted in the report.
  * DRY RUN by default; nothing is written without --apply (CV files are also
    only copied in apply mode).
"""
from __future__ import annotations

import csv
import hashlib
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import Candidate, CandidateSkill, Skill
from services.crm_common import CRM_UPLOAD_DIR

EMAIL_DOMAIN = "import.karnex.in"
SALUTATIONS = {"mr", "mrs", "ms", "miss", "dr", "prof"}


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def parse_name(raw: str) -> dict | None:
    txt = squash(raw).strip(" .")
    if not txt:
        return None
    tokens = [t for t in txt.split(" ") if t.strip(".")]
    sal = None
    if tokens and tokens[0].strip(".").lower() in SALUTATIONS:
        sal = tokens[0].strip(".")
        tokens = tokens[1:]
    if not tokens:
        return None
    first = tokens[0][:120]
    last = tokens[-1][:120] if len(tokens) > 1 else None
    middle = " ".join(tokens[1:-1])[:120] or None if len(tokens) > 2 else None
    return {"salutation": sal, "first_name": first, "middle_name": middle, "last_name": last}


def slugify(*parts: str | None) -> str:
    s = ".".join(p for p in parts if p)
    s = re.sub(r"[^a-z0-9.]+", "", s.lower())
    return s.strip(".") or "candidate"


def numf(v: str | None) -> float | None:
    v = (v or "").strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_rows(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            name = parse_name(r.get("Name") or "")
            if name is None:
                continue
            exp = numf(r.get("Expereince"))
            if exp is not None and not (0 <= exp <= 60):
                exp = None
            ctc_lac = numf(r.get("CTC (Lac) Annual"))
            ctc = None
            if ctc_lac is not None and 0 < ctc_lac <= 500:
                ctc = round(ctc_lac * 100000, 2)          # value given in lac
            elif ctc_lac is not None and 100000 <= ctc_lac <= 5e8:
                ctc = round(ctc_lac, 2)                    # value entered in rupees
            np_raw = squash(r.get("Notice Period"))
            np_num = numf(np_raw)
            notice = (f"{int(np_num)} days" if np_num is not None else (np_raw or None))
            notice = notice[:60] if notice else None
            cv_name = squash(r.get("CV")) or None
            skills = []
            for s in (r.get("Skills") or "").split(","):
                s = squash(s)
                if s and s.lower() not in [x.lower() for x in skills]:
                    skills.append(s[:120])
            email = (f"{slugify(name['first_name'], name['last_name'])}."
                     f"{hashlib.sha256(((r.get('Name') or '') + '|' + (cv_name or '')).encode()).hexdigest()[:8]}"
                     f"@{EMAIL_DOMAIN}")
            out.append({
                **name,
                "email": email,
                "experience_years": exp,
                "current_ctc": ctc,
                "notice_period": notice,
                "roles": squash(r.get("Roles"))[:255] or None,
                "technical_domain": squash(r.get("Domain"))[:120] or None,
                "cv_name": cv_name,
                "skills": skills,
            })
    return out


def dedupe_emails(rows: list[dict]) -> None:
    seen: dict[str, int] = {}
    for r in rows:
        e = r["email"]
        if e in seen:
            seen[e] += 1
            local, _, dom = e.partition("@")
            r["email"] = f"{local}.{seen[e]}@{dom}"
        else:
            seen[e] = 0


def index_cv_dir(cv_dir: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for p in cv_dir.rglob("*"):
        if p.is_file():
            files.setdefault(p.name.lower(), p)
    return files


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    cv_dir = None
    if "--cv-dir" in argv:
        i = argv.index("--cv-dir")
        if i + 1 >= len(argv):
            print("--cv-dir needs a folder path")
            return 2
        cv_dir = Path(argv[i + 1])
        if not cv_dir.is_dir():
            print(f"CV folder not found: {cv_dir}")
            return 2
        argv = argv[:i] + argv[i + 2:]
    paths = [a for a in argv if not a.startswith("--")]
    csv_path = Path(paths[0]) if paths else (
        Path(__file__).resolve().parent.parent.parent / "import_templates" / "all_candidates.csv")
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    rows = load_rows(csv_path)
    dedupe_emails(rows)
    print(f"CSV candidate rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    cv_files = index_cv_dir(cv_dir) if cv_dir else {}
    if cv_dir:
        print(f"CV folder: {cv_dir}  ({len(cv_files)} files found)")

    db = get_session_factory()()
    created = updated = cv_linked = cv_missing = 0
    try:
        skill_rows = {s.name.lower(): s for s in db.execute(select(Skill)).scalars().all()}
        existing = {c.email.lower(): c for c in db.execute(select(Candidate)).scalars().all()}
        existing_links: set[tuple[int, int]] = {
            (link.candidate_id, link.skill_id)
            for link in db.execute(select(CandidateSkill)).scalars().all()
        }

        # Upsert skills master first so ids exist.
        wanted_skills: dict[str, str] = {}
        for r in rows:
            for s in r["skills"]:
                wanted_skills.setdefault(s.lower(), s)
        new_skills = [n for k, n in wanted_skills.items() if k not in skill_rows]
        for name in new_skills:
            obj = Skill(name=name, is_active=True)
            db.add(obj)
            skill_rows[name.lower()] = obj
        if new_skills:
            db.flush()
        print(f"SKILLS master: {len(new_skills)} new "
              f"({', '.join(sorted(new_skills)[:15])}{'...' if len(new_skills) > 15 else ''})")

        cv_target = CRM_UPLOAD_DIR / "cv"
        for r in rows:
            cand = existing.get(r["email"].lower())
            cv_url = None
            if r["cv_name"] and cv_files:
                src = cv_files.get(r["cv_name"].lower())
                if src is None:
                    cv_missing += 1
                else:
                    ext = Path(r["cv_name"]).suffix[:10]
                    data = src.read_bytes()
                    stored = f"{hashlib.sha256(data).hexdigest()[:32]}{ext}"
                    if apply:
                        cv_target.mkdir(parents=True, exist_ok=True)
                        dest = cv_target / stored
                        if not dest.exists():
                            shutil.copyfile(src, dest)
                    cv_url = f"/api/crm-files/cv/{stored}"
                    cv_linked += 1
            fields = {
                "salutation": r["salutation"],
                "first_name": r["first_name"],
                "middle_name": r["middle_name"],
                "last_name": r["last_name"],
                "experience_years": r["experience_years"],
                "current_ctc": r["current_ctc"],
                "notice_period": r["notice_period"],
                "roles": r["roles"],
                "technical_domain": r["technical_domain"],
            }
            if cv_url:
                fields["cv_url"] = cv_url
            if cand is None:
                cand = Candidate(email=r["email"], **fields)
                db.add(cand)
                db.flush()
                existing[r["email"].lower()] = cand
                created += 1
            else:
                for f, v in fields.items():
                    if v is not None:
                        setattr(cand, f, v)
                updated += 1
            for s in r["skills"]:
                sk = skill_rows[s.lower()]
                if (cand.id, sk.id) not in existing_links:
                    db.add(CandidateSkill(candidate_id=cand.id, skill_id=sk.id))
                    existing_links.add((cand.id, sk.id))

        print(f"CANDIDATES: {created} created, {updated} updated")
        if cv_dir:
            print(f"CVs: {cv_linked} linked, {cv_missing} filenames not found in folder")
        elif any(r["cv_name"] for r in rows):
            print("CVs: skipped (no --cv-dir given)")

        if apply:
            db.commit()
            print("\nAPPLIED - candidate import complete.")
        else:
            db.rollback()
            print("\nDry run only - nothing changed. Re-run with --apply to write.")
        return 0
    except Exception as exc:
        db.rollback()
        print(f"FAILED - rolled back, nothing changed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
