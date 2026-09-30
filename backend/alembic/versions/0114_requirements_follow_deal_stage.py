"""Bring every requirement in line with its opportunity's stage (29 Sep 2026).

User report: "if the Sales team closes, holds or anything with an opportunity,
that does not reflect in every login". Two causes, both fixed in code today:

* the stage cascade (services/opportunities.py) skipped requirements still
  waiting on an approval, so a closed or held deal stayed in the RMG Review
  Queue and on the Screening Desk approvals strip;
* deals settled BEFORE the cascade existed (22 Sep 2026) never moved their
  requirement at all.

This revision applies the SAME rules to the data already there:

* closed / rejected / archived deal -> requirement Closed (won, partial) or
  Cancelled (lost, rejected, archived) unless it is already Fulfilled, Closed
  or Cancelled; any hold bookkeeping is cleared;
* Customer Hold / Sales Hold deal -> requirement On_Hold, remembering the
  status it left in `held_from_status` so Reactivate returns it exactly.

Downgrade is a no-op: the previous state was the inconsistency being removed.

Revision ID: 0114
Revises: 0113
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

_SETTLED = "('Fulfilled', 'Closed', 'Cancelled')"
_HOLDABLE = ("('Open_For_Sourcing', 'Posted_On_Portals', 'In_Progress', 'Draft', "
             "'Pending_Sales_Head_Approval', 'Sales_Head_Rejected', "
             "'Pending_Engineering_Review', 'Engineering_Rejected')")


def _close(target: str, stages: str) -> str:
    return (
        f"UPDATE requirements SET status = '{target}', held_from_status = NULL, "
        f"held_reason = NULL WHERE CAST(status AS TEXT) NOT IN {_SETTLED} "
        f"AND opportunity_id IN (SELECT id FROM opportunities "
        f"WHERE CAST(pipeline_stage AS TEXT) IN {stages})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_close("Closed", "('Closed_Won', 'Closed_Partial')")))
    bind.execute(sa.text(_close("Cancelled", "('Closed_Lost', 'Rejected', 'Archived')")))
    bind.execute(sa.text(
        "UPDATE requirements SET held_from_status = CAST(status AS TEXT), "
        "held_reason = 'Opportunity on hold', status = 'On_Hold' "
        f"WHERE CAST(status AS TEXT) IN {_HOLDABLE} "
        "AND opportunity_id IN (SELECT id FROM opportunities "
        "WHERE CAST(pipeline_stage AS TEXT) IN ('On_Hold', 'Sales_Hold'))"
    ))


def downgrade() -> None:
    pass
