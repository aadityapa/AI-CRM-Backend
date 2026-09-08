"""Daily background jobs — the app acting on what it already knows.

Until now this application had no scheduler. Everything it noticed, it noticed
only when a human opened the right screen: the timesheet-due report needed HR
to press a button, the PO-expiry report warned nobody, and `recurring_billing`
on a project did nothing at all. This module closes that gap with three daily
jobs.

FOUR DESIGN RULES, each learned from how this kind of thing usually goes wrong:

1. **Exactly-once, provably.** Every message carries a dedupe key naming the
   exact milestone (``po.expiry:41:15`` = PO 41, fifteen days out). The email
   outbox enforces uniqueness on that key, so a restart, a double run, or two
   app instances cannot produce two emails. Correctness does not depend on the
   scheduler running exactly once.

2. **Catch-up, not clockwork.** The loop checks "has today's run happened
   yet?" instead of firing at an instant. A server that was off at 08:00 and
   starts at 11:00 still runs today's jobs — a fixed-time trigger would skip
   the day silently.

3. **Never break the app.** Every job is wrapped: a failure is logged and the
   other jobs still run. The thread cannot die, and a scheduler problem can
   never take a request path with it.

4. **Admin-visible and admin-controlled.** Each job has an on/off switch and
   the run hour is configurable (Settings), the recipients are the same
   admin-editable Email Flows as everything else, and each run records its
   outcome so the Settings page can prove the scheduler is alive.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

logger = logging.getLogger("karnex.scheduler")

# ---------------------------------------------------------------- settings

#: Defaults live here; Settings -> Organisation overrides them at runtime.
DEFAULTS = {
    "scheduler.enabled": "true",
    "scheduler.run_hour": "8",                       # local hour, 0-23
    "scheduler.timesheet_reminders": "true",
    "scheduler.timesheet_reminder_days": "1,3,5,7",  # days of month, for the PREVIOUS month
    "scheduler.po_expiry": "true",
    #: Days BEFORE expiry that trigger a notice (0 = the expiry day itself).
    "scheduler.po_expiry_days": "45,30,15,5,1,0",
    #: Days AFTER expiry that trigger a notice. Stops after the last one: a PO
    #: 90 days expired needs renewing or cancelling, not more email.
    "scheduler.po_expiry_overdue_days": "1,7,14,30,60,90",
    "scheduler.recurring_invoices": "true",
    "scheduler.pe_leave_credit": "true",
    #: How far back the leave job will repair a month it never ran. A system
    #: that has been off longer than this should be backfilled deliberately
    #: (`run_pe_leave_credit.py --from`), not quietly by a daily job.
    "scheduler.pe_leave_credit_lookback": "12",
}

LAST_RUN_PREFIX = "scheduler.last_run."


def _setting(key: str) -> str:
    """Settings row -> env -> DEFAULTS. Never raises."""
    default = DEFAULTS.get(key, "")
    try:
        from services.org_settings import setting

        value = setting(key, default=default)
        return value if value else default
    except Exception:
        return default


def _flag(key: str) -> bool:
    return _setting(key).strip().lower() in ("1", "true", "yes", "on")


def _int_list(key: str) -> list[int]:
    out: list[int] = []
    for part in _setting(key).split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


def _now_local() -> datetime:
    return datetime.now()


def _record_run(db, job: str, summary: dict) -> None:
    """Store the outcome in app_settings so the admin UI can show liveness.
    Value is capped: the column is VARCHAR(255) and truth beats detail here."""
    from models import AppSetting

    payload = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                          **summary})[:255]
    key = LAST_RUN_PREFIX + job
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=payload, description=f"Last {job} scheduler run"))
    else:
        row.value = payload
    db.commit()


def _last_run_date(db, job: str) -> date | None:
    from models import AppSetting

    row = db.get(AppSetting, LAST_RUN_PREFIX + job)
    if row is None or not row.value:
        return None
    try:
        stamp = json.loads(row.value).get("at")
        return datetime.fromisoformat(stamp).astimezone().date() if stamp else None
    except Exception:
        return None


# ------------------------------------------------------------------- job 1
# Timesheet due reminders


def run_timesheet_reminders(db, *, today: date | None = None) -> dict:
    """Remind employees whose PREVIOUS-month timesheet is still not submitted,
    and send the approvers one digest.

    Runs only on the configured days of the month (default 1st, 3rd, 5th, 7th)
    so a late employee gets a few nudges, not thirty.
    """
    from services.notify import notify_employee, notify_roles
    from services.timesheets import _due_rows, period_label

    today = today or date.today()
    if today.day not in _int_list("scheduler.timesheet_reminder_days"):
        return {"skipped": "not a reminder day"}

    # The month being chased is the one that just ended.
    first_of_this = today.replace(day=1)
    target = first_of_this - timedelta(days=1)
    month, year = target.month, target.year
    label = period_label(year, month)

    rows = _due_rows(db, month, year)
    reminded = 0
    from models import Employee

    for row in rows:
        emp = db.get(Employee, row["employee_id"])
        if emp is None:
            continue
        project_label = row["project_title"] or f"project #{row['project_id']}"
        # notify_employee reaches people with no login account too, which is
        # exactly the population most likely to forget a timesheet.
        if notify_employee(
            db, emp,
            f"Timesheet due for {label} — {project_label}",
            f"Your timesheet for {label} on {project_label} is {row['status'].lower()}. "
            f"Please complete and submit it.",
            "/timesheets",
            event="timesheet.due_reminder",
            dedupe_key=f"timesheet.due:{row.get('project_employee_id') or row['employee_id']}"
                       f":{year}-{month:02d}:{today.day}",
        ):
            reminded += 1

    if rows:
        notify_roles(
            db, ["HR", "RMG"],
            f"{len(rows)} timesheet(s) still due for {label}",
            "These assignments have no submitted timesheet yet:\n"
            + "\n".join(
                f"• {r.get('employee_name') or r.get('project_employee_name') or r['employee_id']}"
                f" — {r['project_title'] or r['project_id']} ({r['status']})"
                for r in rows[:40]
            )
            + (f"\n… and {len(rows) - 40} more" if len(rows) > 40 else ""),
            "/timesheets",
            event="timesheet.due_digest",
            dedupe_prefix=f"timesheet.due_digest:{year}-{month:02d}:{today.day}",
        )
    db.commit()
    return {"period": f"{year}-{month:02d}", "due": len(rows), "reminded": reminded}


# ------------------------------------------------------------------- job 2
# Purchase-order expiry notices


def po_expiry_milestone(days_left: int) -> str | None:
    """Which milestone (if any) today's distance from expiry represents.

    ``days_left`` is ``end_date - today``: positive before expiry, 0 on the
    day, negative after. Returns a stable milestone id used in the dedupe key,
    so each milestone can only ever produce one email per PO.
    """
    if days_left >= 0:
        return f"T-{days_left}" if days_left in _int_list("scheduler.po_expiry_days") else None
    overdue = -days_left
    return f"T+{overdue}" if overdue in _int_list("scheduler.po_expiry_overdue_days") else None


def run_po_expiry_notices(db, *, today: date | None = None) -> dict:
    """Warn about POs approaching expiry, on expiry day, and while overdue."""
    from models import Customer, POStatus, PurchaseOrder
    from services.notify import notify_roles

    today = today or date.today()
    before = _int_list("scheduler.po_expiry_days") or [0]
    after = _int_list("scheduler.po_expiry_overdue_days") or [0]
    # Only look at POs that could possibly hit a milestone today.
    horizon_future = today + timedelta(days=max(before) if before else 0)
    horizon_past = today - timedelta(days=max(after) if after else 0)

    rows = db.execute(
        select(PurchaseOrder, Customer.name)
        .join(Customer, Customer.id == PurchaseOrder.customer_id, isouter=True)
        .where(
            PurchaseOrder.status == POStatus.ACTIVE,
            PurchaseOrder.end_date.is_not(None),
            PurchaseOrder.end_date <= horizon_future,
            PurchaseOrder.end_date >= horizon_past,
        )
        .order_by(PurchaseOrder.end_date)
    ).all()

    sent = 0
    for po, customer_name in rows:
        days_left = (po.end_date - today).days
        milestone = po_expiry_milestone(days_left)
        if milestone is None:
            continue
        balance = float(po.balance_value or 0)
        if days_left > 0:
            headline = f"PO {po.po_number} expires in {days_left} day{'s' if days_left != 1 else ''}"
            urgency = f"It expires on {po.end_date.strftime('%d %b %Y')}."
        elif days_left == 0:
            headline = f"PO {po.po_number} expires TODAY"
            urgency = "Today is the last day of this purchase order."
        else:
            headline = f"PO {po.po_number} expired {-days_left} day{'s' if days_left != -1 else ''} ago"
            urgency = (f"It expired on {po.end_date.strftime('%d %b %Y')} and is still Active. "
                       f"Renew it, or cancel it if the work has ended.")
        notify_roles(
            db, ["Finance", "Sales_Head"],
            headline,
            f"{customer_name or 'Customer'} — {urgency} "
            f"Remaining balance {balance:,.2f} of {float(po.total_value or 0):,.2f}.",
            f"/pos/{po.id}",
            event="po.expiry_warning",
            # The milestone is IN the key: 45/30/15/5/1/0 and each overdue
            # milestone are separate one-time messages for this PO, forever.
            dedupe_prefix=f"po.expiry:{po.id}:{milestone}",
        )
        sent += 1
    db.commit()
    return {"considered": len(rows), "notices": sent}


# ------------------------------------------------------------------- job 3
# Recurring invoice drafts


def run_recurring_invoices(db, *, today: date | None = None) -> dict:
    """Raise invoices automatically for approved timesheets on projects flagged
    ``recurring_billing``.

    Deliberately conservative about the PO. The PO selection gate exists so a
    human names the funding PO; this job therefore only auto-invoices when the
    PO is UNAMBIGUOUS (an existing allocation, or exactly one live candidate).
    When it is ambiguous the invoice is NOT guessed — Finance is told there is
    work waiting. Automation should never quietly spend the wrong budget.
    """
    from decimal import Decimal

    from models import Invoice, InvoiceLine, PaymentStatus, Project, PurchaseOrder, Timesheet, TimesheetStatus
    from services.crm_common import next_sequence_number
    from services.finance import (
        assert_po_allows_new_drawdown, karnex_gst_tax_and_grand,
        log_invoice_created_on_po, resolve_or_create_po_allocation_for_project,
    )
    from services.notify import notify_roles
    from services import tax
    from services.timesheets import linked_invoice_for, timesheet_invoice_preview

    today = today or date.today()
    project_ids = [
        p.id for p in db.execute(
            select(Project).where(Project.recurring_billing.is_(True))
        ).scalars()
    ]
    if not project_ids:
        return {"projects": 0, "created": 0}

    sheets = db.execute(
        select(Timesheet).where(
            Timesheet.project_id.in_(project_ids),
            Timesheet.status == TimesheetStatus.APPROVED,
        ).order_by(Timesheet.id)
    ).scalars().all()

    created, skipped = 0, []
    for ts in sheets:
        if linked_invoice_for(db, ts) is not None:
            continue
        try:
            alloc = resolve_or_create_po_allocation_for_project(db, ts.project_id)
            if alloc is None:
                skipped.append((ts, "no unambiguous PO — pick one manually"))
                continue
            po = db.get(PurchaseOrder, alloc.po_id)
            assert_po_allows_new_drawdown(po, action="auto-generate invoice")

            preview = timesheet_invoice_preview(db, ts)
            sub_total = Decimal(str(preview["totals"]["sub_total"]))
            if sub_total <= 0:
                skipped.append((ts, "nothing billable"))
                continue
            lines = [
                InvoiceLine(
                    s_no=int(li.get("s_no") or i),
                    description=str(li.get("description") or ""),
                    qty=Decimal(str(li.get("total_billed_qty") or 0)),
                    rate=Decimal(str(li.get("rate_per_unit") or 0)),
                    amount=Decimal(str(li.get("amount") or 0)),
                )
                for i, li in enumerate(preview["line_items"], start=1)
            ]
            tax_amount, grand_total, _gst = karnex_gst_tax_and_grand(
                db, po=po, project_id=ts.project_id, lines=lines, sub_total=sub_total,
            )
            if po is not None and Decimal(str(po.balance_value)) < grand_total:
                skipped.append((ts, "PO balance insufficient"))
                continue

            credit_days = 30
            if po is not None and po.payment_terms:
                import re as _re
                m = _re.search(r"(\d+)", str(po.payment_terms))
                if m:
                    credit_days = max(0, min(int(m.group(1)), 365))
            number = next_sequence_number(db, Invoice, Invoice.invoice_number, "INV")
            invoice = Invoice(
                invoice_number=number,
                po_id=po.id if po is not None else None,
                project_id=ts.project_id,
                timesheet_id=ts.id,
                invoice_date=today,
                due_date=today + timedelta(days=credit_days),
                sub_total=sub_total,
                tax_amount=tax_amount,
                grand_total=grand_total,
                paid_amount=Decimal("0"),
                balance_amount=grand_total,
                payment_status=PaymentStatus.UNPAID,
                lines=lines,
            )
            db.add(invoice)
            if po is not None:
                tax.apply_po_consumption(po, grand_total)
                alloc.consumed_amount = Decimal(str(alloc.consumed_amount)) + grand_total
            db.flush()
            log_invoice_created_on_po(db, po, invoice, None)
            notify_roles(
                db, ["Finance"],
                f"Invoice {number} auto-generated for {ts.year}-{ts.month:02d}",
                f"Recurring billing raised {number} ({float(grand_total):,.2f}) from the approved "
                f"timesheet for project #{ts.project_id}, employee #{ts.employee_id}.",
                f"/invoices/{invoice.id}",
                event="invoice.auto_drafted",
                dedupe_prefix=f"invoice.auto:{ts.id}",
            )
            db.commit()
            created += 1
        except Exception as exc:
            db.rollback()
            skipped.append((ts, str(exc)[:80]))
            logger.warning("scheduler.recurring_invoice_failed ts=%s: %s", ts.id, exc)

    if skipped:
        notify_roles(
            db, ["Finance"],
            f"{len(skipped)} approved timesheet(s) need manual invoicing",
            "Recurring billing could not raise these automatically:\n"
            + "\n".join(f"• Timesheet #{ts.id} ({ts.year}-{ts.month:02d}): {why}"
                        for ts, why in skipped[:30]),
            "/timesheets",
            event="invoice.auto_drafted",
            dedupe_prefix=f"invoice.auto_skipped:{today.isoformat()}",
        )
        db.commit()
    return {"projects": len(project_ids), "created": created, "needs_attention": len(skipped)}


# ------------------------------------------------------------------- job 4
# Project-Employee monthly leave credit


def run_pe_leave_credit_job(db, *, today: date | None = None) -> dict:
    """Credit this month's PE leave, and replay any month that never ran.

    Leave accrual used to be CLI-only (`scripts/run_pe_leave_credit.py`), which
    made it the one piece of business logic in the system that silently did
    nothing when nobody remembered to invoke it. Two failure modes followed
    from that, and this job closes both:

    * **A missed month stayed missed.** The credit function only ever credits
      the month of its ``as_of`` and never backfills, so a month with no run
      left every employee permanently short. Here, `missing_credit_periods`
      finds closed months with no ledger row and replays them at their month
      end before today's credit runs.
    * **A missed 31 December lost the year.** `apply_year_end_carry` acts on
      that single date, so a server that was down over new year skipped carry
      forward and expiry entirely, with no way to notice. Replaying December
      at its month end replays the carry with it.

    Replay is safe to run every day: credits are keyed
    ``pe_credit:{pe}:{type}:{YYYY-MM}``, so a month that already ran credits
    zero the second time. That is also why the alert fires on *balances moved*
    rather than on *months replayed* — a month where nothing was accruable
    looks identical to a month that never ran, and only one of them is worth
    waking somebody up for.
    """
    from services.notify import notify_roles
    from services.project_employee_leave_credit import (
        missing_credit_periods,
        run_pe_leave_credit,
        run_pe_leave_credit_backfill,
    )

    today = today or date.today()
    try:
        lookback = max(1, min(36, int(_setting("scheduler.pe_leave_credit_lookback") or 12)))
    except (TypeError, ValueError):
        lookback = 12

    repaired: dict = {"periods": [], "rows_credited": 0, "total_credited": 0.0}
    gaps = missing_credit_periods(db, as_of=today, lookback_months=lookback)
    if gaps:
        repaired = run_pe_leave_credit_backfill(db, periods=gaps)

    current = run_pe_leave_credit(db, as_of=today)

    # Only a repair that actually moved balances is worth an email.
    if repaired["rows_credited"]:
        periods = ", ".join(repaired["periods"])
        notify_roles(
            db, ["HR", "CEO"],
            "Leave accrual repaired a missed month",
            f"The monthly leave credit had not run for {periods}. "
            f"It has now been applied: {repaired['rows_credited']} balance row"
            f"{'s' if repaired['rows_credited'] != 1 else ''} credited, "
            f"{repaired['total_credited']:.2f} day"
            f"{'s' if repaired['total_credited'] != 1 else ''} in total. "
            "Balances are correct as of now; payslips or settlements already "
            "issued for those months may not be.",
            "/my-leave",
            event="leave.credit_repaired",
            # One message per set of repaired months, not per run.
            dedupe_prefix=f"leave.credit_repaired:{'_'.join(repaired['periods'])}",
        )
        db.commit()

    return {
        "as_of": current["as_of"],
        "project_employees": current["project_employees"],
        "rows_credited": current["rows_credited"],
        "total_credited": current["total_credited"],
        "repaired_periods": repaired["periods"],
        "repaired_rows": repaired["rows_credited"],
    }


# --------------------------------------------------------------------- runner

def run_rmg_screening_sla(db, *, today: date | None = None) -> dict:
    """Nudge RMG about applicants stuck in screening Pending past the SLA.

    One SUMMARY notification per day (dedupe key carries the date), never one
    per candidate — an SLA reminder that spams is the first thing people mute.
    Hours configurable via scheduler.rmg_screening_sla_hours (default 48).
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from models import Candidate, CandidateProfile, Opportunity
    from services.candidate_profiles import RMG_SCREENING_PENDING, applied_candidates_link, rmg_gate_enabled
    from services.notify import notify_role
    from services.org_settings import setting

    if not rmg_gate_enabled():
        return {"sent": 0, "reason": "gate disabled"}
    try:
        hours = int(setting("scheduler.rmg_screening_sla_hours") or "48")
    except ValueError:
        hours = 48
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(
        select(CandidateProfile, Candidate, Opportunity)
        .join(Candidate, Candidate.id == CandidateProfile.candidate_id)
        .join(Opportunity, Opportunity.id == CandidateProfile.opportunity_id)
        .where(
            CandidateProfile.rmg_screening_status == RMG_SCREENING_PENDING,
            func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at) < cutoff,
        )
        .order_by(func.coalesce(CandidateProfile.applied_on, CandidateProfile.created_at))
        .limit(50)
    ).all()
    if not rows:
        return {"sent": 0, "overdue": 0}

    def _name(c) -> str:
        return " ".join(p for p in (c.first_name, c.last_name) if p) or f"#{c.id}"

    lines = [f"{_name(c)} ({o.opp_id} — {o.title})" for _p, c, o in rows[:8]]
    more = f" and {len(rows) - 8} more" if len(rows) > 8 else ""
    notify_role(
        db, "RMG",
        f"{len(rows)} applicant(s) waiting on RMG screening > {hours}h",
        "; ".join(lines) + more + " — review and Shortlist/Reject so TAs can proceed.",
        applied_candidates_link(db, rows[0][0]),
        event="profile.rmg_screening_sla",
        dedupe_prefix=f"rmg_sla:{(today or date.today()).isoformat()}",
    )
    db.commit()
    return {"sent": 1, "overdue": len(rows)}


