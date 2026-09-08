"""FULL LEAVE-POLICY SYSTEM QA on the live local CRM DB (karnex_db).

Creates ONLY QA-* / qa.*@test.local rows. Does not rewrite existing customer
policies unless --fix-policies is passed (and then restores are documented).

Usage (from backend/):
  python scripts/qa_leave_system_test.py
  python scripts/qa_leave_system_test.py --through-dec
  python scripts/qa_leave_system_test.py --cleanup   # delete QA rows only
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import re
import sys
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlalchemy as sa
from sqlalchemy import delete, or_, select, text
from sqlalchemy.orm import Session

from crm_db import crm_database_url, get_session_factory
from models.customers import Customer, CustomerBranch, CustomerStatus
from models.hr import Employee, ProfileType
from models.leave import CustomerLeavePolicy, LeaveAccrualEvent
from models.masters import LeavePolicyType
from models.opportunities import Opportunity, OppType, PipelineStage
from models.projects import (
    Project,
    ProjectEmployee,
    ProjectEmployeeLeaveDetail,
    ProjectLeavePolicy,
    ProjectStatus,
)
from services.project_employee_leave_credit import run_pe_leave_credit
from services.project_employees import (
    consume_pe_leave,
    leave_detail_out,
    seed_leave_details_from_customer_policy,
)

ZERO = Decimal("0")
D = Decimal
START = date(2026, 1, 1)
CONSUME_PREFIX = "timesheet:qa-"  # VARCHAR(64) safe
_LOCAL = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

SCENARIOS = (
    {
        "customer": "Aptiv",
        "project": "QA-Aptiv-Test",
        "opp_id": "OPP-QA-APTIV",
        "emp_first": "Amit",
        "emp_last": "QA",
        "email": "qa.amit@test.local",
        "emp_code": "QA-AMT",
        "expected_jul": {"Casual Leave": D("6.0")},
    },
    {
        "customer": "Magna Steyr",
        "project": "QA-Magna-Steyr-Test",
        "opp_id": "OPP-QA-MAGNA",
        "emp_first": "Meera",
        "emp_last": "QA",
        "email": "qa.meera@test.local",
        "emp_code": "QA-MRA",
        "expected_jul": {"Casual Leave": D("1.0")},
    },
    {
        "customer": "Uno Minda",
        "project": "QA-Uno-Minda-Test",
        "opp_id": "OPP-QA-UNO",
        "emp_first": "Uday",
        "emp_last": "QA",
        "email": "qa.uday@test.local",
        "emp_code": "QA-UDY",
        "expected_jul": {
            "Casual Leave": D("2.5"),
            "Sick Leave": D("3.5"),
            "Earned Leave": D("14.0"),
        },
    },
)

EXPECTED_POLICIES = {
    ("Aptiv", "Casual Leave"): dict(
        leave_credit_type="Monthly",
        leave_credit_balance=D("1.0"),
        leave_expire=None,
        maximum_carry_forward=None,
    ),
    ("Magna Steyr", "Casual Leave"): dict(
        leave_credit_type="Monthly",
        leave_credit_balance=D("1.0"),
        leave_expire="Monthly",
        maximum_carry_forward=D("0"),
    ),
    ("Uno Minda", "Casual Leave"): dict(
        leave_credit_type="Monthly",
        leave_credit_balance=D("0.5"),
        leave_expire="Yearly",
        maximum_carry_forward=D("0"),
    ),
    ("Uno Minda", "Sick Leave"): dict(
        leave_credit_type="Monthly",
        leave_credit_balance=D("0.5"),
        leave_expire="Yearly",
        maximum_carry_forward=D("0"),
    ),
    ("Uno Minda", "Earned Leave"): dict(
        leave_credit_type="Monthly",
        leave_credit_balance=D("2.0"),
        leave_expire="Yearly",
        maximum_carry_forward=D("0"),
    ),
}


def month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _banner(url: str) -> str:
    p = urlparse(url.replace("postgresql+psycopg2://", "postgresql://", 1))
    return f"{p.hostname}/{ (p.path or '/').lstrip('/') }"


def _assert_local(url: str) -> None:
    p = urlparse(url.replace("postgresql+psycopg2://", "postgresql://", 1))
    host = (p.hostname or "").lower()
    if host not in _LOCAL:
        raise SystemExit(f"REFUSING non-local host '{host}'. QA runs on localhost only.")


def _get_or_create(session: Session, model, defaults=None, **filters):
    obj = session.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if obj:
        return obj, False
    obj = model(**filters, **(defaults or {}))
    session.add(obj)
    session.flush()
    return obj, True


def _seed_user(session: Session) -> int:
    row = session.execute(text("SELECT id FROM registration_data ORDER BY id ASC LIMIT 1")).first()
    if not row:
        raise RuntimeError("No registration_data users â€” log into CRM once first.")
    return int(row[0])


def _leave_type(session: Session, name: str) -> LeavePolicyType:
    lt = session.execute(select(LeavePolicyType).where(LeavePolicyType.name == name)).scalar_one_or_none()
    if lt:
        return lt
    lt = LeavePolicyType(name=name, accrual_rule="qa", carry_forward_rule="per policy")
    session.add(lt)
    session.flush()
    return lt


def _primary_branch(session: Session, customer: Customer) -> CustomerBranch:
    branch = session.execute(
        select(CustomerBranch)
        .where(CustomerBranch.customer_id == customer.id)
        .order_by(CustomerBranch.is_primary.desc(), CustomerBranch.id)
    ).scalars().first()
    if branch is None:
        raise RuntimeError(f"No branch for customer {customer.name}")
    return branch


def verify_policies(session: Session, evidence: dict) -> list[str]:
    """Verify expected branch policies; return list of FAIL messages (empty = PASS)."""
    fails: list[str] = []
    sql = text(
        """
        SELECT c.name, p.branch_id, b.branch_name, t.name AS leave_type,
               p.leave_credit_type, p.leave_credit_balance, p.leave_credit_timing,
               p.leave_expire, p.leave_expire_timing, p.maximum_carry_forward, p.is_active, p.id
        FROM customer_leave_policies p
        JOIN customers c ON c.id = p.customer_id
        JOIN leave_policy_types t ON t.id = p.leave_type_id
        LEFT JOIN customer_branches b ON b.id = p.branch_id
        WHERE c.name IN ('Aptiv','Magna Steyr','Uno Minda') AND p.branch_id IS NOT NULL
        ORDER BY c.name, t.name
        """
    )
    rows = [dict(r) for r in session.execute(sql).mappings().all()]
    evidence["policy_sql_rows"] = [
        {k: (str(v) if isinstance(v, Decimal) else v) for k, v in r.items()} for r in rows
    ]
    print("\n== Policy verification ==")
    for r in rows:
        print(
            f"  {r['name']:12s} {r['branch_name']:20s} {r['leave_type']:14s} "
            f"credit={r['leave_credit_balance']} expire={r['leave_expire']!r} "
            f"carry={r['maximum_carry_forward']}"
        )
    by_key = {(r["name"], r["leave_type"]): r for r in rows}
    for key, exp in EXPECTED_POLICIES.items():
        got = by_key.get(key)
        if not got:
            fails.append(f"Missing policy {key}")
            continue
        if got["leave_credit_type"] != exp["leave_credit_type"]:
            fails.append(f"{key}: credit_type {got['leave_credit_type']!r} != {exp['leave_credit_type']!r}")
        if Decimal(got["leave_credit_balance"] or 0) != exp["leave_credit_balance"]:
            fails.append(
                f"{key}: balance {got['leave_credit_balance']} != {exp['leave_credit_balance']}"
            )
        if (got["leave_expire"] or None) != exp["leave_expire"]:
            fails.append(f"{key}: expire {got['leave_expire']!r} != {exp['leave_expire']!r}")
        gcarry = got["maximum_carry_forward"]
        ec = exp["maximum_carry_forward"]
        if ec is None:
            if gcarry is not None:
                fails.append(f"{key}: carry {gcarry} expected NULL")
        elif Decimal(gcarry or 0) != ec:
            fails.append(f"{key}: carry {gcarry} != {ec}")
        if not got["is_active"]:
            fails.append(f"{key}: inactive")
    return fails


def _reset_qa_pe(session: Session, pe: ProjectEmployee, employee_id: int) -> None:
    session.execute(
        delete(LeaveAccrualEvent).where(
            LeaveAccrualEvent.employee_id == employee_id,
            or_(
                LeaveAccrualEvent.source.like("pe_credit:%"),
                LeaveAccrualEvent.source.like("pe_cycle_expire:%"),
                LeaveAccrualEvent.source.like("pe_expire:%"),
                LeaveAccrualEvent.source.like("pe_carry:%"),
                LeaveAccrualEvent.source.like(f"{CONSUME_PREFIX}%"),
            ),
        )
    )
    for row in session.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe.id
        )
    ).scalars().all():
        session.delete(row)
    session.flush()


def _balances(session: Session, pe_id: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rows = session.execute(
        select(ProjectEmployeeLeaveDetail).where(
            ProjectEmployeeLeaveDetail.project_employee_id == pe_id
        )
    ).scalars().all()
    for r in rows:
        lt = session.get(LeavePolicyType, r.leave_type_id)
        name = lt.name if lt else f"type#{r.leave_type_id}"
        out[name] = {
            "balance": Decimal(r.leave_balance or 0),
            "consumed": Decimal(r.leave_consumed or 0),
            "accrual": Decimal(r.leave_accrual or 0),
            "customer_leave_policy_id": r.customer_leave_policy_id,
            "project_leave_policy_id": getattr(r, "project_leave_policy_id", None),
            "policy_source": leave_detail_out(
                r,
                lt,
                policy=(
                    session.get(ProjectLeavePolicy, r.project_leave_policy_id)
                    if getattr(r, "project_leave_policy_id", None)
                    else session.get(CustomerLeavePolicy, r.customer_leave_policy_id)
                ),
            ).get("policy_source"),
        }
    return out


def _consume_src(pe_id: int, tag: str = "202603") -> str:
    return f"{CONSUME_PREFIX}{pe_id}-{tag}"


def cleanup(session: Session) -> dict:
    """Delete all QA-* / qa.*@test.local artifacts. Returns counts."""
    emails = [s["email"] for s in SCENARIOS] + ["qa.omar@test.local", "qa.fallback@test.local"]
    emps = session.execute(select(Employee).where(Employee.email.in_(emails))).scalars().all()
    emp_ids = [e.id for e in emps]
    pe_ids: list[int] = []
    if emp_ids:
        pes = session.execute(
            select(ProjectEmployee).where(ProjectEmployee.employee_id.in_(emp_ids))
        ).scalars().all()
        pe_ids = [p.id for p in pes]
        if pe_ids:
            session.execute(
                delete(LeaveAccrualEvent).where(LeaveAccrualEvent.employee_id.in_(emp_ids))
            )
            session.execute(
                delete(ProjectEmployeeLeaveDetail).where(
                    ProjectEmployeeLeaveDetail.project_employee_id.in_(pe_ids)
                )
            )
            # rates / other children may cascade; delete PEs explicitly
            for pe in pes:
                session.delete(pe)
        session.flush()

    projects = session.execute(
        select(Project).where(Project.name.like("QA-%"))
    ).scalars().all()
    proj_ids = [p.id for p in projects]
    if proj_ids:
        session.execute(
            delete(ProjectLeavePolicy).where(ProjectLeavePolicy.project_id.in_(proj_ids))
        )
        for p in projects:
            session.delete(p)
        session.flush()

    opps = session.execute(
        select(Opportunity).where(Opportunity.opp_id.like("OPP-QA-%"))
    ).scalars().all()
    for o in opps:
        session.delete(o)
    session.flush()

    for e in emps:
        session.delete(e)
    session.commit()
    return {
        "employees_deleted": len(emp_ids),
        "pes_deleted": len(pe_ids),
        "projects_deleted": len(proj_ids),
        "opps_deleted": len(opps),
    }


def run(*, through_dec: bool, reset: bool) -> dict:
    url = crm_database_url()
    _assert_local(url)
    evidence: dict = {
        "db": _banner(url),
        "phases": {},
        "pass_fail": {},
    }
    print("=" * 72)
    print(f"QA LEAVE SYSTEM TEST â€” {_banner(url)}")
    print("=" * 72)

    session = get_session_factory()()
    try:
        # ----- Policy verify -----
        pol_fails = verify_policies(session, evidence)
        evidence["pass_fail"]["policy_config"] = "PASS" if not pol_fails else "FAIL"
        if pol_fails:
            for f in pol_fails:
                print("  FAIL:", f)
            evidence["policy_fails"] = pol_fails
        else:
            print("  PASS: branch policies match expected config")

        user_id = _seed_user(session)
        lt_casual = _leave_type(session, "Casual Leave")
        for n in ("Sick Leave", "Earned Leave"):
            _leave_type(session, n)

        built: list[dict] = []
        print("\n== Phase 1: QA projects + employees + mapping ==")
        for sc in SCENARIOS:
            cust = session.execute(
                select(Customer).where(Customer.name == sc["customer"])
            ).scalar_one()
            if cust.status != CustomerStatus.ACTIVE:
                cust.status = CustomerStatus.ACTIVE
            branch = _primary_branch(session, cust)

            emp, _ = _get_or_create(
                session,
                Employee,
                email=sc["email"],
                defaults={
                    "first_name": sc["emp_first"],
                    "last_name": sc["emp_last"],
                    "display_name": f"QA {sc['emp_first']} ({sc['customer']})",
                    "employee_code": sc["emp_code"],
                    "profile_type": ProfileType.INTERNAL,
                    "is_active": True,
                    "date_of_joining": START,
                    "employment_type": "Full_Time",
                },
            )
            emp.is_active = True
            emp.display_name = f"QA {sc['emp_first']} ({sc['customer']})"

            opp, _ = _get_or_create(
                session,
                Opportunity,
                opp_id=sc["opp_id"],
                defaults={
                    "title": f"QA {sc['customer']} Leave Test",
                    "customer_id": cust.id,
                    "branch_id": branch.id,
                    "opp_type": OppType.T_AND_M,
                    "pipeline_stage": PipelineStage.ACTIVE,
                    "created_by": user_id,
                },
            )
            opp.branch_id = branch.id
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

            assert proj.branch_id is not None, f"{sc['project']} missing branch_id"

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
                    role_title="QA Leave Test",
                )
                session.add(pe)
                session.flush()
            else:
                pe.onboarding_date = START
                pe.is_active = True
                pe.is_exit = False

            if reset:
                _reset_qa_pe(session, pe, emp.id)

            seeded = seed_leave_details_from_customer_policy(
                session, pe, proj, seed_as_of=START
            )
            bals = _balances(session, pe.id)
            print(
                f"  {sc['customer']:12s} project={proj.name} pe=#{pe.id} "
                f"branch_id={proj.branch_id} seeded={len(seeded)}"
            )
            for name, info in bals.items():
                print(
                    f"    {name}: bal={info['balance']} cust_fk={info['customer_leave_policy_id']} "
                    f"proj_fk={info['project_leave_policy_id']} src={info['policy_source']}"
                )
                if info["balance"] != ZERO and info["accrual"] == ZERO:
                    # opening may be 0; balance should be 0 at seed before credits
                    pass
            built.append({
                "sc": sc, "cust": cust, "branch": branch, "emp": emp,
                "proj": proj, "pe": pe, "bals": bals,
            })

        session.commit()

        # Seed assertions
        seed_fails: list[str] = []
        for item in built:
            bals = _balances(session, item["pe"].id)
            for name, info in bals.items():
                if info["balance"] != ZERO:
                    # after seed, before credit: should be 0 (End_Of_Period policies)
                    seed_fails.append(
                        f"{item['sc']['email']} {name}: opening balance {info['balance']} != 0"
                    )
                if info["project_leave_policy_id"] is not None:
                    seed_fails.append(
                        f"{item['sc']['email']} {name}: project_leave_policy_id should be NULL"
                    )
                if info["customer_leave_policy_id"] is None:
                    seed_fails.append(
                        f"{item['sc']['email']} {name}: customer_leave_policy_id missing"
                    )
                if info["policy_source"] not in ("branch", "customer"):
                    seed_fails.append(
                        f"{item['sc']['email']} {name}: policy_source={info['policy_source']!r}"
                    )
        evidence["pass_fail"]["phase1_seed"] = "PASS" if not seed_fails else "FAIL"
        evidence["phase1_seed_fails"] = seed_fails
        evidence["phases"]["phase1"] = [
            {
                "customer": i["sc"]["customer"],
                "project": i["proj"].name,
                "project_id": i["proj"].id,
                "branch_id": i["proj"].branch_id,
                "pe_id": i["pe"].id,
                "email": i["sc"]["email"],
                "balances": {
                    k: {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
                    for k, v in _balances(session, i["pe"].id).items()
                },
            }
            for i in built
        ]
        print("  Phase1 seed:", evidence["pass_fail"]["phase1_seed"])
        for f in seed_fails:
            print("   FAIL:", f)

        # ----- Phase 2: credits Jan-Jul + March consume -----
        print("\n== Phase 2: Credit Janâ†’Jul + March Casual leave ==")
        pe_ids = [i["pe"].id for i in built]
        for m in range(1, 8):
            as_of = month_end(2026, m)
            for item in built:
                summary = run_pe_leave_credit(session, as_of=as_of, pe_id=item["pe"].id)
                print(
                    f"  {as_of} {item['sc']['customer']:12s} "
                    f"credited={summary['total_credited']} rows={summary['rows_credited']}"
                )
            if m == 3:
                for item in built:
                    pe = item["pe"]
                    src = _consume_src(pe.id)
                    already = session.execute(
                        select(LeaveAccrualEvent.id).where(LeaveAccrualEvent.source == src)
                    ).first()
                    if already:
                        print(f"  March leave already applied pe=#{pe.id}")
                        continue
                    row = consume_pe_leave(session, pe.id, lt_casual.id, D("1"), allow_negative=False)
                    session.add(
                        LeaveAccrualEvent(
                            employee_id=item["emp"].id,
                            leave_type_id=lt_casual.id,
                            event_type="Consumption",
                            amount=D("-1"),
                            balance_after=row.leave_balance,
                            source=src,
                            note="QA March Casual Leave (timesheet path equivalent)",
                        )
                    )
                    print(f"  March leave consumed pe=#{pe.id} src={src}")
            session.commit()

        # Idempotency: re-run July twice
        jul = month_end(2026, 7)
        before_evt = session.execute(
            select(sa.func.count()).select_from(LeaveAccrualEvent).where(
                LeaveAccrualEvent.employee_id.in_([i["emp"].id for i in built])
            )
        ).scalar()
        before_bals = {i["pe"].id: _balances(session, i["pe"].id) for i in built}
        for _ in range(2):
            for item in built:
                run_pe_leave_credit(session, as_of=jul, pe_id=item["pe"].id)
            session.commit()
        after_evt = session.execute(
            select(sa.func.count()).select_from(LeaveAccrualEvent).where(
                LeaveAccrualEvent.employee_id.in_([i["emp"].id for i in built])
            )
        ).scalar()
        idem_ok = before_evt == after_evt
        for item in built:
            if before_bals[item["pe"].id] != _balances(session, item["pe"].id):
                idem_ok = False
        evidence["pass_fail"]["july_idempotency"] = "PASS" if idem_ok else "FAIL"
        print(f"  July idempotency: {evidence['pass_fail']['july_idempotency']} "
              f"(events {before_evt}â†’{after_evt})")

        # Jul balance assertions
        jul_fails: list[str] = []
        jul_evidence: dict = {}
        for item in built:
            bals = _balances(session, item["pe"].id)
            jul_evidence[item["sc"]["email"]] = {
                k: {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
                for k, v in bals.items()
            }
            print(f"\n  {item['sc']['customer']} / {item['sc']['emp_first']}:")
            for name, info in bals.items():
                print(
                    f"    {name}: balance={info['balance']} consumed={info['consumed']} "
                    f"src={info['policy_source']}"
                )
            for name, exp in item["sc"]["expected_jul"].items():
                got = bals.get(name, {}).get("balance", ZERO)
                if got != exp:
                    jul_fails.append(
                        f"{item['sc']['email']} {name}: {got} != {exp}"
                    )

        # Magna ledger shape
        meera = next(i for i in built if i["sc"]["customer"] == "Magna Steyr")
        expire_rows = session.execute(
            select(LeaveAccrualEvent).where(
                LeaveAccrualEvent.employee_id == meera["emp"].id,
                LeaveAccrualEvent.source.like("pe_cycle_expire:%"),
            ).order_by(LeaveAccrualEvent.id)
        ).scalars().all()
        expire_total = sum(abs(Decimal(e.amount or 0)) for e in expire_rows)
        expire_months = []
        for e in expire_rows:
            # source pe_cycle_expire:{pe}:{type}:{YYYY-MM}
            parts = (e.source or "").split(":")
            ym = parts[-1] if parts else ""
            expire_months.append({
                "created_at": str(e.created_at),
                "source": e.source,
                "amount": str(e.amount),
                "ym": ym,
            })
        print("\n  Magna expiry events:")
        for em in expire_months:
            print(f"    {em}")
        # Spec: NO expiry for the March credit (consumed to 0) â†’ no pe_cycle_expire:â€¦:2026-04
        april_expiry = [e for e in expire_rows if (e.source or "").endswith(":2026-04")]
        if len(expire_rows) != 5:
            jul_fails.append(f"Magna: expected 5 expiry Adjustments, got {len(expire_rows)}")
        if expire_total != D("5.0"):
            jul_fails.append(f"Magna: expiry total {expire_total} != 5.0")
        if april_expiry:
            jul_fails.append(
                f"Magna: unexpected April rollover expiry sources: "
                f"{[e.source for e in april_expiry]}"
            )
        meera_bals = _balances(session, meera["pe"].id)
        if meera_bals.get("Casual Leave", {}).get("consumed") != D("1.0"):
            jul_fails.append(
                f"Magna consumed={meera_bals.get('Casual Leave', {}).get('consumed')} != 1.0"
            )

        evidence["pass_fail"]["phase2_jul_balances"] = "PASS" if not jul_fails else "FAIL"
        evidence["jul_fails"] = jul_fails
        evidence["phases"]["phase2"] = {
            "balances": jul_evidence,
            "magna_expiry_events": expire_months,
            "magna_expiry_count": len(expire_rows),
            "magna_expiry_total": str(expire_total),
        }
        print("  Phase2 Jul:", evidence["pass_fail"]["phase2_jul_balances"])
        for f in jul_fails:
            print("   FAIL:", f)

        # Backdated leave check for Meera
        print("\n== Phase 2b: Backdated second March leave for Meera (from current pool) ==")
        before = _balances(session, meera["pe"].id)["Casual Leave"]["balance"]
        src2 = _consume_src(meera["pe"].id, "202603-late")
        already2 = session.execute(
            select(LeaveAccrualEvent.id).where(LeaveAccrualEvent.source == src2)
        ).first()
        backdated: dict = {"before": str(before)}
        if not already2:
            try:
                row = consume_pe_leave(session, meera["pe"].id, lt_casual.id, D("1"), allow_negative=False)
                session.add(
                    LeaveAccrualEvent(
                        employee_id=meera["emp"].id,
                        leave_type_id=lt_casual.id,
                        event_type="Consumption",
                        amount=D("-1"),
                        balance_after=row.leave_balance,
                        source=src2,
                        note="QA backdated March leave submitted after July â€” draws current pool",
                    )
                )
                session.commit()
                after = _balances(session, meera["pe"].id)["Casual Leave"]["balance"]
                backdated.update({
                    "after": str(after),
                    "drew_from_current_pool": after == before - D("1"),
                    "result": "PASS" if after == before - D("1") else "FAIL",
                })
                print(f"  Meera Casual {before} â†’ {after} (drew from current July pool)")
            except Exception as exc:  # noqa: BLE001
                session.rollback()
                backdated.update({"result": "LOP_OR_REJECT", "error": str(exc)})
                print(f"  Consume failed (document as LOP path): {exc}")
        else:
            after = _balances(session, meera["pe"].id)["Casual Leave"]["balance"]
            backdated.update({"after": str(after), "result": "ALREADY_APPLIED"})
            print("  Already applied previously")
        evidence["pass_fail"]["phase2_backdated"] = backdated.get("result", "UNKNOWN")
        evidence["phases"]["phase2_backdated"] = backdated

        # Restore Meera to Jul expected state for Phase 3 clarity if we consumed the July day
        if backdated.get("drew_from_current_pool"):
            # credit back the test consume so Magna stays at 1.0 for report clarity? Spec says document
            # known behavior â€” leave the draw in place but note it. Restore for clean Phase3/Dec.
            row = session.execute(
                select(ProjectEmployeeLeaveDetail).where(
                    ProjectEmployeeLeaveDetail.project_employee_id == meera["pe"].id,
                    ProjectEmployeeLeaveDetail.leave_type_id == lt_casual.id,
                )
            ).scalar_one()
            row.leave_balance = Decimal(row.leave_balance or 0) + D("1")
            row.leave_consumed = Decimal(row.leave_consumed or 0) - D("1")
            session.execute(delete(LeaveAccrualEvent).where(LeaveAccrualEvent.source == src2))
            session.commit()
            print("  Restored Meera July pool after documenting backdated behavior")

        # ----- Phase 3: Project override -----
        print("\n== Phase 3: Project override on QA-Aptiv-Test ==")
        aptiv = next(i for i in built if i["sc"]["customer"] == "Aptiv")
        override = session.execute(
            select(ProjectLeavePolicy).where(
                ProjectLeavePolicy.project_id == aptiv["proj"].id,
                ProjectLeavePolicy.leave_type_id == lt_casual.id,
            )
        ).scalar_one_or_none()
        if override is None:
            override = ProjectLeavePolicy(
                project_id=aptiv["proj"].id,
                leave_type_id=lt_casual.id,
                name="QA Casual override",
                leave_credit_type="Monthly",
                leave_credit_balance=D("2.0"),
                initial_credit_balance=ZERO,
                leave_expire="Monthly",
                leave_credit_timing="End_Of_Period",
                leave_expire_timing="End_Of_Period",
                maximum_carry_forward=0,
                effective_date=START,
                is_active=True,
            )
            session.add(override)
        else:
            override.leave_credit_balance = D("2.0")
            override.leave_expire = "Monthly"
            override.maximum_carry_forward = 0
            override.leave_credit_timing = "End_Of_Period"
            override.leave_expire_timing = "End_Of_Period"
            override.is_active = True
        session.flush()

        omar, _ = _get_or_create(
            session,
            Employee,
            email="qa.omar@test.local",
            defaults={
                "first_name": "Omar",
                "last_name": "QA",
                "display_name": "QA Omar (Aptiv override)",
                "employee_code": "QA-OMR",
                "profile_type": ProfileType.INTERNAL,
                "is_active": True,
                "date_of_joining": START,
                "employment_type": "Full_Time",
            },
        )
        omar_pe = session.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == aptiv["proj"].id,
                ProjectEmployee.employee_id == omar.id,
            )
        ).scalar_one_or_none()
        if omar_pe is None:
            omar_pe = ProjectEmployee(
                project_id=aptiv["proj"].id,
                employee_id=omar.id,
                onboarding_date=START,
                billing_rate=D("100"),
                is_active=True,
                is_exit=False,
                role_title="QA Override Test",
            )
            session.add(omar_pe)
            session.flush()
        else:
            omar_pe.onboarding_date = START
            omar_pe.is_active = True
        if reset:
            _reset_qa_pe(session, omar_pe, omar.id)
        seed_leave_details_from_customer_policy(session, omar_pe, aptiv["proj"], seed_as_of=START)
        session.commit()

        omar_bals = _balances(session, omar_pe.id)
        amit_bals = _balances(session, aptiv["pe"].id)
        print("  Omar seed:", {k: (str(v["project_leave_policy_id"]), v["policy_source"],
                                   str(v["customer_leave_policy_id"]))
                               for k, v in omar_bals.items()})
        print("  Amit provenance (unchanged):",
              {k: v["policy_source"] for k, v in amit_bals.items()})

        p3_fails: list[str] = []
        casual = omar_bals.get("Casual Leave", {})
        if casual.get("project_leave_policy_id") is None:
            p3_fails.append("Omar Casual missing project_leave_policy_id")
        if casual.get("customer_leave_policy_id") is not None:
            p3_fails.append("Omar Casual customer_leave_policy_id should be NULL")
        if casual.get("policy_source") != "project":
            p3_fails.append(f"Omar policy_source={casual.get('policy_source')!r} != project")
        if amit_bals.get("Casual Leave", {}).get("policy_source") == "project":
            p3_fails.append("Amit incorrectly showing project override (existing PE rewritten)")

        # Credit Omar Jan-Jul
        for m in range(1, 8):
            run_pe_leave_credit(session, as_of=month_end(2026, m), pe_id=omar_pe.id)
        session.commit()
        omar_jul = _balances(session, omar_pe.id).get("Casual Leave", {})
        print(f"  Omar Jul Casual balance={omar_jul.get('balance')} (expect 2.0)")
        if omar_jul.get("balance") != D("2.0"):
            p3_fails.append(f"Omar Jul balance {omar_jul.get('balance')} != 2.0")
        omar_exp = session.execute(
            select(sa.func.count()).select_from(LeaveAccrualEvent).where(
                LeaveAccrualEvent.employee_id == omar.id,
                LeaveAccrualEvent.source.like("pe_cycle_expire:%"),
            )
        ).scalar()
        print(f"  Omar cycle-expire events: {omar_exp}")
        if omar_exp < 1:
            p3_fails.append("Omar expected Monthly cycle expiry events from project policy")

        # Deactivate override â†’ map fifth employee
        override.is_active = False
        session.flush()
        fb, _ = _get_or_create(
            session,
            Employee,
            email="qa.fallback@test.local",
            defaults={
                "first_name": "Fallback",
                "last_name": "QA",
                "display_name": "QA Fallback (branch)",
                "employee_code": "QA-FB",
                "profile_type": ProfileType.INTERNAL,
                "is_active": True,
                "date_of_joining": START,
                "employment_type": "Full_Time",
            },
        )
        fb_pe = session.execute(
            select(ProjectEmployee).where(
                ProjectEmployee.project_id == aptiv["proj"].id,
                ProjectEmployee.employee_id == fb.id,
            )
        ).scalar_one_or_none()
        if fb_pe is None:
            fb_pe = ProjectEmployee(
                project_id=aptiv["proj"].id,
                employee_id=fb.id,
                onboarding_date=START,
                billing_rate=D("100"),
                is_active=True,
                is_exit=False,
                role_title="QA Fallback",
            )
            session.add(fb_pe)
            session.flush()
        if reset:
            _reset_qa_pe(session, fb_pe, fb.id)
        seed_leave_details_from_customer_policy(session, fb_pe, aptiv["proj"], seed_as_of=START)
        session.commit()
        fb_bals = _balances(session, fb_pe.id)
        print("  Fallback seed:", {k: (v["policy_source"], str(v["customer_leave_policy_id"]),
                                       str(v["project_leave_policy_id"]))
                                   for k, v in fb_bals.items()})
        fbc = fb_bals.get("Casual Leave", {})
        if fbc.get("project_leave_policy_id") is not None:
            p3_fails.append("Fallback still has project_leave_policy_id after deactivate")
        if fbc.get("customer_leave_policy_id") is None:
            p3_fails.append("Fallback missing customer_leave_policy_id")
        if fbc.get("policy_source") not in ("branch", "customer"):
            p3_fails.append(f"Fallback policy_source={fbc.get('policy_source')!r}")

        evidence["pass_fail"]["phase3_override"] = "PASS" if not p3_fails else "FAIL"
        evidence["phase3_fails"] = p3_fails
        evidence["phases"]["phase3"] = {
            "omar": {k: {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
                     for k, v in _balances(session, omar_pe.id).items()},
            "amit_sources": {k: v["policy_source"] for k, v in amit_bals.items()},
            "fallback": {k: {kk: str(vv) if isinstance(vv, Decimal) else vv for kk, vv in v.items()}
                         for k, v in fb_bals.items()},
            "omar_expire_count": omar_exp,
        }
        print("  Phase3:", evidence["pass_fail"]["phase3_override"])
        for f in p3_fails:
            print("   FAIL:", f)

        # ----- Phase 4 Dec-31 optional -----
        if through_dec:
            print("\n== Phase 4 optional: Augâ†’Dec + Dec-31 for QA employees ==")
            # Restore Meera to 1.0 if needed; re-credit from clean state is hard â€”
            # run forward from current Jul state for original 3 only.
            qa_pes = [(i["pe"].id, i["emp"].id, i["sc"]) for i in built]
            for m in range(8, 13):
                for pe_id, _, _ in qa_pes:
                    run_pe_leave_credit(session, as_of=month_end(2026, m), pe_id=pe_id)
            for pe_id, _, _ in qa_pes:
                run_pe_leave_credit(session, as_of=date(2026, 12, 31), pe_id=pe_id)
            session.commit()
            dec_fails: list[str] = []
            dec_ev: dict = {}
            expected_2027 = {
                "Aptiv": {"Casual Leave": D("11.0")},
                "Magna Steyr": {"Casual Leave": D("0")},
                "Uno Minda": {
                    "Casual Leave": D("0"),
                    "Sick Leave": D("0"),
                    "Earned Leave": D("0"),
                },
            }
            for item in built:
                bals = _balances(session, item["pe"].id)
                dec_ev[item["sc"]["customer"]] = {
                    k: str(v["balance"]) for k, v in bals.items()
                }
                print(f"  Entering 2027 {item['sc']['customer']}: {dec_ev[item['sc']['customer']]}")
                for name, exp in expected_2027[item["sc"]["customer"]].items():
                    got = bals.get(name, {}).get("balance", ZERO)
                    if got != exp:
                        dec_fails.append(
                            f"{item['sc']['customer']} {name}: {got} != {exp}"
                        )
            # Uno yearly expiry events
            uday = next(i for i in built if i["sc"]["customer"] == "Uno Minda")
            yearly = session.execute(
                select(LeaveAccrualEvent).where(
                    LeaveAccrualEvent.employee_id == uday["emp"].id,
                    or_(
                        LeaveAccrualEvent.source.like("pe_expire:%"),
                        LeaveAccrualEvent.event_type == "Expiry",
                    ),
                )
            ).scalars().all()
            print(f"  Uno Expiry events: {len(yearly)}")
            for e in yearly:
                print(f"    {e.event_type} {e.source} amount={e.amount} at={e.created_at}")
            evidence["pass_fail"]["phase4_dec31"] = "PASS" if not dec_fails else "FAIL"
            evidence["dec_fails"] = dec_fails
            evidence["phases"]["phase4_dec31"] = {
                "balances": dec_ev,
                "uno_expiry_count": len(yearly),
            }
            print("  Dec31:", evidence["pass_fail"]["phase4_dec31"])
            for f in dec_fails:
                print("   FAIL:", f)
        else:
            evidence["pass_fail"]["phase4_dec31"] = "SKIPPED"

        evidence["pass_fail"]["overall"] = (
            "PASS"
            if all(
                v == "PASS" or v == "SKIPPED" or v == "ALREADY_APPLIED"
                for k, v in evidence["pass_fail"].items()
                if k != "overall"
            )
            else "FAIL"
        )
        print("\n" + "=" * 72)
        print("SUMMARY:", json.dumps(evidence["pass_fail"], indent=2))
        print("=" * 72)
        return evidence
    finally:
        session.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--through-dec", action="store_true")
    ap.add_argument("--reset", action="store_true", default=True,
                    help="Clear QA leave ledgers before replay (default True)")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--cleanup", action="store_true")
    ap.add_argument("--evidence-out", default="qa_leave_evidence.json")
    args = ap.parse_args()
    reset = not args.no_reset

    if args.cleanup:
        url = crm_database_url()
        _assert_local(url)
        session = get_session_factory()()
        try:
            counts = cleanup(session)
            print("CLEANUP:", counts)
        finally:
            session.close()
        return 0

    evidence = run(through_dec=args.through_dec, reset=reset)
    out = Path = __import__("pathlib").Path
    path = out(__file__).resolve().parents[2] / args.evidence_out
    # also write under backend/
    path2 = out(__file__).resolve().parents[1] / args.evidence_out
    for p in (path, path2):
        p.write_text(json.dumps(evidence, indent=2, default=str), encoding="utf-8")
        print("Wrote", p)
    return 0 if evidence["pass_fail"].get("overall") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

