"""Seed Aptiv / Magna Steyr / Uno Minda leave scenarios into the live CRM DB.

Uses the app's normal CRM connection (CRM_DATABASE_URL / AUTH_DB_URL / DB_*),
NOT the isolated leave_scenario_test.db harness DB. Never wipes the database —
only find-or-create / upserts scenario entities and (re)plays leave credits
for those three project-employees.

Primary UI state after a successful run: Jul 2026 balances (post March leave):
  Aptiv/Amit   Casual 6.0
  Magna/Meera  Casual 1.0
  Uno/Uday     Casual 2.5, Sick 3.5, Earned 14.0

Usage (from backend/, venv active):
  python scripts/seed_leave_scenarios_crm.py
  python scripts/seed_leave_scenarios_crm.py --through-dec   # also advance to Dec-31 / 2027
  python scripts/seed_leave_scenarios_crm.py --force         # allow non-local hosts
  python scripts/seed_leave_scenarios_crm.py --reset         # clear scenario leave ledger
                                                            # + balances, then re-credit
"""
from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from crm_db import crm_database_url, get_session_factory
from models.customers import Customer, CustomerBranch, CustomerStatus
from models.hr import Employee, ProfileType
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent
from models.masters import LeavePolicyType
from models.opportunities import Opportunity, OppType, PipelineStage
from models.projects import Project, ProjectEmployee, ProjectEmployeeLeaveDetail, ProjectStatus
from services.project_employee_leave_credit import run_pe_leave_credit
from services.project_employees import consume_pe_leave, seed_leave_details_from_customer_policy

ZERO = Decimal("0")
D = Decimal
START = date(2026, 1, 1)
CONSUME_SOURCE_PREFIX = "timesheet:ls-"  # keep under LeaveAccrualEvent.source VARCHAR(64)

# Host substrings that look like hosted / production Postgres.
_PROD_HOST_MARKERS = (
    "supabase.co",
    "amazonaws.com",
    "neon.tech",
    "railway.app",
    "render.com",
    ".rds.",
    "azure.com",
    "digitalocean.com",
    "gcp.",
    "cloudsql",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

SCENARIOS = (
    {
        "customer": "Aptiv",
        "branch": "Aptiv HQ",
        "project": "Aptiv Project",
        "opp_id": "OPP-LEAVE-APTIV",
        "emp_first": "Amit",
        "emp_last": "Aptiv",
        "email": "amit.leave@karnex.test",
        "emp_code": "LEAVE-AMT",
        "policies": [
            {"leave": "Casual Leave", "per_month": "1.0", "expire": None, "carry_cap": None},
        ],
        "expected_jul": {"Casual Leave": D("6.0")},
    },
    {
        "customer": "Magna Steyr",
        "branch": "Magna Steyr HQ",
        "project": "Magna Steyr Project",
        "opp_id": "OPP-LEAVE-MAGNA",
        "emp_first": "Meera",
        "emp_last": "Magna",
        "email": "meera.leave@karnex.test",
        "emp_code": "LEAVE-MRA",
        "policies": [
            {"leave": "Casual Leave", "per_month": "1.0", "expire": "Monthly", "carry_cap": "0"},
        ],
        "expected_jul": {"Casual Leave": D("1.0")},
    },
    {
        "customer": "Uno Minda",
        "branch": "Uno Minda HQ",
        "project": "Uno Minda Project",
        "opp_id": "OPP-LEAVE-UNO",
        "emp_first": "Uday",
        "emp_last": "Uno",
        "email": "uday.leave@karnex.test",
        "emp_code": "LEAVE-UDY",
        "policies": [
            {"leave": "Casual Leave", "per_month": "0.5", "expire": "Yearly", "carry_cap": "0"},
            {"leave": "Sick Leave", "per_month": "0.5", "expire": "Yearly", "carry_cap": "0"},
            {"leave": "Earned Leave", "per_month": "2.0", "expire": "Yearly", "carry_cap": "0"},
        ],
        "expected_jul": {
            "Casual Leave": D("2.5"),
            "Sick Leave": D("3.5"),
            "Earned Leave": D("14.0"),
        },
    },
)


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _sanitize_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


def _db_banner(url: str) -> str:
    parsed = urlparse(url.replace("postgresql+psycopg2://", "postgresql://", 1))
    host = parsed.hostname or "?"
    db = (parsed.path or "/").lstrip("/") or "?"
    return f"{host}/{db}"


def _assert_safe_target(url: str, force: bool) -> None:
    parsed = urlparse(url.replace("postgresql+psycopg2://", "postgresql://", 1))
    host = (parsed.hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return
    if any(m in host for m in _PROD_HOST_MARKERS) and not force:
        raise SystemExit(
            f"REFUSING to seed: DATABASE host '{host}' looks like production/hosted. "
            "Pass --force if you really intend to write there."
        )
    if not force and host not in _LOCAL_HOSTS:
        # Non-local but not clearly prod — still require --force for safety.
        raise SystemExit(
            f"REFUSING to seed non-local host '{host}'. "
            "Local hosts only (localhost / 127.0.0.1). Pass --force to override."
        )


def _get_or_create(session: Session, model, defaults=None, **filters):
    obj = session.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if obj:
        return obj, False
    obj = model(**filters, **(defaults or {}))
    session.add(obj)
    session.flush()
    return obj, True


def _ensure_seed_user(session: Session) -> int:
    row = session.execute(
        sa.text("SELECT id FROM registration_data ORDER BY id ASC LIMIT 1")
    ).first()
    if row:
        return int(row[0])
    raise RuntimeError(
        "No registration_data users found. Log into CRM once (or create an admin) first."
    )


def _ensure_leave_type(session: Session, name: str) -> LeavePolicyType:
    lt, _ = _get_or_create(
        session,
        LeavePolicyType,
        name=name,
        defaults={"accrual_rule": "scenario seed", "carry_forward_rule": "per policy"},
    )
    return lt


def _upsert_policy(
    session: Session,
    customer: Customer,
    branch: CustomerBranch,
    lt: LeavePolicyType,
    *,
    per_month: str,
    expire: str | None,
    carry_cap: str | None,
) -> CustomerLeavePolicy:
    pol = session.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.customer_id == customer.id,
            CustomerLeavePolicy.branch_id == branch.id,
            CustomerLeavePolicy.leave_type_id == lt.id,
        )
    ).scalar_one_or_none()
    fields = dict(
        leave_credit_type="Monthly",
        leave_credit_timing="End_Of_Period",
        leave_credit_balance=D(per_month),
        initial_credit_balance=ZERO,
        leave_expire=expire,
        maximum_carry_forward=None if carry_cap is None else D(carry_cap),
        effective_date=START,
        is_active=True,
        prorate_balance_credit=False,
        is_max_limit=False,
    )
    if pol is None:
        pol = CustomerLeavePolicy(
            customer_id=customer.id,
            branch_id=branch.id,
            leave_type_id=lt.id,
            **fields,
        )
        session.add(pol)
        session.flush()
        return pol
    for k, v in fields.items():
        setattr(pol, k, v)
    session.flush()
    return pol


