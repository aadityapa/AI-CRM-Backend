"""Branch-wise billing policy: make branch policy booleans tri-state (NULL = inherit).

The branch billing columns themselves were added in 0008. This revision only
relaxes the four policy booleans on customer_branches to NULLABLE so a branch
can leave a toggle unset and inherit the customer-level default policy
(field-by-field fallback in services.timesheets.effective_billing_policy).

Existing `false` values are converted to NULL: they were server-default noise
(branch-level resolution did not exist before this feature), so mapping them to
"inherit" preserves today's effective billing behaviour exactly. Explicit `true`
values (authored via the customer form) are kept.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

BOOL_COLS = ("holidays_billable", "weekoff_billable", "leave_billable", "comp_off_billable")
TABLE = "customer_branches"


def _columns(bind) -> dict[str, dict]:
    insp = sa.inspect(bind)
    return {c["name"]: c for c in insp.get_columns(TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    to_alter = [n for n in BOOL_COLS if n in cols and not cols[n].get("nullable")]
    if to_alter:
        with op.batch_alter_table(TABLE) as batch:
            for name in to_alter:
                batch.alter_column(name, existing_type=sa.Boolean(),
                                   nullable=True, server_default=None)
    # Reset default-noise `false` to NULL (= inherit customer default).
    for name in to_alter:
        bind.execute(sa.text(f"UPDATE {TABLE} SET {name} = NULL WHERE {name} = false"))


def downgrade() -> None:
    bind = op.get_bind()
    cols = _columns(bind)
    to_alter = [n for n in BOOL_COLS if n in cols and cols[n].get("nullable")]
    for name in to_alter:
        bind.execute(sa.text(f"UPDATE {TABLE} SET {name} = false WHERE {name} IS NULL"))
    if to_alter:
        with op.batch_alter_table(TABLE) as batch:
            for name in to_alter:
                batch.alter_column(name, existing_type=sa.Boolean(),
                                   nullable=False, server_default=sa.text("false"))
