"""Test the email-notification system without touching real business data.

Run this BEFORE submitting a real timesheet. It answers, in order:
  - is SMTP configured, and is the outbox table there?
  - WHO would actually receive a role notification, by name and address?
  - does one real email reach an inbox, with the right From and Reply-To?
  - what is sitting in the outbox right now, and why did anything fail?

Nothing here writes to a business table. `--send` inserts one row in
email_outbox and delivers it; `--purge-tests` removes those rows afterwards.

Usage (from the backend/ directory):
    python scripts/test_email_notifications.py --check
    python scripts/test_email_notifications.py --who HR Finance RMG
    python scripts/test_email_notifications.py --preview-timesheet 412
    python scripts/test_email_notifications.py --send you@karnex.in
    python scripts/test_email_notifications.py --outbox
    python scripts/test_email_notifications.py --outbox --status Failed
    python scripts/test_email_notifications.py --drain
    python scripts/test_email_notifications.py --purge-tests
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _load_env() -> None:
    """Same loader main.py uses — the repo has no python-dotenv dependency."""
    env_path = BACKEND.parent / ".env"
    if not env_path.exists():
        print(f"WARNING: no .env at {env_path}")
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env()

import sqlalchemy as sa  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from email_smtp import smtp_configured, smtp_enabled  # noqa: E402
from models import EmailOutbox, EmailStatus, Employee, Timesheet  # noqa: E402
from services.email_outbox import (  # noqa: E402
    app_url, drain_once, event_enabled, notifications_enabled, queue_email,
    render_html, render_text,
)
from services.recipients import (  # noqa: E402
    employee_display_name, employee_recipient, reporting_manager_recipient, roles_recipients,
)

TEST_EVENT = "test.notification"
OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"


def _session():
    return get_session_factory()()


# ------------------------------------------------------------------- --check


def cmd_check() -> int:
    print("\n=== Configuration ===")
    problems = 0

    print(f"{OK if smtp_enabled() else BAD} SMTP_ENABLED = {os.getenv('SMTP_ENABLED')}")
    problems += 0 if smtp_enabled() else 1
    print(f"{OK if smtp_configured() else BAD} SMTP host/user/password all present")
    problems += 0 if smtp_configured() else 1
    print(f"         SMTP_HOST = {os.getenv('SMTP_HOST')}  PORT = {os.getenv('SMTP_PORT') or '587'}")
    print(f"         SMTP_USER = {os.getenv('SMTP_USER')}")
    print(f"         SMTP_FROM = {os.getenv('SMTP_FROM') or os.getenv('SMTP_USER')} "
          f"(envelope sender — stays fixed so SPF/DKIM stay aligned)")

    base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    link = app_url("timesheets/1")
    if not link:
        print(f"{WARN} PUBLIC_BASE_URL unset — emails will have no 'Open in Karnex' link")
    elif any(p in base for p in ("192.168.", "10.", "127.0.0.1", "localhost", "172.16.")):
        print(f"{WARN} PUBLIC_BASE_URL is a LAN address ({base})")
        print("         Links in emails will not work outside the office.")
    else:
        print(f"{OK} PUBLIC_BASE_URL = {base}")
    print(f"         example link: {link or '(none)'}")

    worker = (os.getenv("EMAIL_OUTBOX_WORKER") or "true").strip().lower()
    if worker in {"0", "false", "no", "off"}:
        print(f"{WARN} EMAIL_OUTBOX_WORKER = {worker} — QUEUE-ONLY MODE")
        print("         Nothing sends automatically. This is the safe way to test:")
        print("         submit a timesheet, inspect --outbox, then --drain when happy.")
    else:
        print(f"{OK} EMAIL_OUTBOX_WORKER on — the app sends every "
              f"{os.getenv('EMAIL_OUTBOX_INTERVAL_SEC') or '20'}s")

    print("\n=== Database ===")
    try:
        with _session() as db:
            exists = db.execute(sa.text("SELECT to_regclass('public.email_outbox')")).scalar()
            if exists:
                total = db.execute(sa.select(sa.func.count(EmailOutbox.id))).scalar_one()
                print(f"{OK} email_outbox table present ({total} rows)")
            else:
                print(f"{BAD} email_outbox table MISSING — run: alembic upgrade head")
                problems += 1
            rev = db.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
            print(f"{OK if rev == '0064' else WARN} alembic head = {rev} (expected 0064)")
            print(f"{OK if notifications_enabled(db) else BAD} notifications enabled "
                  f"(master switch)")
            for ev in ("timesheet.submitted", "timesheet.approved", "timesheet.rejected",
                       "leave.submitted", "leave.decision"):
                state = "on" if event_enabled(db, ev) else "OFF"
                print(f"{OK if state == 'on' else WARN} event {ev:<24} {state}")
    except Exception as exc:
        print(f"{BAD} cannot reach the CRM database: {exc}")
        problems += 1

    print()
    if problems:
        print(f"{problems} blocking problem(s). Fix these before testing further.")
    else:
        print("Config looks good. Next: --who HR Finance RMG")
    return 1 if problems else 0


# --------------------------------------------------------------------- --who


def cmd_who(role_names: list[str]) -> int:
    """Show exactly who would be emailed for a role notification — the check to
    run before the first real submit, so nobody is surprised."""
    with _session() as db:
        print(f"\n=== Recipients for roles: {', '.join(role_names)} ===")
        people = roles_recipients(db, role_names)
        if not people:
            print("  (nobody) — either no user holds these roles, or every holder is inactive")
            print("  A timesheet submit would queue zero emails.")
            return 1
        for r in sorted(people, key=lambda p: (p.name or "").lower()):
            print(f"  {(r.name or '(no name)'):<30} {r.email:<38} user_id={r.user_id}")
        print(f"\n  {len(people)} recipient(s). Each timesheet submit sends this many emails.")
        print("  (The submitter is excluded from their own notification.)")
    return 0


# ------------------------------------------------- --preview-timesheet <id>


def cmd_preview_timesheet(ts_id: int) -> int:
    """Render the exact email a submit would produce for a real timesheet,
    WITHOUT queuing or sending anything."""
    with _session() as db:
        ts = db.get(Timesheet, ts_id)
        if ts is None:
            print(f"No timesheet #{ts_id}")
            return 1
        emp = db.get(Employee, ts.employee_id) if ts.employee_id else None
        who = employee_display_name(emp) if emp else f"Employee #{ts.employee_id}"
        period = f"{ts.year}-{ts.month:02d}"
        rows = [("Employee", who), ("Period", period), ("Status", str(ts.status))]

        print("\n=== Approvers who would be emailed ===")
        for r in roles_recipients(db, ("HR", "Finance", "RMG")):
            print(f"  {(r.name or '(no name)'):<30} {r.email}")

        print("\n=== The employee, for approve/reject ===")
        er = employee_recipient(emp)
        print(f"  {er.email if er else '(no usable address)'}"
              f"{'   <- employees.email, works even with no login' if er else ''}")
        mgr = reporting_manager_recipient(db, emp)
        print(f"  reporting manager: {mgr.email if mgr else '(not set — role fallback is used)'}")

        print("\n=== Email body it would send ===")
        print("-" * 66)
        print(render_text(
            f"Timesheet submitted for approval — {who}",
            f"{who} submitted their timesheet for {period}. It is waiting for your approval.",
            rows=rows, action_label="Open in Karnex", action_url=app_url(f"timesheets/{ts.id}")))
        print("-" * 66)
    return 0


# -------------------------------------------------------------------- --send


def cmd_send(address: str) -> int:
    """Queue one real email and deliver it now. Proves the whole path end to
    end: queue -> worker claim -> SMTP -> inbox, including the header identity."""
    from datetime import datetime, timezone

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

    class _Actor:
        full_name = "Karnex Test"
        email = (os.getenv("SMTP_USER") or "").strip()
        username = "test"

    rows = [("Sent at", stamp), ("Test", "email notification pipeline")]
    with _session() as db:
        row = queue_email(
            db,
            to_email=address,
            to_name="Test recipient",
            subject=f"Karnex notification test — {stamp}",
            body_text=render_text("Karnex notification test",
                                  "If you are reading this, the outbox pipeline works.",
                                  rows=rows),
            body_html=render_html("Karnex notification test",
                                  "If you are reading this, the outbox pipeline works.",
                                  rows=rows),
            event=TEST_EVENT,
            actor=_Actor(),
        )
        if row is None:
            print(f"{BAD} nothing queued. Either the address is unusable, or "
                  f"notifications are disabled (--check will say which).")
            return 1
        db.commit()
        print(f"{OK} queued outbox row for {address}")

    print("  draining now...")
    result = drain_once(limit=5)
    print(f"  {result}")

    with _session() as db:
        latest = db.execute(
            sa.select(EmailOutbox).where(EmailOutbox.event == TEST_EVENT)
            .order_by(EmailOutbox.id.desc()).limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return 1
        status = latest.status.value if hasattr(latest.status, "value") else str(latest.status)
        if status == "Sent":
            print(f"\n{OK} SENT. Check {address}. The message should show:")
            print(f"         From:     \"{latest.from_name}\" "
                  f"<{os.getenv('SMTP_FROM') or os.getenv('SMTP_USER')}>")
            print(f"         Reply-To: {latest.reply_to_email}")
            print("       Hit reply in your mail client and confirm it addresses the Reply-To.")
            return 0
        print(f"\n{BAD} status={status} attempts={latest.attempts}")
        print(f"       error: {latest.last_error}")
        print("       Common causes: O365 SMTP AUTH disabled for the mailbox, "
              "MFA without an App Password, or the password was rotated.")
        return 1


# ------------------------------------------------------------------ --outbox


def cmd_outbox(status: str | None, limit: int) -> int:
    with _session() as db:
        stmt = sa.select(EmailOutbox).order_by(EmailOutbox.id.desc()).limit(limit)
        if status:
            stmt = stmt.where(EmailOutbox.status == EmailStatus(status))
        rows = db.execute(stmt).scalars().all()

        counts = dict(db.execute(
            sa.select(EmailOutbox.status, sa.func.count(EmailOutbox.id))
            .group_by(EmailOutbox.status)
        ).all())
        print("\n=== Outbox totals ===")
        for st in EmailStatus:
            print(f"  {st.value:<9} {counts.get(st, 0)}")

        print(f"\n=== Last {len(rows)} row(s){' with status ' + status if status else ''} ===")
        if not rows:
            print("  (none)")
            print("\n  If you expected rows here: the notification is queued in the SAME")
            print("  transaction as the business change, so a failed submit queues nothing.")
            return 0
        for r in rows:
            st = r.status.value if hasattr(r.status, "value") else str(r.status)
            print(f"  #{r.id:<5} {st:<8} {r.event:<24} -> {r.to_email:<34} "
                  f"attempts={r.attempts}")
            if r.last_error:
                print(f"         error: {r.last_error[:150]}")
    return 0


# ------------------------------------------------------- --drain / --purge


def cmd_drain(limit: int) -> int:
    print(f"\nDraining up to {limit} due message(s)...")
    print(f"  {drain_once(limit=limit)}")
    return 0


def cmd_purge_tests() -> int:
    with _session() as db:
        n = db.execute(
            sa.delete(EmailOutbox).where(EmailOutbox.event == TEST_EVENT)
        ).rowcount
        db.commit()
    print(f"Removed {n} test row(s) (event={TEST_EVENT}). Real notifications untouched.")
    return 0


# --------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="config, table and switches")
    p.add_argument("--who", nargs="+", metavar="ROLE",
                   help="who would receive a role notification, e.g. --who HR Finance RMG")
    p.add_argument("--preview-timesheet", type=int, metavar="ID",
                   help="render the submit email for a real timesheet, without sending")
    p.add_argument("--send", metavar="ADDRESS", help="queue and deliver one real test email")
    p.add_argument("--outbox", action="store_true", help="show recent outbox rows")
    p.add_argument("--status", choices=[s.value for s in EmailStatus],
                   help="filter --outbox by status")
    p.add_argument("--drain", action="store_true", help="send due messages now")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--purge-tests", action="store_true", help="delete rows queued by --send")
    args = p.parse_args()

    if args.check:
        return cmd_check()
    if args.who:
        return cmd_who(args.who)
    if args.preview_timesheet:
        return cmd_preview_timesheet(args.preview_timesheet)
    if args.send:
        return cmd_send(args.send)
    if args.drain:
        return cmd_drain(args.limit)
    if args.purge_tests:
        return cmd_purge_tests()
    if args.outbox or args.status:
        return cmd_outbox(args.status, args.limit)

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
