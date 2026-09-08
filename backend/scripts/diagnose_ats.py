"""Why is an ATS score lower than expected?

Answers two questions:
  1. Is the OpenAI "eval" key actually working? (the AI half is 40% of the score,
     and it fails silently — that alone can drop a score ~25 points)
  2. What does the deterministic half score for a given resume + JD, and which
     part of the rubric is losing the points?

Usage
    python scripts/diagnose_ats.py                       # key check only
    python scripts/diagnose_ats.py --resume-id 123       # score a stored resume
    python scripts/diagnose_ats.py --resume cv.pdf --jd jd.txt \
        --skills "Embedded,Functional Safety" --exp-min 5 --exp-max 7
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

try:
    from dotenv import load_dotenv
    for candidate in (BACKEND / ".env", BACKEND.parent / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            break
except ImportError:
    pass

OK, BAD, WARN = "  [OK]", "  [FAIL]", "  [WARN]"


def rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------- key check

def check_openai() -> bool:
    rule("1. OpenAI key — powers 40% of every ATS score")
    from openai_client import (  # noqa: PLC2701
        _PURPOSE_ENV, openai_key_configured, openai_key_source,
    )
    from services.resumes import ATS_OPENAI_PURPOSE

    names = _PURPOSE_ENV[ATS_OPENAI_PURPOSE] + _PURPOSE_ENV["eval"] + ("OPENAI_API_KEY",)
    for name in names:
        value = (os.getenv(name) or "").strip()
        # Never print the key itself — a fingerprint is enough to tell two apart.
        state = f"set ({len(value)} chars, {value[:8]}…{value[-4:]})" if value else "not set"
        print(f"  {name:28} {state}")

    if not openai_key_configured(ATS_OPENAI_PURPOSE):
        print(f"{BAD} No usable key for the '{ATS_OPENAI_PURPOSE}' purpose.")
        print("       Set OPENAI_API_KEY_ATS (or OPENAI_API_KEY) in .env.")
        print("       Until then every score is keyword-only and reads ~20-30 low.")
        return False
    print(f"\n{OK} ATS scans will use: {openai_key_source(ATS_OPENAI_PURPOSE)}")
    print("       Now testing it with a live call...")

    try:
        from openai_client import get_openai_client
        res = get_openai_client(ATS_OPENAI_PURPOSE).chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": 'Reply with JSON {"ok":true}'}],
            response_format={"type": "json_object"},
            max_tokens=20,
            timeout=30,
        )
        print(f"{OK} Live call succeeded: {res.choices[0].message.content!r}")
        return True
    except Exception as exc:  # noqa: BLE001 - the whole point is to report it
        print(f"{BAD} Live call FAILED: {type(exc).__name__}: {exc}")
        print("       This is why scores look low. Common causes:")
        print("         * key revoked or belongs to a different org/project")
        print("         * no billing credit on the OpenAI account")
        print("         * corporate proxy/firewall blocking api.openai.com")
        return False


# ------------------------------------------------------------- score a case

def _read_text(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    from services.resumes import extract_text_from_file
    return extract_text_from_file(str(path))


def explain(resume_text: str, jd_text: str, mandatory: list[str], optional: list[str],
            exp_min, exp_max, location) -> None:
    from services.ats_scoring import extract_jd_keywords, score_resume_against_requirement

    rule("2. Deterministic score")
    result = score_resume_against_requirement(
        resume_text, mandatory, optional, exp_min, exp_max, location, jd_text=jd_text,
    )
    b = result["breakdown"]
    d = b.get("score_details", {})

    print(f"  Score                 {result['ats_score']} / 100")
    print(f"  Required skills       {d.get('mandatory_matched')} / {d.get('mandatory_total')}")
    print(f"  JD keywords           {d.get('jd_keywords_matched')} / {d.get('jd_keywords_total')}")
    print(f"  Experience detected   {d.get('detected_experience_years')} "
          f"(band {exp_min}-{exp_max})")

    rule("3. Where the points went")
    for label, key in (("matched skills", "skills_matched"),
                       ("MISSING skills", "skills_missing"),
                       ("JD words found", "jd_keywords_matched"),
                       ("JD words MISSING", "jd_keywords_missing")):
        items = b.get(key) or []
        shown = ", ".join(str(i) for i in items[:12]) or "—"
        more = f"  (+{len(items) - 12} more)" if len(items) > 12 else ""
        print(f"  {label:18} {shown}{more}")

    rule("4. Sanity check on the JD keywords")
    kws = extract_jd_keywords(jd_text) if jd_text else []
    print(f"  {len(kws)} keywords extracted: {', '.join(kws[:25]) or '—'}")
    print("  If any of these are boilerplate (job, title, role, location, a company")
    print("  name, a city) they should not be scored — report them and they can be")
    print("  added to _JD_BOILERPLATE in services/ats_scoring.py.")


def from_db(resume_id: int) -> None:
    from db import SessionLocal
    from models import Requirement, RequirementSkill, Resume, Skill
    from services.resumes import extract_text_from_file, resolve_crm_file

    with SessionLocal() as db:
        resume = db.get(Resume, resume_id)
        if not resume:
            print(f"{BAD} No resume with id {resume_id}")
            return
        req = db.get(Requirement, resume.requirement_id)
        if not req:
            print(f"{BAD} Resume {resume_id} has no requirement")
            return
        rows = (
            db.query(RequirementSkill, Skill)
            .join(Skill, Skill.id == RequirementSkill.skill_id)
            .filter(RequirementSkill.requirement_id == req.id)
            .all()
        )
        mandatory = [s.name for rs, s in rows if getattr(rs, "is_mandatory", True)]
        optional = [s.name for rs, s in rows if not getattr(rs, "is_mandatory", True)]
        text = extract_text_from_file(resolve_crm_file(resume.file_url))
        print(f"  Resume  #{resume.id}  {resume.file_url}")
        print(f"  Req     #{req.id}  {getattr(req, 'title', '')}")
        print(f"  Stored ats_score: {resume.ats_score}")
        explain(text, getattr(req, "job_description", "") or "", mandatory, optional,
                getattr(req, "experience_min", None), getattr(req, "experience_max", None),
                None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--resume-id", type=int, help="score a resume already in the DB")
    ap.add_argument("--resume", type=Path, help="resume file (pdf/docx/txt)")
    ap.add_argument("--jd", type=Path, help="job description file")
    ap.add_argument("--skills", default="", help="comma-separated required skills")
    ap.add_argument("--optional", default="", help="comma-separated optional skills")
    ap.add_argument("--exp-min", type=float)
    ap.add_argument("--exp-max", type=float)
    ap.add_argument("--location")
    ap.add_argument("--skip-openai", action="store_true")
    args = ap.parse_args()

    print("=" * 68)
    print(" ATS diagnosis")
    print("=" * 68)

    ai_ok = True
    if not args.skip_openai:
        ai_ok = check_openai()

    if args.resume_id:
        from_db(args.resume_id)
    elif args.resume:
        if not args.resume.exists():
            print(f"{BAD} {args.resume} not found")
            return 1
        jd = _read_text(args.jd) if args.jd and args.jd.exists() else ""
        explain(
            _read_text(args.resume), jd,
            [s.strip() for s in args.skills.split(",") if s.strip()],
            [s.strip() for s in args.optional.split(",") if s.strip()],
            args.exp_min, args.exp_max, args.location,
        )
    else:
        print("\n  (no resume given — key check only; pass --resume-id or --resume)")

    rule("Verdict")
    if ai_ok:
        print(f"{OK} Scores are the full 60% keyword + 40% AI blend.")
    else:
        print(f"{WARN} Scores are keyword-only. Expect them ~20-30 points below")
        print("       what a human or ChatGPT would give. Fix the key above first.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
