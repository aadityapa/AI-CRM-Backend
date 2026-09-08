from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger("karnex.hr.repository")

_WRITE_LOCK = threading.RLock()
_ASYNC_WRITER = threading.Thread  # alias for tests


def _read_records_unlocked(data_file: Path) -> list[dict]:
    if not data_file.exists():
        return []
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def load_hr_records(data_file: Path) -> list[dict]:
    with _WRITE_LOCK:
        return _read_records_unlocked(data_file)


def save_hr_records(data_file: Path, records: list[dict]) -> None:
    data_file.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(records, indent=2)
    tmp = data_file.with_suffix(data_file.suffix + ".tmp")
    with _WRITE_LOCK:
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(data_file)


def upsert_hr_record(data_file: Path, record: dict) -> None:
    with _WRITE_LOCK:
        records = _read_records_unlocked(data_file)
        target_id = str(record.get("id", "")).strip()
        updated = False
        for idx, item in enumerate(records):
            if str(item.get("id", "")) == target_id:
                records[idx] = record
                updated = True
                break
        if not updated:
            records.append(record)
        data_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(records, indent=2)
        tmp = data_file.with_suffix(data_file.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(data_file)


def upsert_hr_record_async(data_file: Path, record: dict) -> None:
    """Best-effort JSON mirror; Postgres remains the source of truth."""

    def _write() -> None:
        try:
            upsert_hr_record(data_file, record)
        except Exception as exc:
            logger.warning("hr_records.async_write_failed: %s", exc, exc_info=True)

    if str(os.getenv("HR_RECORDS_SYNC", "async")).strip().lower() in {"0", "false", "no", "off", "sync"}:
        upsert_hr_record(data_file, record)
        return
    threading.Thread(target=_write, daemon=True, name="hr-records-json").start()


def _candidate_id_for_record(record: dict) -> str:
    profile = record.get("candidate_profile") or {}
    email = (str(record.get("candidate_email") or profile.get("email") or "")).strip().lower()
    if email and email != "not available":
        return email
    name = (str(record.get("candidate_name") or profile.get("name") or "")).strip().lower()
    return name


def list_records_for_candidate(data_file: Path, candidate_id: str) -> list[dict]:
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return []
    return [r for r in load_hr_records(data_file) if _candidate_id_for_record(r) == cid]


def delete_records_for_candidate(data_file: Path, candidate_id: str) -> int:
    """Filter the HR JSON archive by candidate id and persist atomically."""
    cid = (candidate_id or "").strip().lower()
    if not cid:
        return 0
    with _WRITE_LOCK:
        records = _read_records_unlocked(data_file)
        keep = [r for r in records if _candidate_id_for_record(r) != cid]
        removed = len(records) - len(keep)
        if removed:
            save_hr_records(data_file, keep)
    return removed


def delete_record_by_id(data_file: Path, record_id: str) -> int:
    rid = str(record_id or "").strip()
    if not rid:
        return 0
    with _WRITE_LOCK:
        records = _read_records_unlocked(data_file)
        keep = [r for r in records if str(r.get("id", "")).strip() != rid]
        removed = len(records) - len(keep)
        if removed:
            save_hr_records(data_file, keep)
    return removed
