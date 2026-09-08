"""Karnex CRM initial schema.

Creates all ~45 CRM tables from models.Base metadata, extends the existing
users table (registration_data) with role_id / is_active / employee_id, and
seeds the 7 CRM roles. Never drops or alters legacy interview tables.

Revision ID: 0001
Revises:
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

from models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ROLE_NAMES = ("Admin", "Sales", "Sales_Head", "RMG", "TA", "HR", "Finance")


def upgrade() -> None:
    bind = op.get_bind()

    # 0) CRM tables FK-reference the existing users table. On a brand-new DB,
    #    boot the interview platform once first (it creates its tables at startup).
    if not sa.inspect(bind).has_table("registration_data"):
        raise RuntimeError(
            "Legacy table 'registration_data' not found in this database. "
            "Start the AI Interview backend once against this Postgres "
            "(init_auth_db creates its tables), then re-run: alembic upgrade head"
        )

    # 1) All CRM tables (single source of truth: models package).
    Base.metadata.create_all(bind=bind, checkfirst=True)

    # 2) Seed the 7 CRM roles (idempotent).
    for name in ROLE_NAMES:
        bind.execute(
            sa.text("INSERT INTO roles (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"),
            {"n": name},
        )

    # 3) Extend the existing users table — ADDITIVE only.
    op.execute("ALTER TABLE registration_data ADD COLUMN IF NOT EXISTS role_id INTEGER")
    op.execute("ALTER TABLE registration_data ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
    op.execute("ALTER TABLE registration_data ADD COLUMN IF NOT EXISTS employee_id INTEGER")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_role_id') THEN
                ALTER TABLE registration_data
                    ADD CONSTRAINT fk_users_role_id FOREIGN KEY (role_id) REFERENCES roles (id);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_users_employee_id') THEN
                ALTER TABLE registration_data
                    ADD CONSTRAINT fk_users_employee_id FOREIGN KEY (employee_id) REFERENCES employees (id);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE registration_data DROP CONSTRAINT IF EXISTS fk_users_employee_id")
    op.execute("ALTER TABLE registration_data DROP CONSTRAINT IF EXISTS fk_users_role_id")
    op.execute("ALTER TABLE registration_data DROP COLUMN IF EXISTS employee_id")
    op.execute("ALTER TABLE registration_data DROP COLUMN IF EXISTS is_active")
    op.execute("ALTER TABLE registration_data DROP COLUMN IF EXISTS role_id")
    # Drop CRM tables + their native enums — but NEVER the legacy users table,
    # which exists in metadata only as an FK-resolution stub.
    crm_tables = [t for t in Base.metadata.sorted_tables if t.name != "registration_data"]
    Base.metadata.drop_all(bind=op.get_bind(), tables=crm_tables, checkfirst=True)
