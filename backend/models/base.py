"""Declarative base + shared helpers for Karnex CRM models.

Only NEW CRM tables are mapped here. Existing interview-platform tables
(registration_data, interview_*, job_templates, ...) stay in auth_db.py raw
SQL and are never dropped or rewritten by this package. CRM tables reference
the existing users table (registration_data) by FK only.
"""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase

# Existing users table created by auth_db.py
USERS_TABLE = "registration_data"
USERS_FK = f"{USERS_TABLE}.id"


class Base(DeclarativeBase):
    pass


# Minimal stub of the legacy users table so SQLAlchemy can resolve CRM FKs to
# registration_data.id. Never created or altered by CRM code: the real table is
# managed by auth_db.py, the initial migration verifies it exists before
# create_all (checkfirst skips it), and alembic/env.py excludes legacy tables.
users_table_stub = sa.Table(
    USERS_TABLE,
    Base.metadata,
    sa.Column("id", sa.Integer, primary_key=True),
)


def pg_enum(enum_cls: type[enum.Enum], name: str) -> sa.Enum:
    """Native Postgres enum storing the enum *values* (spec strings)."""
    return sa.Enum(
        enum_cls,
        name=name,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class TimestampMixin:
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    updated_at = sa.Column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class WorkMode(str, enum.Enum):
    """Where the deployment sits. The UI presents this as "Location".

    OFFSHORE was added for the Map Employee form (On Site / Off-Shore / Remote).
    HYBRID stays for existing rows even though the new form no longer offers it —
    removing a Postgres enum value would require rewriting history.
    """

    REMOTE = "Remote"
    ONSITE = "Onsite"
    HYBRID = "Hybrid"
    OFFSHORE = "Off-Shore"
