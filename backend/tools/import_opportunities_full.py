r"""Import the FULL sales opportunities export (all Zoho fields) into the CRM.

Source CSV: import_templates\sales_opportunities_full.csv (79-column export:
core fields + contact/hiring manager + T&M details + leave & billing details +
Candidate CTC Slab / Skill Evaluation / Candidates subforms as JSON).

Usage (from the backend folder, same venv as the app):

    python tools\import_opportunities_full.py                      # DRY RUN
    python tools\import_opportunities_full.py --replace            # + delete opps not in CSV (dry run)
    python tools\import_opportunities_full.py --replace --apply    # write everything
    python tools\import_opportunities_full.py --jd-dir "D:\JDs" --user admin --replace --apply

Behaviour:
  * Upsert by Opportunity_ID. --replace deletes DB opportunities missing from
    the CSV (safe cascade; opportunities with projects are kept + reported).
  * Branch name resolves branch AND customer; duplicate branch names prefer the
    Active customer's branch.
  * Contact Person / Hiring Manager are linked to the customer's imported
    contacts by name (fallback: email).
  * Every form field is filled: customer type, position title/type/count,
    exp min/max, notice period, closing date, duration, role, work location,
    WFO/Remote, leave & holiday details, billing type/hours/days, RFI value,
    onboarding status + count, sales stage. Detail keys are filtered through
    the server's own form schema so nothing invalid is stored.
  * Candidate CTC Slab subform -> opportunity_ctc_slab rows (replaced per opp).
  * Skill Evaluation subform -> opportunity_skills rows (skill upserted by name;
    Beginner/Intermediate/Expert -> level 1/3/5).
  * Candidates subform: linked by candidate name when present. (In this export
    only 1 link exists and its name is blank - reported, nothing to connect.)
  * JD_Attachment + --jd-dir: files copied into app storage as customer JDs.
  * DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import func, select, text

from crm_db import get_session_factory
from models import (
    AiInterviewLink, Candidate, CandidateProfile, ContactPerson, Customer,
    CustomerBranch, InterviewEvent, InterviewSlot, Opportunity,
    OpportunityAttachment, OpportunityCtcSlab, OpportunitySkill, OppType,
    PipelineStage, PipelineStatus, Project, Requirement, Resume, Skill,
    SlotBooking,
)
from services.crm_common import CRM_UPLOAD_DIR
from services.crm_delete import cascade_opportunity_children
from services.opportunity_form_schema import allowed_detail_keys

TYPE_MAP = {
    "time & material": OppType.T_AND_M,
    "work package": OppType.WORK_PACKAGE,
    "fixed price": OppType.FIXED_PRICE,
    "retainer": OppType.RETAINER,
}
STATUS_MAP = {
    "open": PipelineStage.ACTIVE,
    "customer hold": PipelineStage.ON_HOLD,
    "customer differed": PipelineStage.ON_HOLD,
    "closed by customer": PipelineStage.CLOSED_LOST,
    "customer rejected": PipelineStage.REJECTED,
    "rejected by karnex": PipelineStage.REJECTED,
    "closed-fully": PipelineStage.CLOSED_WON,
    "closed fully": PipelineStage.CLOSED_WON,
    "closed-partial": PipelineStage.CLOSED_PARTIAL,
}
LEVEL_MAP = {"beginner": 1, "basic": 1, "intermediate": 3, "advanced": 4, "expert": 5}


def norm(s: str | None) -> str:
    return " ".join((s or "").split()).lower()


def squash(s: str | None) -> str:
    return " ".join((s or "").split())


def numf(v):
    v = squash(v)
    if not v:
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else f
    except ValueError:
        return None


def boolf(v):
    s = norm(v)
    if s in ("true", "yes", "1"):
        return True
    if s in ("false", "no", "0"):
        return False
    return None


def parse_date(raw: str):
    raw = squash(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def fv(cell):
    """Zoho subform FIELDVALUE -> plain text."""
    if isinstance(cell, dict):
        v = cell.get("FIELDVALUE", "")
        if isinstance(v, dict):
            return squash(str(v.get("text", "")))
        return squash(str(v))
    return squash(str(cell or ""))


def subform(row: dict, key: str) -> list[dict]:
    raw = (row.get(key) or "").strip()
    if not raw:
        return []
    try:
        return json.loads(raw).get("SUBFORM_RECORDS") or []
    except Exception:
        return []


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
        Path(__file__).resolve().parent.parent.parent / "import_templates" / "sales_opportunities_full.csv")
    if not csv_path.exists():
        print(f"CSV not found: {csv_path}")
        return 2

    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if squash(r.get("Opportunity_ID"))]
    print(f"CSV opportunity rows: {len(rows)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    jd_files = index_dir(jd_dir) if jd_dir else {}
    if jd_dir:
        print(f"JD folder: {jd_dir}  ({len(jd_files)} files found)")

    db = get_session_factory()()
    created = updated = skipped = deleted = kept = 0
    jd_linked = jd_missing = slab_rows = skill_links = cand_links = cand_unmatched = 0
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
        contacts_by_cust: dict[int, list[ContactPerson]] = {}
        for cp in db.execute(select(ContactPerson)).scalars().all():
            contacts_by_cust.setdefault(cp.customer_id, []).append(cp)
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        cands_by_name: dict[str, list[Candidate]] = {}
        for cd in db.execute(select(Candidate)).scalars().all():
            full = " ".join(x for x in (cd.first_name, cd.middle_name, cd.last_name) if x)
            cands_by_name.setdefault(norm(full), []).append(cd)
        existing = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        existing_att: dict[int, set[str]] = {}
        for a in db.execute(select(OpportunityAttachment)).scalars().all():
            existing_att.setdefault(a.opportunity_id, set()).add(a.file_sha256 or a.file_name or "")

        def find_contact(cust_id: int, name: str, email: str):
            pool = contacts_by_cust.get(cust_id, [])
            n, e = norm(name), norm(email)
            if n:
                hits = [c for c in pool if norm(c.name) == n]
                if len(hits) == 1:
                    return hits[0]
                if hits:
                    return hits[0]
            if e:
                hits = [c for c in pool if norm(c.email) == e]
                if hits:
                    return hits[0]
            return None

        # ---- replace: delete opportunities not in the CSV --------------------
        if replace:
            csv_ids = {squash(r["Opportunity_ID"])[:32] for r in rows}
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
                db.flush()
                db.delete(opp)
                db.flush()
                existing_att.pop(opp.id, None)
                del existing[key]
                deleted += 1
                print(f"  - deleted {opp.opp_id}: {opp.title}")

        att_target = CRM_UPLOAD_DIR / "opportunity_attachments"
        for r in rows:
            opp_id = squash(r["Opportunity_ID"])[:32]
            title = squash(r.get("Opportunity_Title"))[:255]
            bname = squash(r.get("Branch"))
            matches = by_branch.get(norm(bname), [])
            if len(matches) > 1:
                active = [b for b in matches if cust_active.get(b.customer_id)]
                if active:
                    matches = active[:1]
            if len(matches) != 1:
                why = "unknown" if not matches else "ambiguous"
                print(f"  ! skipped {opp_id}: {why} branch {bname!r}")
                skipped += 1
                continue
            branch = matches[0]
            opp_type = TYPE_MAP.get(norm(r.get("Opportunity_Type")))
            if opp_type is None:
                print(f"  ! skipped {opp_id}: unknown type {r.get('Opportunity_Type')!r}")
                skipped += 1
                continue

            status_raw = norm(r.get("Oppurtunity_Status"))
            stage_raw = norm(r.get("Sales_Stage"))
            stage = STATUS_MAP.get(status_raw)
            if stage is None:
                if "rejected" in stage_raw:
                    stage = PipelineStage.REJECTED
                elif "closed fully" in stage_raw:
                    stage = PipelineStage.CLOSED_WON
                else:
                    stage = PipelineStage.ACTIVE

            cp = find_contact(branch.customer_id, r.get("Contact_Persons"), r.get("Contact_Email"))
            hm = find_contact(branch.customer_id, r.get("Hiring_Manager"), r.get("HiringManager_Email"))

            closing = parse_date(r.get("Closing_Date"))
            details_all = {
                "customer_type": squash(r.get("Customer_Type")) or None,
                "contact_email": squash(r.get("Contact_Email")) or None,
                "contact_phone": squash(r.get("Contact_Phone")) or None,
                "hiring_manager_email": squash(r.get("HiringManager_Email")) or None,
                "hiring_manager_contact": squash(r.get("HiringManager_Contact")) or None,
                "sales_stage": squash(r.get("Sales_Stage")) or None,
                # T&M details
                "tm_position_title": squash(r.get("Position_Title")) or title or None,
                "tm_positions_count": numf(r.get("Positions_Count")),
                "tm_exp_min": numf(r.get("Exp_Min")),
                "tm_exp_max": numf(r.get("Exp_Max")),
                "tm_notice_period": squash(r.get("Notice_Period")) or None,
                "tm_closing_date": closing.isoformat() if closing else None,
                "tm_position_type": squash(r.get("Position_Type")) or None,
                "tm_replacement_engineer": squash(r.get("Replacement_Engineer")) or None,
                "tm_duration_months": numf(r.get("Duration_In_Months")),
                "tm_role": squash(r.get("Role")) or None,
                "tm_work_location": squash(r.get("Work_Location")) or None,
                "tm_wfo_remote": squash(r.get("WFO_Remote")) or None,
                # Leave & holiday
                "holidays_billable": boolf(r.get("Holidays_Billable")),
                "weekoff_billable": boolf(r.get("Weekoff_Billable")),
                "leave_billable": boolf(r.get("Leave_Billable")),
                "credit_leave_monthly": numf(r.get("Credit_Leave_Monthly")),
                "leave_policy": squash(r.get("Leave_Policy")) or None,
                "holidays": numf(r.get("Holidays")),
                "weekoff": numf(r.get("Weekoff")),
                "leave": numf(r.get("Leave")),
                # Commercial
                "billing_type": squash(r.get("Billing_Type")) or None,
                "hours_per_day": numf(r.get("Hours_Per_Day")),
                "actual_billing_days": numf(r.get("Actual_Billing_Days")),
                "actual_billing_hours": numf(r.get("Actual_Billing_Hours")),
                # Work Package / project scope
                "project_scope": squash(r.get("Project_Scope")) or None,
            }
            allowed = allowed_detail_keys(opp_type.value)
            details = {k: v for k, v in details_all.items() if v is not None and k in allowed}

            core = {
                "title": title or opp_id,
                "customer_id": branch.customer_id,
                "branch_id": branch.id,
                "contact_person_id": cp.id if cp else None,
                "hiring_manager_id": hm.id if hm else None,
                "opp_type": opp_type,
                "pipeline_stage": stage,
                "rfi_value": numf(r.get("RFI_Value")),
                "rfi_received_date": parse_date(r.get("Received_Date")),
                "onboarding_status": squash(r.get("Onboarding_Status")) or None,
                "onboarded_count": int(numf(r.get("Onboarded_Count")) or 0),
            }

            opp = existing.get(opp_id)
            if opp is None:
                opp = Opportunity(opp_id=opp_id, details=details, created_by=user_id, **core)
                db.add(opp)
                db.flush()
                existing[opp_id] = opp
                created += 1
            else:
                for f, v in core.items():
                    setattr(opp, f, v)
                merged = dict(opp.details or {})
                merged.update(details)
                opp.details = merged
                updated += 1

            # ---- CTC slab: replace rows from the subform --------------------
            slab = subform(r, "Candidate_CTC_Slab")
            if slab:
                db.execute(OpportunityCtcSlab.__table__.delete()
                           .where(OpportunityCtcSlab.opportunity_id == opp.id))
                for i, s in enumerate(slab):
                    approved = numf(fv(s.get("Approved_CTC")))
                    if approved is not None and approved > 1000:
                        approved = round(approved / 100000, 2)  # rupees -> lac
                    db.add(OpportunityCtcSlab(
                        opportunity_id=opp.id, position=i,
                        exp_min=numf(fv(s.get("Exp_Min_Year"))),
                        exp_max=numf(fv(s.get("Exp_Max_Year"))),
                        target_exp=numf(fv(s.get("Target_Exp"))),
                        rate=numf(fv(s.get("Rate"))),
                        revenue_monthly=numf(fv(s.get("Revenue_Monthly"))),
                        revenue_annual=numf(fv(s.get("Revenue_Annual"))),
                        management_cost_pct=numf(fv(s.get("Managment_Cost"))),
                        engineering_budget=numf(fv(s.get("Engineering_Budget"))),
                        hike_pct=numf(fv(s.get("Hike"))),
                        appraisal_cycle=fv(s.get("Appraisal_Cycle"))[:60] or None,
                        approved_ctc_lac=approved,
                    ))
                    slab_rows += 1

            # ---- Skill evaluations -> opportunity_skills --------------------
            existing_skill_ids = {
                os_.skill_id for os_ in db.execute(
                    select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)
                ).scalars().all()
            }
            for s in subform(r, "Skill_Evaluation_Details"):
                sname = fv(s.get("Skill_Name"))
                if not sname:
                    continue
                sk = skills_by_name.get(norm(sname))
                if sk is None:
                    sk = Skill(name=sname[:120], is_active=True)
                    db.add(sk)
                    db.flush()
                    skills_by_name[norm(sname)] = sk
                if sk.id in existing_skill_ids:
                    continue
                db.add(OpportunitySkill(
                    opportunity_id=opp.id, skill_id=sk.id,
                    is_mandatory=boolf(fv(s.get("Is_Mandatory"))) or False,
                    required_level=LEVEL_MAP.get(norm(fv(s.get("Required_Level")))),
                    comment=fv(s.get("Comment")) or None,
                ))
                existing_skill_ids.add(sk.id)
                skill_links += 1

            # ---- Candidates subform -> candidate profiles -------------------
            for s in subform(r, "Candidates"):
                cname = fv(s.get("Candidate_Name"))
                if not cname:
                    cand_unmatched += 1
                    continue
                hits = cands_by_name.get(norm(cname), [])
                if len(hits) != 1:
                    cand_unmatched += 1
                    print(f"      candidate not matched for {opp_id}: {cname!r}")
                    continue
                cand = hits[0]
                dupe = db.execute(
                    select(CandidateProfile).where(
                        CandidateProfile.candidate_id == cand.id,
                        CandidateProfile.opportunity_id == opp.id,
                    )
                ).scalars().first()
                if dupe is None:
                    status_txt = fv(s.get("Candidate_Status")).replace(" ", "_")
                    try:
                        pstat = PipelineStatus(status_txt)
                    except ValueError:
                        pstat = PipelineStatus.SOURCING
                    db.add(CandidateProfile(
                        candidate_id=cand.id, opportunity_id=opp.id,
                        pipeline_status=pstat,
                    ))
                    cand_links += 1

            # ---- JD attachment ---------------------------------------------
            jd_name = squash(r.get("JD_Attachment"))
            if jd_name and jd_dir:
                src = jd_files.get(jd_name.lower())
                if src is None:
                    jd_missing += 1
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
        print(f"CTC slab rows written: {slab_rows}")
        print(f"Skill links: {skill_links}")
        print(f"Candidate links: {cand_links} created, {cand_unmatched} rows without a usable name")
        if jd_dir:
            print(f"JDs: {jd_linked} linked, {jd_missing} filenames not found in folder")

        if apply:
            db.commit()
            print("\nAPPLIED - full opportunity import complete.")
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
