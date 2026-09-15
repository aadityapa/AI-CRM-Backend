"""Support tickets (14 Sep 2026): the in-app Help & Support bot escalates to
Admin/CEO through these two tables. See routers/crm/support.py.
"""
from __future__ import annotations

import enum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from models.base import Base, USERS_FK, TimestampMixin


class TicketStatus(str, enum.Enum):
    OPEN = "Open"
    IN_PROGRESS = "In_Progress"
    RESOLVED = "Resolved"
    CLOSED = "Closed"


class TicketPriority(str, enum.Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    URGENT = "Urgent"


TICKET_CATEGORIES = ("Bug", "Data_Issue", "Access", "How_To", "Feature_Request", "Other")


class SupportTicket(Base, TimestampMixin):
    __tablename__ = "support_tickets"
    id = sa.Column(sa.Integer, primary_key=True)
    #: Human number shown everywhere: T-0001, T-0002 … (assigned at create).
    ticket_no = sa.Column(sa.String(16), nullable=False, unique=True)
    user_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=False, index=True)
    user_name = sa.Column(sa.String(255), nullable=True)
    user_roles = sa.Column(sa.String(255), nullable=True)      # "TA, Sales" at raise time
    subject = sa.Column(sa.String(255), nullable=False)
    description = sa.Column(sa.Text, nullable=False)
    category = sa.Column(sa.String(32), nullable=False, server_default="Other")
    priority = sa.Column(sa.String(16), nullable=False, server_default=TicketPriority.MEDIUM.value, index=True)
    status = sa.Column(sa.String(16), nullable=False, server_default=TicketStatus.OPEN.value, index=True)
    #: Where the user was when they raised it (CRM path or platform view).
    page = sa.Column(sa.String(255), nullable=True)
    #: The bot conversation that preceded the ticket: [{role, content, at}].
    bot_transcript = sa.Column(JSONB, nullable=True)
    assigned_to = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True, index=True)
    assigned_to_name = sa.Column(sa.String(255), nullable=True)
    resolved_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    closed_at = sa.Column(sa.DateTime(timezone=True), nullable=True)
    #: 1–5 the user may leave once resolved.
    rating = sa.Column(sa.SmallInteger, nullable=True)

    messages = relationship("SupportTicketMessage", back_populates="ticket",
                            cascade="all, delete-orphan", order_by="SupportTicketMessage.id")


class SupportTicketMessage(Base):
    __tablename__ = "support_ticket_messages"
    id = sa.Column(sa.Integer, primary_key=True)
    ticket_id = sa.Column(sa.Integer, sa.ForeignKey("support_tickets.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    author_id = sa.Column(sa.Integer, sa.ForeignKey(USERS_FK), nullable=True)
    author_name = sa.Column(sa.String(255), nullable=True)
    #: True for Admin/CEO replies — the UI colours the two sides differently.
    is_staff = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    #: Status changes are recorded as system messages so the thread is the audit trail.
    is_system = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    body = sa.Column(sa.Text, nullable=False)
    created_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    ticket = relationship("SupportTicket", back_populates="messages")
