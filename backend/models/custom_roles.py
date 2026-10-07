"""Custom roles — Admin/CEO-defined roles with their own tab permissions (23 Sep 2026).

The eight built-in roles (`models/rbac.RoleName`) are a native Postgres enum
baked into the code and every gate; adding "GM" there means a migration, an
enum ALTER and a redeploy for every new job title. A CUSTOM role is data:
a name, a description and the same `{tab_key: "view"|"edit"|"create"}` grant
map the Access Templates use.

How it takes effect (no new gate anywhere):
  * `crm_deps.get_current_user` adds the user's ACTIVE custom-role names to
    `CurrentUser.roles`, so the "no CRM role" 403 in `_gate` / `any_crm_role`
    passes and `user.roles` names the role for audit and UI.
  * `services.access_templates.effective_access` resolves the grant map from
    the user's custom roles when they have no explicit Access Template — the
    same authoritative-template path every CRM endpoint already honours.
  * `role_required("Finance")`-style endpoints (hard role lists, e.g. the CEO
    revenue report) are NOT widened by a custom role — by design, exactly as
    Access Templates cannot widen them.

Shapes: `tab_access = {"invoices": "edit", "pos": "view"}`,
        `field_access = {"invoices": {"amount": "view"}}` (optional).
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin, USERS_FK


class CustomRole(Base, TimestampMixin):
    __tablename__ = "custom_roles"

    id = sa.Column(sa.Integer, primary_key=True)
    #: Display + membership name ("GM"). Must not collide with a built-in role.
    name = sa.Column(sa.String(40), nullable=False, unique=True)
    description = sa.Column(sa.String(512), nullable=True)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    tab_access = sa.Column(sa.JSON, nullable=True)
    field_access = sa.Column(sa.JSON, nullable=True)
    #: Approval buttons this role grants (25 Sep 2026) — see AccessTemplate.
    action_access = sa.Column(sa.JSON, nullable=True)
    #: Where the role sits on Access Control (7 Oct 2026, migration 0129): one
    #: of `access_registry.DEPARTMENT_KEYS`; NULL reads as "other".
    department = sa.Column(sa.String(40), nullable=True)
    created_by = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)

    members = relationship("UserCustomRole", back_populates="role",
                           cascade="all, delete-orphan", passive_deletes=True)


class UserCustomRole(Base):
    __tablename__ = "user_custom_roles"

    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False, index=True)
    custom_role_id = sa.Column(sa.Integer, sa.ForeignKey("custom_roles.id", ondelete="CASCADE"),
                               nullable=False, index=True)
    __table_args__ = (sa.UniqueConstraint("user_id", "custom_role_id", name="uq_user_custom_role"),)

    role = relationship("CustomRole", back_populates="members")
