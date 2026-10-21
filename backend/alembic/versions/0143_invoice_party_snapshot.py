"""Issued invoices keep their address; customers get an Activity Log (21 Oct 2026).

Reported: a customer's address changed and every invoice already generated for
it printed the new address — the renderers read the branch live.

* `invoices.party_snapshot` (JSON) — the billing branch, delivery branch and
  customer names as they were when the invoice was issued
  (services/invoice_parties.py). Backfilled below for every existing invoice
  from the records as they are TODAY: the old address is not stored anywhere,
  so an invoice whose customer already moved prints the new one until Finance
  corrects it (invoice page ▸ Address on this invoice ▸ Correct, which can also
  fix every earlier invoice of the customer printing the same address).
* `customer_activity_log` — every change to a customer, its branches, contacts,
  documents and policies from now on, with old → new values.

The backfill is plain SQL + Python (no ORM import), so this migration keeps
working when the models grow later.

Revision ID: 0143
Revises: 0142
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None

#: Frozen copies of services/invoice_parties BRANCH_FIELDS / CUSTOMER_FIELDS.
_BRANCH = ("id", "branch_name", "branch_legal_name", "billing_address", "address_line_2",
           "delivery_address", "city", "state", "pincode", "country", "gstin", "pan")
_CUSTOMER = ("id", "name", "legal_entity_name", "city", "state", "pincode", "country")


def _json_type():
    bind = op.get_bind()
    return postgresql.JSONB(astext_type=sa.Text()) if bind.dialect.name == "postgresql" else sa.JSON()


def upgrade() -> None:
    jt = _json_type()
    op.add_column("invoices", sa.Column("party_snapshot", jt, nullable=True))
    op.create_table(
        "customer_activity_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("branch_id", sa.Integer(), nullable=True),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("registration_data.id"), nullable=True),
        sa.Column("user_name", sa.String(255), nullable=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("changes", jt, nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_customer_activity_log_customer_id", "customer_activity_log", ["customer_id"])
    op.create_index("ix_customer_activity_log_branch_id", "customer_activity_log", ["branch_id"])
    op.create_index("ix_customer_activity_log_timestamp", "customer_activity_log", ["timestamp"])
    _backfill_snapshots(op.get_bind())


def _backfill_snapshots(bind) -> None:
    def rows(sql: str):
        return [dict(r._mapping) for r in bind.execute(sa.text(sql))]

    branches = {r["id"]: r for r in rows(f"SELECT customer_id, is_primary, {', '.join(_BRANCH)} "
                                         "FROM customer_branches")}
    customers = {r["id"]: r for r in rows(f"SELECT {', '.join(_CUSTOMER)} FROM customers")}
    primary: dict[int, dict] = {}
    for b in sorted(branches.values(), key=lambda b: (not b["is_primary"], b["id"])):
        primary.setdefault(b["customer_id"], b)
    pos = {r["id"]: r for r in rows("SELECT id, customer_id, billing_branch_id, delivery_branch_id "
                                    "FROM purchase_orders")}
    projects = {r["id"]: r for r in rows("SELECT id, customer_id, branch_id FROM projects")}
    invoices = rows("SELECT id, po_id, project_id FROM invoices WHERE party_snapshot IS NULL")
    now = datetime.now(timezone.utc).isoformat()

    def pick(d, fields):
        return None if d is None else {f: d.get(f) for f in fields}

    stmt = sa.text("UPDATE invoices SET party_snapshot = :s WHERE id = :i")
    if bind.dialect.name == "postgresql":
        stmt = sa.text("UPDATE invoices SET party_snapshot = CAST(:s AS JSONB) WHERE id = :i")
    for inv in invoices:
        po = pos.get(inv["po_id"])
        project = projects.get(inv["project_id"])
        cid = (po or {}).get("customer_id") or (project or {}).get("customer_id")
        billing = None
        if po and po.get("billing_branch_id"):
            billing = branches.get(po["billing_branch_id"])
        if billing is None and project and project.get("branch_id"):
            billing = branches.get(project["branch_id"])
        if billing is None and cid:
            billing = primary.get(cid)
        delivery = branches.get(po["delivery_branch_id"]) if po and po.get("delivery_branch_id") else None
        if billing is None and cid not in customers:
            continue
        snap = {"v": 1, "captured_at": now, "source": "backfill",
                "customer": pick(customers.get(cid), _CUSTOMER),
                "billing": pick(billing, _BRANCH),
                "delivery": pick(delivery, _BRANCH) if delivery is not None and delivery is not billing else None,
                "corrected": None}
        bind.execute(stmt, {"s": json.dumps(snap, default=str), "i": inv["id"]})


def downgrade() -> None:
    op.drop_index("ix_customer_activity_log_timestamp", table_name="customer_activity_log")
    op.drop_index("ix_customer_activity_log_branch_id", table_name="customer_activity_log")
    op.drop_index("ix_customer_activity_log_customer_id", table_name="customer_activity_log")
    op.drop_table("customer_activity_log")
    op.drop_column("invoices", "party_snapshot")
