"""Self-service user profile — a 1:1 extension of the legacy users table.

registration_data (full_name/email/username/role) stays untouched; the extra
profile fields a user can maintain about themselves live here, in the CRM
Postgres, alembic-managed like the rest of the CRM.
"""
from __future__ import annotations

import sqlalchemy as sa

from models.base import Base, USERS_FK


class UserProfile(Base):
    __tablename__ = "user_profiles"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False, unique=True, index=True)
    phone = sa.Column(sa.String(32), nullable=True)
    job_title = sa.Column(sa.String(120), nullable=True)
    department = sa.Column(sa.String(120), nullable=True)
    timezone = sa.Column(sa.String(64), nullable=True)
    avatar_url = sa.Column(sa.String(1024), nullable=True)
    # Per-user tab-access (Admin/CEO managed). JSON array of tab keys the user may
    # see (e.g. ["crm:dashboard","iv:ats"]). NULL = role-based defaults; non-null
    # = explicit allow-list chosen by Admin in the Users UI.
    tab_access = sa.Column(sa.Text, nullable=True)
    # Per-tab field access: JSON { "<tab_key>": ["field_a", ...] }. A tab absent
    # here = all its fields allowed. Admin/CEO managed alongside tab_access.
    field_access = sa.Column(sa.Text, nullable=True)
    # Live link to a reusable Access Template (Admin/CEO managed). NULL = none.
    # Effective access = template (live) with the per-user tab_access/field_access
    # override winning on top; Admin/CEO always resolve to full access.
    access_template_id = sa.Column(sa.Integer, sa.ForeignKey("access_templates.id"), nullable=True, index=True)
    updated_at = sa.Column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )
