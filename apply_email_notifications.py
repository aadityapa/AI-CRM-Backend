#!/usr/bin/env python3
"""Apply the Karnex email-notification feature to an existing checkout.

Run from the repo root of AI-Interview-Model-B-V2:

    python apply_email_notifications.py            # apply
    python apply_email_notifications.py --check    # report only, change nothing

Idempotent: every edit is skipped if it is already present, so a re-run after a
partial apply is safe. Nothing is deleted and no file is created outside the
paths listed in PLAN below.
"""
from __future__ import annotations

import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NEWFILES_DIR = os.path.join(HERE, "newfiles")

PLAN = """
NEW FILES
  backend/models/email_outbox.py                     EmailOutbox model
  backend/services/recipients.py                     user/role/employee -> email address
  backend/services/email_outbox.py                   queue, render, drain, worker
  backend/routers/crm/email_outbox.py                admin list/stats/drain/retry
  backend/alembic/versions/0064_email_outbox.py      migration

REPLACED
  backend/services/notify.py                         bell + email in one call

PATCHED
  backend/email_smtp.py                               From display name + Reply-To
  backend/models/__init__.py                          export EmailOutbox
  backend/routers/crm/__init__.py                     register the outbox router
  backend/main.py                                     start the outbox worker
  backend/routers/crm/timesheets.py                   submit/approve/reject notifications
  backend/routers/crm/leave_applications.py           submit notification + email decisions
  .env.example                                        document the new settings
"""

report: list[str] = []
CHECK_ONLY = False


def _read(path: str) -> str:
    with io.open(path, "r", encoding="utf-8") as fh:  # universal newlines -> \n
        return fh.read()


def _write(path: str, text: str) -> None:
    if CHECK_ONLY:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def copy_new(rel: str) -> None:
    src = os.path.join(NEWFILES_DIR, rel)
    if not os.path.exists(src):
        report.append(f"MISSING SOURCE  {rel}")
        return
    dst = os.path.join(".", rel)
    if os.path.exists(dst) and _read(dst) == _read(src):
        report.append(f"same            {rel}")
        return
    verb = "overwrite" if os.path.exists(dst) else "create"
    _write(dst, _read(src))
    report.append(f"{verb:<15} {rel}")


def patch(rel: str, old: str, new: str, *, marker: str, count: int = 1) -> None:
    """Replace `old` with `new` in `rel`. `marker` is a string that is present
    only after the patch has been applied — its presence means skip."""
    path = os.path.join(".", rel)
    if not os.path.exists(path):
        report.append(f"MISSING         {rel}")
        return
    text = _read(path)
    if marker in text:
        report.append(f"already applied {rel}  ({marker[:40]}...)")
        return
    found = text.count(old)
    if found != count:
        report.append(f"NO MATCH        {rel}  (expected {count}, found {found})")
        return
    _write(path, text.replace(old, new))
    report.append(f"patched         {rel}")


def append_once(rel: str, block: str, marker: str) -> None:
    path = os.path.join(".", rel)
    if not os.path.exists(path):
        report.append(f"MISSING         {rel}")
        return
    text = _read(path)
    if marker in text:
        report.append(f"already applied {rel}")
        return
    if not text.endswith("\n"):
        text += "\n"
    _write(path, text + block)
    report.append(f"appended        {rel}")


# ============================================================== new files

def apply_new_files() -> None:
    for rel in (
        "backend/models/email_outbox.py",
        "backend/services/recipients.py",
        "backend/services/email_outbox.py",
        "backend/services/notify.py",
        "backend/routers/crm/email_outbox.py",
        "backend/alembic/versions/0064_email_outbox.py",
    ):
        copy_new(rel)


# ============================================================== email_smtp.py

