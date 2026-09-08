from pathlib import Path
import os
import shutil

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"
DOCS_DIR = ROOT_DIR / "docs"


def _resolve_frontend_dir() -> Path:
    """
    Resolve frontend static files for the API server.

    Priority:
    1. FRONTEND_DIR env (absolute or relative path)
    2. backend repo's own frontend/ folder (monolith layout)
    3. sibling AI-Interview-Model-F-V2/frontend (split-repo layout)
    """
    raw = (os.getenv("FRONTEND_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()

    local = ROOT_DIR / "frontend"
    if local.is_dir():
        return local

    sibling = (ROOT_DIR.parent / "AI-Interview-Model-F-V2" / "frontend").resolve()
    if sibling.is_dir():
        return sibling

    return local


FRONTEND_DIR = _resolve_frontend_dir()

HR_RECORDS_FILE = DATA_DIR / "hr_records.json"
LEARNING_FILE = DATA_DIR / "interview_learning.jsonl"
HR_ACCESS_CODE_FILE = DATA_DIR / "hr_access_code.txt"
KARNEX_DB_FILE = DATA_DIR / "karnex_db.db"

LEGACY_HR_RECORDS_FILE = ROOT_DIR / "hr_records.json"
LEGACY_LEARNING_FILE = ROOT_DIR / "interview_learning.jsonl"


PROMPT_LOGS_DIR = LOGS_DIR / "openai-prompts"


def ensure_project_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPT_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)


def migrate_legacy_data_files() -> None:
    if LEGACY_HR_RECORDS_FILE.exists() and not HR_RECORDS_FILE.exists():
        shutil.copy2(LEGACY_HR_RECORDS_FILE, HR_RECORDS_FILE)
    if LEGACY_LEARNING_FILE.exists() and not LEARNING_FILE.exists():
        shutil.copy2(LEGACY_LEARNING_FILE, LEARNING_FILE)
