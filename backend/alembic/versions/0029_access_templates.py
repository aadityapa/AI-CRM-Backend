"""Access Templates — reusable department/role-wise tab + field permissions.

- access_templates: named template with tab_access + field_access JSON (view/edit modes),
  optional department_id / role tag.
- user_profiles.access_template_id: live link from a user to a template.

Revision ID: 0029
Revises: 0028
"""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tab_access", sa.JSON(), nullable=True),
        sa.Column("field_access", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_access_templates_department_id", "access_templates", ["department_id"])
    op.add_column("user_profiles",
                  sa.Column("access_template_id", sa.Integer(),
                            sa.ForeignKey("access_templates.id"), nullable=True))
    op.create_index("ix_user_profiles_access_template_id", "user_profiles", ["access_template_id"])


def downgrade() -> None:
    op.drop_index("ix_user_profiles_access_template_id", table_name="user_profiles")
    op.drop_column("user_profiles", "access_template_id")
    op.drop_index("ix_access_templates_department_id", table_name="access_templates")
    op.drop_table("access_templates")
