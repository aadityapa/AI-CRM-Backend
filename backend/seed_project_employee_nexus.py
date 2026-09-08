"""Idempotent NEXUS Project Employee seed (UC-01..UC-12 scenario world).

Seeds: Avinash (EMP-001), Ranjeet (EMP-002); Karnex / Samsung / Microsoft
customers + leave policies; holiday calendars; Project X / Y; PO-100 / PO-200.

Does NOT auto-map employees to projects (use the CRM Map Employee UI or call
POST /api/projects/{id}/employees) so UC mapping flows stay exercisable.

Usage (from backend/):
  python seed_project_employee_nexus.py

Requires Postgres (CRM_DATABASE_URL / AUTH_DB_URL / DB_*). Run
`alembic upgrade head` and preferably `python seed_crm.py` first.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select, text

from crm_db import get_session_factory
from models.customers import Customer
from models.finance import POProjectAllocation, POStatus, PurchaseOrder
from models.hr import Employee
from models.leave import CustomerLeavePolicy, Holiday
from models.masters import LeavePolicyType
from models.opportunities import Opportunity, OppType, PipelineStage
from models.projects import Project

D = Decimal


def _get_or_create(session, model, defaults=None, **filters):
    obj = session.execute(select(model).filter_by(**filters)).scalar_one_or_none()
    if obj:
        return obj, False
    obj = model(**filters, **(defaults or {}))
    session.add(obj)
    session.flush()
    return obj, True


def _ensure_seed_user(session) -> int:
    """Opportunities.created_by needs a registration_data row; reuse lowest id."""
    row = session.execute(
        text("SELECT id FROM registration_data ORDER BY id ASC LIMIT 1")
    ).first()
    if row:
        return int(row[0])
    raise RuntimeError(
        "No registration_data users found. Create an HR/admin login first "
        "(or run the app once so auth_db initializes users)."
    )


def _ensure_leave_type(session) -> LeavePolicyType:
    lt = session.execute(
        select(LeavePolicyType).where(LeavePolicyType.name == "Earned Leave")
    ).scalar_one_or_none()
    if lt:
        return lt
    lt = session.execute(
        select(LeavePolicyType).where(LeavePolicyType.name == "Earned")
    ).scalar_one_or_none()
    if lt:
        return lt
    lt = session.execute(select(LeavePolicyType).limit(1)).scalar_one_or_none()
    if lt:
        return lt
    lt, _ = _get_or_create(
        session, LeavePolicyType,
        name="Earned Leave",
        defaults={"accrual_rule": "1.25 per month", "carry_forward_rule": "Carry forward up to 30 days"},
    )
    return lt


def _upsert_policy(session, customer: Customer, lt: LeavePolicyType, *, credit_type: str,
                   timing: str, per_period: str, initial: str = "0", carry=None,
                   expire: bool = False, prorate: bool = False) -> CustomerLeavePolicy:
    pol = session.execute(
        select(CustomerLeavePolicy).where(
            CustomerLeavePolicy.customer_id == customer.id,
            CustomerLeavePolicy.leave_type_id == lt.id,
            CustomerLeavePolicy.branch_id.is_(None),
        )
    ).scalar_one_or_none()
    fields = dict(
        leave_credit_type=credit_type,
        leave_credit_timing=timing,
        leave_credit_balance=D(per_period),
        initial_credit_balance=D(initial),
        maximum_carry_forward=(D(carry) if carry is not None else None),
        leave_expire=("Days" if expire else None),
        prorate_balance_credit=prorate,
        is_active=True,
    )
    if pol is None:
        pol = CustomerLeavePolicy(customer_id=customer.id, leave_type_id=lt.id, **fields)
        session.add(pol)
        session.flush()
        return pol
    for k, v in fields.items():
        setattr(pol, k, v)
    session.flush()
    return pol


def _ensure_holiday(session, customer: Customer, d: date, name: str) -> None:
    existing = session.execute(
        select(Holiday).where(
            Holiday.customer_id == customer.id,
            Holiday.holiday_date == d,
        )
    ).scalar_one_or_none()
    if existing:
        existing.name = name
        existing.is_active = True
        existing.year = d.year
        return
    session.add(Holiday(
        name=name, holiday_date=d, holiday_type="Customer",
        customer_id=customer.id, year=d.year, is_active=True,
    ))


def _ensure_project(session, customer: Customer, name: str, user_id: int) -> Project:
    proj = session.execute(
        select(Project).where(Project.name == name, Project.customer_id == customer.id)
    ).scalar_one_or_none()
    if proj:
        return proj
    opp_id = f"OPP-NEXUS-{name.replace(' ', '-').upper()}"
    opp, _ = _get_or_create(
        session, Opportunity,
        opp_id=opp_id,
        defaults={
            "title": name,
            "customer_id": customer.id,
            "opp_type": OppType.T_AND_M,
            "pipeline_stage": PipelineStage.ACTIVE,
            "created_by": user_id,
        },
    )
    proj = Project(opportunity_id=opp.id, customer_id=customer.id, name=name)
    session.add(proj)
    session.flush()
    return proj


def _ensure_po(session, customer: Customer, number: str, value: str, end: date) -> PurchaseOrder:
    po = session.execute(
        select(PurchaseOrder).where(PurchaseOrder.po_number == number)
    ).scalar_one_or_none()
    total = D(value)
    if po is None:
        po = PurchaseOrder(
            po_number=number, customer_id=customer.id, total_value=total,
            consumed_value=D("0"), balance_value=total,
            status=POStatus.ACTIVE, end_date=end, start_date=date(2026, 1, 1),
        )
        session.add(po)
        session.flush()
        return po
    po.customer_id = customer.id
    po.total_value = total
    po.end_date = end
    po.status = POStatus.ACTIVE
    # Preserve consumption if already drawn; recompute balance.
    consumed = D(po.consumed_value or 0)
    po.balance_value = total - consumed
    session.flush()
    return po


def _ensure_alloc(session, po: PurchaseOrder, project: Project, amount: str) -> None:
    a = session.execute(
        select(POProjectAllocation).where(
            POProjectAllocation.po_id == po.id,
            POProjectAllocation.project_id == project.id,
        )
    ).scalar_one_or_none()
    if a is None:
        session.add(POProjectAllocation(
            po_id=po.id, project_id=project.id,
            allocated_amount=D(amount), consumed_amount=D("0"),
        ))
        session.flush()
        return
    a.allocated_amount = D(amount)


def seed() -> dict:
    session = get_session_factory()()
    summary: dict = {"created": [], "updated": []}
    try:
        user_id = _ensure_seed_user(session)
        lt = _ensure_leave_type(session)

        karnex, new = _get_or_create(session, Customer, name="Karnex")
        summary["created" if new else "updated"].append("Customer:Karnex")
        samsung, new = _get_or_create(session, Customer, name="Samsung")
        summary["created" if new else "updated"].append("Customer:Samsung")
        microsoft, new = _get_or_create(session, Customer, name="Microsoft")
        summary["created" if new else "updated"].append("Customer:Microsoft")

        _upsert_policy(session, karnex, lt, credit_type="Monthly", timing="End_Of_Period",
                       per_period="1.0", carry="10", expire=False, prorate=False)
        summary["updated"].append("POL-KARNEX")
        _upsert_policy(session, samsung, lt, credit_type="Monthly", timing="End_Of_Period",
                       per_period="1.5", carry="5", expire=True, prorate=True)
        summary["updated"].append("POL-SAMSUNG")
        _upsert_policy(session, microsoft, lt, credit_type="One_Time", timing="Start_Of_Period",
                       per_period="18", prorate=True)
        summary["updated"].append("POL-MSFT")

        # Holidays
        _ensure_holiday(session, samsung, date(2026, 7, 17), "Samsung Holiday")
        _ensure_holiday(session, samsung, date(2026, 8, 15), "Independence Day")
        _ensure_holiday(session, microsoft, date(2026, 7, 4), "Independence Day (US)")
        _ensure_holiday(session, microsoft, date(2026, 7, 24), "Microsoft Holiday")
        _ensure_holiday(session, microsoft, date(2026, 8, 15), "Independence Day")
        _ensure_holiday(session, karnex, date(2026, 8, 15), "Independence Day")
        summary["updated"].append("holidays")

        proj_x = _ensure_project(session, samsung, "Project X", user_id)
        proj_y = _ensure_project(session, microsoft, "Project Y", user_id)
        summary["updated"].extend(["Project X", "Project Y"])

        po100 = _ensure_po(session, samsung, "PO-100", "1000000", date(2026, 12, 31))
        po200 = _ensure_po(session, microsoft, "PO-200", "200000", date(2026, 9, 30))
        _ensure_alloc(session, po100, proj_x, "1000000")
        _ensure_alloc(session, po200, proj_y, "200000")
        summary["updated"].extend(["PO-100", "PO-200"])

        avinash, new = _get_or_create(
            session, Employee,
            email="avinash@karnex.in",
            defaults={
                "first_name": "Avinash",
                "employee_code": "EMP-001",
                "is_active": True,
            },
        )
        if not new:
            avinash.first_name = "Avinash"
            avinash.employee_code = avinash.employee_code or "EMP-001"
        summary["created" if new else "updated"].append("EMP-001 Avinash")

        ranjeet, new = _get_or_create(
            session, Employee,
            email="ranjeet@karnex.in",
            defaults={
                "first_name": "Ranjeet",
                "employee_code": "EMP-002",
                "is_active": True,
            },
        )
        if not new:
            ranjeet.first_name = "Ranjeet"
            ranjeet.employee_code = ranjeet.employee_code or "EMP-002"
        summary["created" if new else "updated"].append("EMP-002 Ranjeet")

        session.commit()
        print("NEXUS Project Employee seed complete.")
        print(f"  customers: Karnex={karnex.id}, Samsung={samsung.id}, Microsoft={microsoft.id}")
        print(f"  projects:  X={proj_x.id}, Y={proj_y.id}")
        print(f"  POs:       PO-100={po100.id}, PO-200={po200.id}")
        print(f"  employees: Avinash={avinash.id} (EMP-001), Ranjeet={ranjeet.id} (EMP-002)")
        print("  Map employees via CRM -> Project Employees -> Map employee to exercise UC flows.")
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    seed()
