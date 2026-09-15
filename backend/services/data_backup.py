"""Full data backup — one ZIP with every selected dataset as Excel / CSV / JSON
plus every attached file (CVs, offer letters, documents, payment proofs …).

Added 14 Sep 2026 (user request: Admin/CEO one-click backup of the whole
Karnex CRM + AI Hiring application, choosing which tabs to include).

Design
------
* A DATASET is what the user sees as a tab ("Customers", "Employees", …). Each
  one is a list of CRM tables (SQLAlchemy metadata) and, for the AI Hiring
  dataset, legacy raw tables read with plain SQL. Dumping whole tables — not
  the API serializers — is what makes the backup COMPLETE: every column,
  every row, including inactive / closed / rejected records.
* Every row that carries a ``/api/crm-files/...`` URL has that file copied
  into ``files/<table>/<record id>/<original name>`` and the row gets a
  ``_files_folder`` column pointing at it, so the data half and the file half
  of the archive can always be joined back.
* Building can take minutes (7k candidates, GBs of CVs) so it runs on a
  background thread; the router polls :func:`job_status` and streams the
  finished ZIP. Only ONE build runs at a time. Finished archives live in
  ``data/backups/`` and the newest :data:`KEEP_LAST` are kept.
* Secrets never leave the database: password hashes / salts / reset tokens /
  API keys / access keys are dropped by :func:`is_redacted_column`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable

import sqlalchemy as sa
from sqlalchemy.orm import Session

from models import Base
from paths import DATA_DIR
from services.crm_common import resolve_crm_file

logger = logging.getLogger("karnex.backup")

BACKUP_DIR = Path(os.getenv("CRM_BACKUP_DIR") or (Path(DATA_DIR) / "backups"))
KEEP_LAST = int(os.getenv("CRM_BACKUP_KEEP") or 3)
FILE_URL_PREFIX = "/api/crm-files/"

# ---------------------------------------------------------------------------
# Dataset registry — the ONLY place that decides "which tab = which tables"
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dataset:
    key: str
    label: str
    description: str
    tables: tuple[str, ...]            # CRM ORM tables (models.Base metadata)
    legacy_tables: tuple[str, ...] = ()  # raw tables of the interview platform


DATASETS: tuple[Dataset, ...] = (
    Dataset("customers", "Customers",
            "Customers, branches, contacts, documents, billing / leave policies, rate cards, bank accounts, receipts",
            ("customers", "customer_branches", "contact_persons", "contact_roles", "customer_documents",
             "customer_billing_policies", "customer_leave_policies", "customer_rate_cards",
             "customer_receipts", "company_bank_accounts", "branch_holiday_years", "holidays",
             "holiday_names", "calendar_years")),
    Dataset("opportunities", "Opportunities & Requirements",
            "Opportunities, CTC slabs, skills, attachments, activity; requirements, job postings, slots, bookings",
            ("opportunities", "opportunity_ctc_slab", "opportunity_skills", "opportunity_attachments",
             "opportunity_activity_log", "requirements", "requirement_skills", "requirement_attachments",
             "requirement_job_postings", "requirement_activity_log", "interview_slots", "slot_bookings",
             "template_requests")),
    Dataset("candidates", "Candidates",
            "Candidate master with education, experience, skills, outreach, resumes (CV files included)",
            ("candidates", "candidate_education", "candidate_experience", "candidate_skills",
             "candidate_outreach", "resumes", "resume_parse_cache")),
    Dataset("profiles", "Candidate Profiles",
            "Pipeline profiles, offers, skill evaluations, interview rounds, AI interview links, activity",
            ("candidate_profiles", "offer_history", "skill_evaluations", "interview_events",
             "ai_interview_links", "candidate_profile_activity_log")),
    Dataset("projects", "Projects & Timesheets",
            "Projects, project employees, rates, leave details, timesheets, entries, attachments, activity",
            ("projects", "project_leave_policies", "project_communication_matrix", "project_employees",
             "project_employee_rates", "project_employee_leave_details", "employee_project_history",
             "timesheets", "timesheet_entries", "timesheet_attachments", "timesheet_activity_log")),
    Dataset("finance", "Finance",
            "Purchase orders, allocations, invoices, lines, payments, revisions, credit notes, TDS, tax rates",
            ("purchase_orders", "po_project_allocations", "po_activity_log", "invoices", "invoice_lines",
             "invoice_payments", "invoice_revisions", "credit_notes", "credit_note_lines",
             "tds_records", "tds_payments", "tax_rates", "financial_years", "currencies")),
    Dataset("employees", "Employees & Leave",
            "Employee directory, education, experience, leave balances, leave ledger, leave applications",
            ("employees", "employee_education", "employee_experience_details", "employee_leave_balances",
             "leave_accrual_events", "leave_applications", "leave_policy_types", "leave_credit_concepts",
             "departments", "designations", "locations", "skills", "document_types")),
    Dataset("users", "Users & Access",
            "CRM users (no passwords), roles, profiles, access templates, action permissions, preferences",
            ("roles", "user_roles", "user_profiles", "access_templates",
             "action_permissions", "user_table_preferences", "user_notify_prefs"),
            # registration_data is only a one-column FK stub in the ORM
            # (models/base.py) — read the real table through the inspector.
            ("registration_data",)),
    Dataset("settings", "Settings & Logs",
            "App settings, notification routes, notifications, email outbox, support tickets",
            ("app_settings", "notification_routes", "notifications", "email_outbox",
             "support_tickets", "support_ticket_messages")),
    Dataset("ai_hiring", "AI Hiring (interview platform)",
            "Interview schedules, interview records & reports, progress, job templates, HR decisions, prompt logs",
            (),
            ("interview_schedule", "interview_records", "interview_progress", "job_templates",
             "hr_candidate_decisions", "does", "ai_prompt_logs", "opportunity_master", "customer_master")),
)
DATASET_BY_KEY: dict[str, Dataset] = {d.key: d for d in DATASETS}

#: Columns that must NEVER be written to a backup, whatever the table.
#: Matched by SUBSTRING so a new `*_token` / `*_secret` column is covered
#: without anyone remembering this list (interview `access_key` +
#: `invite_token` together open a live interview — CLAUDE.md §10 A1/A3).
_REDACTED_SUBSTRINGS = ("password", "secret", "token", "api_key", "access_key", "device_id", "salt")
_REDACTED_COLUMNS = frozenset({"password_hash", "password_salt", "password", "api_key", "secret"})
#: Key/value settings whose VALUE is a credential.
_REDACTED_SETTING_KEYS = ("secret", "token", "password", "api_key", "smtp_pass", "dsn")


def is_redacted_column(name: str) -> bool:
    n = (name or "").lower()
    return n in _REDACTED_COLUMNS or any(sub in n for sub in _REDACTED_SUBSTRINGS)


def _redact_setting_rows(table: str, rows: list[dict]) -> None:
    if table != "app_settings":
        return
    for r in rows:
        key = str(r.get("key") or "").lower()
        if any(k in key for k in _REDACTED_SETTING_KEYS) and r.get("value"):
            r["value"] = "<redacted>"
#: Legacy tables we refuse to dump even if asked (pure secrets / caches).
_LEGACY_DENY = frozenset({"login_data", "password_reset_tokens", "openai_response_cache"})

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def dataset_options() -> list[dict]:
    return [{"key": d.key, "label": d.label, "description": d.description,
             "tables": len(d.tables) + len(d.legacy_tables)} for d in DATASETS]


def resolve_datasets(keys: Iterable[str] | None) -> list[Dataset]:
    """Empty / None / "all" → every dataset. Unknown keys raise ValueError."""
    wanted = [k.strip() for k in (keys or []) if k and k.strip()]
    if not wanted or "all" in wanted:
        return list(DATASETS)
    unknown = [k for k in wanted if k not in DATASET_BY_KEY]
    if unknown:
        raise ValueError(f"Unknown dataset(s): {', '.join(unknown)}")
    return [DATASET_BY_KEY[k] for k in wanted]


# ---------------------------------------------------------------------------
# Row dumping
# ---------------------------------------------------------------------------


def _scalar(value: Any) -> Any:
    """JSON / CSV / Excel-safe representation of one DB value."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "value"):  # Enum
        return value.value
    if isinstance(value, (dict, list)):
        return value            # kept structured for JSON; flattened for CSV / Excel
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


