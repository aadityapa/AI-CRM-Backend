"""One-off Question Bank DB audit script."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from auth_db import init_auth_db, _connect_postgres, _is_postgres
from main import AUTH_DB_TARGET
from services.question_bank.repository import ensure_question_bank_tables

target = AUTH_DB_TARGET
print("DB target:", target)
init_auth_db(target)
ensure_question_bank_tables(target)

if _is_postgres(target):
    with _connect_postgres(str(target)) as conn:
        with conn.cursor() as cur:
            for tbl in ["question_bank", "question_upload_history", "interview_question", "job_templates"]:
                cur.execute(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_name=%s",
                    (tbl,),
                )
                exists = cur.fetchone()[0]
                if exists:
                    cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                    cnt = cur.fetchone()[0]
                else:
                    cnt = "N/A"
                print(f"{tbl}: exists={exists}, rows={cnt}")
            cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='question_bank'")
            print("question_bank indexes:", [r[0] for r in cur.fetchall()])
            cur.execute(
                "SELECT job_id, job_title, question_type FROM job_templates "
                "WHERE job_id='bb0f2be452' OR job_title ILIKE '%Python%Developer%'"
            )
            for r in cur.fetchall():
                print("template:", r)
            cur.execute(
                "SELECT job_id, question_type, weights->'questionBankConfig' AS qb "
                "FROM job_templates WHERE question_type='question_bank' LIMIT 5"
            )
            for r in cur.fetchall():
                print("qb template:", r[0], r[1], r[2])
            cur.execute(
                "SELECT role, skill, difficulty, category, COUNT(*) "
                "FROM question_bank WHERE is_active GROUP BY 1,2,3,4 ORDER BY 5 DESC LIMIT 10"
            )
            print("top question_bank groups:", cur.fetchall())
else:
    import sqlite3

    conn = sqlite3.connect(str(target))
    for tbl in ["question_bank", "question_upload_history", "interview_question", "job_templates"]:
        try:
            cnt = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            print(f"{tbl}: rows={cnt}")
        except Exception as exc:
            print(f"{tbl}: {exc}")
    conn.close()
