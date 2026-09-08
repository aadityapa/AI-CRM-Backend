"""Production cleanup — remove certification / test / load-test artifacts only.

Usage:
  python scripts/production_cleanup.py              # dry-run (list only)
  python scripts/production_cleanup.py --execute    # apply deletions

Never deletes real admin accounts, production templates, or question bank content
unless it matches explicit certification test patterns.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

# ---------------------------------------------------------------------------
# Match patterns from phase2 / phase3 / phase4 / smoke certification scripts
# ---------------------------------------------------------------------------

JOB_ID_SQL_PATTERNS: list[str] = [
    "job_id LIKE 'dyn_p2_%%'",
    "job_id LIKE 'man_p2_%%'",
    "job_id LIKE 'hat_%%'",
    "job_id LIKE 'phase4_cert_%%'",
    "job_id LIKE 'phase4_load_%%'",
    "job_id ~ '^dyn_[0-9]+$'",  # legacy phase2 orphan (e.g. dyn_1783153798)
]

TEST_HR_USERNAME_SQL = """
    username LIKE 'phase2_hr_%%'
    OR username LIKE 'phase4_hr_%%'
    OR username LIKE 'hat_hr_%%'
    OR username LIKE 'smoke_hr_%%'
"""

TEST_HR_USERNAME_SCHEDULE_SQL = """
    hr_username LIKE 'phase2_hr_%%'
    OR hr_username LIKE 'phase4_hr_%%'
    OR hr_username LIKE 'hat_hr_%%'
    OR hr_username LIKE 'smoke_hr_%%'
"""

TEST_EMAIL_SQL = """
    candidate_email LIKE '%%@verify.local'
    OR candidate_email LIKE '%%@cert.local'
    OR candidate_email LIKE '%%@hat.local'
    OR candidate_email LIKE '%%@load.local'
    OR candidate_email LIKE '%%@local.test'
    OR candidate_email = 'smoke@test.local'
"""

TEST_HR_EMAIL_SQL = """
    email LIKE '%%@verify.local'
    OR email LIKE '%%@cert.local'
    OR email LIKE '%%@hat.local'
    OR email LIKE '%%@local.test'
"""

QB_TEST_SQL = """
    question LIKE 'Manual HAT Q%%'
    OR LOWER(question) LIKE '%%lorem ipsum%%'
    OR LOWER(question) LIKE 'test question%%'
    OR question LIKE 'Phase4 Q%%'
    OR question LIKE 'Manual Q%%: Describe your Python experience.%%'
    OR question LIKE 'Manual Q%%: Explain REST API design.%%'