def patch_email_smtp() -> None:
    patch(
        "backend/email_smtp.py",
        '''def send_email(to_address: str, subject: str, body_text: str, body_html: str | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None) -> dict[str, Any]:
    """`attachments`: optional list of (filename, content_bytes, mime_type) —
    e.g. ("invite.ics", b"BEGIN:VCALENDAR...", "text/calendar")."""''',
        '''def send_email(to_address: str, subject: str, body_text: str, body_html: str | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None,
               from_name: str | None = None, reply_to: str | None = None,
               reply_to_name: str | None = None) -> dict[str, Any]:
    """`attachments`: optional list of (filename, content_bytes, mime_type) —
    e.g. ("invite.ics", b"BEGIN:VCALENDAR...", "text/calendar").

    `from_name` / `reply_to` shape the HEADERS only. The envelope sender stays
    SMTP_FROM so SPF and DKIM remain aligned with the tenant — Office 365
    rejects a From: of another mailbox with "5.7.60 Client does not have
    permissions to send as this sender" unless Send As has been granted.

    So a recruiter-triggered mail goes out as:
        From:     "Pavan Sanap (Karnex)" <SMTP_FROM>
        Reply-To: pavan.sanap@karnex.in
    The candidate sees who is handling them, and hitting reply reaches that
    person's own inbox rather than a shared one.
    """''',
        marker="from_name: str | None = None, reply_to: str | None = None",
    )

    patch(
        "backend/email_smtp.py",
        '''    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_address
''',
        '''    msg["Subject"] = subject
    display = (from_name or "").strip()
    if display:
        from email.utils import formataddr

        msg["From"] = formataddr((display, from_addr))
    else:
        msg["From"] = from_addr
    msg["To"] = to_address
    reply_addr = (reply_to or "").strip()
    if reply_addr and reply_addr.lower() != from_addr.lower():
        from email.utils import formataddr

        rname = (reply_to_name or "").strip()
        msg["Reply-To"] = formataddr((rname, reply_addr)) if rname else reply_addr
''',
        marker='msg["Reply-To"]',
    )


# =========================================================== models/__init__

def patch_models_init() -> None:
    patch(
        "backend/models/__init__.py",
        "from models.rbac import Notification, Role, RoleName, UserRole  # noqa: F401",
        "from models.rbac import Notification, Role, RoleName, UserRole  # noqa: F401\n"
        "from models.email_outbox import EmailOutbox, EmailStatus  # noqa: F401",
        marker="from models.email_outbox import",
    )


# ======================================================= routers/crm/__init__

def patch_router_registry() -> None:
    patch(
        "backend/routers/crm/__init__.py",
        '    "leave_policies", "leave_applications", "ai_assist",\n',
        '    "leave_policies", "leave_applications", "ai_assist", "email_outbox",\n',
        marker='"email_outbox"',
    )


# ==================================================================== main.py

def patch_main() -> None:
    patch(
        "backend/main.py",
        '''@app.on_event("startup")
def _start_interview_recovery_worker() -> None:''',
        '''@app.on_event("startup")
def _start_email_outbox_worker() -> None:
    """Drain the notification email outbox in the background.

    Kept separate from the interview-recovery worker so an SMTP problem can
    never stall interview crash recovery, and so it can be turned off on its own
    with EMAIL_OUTBOX_WORKER=false (e.g. when a cron drains the table instead).
    """
    try:
        from services.email_outbox import start_outbox_worker

        start_outbox_worker()
    except Exception as exc:  # never block boot on the notifier
        logger.warning("email.outbox.worker_start_failed: %s", exc)


@app.on_event("startup")
def _start_interview_recovery_worker() -> None:''',
        marker="_start_email_outbox_worker",
    )


# ================================================================ timesheets

