#!/usr/bin/env python3
"""Production database validator — schema + data-integrity report.

Run this ON THE SERVER (it needs the live Postgres):

    cd backend
    python scripts/validate_database.py            # full report
    python scripts/validate_database.py --no-data  # schema checks only (fast)

What it checks, in order:
  1. Alembic version — is the DB at the code's expected head?
  2. Missing tables — model tables absent from the DB (the app 500s on these).
  3. Unknown tables — DB tables neither in models nor the known legacy set
     (leftovers from experiments; candidates for cleanup, never auto-dropped).
  4. Column drift — per model table: columns the DB is missing (breaks the
     app) and extra columns the models no longer know (dead weight).
  5. Missing unique constraints — declared in models but absent in the DB
     (duplicates can creep in through that gap).
  6. FK orphans — child rows pointing at parents that no longer exist.
  7. Duplicate data probes — natural keys that SHOULD be unique but have no
     DB constraint (duplicate customers, branches, PO numbers …).

Read-only: this script never writes anything. Exit code 0 = clean/warnings
only, 1 = at least one FAIL.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlalchemy as sa  # noqa: E402

EXPECTED_HEAD = "0079"

# Legacy interview-platform tables (auth_db.py raw SQL) — expected, not drift.
LEGACY_TABLES = {
    "registration_data", "login_data", "interview_records", "interview_progress",
    "interview_schedule", "job_templates", "hr_candidate_decisions",
    "password_reset_tokens", "customer_master", "opportunity_master",
    "alembic_version",
}

# Natural keys that should be unique but historically had no DB constraint.
# (table, columns, human label). Missing tables/columns are skipped silently.
DUPLICATE_PROBES: list[tuple[str, tuple[str, ...], str]] = [
    ("customers", ("name",), "customers with the same name"),
    ("customer_branches", ("customer_id", "branch_name"), "branches duplicated within one customer"),
    ("purchase_orders", ("po_number",), "POs sharing a po_number"),
    ("invoices", ("invoice_number",), "invoices sharing an invoice_number"),
    ("opportunities", ("opp_id",), "opportunities sharing an opp_id"),
    ("requirements", ("req_number",), "requirements sharing a req_number"),
    ("employees", ("official_email",), "employees sharing an official email"),
    ("registration_data", ("username",), "users sharing a username"),
    ("candidate_profiles", ("candidate_id", "opportunity_id"), "same candidate applied twice to one opportunity"),
    ("project_employees", ("project_id", "employee_id"), "employee mapped twice to one project"),
    ("holidays", ("customer_id", "branch_id", "holiday_date", "name"), "identical holidays entered twice"),
]

FAIL, WARN, OK = "FAIL", "WARN", "OK"
_counts = {FAIL: 0, WARN: 0, OK: 0}


def emit(level: str, section: str, msg: str) -> None:
    _counts[level] += 1
    print(f"[{level:4}] {section}: {msg}")


def model_metadata():
    import importlib
    for m in ["base", "rbac", "customers", "opportunities", "projects", "leave",
              "timesheets", "finance", "hr", "candidates", "masters", "requirements",
              "profiles", "resumes", "ai_links", "scheduling", "user_profiles",
              "template_requests", "access_templates"]:
        importlib.import_module(f"models.{m}")
    from models.base import Base
    return Base.metadata


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-data", action="store_true",
                    help="skip row-level checks (orphans, duplicates)")
    args = ap.parse_args()

    from crm_db import get_engine
    engine = get_engine()
    insp = sa.inspect(engine)
    meta = model_metadata()

    live_tables = set(insp.get_table_names())
    model_tables = set(meta.tables.keys())

    print(f"Database: {engine.url.render_as_string(hide_password=True)}")
    print(f"Live tables: {len(live_tables)} | model tables: {len(model_tables)}\n")

    # 1 — alembic head ------------------------------------------------------
    with engine.connect() as conn:
        if "alembic_version" in live_tables:
            ver = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            if ver == EXPECTED_HEAD:
                emit(OK, "migrations", f"DB is at head {ver}")
            else:
                emit(FAIL, "migrations",
                     f"DB at {ver}, code expects {EXPECTED_HEAD} — run: python -m alembic upgrade head")
        else:
            emit(FAIL, "migrations", "no alembic_version table — migrations never ran here")

    # 2 — missing tables ----------------------------------------------------
    missing = sorted(model_tables - live_tables - {"registration_data"})
    if missing:
        for t in missing:
            emit(FAIL, "missing-table", f"'{t}' is in the models but NOT in the DB — the app will error on it")
    else:
        emit(OK, "missing-table", "every model table exists in the DB")

    # 3 — unknown tables ----------------------------------------------------
    unknown = sorted(live_tables - model_tables - LEGACY_TABLES)
    if unknown:
        for t in unknown:
            emit(WARN, "unknown-table",
                 f"'{t}' exists in the DB but neither models nor the legacy platform know it "
                 "(leftover? verify, then drop manually)")
    else:
        emit(OK, "unknown-table", "no stray tables")
    legacy_present = sorted(live_tables & (LEGACY_TABLES - {"alembic_version"}))
    emit(OK, "legacy", f"legacy interview tables present (expected): {', '.join(legacy_present) or 'none'}")

    # 4 — column drift ------------------------------------------------------
    drift = 0
    for t in sorted(model_tables & live_tables):
        want = {c.name for c in meta.tables[t].columns}
        have = {c["name"] for c in insp.get_columns(t)}
        for col in sorted(want - have):
            emit(FAIL, "missing-column", f"{t}.{col} — model expects it, DB lacks it")
            drift += 1
        for col in sorted(have - want):
            emit(WARN, "extra-column", f"{t}.{col} — DB has it, models no longer use it")
            drift += 1
    if not drift:
        emit(OK, "columns", "no column drift on any model table")

    # 5 — missing unique constraints ---------------------------------------
    miss_uq = 0
    for t in sorted(model_tables & live_tables):
        want_uq = {tuple(sorted(c.name for c in uc.columns))
                   for uc in meta.tables[t].constraints
                   if isinstance(uc, sa.UniqueConstraint)}
        if not want_uq:
            continue
        have_uq = {tuple(sorted(uc["column_names"])) for uc in insp.get_unique_constraints(t)}
        # unique indexes also satisfy uniqueness
        have_uq |= {tuple(sorted(ix["column_names"])) for ix in insp.get_indexes(t) if ix.get("unique")}
        for cols in sorted(want_uq - have_uq):
            emit(WARN, "missing-unique", f"{t} lacks unique constraint on ({', '.join(cols)})")
            miss_uq += 1
    if not miss_uq:
        emit(OK, "uniques", "all model-declared unique constraints exist")

    if args.no_data:
        return summary()

    # 6 — FK orphans --------------------------------------------------------
    orphan_hits = 0
    with engine.connect() as conn:
        for t in sorted(model_tables & live_tables):
            for fk in meta.tables[t].foreign_key_constraints:
                if len(fk.columns) != 1:
                    continue
                col = list(fk.columns)[0].name
                ref = fk.referred_table.name
                ref_col = list(fk.elements)[0].column.name
                if ref not in live_tables:
                    continue
                try:
                    n = conn.execute(sa.text(
                        f'SELECT COUNT(*) FROM "{t}" c LEFT JOIN "{ref}" p '
                        f'ON c."{col}" = p."{ref_col}" '
                        f'WHERE c."{col}" IS NOT NULL AND p."{ref_col}" IS NULL'
                    )).scalar() or 0
                except Exception as exc:  # column drift already reported above
                    emit(WARN, "orphan-skip", f"{t}.{col}: check skipped ({type(exc).__name__})")
                    continue
                if n:
                    emit(FAIL, "fk-orphans", f"{n} row(s) in {t}.{col} point at missing {ref}.{ref_col}")
                    orphan_hits += 1
    if not orphan_hits:
        emit(OK, "fk-orphans", "no orphaned foreign keys anywhere")

    # 7 — duplicate data ----------------------------------------------------
    dup_hits = 0
    with engine.connect() as conn:
        for t, cols, label in DUPLICATE_PROBES:
            if t not in live_tables:
                continue
            have = {c["name"] for c in insp.get_columns(t)}
            if not set(cols) <= have:
                continue
            col_list = ", ".join(f'"{c}"' for c in cols)
            try:
                rows = conn.execute(sa.text(
                    f'SELECT {col_list}, COUNT(*) AS n FROM "{t}" '
                    f"GROUP BY {col_list} HAVING COUNT(*) > 1 ORDER BY n DESC LIMIT 5"
                )).fetchall()
            except Exception as exc:
                emit(WARN, "dup-skip", f"{t}: probe skipped ({type(exc).__name__})")
                continue
            if rows:
                sample = "; ".join(str(tuple(r)) for r in rows[:3])
                emit(WARN, "duplicates", f"{label}: {len(rows)}+ groups — e.g. {sample}")
                dup_hits += 1
    if not dup_hits:
        emit(OK, "duplicates", "no duplicate natural keys found")

    return summary()


def summary() -> int:
    print(f"\n===== SUMMARY: {_counts[FAIL]} FAIL / {_counts[WARN]} WARN / {_counts[OK]} OK =====")
    if _counts[FAIL]:
        print("FAILs break the application or the data — fix these first.")
    elif _counts[WARN]:
        print("No breakage. WARNs are cleanup candidates — review before deleting anything.")
    else:
        print("Database matches the code exactly. Production-clean.")
    return 1 if _counts[FAIL] else 0


if __name__ == "__main__":
    sys.exit(main())
