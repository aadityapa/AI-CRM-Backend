"""Karnex backup: database + uploaded files, verified, pruned, logged.

The application had NO backup of any kind. One disk failure would have lost
every customer, timesheet, invoice, purchase order and interview recording.
This script is the answer, and it is deliberately a standalone script rather
than an in-app job: a backup must keep working when the application does not
start.

WHAT IT DOES

1. ``pg_dump`` the Postgres database in CUSTOM format (-Fc). Custom format is
   compressed and restorable table-by-table with ``pg_restore`` — a plain .sql
   dump forces all-or-nothing recovery.
2. Zip the uploads directory (resumes, attachments, PO files). Losing the
   database and keeping the files is only half a recovery — and vice versa.
3. VERIFY the dump before counting it as a success. An unverified backup is a
   guess: ``pg_restore --list`` must parse it and report a non-trivial table
   of contents, otherwise the run is a FAILURE even though a file exists.
4. Prune runs older than the retention window, but NEVER prune unless this
   run verified — otherwise a broken backup could delete the last good one.
5. Write a JSON status file the application reads, so the Settings page can
   say "last good backup: 6 hours ago" and go red when backups stop silently.

USAGE (Windows Task Scheduler runs backup_karnex.bat, which calls this):

    python scripts/backup_karnex.py                 # normal run
    python scripts/backup_karnex.py --verify-only   # check the newest backup
    python scripts/backup_karnex.py --list          # what do we have?

CONFIGURATION (.env, all optional — sane defaults):

    BACKUP_DIR=F:\\KarnexBackups        default: <repo>/../KarnexBackups
    BACKUP_RETENTION_DAYS=30
    BACKUP_UPLOADS=true
    PG_BIN=C:\\Program Files\\PostgreSQL\\16\\bin   if pg_dump is not on PATH

RESTORE (write this on the runbook — an untested restore is not a backup):

    pg_restore --clean --if-exists -d karnex db-YYYYmmdd-HHMMSS.dump
    then unzip uploads-YYYYmmdd-HHMMSS.zip over the uploads folder.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

STATUS_FILE = "backup-status.json"


# ------------------------------------------------------------------ helpers

def repo_root() -> Path:
    here = Path(os.getcwd()).resolve()
    for cand in (here, *here.parents):
        if (cand / "backend" / "main.py").is_file():
            return cand
    print("ERROR: run this from the AI-Interview-Model-B-V2 folder.")
    raise SystemExit(2)


def load_env(root: Path) -> None:
    env = root / ".env"
    if not env.is_file():
        return
    for line in io.open(env, encoding="utf-8", errors="replace").read().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def db_settings() -> dict:
    """Connection details from CRM_DATABASE_URL / AUTH_DB_URL, else DB_* parts."""
    url = (os.getenv("CRM_DATABASE_URL") or os.getenv("AUTH_DB_URL") or "").strip()
    if url:
        # postgresql+psycopg2://user:pass@host:port/name -> parts
        cleaned = url.split("+", 1)[0] + "://" + url.split("://", 1)[1] if "+" in url.split("://")[0] else url
        p = urlparse(cleaned)
        if p.hostname:
            return {
                "host": p.hostname,
                "port": str(p.port or 5432),
                "user": unquote(p.username or ""),
                "password": unquote(p.password or ""),
                "name": (p.path or "/").lstrip("/"),
            }
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("DB_PORT", "5432"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
        "name": os.getenv("DB_NAME", "postgres"),
    }


def pg_tool(name: str) -> str:
    """Find pg_dump / pg_restore: PG_BIN, PATH, then the usual Windows spots."""
    exe = name + (".exe" if os.name == "nt" else "")
    pg_bin = (os.getenv("PG_BIN") or "").strip()
    if pg_bin and (Path(pg_bin) / exe).is_file():
        return str(Path(pg_bin) / exe)
    found = shutil.which(name)
    if found:
        return found
    for base in (r"C:\Program Files\PostgreSQL", r"C:\Program Files (x86)\PostgreSQL"):
        root = Path(base)
        if root.is_dir():
            for version in sorted(root.iterdir(), reverse=True):
                cand = version / "bin" / exe
                if cand.is_file():
                    return str(cand)
    return ""


def backup_dir(root: Path) -> Path:
    configured = (os.getenv("BACKUP_DIR") or "").strip()
    path = Path(configured) if configured else root.parent / "KarnexBackups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def write_status(dest: Path, status: dict) -> None:
    io.open(dest / STATUS_FILE, "w", encoding="utf-8").write(json.dumps(status, indent=1))


# -------------------------------------------------------------------- steps

def dump_database(dest: Path, stamp: str) -> tuple[Path | None, str]:
    tool = pg_tool("pg_dump")
    if not tool:
        return None, "pg_dump not found — set PG_BIN in .env to your PostgreSQL bin folder"
    cfg = db_settings()
    out = dest / f"db-{stamp}.dump"
    env = dict(os.environ)
    if cfg["password"]:
        env["PGPASSWORD"] = cfg["password"]
    cmd = [tool, "-h", cfg["host"], "-p", cfg["port"], "-U", cfg["user"],
           "-d", cfg["name"], "-Fc", "-f", str(out)]
    print(f"  database  {cfg['name']}@{cfg['host']}:{cfg['port']} -> {out.name}")
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    except Exception as exc:
        return None, f"pg_dump failed to start: {exc}"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return None, "pg_dump failed: " + (err[-1] if err else f"exit {proc.returncode}")
    if not out.is_file() or out.stat().st_size < 1024:
        return None, "pg_dump produced an empty file"
    return out, ""


def verify_dump(path: Path) -> tuple[bool, str]:
    """A backup nobody can read is not a backup. pg_restore --list must parse
    it and show a real table of contents."""
    tool = pg_tool("pg_restore")
    if not tool:
        return False, "pg_restore not found — cannot verify"
    try:
        proc = subprocess.run([tool, "--list", str(path)],
                              capture_output=True, text=True, timeout=600)
    except Exception as exc:
        return False, f"verify failed to start: {exc}"
    if proc.returncode != 0:
        return False, "verify failed: " + ((proc.stderr or "").strip().splitlines() or ["unreadable"])[-1]
    entries = [ln for ln in (proc.stdout or "").splitlines() if ln and not ln.startswith(";")]
    if len(entries) < 10:
        return False, f"verify failed: only {len(entries)} objects in the dump"
    return True, f"{len(entries)} objects"


def zip_uploads(root: Path, dest: Path, stamp: str) -> tuple[Path | None, str]:
    if (os.getenv("BACKUP_UPLOADS", "true") or "").strip().lower() in ("0", "false", "no"):
        return None, "skipped by BACKUP_UPLOADS"
    candidates = [
        Path((os.getenv("CRM_UPLOAD_DIR") or "").strip()) if os.getenv("CRM_UPLOAD_DIR") else None,
        root / "backend" / "uploads",
        root / "uploads",
    ]
    src = next((c for c in candidates if c and c.is_dir()), None)
    if src is None:
        return None, "no uploads folder found"
    out = dest / f"uploads-{stamp}.zip"
    count = 0
    print(f"  uploads   {src} -> {out.name}")
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in src.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(src))
                    count += 1
    except Exception as exc:
        return None, f"uploads zip failed: {exc}"
    return out, f"{count} file(s)"


def prune(dest: Path, keep_days: int) -> list[str]:
    """Delete runs older than the window. Only ever called after a VERIFIED
    backup, so a failing run can never delete the last good one."""
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    for path in sorted(dest.glob("*")):
        if path.name == STATUS_FILE or not path.is_file():
            continue
        if not (path.name.startswith("db-") or path.name.startswith("uploads-")):
            continue
        if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
            try:
                path.unlink()
                removed.append(path.name)
            except Exception as exc:
                print(f"  [warn] could not delete {path.name}: {exc}")
    return removed


# --------------------------------------------------------------------- main

def do_list(dest: Path) -> int:
    files = sorted([p for p in dest.glob("db-*.dump")], reverse=True)
    print(f"Backups in {dest}\n")
    if not files:
        print("  (none yet)")
        return 1
    for path in files:
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        up = dest / path.name.replace("db-", "uploads-").replace(".dump", ".zip")
        print(f"  {when}  {human(path.stat().st_size):>10}  {path.name}"
              + (f"  + {up.name}" if up.is_file() else "  (no uploads zip)"))
    status = dest / STATUS_FILE
    if status.is_file():
        print("\nLast status: " + io.open(status, encoding="utf-8").read().strip())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Karnex database + uploads backup")
    ap.add_argument("--list", action="store_true", help="show existing backups and exit")
    ap.add_argument("--verify-only", action="store_true", help="verify the newest dump and exit")
    args = ap.parse_args()

    root = repo_root()
    load_env(root)
    dest = backup_dir(root)

    if args.list:
        return do_list(dest)

    if args.verify_only:
        newest = max(dest.glob("db-*.dump"), key=lambda p: p.stat().st_mtime, default=None)
        if newest is None:
            print("No backup found to verify.")
            return 1
        ok, detail = verify_dump(newest)
        print(f"{newest.name}: {'VERIFIED — ' if ok else 'FAILED — '}{detail}")
        return 0 if ok else 1

    started = datetime.now(timezone.utc)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Karnex backup  {stamp}")
    print(f"Destination: {dest}\n")

    status = {"started_at": started.isoformat(timespec="seconds"), "stamp": stamp}

    dump_path, err = dump_database(dest, stamp)
    if dump_path is None:
        print(f"\n  [FAIL] {err}")
        status.update({"ok": False, "error": err})
        write_status(dest, status)
        return 1
    status["database_file"] = dump_path.name
    status["database_bytes"] = dump_path.stat().st_size

    ok, detail = verify_dump(dump_path)
    print(f"  verify    {'OK — ' if ok else 'FAILED — '}{detail}")
    status["verified"] = ok
    status["verify_detail"] = detail
    if not ok:
        # Keep the file for inspection, but never prune on an unverified run.
        status["ok"] = False
        write_status(dest, status)
        print("\n  [FAIL] backup NOT verified — older backups were left untouched.")
        return 1

    up_path, up_detail = zip_uploads(root, dest, stamp)
    if up_path is not None:
        status["uploads_file"] = up_path.name
        status["uploads_bytes"] = up_path.stat().st_size
        print(f"  uploads   OK — {up_detail}, {human(up_path.stat().st_size)}")
    else:
        print(f"  uploads   {up_detail}")
        status["uploads_note"] = up_detail

    try:
        keep = max(1, int(os.getenv("BACKUP_RETENTION_DAYS", "30") or "30"))
    except ValueError:
        keep = 30
    removed = prune(dest, keep)
    status["pruned"] = len(removed)
    if removed:
        print(f"  prune     removed {len(removed)} file(s) older than {keep} days")

    status["ok"] = True
    status["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    status["duration_sec"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    write_status(dest, status)

    print(f"\n  Database  {human(status['database_bytes'])}")
    print("=" * 60)
    print(f"BACKUP OK — verified, {status['duration_sec']}s. Keeping {keep} days.")
    print(f"Restore:  pg_restore --clean --if-exists -d <db> {dump_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