def _consume_source(pe_id: int) -> str:
    return f"{CONSUME_SOURCE_PREFIX}{pe_id}-202603"


def _scenario_ledger_filter(employee_id: int):
    """Events owned by this seed employee that the credit/consume path writes."""
    return sa.and_(
        LeaveAccrualEvent.employee_id == employee_id,
        or_(
            LeaveAccrualEvent.source.like("pe_credit:%"),
            LeaveAccrualEvent.source.like("pe_cycle_expire:%"),
            LeaveAccrualEvent.source.like("pe_expire:%"),
            LeaveAccrualEvent.source.like("pe_carry:%"),
            LeaveAccrualEvent.source.like(f"{CONSUME_SOURCE_PREFIX}%"),
        ),
    )


def _reset_pe_leave(session: Session, pe: ProjectEmployee, employee_id: int) -> None:
    """Clear scenario ledger + leave-detail rows for one PE so credits can replay."""
    session.execute(delete(LeaveAccrualEvent).where(_scenario_ledger_filter(employee_id)))
    details = session.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all()
    for row in details:
        session.delete(row)
    session.flush()


def _bal(session: Session, pe_id: int, leave_type_id: int) -> Decimal:
    row = session.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe_id,
            ProjectEmployeeLeaveDetail.leave_type_id == leave_type_id,
        )
    ).scalar_one_or_none()
    if row is None:
        return ZERO
    return Decimal(row.leave_balance or 0)


def _print_balances(session: Session, pe: ProjectEmployee, label: str) -> dict[str, Decimal]:
    rows = session.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all()
    out: dict[str, Decimal] = {}
    print(f"\n{label}")
    for r in rows:
        lt = session.get(LeavePolicyType, r.leave_type_id)
        name = lt.name if lt else f"type#{r.leave_type_id}"
        bal = Decimal(r.leave_balance or 0)
        out[name] = bal
        print(f"  {name:14s} balance={bal}  consumed={r.leave_consumed}")
    return out


