r"""Replace the live pipeline with the September 2026 Zoho "Active" exports.

Three JSON exports (7 Sep 2026, user request):

  Sales Active Oppurtunity (T&M).json   17 opportunities Sales still owns
  TA Active Positions.json              31 positions TA is sourcing for (a
                                        superset of the 17 — same Opportunity_ID)
  TA Active Candidate Profiles.json     639 candidate <-> position rows with the
                                        Zoho status and the current interview

What it does, in order:
  1. --wipe   deletes EVERY existing opportunity (with its requirement, applicant
              profiles, resumes, AI links, slots, attachments) — except ones a
              Project is already built on, which are kept and reported.
  2. Creates one Opportunity + one Requirement per Opportunity_ID found in the
              two position files. Zoho ids are kept: details.zoho_opportunity_id
              (Sales export ID) and details.zoho_position_id (TA export ID).
              Opportunities are Approved / Active / T&M; requirements are
              Open_For_Sourcing so TA sees them at once.
  3. Creates one CandidateProfile per candidate row, mapped to its opportunity
              by Opportunity_ID, with the Zoho status translated to the app's
              pipeline status, the TA owner, expected CTC, and the current
              interview round recorded on the Interviews tab.
              Candidate match order: CV filename -> exact name -> new candidate
              (placeholder email; no email is in the export).

Usage (from backend/, same venv as the app):
    python tools\import_zoho_active_sept2026.py                 # DRY RUN
    python tools\import_zoho_active_sept2026.py --wipe          # dry run incl. the deletions
    python tools\import_zoho_active_sept2026.py --wipe --apply  # do it
    python tools\import_zoho_active_sept2026.py --dir "D:\zoho" --user admin --wipe --apply

DRY RUN by default; nothing is written without --apply. Take a backup first.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select, text  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateProfile, CandidateProfileActivityLog, ContactPerson, Customer,
    CustomerBranch, InterviewEvent, OppType, Opportunity, OpportunityApprovalStatus,
    PipelineStage, PipelineStatus, Priority, Project, Requirement, RequirementStatus,
)
from services.crm_common import next_sequence_number  # noqa: E402

FILES = {
    "sales": "Sales Active Oppurtunity (T&M).json",
    "positions": "TA Active Positions.json",
    "profiles": "TA Active Candidate Profiles.json",
}

#: Zoho candidate status -> app pipeline status.
STATUS_MAP = {
    "sourcing": PipelineStatus.SOURCING,
    # Nexus "Technical Interviewing" = internal L1/L2 in progress, TA-owned —
    # that is Technical_Screening here (RMG_Review is the stage AFTER the
    # rounds are done). Shown in the UI as "Technical Interviewing".
    "technical interviewing": PipelineStatus.TECHNICAL_SCREENING,
    "technical screening": PipelineStatus.TECHNICAL_SCREENING,
    "rmg review": PipelineStatus.RMG_REVIEW,
    "rmg rejected": PipelineStatus.RMG_REJECTED,
    "sales screening": PipelineStatus.SALES_SCREENING,
    "sales rejected": PipelineStatus.SALES_REJECTED,
    "customer screening": PipelineStatus.CUSTOMER_SCREENING,
    "customer interviewing": PipelineStatus.CUSTOMER_INTERVIEW,
    "customer rejected": PipelineStatus.CUSTOMER_REJECTED,
    "customer approval pending": PipelineStatus.CUSTOMER_APPROVAL,
    "shortlisted": PipelineStatus.SHORTLISTED,
    "preboarding": PipelineStatus.PREBOARDING,
    "joined": PipelineStatus.JOINED,
    "self withdrawn": PipelineStatus.SELF_WITHDRAWN,
}
ROUND_MAP = {"l1-interview": "L1_Interview", "l1 interview": "L1_Interview",
             "l2-interview": "L2_F2F", "l2 interview": "L2_F2F"}
ROUND_STATUS_MAP = {
    "pending": "Pending", "scheduled": "Scheduled", "completed": "Completed",
    "rescheduled requested by candidate": "Rescheduled Requested By Candidate",
    "rescheduled requested by panel": "Rescheduled Requested By Panel",
    "cancelled": "Cancelled", "no show": "No Show", "in-progress": "In-Progress",
}
PREFIXES = ("mr.", "mrs.", "ms.", "miss", "dr.", "mr", "ms", "mrs")
EMAIL_DOMAIN = "import.karnex.in"


def norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def squash(s) -> str:
    return " ".join(str(s or "").split()).strip()


def numf(v):
    try:
        s = str(v or "").strip().replace(",", "")
        return float(s) if s else None
    except ValueError:
        return None


def parse_date(raw):
    s = squash(raw)
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def strip_prefix(name: str) -> str:
    n = squash(name)
    low = n.lower()
    for p in PREFIXES:
        if low.startswith(p + " "):
            return n[len(p) + 1:].strip()
    return n


def split_name(full: str) -> tuple[str, str]:
    parts = strip_prefix(full).split()
    if not parts:
        return "Candidate", ""
    return parts[0], " ".join(parts[1:])


def placeholder_email(first: str, last: str, zoho_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", f"{first} {last}".lower()).strip(".") or "candidate"
    digest = hashlib.sha256((zoho_id or base).encode()).hexdigest()[:8]
    return f"{base}.{digest}@{EMAIL_DOMAIN}"


def load(path: Path) -> list[dict]:
    d = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(d, list):
        return d
    for v in d.values():
        if isinstance(v, list):
            return v
    return []


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    wipe = "--wipe" in argv

    def take(flag):
        if flag in argv:
            i = argv.index(flag)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return None

    src_dir = Path(take("--dir") or Path(__file__).resolve().parent.parent.parent / "import_templates")
    user_arg = take("--user")
    paths = {k: src_dir / v for k, v in FILES.items()}
    for k, p in paths.items():
        if not p.exists():
            print(f"Missing {k} file: {p}")
            return 2
    sales = load(paths["sales"])
    positions = load(paths["positions"])
    profiles = load(paths["profiles"])
    print(f"Sales opportunities: {len(sales)} · TA positions: {len(positions)} · candidate rows: {len(profiles)}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}{' + WIPE existing opportunities' if wipe else ''}")

    db = get_session_factory()()
    try:
        # ---------------------------------------------------------------- user
        if user_arg:
            row = db.execute(text("SELECT id, username FROM registration_data WHERE lower(username)=:u "
                                  "OR lower(email)=:u ORDER BY id LIMIT 1"), {"u": user_arg.lower()}).first()
        else:
            row = db.execute(text("SELECT id, username FROM registration_data ORDER BY id LIMIT 1")).first()
        if row is None:
            print("No user in registration_data — pass --user <username-or-email>")
            return 2
        user_id = row[0]
        print(f"created_by: {row[1]} (id {user_id})")
        users_by_name = {}
        for uid, full, uname in db.execute(text(
                "SELECT id, full_name, username FROM registration_data")).all():
            for key in (norm(full), norm(uname)):
                if key:
                    users_by_name.setdefault(key, uid)

        # ---------------------------------------------------------------- wipe
        deleted = kept = 0
        if wipe:
            doomed: list[int] = []
            for opp in db.execute(select(Opportunity)).scalars().all():
                n_proj = db.execute(select(func.count()).select_from(Project)
                                    .where(Project.opportunity_id == opp.id)).scalar() or 0
                if n_proj:
                    kept += 1
                    print(f"  ! kept {opp.opp_id} ({opp.title}): {n_proj} project(s) — not deleted")
                    continue
                doomed.append(opp.id)
            if doomed:
                # Explicit SQL in FK order (child tables first). The ORM helper
                # leaves deletes pending in mapper order and Postgres then sees
                # the opportunity DELETE before its requirements' — FK error.
                ids = tuple(doomed)
                reqs = "SELECT id FROM requirements WHERE opportunity_id IN :ids"
                profs = "SELECT id FROM candidate_profiles WHERE opportunity_id IN :ids"
                resumes = f"SELECT id FROM resumes WHERE requirement_id IN ({reqs})"
                stmts = [
                    f"DELETE FROM ai_interview_links WHERE opportunity_id IN :ids OR profile_id IN ({profs})",
                    f"DELETE FROM slot_bookings WHERE requirement_id IN ({reqs}) OR resume_id IN ({resumes})",
                    f"DELETE FROM interview_slots WHERE requirement_id IN ({reqs})",
                    f"UPDATE resumes SET possible_duplicate_of = NULL WHERE requirement_id IN ({reqs})",
                    f"DELETE FROM resumes WHERE requirement_id IN ({reqs})",
                    f"UPDATE template_requests SET opportunity_id = NULL WHERE opportunity_id IN :ids",
                    f"DELETE FROM template_requests WHERE requirement_id IN ({reqs})",
                    f"UPDATE employees SET candidate_profile_id = NULL WHERE candidate_profile_id IN ({profs})",
                    f"DELETE FROM interview_events WHERE profile_id IN ({profs})",
                    f"DELETE FROM offer_history WHERE profile_id IN ({profs})",
                    f"DELETE FROM skill_evaluations WHERE profile_id IN ({profs})",
                    f"DELETE FROM candidate_profile_activity_log WHERE profile_id IN ({profs})",
                    "DELETE FROM candidate_profiles WHERE opportunity_id IN :ids",
                    f"DELETE FROM requirement_attachments WHERE requirement_id IN ({reqs})",
                    f"DELETE FROM requirement_skills WHERE requirement_id IN ({reqs})",
                    f"DELETE FROM requirement_job_postings WHERE requirement_id IN ({reqs})",
                    f"DELETE FROM requirement_activity_log WHERE requirement_id IN ({reqs})",
                    "DELETE FROM requirements WHERE opportunity_id IN :ids",
                    "DELETE FROM opportunity_skills WHERE opportunity_id IN :ids",
                    "DELETE FROM opportunity_activity_log WHERE opportunity_id IN :ids",
                    "DELETE FROM opportunity_attachments WHERE opportunity_id IN :ids",
                    "DELETE FROM opportunity_ctc_slab WHERE opportunity_id IN :ids",
                    "DELETE FROM opportunities WHERE id IN :ids",
                ]
                from sqlalchemy import bindparam
                for sql in stmts:
                    db.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": list(ids)})
                db.expire_all()
                deleted = len(doomed)
            print(f"WIPE: {deleted} opportunities deleted, {kept} kept")

        # -------------------------------------------------------- lookups
        branches: dict[str, list[CustomerBranch]] = {}
        for b in db.execute(select(CustomerBranch)).scalars().all():
            branches.setdefault(norm(b.branch_name), []).append(b)
        customers = {c.id: c for c in db.execute(select(Customer)).scalars().all()}
        cust_by_name = {norm(c.name): c for c in customers.values()}
        contacts: dict[tuple[int, str], ContactPerson] = {}
        for cp in db.execute(select(ContactPerson)).scalars().all():
            contacts[(cp.customer_id, norm(cp.name))] = cp
        existing_opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}

        def resolve_branch(name: str):
            hits = branches.get(norm(name), [])
            if len(hits) == 1:
                return hits[0]
            active = [b for b in hits if getattr(customers.get(b.customer_id), "status", None) and
                      getattr(customers[b.customer_id].status, "value", customers[b.customer_id].status) == "Active"]
            if len(active) == 1:
                return active[0]
            # "HARMAN - Bangalore" -> customer "HARMAN" fallback (primary branch)
            head = norm(name).split(" - ")[0]
            c = cust_by_name.get(head)
            if c is not None:
                cb = [b for b in branches.get(norm(name), []) if b.customer_id == c.id] or \
                     [b for bl in branches.values() for b in bl if b.customer_id == c.id and b.is_primary]
                if cb:
                    return cb[0]
            return None

        # ---------------------------------------------- opportunities + requirements
        merged: dict[str, dict] = {}
        for r in positions:
            oid = squash(r.get("Opportunity_ID"))
            if not re.match(r"^C-\d{4}-\d+$", oid):
                print(f"  ! skipped position with bad Opportunity_ID {oid!r} ({r.get('Position_Title')})")
                continue
            merged[oid] = {"position": r}
        for r in sales:
            oid = squash(r.get("Opportunity_ID"))
            merged.setdefault(oid, {})["sales"] = r

        opp_by_zoho_id: dict[str, Opportunity] = {}
        created_opps = created_reqs = skipped = 0
        for oid, parts in sorted(merged.items()):
            s, p = parts.get("sales") or {}, parts.get("position") or {}
            branch_name = s.get("Branch") or s.get("Branch.Branch_Name") or p.get("Branch.Branch_Name")
            branch = resolve_branch(branch_name)
            if branch is None:
                print(f"  ! skipped {oid}: branch {branch_name!r} not found (create the customer/branch first)")
                skipped += 1
                continue
            customer = customers[branch.customer_id]
            title = squash(s.get("Opportunity_Title") or p.get("Position_Title")) or oid
            exp_min = numf(s.get("Exp_Min") or p.get("Exp_Min"))
            exp_max = numf(s.get("Exp_Max") or p.get("Exp_Max"))
            positions_n = int(numf(s.get("Positions_Count") or p.get("Positions_Count")) or 1)
            received = parse_date(s.get("Received_Date") or p.get("Received_Date"))
            contact = contacts.get((customer.id, norm(s.get("Contact_Persons")))) if s.get("Contact_Persons") else None
            details = {
                "zoho_opportunity_id": squash(s.get("ID")) or None,
                "zoho_position_id": squash(p.get("ID")) or None,
                "tm_positions_count": positions_n,
                "tm_exp_min": exp_min, "tm_exp_max": exp_max,
                "tm_work_location": branch.city or None,
                "customer_jd_attachment_name": squash(s.get("Customer_JD_Attachment") or p.get("JD_Attachment")) or None,
                "onboarded_count": int(numf(s.get("Onboarded_Count") or p.get("Onboarded_Count")) or 0),
                "imported_from": "zoho-active-2026-09",
                "in_sales_active": bool(s), "in_ta_active": bool(p),
            }
            opp = existing_opps.get(oid)
            if opp is None:
                opp = Opportunity(
                    opp_id=oid, title=title[:255], customer_id=customer.id, branch_id=branch.id,
                    contact_person_id=contact.id if contact else None,
                    opp_type=OppType.T_AND_M, pipeline_stage=PipelineStage.ACTIVE,
                    approval_status=OpportunityApprovalStatus.APPROVED,
                    rfi_received_date=received.date() if received else None,
                    details=details, created_by=user_id,
                )
                db.add(opp)
                db.flush()
                existing_opps[oid] = opp
                created_opps += 1
            else:
                opp.title = title[:255]
                opp.details = {**(opp.details or {}), **details}
            for zid in (details["zoho_opportunity_id"], details["zoho_position_id"]):
                if zid:
                    opp_by_zoho_id[zid] = opp
            req = db.execute(select(Requirement).where(Requirement.opportunity_id == opp.id)).scalars().first()
            if req is None:
                db.add(Requirement(
                    req_number=next_sequence_number(db, Requirement, Requirement.req_number, "REQ"),
                    opportunity_id=opp.id, customer_id=customer.id, title=title[:255],
                    description=f"Imported from Zoho position {details['zoho_position_id'] or oid}",
                    no_of_positions=positions_n, experience_min=exp_min, experience_max=exp_max,
                    priority=Priority.MEDIUM, status=RequirementStatus.OPEN_FOR_SOURCING,
                    created_by=user_id,
                ))
                db.flush()
                created_reqs += 1
        print(f"OPPORTUNITIES: {created_opps} created, {skipped} skipped · REQUIREMENTS: {created_reqs} created")

        # ---------------------------------------------------- candidates + profiles
        cands = db.execute(select(Candidate)).scalars().all()
        by_cv: dict[str, Candidate] = {}
        by_name: dict[str, list[Candidate]] = {}
        for c in cands:
            if c.cv_url:
                by_cv[Path(c.cv_url).name.lower()] = c
            full = norm(" ".join(x for x in (c.first_name, getattr(c, "middle_name", None), c.last_name) if x))
            by_name.setdefault(full, []).append(c)

        prof_created = prof_updated = cand_created = rounds = unmatched_opp = 0
        matched = {"cv": 0, "name": 0, "new": 0}
        seen_pairs: set[tuple[int, int]] = set()
        for r in profiles:
            oid = squash(r.get("Opportunity_ID"))
            opp = existing_opps.get(oid)
            if opp is None:
                unmatched_opp += 1
                print(f"  ! profile {r.get('ID')} ({r.get('Candidate.Name')}): opportunity {oid!r} not imported")
                continue
            raw_name = r.get("Candidate.Name") or r.get("Candidate") or ""
            first, last = split_name(raw_name)
            cv_name = squash(r.get("CV"))
            cand = by_cv.get(cv_name.lower()) if cv_name else None
            how = "cv" if cand else None
            if cand is None:
                hits = by_name.get(norm(f"{first} {last}"), [])
                if len(hits) == 1:
                    cand, how = hits[0], "name"
            if cand is None:
                cand = Candidate(first_name=first[:120], last_name=(last or None),
                                 email=placeholder_email(first, last, squash(r.get("ID"))),
                                 cv_url=None)
                db.add(cand)
                db.flush()
                by_name.setdefault(norm(f"{first} {last}"), []).append(cand)
                cand_created += 1
                how = "new"
            matched[how] += 1

            status = STATUS_MAP.get(norm(r.get("Candidate_Status")), PipelineStatus.SOURCING)
            ta_name = norm(r.get("Talent_Acquisition_Person"))
            ta_id = users_by_name.get(ta_name)
            expected = numf(r.get("Expected_CTC"))
            if expected is not None and expected <= 500:
                expected = expected * 100000  # lakhs -> rupees
            created_at = parse_date(r.get("Created_Date"))

            key = (cand.id, opp.id)
            prof = db.execute(select(CandidateProfile).where(
                CandidateProfile.candidate_id == cand.id, CandidateProfile.opportunity_id == opp.id)
            ).scalars().first()
            if key in seen_pairs or prof is not None:
                if prof is not None:
                    prof.pipeline_status = status
                    prof.zoho_profile_id = squash(r.get("ID"))[:32] or prof.zoho_profile_id
                    prof_updated += 1
                continue
            seen_pairs.add(key)
            prof = CandidateProfile(
                candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status,
                expected_ctc=Decimal(str(expected)) if expected is not None else None,
                zoho_profile_id=squash(r.get("ID"))[:32] or None,
                ta_owner_id=ta_id, ta_owner_name=squash(r.get("Talent_Acquisition_Person")) or None,
                applied_on=created_at, source="zoho",
            )
            db.add(prof)
            db.flush()
            prof_created += 1
            db.add(CandidateProfileActivityLog(
                profile_id=prof.id, user_id=user_id, action_type="CREATED",
                comment=f"Imported from Zoho (profile {r.get('ID')}) with status {r.get('Candidate_Status')}"))

            # Current interview round -> Interviews tab (manual route: RMG's own L1/L2).
            kind = ROUND_MAP.get(norm(r.get("CurrentInterviewRecord.Interview_Round")))
            if kind:
                rstatus = ROUND_STATUS_MAP.get(norm(r.get("CurrentInterviewRecord.Interview_Status")), "Scheduled")
                when = parse_date(r.get("CurrentInterviewRecord.Interview_Date_Time_From"))
                db.add(InterviewEvent(
                    profile_id=prof.id, candidate_id=cand.id, kind=kind,
                    scheduled_at=when, raw_when=squash(r.get("CurrentInterviewRecord.Interview_Date_Time_From")) or None,
                    status=rstatus, interview_category="Internal", user_role="RMG", created_by=user_id,
                ))
                db.add(CandidateProfileActivityLog(
                    profile_id=prof.id, user_id=user_id,
                    action_type="L1_REQUESTED" if kind == "L1_Interview" else "L2_REQUESTED",
                    comment=f"Imported from Zoho: {r.get('CurrentInterviewRecord.Interview_Round')} "
                            f"({r.get('CurrentInterviewRecord.Interview_Status') or 'no status'})"))
                rounds += 1

        print(f"CANDIDATES: matched by CV {matched['cv']}, by name {matched['name']}, created {matched['new']}")
        print(f"PROFILES: {prof_created} created, {prof_updated} updated (duplicate rows), "
              f"{unmatched_opp} skipped (opportunity missing) · interview rounds recorded: {rounds}")

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
