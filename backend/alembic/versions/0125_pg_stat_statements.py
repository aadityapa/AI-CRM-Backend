"""Turn on `pg_stat_statements` so the next hot query is visible (7 Oct 2026).

Production pulled ~8.7 GB/day from Postgres before anyone could say which
statement did it; the answer had to be dug out of `pg_stat_user_tables` and
guesswork. RDS already loads the module (`shared_preload_libraries`), only
the extension was never created. This creates it where it can:

* skipped on SQLite (the test database);
* a missing permission or an unloaded module is LOGGED and swallowed — an
  observability extension must never block a release.

Read it with (CLAUDE.md §8 has the same query):

    SELECT calls, rows, shared_blks_read, shared_blks_hit,
           round(total_exec_time::numeric, 1) AS ms, left(query, 120) AS q
    FROM pg_stat_statements ORDER BY rows DESC LIMIT 15;

Revision ID: 0125
Revises: 0124
"""
from __future__ import annotations

import logging

from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    try:
        # Its own autocommit connection: a failed CREATE EXTENSION inside the
        # migration transaction would abort the whole upgrade.
        with bind.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
    except Exception as exc:  # noqa: BLE001 - observability, never a gate
        logger.warning(
            "pg_stat_statements not enabled (%s) — run as a superuser / rds_superuser: "
            "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;", exc)


def downgrade() -> None:
    # Deliberately left in place: dropping the extension would erase the
    # statistics the operator may be reading.
    return