def patch_timesheets() -> None:
    patch(
        "backend/routers/crm/timesheets.py",
        "from services.notify import notify_role, notify_user",
        "from services.notify import notify_employee, notify_role, notify_roles, notify_user",
        marker="notify_employee, notify_role, notify_roles",
    )
    # employee_display_name lives in services.timesheets but was not imported here.
    patch(
        "backend/routers/crm/timesheets.py",
        "    employee_for_user, entry_out, for_submission_report_rows,",
        "    employee_display_name, employee_for_user, entry_out, for_submission_report_rows,",
        marker="employee_display_name, employee_for_user",
    )

    # --- helper, inserted just above the workflow section -------------------
    patch(
        "backend/routers/crm/timesheets.py",
        '''# ---------------------------------------------------------------- workflow

@router.post("/{timesheet_id}/submit")''',
        '''# ---------------------------------------------------------------- workflow

#: Anyone in these roles can approve a timesheet — mirrors the dependency on
#: approve_timesheet / reject_timesheet below. Notifying only one of them would
#: leave the other two polling GET /api/timesheets/reports/approvals.
TS_APPROVER_ROLES = ("HR", "Finance", "RMG")


def _timesheet_context(db: Session, ts: Timesheet) -> tuple[Employee | None, str, list[tuple[str, str]]]:
    """(employee, period label, detail rows for the email body)."""
    emp = db.get(Employee, ts.employee_id) if ts.employee_id else None
    period = period_label(ts.year, ts.month)
    project = db.get(Project, ts.project_id) if getattr(ts, "project_id", None) else None
    rows: list[tuple[str, str]] = [
        ("Employee", employee_display_name(emp) if emp else f"#{ts.employee_id}"),
        ("Period", period),
    ]
    if project is not None:
        rows.append(("Project", getattr(project, "name", "") or f"#{project.id}"))
    billable = getattr(ts, "total_billable_days", None)
    if billable is not None:
        rows.append(("Billable days", str(billable)))
    return emp, period, rows


@router.post("/{timesheet_id}/submit")''',
        marker="TS_APPROVER_ROLES",
    )

    # --- submit -------------------------------------------------------------
    patch(
        "backend/routers/crm/timesheets.py",
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_SUBMITTED",
                 f"Timesheet for {period_label(ts.year, ts.month)} submitted for approval")
    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_out(ts), message="Timesheet submitted")''',
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_SUBMITTED",
                 f"Timesheet for {period_label(ts.year, ts.month)} submitted for approval")

    # Tell the approvers. Until now submit notified nobody at all, so an
    # approver only found out by opening the approvals report — and invoicing
    # the customer sits behind this approval.
    emp, period, rows = _timesheet_context(db, ts)
    who = employee_display_name(emp) if emp else f"Employee #{ts.employee_id}"
    notify_roles(
        db, TS_APPROVER_ROLES,
        f"Timesheet submitted for approval — {who}",
        f"{who} submitted their timesheet for {period}. It is waiting for your approval.",
        f"timesheets/{ts.id}",
        exclude_user_id=user.id,
        actor=user,
        event="timesheet.submitted",
        rows=rows,
        dedupe_prefix=f"timesheet.submitted:{ts.id}:{ts.submitted_at.isoformat() if ts.submitted_at else ''}",
    )

    db.commit()
    db.refresh(ts)
    return envelope(data=timesheet_out(ts), message="Timesheet submitted")''',
        marker="timesheet.submitted",
    )

    # --- approve ------------------------------------------------------------
    patch(
        "backend/routers/crm/timesheets.py",
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_APPROVED",
                 f"Timesheet approved{comp_off_note}")
    db.commit()''',
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_APPROVED",
                 f"Timesheet approved{comp_off_note}")

    # The employee is the one waiting on this, and is often the one person who
    # cannot see a bell — employees.user_id is nullable, so contractors and new
    # joiners have no login at all. notify_employee emails them regardless.
    emp, period, rows = _timesheet_context(db, ts)
    notify_employee(
        db, emp,
        f"Timesheet approved — {period}",
        f"Your timesheet for {period} was approved{comp_off_note}.",
        f"timesheets/{ts.id}",
        actor=user,
        event="timesheet.approved",
        rows=rows,
        dedupe_key=f"timesheet.approved:{ts.id}",
    )

    db.commit()''',
        marker="timesheet.approved",
    )

    # --- reject -------------------------------------------------------------
    patch(
        "backend/routers/crm/timesheets.py",
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_REJECTED",
                 f"Timesheet rejected: {reason}")
    db.commit()''',
        '''    log_activity(db, TimesheetActivityLog, "timesheet_id", ts.id, user.id, "TS_REJECTED",
                 f"Timesheet rejected: {reason}")

    # Rejection silently reverses the employee's leave and comp-off ledger
    # (reverse_timesheet_ledger_effects above), so telling them matters twice
    # over: they must resubmit, and their balances just moved.
    emp, period, rows = _timesheet_context(db, ts)
    notify_employee(
        db, emp,
        f"Timesheet rejected — {period}",
        f"Your timesheet for {period} was rejected: {reason}. "
        f"Please correct it and submit again.",
        f"timesheets/{ts.id}",
        actor=user,
        event="timesheet.rejected",
        rows=rows + [("Reason", reason)],
        dedupe_key=f"timesheet.rejected:{ts.id}:{len(reason)}",
    )

    db.commit()''',
        marker="timesheet.rejected",
    )


# =========================================================== leave applications

def patch_leave() -> None:
    patch(
        "backend/routers/crm/leave_applications.py",
        "from services.notify import notify_user",
        "from services.notify import notify_employee, notify_role, notify_user",
        marker="notify_employee, notify_role, notify_user",
    )

    # --- notify HR on submit ------------------------------------------------
    patch(
        "backend/routers/crm/leave_applications.py",
        '''    db.add(app)
    db.commit()
    db.refresh(app)
    return envelope(data=_app_out(db, app), message="Leave application submitted")''',
        '''    db.add(app)
    db.commit()
    db.refresh(app)

    # Approve and reject already notified the employee; submit notified nobody,
    # so an application could sit in Pending with no HR user aware of it.
    notify_role(
        db, "HR",
        f"Leave application — {employee_display_name(employee)}",
        f"{employee_display_name(employee)} applied for {days} day(s) of "
        f"{leave_type.name} from {body.from_date} to {to_date}.",
        f"leave-applications/{app.id}",
        exclude_user_id=user.id,
        actor=user,
        event="leave.submitted",
        rows=[
            ("Employee", employee_display_name(employee)),
            ("Leave type", leave_type.name),
            ("From", str(body.from_date)),
            ("To", str(to_date)),
            ("Days", str(days)),
            ("Reason", body.reason or ""),
        ],
        dedupe_prefix=f"leave.submitted:{app.id}",
    )
    db.commit()

    return envelope(data=_app_out(db, app), message="Leave application submitted")''',
        marker="leave.submitted",
    )

    # --- approve / reject: email as well as bell ----------------------------
    patch(
        "backend/routers/crm/leave_applications.py",
        '''def _notify_employee(db: Session, app: LeaveApplication, title: str, message: str) -> None:
    emp = db.get(Employee, app.employee_id)
    if emp and emp.user_id:
        notify_user(db, emp.user_id, title, message, f"/leave-applications/{app.id}")''',
        '''def _notify_employee(db: Session, app: LeaveApplication, title: str, message: str,
                     actor=None, event: str = "leave.decision") -> None:
    """Bell where possible, email always.

    Email is the channel that actually matters here: someone whose leave was
    just approved is, by definition, often not in the app — and an employee with
    no login account (employees.user_id IS NULL) could never see the bell at all,
    which is exactly the population this used to drop on the floor.
    """
    emp = db.get(Employee, app.employee_id)
    if emp is None:
        return
    leave_type = db.get(LeavePolicyType, app.leave_type_id)
    notify_employee(
        db, emp, title, message, f"leave-applications/{app.id}",
        actor=actor,
        event=event,
        rows=[
            ("Leave type", getattr(leave_type, "name", "") or ""),
            ("From", str(app.from_date)),
            ("To", str(app.to_date)),
            ("Days", str(app.days)),
            ("Status", str(app.status)),
        ],
        dedupe_key=f"{event}:{app.id}:{app.status}",
    )''',
        marker="Bell where possible, email always",
    )


# =============================================================== .env.example

ENV_BLOCK = """
# ---------------------------------------------------------------- Notifications
# Master switch for outbound notification email (approvals, timesheets, leave).
# Overrides the `email_notifications_enabled` app setting when set. Per-event
# opt-out lives in app_settings as `email_event_<event>` = false, e.g.
# `email_event_timesheet.submitted`.
EMAIL_NOTIFICATIONS_ENABLED=true
# Background worker that drains the email_outbox table. Set false if a separate
# process or cron calls POST /api/email-outbox/drain instead.
EMAIL_OUTBOX_WORKER=true
# Seconds between drain passes, and how many messages a pass sends.
EMAIL_OUTBOX_INTERVAL_SEC=20
EMAIL_OUTBOX_BATCH=25
"""


def patch_env_example() -> None:
    append_once(".env.example", ENV_BLOCK.lstrip("\n"), "EMAIL_NOTIFICATIONS_ENABLED")


# ======================================================================= main

def main() -> int:
    global CHECK_ONLY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report only, change nothing")
    args = parser.parse_args()
    CHECK_ONLY = args.check

    if not os.path.isdir("backend") or not os.path.isfile("backend/main.py"):
        print("ERROR: run this from the AI-Interview-Model-B-V2 repo root "
              "(the folder containing backend/).", file=sys.stderr)
        return 2

    apply_new_files()
    patch_email_smtp()
    patch_models_init()
    patch_router_registry()
    patch_main()
    patch_timesheets()
    patch_leave()
    patch_env_example()

    print(PLAN)
    print("=" * 66)
    print("CHECK ONLY — nothing written\n" if CHECK_ONLY else "APPLIED\n")
    for line in report:
        print("  " + line)

    problems = [r for r in report if r.startswith(("NO MATCH", "MISSING"))]
    print()
    if problems:
        print(f"{len(problems)} item(s) need attention — see NO MATCH / MISSING above.")
        print("Most likely cause: that file was edited since this bundle was built.")
        return 1

    print("All edits applied cleanly.")
    print("\nNext:")
    print("  1. cd backend && alembic upgrade head        # creates email_outbox (0064)")
    print("  2. restart the backend                       # starts the outbox worker")
    print("  3. python scripts/test_smtp.py --check       # confirm SMTP still authenticates")
    print("  4. submit a timesheet and watch GET /api/email-outbox/stats")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
