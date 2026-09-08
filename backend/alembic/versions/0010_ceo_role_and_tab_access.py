"""CEO super-role + per-user tab-access override.

Adds a 'CEO' value to the role_name enum and a roles row for it, adds
user_profiles.tab_access (JSON text; NULL = no override), and seeds the two
super-users requested: Karan Singh -> CEO, Vishal -> Admin (idempotent).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1) Add the new enum value. ALTER TYPE ... ADD VALUE cannot run inside the
    #    migration's transaction, so use an autocommit block. IF NOT EXISTS keeps
    #    it safe to re-run across environments.
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE role_name ADD VALUE IF NOT EXISTS 'CEO'")

    # 2) roles row for CEO (enum value now committed, so referencing it is safe).
    op.execute("INSERT INTO roles (name) VALUES ('CEO') ON CONFLICT (name) DO NOTHING")
    op.execute("INSERT INTO roles (name) VALUES ('Admin') ON CONFLICT (name) DO NOTHING")

    # 3) tab_access column.
    op.add_column("user_profiles", sa.Column("tab_access", sa.Text(), nullable=True))

    # 4) Seed the two super-users by username (additive; never removes roles).
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        SELECT rd.id, r.id
        FROM registration_data rd, roles r
        WHERE lower(rd.username) = 'karan' AND r.name = 'CEO'
        ON CONFLICT (user_id, role_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        SELECT rd.id, r.id
        FROM registration_data rd, roles r
        WHERE lower(rd.username) = 'vishal' AND r.name = 'Admin'
        ON CONFLICT (user_id, role_id) DO NOTHING
        """
    )


def downgrade() -> None:
    # Postgres cannot drop a single enum value; leave the type as-is. Remove the
    # column and the CEO role assignments/row.
    op.execute("DELETE FROM user_roles WHERE role_id IN (SELECT id FROM roles WHERE name = 'CEO')")
    op.execute("DELETE FROM roles WHERE name = 'CEO'")
    op.drop_column("user_profiles", "tab_access")
