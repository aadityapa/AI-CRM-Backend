#!/usr/bin/env python3
"""One-off reconciliation: move negative paid leave balances to Loss of Pay.

Dry-run by default. Pass --apply to mutate.

For each PE leave detail / employee leave balance row that is a paid type
(not Comp-Off, not already Loss of Pay) with balance < 0:

  * Floor balance at 0
  * Record the over-drawn amount as Loss of Pay consumption via LeaveAccrualEvent
    (source reconcile:negative-leave:{scope}:{id})

Usage:
  cd backend
  python scripts/reconcile_negative_leave_balances.py
  python scripts/reconcile_negative_leave_balances.py --apply
  python scripts/reconcile_negative_leave_balances.py --apply --pe-id 12
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from crm_db import get_session_factory  # noqa: E402
from models import (  # noqa: E402
    EmployeeLeaveBalance, LeaveAccrualEvent, LeavePolicyType,
    ProjectEmployeeLeaveDetail,
)
from services.employees import (  # noqa: E402
    ensure_loss_of_pay_type, is_comp_off_name, is_loss_of_pay_name,
)

ZERO = Decimal("0")


def _reconcile_pe(db, *, pe_id: int | None, apply: bool) -> list[dict]:
    lop = ensure_loss_of_pay_type(db)
    q = (
        select(ProjectEmployeeLeaveDetail, LeavePolicyType)
        .join(LeavePolicyType, LeavePolicyType.id == ProjectEmployeeLeaveDetail.leave_type_id)
    )
    if pe_id is not None:
        q = q.where(ProjectEmployeeLeaveDetail.project_employee_id == pe_id)
    rows = db.execute(q).all()
    actions: list[dict] = []
    for row, lt in rows:
        name = lt.name or ""
        if is_comp_off_name(name) or is_loss_of_pay_name(name):
            continue
        bal = Decimal(row.leave_balance or 0)
        if bal >= ZERO:
            continue
        over = -bal
        action = {
            "scope": "pe",
            "project_employee_id": row.project_employee_id,
            "leave_type_id": row.leave_type_id,
            "leave_type_name": name,
            "overdrawn": float(over),
            "balance_before": float(bal),
            "balance_after": 0.0,
            "lop_type_id": lop.id,
        }
        actions.append(action)
        if not apply:
            continue
        row.leave_balance = ZERO
        # Track overdraw as LOP consumed on a LOP PE detail row.
        lop_row = db.execute(
            select(ProjectEmployeeLeaveDetail).where(
                ProjectEmployeeLeaveDetail.project_employee_id == row.project_employee_id,
                ProjectEmployeeLeaveDetail.leave_type_id == lop.id,
            )
        ).scalars().first()
        if lop_row is None:
            lop_row = ProjectEmployeeLeaveDetail(
                project_employee_id=row.project_employee_id,
                leave_type_id=lop.id,
                initial_balance=0,
                opening_balance=0,
                leave_accrual=0,
                leave_consumed=0,
                leave_balance=0,
            )
            db.add(lop_row)
            db.flush()
        lop_row.leave_consumed = Decimal(lop_row.leave_consumed or 0) + over
        # Resolve employee_id for the ledger
        from models import ProjectEmployee
        pe = db.get(ProjectEmployee, row.project_employee_id)
        emp_id = pe.employee_id if pe else None
        if emp_id is not None:
            db.add(LeaveAccrualEvent(
                employee_id=emp_id,
                leave_type_id=lop.id,
                event_type="Consumption",
                amount=-over,
                balance_after=Decimal(lop_row.leave_balance or 0),
                source=f"reconcile:negative-leave:pe:{row.id}",
                note=(
                    f"Reconcile negative {name}: moved {float(over):g} day(s) "
                    f"to Loss of Pay (PE#{row.project_employee_id})"
                ),
            ))
    return actions


def _reconcile_employee(db, *, apply: bool) -> list[dict]:
    lop = ensure_loss_of_pay_type(db)
    rows = db.execute(
        select(EmployeeLeaveBalance, LeavePolicyType)
        .join(LeavePolicyType, LeavePolicyType.id == EmployeeLeaveBalance.leave_type_id)
    ).all()
    actions: list[dict] = []
    for bal_row, lt in rows:
        name = lt.name or ""
        if is_comp_off_name(name) or is_loss_of_pay_name(name):
            continue
        bal = Decimal(bal_row.balance or 0)
        if bal >= ZERO:
            continue
        over = -bal
        action = {
            "scope": "employee",
            "employee_id": bal_row.employee_id,
            "year": bal_row.year,
            "leave_type_id": bal_row.leave_type_id,
            "leave_type_name": name,
            "overdrawn": float(over),
            "balance_before": float(bal),
            "balance_after": 0.0,
            "lop_type_id": lop.id,
        }
        actions.append(action)
        if not apply:
            continue
        # Move consumed onto LOP year row; floor paid balance.
        bal_row.consumed = Decimal(bal_row.consumed or 0) - over
        if Decimal(bal_row.consumed or 0) < ZERO:
            bal_row.consumed = ZERO
        bal_row.balance = (
            Decimal(bal_row.accrued or 0)
            + Decimal(bal_row.carry_forward or 0)
            - Decimal(bal_row.consumed or 0)
        )
        if Decimal(bal_row.balance or 0) < ZERO:
            bal_row.balance = ZERO

        lop_bal = db.execute(
            select(EmployeeLeaveBalance).where(
                EmployeeLeaveBalance.employee_id == bal_row.employee_id,
                EmployeeLeaveBalance.leave_type_id == lop.id,
                EmployeeLeaveBalance.year == bal_row.year,
            )
        ).scalars().first()
        if lop_bal is None:
            lop_bal = EmployeeLeaveBalance(
                employee_id=bal_row.employee_id,
                leave_type_id=lop.id,
                year=bal_row.year,
                accrued=0, consumed=0, balance=0, carry_forward=0,
            )
            db.add(lop_bal)
            db.flush()
        lop_bal.consumed = Decimal(lop_bal.consumed or 0) + over
        lop_bal.balance = (
            Decimal(lop_bal.accrued or 0)
            + Decimal(lop_bal.carry_forward or 0)
            - Decimal(lop_bal.consumed or 0)
        )
        db.add(LeaveAccrualEvent(
            employee_id=bal_row.employee_id,
            leave_type_id=lop.id,
            event_type="Consumption",
            amount=-over,
            balance_after=Decimal(lop_bal.balance or 0),
            source=f"reconcile:negative-leave:emp:{bal_row.id}",
            note=(
                f"Reconcile negative {name}: moved {float(over):g} day(s) "
                f"to Loss of Pay (employee #{bal_row.employee_id} / {bal_row.year})"
            ),
        ))
    return actions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile negative paid leave balances → Loss of Pay",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Persist changes (default is dry-run)",
    )
    parser.add_argument("--pe-id", type=int, default=None, help="Limit PE reconcile to one id")
    parser.add_argument(
        "--skip-employee", action="store_true",
        help="Only reconcile PE leave details",
    )
    args = parser.parse_args()
    db = get_session_factory()()
    try:
        pe_actions = _reconcile_pe(db, pe_id=args.pe_id, apply=args.apply)
        emp_actions: list[dict] = []
        if not args.skip_employee and args.pe_id is None:
            emp_actions = _reconcile_employee(db, apply=args.apply)
        if args.apply:
            db.commit()
        else:
            db.rollback()
    finally:
        db.close()

    summary = {
        "dry_run": not args.apply,
        "pe_actions": pe_actions,
        "employee_actions": emp_actions,
        "pe_count": len(pe_actions),
        "employee_count": len(emp_actions),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