JOBS = {
    "timesheet_reminders": ("scheduler.timesheet_reminders", run_timesheet_reminders),
    "rmg_screening_sla": ("scheduler.rmg_screening_sla", run_rmg_screening_sla),
    "po_expiry": ("scheduler.po_expiry", run_po_expiry_notices),
    "recurring_invoices": ("scheduler.recurring_invoices", run_recurring_invoices),
    "pe_leave_credit": ("scheduler.pe_leave_credit", run_pe_leave_credit_job),
}


def run_due_jobs(*, force: bool = False, only: str | None = None) -> dict:
    """Run every job whose turn it is. Safe to call at any time, from anywhere.

    `force` ignores both the run-hour and the already-ran-today check, which is
    what the admin "Run now" button uses; the per-message dedupe keys mean even
    a forced run cannot duplicate anything already sent.
    """
    from crm_db import get_session_factory

    results: dict[str, dict] = {}
    if not force and not _flag("scheduler.enabled"):
        return {"skipped": "scheduler disabled"}

    try:
        run_hour = max(0, min(23, int(_setting("scheduler.run_hour") or 8)))
    except Exception:
        run_hour = 8
    now = _now_local()

    session = get_session_factory()()
    try:
        for job, (flag_key, fn) in JOBS.items():
            if only and job != only:
                continue
            if not force:
                if not _flag(flag_key):
                    continue
                if now.hour < run_hour:
                    continue
                if _last_run_date(session, job) == now.date():
                    continue
            try:
                summary = fn(session)
                results[job] = summary
                _record_run(session, job, summary)
                logger.info("scheduler.job_done job=%s %s", job, summary)
            except Exception as exc:
                session.rollback()
                results[job] = {"error": str(exc)[:200]}
                logger.warning("scheduler.job_failed job=%s: %s", job, exc, exc_info=True)
    finally:
        session.close()
    return results


