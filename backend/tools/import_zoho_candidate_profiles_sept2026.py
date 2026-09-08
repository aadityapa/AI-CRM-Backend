r"""Replace the Zoho-imported candidate pipeline with the FULL NEXUS export.

Input: `KARNEX_TA_Active_Candidate_Profiles_<date>.json` (7 Sep 2026, user
request) — the rich export: `{"_meta": ..., "records": [...]}` where every
record carries the Zoho Candidate Profile form (status, TA, dates, every
Interview_Round with result/feedback/link/panel, Activity_History,
Skill_Evaluation) and the linked Candidate master (real email, phone, gender,
experience, CTC, skills, education, preferred locations).

What it does, in order:
  1. --replace  deletes every candidate profile the PREVIOUS Zoho import
                created (source = "zoho") with its interview rounds, activity
                log, skill evaluations, offers and AI links; then removes the
                placeholder `@import.karnex.in` candidates that are left with
                nothing attached (the old export had no emails — this one does).
  2. Candidate  match order: zoho_candidate_id -> email -> phone (last 10
                digits) -> CV file name -> exact full name (single hit) -> new.
                Empty fields on a matched candidate are filled from the export;
                skills go to the Skill master + candidate_skills; education
                rows are added when the candidate has none.
  3. Profile    one per (candidate, opportunity) keyed by Opportunity_ID —
                the opportunity must already exist (run
                import_zoho_active_sept2026.py first). Nexus "Candidate Status"
                -> app pipeline status via STATUS_MAP; TA owner, expected CTC,
                Created_Date -> applied_on, the three submission dates, Comments.
                An existing profile for the same pair is UPDATED to the Nexus
                status (never duplicated).
  4. Rounds     every Interview_Round row -> InterviewEvent (deduped by
                zoho_round_id): kind L1_Interview / L2_F2F (Stage RMG) or
                Customer_Interview / Customer_L2 (Stage Sales / Customer),
                date, link, mode, duration, panel employee, status, result,
                feedback. L1/L2 rounds also log L1_REQUESTED / L2_REQUESTED so
                the Applied Candidates tab shows the round chips.
  5. Activity   Activity_History rows -> activity log ("NOTE", HTML stripped),
                Skill_Evaluation rows -> skill_evaluations.

Usage (from backend/, same venv as the app):
    python tools\import_zoho_candidate_profiles_sept2026.py --file "D:\x.json"            # DRY RUN
    python tools\import_zoho_candidate_profiles_sept2026.py --file "D:\x.json" --replace  # dry run incl. deletes
    python tools\import_zoho_candidate_profiles_sept2026.py --file "D:\x.json" --replace --apply

DRY RUN by default; nothing is written without --apply. Take a backup first.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import bindparam, func, select, text  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import (  # noqa: E402
    Candidate, CandidateEducation, CandidateProfile, CandidateProfileActivityLog, CandidateSkill,
    Employee, InterviewEvent, Location, Opportunity, PipelineStatus, Skill, SkillEvaluation,
)

#: Nexus "Candidate Status" -> app pipeline status (aligned 7 Sep 2026).
STATUS_MAP = {
    "sourcing": PipelineStatus.SOURCING,
    "technical screening": PipelineStatus.TECHNICAL_SCREENING,
    "technical interviewing": PipelineStatus.TECHNICAL_SCREENING,   # shown as "Technical Interviewing"
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
#: (round name, stage) -> InterviewEvent.kind. RMG rounds are the internal
#: L1/L2; Sales/Customer-stage rounds are the customer's.
INTERNAL_KINDS = {"l1-interview": "L1_Interview", "l1 interview": "L1_Interview",
                  "l2-interview": "L2_F2F", "l2 interview": "L2_F2F"}
CUSTOMER_KINDS = {"l1-interview": "Customer_Interview", "l1 interview": "Customer_Interview",
                  "l2-interview": "Customer_L2", "l2 interview": "Customer_L2"}
REQUEST_ACTION = {"L1_Interview": "L1_REQUESTED", "L2_F2F": "L2_REQUESTED"}
ROUND_STATUS_MAP = {
    "pending": "Pending", "scheduled": "Scheduled", "completed": "Completed",
    "rescheduled requested by candidate": "Rescheduled Requested By Candidate",
    "rescheduled requested by panel": "Rescheduled Requested By Panel",
    "cancelled": "Cancelled", "no show": "No Show", "in-progress": "In-Progress",
}
PREFIXES = ("mr.", "mrs.", "ms.", "miss", "dr.", "mr", "ms", "mrs")
EMAIL_DOMAIN = "import.karnex.in"
DATE_FMTS = ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d")


def norm(s) -> str:
    return " ".join(str(s or "").split()).strip().lower()


def squash(s) -> str:
    return " ".join(str(s or "").split()).strip()


def lookup_value(v) -> str:
    """Zoho lookup `{id, value}` -> value; plain strings pass through."""
    if isinstance(v, dict):
        return squash(v.get("value"))
    if isinstance(v, list):
        return ", ".join(x for x in (lookup_value(i) for i in v) if x)
    return squash(v)


def numf(v):
    try:
        s = str(v if v is not None else "").strip().replace(",", "")
        return float(s) if s else None
    except ValueError:
        return None


def lakhs_to_rupees(v):
    f = numf(v)
    if f is None or f <= 0:
        return None
    r = f * 100000 if f <= 500 else f
    return Decimal(str(round(r, 2))) if r < 10 ** 9 else None   # > 100 crore = junk


def years(v):
    """Experience in years, or None for junk (a phone number typed in the
    box overflowed Numeric(4,1) on the live run)."""
    f = numf(v)
    return round(f, 1) if f is not None and 0 < f < 60 else None


def parse_date(raw):
    s = squash(raw)
    for fmt in DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def strip_html(s) -> str:
    t = re.sub(r"<br\s*/?>", "\n", str(s or ""), flags=re.I)
    t = re.sub(r"</(div|p)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return "\n".join(line.strip() for line in html.unescape(t).splitlines() if line.strip())


def strip_prefix(name: str) -> str:
    n = squash(name)
    low = n.lower()
    for p in PREFIXES:
        if low.startswith(p + " "):
            return n[len(p) + 1:].strip()
    return n


def split_name(rec) -> tuple[str | None, str, str]:
    """(salutation, first, last) from the Candidate master Name block, else the
    display name."""
    cand = rec.get("candidate") or {}
    nm = cand.get("Name") if isinstance(cand.get("Name"), dict) else None
    if nm and squash(nm.get("first_name")):
        first = squash(nm.get("first_name"))
        last = " ".join(x for x in (squash(nm.get("last_name")), squash(nm.get("suffix"))) if x)
        return (squash(nm.get("prefix")) or None), first, last
    full = (rec.get("summary") or {}).get("candidate_name") or lookup_value(
        (rec.get("candidate_profile") or {}).get("Candidate")) or ""
    parts = strip_prefix(full).split()
    sal = squash(full).split(" ")[0] if norm(full).split(" ")[0] in PREFIXES else None
    if not parts:
        return sal, "Candidate", ""
    return sal, parts[0], " ".join(parts[1:])


def phone_key(p) -> str:
    return "".join(ch for ch in str(p or "") if ch.isdigit())[-10:]


def placeholder_email(first: str, last: str, zoho_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", ".", f"{first} {last}".lower()).strip(".") or "candidate"
    digest = hashlib.sha256((zoho_id or base).encode()).hexdigest()[:8]
    return f"{base}.{digest}@{EMAIL_DOMAIN}"


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    replace = "--replace" in argv

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
        cands = sorted((Path(__file__).resolve().parent.parent.parent / "import_templates")
                       .glob("KARNEX_TA_Active_Candidate_Profiles_*.json"))
        if not cands:
            print("Pass --file <export.json>")
            return 2
        path = cands[-1]
    doc = json.loads(path.read_text(encoding="utf-8"))
    records = doc.get("records") if isinstance(doc, dict) else doc
    if not isinstance(records, list):
        print("Unexpected file shape — expected {\"_meta\":..., \"records\":[...]}")
        return 2
    print(f"File: {path.name} · records: {len(records)} · exported {(doc.get('_meta') or {}).get('exported_at')}")
    print(f"Mode: {'APPLY' if apply else 'DRY RUN'}{' + REPLACE previous zoho profiles' if replace else ''}")

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
        users_by_name: dict[str, int] = {}
        for uid, full, uname in db.execute(text("SELECT id, full_name, username FROM registration_data")).all():
            for key in (norm(full), norm(uname)):
                if key:
                    users_by_name.setdefault(key, uid)

        # ------------------------------------------------------------- replace
        if replace:
            profs = "SELECT id FROM candidate_profiles WHERE source = 'zoho'"
            n = db.execute(text(f"SELECT count(*) FROM ({profs}) p")).scalar() or 0
            for sql in [
                f"DELETE FROM ai_interview_links WHERE profile_id IN ({profs})",
                f"UPDATE employees SET candidate_profile_id = NULL WHERE candidate_profile_id IN ({profs})",
                f"DELETE FROM interview_events WHERE profile_id IN ({profs})",
                f"DELETE FROM offer_history WHERE profile_id IN ({profs})",
                f"DELETE FROM skill_evaluations WHERE profile_id IN ({profs})",
                f"DELETE FROM candidate_profile_activity_log WHERE profile_id IN ({profs})",
                "DELETE FROM candidate_profiles WHERE source = 'zoho'",
            ]:
                db.execute(text(sql))
            # Placeholder candidates from the e-mail-less export, now orphaned.
            orphan = f"""
                SELECT c.id FROM candidates c
                WHERE c.email LIKE '%@{EMAIL_DOMAIN}'
                  AND NOT EXISTS (SELECT 1 FROM candidate_profiles p WHERE p.candidate_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM resumes r WHERE r.candidate_id = c.id OR r.possible_duplicate_of = c.id)
                  AND NOT EXISTS (SELECT 1 FROM ai_interview_links l WHERE l.candidate_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM slot_bookings b WHERE b.candidate_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM interview_events e WHERE e.candidate_id = c.id)
                  AND NOT EXISTS (SELECT 1 FROM candidate_outreach o WHERE o.candidate_id = c.id)
            """
            orphans = [r[0] for r in db.execute(text(orphan)).all()]
            if orphans:
                for tbl in ("candidate_skills", "candidate_education", "candidate_experience"):
                    db.execute(text(f"DELETE FROM {tbl} WHERE candidate_id IN :ids")
                               .bindparams(bindparam("ids", expanding=True)), {"ids": orphans})
                db.execute(text("DELETE FROM candidates WHERE id IN :ids")
                           .bindparams(bindparam("ids", expanding=True)), {"ids": orphans})
            db.expire_all()
            print(f"REPLACE: {n} previous zoho profiles deleted, {len(orphans)} placeholder candidates removed")

        # ------------------------------------------------------------- lookups
        opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        employees_by_name: dict[str, int] = {}
        for e in db.execute(select(Employee)).scalars().all():
            employees_by_name.setdefault(norm(f"{e.first_name} {e.last_name or ''}"), e.id)
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        locations_by_city = {norm(loc.city): loc for loc in db.execute(select(Location)).scalars().all()}

        cands = db.execute(select(Candidate)).scalars().all()
        by_zoho: dict[str, Candidate] = {}
        by_email: dict[str, Candidate] = {}
        by_phone: dict[str, Candidate] = {}
        by_cv: dict[str, Candidate] = {}
        by_name: dict[str, list[Candidate]] = {}

        def index_candidate(c: Candidate):
            if c.zoho_candidate_id:
                by_zoho.setdefault(c.zoho_candidate_id, c)
            if c.email and not c.email.lower().endswith("@" + EMAIL_DOMAIN):
                by_email.setdefault(c.email.lower(), c)
            pk = phone_key(c.phone)
            if len(pk) == 10:
                by_phone.setdefault(pk, c)
            for fn in (c.cv_url, getattr(c, "cv_original_filename", None)):
                if fn:
                    by_cv.setdefault(Path(fn).name.lower(), c)
            full = norm(" ".join(x for x in (c.first_name, c.middle_name, c.last_name) if x))
            by_name.setdefault(full, []).append(c)

        for c in cands:
            index_candidate(c)

        def get_skill(name: str) -> Skill | None:
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

        # ------------------------------------------------------------ records
        matched = {"zoho": 0, "email": 0, "phone": 0, "cv": 0, "name": 0, "new": 0}
        prof_created = prof_updated = rounds = acts = skill_evals = unmatched_opp = 0
        status_counts: dict[str, int] = {}
        seen_pairs: set[tuple[int, int]] = set()

        for rec in records:
            summ = rec.get("summary") or {}
            cp = rec.get("candidate_profile") or {}
            cm = rec.get("candidate") or {}
            zoho_profile_id = squash(rec.get("zoho_profile_record_id"))
            zoho_cand_id = squash(rec.get("zoho_candidate_record_id"))
            oid = squash(summ.get("opportunity_id") or cp.get("Opportunity_ID"))
            opp = opps.get(oid)
            if opp is None:
                unmatched_opp += 1
                print(f"  ! {summ.get('candidate_name')} ({summ.get('email')}): opportunity {oid!r} not in the tool")
                continue

            # -------- candidate
            sal, first, last = split_name(rec)
            email = norm(summ.get("email") or cm.get("Email"))
            phone = squash(summ.get("phone") or cm.get("Phone_Number")) or None
            cv_name = squash(rec.get("cv_original_file_name") or cp.get("CV") or cm.get("CV"))
            cand = how = None
            if zoho_cand_id and zoho_cand_id in by_zoho:
                cand, how = by_zoho[zoho_cand_id], "zoho"
            if cand is None and email and email in by_email:
                cand, how = by_email[email], "email"
            if cand is None and len(phone_key(phone)) == 10 and phone_key(phone) in by_phone:
                cand, how = by_phone[phone_key(phone)], "phone"
            if cand is None and cv_name and cv_name.lower() in by_cv:
                cand, how = by_cv[cv_name.lower()], "cv"
            if cand is None:
                hits = by_name.get(norm(f"{first} {last}"), [])
                if len(hits) == 1:
                    cand, how = hits[0], "name"
            if cand is None:
                cand = Candidate(first_name=first[:120], last_name=(last or None)[:120] if last else None,
                                 email=(email if email and "@" in email
                                        else placeholder_email(first, last, zoho_cand_id or zoho_profile_id)))
                db.add(cand)
                db.flush()
                how = "new"
            matched[how] += 1

            # fill-only-empty enrichment from the Candidate master
            def fill(attr, value):
                if value not in (None, "", Decimal("0")) and not getattr(cand, attr, None):
                    setattr(cand, attr, value)

            if zoho_cand_id:
                cand.zoho_candidate_id = zoho_cand_id[:32]
            fill("salutation", (sal or "")[:10] or None)
            fill("phone", (phone or "")[:32] or None)
            if email and "@" in email and cand.email.lower().endswith("@" + EMAIL_DOMAIN) and email not in by_email:
                cand.email = email          # placeholder -> real address
            fill("gender", squash(cm.get("Gender"))[:20] or None)
            exp = years(cm.get("Expereince"))
            if exp:
                fill("experience_years", Decimal(str(exp)))
            npd = squash(cm.get("Notice_Period") or cp.get("Notice_Period"))
            if npd and npd != "0":
                fill("notice_period", (f"{npd} days" if npd.isdigit() else npd)[:60])
            fill("current_ctc", lakhs_to_rupees(cm.get("CTC")))
            fill("expected_ctc", lakhs_to_rupees(cm.get("Expected_CTC_Lac_Annual") or cp.get("Expected_CTC")))
            city = lookup_value(cm.get("City"))
            fill("city", city[:120] or None)
            pref = lookup_value(cm.get("Prefered_Location"))
            fill("preferred_locations", pref[:500] or None)
            if pref and not cand.preferred_location_id:
                loc = locations_by_city.get(norm(pref.split(",")[0]))
                if loc:
                    cand.preferred_location_id = loc.id
            fill("roles", lookup_value(cm.get("Roles"))[:255] or None)
            fill("technical_domain", lookup_value(cm.get("Domains"))[:120] or None)
            fill("cv_original_filename", cv_name[:255] or None)
            addr = cm.get("Current_Address") or {}
            if isinstance(addr, dict):
                line = ", ".join(x for x in (squash(addr.get("address_line_1")), squash(addr.get("address_line_2")),
                                             squash(addr.get("district_city")), squash(addr.get("state_province")),
                                             squash(addr.get("postal_Code"))) if x)
                fill("current_address", line or None)
            fill("resignation_status", True if cm.get("Is_Resigned") is True else None)
            lwd = parse_date(cm.get("Last_Working_Day"))
            if lwd:
                fill("last_working_day", lwd.date())
            dob = parse_date(cm.get("Date_of_Birth"))
            if dob:
                fill("date_of_birth", dob.date())
            created = parse_date(cp.get("Created_Date") or summ.get("created_date"))
            if created:
                fill("source_created_date", created.date())
            # skills
            have = {cs.skill_id for cs in db.execute(
                select(CandidateSkill).where(CandidateSkill.candidate_id == cand.id)).scalars().all()}
            for sk in cm.get("Skills") or []:
                s = get_skill(lookup_value(sk))
                if s and s.id not in have:
                    db.add(CandidateSkill(candidate_id=cand.id, skill_id=s.id))
                    have.add(s.id)
            # education (only when the candidate has none)
            edu_rows = cm.get("Education_Details") or []
            if edu_rows and not db.execute(select(CandidateEducation.id)
                                           .where(CandidateEducation.candidate_id == cand.id).limit(1)).first():
                for e in edu_rows:
                    course = " ".join(x for x in (squash(e.get("Course")), squash(e.get("Branch_Specialization"))) if x)
                    if not course:
                        continue
                    sd, ed = parse_date(e.get("Start_of_Course")), parse_date(e.get("End_of_Course"))
                    db.add(CandidateEducation(candidate_id=cand.id, course=course[:255],
                                              institution=squash(e.get("University_College"))[:255] or None,
                                              start_date=sd.date() if sd else None, end_date=ed.date() if ed else None))
            if how == "new":
                index_candidate(cand)

            # -------- profile
            raw_status = squash(summ.get("candidate_status") or cp.get("Candidate_Status"))
            status = STATUS_MAP.get(norm(raw_status), PipelineStatus.SOURCING)
            status_counts[status.value] = status_counts.get(status.value, 0) + 1
            ta_name = squash(summ.get("talent_acquisition_person") or cp.get("Talent_Acquisition_Person"))
            ta_id = users_by_name.get(norm(ta_name))
            expected = lakhs_to_rupees(cp.get("Expected_CTC") or summ.get("expected_ctc"))
            key = (cand.id, opp.id)
            prof = db.execute(select(CandidateProfile).where(
                CandidateProfile.candidate_id == cand.id, CandidateProfile.opportunity_id == opp.id)
            ).scalars().first()
            is_new = prof is None and key not in seen_pairs
            if prof is None and key in seen_pairs:
                continue
            seen_pairs.add(key)
            if prof is None:
                prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, pipeline_status=status,
                                        source="zoho", ta_owner_id=ta_id)
                db.add(prof)
                prof_created += 1
            else:
                prof.pipeline_status = status
                prof_updated += 1
            prof.zoho_profile_id = zoho_profile_id[:32] or prof.zoho_profile_id
            prof.ta_owner_name = ta_name or prof.ta_owner_name
            prof.ta_owner_id = prof.ta_owner_id or ta_id
            if expected is not None:
                prof.expected_ctc = expected
            cur = lakhs_to_rupees(cp.get("Current_CTC_Lac"))
            if cur is not None:
                prof.current_ctc = cur
            if created and not prof.applied_on:
                prof.applied_on = created
            for attr, key_ in (("technical_submission_date", "Technical_Submission_Date"),
                               ("sales_submission_date", "Sales_Submission_Date"),
                               ("customer_submission_date", "Customer_Submission_Date"),
                               ("customer_onboarding_date", "Customer_Onboarding_Date")):
                d = parse_date(cp.get(key_))
                if d and hasattr(prof, attr):
                    setattr(prof, attr, d.date())
            if exp and hasattr(prof, "total_experience_years") and not prof.total_experience_years:
                prof.total_experience_years = Decimal(str(exp))
            db.flush()

            if is_new:
                db.add(CandidateProfileActivityLog(
                    profile_id=prof.id, user_id=user_id, action_type="CREATED",
                    comment=f"Imported from NEXUS (profile {zoho_profile_id}) — status {raw_status}"
                            + (f", TA {ta_name}" if ta_name else ""),
                    **({"timestamp": created} if created else {})))
            note = strip_html(cp.get("Comments"))
            if note and is_new:
                db.add(CandidateProfileActivityLog(profile_id=prof.id, user_id=user_id,
                                                   action_type="NOTE", comment=note[:4000]))

            # -------- interview rounds
            existing_rounds = {e.zoho_round_id for e in db.execute(
                select(InterviewEvent).where(InterviewEvent.profile_id == prof.id)).scalars().all() if e.zoho_round_id}
            requested_logged: set[str] = set()
            for ir in cp.get("Interview_Round") or []:
                rid = squash(ir.get("_row_id"))
                if rid and rid in existing_rounds:
                    continue
                rname = norm(lookup_value(ir.get("Interview_Round")))
                stage = squash(ir.get("Stage"))
                is_internal = norm(stage) in ("rmg", "ta", "") and norm(ir.get("Interviewer_Category")) != "external"
                kind = (INTERNAL_KINDS if is_internal else CUSTOMER_KINDS).get(rname)
                if not kind:
                    continue
                when = parse_date(ir.get("Interview_Date_Time_From"))
                end = parse_date(ir.get("Interview_Date_Time_End"))
                link = ir.get("Interview_Link")
                link = squash(link.get("zcurl") or link.get("zclnkname")) if isinstance(link, dict) else squash(link)
                panel = lookup_value(ir.get("Employee"))
                ext = squash(ir.get("External_Interviewer") or ir.get("External_Interview_Panel"))
                dur = numf(re.sub(r"[^\d.]", "", squash(ir.get("Interview_Duration"))))
                emp_id = None
                for nm in [p.strip() for p in panel.split(",") if p.strip()]:
                    emp_id = employees_by_name.get(norm(nm))
                    if emp_id:
                        break
                db.add(InterviewEvent(
                    profile_id=prof.id, candidate_id=cand.id, kind=kind,
                    scheduled_at=when, scheduled_end=end,
                    raw_when=squash(ir.get("Interview_Date_Time_From"))[:64] or None,
                    meeting_link=link[:1024] or None,
                    stage=stage[:120] or None, mode=squash(ir.get("Interview_Mode"))[:60] or None,
                    status=ROUND_STATUS_MAP.get(norm(ir.get("Interview_Status")), squash(ir.get("Interview_Status"))[:60] or None),
                    result=squash(ir.get("Result"))[:60] or None,
                    interviewer=(panel or ext)[:200] or None,
                    external_panel=ext[:255] or None,
                    feedback=strip_html(ir.get("OverAll_Feedback")) or None,
                    interview_category=squash(ir.get("Interviewer_Category"))[:20] or ("Internal" if is_internal else "External"),
                    duration_minutes=int(dur) if dur else None,
                    user_role="RMG" if is_internal else "Sales",
                    employee_id=emp_id, venue=squash(ir.get("Venue_Details"))[:255] or None,
                    weightage=squash(ir.get("Weightage"))[:60] or None,
                    zoho_round_id=rid[:32] or None, created_by=user_id,
                ))
                rounds += 1
                act = REQUEST_ACTION.get(kind)
                if act and act not in requested_logged:
                    already = db.execute(select(CandidateProfileActivityLog.id).where(
                        CandidateProfileActivityLog.profile_id == prof.id,
                        CandidateProfileActivityLog.action_type == act).limit(1)).first()
                    if not already:
                        db.add(CandidateProfileActivityLog(
                            profile_id=prof.id, user_id=user_id, action_type=act,
                            comment=f"Imported from NEXUS: {lookup_value(ir.get('Interview_Round'))} "
                                    f"({squash(ir.get('Interview_Status')) or 'no status'})",
                            **({"timestamp": when} if when else {})))
                    requested_logged.add(act)

            # -------- activity history + skill evaluation (new profiles only)
            if is_new:
                for a in cp.get("Activity_History") or []:
                    c = strip_html(a.get("Comments"))
                    if c:
                        db.add(CandidateProfileActivityLog(profile_id=prof.id, user_id=user_id,
                                                           action_type="NOTE", comment=f"[NEXUS] {c}"[:4000]))
                        acts += 1
                for se in cp.get("Skill_Evaluation") or []:
                    s = get_skill(lookup_value(se.get("Skill_Name")))
                    if not s:
                        continue

                    def lvl(v):
                        f = numf(v)
                        return int(max(1, min(5, round(f)))) if f else None

                    db.add(SkillEvaluation(profile_id=prof.id, skill_id=s.id,
                                           required_level=lvl(se.get("Required_Level")),
                                           self_rated=lvl(se.get("Skill_Level_Self_Rating")),
                                           reviewer_rated=lvl(se.get("RMG_Rating"))))
                    skill_evals += 1

        print(f"CANDIDATES matched: by zoho id {matched['zoho']}, email {matched['email']}, phone {matched['phone']}, "
              f"CV {matched['cv']}, name {matched['name']} · created {matched['new']}")
        print(f"PROFILES: {prof_created} created, {prof_updated} updated, {unmatched_opp} skipped (opportunity missing)")
        print(f"ROUNDS: {rounds} interview rounds · ACTIVITY: {acts} history notes · SKILL EVALS: {skill_evals}")
        print("STATUS:", ", ".join(f"{k} {v}" for k, v in sorted(status_counts.items(), key=lambda kv: -kv[1])))

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
