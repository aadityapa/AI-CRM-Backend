"""Karnex CRM database engine / session management.

CRM tables are PostgreSQL-only and managed with SQLAlchemy 2.0 + Alembic.
The existing AI-interview tables keep using auth_db.py (raw SQL) untouched.

Resolution order for the connection URL:
  1. CRM_DATABASE_URL  (explicit override)
  2. AUTH_DB_URL       (same Postgres the interview platform uses in prod)

The engine is created lazily so the interview platform still boots when no
Postgres is configured (CRM endpoints will then return 503).
"""
from __future__ import annotations

import os
import threading

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker


def _load_repo_env() -> None:
    """Load repo-root .env for standalone CRM CLIs (seed_crm.py, assign_crm_role.py,
    alembic). The web app loads it via main.py; setdefault avoids overriding that."""
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_repo_env()

# RLock: get_session_factory() holds the lock while calling get_engine(),
# which acquires it again — a plain Lock would deadlock on that reentry.
_lock = threading.RLock()
_engine: Engine | None = None
_session_factory: sessionmaker | None = None


class CrmNotConfiguredError(RuntimeError):
    """Raised when no PostgreSQL URL is configured for the CRM."""


def _url_from_db_parts() -> str:
    """Same DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD fallback main.py uses."""
    host = (os.getenv("DB_HOST") or "").strip()
    port = (os.getenv("DB_PORT") or "5432").strip()
    name = (os.getenv("DB_NAME") or "").strip()
    user = (os.getenv("DB_USER") or "").strip()
    password = (os.getenv("DB_PASSWORD") or "").strip()
    if host and name and user:
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    return ""


def crm_database_url() -> str:
    url = (os.getenv("CRM_DATABASE_URL") or os.getenv("AUTH_DB_URL") or "").strip()
    if not url:
        url = _url_from_db_parts()
    if not url:
        raise CrmNotConfiguredError(
            "Karnex CRM requires PostgreSQL. Set CRM_DATABASE_URL (or AUTH_DB_URL, "
            "or DB_HOST/DB_NAME/DB_USER) to point at Postgres. See .env.example."
        )
    # Normalise scheme variants to the psycopg2 driver.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg2://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    if not url.startswith("postgresql+"):
        raise CrmNotConfiguredError(
            "Karnex CRM tables are PostgreSQL-only; got a non-postgres DSN. "
            "SQLite is supported only for the legacy interview tables."
        )
    return url


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = create_engine(
                    crm_database_url(),
                    pool_pre_ping=True,
                    pool_size=int(os.getenv("CRM_DB_POOL_SIZE", "5")),
                    max_overflow=int(os.getenv("CRM_DB_MAX_OVERFLOW", "10")),
                    future=True,
                )
    return _engine


def get_session_factory() -> sessionmaker:
    global _session_factory
    if _session_factory is None:
        with _lock:
            if _session_factory is None:
                _session_factory = sessionmaker(
                    bind=get_engine(), autoflush=False, expire_on_commit=False, future=True
                )
    return _session_factory


def get_db():
    """FastAPI dependency yielding a CRM session."""
    session: Session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()
