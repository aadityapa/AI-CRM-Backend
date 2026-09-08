"""Import the Zoho NEXUS candidate -> applied-opportunities mapping.

This is the authoritative candidate<->opportunity linkage that the earlier Zoho
exports were missing (see link_candidates_opportunities.py, which documents that
the Candidates subform came back empty). Each entry in `applied_positions` is one
application = one row in `candidate_profiles`.

The JSON carries the linkage but no interview history, so every application is
joined by `profile_id` to `ta_all_candidate_profiles.ndjson` (Zoho record id) to
pull in the detail Zoho keeps in subforms:

    Interview_Round        -> interview_events
    Skill_Evaluation       -> skill_evaluations
    Offer_Release_History  -> offer_history
    Activity_History       -> candidate_profile_activity_log

Resolution order for the candidate:
    JSON candidate_id -> all_candidates_clean.csv zohoRecId -> email -> app candidate
    -> JSON email -> deterministic placeholder (<slug>.<sha8>@import.karnex.in)
    -> unique exact full-name match
Unresolved rows are skipped and counted, never guessed.

Opportunities resolve on `opportunities.opp_id` (e.g. "C-2026-00047"). Import
opportunities and candidates first:
    python tools/import_opportunities_full.py --apply
    python tools/import_candidates_full.py --apply

Dry run is the default; nothing is written without --apply.

Usage:
    python tools/import_applied_opportunities.py [candidate_applied_opportunities.json]
        [--apply] [--replace] [--user <username-or-email>]
        [--details <ta_all_candidate_profiles.ndjson>]
        [--report <unlinked_report.csv>]

    --replace   delete candidate_profiles that are absent from this file
                (and their activity/interview/skill/offer children)
    --user      acting user for created_by / activity rows (default: lowest id)
    --report    where to write the skipped-rows CSV (default: <json dir>/import_applied_unlinked.csv)
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ on sys.path

from sqlalchemy import select, text

from crm_db import get_session_factory
from models import (
    Candidate, CandidateProfile, CandidateProfileActivityLog, Employee, InterviewEvent,
    OfferHistory, OfferStatus, Opportunity, PipelineStatus, Skill, SkillEvaluation,
)
from services.interview_rounds import CATEGORIES, DURATIONS, STATUSES, USER_ROLES
from tools.import_candidate_profiles import (
    OFFER_STATUS_MAP, STATUS_MAP, lac_to_rupees, load_candidate_csv, norm, numf,
    parse_date, parse_dt, slugify, squash, strip_name, ts_from_comment, txt,
)

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "import_templates"
DEFAULT_JSON = TEMPLATES / "candidate_applied_opportunities.json"
DEFAULT_DETAILS = TEMPLATES / "ta_all_candidate_profiles.ndjson"
EMAIL_DOMAIN = "import.karnex.in"

TAG_RE = re.compile(r"<[^>]+>")

# Zoho "Interview Round" text -> interview_events.kind
ROUND_KIND = {
    "l1": "L1_Interview", "l1-interview": "L1_Interview", "l1 interview": "L1_Interview",
    "l2": "L2_F2F", "l2-interview": "L2_F2F", "l2 interview": "L2_F2F",
    "l2-face to face": "L2_F2F", "face to face": "L2_F2F", "f2f": "L2_F2F",
    "l3": "L3_Interview", "l3-interview": "L3_Interview", "l3 interview": "L3_Interview",
    "l4": "L4_Interview", "l4-interview": "L4_Interview", "l4 interview": "L4_Interview",
    "hr": "HR_Interview", "hr-interview": "HR_Interview", "hr interview": "HR_Interview",
    "client": "Customer_Interview", "customer": "Customer_Interview",
    "customer interview": "Customer_Interview",
}

DURATION_RE = re.compile(r"(\d+)")


def duration_minutes(raw) -> int | None:
    """"15 Minute" / "60 Minutes" / 30 -> minutes. Snapped to the nearest allowed
    value so imported rounds land on a value the feedback form can re-select."""
    m = DURATION_RE.search(squash(raw))
    if not m:
        return None
    val = int(m.group(1))
    return min(DURATIONS, key=lambda d: abs(d - val)) if val else None


def html_text(v):
    """Zoho exports email/phone wrapped in anchor markup - take the visible text."""
    if not isinstance(v, str):
        return None
    return squash(TAG_RE.sub(" ", v)) or None


def round_kind(raw: str) -> str:
    n = norm(raw)
    if n in ROUND_KIND:
        return ROUND_KIND[n]
    for key, val in ROUND_KIND.items():
        if key and key in n:
            return val
    return "Other"


def zoho_link(v):
    if isinstance(v, dict):
        return squash(v.get("zcurl") or v.get("zclnk") or v.get("zclnkname")) or None
    return squash(v) or None


def people_text(v) -> str | None:
    """Zoho people fields (Employee, External_Interview_Panel) come back as a LIST
    of {id, text} dicts — never a bare dict. Without this they stringify into the
    raw repr "[{'id': '2701...', 'text': 'Mohit Arya'}]"."""
    if v in (None, "", []):
        return None
    if isinstance(v, dict):
        return squash(v.get("text") or v.get("name")) or None
    if isinstance(v, (list, tuple)):
        names = [n for n in (people_text(item) for item in v) if n]
        return ", ".join(dict.fromkeys(names)) or None
    return squash(v) or None


#: Prefixes the older importer used when flattening a round into `note`.
LEGACY_NOTE_PREFIXES = ("round:", "stage:", "mode:", "status:", "result:")


def _vocab(raw, allowed: list[str]) -> str | None:
    """Case-insensitive snap onto the app's dropdown vocabulary, so e.g. Zoho's
    "ReScheduled Requested By Candidate" stores as the canonical spelling."""
    v = squash(raw)
    if not v:
        return None
    return next((a for a in allowed if norm(a) == norm(v)), v)


def _note_is_legacy_blob(note: str | None, feedback: str | None) -> bool:
    """True when `note` is the old flattened round summary — i.e. it starts with
    "Round:" or already contains the feedback we now store in its own column.
    Those notes render as a second copy of the feedback on the Interviews tab."""
    n = squash(note)
    if not n:
        return False
    if n.lower().startswith(LEGACY_NOTE_PREFIXES):
        return True
    fb = squash(feedback)
    return bool(fb and fb in n)


def load_details(path: Path) -> dict[str, dict]:
    """Zoho profile record id -> full NDJSON record (interview rounds, offers, ...)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = squash(rec.get("zohoRecId")) or squash(rec.get("PKVALUE"))
            if rid:
                out[rid] = rec
    return out