def seed(*, through_dec: bool = False, force: bool = False, reset: bool = False) -> int:
    url = crm_database_url()
    _assert_safe_target(url, force=force)
    banner = _db_banner(url)
    print("=" * 64)
    print(f"SEEDING CRM DATABASE: {banner}")
    print(f"DSN (sanitized): {_sanitize_url(url)}")
    print("Mode: INSERT/upsert only - unrelated rows are never deleted.")
    if reset:
        print("Reset: scenario leave ledger + PE leave details will be cleared first.")
    print("=" * 64)

    session = get_session_factory()()
    try:
        user_id = _ensure_seed_user(session)
        leave_types = {
            name: _ensure_leave_type(session, name)
            for name in ("Casual Leave", "Sick Leave", "Earned Leave")
        }
        lt_casual = leave_types["Casual Leave"]

        built: list[dict] = []
        for sc in SCENARIOS:
            cust, new_c = _get_or_create(
                session,
                Customer,
                name=sc["customer"],
                defaults={"status": CustomerStatus.ACTIVE},
            )
            if not new_c and cust.status != CustomerStatus.ACTIVE:
                cust.status = CustomerStatus.ACTIVE

            branch = session.execute(
                select(CustomerBranch).where(
                    CustomerBranch.customer_id == cust.id,
                    CustomerBranch.branch_name == sc["branch"],
                )
            ).scalar_one_or_none()
            if branch is None:
                branch = CustomerBranch(
                    customer_id=cust.id,
                    branch_name=sc["branch"],
                    is_primary=True,
                    city="Pune",
                    state="Maharashtra",
                    country="India",
                )
                session.add(branch)
                session.flush()

            for p in sc["policies"]:
                _upsert_policy(
                    session,
                    cust,
                    branch,
                    leave_types[p["leave"]],
                    per_month=p["per_month"],
                    expire=p["expire"],
                    carry_cap=p["carry_cap"],
                )

            emp, new_e = _get_or_create(
                session,
                Employee,
                email=sc["email"],
                defaults={
                    "first_name": sc["emp_first"],
                    "last_name": sc["emp_last"],
                    "display_name": f"{sc['emp_first']} ({sc['customer']})",
                    "employee_code": sc["emp_code"],
                    "profile_type": ProfileType.INTERNAL,
                    "is_active": True,
                    "date_of_joining": START,
                    "employment_type": "Full_Time",
                },
            )
            if not new_e:
                emp.first_name = sc["emp_first"]
                emp.last_name = sc["emp_last"]
                emp.display_name = f"{sc['emp_first']} ({sc['customer']})"
                emp.employee_code = emp.employee_code or sc["emp_code"]
                emp.is_active = True
                if emp.profile_type is None:
                    emp.profile_type = ProfileType.INTERNAL

            opp, _ = _get_or_create(
                session,
                Opportunity,
                opp_id=sc["opp_id"],
                defaults={
                    "title": f"{sc['customer']} Leave Scenario Opp",
                    "customer_id": cust.id,
                    "branch_id": branch.id,
                    "opp_type": OppType.T_AND_M,
                    "pipeline_stage": PipelineStage.ACTIVE,
                    "created_by": user_id,
                },
            )
            if opp.branch_id is None:
                opp.branch_id = branch.id
            if opp.customer_id != cust.id:
                opp.customer_id = cust.id

            proj = session.execute(
                select(Project).where(
                    Project.name == sc["project"],
                    Project.customer_id == cust.id,
                )
            ).scalar_one_or_none()
            if proj is None:
                proj = Project(
                    opportunity_id=opp.id,
                    customer_id=cust.id,
                    branch_id=branch.id,
                    name=sc["project"],
                    status=ProjectStatus.ACTIVE,
                )
                session.add(proj)
                session.flush()
            else:
                proj.branch_id = branch.id
                proj.opportunity_id = opp.id
                proj.status = ProjectStatus.ACTIVE

            pe = session.execute(
                select(ProjectEmployee).where(
                    ProjectEmployee.project_id == proj.id,
                    ProjectEmployee.employee_id == emp.id,
                )
            ).scalar_one_or_none()
            if pe is None:
                pe = ProjectEmployee(
                    project_id=proj.id,
                    employee_id=emp.id,
                    onboarding_date=START,
                    billing_rate=D("100"),
                    is_active=True,
                    is_exit=False,
                    role_title="Leave Scenario",
                )
                session.add(pe)
                session.flush()
            else:
                pe.onboarding_date = pe.onboarding_date or START
                pe.is_active = True
                pe.is_exit = False

            if reset:
                _reset_pe_leave(session, pe, emp.id)

            seeded = seed_leave_details_from_customer_policy(
                session, pe, proj, seed_as_of=START
            )
            built.append({
                "sc": sc,
                "cust": cust,
                "branch": branch,
                "emp": emp,
                "proj": proj,
                "pe": pe,
                "seeded_rows": len(seeded),
            })

        session.commit()

        # Credits must target ONLY scenario PEs (never all live PEs).
        print("\n== Monthly credit job Jan..Jul 2026 (scenario PEs only) ==")
        for m in range(1, 8):
            as_of = month_end(2026, m)
            for item in built:
                summary = run_pe_leave_credit(session, as_of=as_of, pe_id=item["pe"].id)
                print(
                    f"  {as_of.isoformat()}  {item['sc']['customer']:12s}  "
                    f"credited {summary['total_credited']} rows={summary['rows_credited']}"
                )
            if m == 3:
                for item in built:
                    pe = item["pe"]
                    src = _consume_source(pe.id)
                    already = session.execute(
                        select(LeaveAccrualEvent.id).where(
                            LeaveAccrualEvent.source == src
                        ).limit(1)
                    ).first()
                    if already:
                        print(f"    March leave - {item['sc']['customer']}: already applied ({src})")
                        continue
                    row = consume_pe_leave(session, pe.id, lt_casual.id, D("1"))
                    session.add(
                        LeaveAccrualEvent(
                            employee_id=pe.employee_id,
                            leave_type_id=lt_casual.id,
                            event_type="Consumption",
                            amount=D("-1"),
                            balance_after=row.leave_balance,
                            source=src,
                            note="Leave-scenario seed: March timesheet leave 1 day",
                        )
                    )
                    session.commit()
                    print(
                        f"    March leave - {item['sc']['customer']}: "
                        f"consumed 1.0, balance now {row.leave_balance}"
                    )

        print("\n================ LEAVE UPDATE (as of Jul 2026) ================")
        ok = True
        for item in built:
            sc = item["sc"]
            bals = _print_balances(
                session, item["pe"], f"{sc['customer']} - {sc['emp_first']}"
            )
            for lt_name, expected in sc["expected_jul"].items():
                got = bals.get(lt_name, ZERO)
                if got != expected:
                    ok = False
                    print(f"  !! EXPECTED {lt_name}={expected}, got {got}")

        if not ok:
            print(
                "\nBalances do not match Jul 2026 expectations. "
                "Re-run with --reset to clear scenario ledger/details and replay."
            )
            return 1

        print("\n[OK] Jul 2026 assertions PASSED")

        if through_dec:
            print("\n== Advancing Aug..Dec 2026 + Dec-31 year-end carry ==")
            for m in range(8, 13):
                as_of = month_end(2026, m)
                for item in built:
                    run_pe_leave_credit(session, as_of=as_of, pe_id=item["pe"].id)
            for item in built:
                run_pe_leave_credit(session, as_of=date(2026, 12, 31), pe_id=item["pe"].id)
                _print_balances(
                    session,
                    item["pe"],
                    f"{item['sc']['customer']} entering 2027",
                )

        print("\n---------------- UI navigation ----------------")
        print("Login: existing CRM admin (e.g. crm_admin@karnex.test) - no new auth users created.")
        print("Employees are CRM HR records (no portal login required to view balances).")
        for item in built:
            sc = item["sc"]
            print(
                f"  * Customers -> {sc['customer']} -> Branches -> {sc['branch']} "
                f"(Leave Policy)"
            )
            print(
                f"  * Customers -> {sc['customer']} -> {sc['branch']} -> "
                f"{sc['project']} -> Team -> {sc['emp_first']} (Leave Details)"
            )
            print(f"  * Employees -> search '{sc['emp_first']}' / {sc['email']}")
            print(f"  * Projects -> {sc['project']}")
        print("\nRe-run:  python scripts/seed_leave_scenarios_crm.py")
        print("Reset:   python scripts/seed_leave_scenarios_crm.py --reset")
        print("Year-end: python scripts/seed_leave_scenarios_crm.py --through-dec")
        print(f"\nSeed complete on {banner}.")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed leave scenarios into CRM Postgres")
    parser.add_argument(
        "--through-dec",
        action="store_true",
        help="After Jul 2026 state, also run Aug–Dec credits + Dec-31 year-end",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow seeding a non-local / hosted DATABASE_URL",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Clear scenario PE leave details + related ledger events, then replay",
    )
    args = parser.parse_args()
    raise SystemExit(seed(through_dec=args.through_dec, force=args.force, reset=args.reset))


if __name__ == "__main__":
    main()
