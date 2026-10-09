"""My Tasks ticks (8 Oct 2026) — "work done, tick it and close it".

One row per (login, task item) that the person marked as done on My Tasks —
e.g. Finance ticks an employee's invoices once everything for them is handled.
A tick is PERSONAL (another Finance user still sees the item) and never changes
the record behind it: it only hides the item from that person's default view
until they untick it. `item_key` is the desk item's own key ("iv:123").
"""
from __future__ import annotations

import sqlalchemy as sa

from models.base import Base


class WorkDeskMark(Base):
    __tablename__ = "work_desk_marks"
    __table_args__ = (sa.UniqueConstraint("user_id", "item_key", name="uq_work_desk_marks_user_item"),)

    id = sa.Column(sa.Integer, primary_key=True)
    user_id = sa.Column(sa.Integer, nullable=False, index=True)
    item_key = sa.Column(sa.String(80), nullable=False)
    marked_at = sa.Column(sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())
