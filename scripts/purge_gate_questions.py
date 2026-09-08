"""Remove synthetic gate-verify rows from question_bank (safe for production DB)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import main as karnex_main
from auth_db import init_auth_db
from services.question_bank.repository import ensure_question_bank_tables, purge_synthetic_gate_questions


def main() -> int:
    init_auth_db(karnex_main.AUTH_DB_TARGET)
    ensure_question_bank_tables(karnex_main.AUTH_DB_TARGET)
    removed = purge_synthetic_gate_questions(karnex_main.AUTH_DB_TARGET)
    print(f"Removed {removed} synthetic gate-verify question(s) from question_bank.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
