r"""Import sales opportunities from a CSV export, linked to customer + branch.

Run replace_customers.py and import_branches.py first so branches exist.

Usage (from the backend folder, same venv as the app):

    python tools\import_opportunities.py                                   # DRY RUN
    python tools\import_opportunities.py --apply                           # write to the DB
    python tools\import_opportunities.py --jd-dir "D:\JD Files" --apply
    python tools\import_opportunities.py --user admin --apply
    python tools\import_opportunities.py --replace --apply   # DB ends up = the CSV

CSV columns: Opportunity ID, Opportunity Title, Opportunity Type, Branch,
             Industry, Stage, Received Date, Oppurtunity Status,
             JD Attachment, Attachments, Positions (Count)

Behaviour:
  * Upsert by Opportunity ID (e.g. C-2026-00075).
  * Branch name resolves the branch AND its customer; rows with unknown or
    ambiguous branch names are reported and SKIPPED.
  * "Time & Material" -> T&M. Status: Open -> Active, Customer Hold -> On_Hold.
    Stage (Sales Accepted / Bidding / Validation) is kept in details.sales_stage;
    Positions (Count) in details.tm_positions_count. Industry has no field in
    the app and is NOT imported.
  * created_by is required: pass --user <username-or-email>; defaults to the
    first user in registration_data.
  * --jd-dir: folder (searched recursively) with the downloaded JD files.
    Matches are copied into app storage and attached as the customer JD.
  * --replace: existing opportunities NOT in the CSV are deleted (same cascade
    as the app's delete button: requirements, candidate profiles, AI links,
    slots, attachments). Opportunities that already have projects are NEVER
    deleted - they are kept and reported.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import hashlib
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import func, select, text

from crm_db import get_session_factory
from models import (
    AiInterviewLink, CandidateProfile, Customer, CustomerBranch, InterviewEvent,
    InterviewSlot, Opportunity, OpportunityAttachment, OppType, PipelineStage,
    Project, Requirement, Resume, SlotBooking,
)
from services.crm_common import CRM_UPLOAD_DIR
from services.crm_delete import cascade_opportunity_children

TYPE_MAP = {
    "time & material": OppType.T_AND_M,
    "time and material": OppType.T_AND_M,
    "t&m": OppType.T_AND_M,
    "work package": OppType.WORK_PACKAGE,
    "fixed price": OppType.FIXED_PRICE,
    "retainer": OppType.RETAINER,
}
STATUS_MAP = {
    "open": PipelineStage.ACTIVE,
    "customer hold": PipelineStage.ON_HOLD,
    "hold": PipelineStage.ON_HOLD,
    "closed won": PipelineStage.CLOSED_WON,
    "closed lost": PipelineStage.CLOSED_LOST,
}


def norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


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

    def take_flag(flag: str) -> str | None:
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                val = argv[i + 1]
                del argv[i:i + 2]
                return val
            print(f"{flag} needs a value")
            raise SystemExit(2)
        return None

    jd_dir_arg = take_flag("--jd-dir")
    user_arg = take_flag("--user")
    jd_dir = Path(jd_dir_arg) if jd_dir_arg else None
    if jd_dir and not jd_dir.is_dir():
        print(f"JD folder not found: {jd_dir}")
        return 2
    paths = [a for a in argv if not a.startswith("--")]
    csv_path = Path(paths[0]) if paths else (
        Path(__file__).resolve().parent.parent.parent / "import_templates" / "sales_opportunities.csv")
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if squash(r.get("Opportunity ID"))]
    print(f"CSV opportunity rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    jd_files = index_dir(jd_dir) if jd_dir else {}
    if jd_dir:
        print(f"JD folder: {jd_dir}  ({len(jd_files)} files found)")

    db = get_session_factory()()
    created = updated = skipped = jd_linked = jd_missing = deleted = kept = 0
    try:
        # created_by user
        if user_arg:
            row = db.execute(text(
                "SELECT id, username FROM registration_data "
                "WHERE lower(username) = :u OR lower(email) = :u ORDER BY id LIMIT 1"
            ), {"u": user_arg.strip().lower()}).first()
        else:
            row = db.execute(text(
                "SELECT id, username FROM registration_data ORDER BY id LIMIT 1")).first()
        if row is None:
            print("No user found in registration_data - pass --user <username-or-email>.")
            return 2
        user_id, username = row[0], row[1]
        print(f"created_by user: {username} (id {user_id})")

        cust_active: dict[int, bool] = {}
        for c in db.execute(select(Customer)).scalars().all():
            status = c.status.value if hasattr(c.status, "value") else str(c.status or "")
            cust_active[c.id] = status == "Active"
        by_branch: dict[str, list[CustomerBranch]] = {}
        for b in db.execute(select(CustomerBranch)).scalars().all():
            by_branch.setdefault(norm(b.branch_name), []).append(b)
        existing = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        existing_att: dict[int, set[str]] = {}
        for a in db.execute(select(OpportunityAttachment)).scalars().all():
            existing_att.setdefault(a.opportunity_id, set()).add(a.file_sha256 or a.file_name or "")

        if replace:
            csv_ids = {squash(r["Opportunity ID"])[:32] for r in rows}
            for key, opp in list(existing.items()):
                if key in csv_ids:
                    continue
                proj_n = db.execute(
                    select(func.count()).select_from(Project)
                    .where(Project.opportunity_id == opp.id)
                ).scalar() or 0
                if proj_n:
                    kept += 1
                    print(f"  ! kept {opp.opp_id} ({opp.title}): {proj_n} project(s) exist - not deleted")
                    continue
                # Children the app's cascade helper does not remove itself
                # (resumes block the requirement delete; interview events block
                # the profile delete). Delete them first, child tables before
                # their parents.
                req_sel = select(Requirement.id).where(Requirement.opportunity_id == opp.id)
                prof_sel = select(CandidateProfile.id).where(CandidateProfile.opportunity_id == opp.id)
                db.execute(AiInterviewLink.__table__.delete()
                           .where(AiInterviewLink.opportunity_id == opp.id))
                db.execute(SlotBooking.__table__.delete()
                           .where(SlotBooking.requirement_id.in_(req_sel)))
                db.execute(InterviewSlot.__table__.delete()
                           .where(InterviewSlot.requirement_id.in_(req_sel)))
                db.execute(Resume.__table__.delete()
                           .where(Resume.requirement_id.in_(req_sel)))
                db.execute(InterviewEvent.__table__.delete()
                           .where(InterviewEvent.profile_id.in_(prof_sel)))
                cascade_opportunity_children(db, opp.id)
                # Flush children BEFORE deleting the opportunity row: Requirement
                # has no ORM relationship to Opportunity, so in a single flush
                # SQLAlchemy may emit the opportunity DELETE first (FK violation).
                db.flush()
                db.delete(opp)
                db.flush()
                existing_att.pop(opp.id, None)
                del existing[key]
                deleted += 1
                print(f"  - deleted {opp.opp_id}: {opp.title}")

        att_target = CRM_UPLOAD_DIR / "opportunity_attachments"
        for r in rows:
            opp_id = squash(r["Opportunity ID"])[:32]
            title = squash(r.get("Opportunity Title"))[:255]
            bname = squash(r.get("Branch"))
            matches = by_branch.get(norm(bname), [])
            if len(matches) > 1:
                # Same branch name under several customers (e.g. an old Inactive
                # duplicate of the customer) - prefer the Active customer's branch.
                active = [b for b in matches if cust_active.get(b.customer_id)]
                if len(active) >= 1:
                    matches = active[:1] if len(active) == 1 else active
            if len(matches) != 1:
                why = "unknown" if not matches else "ambiguous"
                print(f"  ! skipped {opp_id}: {why} branch {bname!r}")
                skipped += 1
                continue
            branch = matches[0]
            opp_type = TYPE_MAP.get(norm(r.get("Opportunity Type")))
            if opp_type is None:
                print(f"  ! skipped {opp_id}: unknown type {r.get('Opportunity Type')!r}")
                skipped += 1
                continue
            stage = STATUS_MAP.get(norm(r.get("Oppurtunity Status")), PipelineStage.ACTIVE)
            details: dict = {"sales_stage": squash(r.get("Stage")) or None}
            if opp_type is OppType.T_AND_M:
                pos = squash(r.get("Positions (Count)"))
                if pos.isdigit():
                    details["tm_positions_count"] = int(pos)
                details["tm_position_title"] = title or None
            details = {k: v for k, v in details.items() if v is not None}

            opp = existing.get(opp_id)
            is_new = opp is None
            if is_new:
                opp = Opportunity(
                    opp_id=opp_id, title=title or opp_id,
                    customer_id=branch.customer_id, branch_id=branch.id,
                    opp_type=opp_type, pipeline_stage=stage,
                    rfi_received_date=parse_date(r.get("Received Date")),
                    details=details, created_by=user_id,
                )
                db.add(opp)
                db.flush()
                existing[opp_id] = opp
                created += 1
                print(f"  + {opp_id}: {title} -> {bname} [{stage.value}]")
            else:
                opp.title = title or opp.title
                opp.customer_id = branch.customer_id
                opp.branch_id = branch.id
                opp.opp_type = opp_type
                opp.pipeline_stage = stage
                opp.rfi_received_date = parse_date(r.get("Received Date")) or opp.rfi_received_date
                merged = dict(opp.details or {})
                merged.update(details)
                opp.details = merged
                updated += 1
                print(f"  ~ {opp_id}: {title}")

            jd_name = squash(r.get("JD Attachment"))
            if jd_name and jd_dir:
                src = jd_files.get(jd_name.lower())
                if src is None:
                    jd_missing += 1
                    print(f"      JD file not found: {jd_name}")
                else:
                    data = src.read_bytes()
                    sha = hashlib.sha256(data).hexdigest()
                    if sha not in existing_att.get(opp.id, set()):
                        ext = Path(jd_name).suffix[:10]
                        stored = f"{sha[:32]}{ext}"
                        if apply:
                            att_target.mkdir(parents=True, exist_ok=True)
                            dest = att_target / stored
                            if not dest.exists():
                                shutil.copyfile(src, dest)
                        db.add(OpportunityAttachment(
                            opportunity_id=opp.id,
                            file_url=f"/api/crm-files/opportunity_attachments/{stored}",
                            file_name=jd_name[:255], file_sha256=sha,
                            file_size=len(data), kind="customer_jd",
                            uploaded_by=user_id,
                        ))
                        existing_att.setdefault(opp.id, set()).add(sha)
                    jd_linked += 1

        print(f"\nOPPORTUNITIES: {created} created, {updated} updated, {skipped} skipped"
              + (f", {deleted} deleted, {kept} kept (have projects)" if replace else ""))
        if jd_dir:
            print(f"JDs: {jd_linked} linked, {jd_missing} filenames not found in folder")

        if apply:
            db.commit()
            print("\nAPPLIED - opportunity import complete.")
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
