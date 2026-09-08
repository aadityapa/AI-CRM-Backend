"""The customer's own two interview rounds as pipeline stages.

After we submit a candidate, the customer runs its own interviews — typically
two — and gives feedback on each. The pipeline jumped straight from
"Customer Interviewing" to "Customer Shortlisted", so there was no way to see
which of the customer's rounds a candidate was actually waiting on, and the
feedback for the first round had nowhere to live once the second happened.

`L1_Feedback` / `L2_Feedback` are deliberately named for the FEEDBACK rather
than the interview: reaching the stage means that round's verdict is in.

Note: these are the CUSTOMER's rounds. RMG's technical L1/L2 are interview
events (kinds `L1_Interview` / `L2_F2F`) much earlier and are unaffected.

Revision ID: 0063
Revises: 0062
"""
from __future__ import annotations

from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None

ENUM_NAME = "profile_pipeline_status"
NEW_VALUES = ("L1_Feedback", "L2_Feedback")
#: Insert after this so the enum's own order matches the pipeline order —
#: which is what `ORDER BY pipeline_status` will use.
AFTER = "Customer_Interview"


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "postgresql":
        return  # SQLite stores the value as text; nothing to alter.

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block that later
    # uses the new value. autocommit_block commits the surrounding transaction
    # first, which is the supported way to do this in Alembic.
    with op.get_context().autocommit_block():
        previous = AFTER
        for value in NEW_VALUES:
            op.execute(
                f"ALTER TYPE {ENUM_NAME} ADD VALUE IF NOT EXISTS '{value}' AFTER '{previous}'"
            )
            previous = value


def downgrade() -> None:
    """Postgres cannot drop a value from an enum.

    Removing one means rebuilding the type and rewriting every row that uses
    it. Any profile sitting in L1_Feedback / L2_Feedback would have to be moved
    somewhere first, and silently reassigning a candidate's stage during a
    downgrade would be worse than refusing.
    """
    raise NotImplementedError(
        "Downgrade not supported: Postgres cannot remove an enum value, and "
        "profiles may already be sitting in L1_Feedback / L2_Feedback. Move "
        "those profiles to another stage and rebuild the type by hand if this "
        "genuinely needs reverting."
    )
