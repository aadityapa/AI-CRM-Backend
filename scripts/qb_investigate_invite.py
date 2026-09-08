"""Investigate QB invite bootstrap for a scheduled datetime."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from auth_db import _connect_postgres, get_job_template, init_auth_db
from main import (
    AUTH_DB_TARGET,
    _bootstrap_invite_interview_session,
    _coerce_question_type,
    _extract_invite_config_from_notes,
    _questions_from_question_bank,
    _questions_from_question_bank,
    _resolve_invite_job_template,
)
from services.question_bank.repository import count_questions_for_interview, ensure_question_bank_tables
from services.question_bank.selection import bootstrap_question_bank_session, parse_question_bank_config


def find_schedule(target_time: str = "2026-06-24T18:24"):
    init_auth_db(AUTH_DB_TARGET)
    ensure_question_bank_tables(AUTH_DB_TARGET)
    with _connect_postgres(str(AUTH_DB_TARGET)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, candidate_name, candidate_email, scheduled_at_local, invite_token, notes, status
            FROM interview_schedule
            WHERE scheduled_at_local::text LIKE %s OR scheduled_at_local::text LIKE %s
            ORDER BY scheduled_at_local DESC
            LIMIT 5
            """,
            (f"%{target_time}%", f"%2026-06-24%18:24%"),
        )
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]


def investigate(schedule: dict) -> None:
    token = schedule.get("invite_token")
    print("=" * 60)
    print(f"Schedule: {schedule.get('scheduled_at_local')} token={token}")
    print(f"Candidate: {schedule.get('candidate_name')} <{schedule.get('candidate_email')}>")
    invite_cfg = _extract_invite_config_from_notes(str(schedule.get("notes", "")))
    print("invite_cfg:", json.dumps(invite_cfg, indent=2))
    job = _resolve_invite_job_template(invite_cfg)
    if not job:
        print("FAIL: no job template resolved")
        return
    qt = _coerce_question_type(job.get("questionType"))
    print(f"job_id={job.get('jobId')} title={job.get('jobTitle')} questionType={qt} numQ={job.get('numQ')}")
    weights = job.get("weights") if isinstance(job.get("weights"), dict) else {}
    cfg = parse_question_bank_config(weights)
    print("questionBankConfig:", json.dumps(cfg, indent=2))
    role = cfg["role"] or str(job.get("jobTitle") or "").strip()
    skills = cfg["skills"] or (job.get("requiredSkills") or [])
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",") if s.strip()]
    diffs = cfg["difficulties"] or [cfg["difficulty"] or "medium"]
    cats = cfg["categories"] or [cfg["category"] or "technical"]
    pool = count_questions_for_interview(
        AUTH_DB_TARGET,
        role=role,
        skills=list(skills),
        difficulty=diffs,
        category=cats,
        excluded_ids=cfg["excludedQuestionIds"],
    )
    print(f"Strict pool count: {pool}")
    num_q = int(invite_cfg.get("num_q") or job.get("numQ") or 5)
    qs, snap, items = bootstrap_question_bank_session(
        AUTH_DB_TARGET,
        weights=weights,
        job=job,
        num_q=num_q,
        seed="investigate-seed",
        avoid_question_texts=None,
    )
    print(f"bootstrap_question_bank_session: {len(qs)} questions, items={len(items)}")
    boot = _bootstrap_invite_interview_session(token, schedule, fast_only=True)
    if boot.get("error"):
        print("BOOTSTRAP ERROR:", boot["error"])
    else:
        print("BOOTSTRAP OK: questions=", len(boot.get("questions") or []))


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "2026-06-24T18:24"
    schedules = find_schedule(target)
    if not schedules:
        print(f"No schedule found for {target}")
        with get_connection(AUTH_DB_TARGET) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT scheduled_at_local, invite_token, candidate_name
                FROM interview_schedule
                WHERE scheduled_at_local::text LIKE '%2026-06-24%'
                ORDER BY scheduled_at_local DESC LIMIT 15
                """
            )
            print("Recent 2026-06-24 schedules:")
            for r in cur.fetchall():
                print(" ", r)
        sys.exit(1)
    for s in schedules:
        investigate(s)
