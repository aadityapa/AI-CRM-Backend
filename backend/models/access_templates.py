"""Access Templates — reusable, department/role-wise tab + field permissions.

Admin/CEO defines a named template (e.g. "Sales", "RMG") granting, per CRM tab and
per field, a MODE of "view" (read-only) or "edit" (insert/update). Users are LIVE-LINKED
to a template via `user_profiles.access_template_id`, so editing a template updates every
user on it. A per-user override (UserProfile.tab_access/field_access) still wins on top.

Shapes (JSON):
  tab_access   = { "<tab_key>": "view" | "edit" }                 # tab absent = no access
  field_access = { "<tab_key>": { "<field_key>": "view"|"edit" } } # field absent = inherits tab mode
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from models.base import Base, TimestampMixin


class AccessTemplate(Base, TimestampMixin):
    __tablename__ = "access_templates"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String(120), nullable=False, unique=True)
    description = sa.Column(sa.String(512), nullable=True)
    # Optional tags — a template may be associated with a department and/or a CRM role.
    department_id = sa.Column(sa.Integer, sa.ForeignKey("departments.id"), nullable=True, index=True)
    role = sa.Column(sa.String(64), nullable=True)  # e.g. Sales / RMG / TA / HR (free tag)
    is_active = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    # { tab_key: "view" | "edit" }
    tab_access = sa.Column(sa.JSON, nullable=True)
    # { tab_key: { field_key: "view" | "edit" } }
    field_access = sa.Column(sa.JSON, nullable=True)

    department = relationship("Department")