#: The column a date-range backup filters on, first match wins. Tables with
#: none of these (masters, settings, policies) are always included in full —
#: a partial backup with no departments or leave types would be useless.
_DATE_COLUMNS = ("created_at", "entry_date", "invoice_date", "applied_on", "received_date",
                 "scheduled_at", "from_date", "date_of_joining")


def date_column_for(table_name: str) -> str | None:
    orm = Base.metadata.tables.get(table_name)
    if orm is None or table_name == "registration_data":
        return None
    for c in _DATE_COLUMNS:
        if c in orm.columns:
            return c
    return None


def dump_table(db: Session, table_name: str, *,
               date_from: date | None = None, date_to: date | None = None,
               ) -> tuple[list[str], list[dict]]:
    """All rows of one table (ORM or legacy) as plain dicts, secrets removed.

    ``date_from`` / ``date_to`` (inclusive) restrict tables that have a date
    column (see :func:`date_column_for`); every other table is dumped whole.

    Returns ``([], [])`` when the table does not exist in this database (the
    legacy SQLite-era tables are optional on a Postgres deployment) — the
    README lists it as skipped rather than failing the whole backup.
    """
    orm = Base.metadata.tables.get(table_name)
    if orm is not None and table_name != "registration_data":
        cols = [c.name for c in orm.columns if not is_redacted_column(c.name)]
        order = [orm.columns["id"]] if "id" in orm.columns else []
        stmt = sa.select(*[orm.columns[c] for c in cols]).order_by(*order)
        dcol = date_column_for(table_name)
        if dcol and (date_from or date_to):
            col = orm.columns[dcol]
            is_dt = isinstance(col.type, sa.DateTime)
            if date_from:
                stmt = stmt.where(col >= (datetime.combine(date_from, datetime.min.time()) if is_dt else date_from))
            if date_to:
                stmt = stmt.where(col < (datetime.combine(date_to + timedelta(days=1), datetime.min.time())
                                         if is_dt else date_to + timedelta(days=1)))
        rows = [{c: _scalar(v) for c, v in zip(cols, r)} for r in db.execute(stmt).yield_per(2000)]
        _redact_setting_rows(table_name, rows)
        return cols, rows
    if table_name in _LEGACY_DENY or not re.fullmatch(r"[a-z_][a-z0-9_]*", table_name):
        return [], []
    bind = db.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table(table_name):
        return [], []
    cols = [c["name"] for c in insp.get_columns(table_name) if not is_redacted_column(c["name"])]
    if not cols:
        return [], []
    q = bind.dialect.identifier_preparer.quote
    stmt = sa.text(f"SELECT {', '.join(q(c) for c in cols)} FROM {q(table_name)}")
    rows = [{c: _scalar(v) for c, v in zip(cols, r)} for r in db.execute(stmt).yield_per(2000)]
    return cols, rows


