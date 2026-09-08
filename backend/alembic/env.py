"""Alembic environment for Karnex CRM tables.

Only CRM tables (models package) are managed here. Legacy interview-platform
tables created by auth_db.py are excluded from autogenerate so Alembic never
tries to drop or alter them.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# backend/ on sys.path so flat imports (models, crm_db) work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env() -> None:
    """Mirror main.py: load repo-root .env so DB_* vars are available to Alembic."""
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"
    )
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()

from crm_db import crm_database_url  # noqa: E402
from models import Base  # noqa: E402  (registers all CRM tables)

# Legacy raw-SQL tables that Alembic must never touch.
LEGACY_TABLES = {
    "registration_data", "login_data", "interview_schedule", "interview_records",
    "interview_progress", "job_templates", "hr_candidate_decisions",
    "opportunity_master", "customer_master", "prompt_log",
}

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in LEGACY_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=crm_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(crm_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
