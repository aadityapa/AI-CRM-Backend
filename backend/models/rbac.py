"""RBAC (roles / user_roles) + notifications.

user_roles joins the EXISTING users table (registration_data). The legacy
text column registration_data.role ('hr'/'candidate') is kept untouched for
the interview platform; CRM roles live here.
"""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK, pg_enum


class RoleName(str, enum.Enum):
    CEO = "CEO"  # super-admin: everything Admin can do, plus edits per-user tab access
    ADMIN = "Admin"
    SALES = "Sales"
    SALES_HEAD = "Sales_Head"
    RMG = "RMG"
    TA = "TA"
    HR = "HR"
    FINANCE = "Finance"


class Role(Base):
    __tablename__ = "roles"
    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(pg_enum(RoleName, "role_name"), nullable=False, unique=True)


class UserRole(Base):
    __tablename__ = "user_roles"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False, index=True)
    role_id = sa.Column(sa.Integer, sa.ForeignKey("roles.id"), nullable=False)
    __table_args__ = (sa.UniqueConstraint("user_id", "role_id", name="uq_user_role"),)

    role = relationship("Role")


class Notification(Base):
    __tablename__ = "notifications"
    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False, index=True)
    title = sa.Column(sa.String(255), nullable=False)
    message = sa.Column(sa.Text, nullable=True)
    link = sa.Column(sa.String(1024), nullable=True)
    is_read = sa.Column(sa.Boolean, nullable=False, server_default=sa.false(), index=True)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