def last_runs() -> dict:
    """What each job did last time — powers the Settings liveness panel."""
    from crm_db import get_session_factory
    from models import AppSetting

    out: dict[str, dict] = {}
    try:
        session = get_session_factory()()
        try:
            for job in JOBS:
                row = session.get(AppSetting, LAST_RUN_PREFIX + job)
                if row is not None and row.value:
                    try:
                        out[job] = json.loads(row.value)
                    except Exception:
                        out[job] = {"raw": row.value}
                else:
                    out[job] = {}
        finally:
            session.close()
    except Exception as exc:
        logger.debug("scheduler.last_runs_failed: %s", exc)
    return out


# ---------------------------------------------------------------------- thread

_STARTED = False
_LOCK = threading.Lock()


def _loop() -> None:
    # Checking every 15 minutes (not once a day) is what makes the catch-up
    # behaviour work after a restart or a machine that sleeps overnight.
    interval = max(60, min(3600, int(os.getenv("SCHEDULER_INTERVAL_SEC", "900") or "900")))
    # Let the app finish booting before the first pass.
    time.sleep(30)
    while True:
        try:
            run_due_jobs()
        except Exception as exc:  # pragma: no cover - the loop must never die
            logger.warning("scheduler.loop_error: %s", exc, exc_info=True)
        time.sleep(interval)


def start_scheduler() -> bool:
    """Start the daily-jobs thread once per process. SCHEDULER_WORKER=false
    disables it entirely (e.g. on a second app instance, so only one runs
    jobs — though the dedupe keys make even a double start harmless)."""
    global _STARTED
    if (os.getenv("SCHEDULER_WORKER", "true") or "").strip().lower() in ("0", "false", "no", "off"):
        logger.info("scheduler.worker_disabled")
        return False
    with _LOCK:
        if _STARTED:
            return False
        threading.Thread(target=_loop, name="karnex-scheduler", daemon=True).start()
        _STARTED = True
    logger.info("scheduler.worker_started")
    return True
