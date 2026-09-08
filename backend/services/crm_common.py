"""Shared CRM service helpers: pagination, activity logs, sequence numbers, CSV, file storage."""
from __future__ import annotations

import csv
import io
import os
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from fastapi import HTTPException, UploadFile
from sqlalchemy import Select, func, select
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm import Session

from paths import DATA_DIR  # existing helper for the repo's data/ directory

CRM_UPLOAD_DIR = Path(os.getenv("CRM_UPLOAD_DIR") or (Path(DATA_DIR) / "crm_uploads"))


def to_dict(instance) -> dict:
    """Serialize a SQLAlchemy model instance to a JSON-friendly dict.

    Only mapped columns are included; datetimes/dates become ISO strings and
    Decimals become floats so the result is directly serializable by FastAPI.
    """
    if instance is None:
        return {}
    result: dict = {}
    for column in sa_inspect(instance).mapper.column_attrs:
        value = getattr(instance, column.key)
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        elif isinstance(value, Decimal):
            value = float(value)
        result[column.key] = value
    return result


def paginate(db: Session, stmt: Select, page: int, limit: int) -> tuple[list, dict]:
    # Fetch one extra row: if page 1 comes back short, the count is simply the
    # number of rows we already have and the COUNT(*) can be skipped entirely.
    # That COUNT wrapped the full filtered statement (joins and all) and ran on
    # EVERY list request — on an 8k-row join it cost as much as the page itself.
    rows = db.execute(stmt.offset((page - 1) * limit).limit(limit + 1)).scalars().all()
    has_more = len(rows) > limit
    items = rows[:limit]
    if page == 1 and not has_more:
        total = len(items)
    else:
        total = db.execute(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        ).scalar() or 0
    pages = (total + limit - 1) // limit if limit else 1
    return items, {"page": page, "limit": limit, "total": total, "pages": pages}


def system_user_id(db: Session) -> int | None:
    """Lowest-id platform user, used to attribute automatic (non-human) activity.

    Activity logs have a NOT NULL user_id FK to registration_data. Automated
    self-heals have no acting user — attributing them to a *candidate* id (a
    different table) corrupts the audit trail and can violate the FK, so use
    this instead.
    """
    from sqlalchemy import text as _text

    row = db.execute(_text("SELECT id FROM registration_data ORDER BY id LIMIT 1")).first()
    return int(row[0]) if row else None


def log_activity(db: Session, log_model, fk_field: str, entity_id: int, user_id: int,
                 action_type: str, comment: str | None = None) -> None:
    """Write one row to an *_activity_log table. Caller commits.

    ``user_id`` must be a registration_data id. When the caller has no acting
    user (background/self-heal paths) it falls back to the system user; if the
    platform has no users at all the entry is skipped rather than crashing the
    request it is piggybacking on.
    """
    if not user_id:
        user_id = system_user_id(db)
        if not user_id:
            return
    db.add(log_model(**{fk_field: entity_id}, user_id=user_id,
                     action_type=action_type, comment=comment))


def next_sequence_number(db: Session, model, column, prefix: str) -> str:
    """Generate PREFIX-YYYY-NNN (e.g. REQ-2026-001), scanning existing max for this year."""
    year = datetime.now().year
    like = f"{prefix}-{year}-%"
    values = db.execute(select(column).where(column.like(like))).scalars().all()
    max_n = 0
    for v in values:
        m = re.match(rf"{re.escape(prefix)}-{year}-(\d+)$", str(v))
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"{prefix}-{year}-{max_n + 1:03d}"



#: Extensions we are willing to store. Anything else is refused rather than
#: written to disk with an attacker-chosen extension — an uploaded .html or .svg
#: served back from our own origin is stored XSS against whoever opens it.
ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".rtf", ".odt", ".txt",
    ".xls", ".xlsx", ".csv",
    ".ppt", ".pptx",
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".zip",
}
#: Types that are safe to render in the browser. Everything else downloads.
INLINE_SAFE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt"}

#: Hard ceiling for any single upload (bytes). Individual endpoints may be stricter.
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES") or 20 * 1024 * 1024)


def safe_upload_extension(filename: str | None) -> str:
    """Allowlisted, lower-cased extension for a stored file.

    Refuses rather than falling back, so a bad extension can never reach disk.
    """
    ext = Path(filename or "").suffix.lower()[:10]
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"File type '{ext or 'unknown'}' is not accepted. Allowed: "
                   + ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS)),
        )
    return ext


def read_upload_capped(file, max_bytes: int = MAX_UPLOAD_BYTES) -> bytes:
    """Read an upload in chunks, aborting once it exceeds the cap.

    Reading first and checking the length afterwards still lets an attacker put
    the whole body in memory, which is the DoS this prevents.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = file.file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File is larger than {max_bytes // (1024 * 1024)} MB",
            )
        chunks.append(chunk)
    if total == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    return b"".join(chunks)

def save_upload(file: UploadFile, subdir: str = "") -> str:
    """Persist an uploaded file under data/crm_uploads and return its serving URL."""
    url, _sha, _size = save_upload_hashed(file, subdir)
    return url


def save_upload_hashed(file: UploadFile, subdir: str = "") -> tuple[str, str, int]:
    """Persist an upload and return (serving_url, sha256_hex, size_bytes).

    The SHA-256 is over the exact stored bytes — used for integrity (verify on
    read) and to deduplicate identical files. Filenames are never trusted: the
    stored name is a random uuid + the sanitized extension only.
    """
    import hashlib

    target_dir = CRM_UPLOAD_DIR / subdir if subdir else CRM_UPLOAD_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = safe_upload_extension(file.filename)
    name = f"{uuid.uuid4().hex}{ext}"
    dest = target_dir / name
    data = read_upload_capped(file)
    with dest.open("wb") as fh:
        fh.write(data)
    rel = f"{subdir}/{name}" if subdir else name
    return f"/api/crm-files/{rel}", hashlib.sha256(data).hexdigest(), len(data)


def verify_crm_file_checksum(rel_path: str, expected_sha256: str) -> bool:
    """Re-hash a stored CRM file and constant-time compare to the expected digest."""
    import hashlib
    import hmac

    path = resolve_crm_file(rel_path)
    if not path or not expected_sha256:
        return False
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return hmac.compare_digest(actual, expected_sha256.strip().lower())


def resolve_crm_file(rel_path: str) -> Path | None:
    """Map /api/crm-files/<rel> back to a real path, refusing traversal."""
    candidate = (CRM_UPLOAD_DIR / rel_path).resolve()
    try:
        candidate.relative_to(CRM_UPLOAD_DIR.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def rows_to_csv(rows: list[dict], filename: str = "report.csv"):
    """Build a text/csv response body from a list of dicts."""
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    from fastapi.responses import Response
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def get_app_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.execute(sa.text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default


def commit_or_conflict(
    db: Session,
    detail: str = "Cannot delete: record is still referenced by other data. Remove dependencies first.",
) -> None:
    """Commit; on FK violation roll back and raise HTTP 409 (never bubble as 500)."""
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=detail)
