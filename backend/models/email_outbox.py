"""Durable outbox for outbound notification email.

Why a table instead of sending inline: `email_smtp.send_email` is synchronous
smtplib with a 30-second timeout, and several notifications fan out to every
holder of a role. Sending inline would put minutes of SMTP latency inside a
request. Sending fire-and-forget would repeat the `hr_records.json` failure
mode — a transient O365 error swallowed on a detached thread, with nobody
ever learning the message was lost.

So: request handlers INSERT a row in the same transaction as the business
change (if the timesheet submit rolls back, the email is never queued), and a
background worker drains the table with retry and backoff. The table doubles
as the audit trail for "did the candidate actually get their invite?".
"""
from __future__ import annotations

import enum

import sqlalchemy as sa

from models.base import Base, TimestampMixin, pg_enum


class EmailStatus(str, enum.Enum):
    QUEUED = "Queued"
    SENT = "Sent"
    FAILED = "Failed"       # retries exhausted
    SKIPPED = "Skipped"     # suppressed by config, or no usable address


class EmailOutbox(TimestampMixin, Base):
    __tablename__ = "email_outbox"

    id = sa.Column(sa.Integer, primary_key=True)

    #: Stable event key, e.g. "timesheet.submitted". Used for per-event opt-out
    #: (app_settings key "email_event_<event>") and for filtering the admin view.
    event = sa.Column(sa.String(64), nullable=False, index=True)

    to_email = sa.Column(sa.String(320), nullable=False)
    to_name = sa.Column(sa.String(255), nullable=True)
    subject = sa.Column(sa.String(500), nullable=False)
    body_text = sa.Column(sa.Text, nullable=False)
    body_html = sa.Column(sa.Text, nullable=True)

    #: Sender identity. The envelope sender is always SMTP_FROM (so SPF/DKIM
    #: stay aligned with the tenant); these two only shape the headers.
    #: from_name  -> From: "Pavan Sanap (Karnex)" <SMTP_FROM>
    #: reply_to_* -> Reply-To: the user whose action triggered the mail, so the
    #: candidate's reply lands in that recruiter's inbox rather than a shared one.
    from_name = sa.Column(sa.String(255), nullable=True)
    reply_to_email = sa.Column(sa.String(320), nullable=True)
    reply_to_name = sa.Column(sa.String(255), nullable=True)

    status = sa.Column(pg_enum(EmailStatus, "email_outbox_status"), nullable=False,
                       server_default=EmailStatus.QUEUED.value, index=True)
    attempts = sa.Column(sa.Integer, nullable=False, server_default="0")
    last_error = sa.Column(sa.Text, nullable=True)
    #: Worker only picks up rows whose next_attempt_at has passed (backoff).
    next_attempt_at = sa.Column(sa.DateTime(timezone=True), nullable=False,
                                server_default=sa.func.now(), index=True)
    sent_at = sa.Column(sa.DateTime(timezone=True), nullable=True)

    #: Optional idempotency guard. Set it to something like
    #: "timesheet.submitted:412:user:9" and a duplicate submit will not send twice.
    dedupe_key = sa.Column(sa.String(255), nullable=True, unique=True)

    #: Loose backlink for the admin view ("timesheet"/412). Deliberately not an
    #: FK: the referenced row may be deleted and we still want the send record.
    related_type = sa.Column(sa.String(48), nullable=True)
    related_id = sa.Column(sa.Integer, nullable=True)

    __table_args__ = (
        # The worker's hot query: pending rows that are due, oldest first.
        sa.Index("ix_email_outbox_pending", "status", "next_attempt_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<EmailOutbox {self.id} {self.event} {self.status} -> {self.to_email}>"
