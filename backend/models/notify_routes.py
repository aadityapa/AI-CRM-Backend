"""Admin-editable email routing: who receives which application email.

Before this, WHICH ROLES an event notified was a tuple in the source —
changing "timesheets also go to Sales" meant editing code and redeploying.
Now each named event has a row here that Admin/CEO edit from the Users tab,
and the notify helpers consult it at send time. No row (or a fresh install
before the migration ran) falls back to the code default, so the table can
never break notifications by being empty.

user_notify_prefs carries the per-person switch: when someone leaves, their
email is PAUSED here first (bell rows still accrue; nothing bounces from a
dead mailbox), and the account is deactivated/deleted separately.
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from models.base import Base


class NotificationRoute(Base):
    __tablename__ = "notification_routes"
    id = sa.Column(sa.Integer, primary_key=True)
    #: Stable event key the code sends with, e.g. "timesheet.submitted".
    event = sa.Column(sa.String(64), nullable=False, unique=True)
    #: CRM role names that receive this event's email + bell.
    roles = sa.Column(JSONB, nullable=False, server_default="[]")
    #: Extra literal addresses (auditors, group mailboxes) — email only.
    extra_emails = sa.Column(JSONB, nullable=False, server_default="[]")
    #: False = event silenced entirely (no email, no bell fan-out).
    enabled = sa.Column(sa.Boolean, nullable=False, server_default=sa.true())
    #: Admin-authored wording (0071). NULL = code-composed text, unchanged.
    #: Placeholders: {subject} {body} {recipient} {company} — filled at queue time.
    subject_template = sa.Column(sa.String(255), nullable=True)
    body_template = sa.Column(sa.Text, nullable=True)
    #: Admin-created drafts (0093) — rows keyed `custom.<slug>` that no code
    #: path fires; the composers offer them as "Use a draft". NULL for every
    #: code-defined event, whose label lives in email_flows.EVENTS.
    label = sa.Column(sa.String(160), nullable=True)
    description = sa.Column(sa.String(500), nullable=True)
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now(), nullable=False)
    updated_by = sa.Column(sa.Integer, nullable=True)


#: Event-key prefix of admin-created drafts.
CUSTOM_DRAFT_PREFIX = "custom."


class ActionPermission(Base):
    """Admin-edited role list for one gated write action (e.g. approving a
    timesheet). No row -> the code default applies; an EMPTY saved list means
    "Admin/CEO only" — admins always pass, so lock-out is impossible."""

    __tablename__ = "action_permissions"
    id = sa.Column(sa.Integer, primary_key=True)
    action = sa.Column(sa.String(64), nullable=False, unique=True)
    roles = sa.Column(JSONB, nullable=False, server_default="[]")
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now(), nullable=False)
    updated_by = sa.Column(sa.Integer, nullable=True)


class UserNotifyPref(Base):
    __tablename__ = "user_notify_prefs"
    user_id = sa.Column(sa.Integer, primary_key=True)  # registration_data.id
    email_paused = sa.Column(sa.Boolean, nullable=False, server_default=sa.false())
    updated_at = sa.Column(sa.DateTime(timezone=True), server_default=sa.func.now(),
                           onupdate=sa.func.now(), nullable=False)
    updated_by = sa.Column(sa.Integer, nullable=True)