"""

PROTECTED_USERNAMES = {
    u.strip().lower()
    for u in (os.getenv("SUPER_ADMIN_USERNAMES") or "admin").split(",")
    if u.strip()
}


def _db_connect():
    import psycopg2

    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "karnex_db")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    direct = (os.getenv("AUTH_DB_URL") or os.getenv("DATABASE_URL") or "").strip()
    if direct and not os.getenv("USE_LOCAL_DB", "").lower() in {"1", "true", "yes"}:
        return psycopg2.connect(direct), "postgresql"
    return psycopg2.connect(host=host, port=port, dbname=name, user=user, password=password), "postgresql"


def _job_id_where(alias: str = "") -> str:
    col = f"{alias}job_id" if alias else "job_id"
    parts = [p.replace("job_id", col) for p in JOB_ID_SQL_PATTERNS]
    return "(" + " OR ".join(parts) + ")"


def _load_cert_log_ids() -> dict[str, list[str]]:
    """Parse certification JSON logs for explicit IDs (supplementary)."""
    extra: dict[str, list[str]] = {"job_ids": [], "usernames": [], "emails": []}
    for rel in (
        "logs/phase2_runtime_verify.json",
        "logs/phase3_hat_results.json",
        "logs/phase4_production_certification.json",
    ):
        path = ROOT / rel
        if not path.exists():
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            blob = json.dumps(data)
            for m in re.findall(r"hat_[a-z0-9_]+", blob):
                if m not in extra["job_ids"]:
                    extra["job_ids"].append(m)
            for m in re.findall(r"phase4_(?:cert|load|hr)_[0-9]+", blob):
                if m not in extra["job_ids"] and m not in extra["usernames"]:
                    if m.startswith("phase4_hr"):
                        extra["usernames"].append(m)
                    else:
                        extra["job_ids"].append(m)
            for m in re.findall(r"phase2_hr_[0-9]+", blob):
                if m not in extra["usernames"]:
                    extra["usernames"].append(m)
        except Exception:
            pass
    return extra


def _count(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _fetch(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def build_cleanup_plan(cur) -> dict[str, Any]:
    job_where = _job_id_where()
    plan: dict[str, Any] = {"tables": {}, "samples": {}, "protected_skipped": []}

    # Schedules tied to test emails or test HR accounts
    schedule_sql = f"""
        SELECT id, invite_token, candidate_email, hr_username
        FROM interview_schedule
        WHERE ({TEST_EMAIL_SQL}) OR ({TEST_HR_USERNAME_SCHEDULE_SQL})
    """
    schedules = _fetch(cur, schedule_sql)
    schedule_ids = [r[0] for r in schedules]
    invite_tokens = [r[1] for r in schedules if r[1]]
    plan["tables"]["interview_schedule"] = len(schedule_ids)
    plan["samples"]["interview_schedule"] = [
        {"id": r[0], "email": r[2], "hr": r[3]} for r in schedules[:8]
    ]

    # Interview records
    record_sql = f"""
        SELECT id, candidate_email, payload->>'job_id' AS job_id
        FROM interview_records
        WHERE ({TEST_EMAIL_SQL})
           OR ({_job_id_where().replace("job_id", "payload->>'job_id'")})
    """
    records = _fetch(cur, record_sql)
    record_ids = [r[0] for r in records]
    plan["tables"]["interview_records"] = len(record_ids)
    plan["samples"]["interview_records"] = [
        {"id": r[0], "email": r[1], "job_id": r[2]} for r in records[:8]
    ]

    interview_ids = list({*record_ids})

    # Progress rows by invite token or email or interview_id
    if invite_tokens:
        cur.execute(
            "SELECT interview_id FROM interview_progress WHERE invite_token = ANY(%s)",
            (invite_tokens,),
        )
        interview_ids.extend(r[0] for r in cur.fetchall() if r[0])
    progress_email_sql = f"""
        SELECT interview_id FROM interview_progress
        WHERE ({TEST_EMAIL_SQL})
    """
    cur.execute(progress_email_sql)
    interview_ids.extend(r[0] for r in cur.fetchall() if r[0])
    interview_ids = sorted(set(interview_ids))

    plan["tables"]["interview_progress"] = len(interview_ids)
    plan["samples"]["interview_progress"] = interview_ids[:8]

    # Child QB tables
    if interview_ids:
        plan["tables"]["interview_question"] = _count(
            cur,
            "SELECT COUNT(*) FROM interview_question WHERE interview_id = ANY(%s)",
            (interview_ids,),
        )
        ca_ids_sql = "SELECT id FROM candidate_answer WHERE interview_id = ANY(%s)"
        ca_rows = _fetch(cur, ca_ids_sql, (interview_ids,))
        ca_ids = [r[0] for r in ca_rows]
        plan["tables"]["candidate_answer"] = len(ca_ids)
        plan["tables"]["evaluation_result"] = (
            _count(
                cur,
                "SELECT COUNT(*) FROM evaluation_result WHERE candidate_answer_id = ANY(%s)",
                (ca_ids,),
            )
            if ca_ids
            else 0
        )
    else:
        plan["tables"]["interview_question"] = 0
        plan["tables"]["candidate_answer"] = 0
        plan["tables"]["evaluation_result"] = 0

    # Job templates
    jobs = _fetch(cur, f"SELECT job_id, job_title FROM job_templates WHERE {job_where} ORDER BY job_id")
    job_ids = [r[0] for r in jobs]
    plan["tables"]["job_templates"] = len(job_ids)
    plan["samples"]["job_templates"] = [{"job_id": r[0], "title": r[1]} for r in jobs[:8]]

    # Test HR users (never protected admins)
    users = _fetch(
        cur,
        f"""
        SELECT username, email FROM registration_data
        WHERE ({TEST_HR_USERNAME_SQL}) OR ({TEST_HR_EMAIL_SQL})
        ORDER BY username
        """,
    )
    test_users = [(r[0], r[1]) for r in users if r[0].lower() not in PROTECTED_USERNAMES]
    skipped = [r[0] for r in users if r[0].lower() in PROTECTED_USERNAMES]
    plan["protected_skipped"] = skipped
    plan["tables"]["registration_data"] = len(test_users)
    plan["samples"]["registration_data"] = [{"username": u, "email": e} for u, e in test_users[:8]]
    test_usernames = [u for u, _ in test_users]

    plan["tables"]["login_data"] = (
        _count(
            cur,
            "SELECT COUNT(*) FROM login_data WHERE username = ANY(%s)",
            (test_usernames,),
        )
        if test_usernames
        else 0
    )

    # Audit + prompt logs
    tpl_where = _job_id_where().replace("job_id", "template_id")
    plan["tables"]["ai_prompt_logs"] = _count(
        cur,
        f"""
        SELECT COUNT(*) FROM ai_prompt_logs
        WHERE ({tpl_where})
           OR ({TEST_EMAIL_SQL.replace('candidate_email', 'candidate_name')})
           OR candidate_name ILIKE '%%phase4%%'
           OR candidate_name ILIKE '%%load test%%'
           OR candidate_name ILIKE '%%smoke test%%'
           OR candidate_name ILIKE '%%hat %%'
        """,
    )

    if test_usernames:
        plan["tables"]["hr_audit_log"] = _count(
            cur,
            "SELECT COUNT(*) FROM hr_audit_log WHERE actor_username = ANY(%s)",
            (test_usernames,),
        )
    else:
        plan["tables"]["hr_audit_log"] = 0

    plan["tables"]["hr_candidate_decisions"] = (
        _count(
            cur,
            "SELECT COUNT(*) FROM hr_candidate_decisions WHERE candidate_id = ANY(%s)",
            (record_ids,),
        )
        if record_ids
        else 0
    )

    # Question bank test rows only
    qb_rows = _fetch(cur, f"SELECT id, LEFT(question, 80) FROM question_bank WHERE {QB_TEST_SQL} LIMIT 20")
    plan["tables"]["question_bank"] = _count(cur, f"SELECT COUNT(*) FROM question_bank WHERE {QB_TEST_SQL}")
    plan["samples"]["question_bank"] = [{"id": r[0], "question": r[1]} for r in qb_rows[:8]]

    plan["_schedule_ids"] = schedule_ids
    plan["_invite_tokens"] = invite_tokens
    plan["_record_ids"] = record_ids
    plan["_interview_ids"] = interview_ids
    plan["_job_ids"] = job_ids
    plan["_test_usernames"] = test_usernames
    return plan


def execute_cleanup(cur, plan: dict[str, Any]) -> dict[str, int]:
    deleted: dict[str, int] = {}
    interview_ids = plan["_interview_ids"]
    record_ids = plan["_record_ids"]
    job_ids = plan["_job_ids"]
    test_usernames = plan["_test_usernames"]
    invite_tokens = plan["_invite_tokens"]

    if interview_ids:
        cur.execute(
            """
            DELETE FROM evaluation_result
            WHERE candidate_answer_id IN (
                SELECT id FROM candidate_answer WHERE interview_id = ANY(%s)
            )
            """,
            (interview_ids,),
        )
        deleted["evaluation_result"] = cur.rowcount
        cur.execute("DELETE FROM candidate_answer WHERE interview_id = ANY(%s)", (interview_ids,))
        deleted["candidate_answer"] = cur.rowcount
        cur.execute("DELETE FROM interview_question WHERE interview_id = ANY(%s)", (interview_ids,))
        deleted["interview_question"] = cur.rowcount
        cur.execute("DELETE FROM interview_progress WHERE interview_id = ANY(%s)", (interview_ids,))
        deleted["interview_progress_by_id"] = cur.rowcount
    else:
        deleted["evaluation_result"] = deleted["candidate_answer"] = deleted["interview_question"] = 0
        deleted["interview_progress_by_id"] = 0

    if invite_tokens:
        cur.execute("DELETE FROM interview_progress WHERE invite_token = ANY(%s)", (invite_tokens,))
        deleted["interview_progress_by_token"] = cur.rowcount
    else:
        deleted["interview_progress_by_token"] = 0

    if record_ids:
        cur.execute("DELETE FROM hr_candidate_decisions WHERE candidate_id = ANY(%s)", (record_ids,))
        deleted["hr_candidate_decisions"] = cur.rowcount
        cur.execute("DELETE FROM interview_records WHERE id = ANY(%s)", (record_ids,))
        deleted["interview_records"] = cur.rowcount
    else:
        deleted["hr_candidate_decisions"] = deleted["interview_records"] = 0

    schedule_ids = plan["_schedule_ids"]
    if schedule_ids:
        cur.execute("DELETE FROM interview_schedule WHERE id = ANY(%s)", (schedule_ids,))
        deleted["interview_schedule"] = cur.rowcount
    else:
        deleted["interview_schedule"] = 0

    tpl_where = _job_id_where().replace("job_id", "template_id")
    cur.execute(
        f"""
        DELETE FROM ai_prompt_logs
        WHERE ({tpl_where})
           OR ({TEST_EMAIL_SQL.replace('candidate_email', 'candidate_name')})
           OR candidate_name ILIKE '%%phase4%%'
           OR candidate_name ILIKE '%%load test%%'
           OR candidate_name ILIKE '%%smoke test%%'
           OR candidate_name ILIKE '%%hat %%'
        """
    )
    deleted["ai_prompt_logs"] = cur.rowcount

    if test_usernames:
        cur.execute("DELETE FROM hr_audit_log WHERE actor_username = ANY(%s)", (test_usernames,))
        deleted["hr_audit_log"] = cur.rowcount
        cur.execute("DELETE FROM login_data WHERE username = ANY(%s)", (test_usernames,))
        deleted["login_data"] = cur.rowcount
        cur.execute("DELETE FROM registration_data WHERE username = ANY(%s)", (test_usernames,))
        deleted["registration_data"] = cur.rowcount
    else:
        deleted["hr_audit_log"] = deleted["login_data"] = deleted["registration_data"] = 0

    if job_ids:
        cur.execute("DELETE FROM job_templates WHERE job_id = ANY(%s)", (job_ids,))
        deleted["job_templates"] = cur.rowcount
    else:
        deleted["job_templates"] = 0

    cur.execute(f"DELETE FROM question_bank WHERE {QB_TEST_SQL}")
    deleted["question_bank"] = cur.rowcount

    return deleted


def build_orphan_plan(cur) -> dict[str, Any]:
    """Progress rows with no invite_token and no linked interview_record or schedule."""
    rows = _fetch(
        cur,
        """
        SELECT ip.interview_id, ip.candidate_email, ip.status, ip.created_at_ist
        FROM interview_progress ip
        WHERE (ip.invite_token IS NULL OR ip.invite_token = '')
          AND NOT EXISTS (
              SELECT 1 FROM interview_records ir WHERE ir.id = ip.interview_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM interview_schedule s
              WHERE s.candidate_email = ip.candidate_email
                AND COALESCE(ip.candidate_email, '') <> ''
          )
        ORDER BY ip.interview_id
        """,
    )
    interview_ids = [r[0] for r in rows]
    plan: dict[str, Any] = {
        "orphan_interview_progress": len(interview_ids),
        "samples": [
            {"interview_id": r[0], "email": r[1], "status": r[2], "created_at_ist": r[3]}
            for r in rows[:10]
        ],
        "_orphan_interview_ids": interview_ids,
    }
    if interview_ids:
        plan["interview_question"] = _count(
            cur,
            "SELECT COUNT(*) FROM interview_question WHERE interview_id = ANY(%s)",
            (interview_ids,),
        )
        ca_rows = _fetch(cur, "SELECT id FROM candidate_answer WHERE interview_id = ANY(%s)", (interview_ids,))
        ca_ids = [r[0] for r in ca_rows]
        plan["candidate_answer"] = len(ca_ids)
        plan["evaluation_result"] = (
            _count(
                cur,
                "SELECT COUNT(*) FROM evaluation_result WHERE candidate_answer_id = ANY(%s)",
                (ca_ids,),
            )
            if ca_ids
            else 0
        )
    else:
        plan["interview_question"] = plan["candidate_answer"] = plan["evaluation_result"] = 0
    return plan


def execute_orphan_cleanup(cur, plan: dict[str, Any]) -> dict[str, int]:
    deleted: dict[str, int] = {}
    interview_ids = plan.get("_orphan_interview_ids") or []
    if not interview_ids:
        return {
            "evaluation_result": 0,
            "candidate_answer": 0,
            "interview_question": 0,
            "interview_progress_orphan": 0,
        }
    cur.execute(
        """
        DELETE FROM evaluation_result
        WHERE candidate_answer_id IN (
            SELECT id FROM candidate_answer WHERE interview_id = ANY(%s)
        )
        """,
        (interview_ids,),
    )
    deleted["evaluation_result"] = cur.rowcount
    cur.execute("DELETE FROM candidate_answer WHERE interview_id = ANY(%s)", (interview_ids,))
    deleted["candidate_answer"] = cur.rowcount
    cur.execute("DELETE FROM interview_question WHERE interview_id = ANY(%s)", (interview_ids,))
    deleted["interview_question"] = cur.rowcount
    cur.execute("DELETE FROM interview_progress WHERE interview_id = ANY(%s)", (interview_ids,))
    deleted["interview_progress_orphan"] = cur.rowcount
    return deleted


def remove_artifact_files(*, include_prompt_logs: bool = True) -> tuple[list[str], int]:
    removed: list[str] = []
    bytes_freed = 0
    candidates = [
        ROOT / "scripts" / "phase2_runtime_verify.py",
        ROOT / "scripts" / "phase3_hat_verify.py",
        ROOT / "scripts" / "phase4_production_certify.py",
        ROOT / "scripts" / "phase4_api_certify.py",
        ROOT / "scripts" / "phase4_load_test.py",
        ROOT / "scripts" / "qb_e2e_verify.py",
        ROOT / "logs" / "phase2_runtime_verify.json",
        ROOT / "logs" / "phase3_hat_results.json",
        ROOT / "logs" / "phase4_production_certification.json",
        ROOT / "logs" / "phase4_production_certification.md",
        ROOT / "logs" / "phase4_api_certification.json",
        ROOT / "logs" / "phase4_load_test.json",
        ROOT / "logs" / "server-https.log",
        ROOT / "logs" / "server.log",
        ROOT / "logs" / "server-https-verify.log",
        ROOT / "logs" / "server-https-29853.log",
        ROOT / "logs" / "server-test.log",
        ROOT / "logs" / "test-start.log",
        ROOT / "backend" / "logs" / "server-https.log",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            try:
                size = path.stat().st_size
                path.unlink()
                bytes_freed += size
                removed.append(str(path.relative_to(ROOT)))
            except OSError as exc:
                removed.append(f"SKIPPED (locked): {path.relative_to(ROOT)} — {exc}")

    cache_dirs = [
        ROOT / ".pytest_cache",
        ROOT / "backend" / ".pytest_cache",
        Path(r"E:\AI-Interview-Model-F-V2") / ".pytest_cache",
    ]
    for d in cache_dirs:
        if d.exists():
            import shutil

            for p in d.rglob("*"):
                if p.is_file():
                    bytes_freed += p.stat().st_size
            shutil.rmtree(d, ignore_errors=True)
            removed.append(str(d))

    for pycache in list(ROOT.rglob("__pycache__")):
        if "node_modules" in pycache.parts:
            continue
        import shutil

        for p in pycache.rglob("*"):
            if p.is_file():
                bytes_freed += p.stat().st_size
        shutil.rmtree(pycache, ignore_errors=True)
        removed.append(str(pycache.relative_to(ROOT)))

    # Temp / backup artifacts
    for pattern in ("*.tmp", "*.bak", "*.old", "*.cache", ".coverage", "coverage.xml"):
        for path in ROOT.rglob(pattern):
            if "node_modules" in path.parts or ".venv" in path.parts or "venv" in path.parts:
                continue
            if path.is_file():
                try:
                    size = path.stat().st_size
                    path.unlink()
                    bytes_freed += size
                    removed.append(str(path.relative_to(ROOT)))
                except OSError as exc:
                    removed.append(f"SKIPPED: {path.relative_to(ROOT)} — {exc}")

    # File-based OpenAI prompt request logs (DB ai_prompt_logs is canonical in production)
    if include_prompt_logs:
        prompt_dir = ROOT / "logs" / "openai-prompts"
        if prompt_dir.exists():
            import shutil

            for p in prompt_dir.rglob("*"):
                if p.is_file():
                    bytes_freed += p.stat().st_size
            for day_dir in list(prompt_dir.iterdir()):
                if day_dir.is_dir():
                    shutil.rmtree(day_dir, ignore_errors=True)
                    removed.append(str(day_dir.relative_to(ROOT)))
                elif day_dir.is_file():
                    size = day_dir.stat().st_size
                    day_dir.unlink()
                    bytes_freed += size
                    removed.append(str(day_dir.relative_to(ROOT)))

    return removed, bytes_freed


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove certification/test artifacts before production deploy.")
    parser.add_argument("--execute", action="store_true", help="Apply deletions (default is dry-run).")
    parser.add_argument("--skip-files", action="store_true", help="Skip filesystem artifact removal.")
    parser.add_argument(
        "--orphans",
        action="store_true",
        help="Also remove abandoned interview_progress rows (no invite_token, no record, no schedule).",
    )
    parser.add_argument(
        "--orphans-only",
        action="store_true",
        help="Run orphan cleanup only (skip certification pattern cleanup).",
    )
    args = parser.parse_args()

    cert_extra = _load_cert_log_ids()
    conn, backend = _db_connect()
    conn.autocommit = False
    cur = conn.cursor()

    print(f"Database backend: {backend}")
    print(f"Protected usernames: {sorted(PROTECTED_USERNAMES)}")
    if cert_extra["job_ids"]:
        print(f"Cert log supplementary job IDs: {len(cert_extra['job_ids'])}")

    plan: dict[str, Any] = {"tables": {}}
    orphan_plan: dict[str, Any] = {}
    if not args.orphans_only:
        plan = build_cleanup_plan(cur)
        print("\n=== CLEANUP PLAN (dry-run) ===")
        for table, count in sorted(plan["tables"].items()):
            print(f"  {table}: {count}")
        for key, samples in plan.get("samples", {}).items():
            if samples:
                print(f"\n  Sample {key}:")
                for s in samples[:5]:
                    print(f"    {s}")

    if args.orphans or args.orphans_only:
        orphan_plan = build_orphan_plan(cur)
        print("\n=== ORPHAN PLAN (dry-run) ===")
        print(f"  orphan_interview_progress: {orphan_plan['orphan_interview_progress']}")
        for s in orphan_plan.get("samples", []):
            print(f"    {s}")

    deleted: dict[str, int] = {}
    orphan_deleted: dict[str, int] = {}
    if args.execute:
        if not args.orphans_only:
            print("\n=== EXECUTING DB CLEANUP ===")
            deleted = execute_cleanup(cur, plan)
            for table, count in sorted(deleted.items()):
                print(f"  deleted {table}: {count}")
        if args.orphans or args.orphans_only:
            print("\n=== EXECUTING ORPHAN CLEANUP ===")
            orphan_deleted = execute_orphan_cleanup(cur, orphan_plan)
            for table, count in sorted(orphan_deleted.items()):
                print(f"  deleted {table}: {count}")
        conn.commit()
    else:
        conn.rollback()
        print("\nDry-run only — pass --execute to apply DB deletions.")

    files_removed: list[str] = []
    bytes_freed = 0
    if args.execute and not args.skip_files:
        files_removed, bytes_freed = remove_artifact_files()
        print(f"\nRemoved {len(files_removed)} filesystem artifacts (~{bytes_freed // 1024} KB)")

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": "execute" if args.execute else "dry-run",
        "database_backend": backend,
        "plan_counts": plan.get("tables", {}),
        "orphan_plan": {
            k: v for k, v in orphan_plan.items() if not k.startswith("_")
        },
        "deleted_counts": deleted,
        "orphan_deleted_counts": orphan_deleted,
        "samples": plan.get("samples", {}),
        "protected_skipped": plan.get("protected_skipped", []),
        "cert_log_supplement": cert_extra,
        "files_removed": files_removed,
        "disk_bytes_freed_estimate": bytes_freed,
        "manual_review_required": [
            "data/question_bank/sample_questions.csv — sample CSV kept (reference import format).",
            "scripts/smoke_test.py — retained for ops monitoring (referenced in README).",
            "scripts/qb_audit_db.py, qb_investigate_invite.py, profile_admin_apis.py — ops utilities kept.",
        ],
    }
    report_path = ROOT / "logs" / "production_cleanup_report.json"
    report_path.parent.mkdir(exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nReport written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