# ---------------------------------------------------------------------------
# Attached files
# ---------------------------------------------------------------------------


def _safe(name: str, limit: int = 60) -> str:
    return (_SAFE_NAME.sub("_", name or "").strip("_") or "file")[:limit]


def _record_folder(table: str, row: dict) -> str:
    rid = row.get("id")
    label = row.get("candidate_name") or row.get("full_name") or row.get("name") \
        or row.get("first_name") or row.get("invoice_number") or row.get("po_number") or ""
    tail = f"{rid}_{_safe(str(label))}" if label else str(rid)
    return f"files/{table}/{tail}"


def file_refs_in_row(row: dict) -> list[tuple[str, str]]:
    """``[(column, rel_path)]`` for every ``/api/crm-files/…`` value in the row."""
    out: list[tuple[str, str]] = []
    for col, val in row.items():
        if isinstance(val, str) and val.startswith(FILE_URL_PREFIX):
            out.append((col, val[len(FILE_URL_PREFIX):].split("?", 1)[0]))
    return out


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def _flat(v: Any) -> Any:
    """Cell value for CSV / Excel: JSON objects become text."""
    return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v


_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection: a value starting with = + - @
    is prefixed with a single quote (the CSV convention Excel/Sheets honour).
    Numbers are untouched, so negative amounts survive."""
    v = _flat(v)
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in _FORMULA_LEAD:
        return "'" + v
    return v


def _csv_bytes(cols: list[str], rows: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: _csv_cell(r.get(c)) for c in cols})
    return buf.getvalue().encode("utf-8-sig")


def _json_bytes(rows: list[dict]) -> bytes:
    return json.dumps(rows, ensure_ascii=False, default=str).encode("utf-8")


_EXCEL_CELL_LIMIT = 32_000   # Excel refuses strings above 32,767 chars


def _excel_value(v: Any) -> Any:
    """Excel cell: long text truncated; formula-looking text stays TEXT because
    the write-only sheet receives it as a plain string (openpyxl never
    evaluates), so phone numbers like +91… keep their sign untouched."""
    v = _flat(v)
    if isinstance(v, str) and len(v) > _EXCEL_CELL_LIMIT:
        return v[:_EXCEL_CELL_LIMIT] + "…[truncated]"
    return v


# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------


@dataclass
class BackupJob:
    id: str
    datasets: list[str]
    requested_by: str
    date_from: date | None = None   # inclusive window on dated tables; None = everything
    date_to: date | None = None
    status: str = "queued"          # queued | running | done | failed
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    current: str = ""               # what is being processed right now
    tables_done: int = 0
    tables_total: int = 0
    rows: int = 0
    files: int = 0
    files_missing: int = 0
    bytes_files: int = 0
    error: str | None = None
    archive: Path | None = None
    table_counts: dict[str, int] = field(default_factory=dict)
    skipped_tables: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        size = self.archive.stat().st_size if self.archive and self.archive.exists() else None
        return {
            "id": self.id, "status": self.status, "datasets": self.datasets,
            "requested_by": self.requested_by,
            "date_from": self.date_from.isoformat() if self.date_from else None,
            "date_to": self.date_to.isoformat() if self.date_to else None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "current": self.current, "tables_done": self.tables_done, "tables_total": self.tables_total,
            "percent": round(100 * self.tables_done / self.tables_total) if self.tables_total else 0,
            "rows": self.rows, "files": self.files, "files_missing": self.files_missing,
            "bytes_files": self.bytes_files, "error": self.error,
            "archive_name": self.archive.name if self.archive else None,
            "archive_bytes": size,
            "skipped_tables": list(self.skipped_tables),
        }


_JOBS: dict[str, BackupJob] = {}
_LOCK = threading.Lock()
_RUNNING: str | None = None


def job_status(job_id: str | None = None) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id) if job_id else (
            sorted(_JOBS.values(), key=lambda j: j.started_at)[-1] if _JOBS else None)
    return job.to_dict() if job else None


def list_archives() -> list[dict]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(BACKUP_DIR.glob("karnex-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {}
        try:
            meta = json.loads(p.with_suffix(".json").read_text("utf-8"))
        except Exception:  # noqa: BLE001 — sidecar is optional
            pass
        out.append({"name": p.name, "bytes": p.stat().st_size,
                    "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
                    **{k: meta.get(k) for k in ("datasets", "requested_by", "rows", "files", "date_from", "date_to")}})
    return out


def archive_path(name: str) -> Path | None:
    """Only files we produced, inside BACKUP_DIR — never an arbitrary path."""
    if not re.fullmatch(r"karnex-backup-[0-9A-Za-z_-]+\.zip", name):
        return None
    p = (BACKUP_DIR / name).resolve()
    try:
        p.relative_to(BACKUP_DIR.resolve())
    except ValueError:
        return None
    return p if p.is_file() else None


def _prune(keep: int = KEEP_LAST, out_dir: Path | None = None) -> None:
    out_dir = out_dir or BACKUP_DIR
    archives = sorted(out_dir.glob("karnex-backup-*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in archives[keep:]:
        for p in (old, old.with_suffix(".json")):
            p.unlink(missing_ok=True)
    # leftovers of a crashed process (a live build is at most a few hours old)
    cutoff = datetime.now(timezone.utc).timestamp() - 6 * 3600
    for p in (*out_dir.glob("*.part"), *out_dir.glob("*.xlsx.tmp")):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


def build_archive(db: Session, datasets: list[Dataset], job: BackupJob, *,
                  out_dir: Path | None = None) -> Path:
    """Write the ZIP for ``datasets`` and return its path. Synchronous; the
    router wraps it in a thread. ``job`` is updated in place for progress."""
    from openpyxl import Workbook

    out_dir = out_dir or BACKUP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    final = out_dir / f"karnex-backup-{stamp}-{job.id[:6]}.zip"
    partial = out_dir / f"{final.name}.part"
    xlsx_tmp = out_dir / f"{final.name}.xlsx.tmp"

    plan: list[tuple[Dataset, str]] = [(d, t) for d in datasets for t in (*d.tables, *d.legacy_tables)]
    job.tables_total = len(plan)
    job.status = "running"
    # write_only: rows stream to disk instead of living as cells in memory —
    # a 100k-row dump would otherwise cost hundreds of MB.
    wb = Workbook(write_only=True)
    readme_rows: list[str] = []

    try:
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for ds, table in plan:
                job.current = f"{ds.label} · {table}"
                cols, rows = dump_table(db, table, date_from=job.date_from, date_to=job.date_to)
                if not cols:
                    job.skipped_tables.append(table)
                    job.tables_done += 1
                    continue
                # Attached files: every row is scanned (a `startswith` per
                # cell is cheap); the row gets a pointer to its folder.
                touched = False
                for r in rows:
                    folder = ""
                    for col, rel in file_refs_in_row(r):
                        src = resolve_crm_file(rel)
                        if src is None:
                            job.files_missing += 1
                            continue
                        folder = folder or _record_folder(table, r)
                        zf.write(src, f"{folder}/{_safe(col)}__{_safe(src.name, 120)}")
                        job.files += 1
                        job.bytes_files += src.stat().st_size
                    if folder:
                        r["_files_folder"] = folder
                        touched = True
                if touched:
                    cols = [*cols, "_files_folder"]
                zf.writestr(f"{ds.key}/csv/{table}.csv", _csv_bytes(cols, rows))
                zf.writestr(f"{ds.key}/json/{table}.json", _json_bytes(rows))
                ws = wb.create_sheet(title=table[:31])
                ws.append(cols)
                for r in rows:
                    ws.append([_excel_value(r.get(c)) for c in cols])
                job.table_counts[table] = len(rows)
                job.rows += len(rows)
                job.tables_done += 1
                readme_rows.append(f"  {ds.label:32s} {table:40s} {len(rows):>8d} rows")
                del rows

            job.current = "Excel workbook"
            wb.save(xlsx_tmp)
            zf.write(xlsx_tmp, "karnex-backup.xlsx")

            readme = "\n".join([
                "KARNEX CRM + AI HIRING — FULL DATA BACKUP",
                f"Generated : {datetime.now(timezone.utc).isoformat()} (UTC)",
                f"Requested : {job.requested_by}",
                f"Datasets  : {', '.join(d.label for d in datasets)}",
                (f"Period    : {job.date_from or '…'} to {job.date_to or '…'} (inclusive) — applied to tables "
                 "with a date column; masters, policies and settings are always complete"
                 if (job.date_from or job.date_to) else "Period    : everything (all years)"),
                f"Rows      : {job.rows}    Files: {job.files} ({job.bytes_files / 1e6:.1f} MB)"
                + (f"    Missing files: {job.files_missing}" if job.files_missing else ""),
                "",
                "LAYOUT",
                "  karnex-backup.xlsx            every table as a sheet",
                "  <dataset>/csv/<table>.csv     one CSV per table (UTF-8 with BOM, opens in Excel)",
                "  <dataset>/json/<table>.json   same rows as JSON",
                "  files/<table>/<id>_<name>/    attached files of that record; the row's",
                "                                _files_folder column names this folder",
                "",
                "Secrets (password hashes, salts, tokens, access keys, API keys) are never included.",
                "This archive contains personal data — store it encrypted and restrict access.",
                "",
                "TABLES", *readme_rows,
                *(["", "SKIPPED (not present in this database):", *(f"  {t}" for t in job.skipped_tables)]
                  if job.skipped_tables else []),
                *(["", "DATE FILTER — column used per table:",
                   *(f"  {t:40s} {date_column_for(t) or '(whole table)'}"
                     for _, t in plan if t not in job.skipped_tables)]
                  if (job.date_from or job.date_to) else []),
                "",
            ])
            zf.writestr("README.txt", readme.encode("utf-8"))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        xlsx_tmp.unlink(missing_ok=True)

    partial.replace(final)
    final.with_suffix(".json").write_text(json.dumps({
        "datasets": [d.key for d in datasets], "requested_by": job.requested_by,
        "date_from": job.date_from.isoformat() if job.date_from else None,
        "date_to": job.date_to.isoformat() if job.date_to else None,
        "rows": job.rows, "files": job.files, "files_missing": job.files_missing,
        "table_counts": job.table_counts,
    }), "utf-8")
    _prune(out_dir=out_dir)
    return final


def start_job(datasets: list[Dataset], requested_by: str,
              session_factory: Callable[[], Session], *,
              date_from: date | None = None, date_to: date | None = None) -> BackupJob:
    """Start a background build. Raises RuntimeError when one is already running."""
    global _RUNNING
    if date_from and date_to and date_from > date_to:
        raise ValueError("date_from must be on or before date_to")
    with _LOCK:
        if _RUNNING and _JOBS[_RUNNING].status in ("queued", "running"):
            raise RuntimeError("A backup is already running — wait for it to finish.")
        job = BackupJob(id=uuid.uuid4().hex[:12], datasets=[d.key for d in datasets],
                        requested_by=requested_by, date_from=date_from, date_to=date_to)
        _JOBS[job.id] = job
        _RUNNING = job.id
        # keep the in-memory table small
        for old in sorted(_JOBS.values(), key=lambda j: j.started_at)[:-10]:
            _JOBS.pop(old.id, None)

    def _run() -> None:
        global _RUNNING
        db = session_factory()
        try:
            job.archive = build_archive(db, datasets, job)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI, never raised on a thread
            logger.exception("backup %s failed", job.id)
            job.status = "failed"
            job.error = str(exc)[:500]
        finally:
            job.finished_at = datetime.now(timezone.utc)
            job.current = ""
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
            with _LOCK:
                if _RUNNING == job.id:
                    _RUNNING = None

    threading.Thread(target=_run, name=f"backup-{job.id}", daemon=True).start()
    return job


def free_space_bytes() -> int | None:
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(BACKUP_DIR).free
    except OSError:
        return None