def flatten(src: Path) -> tuple[list[dict], list[dict]]:
    """JSON -> (applications, unlinked). One application per applied_positions entry."""
    doc = json.loads(src.read_text(encoding="utf-8"))
    apps: list[dict] = []
    for cand in doc.get("candidates") or []:
        zid = squash(cand.get("candidate_id"))
        email = html_text(cand.get("email"))
        phone = html_text(cand.get("phone"))
        name = strip_name(cand.get("name") or "")
        for pos in cand.get("applied_positions") or []:
            apps.append({
                "profile_id": squash(pos.get("profile_id")),
                "cand_zoho_id": zid,
                "cand_email": email,
                "cand_phone": phone,
                "cand_name": name,
                "opp_id": squash(pos.get("opportunity_id")) or None,
                "opp_title": squash(pos.get("opportunity_title")) or None,
                "customer": squash(pos.get("customer")) or None,
                "status": squash(pos.get("status")) or None,
                "ta_person": squash(pos.get("ta_person")) or None,
                "expected_ctc": pos.get("expected_ctc"),
                "applied_on": parse_dt(pos.get("applied_on")),
            })
    unlinked = [{
        "profile_id": squash(u.get("profile_id")),
        "candidate_name": squash(u.get("candidate_name")) or None,
        "opp_id": squash(u.get("opportunity_id")) or None,
        "opp_title": squash(u.get("opportunity_title")) or None,
        "customer": squash(u.get("customer")) or None,
        "status": squash(u.get("status")) or None,
        "reason": "no candidate attached in Zoho",
    } for u in doc.get("unlinked_applications") or []]
    return apps, unlinked


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    replace = "--replace" in argv

    def take_flag(flag: str):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
        return None

    username = take_flag("--user")
    details_arg = take_flag("--details")
    report_arg = take_flag("--report")
    skip = {"--apply", "--replace"}
    positional = [a for i, a in enumerate(argv)
                  if a not in skip and not a.startswith("--")
                  and (i == 0 or argv[i - 1] not in {"--user", "--details", "--report"})]
    src = Path(positional[0]) if positional else DEFAULT_JSON
    details_path = Path(details_arg) if details_arg else DEFAULT_DETAILS
    report_path = Path(report_arg) if report_arg else src.parent / "import_applied_unlinked.csv"

    if not src.exists():
        print(f"ERROR: source not found: {src}")
        return 2

    apps, unlinked = flatten(src)
    details = load_details(details_path)
    print(f"Source     : {src}")
    print(f"Details    : {details_path} ({len(details):,} records)"
          if details else f"Details    : {details_path} (MISSING - interview history skipped)")
    print(f"Parsed     : {len(apps):,} applications, {len(unlinked)} unlinked")
    print(f"Mode       : {'APPLY' if apply else 'DRY RUN'}"
          f"{' + REPLACE' if replace else ''}\n")

    zoho_cands = load_candidate_csv(TEMPLATES / "all_candidates_clean.csv")
    db = get_session_factory()()
    stats = {k: 0 for k in (
        "created", "updated", "skipped_no_cand", "skipped_no_opp", "dupe_pair",
        "events", "events_merged", "evals", "offers", "activity", "deleted", "ta_matched")}
    skipped_rows: list[dict] = []

    try:
        if username:
            row = db.execute(
                text("SELECT id FROM registration_data WHERE username=:u OR email=:u LIMIT 1"),
                {"u": username},
            ).first()
            if not row:
                print(f"ERROR: user not found: {username}")
                return 2
            actor_id = row[0]
        else:
            row = db.execute(text("SELECT id FROM registration_data ORDER BY id LIMIT 1")).first()
            actor_id = row[0] if row else None

        users = {norm(u[1]): u[0] for u in db.execute(
            text("SELECT id, username FROM registration_data WHERE username IS NOT NULL")
        ).all()}
        users.update({norm(u[1]): u[0] for u in db.execute(
            text("SELECT id, full_name FROM registration_data WHERE full_name IS NOT NULL")
        ).all() if u[1]})

        cands_by_email: dict[str, Candidate] = {}
        cands_by_name: dict[str, list[Candidate]] = {}
        cands_by_zoho: dict[str, Candidate] = {}
        for c in db.execute(select(Candidate)).scalars().all():
            if c.email:
                cands_by_email[norm(c.email)] = c
            zid = getattr(c, "zoho_candidate_id", None)
            if zid:
                cands_by_zoho[zid] = c
            full = " ".join(x for x in (c.first_name, c.middle_name, c.last_name) if x)
            cands_by_name.setdefault(norm(full), []).append(c)
        opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        profiles = {(p.candidate_id, p.opportunity_id): p
                    for p in db.execute(select(CandidateProfile)).scalars().all()}
        # Panel members that exist in the Employees tab get a real FK; external
        # interviewers keep only the name text.
        employee_by_name: dict[str, int] = {}
        for e_id, first, middle, last in db.execute(
            select(Employee.id, Employee.first_name, Employee.middle_name, Employee.last_name)
        ).all():
            full = norm(" ".join(p for p in (first, middle, last) if p))
            if full:
                employee_by_name.setdefault(full, e_id)
        print(f"DB         : {len(cands_by_email):,} candidates, {len(opps):,} opportunities, "
              f"{len(profiles):,} existing profiles\n")

        def resolve_candidate(a: dict):
            # Direct hit once candidates.json has been imported — no CSV bridge,
            # and immune to anyone editing the email in the app.
            if a["cand_zoho_id"]:
                hit = cands_by_zoho.get(a["cand_zoho_id"])
                if hit is not None:
                    return hit
            bridged = zoho_cands.get(a["cand_zoho_id"]) if a["cand_zoho_id"] else None
            if bridged and bridged.get("email"):
                hit = cands_by_email.get(norm(bridged["email"]))
                if hit:
                    return hit
            if a["cand_email"]:
                hit = cands_by_email.get(norm(a["cand_email"]))
                if hit:
                    return hit
            nm = a["cand_name"] or strip_name((bridged or {}).get("name") or "")
            if nm and a["cand_zoho_id"]:
                toks = nm.split(" ")
                first, last = toks[0], (toks[-1] if len(toks) > 1 else None)
                placeholder = (f"{slugify(first, last)}."
                               f"{hashlib.sha256(a['cand_zoho_id'].encode()).hexdigest()[:8]}"
                               f"@{EMAIL_DOMAIN}")
                hit = cands_by_email.get(norm(placeholder))
                if hit:
                    return hit
            if nm:
                hits = cands_by_name.get(norm(nm), [])
                if len(hits) == 1:
                    return hits[0]
            return None

        # newest application wins when Zoho holds two rows for one (candidate, opportunity)
        apps.sort(key=lambda a: (a["applied_on"] or datetime.min))
        seen_pairs: dict[tuple[int, int], str] = {}
        kept_ids: set[int] = set()

        for a in apps:
            cand = resolve_candidate(a)
            if cand is None:
                stats["skipped_no_cand"] += 1
                skipped_rows.append({**a, "reason": "candidate not found in DB"})
                continue
            opp = opps.get(a["opp_id"]) if a["opp_id"] else None
            if opp is None:
                stats["skipped_no_opp"] += 1
                skipped_rows.append({
                    **a, "reason": f"opportunity {a['opp_id'] or '(blank)'} not found in DB"})
                continue

            pair = (cand.id, opp.id)
            if pair in seen_pairs:
                stats["dupe_pair"] += 1
            seen_pairs[pair] = a["profile_id"]

            prof = profiles.get(pair)
            new = prof is None
            if new:
                prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id)
                db.add(prof)
                profiles[pair] = prof
                stats["created"] += 1
            else:
                stats["updated"] += 1

            prof.zoho_profile_id = a["profile_id"][:32] if a["profile_id"] else None
            status = STATUS_MAP.get(norm(a["status"])) if a["status"] else None
            prof.pipeline_status = status or PipelineStatus.SOURCING
            exp = lac_to_rupees(a["expected_ctc"])
            if exp is not None:
                prof.expected_ctc = exp
            if a["applied_on"]:
                prof.applied_on = a["applied_on"]
            if a["ta_person"]:
                prof.ta_owner_name = a["ta_person"][:120]
                uid = users.get(norm(a["ta_person"]))
                if uid:
                    prof.ta_owner_id = uid
                    stats["ta_matched"] += 1

            db.flush()
            kept_ids.add(prof.id)

            rec = details.get(a["profile_id"]) if details else None
            if not rec:
                continue

            # ---- commercials Zoho keeps only on the profile record ----------
            for field, keys in (
                ("current_ctc", ("Current_CTC_Lac", "Current_CTC")),
                ("ctc_approval_amount", ("CTC_Approved_Lac", "CTC_Approval_Lac", "Approved_CTC")),
            ):
                if getattr(prof, field) is None:
                    for k in keys:
                        val = lac_to_rupees(rec.get(k))
                        if val is not None:
                            setattr(prof, field, val)
                            break
            hike = numf(rec.get("Hike_Expected"))
            if hike is not None and prof.hike_percent is None:
                prof.hike_percent = round(hike, 2)
            if norm(rec.get("Commercial_Approval_Status")) in {"approved", "true", "yes"}:
                prof.commercial_approved = True

            # ---- interview rounds ------------------------------------------
            # Keyed on (kind, scheduled_at) — the SAME key the older
            # import_candidate_profiles.py used. Anything else creates a second row
            # for a round that importer already wrote, which is what produced the
            # duplicate cards on the Interviews tab.
            have_ev: dict[tuple[str, object], InterviewEvent] = {}
            for e in db.execute(
                select(InterviewEvent).where(InterviewEvent.profile_id == prof.id)
            ).scalars().all():
                have_ev.setdefault((e.kind, e.scheduled_at), e)
            for r in rec.get("Interview_Round") or []:
                kind = round_kind(txt(r.get("Interview_Round")))
                when = parse_dt(r.get("Interview_Date_Time_From"))
                fields = {
                    "raw_when": squash(r.get("Interview_Date_Time_From"))[:64] or None,
                    "meeting_link": (zoho_link(r.get("Interview_Link")) or "")[:1024] or None,
                    "stage": squash(r.get("Stage"))[:120] or None,
                    "mode": squash(r.get("Interview_Mode"))[:60] or None,
                    "status": squash(r.get("Interview_Status"))[:60] or None,
                    "result": squash(r.get("Result"))[:60] or None,
                    "interviewer": (people_text(r.get("Employee"))
                                    or people_text(r.get("External_Interview_Panel"))
                                    or squash(r.get("External_Interviewer")) or None),
                    "feedback": squash(r.get("OverAll_Feedback")) or None,
                    "zoho_round_id": squash((r.get("Interview_Round") or {}).get("id"))[:32] or None,
                    "note": squash(r.get("Venue_Details")) or None,
                    # Snapped to the app's vocabulary so imported rounds open cleanly
                    # in the RMG feedback form instead of showing unknown values.
                    "interview_category": _vocab(r.get("Interviewer_Category"), CATEGORIES),
                    "duration_minutes": duration_minutes(r.get("Interview_Duration")),
                    "user_role": _vocab(r.get("UserRole"), USER_ROLES),
                }
                fields["status"] = _vocab(r.get("Interview_Status"), STATUSES) or fields["status"]
                emp_id = employee_by_name.get(norm(fields["interviewer"]))
                if emp_id:
                    fields["employee_id"] = emp_id
                existing = have_ev.get((kind, when))
                if existing is not None:
                    # Legacy row from the older importer: it packed round/stage/mode/
                    # status/result/feedback into `note`. Promote them to real columns
                    # and drop the redundant note instead of inserting a duplicate.
                    for key, val in fields.items():
                        if val is not None and getattr(existing, key, None) in (None, ""):
                            setattr(existing, key, val[:200] if key == "interviewer" else val)
                    if fields["feedback"] and _note_is_legacy_blob(existing.note, fields["feedback"]):
                        existing.note = fields["note"]
                    stats["events_merged"] += 1
                    continue
                ev = InterviewEvent(
                    profile_id=prof.id, candidate_id=cand.id, kind=kind, scheduled_at=when,
                    created_by=actor_id,
                    **{k: (v[:200] if k == "interviewer" and v else v) for k, v in fields.items()},
                )
                db.add(ev)
                have_ev[(kind, when)] = ev
                stats["events"] += 1

            # ---- skill evaluations -----------------------------------------
            have_sk = {e.skill_id for e in db.execute(
                select(SkillEvaluation).where(SkillEvaluation.profile_id == prof.id)
            ).scalars().all()}
            for s in rec.get("Skill_Evaluation") or []:
                name = txt(s.get("Skill_Name"))
                if not name:
                    continue
                skill = skills_by_name.get(norm(name))
                if skill is None:
                    skill = Skill(name=name[:120], is_active=True)
                    db.add(skill)
                    db.flush()
                    skills_by_name[norm(name)] = skill
                if skill.id in have_sk:
                    continue
                have_sk.add(skill.id)
                db.add(SkillEvaluation(
                    profile_id=prof.id, skill_id=skill.id,
                    self_rated=numf(s.get("Skill_Level_Self_Rating")),
                    reviewer_rated=numf(s.get("RMG_Rating")),
                ))
                stats["evals"] += 1

            # ---- offers ------------------------------------------------------
            have_of = {(o.offer_date, o.ctc) for o in db.execute(
                select(OfferHistory).where(OfferHistory.profile_id == prof.id)
            ).scalars().all()}
            for o in rec.get("Offer_Release_History") or []:
                odate = parse_date(o.get("Offer_Release_Date") or o.get("Offer_Accepted"))
                ctc = lac_to_rupees(o.get("Offer_CTC"))
                if odate is None or ctc is None:
                    continue
                if (odate, ctc) in have_of:
                    continue
                have_of.add((odate, ctc))
                db.add(OfferHistory(
                    profile_id=prof.id, offer_date=odate, ctc=ctc,
                    joining_date=parse_date(o.get("Joining_Date")),
                    expiry_date=parse_date(o.get("Offer_Expire_Date")),
                    acceptance_date=parse_date(o.get("Offer_Accepted")),
                    offer_letter_url=squash(o.get("Offer_Letter"))[:1024] or None,
                    status=OFFER_STATUS_MAP.get(norm(o.get("Status")), OfferStatus.PENDING),
                ))
                stats["offers"] += 1

            # ---- activity history --------------------------------------------
            have_ac = {(l.action_type, (l.comment or "")[:200]) for l in db.execute(
                select(CandidateProfileActivityLog)
                .where(CandidateProfileActivityLog.profile_id == prof.id)
            ).scalars().all()}
            for e in rec.get("Activity_History") or []:
                comment = squash(e.get("Comments"))
                if not comment:
                    continue
                op = squash(e.get("Operation"))
                action = (op or ("Profile added" if comment.lower().startswith("profile added")
                                 else "Comment"))[:64]
                if (action, comment[:200]) in have_ac:
                    continue
                have_ac.add((action, comment[:200]))
                log = CandidateProfileActivityLog(
                    profile_id=prof.id, user_id=actor_id,
                    action_type=action, comment=comment,
                )
                ts = ts_from_comment(comment)
                if ts:
                    log.timestamp = ts
                db.add(log)
                stats["activity"] += 1

        db.flush()

        if replace:
            for _pair, prof in list(profiles.items()):
                if prof.id in kept_ids:
                    continue
                for model in (CandidateProfileActivityLog, InterviewEvent,
                              SkillEvaluation, OfferHistory):
                    for child in db.execute(
                        select(model).where(model.profile_id == prof.id)
                    ).scalars().all():
                        db.delete(child)
                db.execute(text("UPDATE employees SET candidate_profile_id=NULL "
                                "WHERE candidate_profile_id=:p"), {"p": prof.id})
                db.execute(text("DELETE FROM ai_interview_links WHERE profile_id=:p"),
                           {"p": prof.id})
                db.delete(prof)
                stats["deleted"] += 1
            db.flush()

        # ---- skipped / unlinked report --------------------------------------
        rows = skipped_rows + [{**u, "cand_name": u.get("candidate_name")} for u in unlinked]
        if rows:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            cols = ["profile_id", "cand_zoho_id", "cand_name", "cand_email", "opp_id",
                    "opp_title", "customer", "status", "ta_person", "reason"]
            with report_path.open("w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for r in rows:
                    w.writerow({c: r.get(c) for c in cols})

        print("Profiles   : "
              f"{stats['created']:>6,} created   {stats['updated']:>6,} updated   "
              f"{stats['deleted']:>6,} deleted")
        print("Children   : "
              f"{stats['events']:>6,} interview rounds   {stats['evals']:>6,} skill evals   "
              f"{stats['offers']:>6,} offers   {stats['activity']:>6,} activity")
        print("Merged     : "
              f"{stats['events_merged']:>6,} existing rounds upgraded in place (no duplicates)")
        print("TA owners  : "
              f"{stats['ta_matched']:>6,} matched to CRM users")
        print("Skipped    : "
              f"{stats['skipped_no_cand']:>6,} no candidate   "
              f"{stats['skipped_no_opp']:>6,} no opportunity   "
              f"{stats['dupe_pair']:>6,} duplicate (candidate, opportunity) collapsed")
        print(f"Unlinked   : {len(unlinked):>6,} applications with no candidate in Zoho")
        if rows:
            print(f"Report     : {report_path}  ({len(rows):,} rows)")

        if apply:
            db.commit()
            print("\nCOMMITTED.")
        else:
            db.rollback()
            print("\nDRY RUN - nothing written. Re-run with --apply to commit.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
