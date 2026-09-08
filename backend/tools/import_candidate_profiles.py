r"""Import Candidate Profiles (the candidate <-> opportunity relation) from the
Zoho TA export, plus activity history, interview rounds, skill evaluations and
offer history.

Source: import_templates\ta_all_candidate_profiles.ndjson (one JSON per line).

Run AFTER import_opportunities_full.py and import_candidates_full.py.

Usage (from the backend folder, same venv as the app):

    python tools\import_candidate_profiles.py                    # DRY RUN
    python tools\import_candidate_profiles.py --apply            # write
    python tools\import_candidate_profiles.py --replace --apply  # + drop profiles not in the file
    python tools\import_candidate_profiles.py --user admin --apply

What it maps:
  * Candidate: Candidate.id (Zoho record id) -> candidates CSV zohoRecId ->
    Email -> the app's candidate. Falls back to the profile's own Email field,
    then to an exact full-name match.
  * Opportunity: Opportunity_ID (e.g. C-2026-00068) -> the app's opportunity.
  * Profile: pipeline status (Zoho status -> app PipelineStatus), current CTC
    (Current_CTC_Lac), expected CTC, hike %, commercial approval, approved CTC.
  * Activity_History -> candidate_profile_activity_log (action type from the
    Zoho Operation or derived from the comment; timestamp parsed out of the
    comment text when present, e.g. "Profile added by x@ 26-Jun-2026 17:23:31").
  * Interview_Round -> interview_events (L1/L2/L3 round, scheduled date/time,
    meeting link, result + feedback in the note).
  * Skill_Evaluation -> skill_evaluations (skill upserted into the master).
  * Offer_Release_History -> offer_history (CTC, release/accept/expiry dates,
    joining date, status).

Rows whose candidate or opportunity cannot be resolved are reported and skipped
— nothing is guessed. DRY RUN by default; nothing is written without --apply.
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
    Candidate, CandidateProfile, CandidateProfileActivityLog, InterviewEvent,
    OfferHistory, OfferStatus, Opportunity, PipelineStatus, Skill, SkillEvaluation,
)

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "import_templates"

STATUS_MAP = {
    "sourcing": PipelineStatus.SOURCING,
    "technical screening": PipelineStatus.TECHNICAL_SCREENING,
    "technical interviewing": PipelineStatus.TECHNICAL_SCREENING,
    "l1-technical schedhuled": PipelineStatus.TECHNICAL_SCREENING,
    "hr screening": PipelineStatus.TECHNICAL_SCREENING,
    "rmg review": PipelineStatus.RMG_REVIEW,
    "sales screening": PipelineStatus.SALES_SCREENING,
    "ta need to calrify-sales": PipelineStatus.SALES_SCREENING,
    "customer screening": PipelineStatus.CUSTOMER_SCREENING,
    "customer interviewing": PipelineStatus.CUSTOMER_INTERVIEW,
    "customer interview": PipelineStatus.CUSTOMER_INTERVIEW,
    "shortlisted": PipelineStatus.SHORTLISTED,
    "hold": PipelineStatus.SHORTLISTED,
    "customer approval pending": PipelineStatus.CUSTOMER_APPROVAL,
    "customer approval": PipelineStatus.CUSTOMER_APPROVAL,
    "project allocation pending": PipelineStatus.PREBOARDING,
    "preboarding": PipelineStatus.PREBOARDING,
    "joined": PipelineStatus.JOINED,
    "sales rejected": PipelineStatus.SALES_REJECTED,
    "rmg rejected": PipelineStatus.RMG_REJECTED,
    "customer rejected": PipelineStatus.CUSTOMER_REJECTED,
    "self withdrawn": PipelineStatus.SELF_WITHDRAWN,
    "self withdrawal": PipelineStatus.SELF_WITHDRAWN,
    "rejected": PipelineStatus.REJECTED,
}
OFFER_STATUS_MAP = {
    "offer accepted": OfferStatus.ACCEPTED,
    "accepted": OfferStatus.ACCEPTED,
    "offer rejected": OfferStatus.REJECTED,
    "rejected": OfferStatus.REJECTED,
    "expired": OfferStatus.EXPIRED,
}
TS_RE = re.compile(r"(\d{2}-[A-Za-z]{3}-\d{4})[ T](\d{2}:\d{2}(?::\d{2})?)")
SALUTATIONS = {"mr", "mrs", "ms", "miss", "dr", "prof"}


def squash(s) -> str:
    return " ".join(str(s or "").split())


def norm(s) -> str:
    return squash(s).lower()


def txt(v):
    """Zoho lookup field -> display text."""
    if isinstance(v, dict):
        return squash(v.get("text"))
    return squash(v)


def numf(v):
    v = squash(v)
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def lac_to_rupees(v):
    x = numf(v)
    if x is None or x <= 0:
        return None
    return round(x * 100000, 2) if x <= 500 else round(x, 2)


def parse_dt(raw):
    raw = squash(raw)
    if not raw:
        return None
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_date(raw):
    dt = parse_dt(raw)
    return dt.date() if dt else None


def ts_from_comment(comment: str):
    m = TS_RE.search(comment or "")
    if not m:
        return None
    return parse_dt(f"{m.group(1)} {m.group(2)}")


def action_from_entry(entry: dict) -> str:
    op = squash(entry.get("Operation"))
    if op:
        return op[:64]
    c = squash(entry.get("Comments"))
    low = c.lower()
    if low.startswith("profile added"):
        return "Profile added"
    if low.startswith("new record added"):
        return "Created"
    if "status changed to" in low:
        return "Status changed"
    return "Comment"


def strip_name(raw: str) -> str:
    t = squash(raw).strip(" .,")
    parts = [p for p in t.split(" ") if p.strip(".")]
    if parts and parts[0].strip(".").lower() in SALUTATIONS:
        parts = parts[1:]
    return " ".join(parts)


def slugify(*parts) -> str:
    s = re.sub(r"[^a-z0-9.]+", "", ".".join(p for p in parts if p).lower())
    return s.strip(".") or "candidate"


def load_candidate_csv(path: Path) -> dict[str, dict]:
    """zohoRecId -> {email, name} from the candidates export (bridges Zoho ids
    to the app's candidates, which are keyed by email)."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            zid = squash(r.get("zohoRecId"))
            if zid:
                out[zid] = {"email": squash(r.get("Email")), "name": squash(r.get("Name"))}
    return out


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

    user_arg = take_flag("--user")
    paths = [a for a in argv if not a.startswith("--")]
    src = Path(paths[0]) if paths else TEMPLATES / "ta_all_candidate_profiles.ndjson"
    if not src.exists():
        print(f"NDJSON not found: {src}")
        return 2

    recs = []
    with src.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                recs.append(json.loads(line))
    print(f"Profile records: {len(recs)}  (mode: {'APPLY' if apply else 'DRY RUN'})")

    zoho_cands = load_candidate_csv(TEMPLATES / "all_candidates_clean.csv")
    print(f"Candidate id bridge: {len(zoho_cands)} rows from all_candidates_clean.csv")

    db = get_session_factory()()
    created = updated = skipped = deleted = 0
    acts = ivs = skills_n = offers_n = 0
    no_cand = no_opp = 0
    try:
        if user_arg:
            urow = db.execute(text(
                "SELECT id, username FROM registration_data "
                "WHERE lower(username) = :u OR lower(email) = :u ORDER BY id LIMIT 1"
            ), {"u": user_arg.strip().lower()}).first()
        else:
            urow = db.execute(text(
                "SELECT id, username FROM registration_data ORDER BY id LIMIT 1")).first()
        if urow is None:
            print("No user found in registration_data - pass --user <username-or-email>.")
            return 2
        user_id, username = urow[0], urow[1]
        print(f"activity/user attribution: {username} (id {user_id})")

        cands_by_email: dict[str, Candidate] = {}
        cands_by_name: dict[str, list[Candidate]] = {}
        for c in db.execute(select(Candidate)).scalars().all():
            if c.email:
                cands_by_email[norm(c.email)] = c
            full = " ".join(x for x in (c.first_name, c.middle_name, c.last_name) if x)
            cands_by_name.setdefault(norm(full), []).append(c)
        opps = {o.opp_id: o for o in db.execute(select(Opportunity)).scalars().all()}
        skills_by_name = {norm(s.name): s for s in db.execute(select(Skill)).scalars().all()}
        profiles = {
            (p.candidate_id, p.opportunity_id): p
            for p in db.execute(select(CandidateProfile)).scalars().all()
        }

        def resolve_candidate(rec: dict):
            c = rec.get("Candidate") or {}
            zid = squash(c.get("id"))
            bridged = zoho_cands.get(zid) if zid else None
            if bridged and bridged["email"]:
                hit = cands_by_email.get(norm(bridged["email"]))
                if hit:
                    return hit
            email = txt(rec.get("Email"))
            if email:
                hit = cands_by_email.get(norm(email))
                if hit:
                    return hit
            # candidate imported without an email -> deterministic placeholder
            name_src = (bridged or {}).get("name") or txt(c)
            nm = strip_name(name_src)
            if nm and zid:
                toks = nm.split(" ")
                first, last = toks[0], (toks[-1] if len(toks) > 1 else None)
                placeholder = (f"{slugify(first, last)}."
                               f"{hashlib.sha256(zid.encode()).hexdigest()[:8]}@import.karnex.in")
                hit = cands_by_email.get(norm(placeholder))
                if hit:
                    return hit
            if nm:
                hits = cands_by_name.get(norm(nm), [])
                if len(hits) == 1:
                    return hits[0]
            return None

        seen_pairs: set[tuple[int, int]] = set()
        for rec in recs:
            opp_id = txt(rec.get("Opportunity_ID"))
            opp = opps.get(opp_id) if opp_id else None
            cand = resolve_candidate(rec)
            if cand is None:
                no_cand += 1
                skipped += 1
                continue
            if opp is None:
                no_opp += 1
                skipped += 1
                continue

            status = STATUS_MAP.get(norm(rec.get("Candidate_Status")), PipelineStatus.SOURCING)
            fields = {
                "current_ctc": lac_to_rupees(rec.get("Current_CTC_Lac")),
                "expected_ctc": lac_to_rupees(rec.get("Expected_CTC")),
                "hike_percent": numf(rec.get("Hike_Expected")),
                "pipeline_status": status,
                "commercial_approved": norm(rec.get("Commercial_Approval_Status")) in ("approved", "true"),
                "ctc_approval_amount": lac_to_rupees(
                    rec.get("CTC_Approved_Lac") or rec.get("CTC_Approval_Lac") or rec.get("Approved_CTC")),
            }
            key = (cand.id, opp.id)
            prof = profiles.get(key)
            if prof is None:
                prof = CandidateProfile(candidate_id=cand.id, opportunity_id=opp.id, **fields)
                db.add(prof)
                db.flush()
                profiles[key] = prof
                created += 1
            else:
                for f, v in fields.items():
                    if v is not None:
                        setattr(prof, f, v)
                updated += 1
            seen_pairs.add(key)

            # ---- activity history ------------------------------------------
            have = {
                (a.action_type, (a.comment or "")[:200])
                for a in db.execute(
                    select(CandidateProfileActivityLog)
                    .where(CandidateProfileActivityLog.profile_id == prof.id)
                ).scalars().all()
            }
            for e in rec.get("Activity_History") or []:
                comment = squash(e.get("Comments"))
                action = action_from_entry(e)
                if (action, comment[:200]) in have:
                    continue
                row = CandidateProfileActivityLog(
                    profile_id=prof.id, user_id=user_id,
                    action_type=action, comment=comment or None,
                )
                ts = ts_from_comment(comment) or parse_dt(rec.get("Created_Date"))
                if ts:
                    row.timestamp = ts
                db.add(row)
                have.add((action, comment[:200]))
                acts += 1

            # ---- interview rounds ------------------------------------------
            existing_iv = {
                (iv.kind, iv.scheduled_at)
                for iv in db.execute(
                    select(InterviewEvent).where(InterviewEvent.profile_id == prof.id)
                ).scalars().all()
            }
            for e in rec.get("Interview_Round") or []:
                rnd = txt(e.get("Interview_Round")) or "Interview"
                kind = ("L1_Interview" if "l1" in rnd.lower()
                        else "L2_F2F" if "l2" in rnd.lower()
                        else "L3_Interview" if "l3" in rnd.lower()
                        else "Other")[:32]
                when = parse_dt(e.get("Interview_Date_Time_From"))
                if (kind, when) in existing_iv:
                    continue
                link = e.get("Interview_Link")
                if isinstance(link, dict):
                    link = link.get("zcurl") or link.get("zclnk")
                note_bits = [
                    f"Round: {rnd}",
                    f"Stage: {squash(e.get('Stage'))}" if e.get("Stage") else "",
                    f"Mode: {squash(e.get('Interview_Mode'))}" if e.get("Interview_Mode") else "",
                    f"Status: {squash(e.get('Interview_Status'))}" if e.get("Interview_Status") else "",
                    f"Result: {squash(e.get('Result'))}" if e.get("Result") else "",
                    squash(e.get("OverAll_Feedback")),
                ]
                db.add(InterviewEvent(
                    profile_id=prof.id, candidate_id=cand.id, kind=kind,
                    scheduled_at=when,
                    raw_when=squash(e.get("Interview_Date_Time_From"))[:64] or None,
                    meeting_link=(squash(link)[:1024] or None),
                    note="\n".join(b for b in note_bits if b) or None,
                    created_by=user_id,
                ))
                existing_iv.add((kind, when))
                ivs += 1

            # ---- skill evaluations -----------------------------------------
            have_sk = {
                se.skill_id for se in db.execute(
                    select(SkillEvaluation).where(SkillEvaluation.profile_id == prof.id)
                ).scalars().all()
            }
            for e in rec.get("Skill_Evaluation") or []:
                sname = txt(e.get("Skill_Name"))
                if not sname:
                    continue
                sk = skills_by_name.get(norm(sname))
                if sk is None:
                    sk = Skill(name=sname[:120], is_active=True)
                    db.add(sk)
                    db.flush()
                    skills_by_name[norm(sname)] = sk
                if sk.id in have_sk:
                    continue
                db.add(SkillEvaluation(
                    profile_id=prof.id, skill_id=sk.id,
                    self_rated=int(numf(e.get("Skill_Level_Self_Rating")) or 0) or None,
                    reviewer_rated=int(numf(e.get("RMG_Rating")) or 0) or None,
                ))
                have_sk.add(sk.id)
                skills_n += 1

            # ---- offer history ---------------------------------------------
            have_off = {
                (o.offer_date, o.ctc) for o in db.execute(
                    select(OfferHistory).where(OfferHistory.profile_id == prof.id)
                ).scalars().all()
            }
            for e in rec.get("Offer_Release_History") or []:
                odate = parse_date(e.get("Offer_Release_Date")) or parse_date(e.get("Offer_Accepted"))
                ctc = lac_to_rupees(e.get("Offer_CTC"))
                if odate is None or ctc is None:
                    continue
                if (odate, ctc) in have_off:
                    continue
                db.add(OfferHistory(
                    profile_id=prof.id, offer_date=odate, ctc=ctc,
                    joining_date=parse_date(e.get("Joining_Date")),
                    acceptance_date=parse_date(e.get("Offer_Accepted")),
                    expiry_date=parse_date(e.get("Offer_Expire_Date")),
                    status=OFFER_STATUS_MAP.get(norm(e.get("Status")), OfferStatus.PENDING),
                ))
                have_off.add((odate, ctc))
                offers_n += 1

        # ---- replace: drop profiles not present in the file -----------------
        if replace:
            for key, prof in list(profiles.items()):
                if key in seen_pairs:
                    continue
                db.execute(CandidateProfileActivityLog.__table__.delete()
                           .where(CandidateProfileActivityLog.profile_id == prof.id))
                db.execute(InterviewEvent.__table__.delete()
                           .where(InterviewEvent.profile_id == prof.id))
                db.execute(SkillEvaluation.__table__.delete()
                           .where(SkillEvaluation.profile_id == prof.id))
                db.execute(OfferHistory.__table__.delete()
                           .where(OfferHistory.profile_id == prof.id))
                db.flush()
                db.delete(prof)
                deleted += 1
            db.flush()

        print(f"\nPROFILES: {created} created, {updated} updated, {skipped} skipped"
              + (f", {deleted} deleted" if replace else ""))
        print(f"  candidate not resolved: {no_cand} | opportunity not found: {no_opp}")
        print(f"ACTIVITY entries: {acts}")
        print(f"INTERVIEW rounds: {ivs}")
        print(f"SKILL evaluations: {skills_n}")
        print(f"OFFERS: {offers_n}")

        if apply:
            db.commit()
            print("\nAPPLIED - candidate profiles imported.")
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
