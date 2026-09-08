r"""Enrich the imported opportunities from the FULL NEXUS "TA Active Positions" export
and align every candidate profile with its position.

Input: `KARNEX_TA_Active_Positions_<date>.json` (7 Sep 2026, user request) —
`{"_meta": ..., "records": [...]}`, one record per position with the whole
Zoho "Opportunity" form (commercials, leave/holiday policy, contact + hiring
manager, CTC slab, skills, JD file name) and `linked_candidate_profiles`.

For every position (keyed by Opportunity_ID, e.g. C-2026-00057):
  Opportunity  created when missing (branch must exist), otherwise FILLED —
               title, RFI value / received date, onboarded count, onboarding
               status, pipeline stage (Open -> Active, Closed-Partial ->
               Closed_Partial), contact person + hiring manager (ContactPerson
               rows created under the customer when missing), and every
               T&M detail the form shows: position title / count / exp /
               notice period / closing date / position type / replacement
               engineer / duration / role / work location / WFO, billing type,
               hours per day, leave-holiday-weekoff figures and billable flags,
               credit-leave, leave policy, customer type, industry, sales stage,
               JD file name; billing bases recomputed by the app's own engine.
  CTC slab     Candidate_CTC_Slab rows -> opportunity_ctc_slab (replaced),
               derived through the app's derive_ctc_row so the chain matches
               what the form would compute.
  Skills       Skill_Evaluation_Details -> opportunity_skills + requirement_skills
               (Skill master rows created when new; Expert=5 Advanced=4
               Intermediate=3 Basic=2 Beginner=1).
  Requirement  positions, exp range, target closure, location, work mode,
               JD text when present.
  Profiles     every linked candidate profile (by zoho_profile_id) is checked
               to sit on THIS opportunity — moved when it points elsewhere,
               reported when it is missing (run
               import_zoho_candidate_profiles_sept2026.py to create it).

Usage (from backend/, same venv as the app):
    python tools\import_zoho_positions_sept2026.py                # DRY RUN
    python tools\import_zoho_positions_sept2026.py --apply
    python tools\import_zoho_positions_sept2026.py --file "D:\KARNEX_TA_Active_Positions_2026-09-07.json" --apply

DRY RUN by default; nothing is written without --apply.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, text  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import (  # noqa: E402
    CandidateProfile, ContactPerson, Customer, CustomerBranch, Location, OppType, Opportunity,
    OpportunityApprovalStatus, OpportunityCtcSlab, OpportunitySkill, PipelineStage, Priority,
    Requirement, RequirementSkill, RequirementStatus, Skill, WorkMode,
)
from services.crm_common import next_sequence_number  # noqa: E402
from services.opportunity_ctc import derive_ctc_row, normalize_tm_billing_details  # noqa: E402

STAGE_MAP = {
    "open": PipelineStage.ACTIVE,
    "closed-partial": PipelineStage.CLOSED_PARTIAL,
    "closed partial": PipelineStage.CLOSED_PARTIAL,
    "closed": PipelineStage.CLOSED_WON,
    "on hold": PipelineStage.ON_HOLD,
}
LEVEL_MAP = {"expert": 5, "advanced": 4, "intermediate": 3, "basic": 2, "beginner": 1}
WORK_MODE_MAP = {"onsite": WorkMode.ONSITE, "on site": WorkMode.ONSITE, "on-site": WorkMode.ONSITE,
                 "remote": WorkMode.REMOTE, "hybrid": WorkMode.HYBRID, "off-shore": WorkMode.OFFSHORE,
                 "offshore": WorkMode.OFFSHORE}
DATE_FMTS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d")


def norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def squash(s) -> str:
    return " ".join(str(s or "").split()).strip()


def lookup_value(v) -> str:
    if isinstance(v, dict):
        return squash(v.get("value"))
    if isinstance(v, list):
        return ", ".join(x for x in (lookup_value(i) for i in v) if x)
    return squash(v)


def lookup_id(v) -> str:
    return squash(v.get("id")) if isinstance(v, dict) else ""


def numf(v):
    try:
        s = str(v if v is not None else "").strip().replace(",", "")
        return float(s) if s else None
    except ValueError:
        return None


def parse_date(raw):
    s = squash(raw)
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def strip_html(s) -> str:
    import html
    t = re.sub(r"<br\s*/?>", "\n", str(s or ""), flags=re.I)
    t = re.sub(r"</(div|p)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return "\n".join(line.strip() for line in html.unescape(t).splitlines() if line.strip())


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv

    def take(flag):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return None

    file_arg = take("--file")
    user_arg = take("--user")
    if file_arg:
        path = Path(file_arg)
    else:
        found = sorted((Path(__file__).resolve().parent.parent.parent / "import_templates")
                       .glob("KARNEX_TA_Active_Positions_*.json"))
        if not found:
            print("Pass --file <export.json>")
            return 2
        path = found[-1]
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = doc.get("records") if isinstance(doc, dict) else doc
    print(f"File: {path.name} · positions: {len(records)} · exported {(doc.get('_meta') or {}).get('exported_at')}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}")

    db = get_session_factory()()
    try:
        if user_arg:
            row = db.execute(text("SELECT id, username FROM registration_data WHERE lower(username)=:u "
                                  "OR lower(email)=:u ORDER BY id LIMIT 1"), {"u": user_arg.lower()}).first()
        else:
            row = db.execute(text("SELECT id, username FROM registration_data ORDER BY id LIMIT 1")).first()
        if row is None:
            print("No user in registration_data — pass --user <username-or-email>")
            return 2
        user_id = row[0]

        # ------------------------------------------------------------ lookups
        branches: dict[str, list[CustomerBranch]] = {}
        for b in db.execute(select(CustomerBranch)).scalars().all():
            branches.setdefault(norm(b.branch_name), []).append(b)
        customers = {c.id: c for c in db.execute(select(Customer)).scalars().all()}
        cust_by_name = {norm(c.name): c for c in customers.values()}
        contacts: dict[tuple[int, str], ContactPerson] = {}
        for cp in db.execute(select(ContactPerson)).scalars().all():
            contacts.setdefault((cp.customer_id, norm(cp.name)), cp)
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        locations_by_city = {norm(loc.city): loc for loc in db.execute(select(Location)).scalars().all()}
        opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        opps_by_zoho = {}
        for o in opps.values():
            z = (o.details or {}).get("zoho_position_id") or (o.details or {}).get("zoho_opportunity_id")
            if z:
                opps_by_zoho.setdefault(str(z), o)

        def resolve_branch(name: str):
            hits = branches.get(norm(name), [])
            if len(hits) == 1:
                return hits[0]
            active = [b for b in hits if getattr(customers.get(b.customer_id), "status", None) and
                      getattr(customers[b.customer_id].status, "value", customers[b.customer_id].status) == "Active"]
            if len(active) == 1:
                return active[0]
            head = norm(name).split(" - ")[0]
            c = cust_by_name.get(head)
            if c is not None:
                cb = [b for b in hits if b.customer_id == c.id] or \
                     [b for bl in branches.values() for b in bl if b.customer_id == c.id and b.is_primary]
                if cb:
                    return cb[0]
            return None

        def get_skill(name: str):
            key = norm(name)
            if not key:
                return None
            s = skills_by_name.get(key)
            if s is None:
                s = Skill(name=squash(name)[:120])
                db.add(s)
                db.flush()
                skills_by_name[key] = s
            return s

        def get_contact(customer_id: int, branch_id, name: str, email: str, phone: str, hiring: bool):
            if not name:
                return None
            cp = contacts.get((customer_id, norm(name)))
            if cp is None:
                cp = ContactPerson(customer_id=customer_id, branch_id=branch_id, name=name[:255],
                                   email=email[:255] or None, phone=phone[:32] or None,
                                   is_hiring_manager=hiring)
                db.add(cp)
                db.flush()
                contacts[(customer_id, norm(name))] = cp
            else:
                if email and not cp.email:
                    cp.email = email[:255]
                if phone and not cp.phone:
                    cp.phone = phone[:32]
                if hiring and not cp.is_hiring_manager:
                    cp.is_hiring_manager = True
            return cp

        created = updated = skipped = slabs = skills_n = 0
        moved = missing_prof = ok_prof = 0
        for rec in records:
            o = rec.get("opportunity") or {}
            summ = rec.get("summary") or {}
            oid = squash(summ.get("opportunity_id") or o.get("Opportunity_ID"))
            zid = squash(rec.get("zoho_opportunity_record_id"))
            title = squash(o.get("Position_Title") or o.get("Opportunity_Title") or summ.get("position_title")) or oid
            branch_name = lookup_value(o.get("Branch")) or summ.get("branch")
            branch = resolve_branch(branch_name)
            opp = opps.get(oid) or opps_by_zoho.get(zid)
            if opp is None and not re.match(r"^C-\d{4}-\d+$", oid):
                print(f"  ! skipped bad Opportunity_ID {oid!r} ({title}) — fix the id in NEXUS or create it by hand")
                skipped += 1
                continue
            if branch is None and opp is None:
                print(f"  ! skipped {oid} ({title}): branch {branch_name!r} not found — create the customer/branch first")
                skipped += 1
                continue
            customer = customers[branch.customer_id] if branch else customers[opp.customer_id]
            if branch is None:
                branch = db.get(CustomerBranch, opp.branch_id) if opp.branch_id else None

            contact = get_contact(customer.id, branch.id if branch else None, lookup_value(o.get("Contact_Persons")),
                                  squash(o.get("Contact_Email")), squash(o.get("Contact_Phone")), False)
            hm = get_contact(customer.id, branch.id if branch else None, lookup_value(o.get("Hiring_Manager")),
                             squash(o.get("HiringManager_Email")), squash(o.get("HiringManager_Contact")), True)

            exp_min, exp_max = numf(o.get("Exp_Min")), numf(o.get("Exp_Max"))
            positions_n = int(numf(o.get("Positions_Count")) or 1)
            received = parse_date(o.get("Received_Date"))
            closing = parse_date(o.get("Closing_Date"))
            work_loc = lookup_value(o.get("Work_Location")) or (branch.city if branch else "")
            jd_name = squash(rec.get("jd_original_file_name") or rec.get("customer_jd_original_file_name")
                             or o.get("JD_Attachment") or o.get("Customer_JD_Attachment"))
            billing_type = "Per Hour" if norm(o.get("Billing_Type")).startswith("per hour") else \
                           ("Per Month" if norm(o.get("Billing_Type")).startswith("per month") else squash(o.get("Billing_Type")))
            details = {
                "zoho_position_id": zid or None,
                "imported_from": "zoho-active-2026-09",
                "in_ta_active": True,
                "tm_position_title": title,
                "tm_positions_count": positions_n,
                "tm_exp_min": exp_min, "tm_exp_max": exp_max,
                "tm_notice_period": squash(o.get("Notice_Period")) or None,
                "tm_closing_date": closing.isoformat() if closing else None,
                "tm_position_type": squash(o.get("Position_Type")) or None,
                "tm_replacement_engineer": lookup_value(o.get("Replacement_Engineer")) or None,
                "tm_duration_months": numf(o.get("Duration_In_Months")),
                "tm_role": lookup_value(o.get("Role")) or None,
                "tm_work_location": work_loc or None,
                "tm_wfo_remote": squash(o.get("WFO_Remote")) or None,
                "holidays_billable": bool(o.get("Holidays_Billable")),
                "weekoff_billable": bool(o.get("Weekoff_Billable")),
                "leave_billable": bool(o.get("Leave_Billable")),
                "credit_leave_monthly": numf(o.get("Credit_Leave_Monthly")),
                "leave_policy": squash(o.get("Leave_Policy")) or None,
                "holidays": numf(o.get("Holidays")), "weekoff": numf(o.get("Weekoff")), "leave": numf(o.get("Leave")),
                "billing_type": billing_type or None,
                "hours_per_day": numf(o.get("Hours_Per_Day")) or 8,
                "customer_type": squash(o.get("Customer_Type")) or None,
                "contact_email": squash(o.get("Contact_Email")) or None,
                "contact_phone": squash(o.get("Contact_Phone")) or None,
                "hiring_manager_email": squash(o.get("HiringManager_Email")) or None,
                "hiring_manager_contact": squash(o.get("HiringManager_Contact")) or None,
                "sales_stage": squash(o.get("Sales_Stage")) or None,
                "industry": squash(o.get("Industry")) or None,
                "ta_status": squash(o.get("TA_Status")) or None,
                "internal_external": squash(o.get("Select_Internal_External")) or None,
                "publish_public": squash(o.get("Publish_Public")) or None,
                "customer_jd_attachment_name": jd_name or None,
                "zoho_rmg": lookup_value(o.get("RMG")) or None,
                "onboarded_count": int(numf(o.get("Onboarded_Count")) or 0),
            }
            details = {k: v for k, v in details.items() if v is not None or k.endswith("_billable")}
            details = normalize_tm_billing_details(details)
            stage = STAGE_MAP.get(norm(o.get("Oppurtunity_Status")), PipelineStage.ACTIVE)
            rfi = numf(o.get("RFI_Value"))

            if opp is None:
                opp = Opportunity(
                    opp_id=oid, title=title[:255], customer_id=customer.id, branch_id=branch.id,
                    contact_person_id=contact.id if contact else None,
                    hiring_manager_id=hm.id if hm else None,
                    opp_type=OppType.T_AND_M, pipeline_stage=stage,
                    approval_status=OpportunityApprovalStatus.APPROVED,
                    rfi_value=Decimal(str(rfi)) if rfi else None, rfi_received_date=received,
                    onboarding_status=squash(o.get("Onboarding_Status")) or None,
                    onboarded_count=details.get("onboarded_count", 0),
                    details=details, created_by=user_id,
                )
                db.add(opp)
                db.flush()
                opps[oid] = opp
                created += 1
            else:
                opp.title = title[:255]
                if branch and not opp.branch_id:
                    opp.branch_id = branch.id
                if contact and not opp.contact_person_id:
                    opp.contact_person_id = contact.id
                if hm and not opp.hiring_manager_id:
                    opp.hiring_manager_id = hm.id
                if rfi and not opp.rfi_value:
                    opp.rfi_value = Decimal(str(rfi))
                if received and not opp.rfi_received_date:
                    opp.rfi_received_date = received
                if not opp.onboarding_status:
                    opp.onboarding_status = squash(o.get("Onboarding_Status")) or None
                opp.onboarded_count = max(opp.onboarded_count or 0, details.get("onboarded_count", 0))
                if opp.pipeline_stage in (PipelineStage.NEW, PipelineStage.ACTIVE):
                    opp.pipeline_stage = stage
                # existing keys win only where the export is blank
                merged = dict(opp.details or {})
                for k, v in details.items():
                    if v is not None and v != "" or k not in merged:
                        merged[k] = v
                opp.details = merged
                updated += 1

            # -------- CTC slab (replace)
            slab_rows = o.get("Candidate_CTC_Slab") or []
            if slab_rows:
                db.query(OpportunityCtcSlab).filter(OpportunityCtcSlab.opportunity_id == opp.id).delete()
                for idx, s in enumerate(slab_rows):
                    raw = {
                        "exp_min": numf(s.get("Exp_Min_Year")), "exp_max": numf(s.get("Exp_Max_Year")),
                        "target_exp": numf(s.get("Target_Exp")), "rate": numf(s.get("Rate")),
                        "revenue_monthly": numf(s.get("Revenue_Monthly")), "revenue_annual": numf(s.get("Revenue_Annual")),
                        "management_cost_pct": numf(s.get("Managment_Cost")),
                        "engineering_budget": numf(s.get("Engineering_Budget")), "hike_pct": numf(s.get("Hike")),
                        "appraisal_cycle": squash(s.get("Appraisal_Cycle")) or None,
                        "approved_ctc_lac": (lambda v: v / 100000 if v and v > 1000 else v)(numf(s.get("Approved_CTC"))),
                    }
                    data = derive_ctc_row(raw, opportunity_type="T&M", details=opp.details or {})
                    db.add(OpportunityCtcSlab(opportunity_id=opp.id, position=idx, **data))
                    slabs += 1

            # -------- requirement
            req = db.execute(select(Requirement).where(Requirement.opportunity_id == opp.id)).scalars().first()
            loc = locations_by_city.get(norm(work_loc.split(",")[0])) if work_loc else None
            wm = WORK_MODE_MAP.get(norm(o.get("WFO_Remote")))
            jd_text = strip_html(o.get("Job_Description"))
            if req is None:
                req = Requirement(
                    req_number=next_sequence_number(db, Requirement, Requirement.req_number, "REQ"),
                    opportunity_id=opp.id, customer_id=customer.id, title=title[:255],
                    description=f"Imported from NEXUS position {zid or oid}",
                    no_of_positions=positions_n, experience_min=exp_min, experience_max=exp_max,
                    target_closure_date=closing, location_id=loc.id if loc else None, work_mode=wm,
                    rmg_jd_text=jd_text or None,
                    priority=Priority.MEDIUM, status=RequirementStatus.OPEN_FOR_SOURCING, created_by=user_id,
                )
                db.add(req)
                db.flush()
            else:
                req.title = title[:255]
                req.no_of_positions = positions_n
                req.experience_min, req.experience_max = exp_min, exp_max
                if closing and not req.target_closure_date:
                    req.target_closure_date = closing
                if loc and not req.location_id:
                    req.location_id = loc.id
                if wm and not req.work_mode:
                    req.work_mode = wm
                if jd_text and not req.rmg_jd_text:
                    req.rmg_jd_text = jd_text

            # -------- skills (opportunity + requirement)
            have_o = {x.skill_id: x for x in db.execute(
                select(OpportunitySkill).where(OpportunitySkill.opportunity_id == opp.id)).scalars().all()}
            have_r = {x.skill_id: x for x in db.execute(
                select(RequirementSkill).where(RequirementSkill.requirement_id == req.id)).scalars().all()}
            for se in o.get("Skill_Evaluation_Details") or []:
                s = get_skill(lookup_value(se.get("Skill_Name")))
                if not s:
                    continue
                lvl = LEVEL_MAP.get(norm(se.get("Required_Level")))
                mand = bool(se.get("Is_Mandatory"))
                if s.id not in have_o:
                    db.add(OpportunitySkill(opportunity_id=opp.id, skill_id=s.id, is_mandatory=mand,
                                            required_level=lvl, comment=squash(se.get("Comment")) or None))
                    have_o[s.id] = True
                    skills_n += 1
                if s.id not in have_r:
                    db.add(RequirementSkill(requirement_id=req.id, skill_id=s.id, is_mandatory=mand, min_rating=lvl))
                    have_r[s.id] = True

            # -------- candidate profile alignment
            for lp in rec.get("linked_candidate_profiles") or []:
                zp = squash(lp.get("zoho_profile_record_id"))
                if not zp:
                    continue
                prof = db.execute(select(CandidateProfile).where(CandidateProfile.zoho_profile_id == zp)).scalars().first()
                if prof is None:
                    missing_prof += 1
                    print(f"  ! {oid}: candidate profile {lp.get('candidate_name')} ({zp}) not in the tool — "
                          f"run import_zoho_candidate_profiles_sept2026.py")
                elif prof.opportunity_id != opp.id:
                    clash = db.execute(select(CandidateProfile.id).where(
                        CandidateProfile.candidate_id == prof.candidate_id,
                        CandidateProfile.opportunity_id == opp.id)).first()
                    if clash:
                        print(f"  ! {oid}: {lp.get('candidate_name')} already has a profile here; "
                              f"left profile {prof.id} on its current opportunity")
                    else:
                        prof.opportunity_id = opp.id
                        moved += 1
                else:
                    ok_prof += 1
            db.flush()

        print(f"OPPORTUNITIES: {created} created, {updated} enriched, {skipped} skipped · "
              f"CTC slab rows {slabs} · skills added {skills_n}")
        print(f"PROFILES: {ok_prof} already aligned, {moved} moved to the right opportunity, {missing_prof} missing")
        if apply:
            db.commit()
            print("\nAPPLIED.")
        else:
            db.rollback()
            print("\nDRY RUN — nothing written. Re-run with --apply to write.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
