r"""Import the FULL candidate export (all Zoho fields) into the CRM.

Source CSV: import_templates\all_candidates_clean.csv

Usage (from the backend folder, same venv as the app):

    python tools\import_candidates_full.py                    # DRY RUN
    python tools\import_candidates_full.py --replace          # + remove candidates not in CSV (dry run)
    python tools\import_candidates_full.py --replace --apply  # write everything
    python tools\import_candidates_full.py --cv-dir "D:\CVs" --replace --apply

CSV columns used: Name, Email, Phone_Number, Gender, Date_of_Birth, Expereince,
Notice_Period, CTC, Expected_CTC_Lac_Annual, Skills, Roles, Domains, City,
Prefered_Location, Current_Address, Permament_Address, Is_Resigned,
Last_Working_Day, Resignation_Certificate, CV, Opportunity_ID.

Behaviour:
  * Upsert by Email (the app's unique key). Rows without an email get a stable
    placeholder <name-slug>.<hash>@import.karnex.in so they still import.
  * Duplicate emails in the CSV are merged (non-empty values win).
  * Skills are comma/semicolon split, upserted into the Skills master, linked.
  * Roles -> designation master (upserted); Prefered_Location / City -> location
    master (upserted, first value when several are given).
  * CTC / Expected CTC are lac -> rupees; junk experience (>60y) is dropped.
  * Numeric notice periods are stored as "<n> days".
  * Opportunity_ID: when filled, the candidate is linked to that opportunity as
    a candidate profile (pipeline: Sourcing). Blank in the current export.
  * --cv-dir: folder (searched recursively) with the downloaded CV files;
    matches are copied into app storage and linked so the CV opens in the UI.
  * --replace: candidates NOT in the CSV are deleted using the app's own
    cascade (profiles, AI links, bookings, outreach; resumes are detached).
    Candidates linked to an employee record are kept and reported.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import hashlib
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select

from crm_db import get_session_factory
from models import (
    Candidate, CandidateOutreach, CandidateProfile, CandidateSkill, Designation,
    Employee, Location, Opportunity, PipelineStatus, Resume, Skill,
)
from services.crm_common import CRM_UPLOAD_DIR
from services.crm_delete import cascade_candidate_children

EMAIL_DOMAIN = "import.karnex.in"
SALUTATIONS = {"mr", "mrs", "ms", "miss", "dr", "prof"}
SPLIT_RE = re.compile(r"[;,]")


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def norm(s: str | None) -> str:
    return squash(s).lower()


def numf(v):
    v = squash(v)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def boolf(v):
    return norm(v) in ("true", "yes", "1")


def parse_date(raw):
    raw = squash(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


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
    return {
        "salutation": sal,
        "first_name": tokens[0][:120],
        "middle_name": (" ".join(tokens[1:-1])[:120] or None) if len(tokens) > 2 else None,
        "last_name": tokens[-1][:120] if len(tokens) > 1 else None,
    }


def slugify(*parts) -> str:
    s = re.sub(r"[^a-z0-9.]+", "", ".".join(p for p in parts if p).lower())
    return s.strip(".") or "candidate"


def split_multi(raw: str) -> list[str]:
    out: list[str] = []
    for piece in SPLIT_RE.split(raw or ""):
        p = squash(piece)
        if p and p.lower() not in [x.lower() for x in out]:
            out.append(p)
    return out


def load_rows(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            name = parse_name(r.get("Name") or "")
            email = squash(r.get("Email"))
            if name is None and not email:
                continue
            if name is None:  # email only -> derive a name from the local part
                local = email.split("@")[0].replace(".", " ").replace("_", " ")
                name = parse_name(local) or {"salutation": None, "first_name": local[:120],
                                             "middle_name": None, "last_name": None}
            exp = numf(r.get("Expereince"))
            if exp is not None and not (0 <= exp <= 60):
                exp = None

            def ctc(v):
                x = numf(v)
                if x is None or x <= 0:
                    return None
                return round(x * 100000, 2) if x <= 500 else round(x, 2)

            np_raw = squash(r.get("Notice_Period"))
            np_num = numf(np_raw)
            notice = f"{int(np_num)} days" if np_num is not None else (np_raw or None)
            key = norm(email) or f"__noemail__{slugify(name['first_name'], name['last_name'])}." \
                                 f"{hashlib.sha256((r.get('zohoRecId') or '').encode()).hexdigest()[:8]}"
            out.append({
                **name,
                "key": key,
                "email": email or None,
                "phone": squash(r.get("Phone_Number"))[:32] or None,
                "gender": squash(r.get("Gender"))[:20] or None,
                "date_of_birth": parse_date(r.get("Date_of_Birth")),
                "experience_years": exp,
                "notice_period": (notice or "")[:60] or None,
                "current_ctc": ctc(r.get("CTC")),
                "expected_ctc": ctc(r.get("Expected_CTC_Lac_Annual")),
                "current_address": squash(r.get("Current_Address")) or None,
                "permanent_address": squash(r.get("Permament_Address")) or None,
                "technical_domain": (split_multi(r.get("Domains")) or [None])[0],
                "roles": ", ".join(split_multi(r.get("Roles")))[:255] or None,
                "resignation_status": boolf(r.get("Is_Resigned")),
                "last_working_day": parse_date(r.get("Last_Working_Day")),
                "designation": (split_multi(r.get("Roles")) or [None])[0],
                "location": (split_multi(r.get("Prefered_Location")) or split_multi(r.get("City")) or [None])[0],
                "skills": [s[:120] for s in split_multi(r.get("Skills"))],
                "cv_name": squash(r.get("CV")) or None,
                "opportunity_id": squash(r.get("Opportunity_ID")) or None,
                "zoho_id": squash(r.get("zohoRecId")) or None,
            })
    return out


def merge_rows(rows: list[dict]) -> list[dict]:
    """Merge duplicate keys (same email) — later non-empty values win."""
    merged: dict[str, dict] = {}
    for r in rows:
        cur = merged.get(r["key"])
        if cur is None:
            merged[r["key"]] = dict(r)
            continue
        for k, v in r.items():
            if k in ("key", "skills"):
                continue
            if v not in (None, "", False):
                cur[k] = v
        for s in r["skills"]:
            if s.lower() not in [x.lower() for x in cur["skills"]]:
                cur["skills"].append(s)
    return list(merged.values())


def index_dir(folder: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for p in folder.rglob("*"):
        if p.is_file():
            files.setdefault(p.name.lower(), p)
    return files


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    replace = "--replace" in argv

    def take_flag(flag: str):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                val = argv[i + 1]
                del argv[i:i + 2]
                return val
            print(f"{flag} needs a value")
            raise SystemExit(2)
        return None

    cv_dir_arg = take_flag("--cv-dir")
    cv_dir = Path(cv_dir_arg) if cv_dir_arg else None
    if cv_dir and not cv_dir.is_dir():
        print(f"CV folder not found: {cv_dir}")
        return 2
    paths = [a for a in argv if not a.startswith("--")]
    csv_path = Path(paths[0]) if paths else (
        Path(__file__).resolve().parent.parent.parent / "import_templates" / "all_candidates_clean.csv")
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    rows = merge_rows(load_rows(csv_path))
    print(f"CSV candidate rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    cv_files = index_dir(cv_dir) if cv_dir else {}
    if cv_dir:
        print(f"CV folder: {cv_dir}  ({len(cv_files)} files found)")

    db = get_session_factory()()
    created = updated = deleted = kept = 0
    cv_linked = cv_missing = opp_links = opp_unmatched = 0
    try:
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        desigs = {norm(d.name): d for d in db.execute(select(Designation)).scalars().all()}
        locs = {norm(l.city): l for l in db.execute(select(Location)).scalars().all()}
        opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        existing = {norm(c.email): c for c in db.execute(select(Candidate)).scalars().all()}
        emp_cand_ids = set()
        for e in db.execute(select(Employee)).scalars().all():
            prof_id = getattr(e, "candidate_profile_id", None)
            if prof_id:
                prof = db.get(CandidateProfile, prof_id)
                if prof:
                    emp_cand_ids.add(prof.candidate_id)
        links = {(l.candidate_id, l.skill_id)
                 for l in db.execute(select(CandidateSkill)).scalars().all()}

        # ---- resolve final emails (placeholder for rows without one) --------
        used = set(existing)
        for r in rows:
            if not r["email"]:
                base = f"{slugify(r['first_name'], r['last_name'])}." \
                       f"{hashlib.sha256((r['zoho_id'] or r['key']).encode()).hexdigest()[:8]}"
                r["email"] = f"{base}@{EMAIL_DOMAIN}"
            e = norm(r["email"])
            if e in used and e not in existing:
                local, _, dom = r["email"].partition("@")
                r["email"] = f"{local}.{hashlib.sha256(r['key'].encode()).hexdigest()[:4]}@{dom}"
            used.add(norm(r["email"]))

        # ---- masters -------------------------------------------------------
        for r in rows:
            for s in r["skills"]:
                if norm(s) not in skills_by_name:
                    obj = Skill(name=s, is_active=True)
                    db.add(obj)
                    skills_by_name[norm(s)] = obj
            if r["designation"] and norm(r["designation"]) not in desigs:
                obj = Designation(name=r["designation"][:120], is_active=True)
                db.add(obj)
                desigs[norm(r["designation"])] = obj
            if r["location"] and norm(r["location"]) not in locs:
                obj = Location(city=r["location"][:120], country="India")
                db.add(obj)
                locs[norm(r["location"])] = obj
        db.flush()

        # ---- replace: delete candidates missing from the CSV ---------------
        if replace:
            csv_emails = {norm(r["email"]) for r in rows}
            for key, cand in list(existing.items()):
                if key in csv_emails:
                    continue
                if cand.id in emp_cand_ids:
                    kept += 1
                    print(f"  ! kept {cand.email}: linked to an employee record")
                    continue
                cascade_candidate_children(db, cand.id)
                db.execute(CandidateOutreach.__table__.delete()
                           .where(CandidateOutreach.candidate_id == cand.id))
                db.execute(Resume.__table__.update()
                           .where(Resume.candidate_id == cand.id)
                           .values(candidate_id=None))
                db.execute(CandidateSkill.__table__.delete()
                           .where(CandidateSkill.candidate_id == cand.id))
                db.flush()
                db.delete(cand)
                db.flush()
                del existing[key]
                deleted += 1

        # ---- upsert candidates ---------------------------------------------
        cv_target = CRM_UPLOAD_DIR / "cv"
        for r in rows:
            key = norm(r["email"])
            cand = existing.get(key)
            fields = {
                "salutation": r["salutation"],
                "first_name": r["first_name"],
                "middle_name": r["middle_name"],
                "last_name": r["last_name"],
                "phone": r["phone"],
                "gender": r["gender"],
                "date_of_birth": r["date_of_birth"],
                "experience_years": r["experience_years"],
                "notice_period": r["notice_period"],
                "current_ctc": r["current_ctc"],
                "expected_ctc": r["expected_ctc"],
                "current_address": r["current_address"],
                "permanent_address": r["permanent_address"],
                "technical_domain": r["technical_domain"],
                "roles": r["roles"],
                "resignation_status": bool(r["resignation_status"]),
                "last_working_day": r["last_working_day"],
                "designation_id": desigs[norm(r["designation"])].id if r["designation"] else None,
                "preferred_location_id": locs[norm(r["location"])].id if r["location"] else None,
            }
            if r["cv_name"] and cv_files:
                src = cv_files.get(r["cv_name"].lower())
                if src is None:
                    cv_missing += 1
                else:
                    data = src.read_bytes()
                    stored = f"{hashlib.sha256(data).hexdigest()[:32]}{Path(r['cv_name']).suffix[:10]}"
                    if apply:
                        cv_target.mkdir(parents=True, exist_ok=True)
                        dest = cv_target / stored
                        if not dest.exists():
                            shutil.copyfile(src, dest)
                    fields["cv_url"] = f"/api/crm-files/cv/{stored}"
                    cv_linked += 1

            if cand is None:
                cand = Candidate(email=r["email"], **fields)
                db.add(cand)
                db.flush()
                existing[key] = cand
                created += 1
            else:
                for f, v in fields.items():
                    if v is not None:
                        setattr(cand, f, v)
                updated += 1

            for s in r["skills"]:
                sk = skills_by_name[norm(s)]
                if (cand.id, sk.id) not in links:
                    db.add(CandidateSkill(candidate_id=cand.id, skill_id=sk.id))
                    links.add((cand.id, sk.id))

            # ---- link to opportunity (candidate profile) --------------------
            if r["opportunity_id"]:
                opp = opps.get(r["opportunity_id"])
                if opp is None:
                    opp_unmatched += 1
                    print(f"      opportunity not found for {r['email']}: {r['opportunity_id']}")
                else:
                    dupe = db.execute(
                        select(CandidateProfile).where(
                            CandidateProfile.candidate_id == cand.id,
                            CandidateProfile.opportunity_id == opp.id,
                        )
                    ).scalars().first()
                    if dupe is None:
                        db.add(CandidateProfile(
                            candidate_id=cand.id, opportunity_id=opp.id,
                            current_ctc=r["current_ctc"], expected_ctc=r["expected_ctc"],
                            pipeline_status=PipelineStatus.SOURCING,
                        ))
                        opp_links += 1

        print(f"\nCANDIDATES: {created} created, {updated} updated"
              + (f", {deleted} deleted, {kept} kept (linked to employees)" if replace else ""))
        print(f"Opportunity links: {opp_links} created, {opp_unmatched} unmatched")
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
        import traceback
        traceback.print_exc()
        print(f"FAILED - rolled back, nothing changed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
